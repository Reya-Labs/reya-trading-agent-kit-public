"""Short symbols, client IDs, position PnL estimates, fee reads, error envelopes, cancel-then-close,
paper/live command parity and the inline-key permission warning."""

from types import SimpleNamespace

import argparse
import asyncio
import json
import os
from decimal import Decimal
from unittest.mock import AsyncMock, create_autospec

import pytest
from sdk.open_api.api.wallet_data_api import WalletDataApi
from sdk.open_api.exceptions import ApiException
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.fee_tier_parameters import FeeTierParameters
from sdk.open_api.models.global_fee_parameters import GlobalFeeParameters
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.side import Side
from sdk.open_api.models.wallet_configuration import WalletConfiguration

from agent_kit.scripts import data, doctor, paper, trade
from agent_kit.scripts._common import exception_result, symbol_arg
from agent_kit.scripts._http import RegionUnavailableError
from agent_kit.scripts._v2 import V2Client
from agent_kit.scripts.query import cmd_fees, cmd_positions
from agent_kit.tests.conftest import (
    DEFAULT_ACCOUNT_ID,
    FakeClient,
    make_market_def,
    make_market_summary,
    make_order,
    make_position,
    make_price,
    parse_stdout,
)

REGION_PAGE = "<html><body><h1>Not available in your region</h1></body></html>"


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("eth", "ETHRUSDPERP"),
        (" Btc ", "BTCRUSDPERP"),
        ("1000pepe", "1000PEPERUSDPERP"),
        ("ETHRUSDPERP", "ETHRUSDPERP"),
        ("ethrusdperp", "ETHRUSDPERP"),
        ("RUSD", "RUSD"),
        ("WETHRUSD", "WETHRUSD"),
        ("ETH-PERP", "ETH-PERP"),
    ],
)
def test_symbol_arg_expands_only_bare_tickers(text, expected):
    assert symbol_arg(text) == expected


def test_trade_and_data_parsers_expand_tickers_but_search_stays_literal():
    args = trade.build_parser().parse_args(["order", "market", "eth", "--side", "long", "--qty", "0.01"])
    assert args.symbol == "ETHRUSDPERP"
    args = trade.build_parser().parse_args(["positions", "close_all", "--symbol", "btc"])
    assert args.symbol == "BTCRUSDPERP"
    assert data.build_parser().parse_args(["market", "book", "--symbol", "sol"]).symbol == "SOLRUSDPERP"
    assert data.build_parser().parse_args(["market", "search", "--symbol", "eth"]).symbol == "eth"


# --------------------------------------------------------------------------
# Client order IDs
# --------------------------------------------------------------------------


