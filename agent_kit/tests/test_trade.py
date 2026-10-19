"""Unit tests for `agent_kit/scripts/trade.py`.

Strategy: same as test_query — drive cmd_* directly, mock the SDK, capture
stdout, assert. For writes we also assert the SDK was called with the right
parameters (since the JSON-output contract doesn't fully cover that).
"""

import argparse
from decimal import Decimal

import pytest
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.side import Side
from sdk.open_api.models.time_in_force import TimeInForce

from agent_kit.scripts.trade import (
    cmd_order_cancel,
    cmd_order_cancel_all,
    cmd_order_limit,
    cmd_order_market,
    cmd_order_sl,
    cmd_order_tp,
    cmd_positions_close_all,
)
from agent_kit.tests.conftest import (
    DEFAULT_ACCOUNT_ID,
    FakeClient,
    make_market_def,
    make_order,
    make_position,
    make_price,
    parse_stdout,
)


def _mock_order_response(order_id: str = "test-id", status: str = "FILLED") -> CreateOrderResponse:
    return CreateOrderResponse(
        order_id=order_id, status=status, cum_qty="0.05" if status == "FILLED" else "0", avg_fill_px="2000"
    )


# --------------------------------------------------------------------------
# order market
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_market_computes_qty_from_notional(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """`--notional N` divides by the mark price and rounds down to qty_step_size."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(
        symbol="ETHRUSDPERP", oracle_price="2000", mark_price="2500"
    )
    patch_client.create_limit_order.return_value = _mock_order_response(order_id="ioc-id", status="FILLED")

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        qty=None,
        notional=Decimal("100"),
        slippage=Decimal("0.01"),
        reduce_only=False,
        force=False,
    )
    await cmd_order_market(args)

    # 100 / mark 2500 (the 2000 oracle is ignored) = 0.04 → aligned to step 0.001
    params = patch_client.create_limit_order.call_args.args[0]
    assert params.qty == "0.04"


@pytest.mark.asyncio
async def test_order_market_long_uses_aggressive_buy_slippage(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """A long market order sends a BUY IOC with limit_px = mark × (1 + slippage), rounded UP."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(
        symbol="ETHRUSDPERP", oracle_price="2000", mark_price="2100"
    )
    patch_client.create_limit_order.return_value = _mock_order_response()

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        qty=Decimal("0.05"),
        notional=None,
        slippage=Decimal("0.01"),
        reduce_only=False,
        force=False,
    )
    await cmd_order_market(args)

    params = patch_client.create_limit_order.call_args.args[0]
    assert params.is_buy is True
    assert params.time_in_force == TimeInForce.IOC
    assert float(params.limit_px) == 2121.0  # mark 2100 × 1.01 (oracle 2000 ignored), rounded up to tick 0.001


@pytest.mark.asyncio
async def test_order_market_short_uses_aggressive_sell_slippage(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """A short market order sends a SELL IOC with limit_px = mark × (1 − slippage), rounded DOWN."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(
        symbol="ETHRUSDPERP", oracle_price="2000", mark_price="2100"
    )
    patch_client.create_limit_order.return_value = _mock_order_response()

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="short",
        qty=Decimal("0.05"),
        notional=None,
        slippage=Decimal("0.01"),
        reduce_only=False,
        force=False,
    )
    await cmd_order_market(args)

    params = patch_client.create_limit_order.call_args.args[0]
    assert params.is_buy is False
    assert float(params.limit_px) == 2079.0  # mark 2100 × 0.99


@pytest.mark.asyncio
async def test_order_market_passes_reduce_only(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_limit_order.return_value = _mock_order_response()

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        qty=Decimal("0.05"),
        notional=None,
        slippage=Decimal("0.01"),
        reduce_only=True,
        force=False,
    )
    await cmd_order_market(args)
    params = patch_client.create_limit_order.call_args.args[0]
    assert params.reduce_only is True


@pytest.mark.asyncio
async def test_order_market_raises_for_unknown_symbol(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.reference.get_perp_market_definitions.return_value = []
    args = argparse.Namespace(
        symbol="DOGERUSDPERP",
        side="long",
        qty=Decimal("0.01"),
        notional=None,
        slippage=Decimal("0.01"),
        reduce_only=False,
        force=False,
    )
    with pytest.raises(ValueError, match="Unknown symbol 'DOGERUSDPERP'"):
        await cmd_order_market(args)


# --------------------------------------------------------------------------
# order limit
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_limit_long_rounds_price_down_to_tick(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """For a buy limit, price rounds DOWN — never more aggressive than the user asked."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.01", min_order_qty="0.01"),
    ]
    patch_client.create_limit_order.return_value = _mock_order_response(order_id="lim-1", status="OPEN")

    args = argparse.Namespace(
        symbol="ETHRUSDPERP", side="long", price=Decimal("2000.019"), qty=Decimal("0.05"), notional=None
    )
    await cmd_order_limit(args)
    params = patch_client.create_limit_order.call_args.args[0]
    assert params.is_buy is True
    assert params.time_in_force == TimeInForce.GTC
    assert float(params.limit_px) == 2000.01  # 2000.019 floored to tick 0.01


