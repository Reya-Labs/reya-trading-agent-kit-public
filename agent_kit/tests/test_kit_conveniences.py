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
from agent_kit.scripts._live import Published
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
        self.headers: dict = {}
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
# close_all: TP/SL stay armed until the position is flat
# --------------------------------------------------------------------------


def _close_args(**overrides):
    values = dict(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False, cancel_orders=True)
    values.update(overrides)
    return argparse.Namespace(**values)


class _Venue:
    """One ETH position and this account's open orders, as the venue holds them.

    Position reads can trail the fill (`lag_reads` of them show nothing); a close fills up to
    `fill`; once the position is flat the venue retires its TP/SL unless `retires` is False.
    Every read and write is recorded in `calls`.
    """

    def __init__(self, client: FakeClient, *, lag_reads: int = 0, fill: str = "0.05", retires: bool = True):
        self.calls: list[str] = []
        self.qty = Decimal("0.05")
        self.lag_reads, self.position_reads = lag_reads, 0
        self.fill, self.retires = Decimal(fill), retires
        self.orders = [
            make_order(order_id="eth-1", symbol="ETHRUSDPERP"),
            make_order(order_id="btc-1", symbol="BTCRUSDPERP"),
            make_order(order_id="other", symbol="ETHRUSDPERP", account_id=DEFAULT_ACCOUNT_ID + 1),
        ]
        client.reference.get_perp_market_definitions.return_value = [
            make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
            make_market_def(symbol="BTCRUSDPERP", market_id=2),
        ]
        client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
        client.get_positions.side_effect = self.get_positions
        client.get_open_orders.side_effect = self.get_open_orders
        client.cancel_order.side_effect = self.cancel_order
        client.create_limit_order.side_effect = self.create_limit_order

    def protect(self) -> None:
        """Add a stop-loss and a take-profit on ETH, another limit on ETH, a BTC stop and another account's stop."""
        self.orders = [
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

    async def get_positions(self):
        self.calls.append("positions")
        self.position_reads += 1
        if self.position_reads <= self.lag_reads or self.qty == 0:
            return []
        return [make_position(symbol="ETHRUSDPERP", qty=format(self.qty, "f"))]

    async def get_open_orders(self):
        self.calls.append("orders")
        return list(self.orders)

    async def cancel_order(self, **kwargs):
        self.calls.append(f"cancel:{kwargs['order_id']}")
        self.orders = [o for o in self.orders if o.order_id != kwargs["order_id"]]
        return CreateOrderResponse(order_id=kwargs["order_id"], status="CANCELLED")

    async def create_limit_order(self, params):
        self.calls.append(f"close:{params.symbol}")
        filled = min(self.fill, Decimal(params.qty))
        self.qty -= filled
        if self.qty == 0 and self.retires:
            self.orders = [o for o in self.orders if o.order_type.value not in ("TAKE_PROFIT", "STOP_LOSS")]
        status = "FILLED" if filled == Decimal(params.qty) else "CANCELLED"
        return CreateOrderResponse(order_id="c1", status=status, cum_qty=format(filled, "f"), avg_fill_px="2000")

    def writes(self) -> list[str]:
        return [c for c in self.calls if c.startswith(("cancel", "close"))]


def _ids(rows) -> list[str]:
    return [row["order_id"] for row in rows]


ETH_ONLY = dict(symbol="ETHRUSDPERP", all=False)


async def test_close_all_reads_before_any_write_then_cancels_other_orders_and_closes(patch_client: FakeClient, capsys):
    venue = _Venue(patch_client)
    await trade.cmd_positions_close_all(_close_args(**ETH_ONLY))
    # Only this account's orders on the symbol; positions are read again after the cancel.
    assert venue.calls == ["positions", "orders", "cancel:eth-1", "positions", "close:ETHRUSDPERP", "positions"]
    output = parse_stdout(capsys)
    assert _ids(output["cancelled"]) == ["eth-1"] and output["cancelled_count"] == 1
    assert output["closed_count"] == 1
    assert output["kept_protection"] == output["cancelled_protection"] == output["retired_protection"] == []


async def test_close_all_cancel_preview_sends_nothing(patch_client: FakeClient, capsys):
    venue = _Venue(patch_client)
    await trade.cmd_positions_close_all(_close_args(preview=True))
    assert not venue.writes()
    output = parse_stdout(capsys)
    assert set(_ids(output["would_cancel"])) == {"eth-1", "btc-1"}
    assert output["would_cancel_count"] == 2 and output["would_close_count"] == 1
    assert output["would_keep_protection"] == []


@pytest.mark.parametrize("retires", [True, False])
async def test_close_all_keeps_tp_sl_while_closing_and_clears_what_is_left_once_flat(
    patch_client: FakeClient, capsys, retires
):
    venue = _Venue(patch_client, retires=retires)
    venue.protect()
    await trade.cmd_positions_close_all(_close_args(**ETH_ONLY))
    close = venue.calls.index("close:ETHRUSDPERP")
    assert venue.calls[:close] == ["positions", "orders", "cancel:eth-limit", "positions"]
    # After the read-back shows ETH flat: one order read, then only what the venue has not retired.
    assert venue.calls[close + 1 :] == ["positions", "orders", *([] if retires else ["cancel:eth-sl", "cancel:eth-tp"])]
    output = parse_stdout(capsys)
    assert "error" not in output and output["closed_count"] == 1
    assert _ids(output["cancelled"]) == ["eth-limit"]
    assert output["kept_protection"] == []
    assert output["retired_protection"] == (["eth-sl", "eth-tp"] if retires else [])
    assert _ids(output["cancelled_protection"]) == ([] if retires else ["eth-sl", "eth-tp"])


async def test_close_all_partial_close_keeps_the_tp_sl_on_what_is_left(patch_client: FakeClient, capsys):
    venue = _Venue(patch_client, fill="0.02")
    venue.protect()
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args(**ETH_ONLY))
    assert venue.writes() == ["cancel:eth-limit", "close:ETHRUSDPERP"]
    output = parse_stdout(capsys)
    assert output["remaining_positions"] == [{"symbol": "ETHRUSDPERP", "side": "B", "qty": "0.03"}]
    assert output["closed_count"] == 0 and "Close incomplete" in output["error"]
    assert _ids(output["kept_protection"]) == ["eth-sl", "eth-tp"]