def _market_args(**overrides):
    values = dict(
        symbol="ETHRUSDPERP",
        side="long",
        qty=Decimal("0.05"),
        notional=None,
        slippage=Decimal("0.01"),
        reduce_only=False,
        force=False,
        client_order_id=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _arm_market(client: FakeClient, response: CreateOrderResponse | Exception) -> None:
    client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    if isinstance(response, Exception):
        client.create_limit_order.side_effect = response
    else:
        client.create_limit_order.return_value = response


async def test_create_without_client_id_generates_one_and_reports_it(patch_client: FakeClient, capsys):
    _arm_market(patch_client, CreateOrderResponse(order_id="1", status="FILLED", cum_qty="0.05", avg_fill_px="2000"))
    args = _market_args()
    await trade.cmd_order_market(args)

    sent = patch_client.create_limit_order.call_args.args[0].client_order_id
    assert isinstance(sent, int) and 1 <= sent < 2**64
    assert args.client_order_id == sent
    # The venue did not echo it, so the kit reports the ID it sent.
    assert parse_stdout(capsys)["client_order_id"] == str(sent)


async def test_explicit_client_id_is_kept(patch_client: FakeClient, capsys):
    _arm_market(
        patch_client,
        CreateOrderResponse(order_id="1", client_order_id="42", status="FILLED", cum_qty="0.05", avg_fill_px="2000"),
    )
    await trade.cmd_order_market(_market_args(client_order_id=42))
    assert patch_client.create_limit_order.call_args.args[0].client_order_id == 42
    assert parse_stdout(capsys)["client_order_id"] == "42"


async def test_failed_create_still_reports_the_client_id(patch_client: FakeClient):
    _arm_market(patch_client, asyncio.TimeoutError())
    args = _market_args()
    with pytest.raises(asyncio.TimeoutError) as raised:
        await trade.cmd_order_market(args)
    args.group, args.action = "order", "market"
    result = trade._error_result(args, raised.value)
    assert result["client_order_id"] == str(args.client_order_id)
    # Non-create commands never get one.
    assert "client_order_id" not in trade._error_result(
        argparse.Namespace(group="order", action="cancel_all"), ValueError("x")
    )


# --------------------------------------------------------------------------
# Position PnL estimate
# --------------------------------------------------------------------------


async def test_positions_add_estimated_pnl_at_the_mark(patch_client: FakeClient, capsys):
    patch_client.get_positions.return_value = [
        make_position(symbol="ETHRUSDPERP", side=Side.B, qty="0.05", avg_entry_price="2000"),
        make_position(symbol="BTCRUSDPERP", side=Side.A, qty="0.01", avg_entry_price="60000"),
    ]
    patch_client.markets.get_perp_markets_summary.return_value = [
        make_market_summary(symbol="ETHRUSDPERP", mark_price="2100"),
        make_market_summary(symbol="BTCRUSDPERP", mark_price="61000"),
    ]
    await cmd_positions(argparse.Namespace(symbol=None))
    by_symbol = {row["symbol"]: row for row in parse_stdout(capsys)}
    assert Decimal(by_symbol["ETHRUSDPERP"]["estimated_unrealized_pnl_usd"]) == Decimal("5")
    assert Decimal(by_symbol["BTCRUSDPERP"]["estimated_unrealized_pnl_usd"]) == Decimal("-10")
    assert by_symbol["ETHRUSDPERP"]["mark_price"] == "2100"
    assert "mark_unavailable_reason" not in by_symbol["ETHRUSDPERP"]


async def test_missing_mark_leaves_estimate_null_not_zero(patch_client: FakeClient, capsys):
    patch_client.get_positions.return_value = [make_position(symbol="ETHRUSDPERP")]
    patch_client.markets.get_perp_markets_summary.return_value = [
        make_market_summary(symbol="ETHRUSDPERP", mark_price=None)
    ]
    await cmd_positions(argparse.Namespace(symbol=None))
    assert parse_stdout(capsys)[0]["estimated_unrealized_pnl_usd"] is None


async def test_mark_read_failure_keeps_positions(patch_client: FakeClient, capsys):
    patch_client.get_positions.return_value = [make_position(symbol="ETHRUSDPERP", qty="0.05")]
    patch_client.markets.get_perp_markets_summary.side_effect = ApiException(status=503, body="down")
    await cmd_positions(argparse.Namespace(symbol=None))
    row = parse_stdout(capsys)[0]
    assert row["qty"] == "0.05"
    assert row["estimated_unrealized_pnl_usd"] is None
    assert row["mark_unavailable_reason"]


# --------------------------------------------------------------------------
# Fee tiers
# --------------------------------------------------------------------------


def _fee_apis(patch_reference_api):
    patch_reference_api.get_fee_tier_parameters.return_value = [
        FeeTierParameters(tier_id=0, taker_fee="0.0003", maker_fee="0", volume30d="0", tier_type="REGULAR"),
        FeeTierParameters(tier_id=1, taker_fee="0.00028", maker_fee="0", volume30d="1000000", tier_type="REGULAR"),
    ]
    patch_reference_api.get_global_fee_parameters.return_value = GlobalFeeParameters(
        og_discount="0.1", referee_discount="0.05", referrer_rebate="0.1", affiliate_referrer_rebate="0.2"
    )


async def test_fees_without_owner_lists_public_tiers(patch_reference_api, capsys):
    _fee_apis(patch_reference_api)
    await cmd_fees(argparse.Namespace())
    output = parse_stdout(capsys)
    assert [(t["tier_id"], t["taker_fee_bps"]) for t in output["fee_tiers"]] == [(0, "3"), (1, "2.8")]
    assert output["wallet"] is None
    assert "mark price" in output["fee_base"]


async def test_fees_with_owner_reports_the_wallet_tier(monkeypatch, patch_reference_api, capsys):
    _fee_apis(patch_reference_api)
    owner = "0x" + "11" * 20
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", owner)
    monkeypatch.setattr(
        "agent_kit.scripts.query.get_trading_config", lambda **kw: SimpleNamespace(owner_wallet_address=owner)
    )
    wallet_api = create_autospec(WalletDataApi, instance=True)
    wallet_api.get_wallet_configuration.return_value = WalletConfiguration(
        fee_tier_id=1, og_status=True, affiliate_status=False, referee_status=False
    )
    monkeypatch.setattr("agent_kit.scripts.query.WalletDataApi", lambda *a, **kw: wallet_api)
    await cmd_fees(argparse.Namespace())
    wallet = parse_stdout(capsys)["wallet"]
    wallet_api.get_wallet_configuration.assert_awaited_once_with(address=owner)
    assert (wallet["fee_tier_id"], wallet["taker_fee_bps"], wallet["og_status"]) == (1, "2.8", True)


# --------------------------------------------------------------------------
# Error envelopes: regional block, edge rate limit, retry hints
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", [REGION_PAGE, REGION_PAGE.encode()])
def test_sdk_regional_403_becomes_region_unavailable(body):
    result = exception_result(ApiException(status=403, body=body))
    assert result["error_code"] == "REGION_UNAVAILABLE" and result["http_status"] == 403
    assert "nothing was sent" in result["error"]


def test_other_403_is_not_reported_as_a_region_block():
    result = exception_result(ApiException(status=403, body='{"error":"UNAUTHORIZED_SIGNATURE_ERROR"}'))
    assert result["error_code"] == "UNAUTHORIZED_SIGNATURE_ERROR"


def test_edge_429_is_named_and_venue_retry_hint_is_kept():
    edge = exception_result(ApiException(status=429, body='{"error":"rate_limited","scope":"ip"}'))
    assert edge["error_code"] == "EDGE_RATE_LIMITED" and edge["http_status"] == 429
    venue = exception_result(
        ApiException(status=400, body='{"error":"RATE_LIMITED_ERROR","message":"slow down","retryAfterMs":1500}')
    )
    assert (venue["error_code"], venue["retry_after_ms"]) == ("RATE_LIMITED_ERROR", 1500)
    for hint in (True, -1, "1500", 10**9):
        body = json.dumps({"error": "RATE_LIMITED_ERROR", "retryAfterMs": hint})
        assert "retry_after_ms" not in exception_result(ApiException(status=400, body=body))


class _Response:
    def __init__(self, status, body):
        self.status = status
        self.content = SimpleNamespace(readexactly=AsyncMock(side_effect=asyncio.IncompleteReadError(body, None)))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


async def test_v2_reads_raise_region_unavailable_on_the_block_page(monkeypatch):
    client = V2Client("https://example.invalid/v2")
    monkeypatch.setattr(
        client, "session", SimpleNamespace(get=lambda *a, **kw: _Response(403, REGION_PAGE.encode())), raising=False
    )
    with pytest.raises(RegionUnavailableError):
        await client.get("/perpMarketDefinitions")
    monkeypatch.setattr(
        client, "session", SimpleNamespace(get=lambda *a, **kw: _Response(403, b"forbidden")), raising=False
    )
    with pytest.raises(ValueError, match="HTTP 403"):
        await client.get("/perpMarketDefinitions")


# --------------------------------------------------------------------------
# close_all --cancel_orders
# --------------------------------------------------------------------------


def _close_args(**overrides):
    values = dict(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False, cancel_orders=True)
    values.update(overrides)
    return argparse.Namespace(**values)


def _arm_close(client: FakeClient) -> list[str]:
    calls: list[str] = []
    client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
        make_market_def(symbol="BTCRUSDPERP", market_id=2),
    ]
    client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    client.get_open_orders.return_value = [
        make_order(order_id="eth-1", symbol="ETHRUSDPERP"),
        make_order(order_id="btc-1", symbol="BTCRUSDPERP"),
        make_order(order_id="other", symbol="ETHRUSDPERP", account_id=DEFAULT_ACCOUNT_ID + 1),
    ]
    positions = iter(
        [
            [make_position(symbol="ETHRUSDPERP", qty="0.05")],  # read before closing
            [],  # readback after closing
        ]
    )

    async def get_positions():
        calls.append("positions")
        return next(positions)

    async def cancel_order(**kwargs):
        calls.append(f"cancel:{kwargs['order_id']}")
        return CreateOrderResponse(order_id=kwargs["order_id"], status="CANCELLED")

    async def create_limit_order(params):
        calls.append(f"close:{params.symbol}")
        return CreateOrderResponse(order_id="c1", status="FILLED", cum_qty="0.05", avg_fill_px="2000")

    client.get_positions.side_effect = get_positions
    client.cancel_order.side_effect = cancel_order
    client.create_limit_order.side_effect = create_limit_order
    return calls