@pytest.mark.asyncio
async def test_order_limit_short_rounds_price_up_to_tick(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """For a sell limit, price rounds UP — never more aggressive than the user asked."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.01", min_order_qty="0.01"),
    ]
    patch_client.create_limit_order.return_value = _mock_order_response()

    args = argparse.Namespace(
        symbol="ETHRUSDPERP", side="short", price=Decimal("2000.001"), qty=Decimal("0.05"), notional=None
    )
    await cmd_order_limit(args)
    params = patch_client.create_limit_order.call_args.args[0]
    assert params.is_buy is False
    assert float(params.limit_px) == 2000.01  # ceiled to tick 0.01


@pytest.mark.asyncio
async def test_order_limit_computes_qty_from_notional(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.01", min_order_qty="0.01"),
    ]
    patch_client.create_limit_order.return_value = _mock_order_response()

    # At limit price 2000, $100 notional → 0.05
    args = argparse.Namespace(
        symbol="ETHRUSDPERP", side="long", price=Decimal("2000"), qty=None, notional=Decimal("100")
    )
    await cmd_order_limit(args)
    params = patch_client.create_limit_order.call_args.args[0]
    assert params.qty == "0.05"


# --------------------------------------------------------------------------
# order cancel
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_cancel_passes_order_id(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.cancel_order.return_value = _mock_order_response(order_id="abc-123", status="CANCELLED")
    patch_client.get_open_orders.return_value = [make_order(order_id="abc-123")]
    await cmd_order_cancel(argparse.Namespace(order_id="abc-123"))
    patch_client.cancel_order.assert_awaited_once_with(
        symbol="ETHRUSDPERP", account_id=DEFAULT_ACCOUNT_ID, order_id="abc-123"
    )
    output = parse_stdout(capsys)
    assert output == {
        "action": "order_cancel",
        "order_id": "abc-123",
        "client_order_id": None,
        "status": "CANCELLED",
        "symbol": "ETHRUSDPERP",
        "account_id": DEFAULT_ACCOUNT_ID,
    }


# --------------------------------------------------------------------------
# order cancel_all
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_cancel_all_filters_to_configured_account(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    patch_client.get_open_orders.return_value = [
        make_order(order_id="mine-1", account_id=DEFAULT_ACCOUNT_ID),
        make_order(order_id="theirs", account_id=8010),  # other account
        make_order(order_id="mine-2", account_id=DEFAULT_ACCOUNT_ID),
    ]
    patch_client.cancel_order.return_value = _mock_order_response(status="CANCELLED")

    await cmd_order_cancel_all(argparse.Namespace(symbol=None, all=True, preview=False))

    # Only the two orders on PERP_ACCOUNT_ID_1 should be cancelled.
    assert patch_client.cancel_order.await_count == 2
    cancelled_ids = {call.kwargs["order_id"] for call in patch_client.cancel_order.await_args_list}
    assert cancelled_ids == {"mine-1", "mine-2"}

    output = parse_stdout(capsys)
    assert output["cancelled_count"] == 2


@pytest.mark.asyncio
async def test_order_cancel_all_symbol_filter(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.get_open_orders.return_value = [
        make_order(order_id="eth-1", symbol="ETHRUSDPERP"),
        make_order(order_id="btc-1", symbol="BTCRUSDPERP"),
    ]
    patch_client.cancel_order.return_value = _mock_order_response(status="CANCELLED")

    await cmd_order_cancel_all(argparse.Namespace(symbol="ETHRUSDPERP", preview=False))
    assert patch_client.cancel_order.await_count == 1
    patch_client.cancel_order.assert_awaited_once_with(
        symbol="ETHRUSDPERP", account_id=DEFAULT_ACCOUNT_ID, order_id="eth-1"
    )


@pytest.mark.asyncio
async def test_order_cancel_all_collects_per_order_errors(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """One cancellation failing does NOT stop the batch — the failure is reported under `errors`."""
    patch_client.get_open_orders.return_value = [
        make_order(order_id="o1"),
        make_order(order_id="o2"),
    ]
    patch_client.cancel_order.side_effect = [
        _mock_order_response(status="CANCELLED"),
        Exception("backend rejected"),
    ]

    with pytest.raises(SystemExit) as exit_info:
        await cmd_order_cancel_all(argparse.Namespace(symbol=None, all=True, preview=False))
    assert exit_info.value.code == 1
    output = parse_stdout(capsys)
    assert output["cancelled_count"] == 1
    assert output["error_count"] == 1
    assert "backend rejected" in output["errors"][0]["error"]


@pytest.mark.asyncio
async def test_order_cancel_all_empty_state(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.get_open_orders.return_value = []
    await cmd_order_cancel_all(argparse.Namespace(symbol=None, all=True, preview=False))
    output = parse_stdout(capsys)
    assert output["cancelled_count"] == 0
    assert output["cancelled"] == []
    assert patch_client.cancel_order.await_count == 0


# --------------------------------------------------------------------------
# order tp / order sl  (trigger orders)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "side, expected_is_buy, expected_position_side, oracle_price",
    [
        # A TP for a long must sit above the mark; for a short, below.
        ("long", False, "long", "2000"),
        ("buy", False, "long", "2000"),
        ("short", True, "short", "2500"),
        ("sell", True, "short", "2500"),
    ],
)
@pytest.mark.asyncio
async def test_order_tp_derives_is_buy_from_side(
    patch_client: FakeClient,
    capsys: pytest.CaptureFixture,
    side: str,
    expected_is_buy: bool,
    expected_position_side: str,
    oracle_price: str,
) -> None:
    """`--side long` → `is_buy=False` (sell-to-close). `--side short` → `is_buy=True`."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", tick_size="0.001"),
    ]
    pos_side = Side.B if expected_position_side == "long" else Side.A
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=pos_side),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(
        symbol="ETHRUSDPERP", oracle_price=oracle_price, mark_price=oracle_price
    )
    patch_client.create_trigger_order.return_value = _mock_order_response(order_id="tp-1", status="OPEN")

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side=side,
        limit_px=Decimal("2250"),
        time_in_force="IOC",
        trigger_px=Decimal("2300"),
        force=False,
    )
    await cmd_order_tp(args)

    params = patch_client.create_trigger_order.call_args.args[0]
    assert params.is_buy is expected_is_buy
    assert params.symbol == "ETHRUSDPERP"
    assert params.trigger_px == "2300"
    assert params.trigger_type == OrderType.TAKE_PROFIT

    output = parse_stdout(capsys)
    assert output["action"] == "order_tp"
    assert output["position_side"] == expected_position_side
    assert output["trigger_px_usd"] == "2300"
    assert output["order_id"] == "tp-1"
    assert output["status"] == "OPEN"


