"""`order market --sl/--tp` and the bounded position wait behind every TP/SL.

Position reads trail an entry's fill, so protection waits for evidence (the entry's side
with a newer trade sequence number), places the stop-loss before the take-profit, reads
both back, and reports `protection`. A miss is loud and never resends the entry.
"""

import argparse
import json
from decimal import Decimal

import pytest
from sdk.open_api.exceptions import ApiException
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.side import Side
from sdk.open_api.models.time_in_force import TimeInForce

from agent_kit.scripts import trade
from agent_kit.tests.conftest import FakeClient, make_order, make_position, make_price, parse_stdout

ENTRY_FILLED = CreateOrderResponse(order_id="entry-1", status="FILLED", cum_qty="0.05", avg_fill_px="2000")


def _market_args(**overrides) -> argparse.Namespace:
    values = dict(
        symbol="ETHRUSDPERP",
        side="long",
        qty=Decimal("0.05"),
        notional=None,
        slippage=Decimal("0.01"),
        reduce_only=False,
        force=False,
        preview=False,
        allow_large=False,
        fees_wait_s=0,
        client_order_id=None,
        sl=trade.trigger_level_arg("0.35%"),
        tp=trade.trigger_level_arg("0.5%"),
        trigger_slippage=trade.DEFAULT_TRIGGER_SLIPPAGE,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _arm(
    client: FakeClient,
    *,
    entry=ENTRY_FILLED,
    before=None,
    after=None,
    hidden_reads=0,
    trigger_errors=(),
    orders_before=(),
    show_triggers=True,
    mark_after="2000",
) -> list:
    """Script the venue around one entry; returns the ordered call log.

    `before` is the position (or None) before the entry; after the entry, position reads
    return `before` for `hidden_reads` reads, then `after` (default: a 0.05 long, seq 2).
    `trigger_errors` maps TAKE_PROFIT/STOP_LOSS to the exception its create raises.
    """
    calls: list = []
    after = make_position(qty="0.05", last_trade_sequence_number=2) if after is None else after
    created: list = []
    post_entry_reads = [0]

    def sent() -> bool:
        return client.create_limit_order.await_count > 0

    async def positions():
        if not sent():
            calls.append("positions:before")
            return [before] if before is not None else []
        post_entry_reads[0] += 1
        calls.append("positions:after")
        if post_entry_reads[0] <= hidden_reads:
            return [before] if before is not None else []
        return [after] if after is not False else []

    async def open_orders():
        calls.append("orders")
        if not show_triggers:
            return list(orders_before)
        return list(orders_before) + [
            make_order(order_id=order_id, order_type=order_type, side=Side.A) for order_id, order_type in created
        ]

    async def create_limit_order(params):
        calls.append("entry")
        return entry

    async def create_trigger_order(params):
        calls.append(f"trigger:{params.trigger_type.value}")
        error = dict(trigger_errors).get(params.trigger_type)
        if error is not None:
            raise error
        order_id = "sl-1" if params.trigger_type == OrderType.STOP_LOSS else "tp-1"
        created.append((order_id, params.trigger_type))
        return CreateOrderResponse(order_id=order_id, status="OPEN", client_order_id=str(params.client_order_id))

    async def mark(symbol):
        return make_price(mark_price=mark_after if sent() else "2000")

    client.get_positions.side_effect = positions
    client.get_open_orders.side_effect = open_orders
    client.create_limit_order.side_effect = create_limit_order
    client.create_trigger_order.side_effect = create_trigger_order
    client.markets.get_perp_market_summary.side_effect = mark
    return calls


def _triggers(client: FakeClient) -> list:
    return [call.args[0] for call in client.create_trigger_order.call_args_list]


async def _run_expecting_exit(args, capsys) -> dict:
    with pytest.raises(SystemExit) as exc:
        await trade.cmd_order_market(args)
    assert exc.value.code == 1
    return parse_stdout(capsys)


# --------------------------------------------------------------------------
# After the fill
# --------------------------------------------------------------------------


async def test_position_visible_on_first_read_arms_stop_then_target(patch_client: FakeClient, capsys):
    calls = _arm(patch_client)
    await trade.cmd_order_market(_market_args())

    stop, target = _triggers(patch_client)
    assert stop.trigger_type == OrderType.STOP_LOSS and target.trigger_type == OrderType.TAKE_PROFIT
    # Percent levels from the 2000 fill; IOC children 1% beyond each trigger, closing the long.
    assert (stop.trigger_px, stop.limit_px) == ("1993", "1973.07")
    assert (target.trigger_px, target.limit_px) == ("2010", "1989.9")
    assert not stop.is_buy and not target.is_buy
    assert stop.time_in_force == TimeInForce.IOC and stop.client_order_id != target.client_order_id
    # Evidence first, then the stop, then the target, then the read-back.
    entry_at = calls.index("entry")
    assert calls[entry_at + 1 : entry_at + 4] == ["positions:after", "trigger:STOP_LOSS", "trigger:TAKE_PROFIT"]
    assert "orders" in calls[entry_at + 4 :]

    output = parse_stdout(capsys)
    protection = output["protection"]
    assert "error" not in output and protection["state"] == "protected" and protection["verified"] is True
    assert protection["reference"] == "avg_fill_px" and protection["reference_px_usd"] == "2000"
    assert protection["position"] == {"side": "long", "qty": "0.05", "last_trade_sequence_number": 2}
    assert protection["stop_loss"]["order_id"] == "sl-1" and protection["stop_loss"]["visible"] is True
    assert protection["take_profit"]["order_id"] == "tp-1" and protection["take_profit"]["armed"] is True
    assert "stop-loss at 1993 (limit 1973.07)" in protection["message"]
    assert "protection_plan" not in output


async def test_position_visible_after_several_reads_still_protects(patch_client: FakeClient, capsys):
    calls = _arm(patch_client, hidden_reads=3)
    await trade.cmd_order_market(_market_args())
    entry_at = calls.index("entry")
    assert calls[entry_at + 1 : entry_at + 6] == ["positions:after"] * 4 + ["trigger:STOP_LOSS"]
    assert parse_stdout(capsys)["protection"]["state"] == "protected"


async def test_position_never_visible_places_nothing_and_says_unprotected(patch_client: FakeClient, capsys):
    calls = _arm(patch_client, after=False)
    output = await _run_expecting_exit(_market_args(), capsys)

    assert patch_client.create_trigger_order.await_count == 0
    assert patch_client.create_limit_order.await_count == 1  # the entry is never resent
    assert calls.count("positions:after") == trade.ENTRY_POSITION_READS
    assert output["protection"]["state"] == "unprotected"
    assert output["error"].startswith("Entry filled 0.05 ETHRUSDPERP") and "UNPROTECTED" in output["error"]
    # The exact commands to arm it once the position shows, and never a repeat of the entry.
    assert (
        "order sl ETHRUSDPERP --side long --trigger_px 1993 --limit_px 1973.07 --time_in_force IOC" in output["error"]
    )
    assert "order tp ETHRUSDPERP --side long --trigger_px 2010" in output["error"]
    assert "Never repeat the entry" in output["error"]
    assert output["filled_qty"] == "0.05" and output["order_id"] == "entry-1"


async def test_wrong_side_position_is_not_evidence(patch_client: FakeClient, capsys):
    _arm(patch_client, after=make_position(side=Side.A, last_trade_sequence_number=2))
    output = await _run_expecting_exit(_market_args(), capsys)
    assert patch_client.create_trigger_order.await_count == 0
    assert output["protection"]["state"] == "unprotected"


async def test_adding_to_a_position_waits_for_a_newer_trade_sequence(patch_client: FakeClient, capsys):
    before = make_position(qty="0.05", last_trade_sequence_number=10)
    after = make_position(qty="0.1", last_trade_sequence_number=11)
    calls = _arm(patch_client, before=before, after=after, hidden_reads=2)
    await trade.cmd_order_market(_market_args())
    # The two stale reads still show the old long; only the newer sequence counts as the fill.
    entry_at = calls.index("entry")
    assert calls[entry_at + 1 : entry_at + 5] == ["positions:after"] * 3 + ["trigger:STOP_LOSS"]
    protection = parse_stdout(capsys)["protection"]
    assert protection["state"] == "protected" and protection["position"]["qty"] == "0.1"


async def test_partial_fill_protects_what_filled_and_keeps_the_ioc_error(patch_client: FakeClient, capsys):
    partial = CreateOrderResponse(order_id="entry-1", status="CANCELLED", cum_qty="0.03", avg_fill_px="2000")
    _arm(patch_client, entry=partial, after=make_position(qty="0.03", last_trade_sequence_number=2))
    output = await _run_expecting_exit(_market_args(), capsys)
    assert len(_triggers(patch_client)) == 2
    assert output["protection"]["state"] == "protected"
    assert output["error"].startswith("IOC did not fill completely")
    assert "(0.03 ETHRUSDPERP)" in output["protection"]["message"]


async def test_zero_fill_places_nothing(patch_client: FakeClient, capsys):
    unfilled = CreateOrderResponse(order_id="entry-1", status="CANCELLED", cum_qty="0")
    calls = _arm(patch_client, entry=unfilled)
    output = await _run_expecting_exit(_market_args(), capsys)
    assert patch_client.create_trigger_order.await_count == 0
    assert "positions:after" not in calls
    assert output["protection"]["state"] == "not_placed"
    assert output["error"].startswith("IOC did not fill completely")


async def test_unknown_entry_outcome_places_nothing(patch_client: FakeClient, capsys):
    unknown = CreateOrderResponse(order_id="entry-1", status="SOMETHING_NEW")
    _arm(patch_client, entry=unknown)
    output = await _run_expecting_exit(_market_args(), capsys)
    assert patch_client.create_trigger_order.await_count == 0
    assert output["protection"]["state"] == "unknown"
    assert "UNPROTECTED until you arm it" in output["error"] and "Never repeat the entry" in output["error"]


def _rejected(code: str, message: str) -> ApiException:
    return ApiException(status=400, body=json.dumps({"error": code, "message": message}))


async def test_stop_loss_failure_after_fill_is_loud_and_not_retried(patch_client: FakeClient, capsys):
    _arm(
        patch_client,
        trigger_errors={OrderType.STOP_LOSS: _rejected("TRIGGER_LIMIT_OUTSIDE_BAND_ERROR", "Limit outside band")},
    )
    output = await _run_expecting_exit(_market_args(), capsys)

    assert [p.trigger_type for p in _triggers(patch_client)] == [OrderType.STOP_LOSS, OrderType.TAKE_PROFIT]
    assert patch_client.create_limit_order.await_count == 1
    protection = output["protection"]
    assert protection["state"] == "unprotected"
    assert protection["stop_loss"]["armed"] is False and protection["take_profit"]["armed"] is True
    assert output["error"].startswith("Entry filled 0.05 ETHRUSDPERP, but the stop-loss was not placed")
    assert "TRIGGER_LIMIT_OUTSIDE_BAND_ERROR: Limit outside band" in output["error"]
    assert "UNPROTECTED (armed: take-profit at 2010" in output["error"]
    assert "`order sl ETHRUSDPERP --side long --trigger_px 1993" in output["error"]
    # A definitive rejection is not reported as a possibly lost reply.
    assert "may still have reached the venue" not in output["error"]


async def test_lost_stop_loss_reply_points_at_its_client_order_id(patch_client: FakeClient, capsys):
    _arm(patch_client, trigger_errors={OrderType.STOP_LOSS: TimeoutError("read timed out")})
    output = await _run_expecting_exit(_market_args(), capsys)
    stop = output["protection"]["stop_loss"]
    assert output["protection"]["state"] == "unprotected"
    assert f"client_order_id {stop['client_order_id']} before arming again" in stop["error"]


async def test_take_profit_failure_is_partial_with_the_stop_armed(patch_client: FakeClient, capsys):
    _arm(patch_client, trigger_errors={OrderType.TAKE_PROFIT: _rejected("INPUT_VALIDATION_ERROR", "Bad trigger")})
    output = await _run_expecting_exit(_market_args(), capsys)
    assert output["protection"]["state"] == "partial"
    assert output["error"].startswith("Armed: stop-loss at 1993 (limit 1973.07). The take-profit was not placed")
    assert "`order tp ETHRUSDPERP --side long --trigger_px 2010" in output["error"]


async def test_take_profit_only_failure_is_unprotected(patch_client: FakeClient, capsys):
    _arm(patch_client, trigger_errors={OrderType.TAKE_PROFIT: _rejected("INPUT_VALIDATION_ERROR", "Bad trigger")})
    output = await _run_expecting_exit(_market_args(sl=None), capsys)
    assert output["protection"]["state"] == "unprotected" and output["protection"]["stop_loss"] is None


async def test_price_already_through_the_stop_is_not_placed(patch_client: FakeClient, capsys):
    _arm(patch_client, mark_after="1990")
    output = await _run_expecting_exit(_market_args(), capsys)
    assert [p.trigger_type for p in _triggers(patch_client)] == [OrderType.TAKE_PROFIT]
    assert output["protection"]["state"] == "unprotected"
    assert "has already passed the stop-loss level 1993" in output["error"]


async def test_unconfirmed_read_back_is_flagged_not_hidden(patch_client: FakeClient, capsys):
    calls = _arm(patch_client, show_triggers=False)
    await trade.cmd_order_market(_market_args())
    protection = parse_stdout(capsys)["protection"]
    assert protection["state"] == "protected" and protection["verified"] is False
    assert protection["stop_loss"]["visible"] is False
    assert "open-order reads do not show them yet" in protection["message"]
    assert calls.count("orders") == 1 + trade.TRIGGER_READBACK_READS


async def test_unexpected_failure_while_protecting_keeps_the_fill_and_says_so(
    patch_client: FakeClient, capsys, monkeypatch
):
    _arm(patch_client)

    async def broken(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(trade, "_protect_entry", broken)
    output = await _run_expecting_exit(_market_args(), capsys)
    assert output["order_id"] == "entry-1" and output["filled_qty"] == "0.05"
    assert output["protection"]["state"] == "unprotected"
    assert output["error"].startswith("Protecting the entry failed (RuntimeError: unexpected)")
    assert "never repeat the entry" in output["error"]


async def test_percent_levels_follow_the_average_fill_price(patch_client: FakeClient, capsys):
    filled = CreateOrderResponse(order_id="entry-1", status="FILLED", cum_qty="0.05", avg_fill_px="2010")
    _arm(patch_client, entry=filled, mark_after="2010")
    await trade.cmd_order_market(_market_args())
    stop, target = _triggers(patch_client)
    assert stop.trigger_px == "2002.965" and target.trigger_px == "2020.05"


async def test_short_preview_signs_pnl_for_the_short(patch_client: FakeClient, capsys):
    _arm(patch_client)
    await trade.cmd_order_market(_market_args(side="short", preview=True))
    plan = parse_stdout(capsys)["protection_plan"]
    assert plan["stop_loss"]["est_pnl_at_trigger_usd"] == -0.35 and plan["take_profit"]["est_pnl_at_trigger_usd"] == 0.5


async def test_short_entry_puts_the_stop_above_and_buys_to_close(patch_client: FakeClient, capsys):
    _arm(patch_client, after=make_position(side=Side.A, qty="0.05", last_trade_sequence_number=2))
    await trade.cmd_order_market(_market_args(side="short"))
    stop, target = _triggers(patch_client)
    assert stop.is_buy and target.is_buy
    assert (stop.trigger_px, stop.limit_px) == ("2007", "2027.07")
    assert (target.trigger_px, target.limit_px) == ("1990", "2009.9")
    assert parse_stdout(capsys)["protection"]["state"] == "protected"


async def test_absolute_levels_are_used_as_given(patch_client: FakeClient, capsys):
    _arm(patch_client)
    args = _market_args(sl=trade.trigger_level_arg("1900"), tp=trade.trigger_level_arg("2100"))
    await trade.cmd_order_market(args)
    stop, target = _triggers(patch_client)
    assert (stop.trigger_px, target.trigger_px) == ("1900", "2100")


# --------------------------------------------------------------------------
# Before sending
# --------------------------------------------------------------------------


async def test_preview_shows_the_planned_protection_and_sends_nothing(patch_client: FakeClient, capsys):
    _arm(patch_client)
    await trade.cmd_order_market(_market_args(preview=True))
    assert patch_client.create_limit_order.await_count == 0
    assert patch_client.create_trigger_order.await_count == 0
    output = parse_stdout(capsys)
    assert output["preview"] is True and output["would_send"] is True
    plan = output["protection_plan"]
    assert plan["stop_loss"]["est_trigger_px_usd"] == "1993" and plan["stop_loss"]["est_limit_px_usd"] == "1973.07"
    # 0.05 x (1993 - 2000) at the trigger and 0.05 x (1973.07 - 2000) at the child limit, before fees.
    assert plan["stop_loss"]["est_pnl_at_trigger_usd"] == -0.35
    assert plan["stop_loss"]["est_pnl_at_limit_usd"] == -1.35
    # Signed: a take-profit child limit below the entry is a worst-case loss, not a profit.
    assert plan["take_profit"]["est_pnl_at_trigger_usd"] == 0.5
    assert plan["take_profit"]["est_pnl_at_limit_usd"] == -0.5  # -0.505, half-even
    assert plan["take_profit"]["est_trigger_vs_mark_pct"] == 0.5
    assert plan["time_in_force"] == "IOC" and plan["trigger_slippage"] == 0.01


@pytest.mark.parametrize(
    "order_type,flag", [(OrderType.STOP_LOSS, "sl"), (OrderType.TAKE_PROFIT, "tp")], ids=["stop", "target"]
)
async def test_armed_trigger_of_the_same_kind_refuses_before_sending(
    patch_client: FakeClient, capsys, order_type, flag
):
    armed = make_order(order_id="old", order_type=order_type, side=Side.A, trigger_px="1800")
    _arm(patch_client, orders_before=[armed])
    output = await _run_expecting_exit(_market_args(), capsys)
    assert patch_client.create_limit_order.await_count == 0
    refusal = next(r for r in output["refused_by"] if r["check"] == "protection_exists")
    assert f"Enter without --{flag}" in refusal["message"]


async def test_armed_trigger_of_the_other_kind_does_not_refuse(patch_client: FakeClient, capsys):
    armed = make_order(order_id="old", order_type=OrderType.STOP_LOSS, side=Side.A, trigger_px="1800")
    _arm(patch_client, orders_before=[armed])
    await trade.cmd_order_market(_market_args(sl=None))
    assert [p.trigger_type for p in _triggers(patch_client)] == [OrderType.TAKE_PROFIT]


async def test_opposite_position_refuses_before_sending(patch_client: FakeClient, capsys):
    _arm(patch_client, before=make_position(side=Side.A, qty="0.02"))
    output = await _run_expecting_exit(_market_args(), capsys)
    assert patch_client.create_limit_order.await_count == 0
    assert any(r["check"] == "protection_opposite_position" for r in output["refused_by"])


async def test_absolute_stop_through_the_mark_refuses_before_sending(patch_client: FakeClient, capsys):
    _arm(patch_client)
    output = await _run_expecting_exit(_market_args(sl=trade.trigger_level_arg("2010")), capsys)
    assert patch_client.create_limit_order.await_count == 0
    refusal = next(r for r in output["refused_by"] if r["check"] == "protection_fires_at_once")
    assert "must be below the mark" in refusal["message"]


async def test_fat_finger_guards_still_apply_to_protected_entries(patch_client: FakeClient, capsys):
    _arm(patch_client)
    output = await _run_expecting_exit(_market_args(qty=Decimal("100")), capsys)  # 200k USDC on 100k
    assert patch_client.create_limit_order.await_count == 0
    assert any(r["check"] == "max_order_notional" for r in output["refused_by"])


async def test_reduce_only_entries_cannot_carry_protection(patch_client: FakeClient):
    with pytest.raises(ValueError, match="drop them for a --reduce_only order"):
        await trade.cmd_order_market(_market_args(reduce_only=True))
    assert patch_client.create_limit_order.await_count == 0


async def test_limit_entries_refuse_protection_with_guidance(patch_client: FakeClient):
    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        price=Decimal("1990"),
        qty=Decimal("0.05"),
        notional=None,
        sl=trade.trigger_level_arg("1%"),
        tp=None,
    )
    with pytest.raises(ValueError, match="attach only to `order market`"):
        await trade.cmd_order_limit(args)
    assert patch_client.create_limit_order.await_count == 0


def test_cli_parses_levels_and_bounds_trigger_slippage():
    parser = trade.build_parser()
    args = parser.parse_args(
        ["order", "market", "ETH", "--side", "long", "--qty", "1", "--sl", "0.35%", "--tp", "2100"]
    )
    assert args.sl == trade.TriggerLevel("0.35%", Decimal("0.0035"), None)
    assert args.tp == trade.TriggerLevel("2100", None, Decimal("2100"))
    assert args.trigger_slippage == Decimal("0.01")
    for bad in (["--sl", "50%"], ["--sl", "-1%"], ["--tp", "0"], ["--sl", "1%", "--trigger_slippage", "0.031"]):
        with pytest.raises(SystemExit):
            parser.parse_args(["order", "market", "ETH", "--side", "long", "--qty", "1", *bad])


# --------------------------------------------------------------------------
# Standalone `order tp/sl`: the same bounded wait as inside the runner
# --------------------------------------------------------------------------


def _trigger_args(**overrides) -> argparse.Namespace:
    values = dict(
        symbol="ETHRUSDPERP",
        side="long",
        trigger_px=Decimal("1900"),
        limit_px=Decimal("1890"),
        time_in_force="IOC",
        force=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


async def test_standalone_stop_loss_waits_for_a_lagging_position(patch_client: FakeClient, capsys):
    reads = iter([[], [], [], [make_position()]])
    patch_client.get_positions.side_effect = lambda: next(reads)
    patch_client.create_trigger_order.return_value = CreateOrderResponse(order_id="sl-1", status="OPEN")
    await trade.cmd_order_sl(_trigger_args())
    assert patch_client.get_positions.await_count == 4
    assert parse_stdout(capsys)["order_id"] == "sl-1"


async def test_standalone_stop_loss_keeps_reading_through_a_flip(patch_client: FakeClient, capsys):
    reads = iter([[make_position(side=Side.A)], [make_position(side=Side.B)]])
    patch_client.get_positions.side_effect = lambda: next(reads)
    patch_client.create_trigger_order.return_value = CreateOrderResponse(order_id="sl-1", status="OPEN")
    await trade.cmd_order_sl(_trigger_args())
    assert patch_client.get_positions.await_count == 2 and "warning" not in parse_stdout(capsys)


async def test_standalone_stop_loss_without_position_reads_to_the_bound_and_refuses(patch_client: FakeClient):
    patch_client.get_positions.return_value = []
    with pytest.raises(ValueError, match="No open position") as exc:
        await trade.cmd_order_sl(_trigger_args())
    assert patch_client.get_positions.await_count == trade.TRIGGER_POSITION_READS
    assert patch_client.create_trigger_order.await_count == 0
    assert "--force" not in str(exc.value) and "never repeat the entry" in str(exc.value)


async def test_standalone_read_errors_are_retried_within_the_bound(patch_client: FakeClient, capsys):
    reads = iter([ApiException(status=503, body="down"), [make_position()]])

    async def positions():
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    patch_client.get_positions.side_effect = positions
    patch_client.create_trigger_order.return_value = CreateOrderResponse(order_id="sl-1", status="OPEN")
    await trade.cmd_order_sl(_trigger_args())
    assert parse_stdout(capsys)["order_id"] == "sl-1"