async def test_close_all_cancels_scoped_orders_before_reading_and_closing(patch_client: FakeClient, capsys):
    calls = _arm_close(patch_client)
    await trade.cmd_positions_close_all(_close_args(symbol="ETHRUSDPERP", all=False))
    # Only this account's orders on the symbol; positions are read after the cancel.
    assert calls == ["cancel:eth-1", "positions", "close:ETHRUSDPERP", "positions"]
    output = parse_stdout(capsys)
    assert output["cancelled_count"] == 1 and output["cancelled"][0]["order_id"] == "eth-1"
    assert output["closed_count"] == 1


async def test_close_all_cancel_preview_sends_nothing(patch_client: FakeClient, capsys):
    calls = _arm_close(patch_client)
    await trade.cmd_positions_close_all(_close_args(preview=True))
    assert not [c for c in calls if c.startswith(("cancel", "close"))]
    output = parse_stdout(capsys)
    assert {o["order_id"] for o in output["would_cancel"]} == {"eth-1", "btc-1"}
    assert output["would_cancel_count"] == 2 and output["would_close_count"] == 1


def _arm_protection(client: FakeClient) -> None:
    client.get_open_orders.return_value = [
        make_order(order_id="eth-limit", symbol="ETHRUSDPERP"),
        make_order(
            order_id="eth-sl",
            symbol="ETHRUSDPERP",
            side=Side.A,
            order_type=OrderType.STOP_LOSS,
            trigger_px="1900",
            limit_px="1880",
        ),
        make_order(
            order_id="eth-tp",
            symbol="ETHRUSDPERP",
            side=Side.A,
            order_type=OrderType.TAKE_PROFIT,
            trigger_px="2200",
            limit_px="2180",
        ),
        make_order(order_id="btc-sl", symbol="BTCRUSDPERP", order_type=OrderType.STOP_LOSS, trigger_px="60000"),
        make_order(
            order_id="other-sl",
            symbol="ETHRUSDPERP",
            order_type=OrderType.STOP_LOSS,
            account_id=DEFAULT_ACCOUNT_ID + 1,
            trigger_px="1900",
        ),
    ]