@pytest.mark.asyncio
async def test_order_sl_uses_sl_order_type(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """`order sl` shares the trigger-order machinery with `order tp`; only the trigger_type differs."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", tick_size="0.001"),
    ]
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_trigger_order.return_value = _mock_order_response(order_id="sl-1", status="OPEN")

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        limit_px=Decimal("2250"),
        time_in_force="IOC",
        trigger_px=Decimal("1900"),
        force=False,
    )
    await cmd_order_sl(args)

    params = patch_client.create_trigger_order.call_args.args[0]
    assert params.trigger_type == OrderType.STOP_LOSS
    assert params.is_buy is False  # long → sell-to-close
    output = parse_stdout(capsys)
    assert output["action"] == "order_sl"


@pytest.mark.asyncio
async def test_order_tp_rounds_trigger_px_to_tick(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", tick_size="0.01"),
    ]
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_trigger_order.return_value = _mock_order_response()

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        limit_px=Decimal("2250"),
        time_in_force="IOC",
        trigger_px=Decimal("2300.123"),
        force=False,
    )
    await cmd_order_tp(args)
    params = patch_client.create_trigger_order.call_args.args[0]
    # 2300.123 with nearest tick 0.01 → 2300.12
    assert params.trigger_px == "2300.12"


@pytest.mark.asyncio
async def test_order_tp_rejects_invalid_side(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="bogus",
        limit_px=Decimal("2250"),
        time_in_force="IOC",
        trigger_px=Decimal("2000"),
        force=False,
    )
    with pytest.raises(ValueError, match="Invalid --side 'bogus'"):
        await cmd_order_tp(args)


@pytest.mark.asyncio
async def test_order_tp_rejects_when_no_position(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """A trigger with no position behind it protects nothing — hard error, no order."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", tick_size="0.001"),
    ]
    patch_client.get_positions.return_value = []
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        limit_px=Decimal("2250"),
        time_in_force="IOC",
        trigger_px=Decimal("2300"),
        force=False,
    )
    with pytest.raises(ValueError, match="No open position"):
        await cmd_order_tp(args)
    assert patch_client.create_trigger_order.await_count == 0