async def test_close_all_waits_for_a_protected_position_the_reads_do_not_show_yet(patch_client: FakeClient, capsys):
    # The fill is in, but the first read and the read after the cancel both trail it.
    venue = _Venue(patch_client, lag_reads=2)
    venue.protect()
    await trade.cmd_positions_close_all(_close_args(**ETH_ONLY))
    assert venue.calls[:6] == ["positions", "orders", "cancel:eth-limit", "positions", "positions", "close:ETHRUSDPERP"]
    output = parse_stdout(capsys)
    assert "error" not in output and output["closed_count"] == 1
    assert output["retired_protection"] == ["eth-sl", "eth-tp"]


async def test_close_all_never_strips_tp_sl_or_reports_success_when_the_position_never_shows(
    patch_client: FakeClient, capsys
):
    # Tester report: --cancel_orders cancelled the stop-loss and take-profit, one read showed no
    # position, nothing was closed and no error was returned.
    venue = _Venue(patch_client, lag_reads=10**6)
    venue.protect()
    venue.orders = [o for o in venue.orders if o.order_id in ("eth-sl", "eth-tp")]
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args(**ETH_ONLY))
    assert venue.writes() == []
    assert venue.position_reads > 2  # read again within the indexing ceiling before concluding
    output = parse_stdout(capsys)
    assert output["close_orders"] == [] and output["cancelled_count"] == 0
    assert _ids(output["kept_protection"]) == ["eth-sl", "eth-tp"]
    [not_shown] = output["errors"]
    assert not_shown["context"] == "Position not shown" and not_shown["symbol"] == "ETHRUSDPERP"
    assert "TP/SL were kept" in not_shown["error"]


async def test_close_all_preview_flags_protection_without_a_shown_position(patch_client: FakeClient, capsys):
    # Tester report: the preview listed the stop-loss and take-profit under would_cancel and said
    # would_drop_protection_count 0.
    venue = _Venue(patch_client, lag_reads=10**6)
    venue.protect()
    venue.orders = [o for o in venue.orders if o.order_id in ("eth-sl", "eth-tp")]
    await trade.cmd_positions_close_all(_close_args(preview=True, **ETH_ONLY))
    assert venue.writes() == [] and venue.position_reads == 1
    output = parse_stdout(capsys)
    assert output["would_cancel"] == [] and output["would_close_count"] == 0
    assert _ids(output["would_keep_protection"]) == ["eth-sl", "eth-tp"]
    assert output["would_drop_protection_count"] is None
    assert [p["symbol"] for p in output["positions_not_shown"]] == ["ETHRUSDPERP"]
    assert "TP/SL are armed there" in output["warning"]