@pytest.mark.parametrize("cancel_orders", [False, True])
async def test_close_all_preview_lists_the_tp_sl_each_close_drops(patch_client: FakeClient, capsys, cancel_orders):
    calls = _arm_close(patch_client)
    _arm_protection(patch_client)
    await trade.cmd_positions_close_all(_close_args(preview=True, cancel_orders=cancel_orders))
    assert not [c for c in calls if c.startswith(("cancel", "close"))]
    output = parse_stdout(capsys)
    # Only this account's TP/SL on the closed market: the venue cancels them once it is flat.
    assert output["would_close"][0]["drops_protection"] == [
        {"order_id": "eth-sl", "order_type": "STOP_LOSS", "trigger_px": "1900", "limit_px": "1880"},
        {"order_id": "eth-tp", "order_type": "TAKE_PROFIT", "trigger_px": "2200", "limit_px": "2180"},
    ]
    assert output["would_drop_protection_count"] == 2
    assert ("would_cancel" in output) is cancel_orders
    assert "error" not in output


async def test_close_all_preview_without_tp_sl_drops_nothing(patch_client: FakeClient, capsys):
    _arm_close(patch_client)
    await trade.cmd_positions_close_all(_close_args(preview=True, cancel_orders=False))
    output = parse_stdout(capsys)
    assert output["would_close"][0]["drops_protection"] == []
    assert output["would_drop_protection_count"] == 0