@pytest.mark.asyncio
async def test_order_tp_forced_without_position_warns(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """--force places the unbacked trigger but surfaces the no-position warning."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", tick_size="0.001"),
    ]
    patch_client.get_positions.return_value = []
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_trigger_order.return_value = _mock_order_response(order_id="tp-np", status="OPEN")

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        limit_px=Decimal("2250"),
        time_in_force="IOC",
        trigger_px=Decimal("2300"),
        force=True,
    )
    await cmd_order_tp(args)
    output = parse_stdout(capsys)
    assert "No open position" in output["warning"]
    assert output["status"] == "OPEN"


@pytest.mark.asyncio
async def test_order_tp_rejects_position_side_mismatch(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """--side must agree with the actual position side — a mismatch is almost certainly a mistake."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", tick_size="0.001"),
    ]
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.A),  # short
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        limit_px=Decimal("2250"),
        time_in_force="IOC",
        trigger_px=Decimal("2300"),
        force=False,
    )
    with pytest.raises(ValueError, match="is short, but --side says long"):
        await cmd_order_tp(args)
    assert patch_client.create_trigger_order.await_count == 0


@pytest.mark.asyncio
async def test_order_tp_forced_mismatch_combines_warnings(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """When --force overrides both guardrails, the warning field carries both messages."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", tick_size="0.001"),
    ]
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.A),  # short
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_trigger_order.return_value = _mock_order_response(order_id="tp-mm", status="OPEN")

    # Stated side long + TP at 1900 < mark 2000: side mismatch AND immediate-fire.
    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        limit_px=Decimal("2250"),
        time_in_force="IOC",
        trigger_px=Decimal("1900"),
        force=True,
    )
    await cmd_order_tp(args)
    output = parse_stdout(capsys)
    assert "is short, but --side says long" in output["warning"]
    assert "fire immediately" in output["warning"]


# --------------------------------------------------------------------------
# positions close_all
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_all_filters_to_configured_account(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B, qty="0.05"),
        make_position(account_id=8010, symbol="BTCRUSDPERP", side=Side.B, qty="0.001"),  # other account
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_limit_order.return_value = _mock_order_response(status="FILLED")

    patch_client.get_positions.side_effect = [patch_client.get_positions.return_value, []]
    await cmd_positions_close_all(
        argparse.Namespace(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False)
    )
    # Only the position on PERP_ACCOUNT_ID_1 should be closed.
    assert patch_client.create_limit_order.await_count == 1
    output = parse_stdout(capsys)
    assert output["closed_count"] == 1
    assert output["closed"][0]["symbol"] == "ETHRUSDPERP"


@pytest.mark.asyncio
async def test_close_all_long_sends_reduce_only_sell(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """Closing a long means a SELL IOC with reduce_only=True, limit rounded DOWN with slippage."""
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B, qty="0.05"),
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_limit_order.return_value = _mock_order_response(status="FILLED")

    patch_client.get_positions.side_effect = [patch_client.get_positions.return_value, []]
    await cmd_positions_close_all(
        argparse.Namespace(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False)
    )
    params = patch_client.create_limit_order.call_args.args[0]
    assert params.is_buy is False  # sell-to-close
    assert params.reduce_only is True
    assert params.time_in_force == TimeInForce.IOC
    assert float(params.limit_px) == 1980.0  # 2000 × 0.99


@pytest.mark.asyncio
async def test_close_all_short_sends_reduce_only_buy(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """Closing a short means a BUY IOC with reduce_only=True, limit rounded UP with slippage."""
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.A, qty="0.05"),
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_limit_order.return_value = _mock_order_response(status="FILLED")

    patch_client.get_positions.side_effect = [patch_client.get_positions.return_value, []]
    await cmd_positions_close_all(
        argparse.Namespace(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False)
    )
    params = patch_client.create_limit_order.call_args.args[0]
    assert params.is_buy is True
    assert params.reduce_only is True
    assert float(params.limit_px) == 2020.0  # 2000 × 1.01


@pytest.mark.asyncio
async def test_close_all_symbol_filter(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.get_positions.return_value = [
        make_position(symbol="ETHRUSDPERP", side=Side.B, qty="0.05"),
        make_position(symbol="BTCRUSDPERP", side=Side.B, qty="0.001"),
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
        make_market_def(
            symbol="BTCRUSDPERP", market_id=2, qty_step_size="0.0001", tick_size="0.001", min_order_qty="0.001"
        ),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_limit_order.return_value = _mock_order_response(status="FILLED")

    patch_client.get_positions.side_effect = [patch_client.get_positions.return_value, []]
    await cmd_positions_close_all(
        argparse.Namespace(symbol="ETHRUSDPERP", slippage=Decimal("0.01"), preview=False, force=False)
    )
    assert patch_client.create_limit_order.await_count == 1


@pytest.mark.asyncio
async def test_close_all_collects_per_position_errors(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """One close failing does NOT stop the batch."""
    patch_client.get_positions.return_value = [
        make_position(symbol="ETHRUSDPERP", side=Side.B, qty="0.05"),
        make_position(symbol="BTCRUSDPERP", side=Side.B, qty="0.001"),
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
        make_market_def(
            symbol="BTCRUSDPERP", market_id=2, qty_step_size="0.0001", tick_size="0.001", min_order_qty="0.001"
        ),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_limit_order.side_effect = [
        _mock_order_response(status="FILLED"),
        Exception("liquidity exhausted"),
    ]

    patch_client.get_positions.side_effect = [
        patch_client.get_positions.return_value,
        [make_position(symbol="BTCRUSDPERP", qty="0.001")],
    ]
    with pytest.raises(SystemExit) as exit_info:
        await cmd_positions_close_all(
            argparse.Namespace(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False)
        )
    assert exit_info.value.code == 1
    output = parse_stdout(capsys)
    assert output["closed_count"] == 1
    assert output["error_count"] == 1
    assert "liquidity exhausted" in output["errors"][0]["error"]


@pytest.mark.asyncio
async def test_close_all_empty_state(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.get_positions.return_value = []
    patch_client.reference.get_perp_market_definitions.return_value = []
    await cmd_positions_close_all(
        argparse.Namespace(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False)
    )
    output = parse_stdout(capsys)
    assert output["closed_count"] == 0
    assert output["closed"] == []
    assert patch_client.create_limit_order.await_count == 0


# --------------------------------------------------------------------------
# guardrails — slippage cap and TP/SL immediate-fire check
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_market_blocks_high_slippage_without_force(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """Slippage above the 5% soft cap raises before any network call."""
    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        qty=Decimal("0.05"),
        notional=None,
        slippage=Decimal("0.06"),
        reduce_only=False,
        force=False,
    )
    with pytest.raises(ValueError, match="safety cap"):
        await cmd_order_market(args)
    assert patch_client.create_limit_order.await_count == 0


@pytest.mark.asyncio
async def test_order_market_force_overrides_slippage_cap(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_limit_order.return_value = _mock_order_response()

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        qty=Decimal("0.05"),
        notional=None,
        slippage=Decimal("0.06"),
        reduce_only=False,
        force=True,
    )
    await cmd_order_market(args)
    params = patch_client.create_limit_order.call_args.args[0]
    assert float(params.limit_px) == 2120.0  # 2000 × 1.06


@pytest.mark.asyncio
async def test_order_market_echoes_actual_notional(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """The response reports the post-rounding notional, not the requested one."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(
        symbol="ETHRUSDPERP", oracle_price="1500", mark_price="2000"
    )
    patch_client.create_limit_order.return_value = _mock_order_response(order_id="ioc-id", status="FILLED")

    # 100.5 / mark 2000 = 0.05025 → floored to 0.05 → actual notional 100.00
    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        qty=None,
        notional=Decimal("100.5"),
        slippage=Decimal("0.01"),
        reduce_only=False,
        force=False,
    )
    await cmd_order_market(args)
    output = parse_stdout(capsys)
    assert output["qty"] == "0.05"
    assert output["notional_usd"] == 100.0


