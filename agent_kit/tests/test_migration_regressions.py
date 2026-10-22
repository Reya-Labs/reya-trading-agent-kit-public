"""Failure modes that must not be presented to an agent as successful trades."""

from unittest.mock import AsyncMock

import pytest
from sdk.open_api.models.cancel_order_response import CancelOrderResponse
from sdk.open_api.models.create_order_response import CreateOrderResponse

from agent_kit.scripts import query, trade
from agent_kit.tests.conftest import (
    make_asset_def,
    make_asset_price,
    make_balance,
    make_order,
    make_position,
    make_price,
    parse_stdout,
)


def market_args():
    return trade.build_parser().parse_args(["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "0.05"])


@pytest.mark.parametrize("status,filled", [("CANCELLED", "0"), ("CANCELLED", "0.02"), ("UNKNOWN", None)])
async def test_ioc_order_id_does_not_prove_fill(patch_client, capsys, status, filled):
    patch_client.markets.get_perp_market_summary.return_value = make_price()
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="123", status=status, cum_qty=filled, cancel_reason="IOC_CANCELLED" if status == "CANCELLED" else None
    )
    with pytest.raises(SystemExit) as exc:
        await trade.cmd_order_market(market_args())
    assert exc.value.code == 1
    result = parse_stdout(capsys)
    assert result["order_id"] == "123"
    assert result["filled_qty"] == filled
    assert result["status"] == status
    assert "error" in result
    patch_client.create_limit_order.assert_awaited_once()


async def test_missing_price_does_not_submit_order(patch_client):
    patch_client.markets.get_perp_market_summary.return_value = make_price(mark_price=None)
    with pytest.raises(ValueError, match="unavailable"):
        await trade.cmd_order_market(market_args())
    patch_client.create_limit_order.assert_not_awaited()


async def test_missing_public_prices_remain_null(patch_market_api, capsys):
    patch_market_api.get_perp_market_summary.return_value = make_price(oracle_price=None, mark_price=None)
    await query.cmd_prices(query.build_parser().parse_args(["prices", "--symbol", "ETHRUSDPERP"]))
    result = parse_stdout(capsys)
    assert result["oracle_price_usd"] is None
    assert result["mark_price_usd"] is None
    assert "pool_price_usd" not in result


@pytest.mark.parametrize("readback", ["residual", "failed"])
async def test_close_needs_flat_readback(patch_client, capsys, readback, monkeypatch):
    monkeypatch.setattr(trade.asyncio, "sleep", AsyncMock())
    patch_client.get_positions.side_effect = [[make_position()]] + (
        [[make_position(qty="0.01")]] * 5 if readback == "residual" else [RuntimeError("read timeout")]
    )
    patch_client.markets.get_perp_market_summary.return_value = make_price()
    patch_client.create_limit_order.return_value = CreateOrderResponse(order_id="123", status="FILLED", cum_qty="0.05")
    with pytest.raises(SystemExit):
        await trade.cmd_positions_close_all(trade.build_parser().parse_args(["positions", "close_all", "--all"]))
    result = parse_stdout(capsys)
    assert result["closed_count"] == 0
    assert "error" in result
    assert result["close_orders"][0]["order_id"] == "123"
    assert patch_client.create_limit_order.call_args.args[0].reduce_only is True
    patch_client.create_limit_order.assert_awaited_once()


async def test_cancel_unknown_status_fails(patch_client, capsys):
    patch_client.get_open_orders.return_value = [make_order(order_id="123")]
    patch_client.cancel_order.return_value = CancelOrderResponse(order_id="123", status="UNKNOWN")
    with pytest.raises(SystemExit):
        await trade.cmd_order_cancel(trade.build_parser().parse_args(["order", "cancel", "--order_id", "123"]))
    assert parse_stdout(capsys)["status"] == "UNKNOWN"


async def test_cancel_cannot_target_another_account(patch_client):
    patch_client.get_open_orders.return_value = [make_order(order_id="123", account_id=999)]
    with pytest.raises(ValueError, match="configured account"):
        await trade.cmd_order_cancel(trade.build_parser().parse_args(["order", "cancel", "--order_id", "123"]))
    patch_client.cancel_order.assert_not_awaited()


async def test_cancel_all_leaves_spot_orders_untouched(patch_client, capsys):
    patch_client.get_open_orders.return_value = [make_order(order_id="123", symbol="WETHRUSD")]
    await trade.cmd_order_cancel_all(trade.build_parser().parse_args(["order", "cancel_all", "--all"]))
    assert parse_stdout(capsys)["cancelled_count"] == 0
    patch_client.cancel_order.assert_not_awaited()


async def test_close_rejects_misspelled_symbol(patch_client):
    with pytest.raises(ValueError, match="Unknown perp symbol"):
        await trade.cmd_positions_close_all(
            trade.build_parser().parse_args(["positions", "close_all", "--symbol", "TYPO"])
        )
    patch_client.create_limit_order.assert_not_awaited()


async def test_collateral_prices_are_keyed_by_asset_not_spot_symbol(patch_client, capsys):
    patch_client.get_account_balances.return_value = [make_balance(asset="USDC", real_balance="100")]
    patch_client.reference.get_asset_definitions.return_value = [
        make_asset_def(asset="USDC", spot_market_symbol=None, price_haircut="0.1")
    ]
    patch_client.markets.get_asset_oracle_prices.return_value = [make_asset_price("USDC", "0.98")]
    await query.cmd_margin(query.build_parser().parse_args(["margin"]))
    result = parse_stdout(capsys)
    assert result["collateral_value_usd"] == 98
    assert result["effective_collateral_usd"] == 88.2


@pytest.mark.parametrize("field,value", [("--qty", "0"), ("--qty", "-1"), ("--price", "0"), ("--price", "NaN")])
def test_invalid_price_or_size_stops_at_parser(field, value, capsys):
    args = ["order", "limit", "ETHRUSDPERP", "--side", "long", "--qty", "0.05", "--price", "2000"]
    args[args.index(field) + 1] = value
    with pytest.raises(SystemExit) as exc:
        trade.build_parser().parse_args(args)
    assert exc.value.code == 1
    assert "error" in parse_stdout(capsys)


def test_trigger_requires_execution_choice(capsys):
    with pytest.raises(SystemExit):
        trade.build_parser().parse_args(["order", "tp", "ETHRUSDPERP", "--side", "long", "--trigger_px", "2300"])
    assert "--limit_px" in parse_stdout(capsys)["error"]


async def test_close_waits_for_readback_without_resubmitting(patch_client, capsys, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(trade.asyncio, "sleep", sleep)
    patch_client.get_positions.side_effect = [[make_position()], [make_position()], []]
    patch_client.markets.get_perp_market_summary.return_value = make_price()
    patch_client.create_limit_order.return_value = CreateOrderResponse(order_id="123", status="FILLED", cum_qty="0.05")
    await trade.cmd_positions_close_all(trade.build_parser().parse_args(["positions", "close_all", "--all"]))
    result = parse_stdout(capsys)
    assert result["closed_count"] == 1
    assert result["remaining_positions"] == []
    assert "error" not in result
    patch_client.create_limit_order.assert_awaited_once()
    sleep.assert_awaited_once_with(0.5)