async def test_close_all_preview_says_when_the_tp_sl_are_unknown(patch_client: FakeClient, capsys):
    calls = _arm_close(patch_client)
    patch_client.get_open_orders.side_effect = ApiException(status=503, body="down")
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args(preview=True, cancel_orders=False))
    assert not [c for c in calls if c.startswith(("cancel", "close"))]
    output = parse_stdout(capsys)
    assert output["would_close_count"] == 1
    assert output["would_close"][0]["drops_protection"] is None
    assert output["would_drop_protection_count"] is None
    assert output["errors"][0]["context"] == "TP/SL read"


async def test_close_all_preview_with_nothing_to_close_drops_nothing_even_if_orders_are_unreadable(
    patch_client: FakeClient, capsys
):
    _arm_close(patch_client)
    patch_client.get_positions.side_effect = None
    patch_client.get_positions.return_value = []
    patch_client.get_open_orders.side_effect = ApiException(status=503, body="down")
    await trade.cmd_positions_close_all(_close_args(preview=True, cancel_orders=False))
    output = parse_stdout(capsys)
    assert output["would_close_count"] == 0
    assert output["would_drop_protection_count"] == 0
    assert "errors" not in output


async def test_close_all_reads_no_orders_without_preview_or_cancel(patch_client: FakeClient, capsys):
    _arm_close(patch_client)
    _arm_protection(patch_client)
    await trade.cmd_positions_close_all(_close_args(cancel_orders=False))
    assert patch_client.get_open_orders.await_count == 0
    assert "drops_protection" not in parse_stdout(capsys)["closed"][0]


async def test_close_all_reports_cancel_failures_and_still_flattens(patch_client: FakeClient, capsys):
    _arm_close(patch_client)
    patch_client.cancel_order.side_effect = ApiException(status=400, body='{"error":"ORDER_NOT_FOUND_ERROR"}')
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args())
    output = parse_stdout(capsys)
    assert output["cancelled_count"] == 0
    assert {e["context"] for e in output["errors"]} == {"Cancel before close"}
    assert output["close_orders"][0]["status"] == "FILLED"


async def test_close_all_without_flag_leaves_orders_alone(patch_client: FakeClient, capsys):
    calls = _arm_close(patch_client)
    await trade.cmd_positions_close_all(_close_args(cancel_orders=False))
    assert not [c for c in calls if c.startswith("cancel")]
    assert "cancelled" not in parse_stdout(capsys)


