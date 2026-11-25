"""Order-book context around an IOC `order market`: offline, the depth read is scripted."""

import argparse
import asyncio
import time
from decimal import Decimal

import pytest
from sdk.open_api.models.create_order_response import CreateOrderResponse

from agent_kit.scripts import _book, runner, trade
from agent_kit.tests.conftest import DEFAULT_ACCOUNT_ID, FakeClient, make_position, parse_stdout
from agent_kit.tests.test_runner import client, outputs, stream  # noqa: F401
from agent_kit.tests.test_runner_warm import MARKET, wide

SYMBOL = "ETHRUSDPERP"  # conftest market: tick 0.001, step 0.001, mark 2000
NO_LIQUIDITY = dict(
    cancel_reason="NO_LIQUIDITY",
    cancel_reason_message="Order cancelled: no fillable liquidity available at the limit.",
)


def depth(bids=(), asks=(), *, updated_at=None, symbol=SYMBOL) -> dict:
    return {
        "symbol": symbol,
        "type": "SNAPSHOT",
        "updatedAt": int(time.time() * 1000) - 400 if updated_at is None else updated_at,
        "bids": [{"px": px, "qty": qty} for px, qty in bids],
        "asks": [{"px": px, "qty": qty} for px, qty in asks],
    }


def serve_book(monkeypatch, book, clock=None) -> list:
    reads: list = []

    async def read_book(api_url, symbol):
        reads.append(symbol)
        return book, clock

    monkeypatch.setattr(_book, "read_book", read_book)
    return reads


def market(**overrides) -> argparse.Namespace:
    values = dict(
        symbol=SYMBOL,
        side="long",
        qty=Decimal("0.05"),
        notional=None,
        slippage=Decimal("0.01"),  # long limit 2020, short limit 1980
        reduce_only=False,
        force=False,
        preview=False,
        fees_wait_s=0,
        client_order_id=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


async def run(args, capsys, *, fails: bool) -> dict:
    if fails:
        with pytest.raises(SystemExit) as exc:
            await trade.cmd_order_market(args)
        assert exc.value.code == 1
    else:
        await trade.cmd_order_market(args)
    return parse_stdout(capsys)


# --------------------------------------------------------------------------
# After the send
# --------------------------------------------------------------------------


async def test_zero_fill_carries_the_book_and_says_not_to_resend(patch_client: FakeClient, monkeypatch, capsys):
    reads = serve_book(monkeypatch, depth(bids=[("1990.000", "0.500")], asks=[("2030.000", "1.000")]))
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="ioc-1", status="CANCELLED", cum_qty="0", **NO_LIQUIDITY
    )
    output = await run(market(), capsys, fails=True)
    assert reads == [SYMBOL]
    assert output["status"] == "CANCELLED" and output["order_id"] == "ioc-1" and output["filled_qty"] == "0"
    book = output["book"]
    assert book["best_ask"] == {"px": "2030", "qty": "1"} and book["best_bid"] == {"px": "1990", "qty": "0.5"}
    assert book["limit_px"] == "2020" and book["qty_within_limit"] == "0" and book["qty_within_limit_complete"]
    assert book["touch_past_limit_pct"] == 0.5  # (2030 - 2020) / 2020
    assert 0 <= book["age_ms"] < 5000
    assert output["error"].startswith("Nothing filled at the limit 2020 (Order cancelled: no fillable liquidity")
    assert "`book` shows the market as of its `updated_at_ms`" in output["error"]
    assert "Do not resend at the same or a nearby price" in output["error"]
    assert "never widen the slippage without the user's yes" in output["error"]


async def test_partial_fill_carries_the_book(patch_client: FakeClient, monkeypatch, capsys):
    reads = serve_book(monkeypatch, depth(bids=[("1999.000", "2.000")], asks=[("2025.000", "1.000")]))
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="ioc-2",
        status="CANCELLED",
        cum_qty="0.03",
        avg_fill_px="2010",
        cancel_reason="IOC_REMAINDER",
        cancel_reason_message="Immediate-or-cancel order partially filled; the unfilled remainder was cancelled.",
    )
    output = await run(market(), capsys, fails=True)
    assert reads == [SYMBOL]
    assert output["filled_qty"] == "0.03" and output["book"]["qty_within_limit"] == "0"
    assert output["error"].startswith("Filled 0.03 of 0.05; the rest was cancelled at the limit 2020")


