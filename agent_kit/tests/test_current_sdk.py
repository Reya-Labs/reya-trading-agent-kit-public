"""Exercise kit commands through the real SDK's signing and wire-model boundary.

Only network responses are stubbed. Renamed methods, stale enum values and
missing signing fields must fail here, before an operator uses a live account.
"""

from unittest.mock import AsyncMock

import pytest
from eth_account import Account
from sdk.open_api.models.account import Account as TradingAccount
from sdk.open_api.models.cancel_order_response import CancelOrderResponse
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.reya_rest_api import ReyaTradingClient
from sdk.reya_rest_api.config import DEVNET1_ORDERS_GATEWAY, TradingConfig

from agent_kit.scripts import trade
from agent_kit.tests.conftest import make_balance, make_market_def, make_order, make_position, make_price, parse_stdout


@pytest.fixture
async def signed_client(monkeypatch):
    # Public deterministic test key only; no .env and no live connectivity.
    key = "0x" + "11" * 32
    config = TradingConfig(
        api_url="https://invalid.example/v2",
        chain_id=89346162,
        owner_wallet_address=Account.from_key(key).address,
        private_key=key,
        account_id=8009,
        orders_gateway_address=DEVNET1_ORDERS_GATEWAY,
        dex_id_override=1,
    )
    client = ReyaTradingClient(config)
    monkeypatch.setattr(client.reference, "get_perp_market_definitions", AsyncMock(return_value=[make_market_def()]))
    monkeypatch.setattr(client.reference, "get_spot_market_definitions", AsyncMock(return_value=[]))
    monkeypatch.setattr(client.markets, "get_perp_market_summary", AsyncMock(return_value=make_price()))
    monkeypatch.setattr(
        client, "get_accounts", AsyncMock(return_value=[TradingAccount(account_id=8009, name="test", type="SUBPERP")])
    )
    monkeypatch.setattr(client, "get_positions", AsyncMock(return_value=[make_position()]))
    monkeypatch.setattr(client, "get_account_balances", AsyncMock(return_value=[make_balance(real_balance="1000")]))
    monkeypatch.setattr(
        client.orders,
        "create_order",
        AsyncMock(return_value=CreateOrderResponse(order_id="123456", status="OPEN", cum_qty="0")),
    )
    monkeypatch.setattr(trade, "get_trading_config", lambda **kwargs: config)
    monkeypatch.setattr(trade, "ReyaTradingClient", lambda config: client)
    yield client
    await client.close()


@pytest.mark.parametrize("command,type_", [("tp", "TAKE_PROFIT"), ("sl", "STOP_LOSS")])
@pytest.mark.parametrize("tif", ["IOC", "GTC"])
async def test_trigger_commands_sign_current_wire_contract(signed_client, command, type_, tif, capsys):
    trigger = "2300" if command == "tp" else "1800"
    args = trade.build_parser().parse_args(
        [
            "order",
            command,
            "ETHRUSDPERP",
            "--side",
            "long",
            "--trigger_px",
            trigger,
            "--limit_px",
            trigger,
            "--time_in_force",
            tif,
        ]
    )
    await getattr(trade, "cmd_order_" + command)(args)
    request = signed_client.orders.create_order.call_args.kwargs["create_order_request"].to_dict()
    assert request["orderType"] == type_
    assert request["accountId"] == 8009
    assert request["exchangeId"] == 1
    assert request["isBuy"] is False
    assert request["timeInForce"] == tif
    assert request["triggerPx"] == request["limitPx"] == trigger
    assert len(request["signature"]) == 132
    assert {"qty", "reduceOnly", "postOnly", "expiresAfter"}.isdisjoint(request)
    assert parse_stdout(capsys)["status"] == "OPEN"


async def test_limit_and_cancel_use_current_account_and_symbol(signed_client, monkeypatch, capsys):
    args = trade.build_parser().parse_args(
        ["order", "limit", "ETHRUSDPERP", "--side", "long", "--qty", "0.05", "--price", "1900"]
    )
    await trade.cmd_order_limit(args)
    request = signed_client.orders.create_order.call_args.kwargs["create_order_request"].to_dict()
    assert (request["qty"], request["limitPx"], request["timeInForce"], request["orderType"]) == (
        "0.05",
        "1900",
        "GTC",
        "LIMIT",
    )
    parse_stdout(capsys)
    monkeypatch.setattr(signed_client, "get_open_orders", AsyncMock(return_value=[make_order(order_id="123456")]))
    cancel = AsyncMock(return_value=CancelOrderResponse(order_id="123456", status="CANCELLED"))
    monkeypatch.setattr(signed_client.orders, "cancel_order", cancel)
    args = trade.build_parser().parse_args(["order", "cancel", "--order_id", "123456"])
    await trade.cmd_order_cancel(args)
    request = cancel.call_args.args[0].to_dict()
    assert (request["accountId"], request["symbol"], request["orderId"]) == (8009, "ETHRUSDPERP", "123456")
    assert len(request["signature"]) == 132
    assert parse_stdout(capsys)["status"] == "CANCELLED"