# --------------------------------------------------------------------------
# Paper accepts the live command shape
# --------------------------------------------------------------------------


def _paper(argv):
    return paper._paper_request(paper.build_parser().parse_args(["order", *argv]))


def test_paper_accepts_the_live_market_order_shape():
    request = _paper(["market", "eth", "--side", "long", "--qty", "0.01", "--reduce_only", "--client_order_id", "7"])
    assert request == {
        "request_id": "7",
        "symbol": "ETHRUSDPERP",
        "side": "buy",
        "kind": "market",
        "qty": "0.01",
        "price": None,
        "slippage": "0.01",
        "reduce_only": True,
    }
    assert _paper(["market", "btc", "--side", "short", "--qty", "1", "--request-id", "r"])["side"] == "sell"


def test_paper_flag_form_still_works():
    request = _paper(["--request-id", "r1", "--symbol", "ETHRUSDPERP", "--side", "buy", "--qty", "1"])
    assert (request["symbol"], request["side"], request["kind"]) == ("ETHRUSDPERP", "buy", "market")
    ioc = _paper(
        ["--request-id", "r2", "--symbol", "eth", "--side", "sell", "--qty", "1", "--kind", "ioc", "--price", "9"]
    )
    assert (ioc["kind"], ioc["price"]) == ("ioc", "9")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["market", "eth", "--side", "long", "--qty", "1"], "need --request-id"),
        (["limit", "eth", "--side", "long", "--qty", "1", "--request-id", "r"], "resting GTC"),
        (["market", "eth", "--symbol", "btc", "--side", "long", "--qty", "1", "--request-id", "r"], "Conflicting"),
        (["market", "eth", "--kind", "ioc", "--side", "long", "--qty", "1", "--request-id", "r"], "--kind ioc"),
        (["--side", "long", "--qty", "1", "--request-id", "r"], "symbol is required"),
    ],
)
def test_paper_refuses_unsupported_or_ambiguous_orders(argv, message):
    with pytest.raises(ValueError, match=message):
        _paper(argv)


# --------------------------------------------------------------------------
# doctor: inline key in a readable .env
# --------------------------------------------------------------------------

DUMMY_KEY = "0x" + "ab" * 32


@pytest.mark.parametrize(
    ("contents", "mode", "warned"),
    [
        (f"PERP_PRIVATE_KEY_1={DUMMY_KEY}\n", 0o644, True),
        (f"export PERP_PRIVATE_KEY_1='{DUMMY_KEY}'\n", 0o640, True),
        (f"PERP_PRIVATE_KEY_1={DUMMY_KEY}\n", 0o600, False),
        ("PERP_PRIVATE_KEY_FILE=/abs/key\n", 0o644, False),
        ("PERP_PRIVATE_KEY_1=\n", 0o644, False),
    ],
)
def test_doctor_warns_about_a_readable_inline_key_without_revealing_it(monkeypatch, tmp_path, contents, mode, warned):
    monkeypatch.setattr(doctor._env, "load_env", lambda: None)  # keep the dummy key out of os.environ
    env_file = tmp_path / ".env"
    env_file.write_text(contents)
    os.chmod(env_file, mode)
    result = doctor.diagnose()
    assert bool(result["warnings"]) is warned
    assert result["status"] == "ok"
    assert DUMMY_KEY[2:] not in json.dumps(result)
    if warned:
        assert f"{mode:04o}" in result["warnings"][0]


def test_doctor_has_no_warning_without_an_env_file():
    assert doctor.diagnose()["warnings"] == []


# --------------------------------------------------------------------------
# Review follow-ups
# --------------------------------------------------------------------------