async def test_short_zero_fill_measures_the_bids(patch_client: FakeClient, monkeypatch, capsys):
    serve_book(monkeypatch, depth(bids=[("1970.000", "2.000")], asks=[("2001.000", "1.000")]))
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="ioc-3", status="CANCELLED", cum_qty="0", **NO_LIQUIDITY
    )
    output = await run(market(side="short"), capsys, fails=True)
    assert output["book"]["limit_px"] == "1980"
    assert output["book"]["touch_past_limit_pct"] == 0.51  # (1980 - 1970) / 1980
    assert output["book"]["qty_within_limit"] == "0"


async def test_full_fill_reads_no_book(patch_client: FakeClient, monkeypatch, capsys):
    reads = serve_book(monkeypatch, depth(asks=[("2001.000", "1.000")]))
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="ioc-4", status="FILLED", cum_qty="0.05", avg_fill_px="2001"
    )
    output = await run(market(), capsys, fails=False)
    assert reads == [] and "book" not in output and "error" not in output


async def test_failed_book_read_keeps_the_result(patch_client: FakeClient, capsys):
    # conftest's default read_book fails, as an unreachable or 503 depth endpoint would.
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="ioc-5", status="CANCELLED", cum_qty="0", **NO_LIQUIDITY
    )
    output = await run(market(), capsys, fails=True)
    assert output["status"] == "CANCELLED" and output["order_id"] == "ioc-5" and output["filled_qty"] == "0"
    assert output["book"] == {"unavailable": "ValueError: Order book reads are stubbed in offline tests"}
    assert output["error"].startswith("Nothing filled at the limit 2020")
    assert "`data market book --symbol ETHRUSDPERP` reads it" in output["error"]


async def test_slow_book_read_times_out_without_holding_the_result(patch_client: FakeClient, monkeypatch, capsys):
    async def slow(api_url, symbol):
        await asyncio.sleep(5)

    monkeypatch.setattr(_book, "read_book", slow)
    monkeypatch.setattr(_book, "BOOK_READ_TIMEOUT_S", 0.05)
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="ioc-6", status="CANCELLED", cum_qty="0", **NO_LIQUIDITY
    )
    started = time.monotonic()
    output = await run(market(), capsys, fails=True)
    assert time.monotonic() - started < 2
    assert output["book"] == {"unavailable": "The order book read timed out after 0.05 s"}


async def test_other_cancel_reasons_keep_the_generic_error(patch_client: FakeClient, monkeypatch, capsys):
    serve_book(monkeypatch, depth(asks=[("2001.000", "1.000")]))
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="ioc-7", status="CANCELLED", cum_qty="0", cancel_reason="SELF_TRADE_PREVENTION"
    )
    output = await run(market(), capsys, fails=True)
    assert "book" in output  # the context is still useful
    assert output["error"].startswith("IOC did not fill completely")


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------


async def preview(patch_client, monkeypatch, capsys, book, **overrides) -> tuple[dict, list]:
    reads = serve_book(monkeypatch, book)
    output = await run(market(preview=True, **overrides), capsys, fails=False)
    patch_client.create_limit_order.assert_not_awaited()
    assert output["preview"] is True and output["would_send"] is True
    return output, reads


async def test_preview_warns_when_the_book_holds_less_than_the_order(patch_client, monkeypatch, capsys):
    asks = [("2010.000", "0.020"), ("2020.000", "0.010"), ("2030.000", "5.000")]  # 2020 is the limit: inside
    output, reads = await preview(patch_client, monkeypatch, capsys, depth(asks=asks))
    assert reads == [SYMBOL] and output["book"]["qty_within_limit"] == "0.03"
    assert output["warning"].startswith("Only 0.03 sits in the asks at or inside the limit 2020, less than the 0.05")
    assert "likely fill only part" in output["warning"]