@pytest.mark.asyncio
async def test_order_tp_rejects_immediate_fire_trigger(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """A TP below the mark for a long would fire instantly — hard error, no order."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", tick_size="0.001"),
    ]
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        limit_px=Decimal("2250"),
        time_in_force="IOC",
        trigger_px=Decimal("1900"),
        force=False,
    )
    with pytest.raises(ValueError, match="fire immediately"):
        await cmd_order_tp(args)
    assert patch_client.create_trigger_order.await_count == 0


@pytest.mark.asyncio
async def test_order_tp_forced_implausible_trigger_warns(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """--force submits the implausible trigger but surfaces a warning in the response."""
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", tick_size="0.001"),
    ]
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_trigger_order.return_value = _mock_order_response(order_id="tp-f", status="OPEN")

    args = argparse.Namespace(
        symbol="ETHRUSDPERP",
        side="long",
        limit_px=Decimal("2250"),
        time_in_force="IOC",
        trigger_px=Decimal("1900"),
        force=True,
    )
    await cmd_order_tp(args)
    output = parse_stdout(capsys)
    assert "fire immediately" in output["warning"]
    assert output["mark_price_usd"] == 2000.0
    assert output["status"] == "OPEN"


# --------------------------------------------------------------------------
# close_all robustness — exact-qty passthrough, no write retries, below-min, preview
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_all_sends_position_qty_verbatim(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """close_all closes the venue's reported qty exactly — including sub-step dust."""
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B, qty="0.0467"),
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_limit_order.return_value = _mock_order_response(status="FILLED")

    patch_client.get_positions.side_effect = [patch_client.get_positions.return_value, []]
    await cmd_positions_close_all(
        argparse.Namespace(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False)
    )
    params = patch_client.create_limit_order.call_args.args[0]
    assert params.qty == "0.0467"  # not floored to 0.046
    output = parse_stdout(capsys)
    assert output["closed"][0]["qty"] == "0.0467"
    assert "residual_qty" not in output["closed"][0]