@pytest.mark.parametrize("cancel_orders", [False, True])
async def test_close_all_preview_lists_the_tp_sl_each_close_drops(patch_client: FakeClient, capsys, cancel_orders):
    venue = _Venue(patch_client)
    venue.protect()
    await trade.cmd_positions_close_all(_close_args(preview=True, cancel_orders=cancel_orders, **ETH_ONLY))
    assert not venue.writes()
    output = parse_stdout(capsys)
    # Only this account's TP/SL on the closed market: the venue cancels them once it is flat.
    assert output["would_close"][0]["drops_protection"] == [
        {"order_id": "eth-sl", "order_type": "STOP_LOSS", "trigger_px": "1900", "limit_px": "1880"},
        {"order_id": "eth-tp", "order_type": "TAKE_PROFIT", "trigger_px": "2200", "limit_px": "2180"},
    ]
    assert output["would_drop_protection_count"] == 2
    if cancel_orders:
        assert _ids(output["would_cancel"]) == ["eth-limit"]
        assert _ids(output["would_keep_protection"]) == ["eth-sl", "eth-tp"]
    else:
        assert "would_cancel" not in output and "would_keep_protection" not in output
    assert "error" not in output and "positions_not_shown" not in output


async def test_close_all_preview_does_not_count_protection_on_a_market_with_no_position_shown(
    patch_client: FakeClient, capsys
):
    venue = _Venue(patch_client)
    venue.protect()  # BTC has a stop-loss but no position
    await trade.cmd_positions_close_all(_close_args(preview=True))
    output = parse_stdout(capsys)
    assert output["would_close_count"] == 1
    assert output["would_drop_protection_count"] is None
    assert output["positions_not_shown"] == [
        {"symbol": "BTCRUSDPERP", "protection": [trade._order_preview(venue.orders[3])]}
    ]


async def test_close_all_preview_without_tp_sl_drops_nothing(patch_client: FakeClient, capsys):
    _Venue(patch_client)
    await trade.cmd_positions_close_all(_close_args(preview=True, cancel_orders=False))
    output = parse_stdout(capsys)
    assert output["would_close"][0]["drops_protection"] == []
    assert output["would_drop_protection_count"] == 0


async def test_close_all_preview_says_when_the_tp_sl_are_unknown(patch_client: FakeClient, capsys):
    venue = _Venue(patch_client)
    patch_client.get_open_orders.side_effect = ApiException(status=503, body="down")
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args(preview=True, cancel_orders=False))
    assert not venue.writes()
    output = parse_stdout(capsys)
    assert output["would_close_count"] == 1
    assert output["would_close"][0]["drops_protection"] is None
    assert output["would_drop_protection_count"] is None
    assert output["errors"][0]["context"] == "TP/SL read"


async def test_close_all_preview_with_nothing_to_close_drops_nothing_even_if_orders_are_unreadable(
    patch_client: FakeClient, capsys
):
    _Venue(patch_client, lag_reads=10**6)
    patch_client.get_open_orders.side_effect = ApiException(status=503, body="down")
    await trade.cmd_positions_close_all(_close_args(preview=True, cancel_orders=False))
    output = parse_stdout(capsys)
    assert output["would_close_count"] == 0
    assert output["would_drop_protection_count"] == 0
    assert "errors" not in output


async def test_close_all_without_cancel_reads_orders_but_cancels_nothing(patch_client: FakeClient, capsys):
    venue = _Venue(patch_client)
    venue.protect()
    await trade.cmd_positions_close_all(_close_args(cancel_orders=False, **ETH_ONLY))
    assert venue.calls == ["positions", "orders", "close:ETHRUSDPERP", "positions"]
    output = parse_stdout(capsys)
    assert "drops_protection" not in output["closed"][0]
    assert "cancelled" not in output and "kept_protection" not in output


async def test_close_all_without_cancel_warns_when_orders_are_unreadable(patch_client: FakeClient, capsys):
    venue = _Venue(patch_client)
    patch_client.get_open_orders.side_effect = ApiException(status=503, body="down")
    await trade.cmd_positions_close_all(_close_args(cancel_orders=False))
    assert venue.writes() == ["close:ETHRUSDPERP"]
    output = parse_stdout(capsys)
    assert "error" not in output and output["closed_count"] == 1
    assert "could not be detected" in output["warning"]


async def test_close_all_reports_cancel_failures_and_still_flattens(patch_client: FakeClient, capsys):
    _Venue(patch_client)
    patch_client.cancel_order.side_effect = ApiException(status=400, body='{"error":"ORDER_NOT_FOUND_ERROR"}')
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args())
    output = parse_stdout(capsys)
    assert output["cancelled_count"] == 0
    assert {e["context"] for e in output["errors"]} == {"Cancel before close"}
    assert output["close_orders"][0]["status"] == "FILLED"


async def test_close_all_counts_a_tp_sl_gone_by_its_cancel_as_retired(patch_client: FakeClient, capsys):
    venue = _Venue(patch_client, retires=False)
    venue.protect()
    cancel = venue.cancel_order

    async def retired_meanwhile(**kwargs):
        if kwargs["order_id"] == "eth-sl":
            raise ApiException(status=400, body='{"error":"ORDER_NOT_FOUND_ERROR","message":"not found"}')
        return await cancel(**kwargs)

    patch_client.cancel_order.side_effect = retired_meanwhile
    await trade.cmd_positions_close_all(_close_args(**ETH_ONLY))
    output = parse_stdout(capsys)
    assert "error" not in output
    assert output["retired_protection"] == ["eth-sl"]
    assert _ids(output["cancelled_protection"]) == ["eth-tp"]