async def test_preview_has_no_warning_when_the_book_covers_the_order(patch_client, monkeypatch, capsys):
    output, _ = await preview(patch_client, monkeypatch, capsys, depth(asks=[("2010.000", "0.050")]))
    assert output["book"]["qty_within_limit"] == "0.05" and "warning" not in output


async def test_preview_short_against_a_one_sided_book(patch_client, monkeypatch, capsys):
    output, _ = await preview(patch_client, monkeypatch, capsys, depth(asks=[("2001.000", "1.000")]), side="short")
    assert output["book"]["best_bid"] is None and output["book"]["touch_past_limit_pct"] is None
    assert output["warning"].startswith("The book has no bids at all (one-sided)")


async def test_preview_short_with_bids_only_past_the_limit(patch_client, monkeypatch, capsys):
    output, _ = await preview(patch_client, monkeypatch, capsys, depth(bids=[("1979.999", "9.000")]), side="short")
    assert output["warning"].startswith("No bids sit at or inside the limit 1980")


async def test_preview_does_not_warn_from_a_truncated_book(patch_client, monkeypatch, capsys):
    # Every one of the 1,000 served levels sits inside the limit, so more may sit beyond what was served.
    asks = [(f"{Decimal('2000') + Decimal(i) / 1000:.3f}", "0.001") for i in range(_book.BOOK_LEVELS)]
    output, _ = await preview(patch_client, monkeypatch, capsys, depth(asks=asks))
    assert output["book"]["qty_within_limit"] == "1" and output["book"]["qty_within_limit_complete"] is False
    assert "warning" not in output


async def test_preview_with_an_unreadable_book_still_previews(patch_client, capsys):
    output = await run(market(preview=True), capsys, fails=False)
    assert output["book"] == {"unavailable": "ValueError: Order book reads are stubbed in offline tests"}
    assert "warning" not in output and output["would_send"] is True


# --------------------------------------------------------------------------
# book_context
# --------------------------------------------------------------------------


def test_age_uses_the_server_clock_when_known():
    now = 1_800_000_000_000
    context = _book.book_context(
        depth(asks=[("2010.000", "1.000")], updated_at=now - 1000),
        {"offset_ms": 300, "uncertainty_ms": 520, "source": "http_date"},
        symbol=SYMBOL,
        is_buy=True,
        limit_px=Decimal("2020"),
        tick=Decimal("0.001"),
        step=Decimal("0.001"),
        now_ms=now,
    )
    assert context["updated_at_ms"] == now - 1000 and context["age_ms"] == 700  # local clock 300 ms ahead
    assert "clock_warning" not in context


def test_a_book_for_another_market_is_refused():
    with pytest.raises(ValueError, match="Mismatched market"):
        _book.book_context(
            depth(symbol="BTCRUSDPERP"),
            None,
            symbol=SYMBOL,
            is_buy=True,
            limit_px=Decimal("2020"),
            tick=Decimal("0.001"),
            step=Decimal("0.001"),
            now_ms=0,
        )


# --------------------------------------------------------------------------
# positions close_all
# --------------------------------------------------------------------------


def close_all(**overrides) -> argparse.Namespace:
    values = dict(symbol=SYMBOL, all=False, slippage=Decimal("0.01"), preview=False, force=False, cancel_orders=False)
    values.update(overrides)
    return argparse.Namespace(**values)


def hold_long(patch_client: FakeClient, qty: str = "0.05") -> None:
    patch_client.get_open_orders.return_value = []
    patch_client.get_positions.return_value = [make_position(account_id=DEFAULT_ACCOUNT_ID, symbol=SYMBOL, qty=qty)]


