"""`order market` without --sl/--tp says what, if anything, protects the position on Reya.

An entry sent with no stop-loss reports `protection.state: "not_requested"`, whether a stop-loss
already armed on the market covers the position (`stop_loss_armed`), and a message to relay. A
close reports nothing. The read never delays the send and never changes the order's outcome.
"""

import argparse
import asyncio
import time
from decimal import Decimal

import pytest
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.side import Side

from agent_kit.scripts import runner, trade
from agent_kit.tests.conftest import FakeClient, make_order, make_position, parse_stdout

FILLED = CreateOrderResponse(order_id="entry-1", status="FILLED", cum_qty="0.05", avg_fill_px="2000")


def _args(**overrides) -> argparse.Namespace:
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
        sl=None,
        tp=None,
        trigger_slippage=trade.DEFAULT_TRIGGER_SLIPPAGE,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _stop(side: Side, order_type: OrderType = OrderType.STOP_LOSS, **kwargs):
    return make_order(order_id=f"{order_type.value}-{side.value}", order_type=order_type, side=side, **kwargs)


async def test_preview_of_an_entry_without_a_stop_says_the_position_has_none(patch_client: FakeClient, capsys):
    await trade.cmd_order_market(_args(preview=True))
    plan = parse_stdout(capsys)

    assert plan["preview"] is True and plan["would_send"] is True
    protection = plan["protection"]
    assert protection["state"] == "not_requested"
    assert protection["stop_loss"] is None and protection["take_profit"] is None
    assert protection["stop_loss_armed"] is False and protection["take_profit_armed"] is False
    assert "no stop-loss on Reya" in protection["message"]
    patch_client.create_limit_order.assert_not_awaited()


async def test_a_stop_already_armed_for_that_side_covers_the_entry(patch_client: FakeClient, capsys):
    # A sell stop-loss closes a long: it covers this long entry too.
    patch_client.get_positions.return_value = [make_position(qty="0.05")]
    patch_client.get_open_orders.return_value = [_stop(Side.A), _stop(Side.A, OrderType.TAKE_PROFIT)]
    await trade.cmd_order_market(_args(preview=True))
    protection = parse_stdout(capsys)["protection"]

    assert protection["stop_loss_armed"] is True and protection["take_profit_armed"] is True
    assert "already armed" in protection["message"] and "no stop-loss" not in protection["message"]


@pytest.mark.parametrize(
    "orders",
    [
        [_stop(Side.B)],  # a buy stop-loss protects a short, not this long
        [_stop(Side.A, symbol="BTCRUSDPERP")],  # another market
        [_stop(Side.A, account_id=1)],  # another account
        [make_order(order_type=OrderType.LIMIT, side=Side.A)],  # a resting limit is not a stop
    ],
)
async def test_only_a_stop_for_this_market_side_and_account_counts(patch_client: FakeClient, capsys, orders):
    patch_client.get_open_orders.return_value = orders
    await trade.cmd_order_market(_args(preview=True))
    assert parse_stdout(capsys)["protection"]["stop_loss_armed"] is False


async def test_a_live_entry_reports_it_and_reads_only_after_the_send(patch_client: FakeClient, capsys):
    calls: list = []

    async def open_orders():
        calls.append("orders")
        return []

    async def create(params):
        calls.append("entry")
        return FILLED

    patch_client.get_open_orders.side_effect = open_orders
    patch_client.create_limit_order.side_effect = create
    await trade.cmd_order_market(_args())  # exits 0: an entry without a stop is not an error
    result = parse_stdout(capsys)

    assert calls == ["entry", "orders"]
    assert "error" not in result
    assert result["protection"]["state"] == "not_requested"
    assert result["protection"]["stop_loss_armed"] is False
    assert "no stop-loss on Reya" in result["protection"]["message"]


async def test_a_failed_read_never_changes_the_outcome(patch_client: FakeClient, capsys):
    patch_client.get_open_orders.side_effect = RuntimeError("read failed")
    patch_client.create_limit_order.return_value = FILLED
    await trade.cmd_order_market(_args())
    result = parse_stdout(capsys)

    assert "error" not in result and result["status"] == "FILLED"
    assert result["protection"]["stop_loss_armed"] is None
    assert "could not read" in result["protection"]["message"]


async def test_an_entry_that_filled_nothing_says_so(patch_client: FakeClient, capsys):
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="entry-1", status="CANCELLED", cum_qty="0"
    )
    with pytest.raises(SystemExit):
        await trade.cmd_order_market(_args())
    result = parse_stdout(capsys)
    assert result["protection"]["state"] == "not_requested"
    assert "did not fill" in result["protection"]["message"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"reduce_only": True, "side": "short"},  # a close with --reduce_only
        {"side": "short", "qty": Decimal("0.02")},  # an opposite order that only reduces the long
    ],
)
async def test_a_close_reports_no_protection(patch_client: FakeClient, capsys, overrides):
    patch_client.get_positions.return_value = [make_position(qty="0.05")]
    await trade.cmd_order_market(_args(preview=True, **overrides))
    plan = parse_stdout(capsys)
    assert "protection" not in plan