@pytest.mark.asyncio
async def test_close_all_does_not_resubmit_quantity_errors(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    patch_client.get_positions.return_value = [make_position(qty="0.0467")]
    patch_client.reference.get_perp_market_definitions.return_value = [make_market_def()]
    patch_client.markets.get_perp_market_summary.return_value = make_price()
    patch_client.create_limit_order.side_effect = ValueError("qty must be a multiple of step size")
    with pytest.raises(SystemExit):
        await cmd_positions_close_all(
            argparse.Namespace(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False)
        )
    assert patch_client.create_limit_order.await_count == 1
    output = parse_stdout(capsys)
    assert output["closed_count"] == 0
    assert output["remaining_positions"][0]["qty"] == "0.0467"
    assert "qty must be a multiple" in output["errors"][0]["error"]


@pytest.mark.asyncio
async def test_close_all_does_not_retry_on_transport_errors(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """Transport failures must never trigger another write: a transport
    error can land after venue acceptance, so resubmitting risks a duplicate order."""
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B, qty="0.0467"),
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_limit_order.side_effect = Exception("Connection timeout")

    with pytest.raises(SystemExit) as exit_info:
        await cmd_positions_close_all(
            argparse.Namespace(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False)
        )
    assert exit_info.value.code == 1
    assert patch_client.create_limit_order.await_count == 1  # no second attempt
    output = parse_stdout(capsys)
    assert output["closed_count"] == 0
    assert output["error_count"] == 1
    assert "Connection timeout" in output["errors"][0]["error"]


@pytest.mark.asyncio
async def test_close_all_attempts_below_min_position_and_annotates(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """A sub-minimum position is attempted (not skipped client-side) and flagged."""
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B, qty="0.005"),
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_limit_order.return_value = _mock_order_response(status="FILLED")

    patch_client.get_positions.side_effect = [patch_client.get_positions.return_value, []]
    await cmd_positions_close_all(
        argparse.Namespace(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False)
    )
    output = parse_stdout(capsys)
    assert output["closed_count"] == 1
    assert output["closed"][0]["below_min_order_qty"] is True


@pytest.mark.asyncio
async def test_close_all_below_min_rejection_lands_in_errors_with_flag(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """If the venue rejects a sub-min close, the agent gets a structured unclosable record."""
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B, qty="0.005"),
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")
    patch_client.create_limit_order.side_effect = Exception("below minimum")

    with pytest.raises(SystemExit) as exit_info:
        await cmd_positions_close_all(
            argparse.Namespace(symbol=None, all=True, slippage=Decimal("0.01"), preview=False, force=False)
        )
    assert exit_info.value.code == 1
    output = parse_stdout(capsys)
    assert output["closed_count"] == 0
    assert output["error_count"] == 1
    err = output["errors"][0]
    assert err["qty"] == "0.005"
    assert err["below_min_order_qty"] is True
    assert "cannot be closed with a reduce-only order" in err["hint"]


@pytest.mark.asyncio
async def test_close_all_preview_sends_nothing(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """--preview computes the full close plan without invoking create_limit_order."""
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B, qty="0.05"),
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", qty_step_size="0.001", tick_size="0.001", min_order_qty="0.01"),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(
        symbol="ETHRUSDPERP", oracle_price="1500", mark_price="2000"
    )

    await cmd_positions_close_all(
        argparse.Namespace(symbol=None, all=True, slippage=Decimal("0.01"), preview=True, force=False)
    )
    assert patch_client.create_limit_order.await_count == 0
    output = parse_stdout(capsys)
    assert output["preview"] is True
    assert output["would_close_count"] == 1
    plan = output["would_close"][0]
    assert plan["close_side"] == "sell"
    assert plan["qty"] == "0.05"
    assert plan["est_notional_usd"] == 100.0
    assert float(plan["limit_px_usd"]) == 1980.0  # mark 2000 × 0.99: same slippage math as the real call
    assert "closed" not in output


@pytest.mark.asyncio
async def test_cancel_all_preview_cancels_nothing(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.get_open_orders.return_value = [
        make_order(order_id="o1", symbol="ETHRUSDPERP"),
        make_order(order_id="o2", symbol="BTCRUSDPERP"),
    ]
    await cmd_order_cancel_all(argparse.Namespace(symbol=None, all=True, preview=True))
    assert patch_client.cancel_order.await_count == 0
    output = parse_stdout(capsys)
    assert output["preview"] is True
    assert output["would_cancel_count"] == 2
    assert {o["order_id"] for o in output["would_cancel"]} == {"o1", "o2"}
    assert output["would_cancel"][0]["order_type"] == "LIMIT"