async def test_close_all_zero_fill_carries_the_book_and_says_not_to_resend(patch_client, monkeypatch, capsys):
    hold_long(patch_client)  # the close sells at the 1980 limit
    reads = serve_book(monkeypatch, depth(bids=[("1975.000", "2.000")], asks=[("2001.000", "1.000")]))
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="c-1", status="CANCELLED", cum_qty="0", **NO_LIQUIDITY
    )
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(close_all())
    (close,) = parse_stdout(capsys)["close_orders"]
    assert reads == [SYMBOL]
    assert close["book"]["best_bid"] == {"px": "1975", "qty": "2"} and close["book"]["qty_within_limit"] == "0"
    assert close["book"]["limit_px"] == "1980"
    assert close["error"].startswith("Nothing filled at the limit 1980")
    assert "Do not resend at the same or a nearby price" in close["error"]


async def test_close_all_partial_close_carries_the_book(patch_client, monkeypatch, capsys):
    hold_long(patch_client)
    reads = serve_book(monkeypatch, depth(bids=[("1985.000", "0.020")]))
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="c-2", status="CANCELLED", cum_qty="0.02", avg_fill_px="1985", cancel_reason="IOC_REMAINDER"
    )
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(close_all())
    (close,) = parse_stdout(capsys)["close_orders"]
    assert reads == [SYMBOL] and "book" in close
    assert close["error"].startswith("Filled 0.02 of 0.05; the rest was cancelled at the limit 1980")


async def test_close_all_full_close_reads_no_book(patch_client, monkeypatch, capsys):
    hold_long(patch_client)
    reads = serve_book(monkeypatch, depth(bids=[("1999.000", "1.000")]))
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="c-3", status="FILLED", cum_qty="0.05", avg_fill_px="1999"
    )
    patch_client.get_positions.side_effect = [patch_client.get_positions.return_value, []]
    await trade.cmd_positions_close_all(close_all())
    (close,) = parse_stdout(capsys)["close_orders"]
    assert reads == [] and "book" not in close and "error" not in close


async def test_close_all_preview_warns_when_the_book_holds_less_than_the_position(patch_client, monkeypatch, capsys):
    hold_long(patch_client)
    reads = serve_book(monkeypatch, depth(bids=[("1990.000", "0.010"), ("1970.000", "5.000")]))
    await trade.cmd_positions_close_all(close_all(preview=True))
    (planned,) = parse_stdout(capsys)["would_close"]
    patch_client.create_limit_order.assert_not_awaited()
    assert reads == [SYMBOL] and planned["book"]["qty_within_limit"] == "0.01"
    assert planned["warning"].startswith("Only 0.01 sits in the bids at or inside the limit 1980, less than the 0.05")


async def test_close_all_preview_with_an_unreadable_book_still_previews(patch_client, capsys):
    hold_long(patch_client)  # no scripted book: the offline guard refuses the read
    await trade.cmd_positions_close_all(close_all(preview=True))
    (planned,) = parse_stdout(capsys)["would_close"]
    assert "unavailable" in planned["book"] and "warning" not in planned


# --------------------------------------------------------------------------
# Inside the runner
# --------------------------------------------------------------------------


async def test_runner_full_fill_reads_no_book(client, monkeypatch, capsys):  # noqa: F811
    reads = serve_book(monkeypatch, depth(asks=[("2001.000", "1.000")]))
    client.orders.create_order.side_effect = [
        CreateOrderResponse(order_id="5", status="FILLED", cum_qty="0.01", avg_fill_px="2001")
    ]
    await runner.serve(stream(MARKET), client, wide(client))
    first = outputs(capsys)[1]["result"]
    assert first["status"] == "FILLED" and "book" not in first
    assert reads == []


async def test_runner_zero_fill_reads_the_book_once_and_keeps_the_session(client, monkeypatch, capsys):  # noqa: F811
    reads = serve_book(monkeypatch, depth(asks=[("2030.000", "1.000")]))
    client.orders.create_order.side_effect = [
        CreateOrderResponse(order_id="5", status="CANCELLED", cum_qty="0", **NO_LIQUIDITY)
    ]
    result = await runner.serve(stream(MARKET), client, wide(client))
    first = outputs(capsys)[1]["result"]
    assert reads == [SYMBOL] and first["book"]["best_ask"] == {"px": "2030", "qty": "1"}
    assert first["session_continues"] is True and result["reason"] == "eof"