async def test_a_protected_entry_keeps_its_own_protection_plan(patch_client: FakeClient, capsys):
    await trade.cmd_order_market(_args(preview=True, sl=trade.trigger_level_arg("1%")))
    plan = parse_stdout(capsys)
    assert "protection" not in plan and plan["protection_plan"]["stop_loss"] is not None


def test_an_entry_without_a_stop_does_not_halt_the_runner():
    # An IOC acknowledged as cancelled with its fills keeps the session; reporting no stop must not change that.
    result = {
        "order_id": "1",
        "status": "CANCELLED",
        "filled_qty": "0.01",
        "protection": {"state": "not_requested"},
    }
    assert runner.definitive_outcome(result, ["order", "market", "ETHRUSDPERP"]) is True


class WarmClient(FakeClient):
    """The runner's client: open orders from its warm order feed, or None while the feed is unverified."""

    def __init__(self, warm_orders):
        super().__init__()
        self.warm_orders = warm_orders

    def warm_open_orders(self):
        return self.warm_orders


async def test_inside_the_runner_the_warm_order_feed_answers_without_a_read(patch_client, monkeypatch, capsys):
    client = WarmClient([_stop(Side.A)])
    monkeypatch.setattr("agent_kit.scripts.trade.ReyaTradingClient", lambda *a, **kw: client)
    client.create_limit_order.return_value = FILLED
    await trade.cmd_order_market(_args())

    assert parse_stdout(capsys)["protection"]["stop_loss_armed"] is True
    client.get_open_orders.assert_not_awaited()


async def test_an_unverified_feed_falls_back_to_one_rest_read(patch_client, monkeypatch, capsys):
    client = WarmClient(None)
    client.get_open_orders.return_value = [_stop(Side.A)]
    monkeypatch.setattr("agent_kit.scripts.trade.ReyaTradingClient", lambda *a, **kw: client)
    client.create_limit_order.return_value = FILLED
    await trade.cmd_order_market(_args())

    assert parse_stdout(capsys)["protection"]["stop_loss_armed"] is True
    assert client.get_open_orders.await_count == 1


async def test_a_buy_stop_covers_a_short_entry(patch_client: FakeClient, capsys):
    patch_client.get_open_orders.return_value = [_stop(Side.B)]
    await trade.cmd_order_market(_args(preview=True, side="short"))
    protection = parse_stdout(capsys)["protection"]
    assert protection["stop_loss_armed"] is True and "short position" in protection["message"]


async def test_a_flip_does_not_count_the_old_sides_stop(patch_client: FakeClient, capsys):
    # Selling 0.1 against a 0.05 long leaves a short: the long's sell stop does not protect it.
    patch_client.get_positions.return_value = [make_position(qty="0.05")]
    patch_client.get_open_orders.return_value = [_stop(Side.A)]
    await trade.cmd_order_market(_args(preview=True, side="short", qty=Decimal("0.1")))
    protection = parse_stdout(capsys)["protection"]
    assert protection["stop_loss_armed"] is False and "short position has no stop-loss" in protection["message"]


async def test_a_stop_that_already_fired_does_not_count(patch_client: FakeClient, capsys):
    fired = _stop(Side.A)
    fired.triggered = True
    patch_client.get_open_orders.return_value = [fired]
    await trade.cmd_order_market(_args(preview=True))
    assert parse_stdout(capsys)["protection"]["stop_loss_armed"] is False


async def test_an_unknown_outcome_says_if_it_filled(patch_client: FakeClient, capsys):
    patch_client.create_limit_order.return_value = CreateOrderResponse(order_id="entry-1", status="UNKNOWN")
    with pytest.raises(SystemExit):
        await trade.cmd_order_market(_args())
    message = parse_stdout(capsys)["protection"]["message"]
    assert message.startswith("The entry's outcome is unknown. If `query positions")
    assert "it has no stop-loss on Reya" in message  # the read found none, so no "check" advice


async def test_a_slow_read_reports_unknown_instead_of_holding_back_the_result(
    patch_client: FakeClient, capsys, monkeypatch
):
    monkeypatch.setattr(trade, "ARMED_READ_TIMEOUT_S", 0.05)

    async def hang():
        await asyncio.sleep(60)

    patch_client.get_open_orders.side_effect = hang
    patch_client.create_limit_order.return_value = FILLED
    started = time.monotonic()
    await trade.cmd_order_market(_args())
    result = parse_stdout(capsys)

    assert time.monotonic() - started < 5
    assert "error" not in result and result["protection"]["stop_loss_armed"] is None


async def test_a_live_close_reads_no_orders(patch_client: FakeClient, capsys):
    patch_client.get_positions.return_value = [make_position(qty="0.05")]
    patch_client.create_limit_order.return_value = FILLED
    await trade.cmd_order_market(_args(side="short", reduce_only=True))
    result = parse_stdout(capsys)
    assert "protection" not in result
    patch_client.get_open_orders.assert_not_awaited()