async def test_close_all_without_flag_leaves_orders_alone(patch_client: FakeClient, capsys):
    venue = _Venue(patch_client)
    await trade.cmd_positions_close_all(_close_args(cancel_orders=False))
    assert not [c for c in venue.calls if c.startswith("cancel")]
    assert "cancelled" not in parse_stdout(capsys)


async def test_close_all_flattens_when_the_open_order_read_fails(patch_client: FakeClient, capsys):
    venue = _Venue(patch_client)
    patch_client.get_open_orders.side_effect = ApiException(status=503, body="down")
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args())
    assert venue.writes() == ["close:ETHRUSDPERP"]
    output = parse_stdout(capsys)
    assert output["cancelled_count"] == 0
    assert output["errors"][0]["context"] == "Cancel before close"
    assert output["close_orders"][0]["status"] == "FILLED"
    assert "kept_protection" not in output  # unknown: the orders were unreadable


async def test_close_all_preview_reports_an_open_order_read_failure(patch_client: FakeClient, capsys):
    _Venue(patch_client)
    patch_client.get_open_orders.side_effect = ApiException(status=503, body="down")
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args(preview=True))
    output = parse_stdout(capsys)
    assert output["would_cancel_count"] == 0 and output["would_keep_protection"] is None
    # The one failed read leaves both the cancels and the dropped TP/SL unknown.
    assert [e["context"] for e in output["errors"]] == ["Cancel before close", "TP/SL read"]
    assert output["would_close"][0]["drops_protection"] is None
    assert output["would_drop_protection_count"] is None


@pytest.mark.parametrize("cancel_orders", [False, True])
async def test_close_all_sends_nothing_when_the_first_position_read_fails(patch_client: FakeClient, cancel_orders):
    venue = _Venue(patch_client)
    patch_client.get_positions.side_effect = ApiException(status=503, body="down")
    with pytest.raises(ApiException):
        await trade.cmd_positions_close_all(_close_args(cancel_orders=cancel_orders))
    assert venue.writes() == []


async def test_close_all_keeps_tp_sl_and_confirmed_cancels_when_the_read_after_them_fails(
    patch_client: FakeClient, capsys
):
    venue = _Venue(patch_client)
    venue.protect()
    read = venue.get_positions

    async def fails_after_the_cancel():
        if venue.writes():
            raise ApiException(status=503, body="down")
        return await read()

    patch_client.get_positions.side_effect = fails_after_the_cancel
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(_close_args(**ETH_ONLY))
    assert venue.writes() == ["cancel:eth-limit"]
    output = parse_stdout(capsys)
    assert _ids(output["cancelled"]) == ["eth-limit"]
    assert _ids(output["kept_protection"]) == ["eth-sl", "eth-tp"]
    assert output["errors"][-1]["context"] == "Position read"
    assert "nothing was closed" in output["error"]


class _RunnerFeedClient(FakeClient):
    """A client with the runner's warm-state hooks: its feed shows a position REST does not show yet."""

    def __init__(self) -> None:
        super().__init__()
        self.own_fill_waits: list[float] = []
        self.feed_position = make_position(symbol="ETHRUSDPERP", qty="0.05")

    def warm_position(self, account_id: int, symbol: str):
        return Published(self.feed_position if symbol == "ETHRUSDPERP" else None, 7)

    async def wait_own_fills(self, timeout: float) -> None:
        self.own_fill_waits.append(timeout)

    async def warm_wallet_changed(self, timeout: float) -> None:
        await asyncio.sleep(0)


async def test_close_all_in_the_runner_waits_for_own_fills_and_closes_what_the_feed_shows(monkeypatch, capsys):
    client = _RunnerFeedClient()
    monkeypatch.setattr("agent_kit.scripts.trade.ReyaTradingClient", lambda *a, **kw: client)
    monkeypatch.setattr("agent_kit.scripts.trade.get_trading_config", lambda **kw: None)
    venue = _Venue(client, lag_reads=10**6)  # REST trails the feed throughout
    await trade.cmd_positions_close_all(_close_args(cancel_orders=False, **ETH_ONLY))
    assert client.own_fill_waits == [trade.entry_position_bounds(client)[1]]
    assert venue.writes() == ["close:ETHRUSDPERP"]
    assert client.create_limit_order.call_args.args[0].qty == "0.05"
    assert parse_stdout(capsys)["closed_count"] == 1


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