async def test_close_all_flattens_when_the_open_order_read_fails(patch_client: FakeClient, capsys):
    calls = _arm_close(patch_client)
    patch_client.get_open_orders.side_effect = ApiException(status=503, body="down")
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args())
    assert "close:ETHRUSDPERP" in calls and not [c for c in calls if c.startswith("cancel")]
    output = parse_stdout(capsys)
    assert output["cancelled_count"] == 0
    assert output["errors"][0]["context"] == "Cancel before close"
    assert output["close_orders"][0]["status"] == "FILLED"


async def test_close_all_preview_reports_an_open_order_read_failure(patch_client: FakeClient, capsys):
    _arm_close(patch_client)
    patch_client.get_open_orders.side_effect = ApiException(status=503, body="down")
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args(preview=True))
    output = parse_stdout(capsys)
    assert output["would_cancel_count"] == 0
    # The one failed read leaves both the cancels and the dropped TP/SL unknown.
    assert [e["context"] for e in output["errors"]] == ["Cancel before close", "TP/SL read"]
    assert output["would_close"][0]["drops_protection"] is None
    assert output["would_drop_protection_count"] is None


async def test_close_all_keeps_confirmed_cancels_when_the_position_read_fails(patch_client: FakeClient, capsys):
    calls = _arm_close(patch_client)
    patch_client.get_positions.side_effect = ApiException(status=503, body="down")
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args())
    assert not [c for c in calls if c.startswith("close")]
    output = parse_stdout(capsys)
    assert {o["order_id"] for o in output["cancelled"]} == {"eth-1", "btc-1"}
    assert output["errors"][-1]["context"] == "Position read"
    assert "nothing was closed" in output["error"]


async def test_close_all_without_cancel_still_raises_on_position_read_failure(patch_client: FakeClient):
    _arm_close(patch_client)
    patch_client.get_positions.side_effect = ApiException(status=503, body="down")
    with pytest.raises(ApiException):
        await trade.cmd_positions_close_all(_close_args(cancel_orders=False))


def test_paper_refuses_two_different_replay_keys():
    with pytest.raises(ValueError, match="differ"):
        _paper(["market", "eth", "--side", "long", "--qty", "1", "--request-id", "a", "--client_order_id", "b"])
    assert (
        _paper(["market", "eth", "--side", "long", "--qty", "1", "--request-id", "a", "--client_order_id", "a"])[
            "request_id"
        ]
        == "a"
    )


async def test_fees_owner_lookup_needs_no_trading_settings(monkeypatch, patch_reference_api, capsys):
    _fee_apis(patch_reference_api)
    owner = "0x" + "22" * 20
    # devnet without REYA_DEX_ID / REYA_ORDERS_GATEWAY: trading config would refuse this.
    monkeypatch.setenv("REYA_API_URL", "https://api-devnet.reya-cronos.network/v2")
    monkeypatch.setenv("CHAIN_ID", "89346162")
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", owner)
    wallet_api = create_autospec(WalletDataApi, instance=True)
    wallet_api.get_wallet_configuration.return_value = WalletConfiguration(
        fee_tier_id=0, og_status=False, affiliate_status=False, referee_status=False
    )
    monkeypatch.setattr("agent_kit.scripts.query.WalletDataApi", lambda *a, **kw: wallet_api)
    await cmd_fees(argparse.Namespace())
    assert parse_stdout(capsys)["wallet"]["address"] == owner


def test_fees_rejects_an_invalid_owner(monkeypatch):
    from agent_kit.scripts import _env

    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", "0x" + "00" * 20)
    with pytest.raises(ValueError, match="nonzero EVM address"):
        _env.get_owner_address()


def test_doctor_mode_check_works_without_getuid(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor._env, "load_env", lambda: None)
    monkeypatch.delattr(doctor.os, "getuid")
    env_file = tmp_path / ".env"
    env_file.write_text(f"PERP_PRIVATE_KEY_1={DUMMY_KEY}\n")
    os.chmod(env_file, 0o644)
    assert doctor.diagnose()["warnings"]
    os.chmod(env_file, 0o600)
    assert doctor.diagnose()["warnings"] == []
