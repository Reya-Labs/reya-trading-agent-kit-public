"""Mainnet config, unsigned inspection and real signing, with all I/O stubbed."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from sdk.open_api.api.reference_data_api import ReferenceDataApi
from sdk.open_api.api.wallet_data_api import WalletDataApi
from sdk.open_api.models.account import Account as TradingAccount
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.pagination_meta import PaginationMeta
from sdk.open_api.models.perp_execution_list import PerpExecutionList
from sdk.reya_rest_api import ReyaTradingClient

from agent_kit.scripts import query, trade
from agent_kit.scripts._env import get_trading_config
from agent_kit.scripts._v2 import V2Client, V2StatusError
from agent_kit.tests.conftest import make_balance, make_market_def, make_order, make_position, make_price, parse_stdout

# Public test identities only. Owner and delegated signer deliberately differ.
OWNER = "0x" + "22" * 20
TEST_KEY = "0x" + "11" * 32
GATEWAY = "0xfc8c96be87da63cecddbf54abfa7b13ee8044739"


@pytest.fixture
def mainnet_env(monkeypatch):
    for key, value in {
        "CHAIN_ID": "1729",
        "REYA_API_URL": "https://api.reya.xyz/v2",
        "REYA_ORDERS_GATEWAY": GATEWAY,
        "REYA_DEX_ID": "2",
        "PERP_ACCOUNT_ID_1": "8009",
        "PERP_WALLET_ADDRESS_1": OWNER,
    }.items():
        monkeypatch.setenv(key, value)


def test_explicit_mainnet_config_and_missing_key(mainnet_env):
    config = get_trading_config()
    assert config.api_url == "https://api.reya.xyz/v2"
    assert config.chain_id == 1729
    assert config.default_orders_gateway_address == GATEWAY
    assert config.dex_id == 2
    assert config.account_id == 8009 and config.owner_wallet_address == OWNER
    assert config.private_key is None
    with pytest.raises(ValueError, match="Private key is required"):
        ReyaTradingClient(config)  # writes cannot initialize without a signer


@pytest.mark.parametrize(
    "variable,value", [("CHAIN_ID", "89346162"), ("REYA_ORDERS_GATEWAY", "0x7Ec89E555c771D2B5939aBE5C4E4291852633D4D")]
)
def test_mainnet_rejects_mixed_deployment_settings(mainnet_env, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match="different deployments|does not match"):
        get_trading_config()


@pytest.mark.parametrize(
    "command,method,data",
    [
        ("positions", "get_wallet_positions", [make_position(), make_position(account_id=8010)]),
        ("orders", "get_wallet_open_orders", [make_order(), make_order(account_id=8010)]),
        ("balances", "get_wallet_account_balances", [make_balance(), make_balance(account_id=8010)]),
    ],
)
async def test_account_reads_need_no_key_and_use_owner(mainnet_env, monkeypatch, capsys, command, method, data):
    accounts = AsyncMock(return_value=[TradingAccount(account_id=8009, name="test", type="SUBPERP")])
    read = AsyncMock(return_value=data)
    monkeypatch.setattr(WalletDataApi, "get_wallet_accounts", accounts)
    monkeypatch.setattr(WalletDataApi, method, read)
    # `positions` also reads the API's valuation for its optional figures; keep it unavailable.
    monkeypatch.setattr(V2Client, "get", AsyncMock(side_effect=V2StatusError("/wallet/x/positionRisk", 503)))
    # `positions` reads market definitions once, for the hours warning.
    monkeypatch.setattr(ReferenceDataApi, "get_perp_market_definitions", AsyncMock(return_value=[]))
    await getattr(query, "cmd_" + command)(query.build_parser().parse_args([command]))
    assert len(parse_stdout(capsys)) == 1
    accounts.assert_awaited_once_with(address=OWNER)
    read.assert_awaited_once_with(address=OWNER)


async def test_wrong_owner_is_not_reported_as_flat(mainnet_env, monkeypatch):
    monkeypatch.setattr(WalletDataApi, "get_wallet_accounts", AsyncMock(return_value=[]))
    read = AsyncMock()
    monkeypatch.setattr(WalletDataApi, "get_wallet_positions", read)
    with pytest.raises(ValueError, match="does not belong"):
        await query.cmd_positions(query.build_parser().parse_args(["positions"]))
    read.assert_not_awaited()


@pytest.mark.parametrize(
    "command,args",
    [
        ("cmd_order_market", ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "0.01"]),
        ("cmd_order_limit", ["order", "limit", "ETHRUSDPERP", "--side", "long", "--qty", "0.01", "--price", "1900"]),
        ("cmd_order_cancel", ["order", "cancel", "--order_id", "123456"]),
        ("cmd_order_cancel_all", ["order", "cancel_all", "--all"]),
        (
            "cmd_order_tp",
            [
                "order",
                "tp",
                "ETHRUSDPERP",
                "--side",
                "long",
                "--trigger_px",
                "2300",
                "--limit_px",
                "2300",
                "--time_in_force",
                "IOC",
            ],
        ),
        (
            "cmd_order_sl",
            [
                "order",
                "sl",
                "ETHRUSDPERP",
                "--side",
                "long",
                "--trigger_px",
                "1800",
                "--limit_px",
                "1800",
                "--time_in_force",
                "IOC",
            ],
        ),
        ("cmd_positions_close_all", ["positions", "close_all", "--all"]),
    ],
)
async def test_wrong_owner_blocks_write(patch_client, command, args):
    patch_client.get_accounts.return_value = []
    with pytest.raises(ValueError, match="does not belong"):
        await getattr(trade, command)(trade.build_parser().parse_args(args))
    patch_client.create_limit_order.assert_not_awaited()
    patch_client.create_trigger_order.assert_not_awaited()
    patch_client.cancel_order.assert_not_awaited()


@pytest.mark.parametrize("operation", ["market", "limit", "tp", "sl", "cancel", "close"])
async def test_mainnet_signing_envelope(mainnet_env, monkeypatch, capsys, operation):
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", TEST_KEY)
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    config = get_trading_config()
    client = ReyaTradingClient(config)
    # Capture the actual SDK typed message while still computing a real signature.
    signing = MagicMock(wraps=Account.sign_typed_data)
    monkeypatch.setattr(Account, "sign_typed_data", signing)
    monkeypatch.setattr(trade, "ReyaTradingClient", lambda config: client)
    monkeypatch.setattr(client.reference, "get_perp_market_definitions", AsyncMock(return_value=[make_market_def()]))
    monkeypatch.setattr(client.reference, "get_spot_market_definitions", AsyncMock(return_value=[]))
    monkeypatch.setattr(client.reference, "get_fee_tier_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(client.markets, "get_perp_market_summary", AsyncMock(return_value=make_price()))
    monkeypatch.setattr(
        client, "get_accounts", AsyncMock(return_value=[TradingAccount(account_id=8009, name="test", type="SUBPERP")])
    )
    monkeypatch.setattr(client, "get_positions", AsyncMock(return_value=[make_position()]))
    # An entry without --sl/--tp reads open orders for a stop already armed on its market.
    monkeypatch.setattr(client, "get_open_orders", AsyncMock(return_value=[]))
    monkeypatch.setattr(client, "get_account_balances", AsyncMock(return_value=[make_balance(real_balance="1000")]))
    response = CreateOrderResponse(order_id="123456", status="FILLED", cum_qty="0.05")
    monkeypatch.setattr(
        client.wallet,
        "get_wallet_perp_executions",
        AsyncMock(return_value=PerpExecutionList(data=[], meta=PaginationMeta(limit=100, count=0))),
    )
    create = AsyncMock(return_value=response)
    monkeypatch.setattr(client.orders, "create_order", create)
    if operation == "cancel":
        from sdk.open_api.models.cancel_order_response import CancelOrderResponse

        monkeypatch.setattr(client, "get_open_orders", AsyncMock(return_value=[make_order(order_id="123456")]))
        monkeypatch.setattr(
            client.orders,
            "cancel_order",
            AsyncMock(return_value=CancelOrderResponse(order_id="123456", status="CANCELLED")),
        )
        args = ["order", "cancel", "--order_id", "123456"]
        fn = trade.cmd_order_cancel
    elif operation == "close":
        client.get_positions.side_effect = [[make_position()], []]
        monkeypatch.setattr(client, "get_open_orders", AsyncMock(return_value=[]))
        args = ["positions", "close_all", "--symbol", "ETHRUSDPERP"]
        fn = trade.cmd_positions_close_all
    else:
        args = ["order", operation, "ETHRUSDPERP", "--side", "long"]
        if operation in ["market", "limit"]:
            args += ["--qty", "0.05"]
            if operation == "limit":
                args += ["--price", "1900", "--post_only", "--client_order_id", "18446744073709551615"]
        else:
            trigger = "2300" if operation == "tp" else "1800"
            args += ["--trigger_px", trigger, "--limit_px", trigger, "--time_in_force", "IOC"]
            create.return_value = CreateOrderResponse(order_id="123456", status="OPEN", cum_qty="0")
        fn = getattr(trade, "cmd_order_" + operation)
    await fn(trade.build_parser().parse_args(args))
    assert "error" not in parse_stdout(capsys)
    signing.assert_called_once()
    _, domain, types, message = signing.call_args.args
    assert domain == {"name": "Reya", "version": "1", "verifyingContract": GATEWAY}
    assert message["verifyingChainId"] == 1729
    details = message["cancel" if operation == "cancel" else "order"]
    assert details["accountId"] == 8009 and details["marketId"] == 1
    if operation != "cancel":
        if operation == "limit":
            assert details["postOnly"] is True
            assert details["clientOrderId"] == 18446744073709551615
            payload = create.call_args.kwargs["create_order_request"].to_dict()
            assert payload["clientOrderId"] == "18446744073709551615"
            assert payload["postOnly"] is True
        assert details["exchangeId"] == 2
        assert details["signer"] == Account.from_key(TEST_KEY).address
        assert details["signer"].lower() != OWNER.lower()
        request = create.call_args.kwargs["create_order_request"].to_dict()
        assert request["signerWallet"] == details["signer"]
        assert details["reduceOnly"] is (operation == "close")
        assert details["quantity"] == (
            -((1 << 255) - 1) if operation in ["tp", "sl"] else (-5 * 10**16 if operation == "close" else 5 * 10**16)
        )
    else:
        assert details["orderId"] == 123456 and details["clOrdId"] == 0
        request = client.orders.cancel_order.call_args.args[0].to_dict()
    signature = request["signature"]
    signer = Account.from_key(TEST_KEY).address
    signed = encode_typed_data(domain_data=domain, message_types=types, message_data=message)
    assert Account.recover_message(signed, signature=signature) == signer
    # Same signature must not authorize another chain or gateway.
    for other_domain, other_message in [
        (domain, {**message, "verifyingChainId": 89346162}),
        ({**domain, "verifyingContract": "0x7Ec89E555c771D2B5939aBE5C4E4291852633D4D"}, message),
    ]:
        other = encode_typed_data(domain_data=other_domain, message_types=types, message_data=other_message)
        assert Account.recover_message(other, signature=signature) != signer


async def test_unsigned_margin_reads_the_api_valuation_of_the_owners_account(mainnet_env, monkeypatch, capsys):
    from agent_kit.tests.conftest import make_account_summary, make_position_risk

    monkeypatch.setattr(
        WalletDataApi,
        "get_wallet_accounts",
        AsyncMock(return_value=[TradingAccount(account_id=8009, name="test", type="SUBPERP")]),
    )
    monkeypatch.setattr(
        WalletDataApi, "get_wallet_account_balances", AsyncMock(return_value=[make_balance(real_balance="100")])
    )
    monkeypatch.setattr(ReferenceDataApi, "get_perp_market_definitions", AsyncMock(return_value=[make_market_def()]))
    served = {
        f"/wallet/{OWNER}/accountSummaries": [make_account_summary(collateral_value="100", unrealized_pnl="0.5")],
        f"/wallet/{OWNER}/positionRisk": [make_position_risk(mark_price="2010")],
    }
    seen = []

    async def get(self, path, **params):
        assert self.api_url == "https://api.reya.xyz/v2"
        seen.append((path, params))
        return served[path]

    monkeypatch.setattr(V2Client, "get", get)
    await query.cmd_margin(query.build_parser().parse_args(["margin"]))
    result = parse_stdout(capsys)
    assert result["equity_usd"] == 100.5 and result["positions"][0]["mark_price_usd"] == 2010
    assert sorted(seen) == [(path, {"accountId": 8009}) for path in sorted(served)]


def test_mainnet_default_exchange_is_not_import_time_devnet_value(mainnet_env, monkeypatch):
    monkeypatch.delenv("REYA_DEX_ID")
    monkeypatch.setattr("sdk.reya_rest_api.config.REYA_DEX_ID", 1)
    assert get_trading_config().dex_id == 2
