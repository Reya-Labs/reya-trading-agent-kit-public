"""Fat-finger guards on creates: limit-price band, per-order notional cap, leverage limit and --preview.

Every refusal must happen before anything is signed or sent.
"""

from types import SimpleNamespace

import sys
import threading
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.side import Side

from agent_kit.scripts import trade
from agent_kit.scripts._common import limit_price_band_refusal
from agent_kit.tests.conftest import (
    FakeClient,
    make_asset_def,
    make_asset_price,
    make_balance,
    make_order,
    make_position,
    parse_stdout,
)


def args(*command):
    return trade.build_parser().parse_args(list(command))


def market(*extra, side="long", qty="0.05"):
    return args("order", "market", "eth", "--side", side, "--qty", qty, *extra)


def limit(price, *extra, side="long", qty="0.05"):
    return args("order", "limit", "eth", "--side", side, "--qty", qty, "--price", price, *extra)


@pytest.fixture
def account(patch_client: FakeClient) -> FakeClient:
    """Mark 2000, 100 USDC of collateral and no position."""
    patch_client.get_account_balances.return_value = [make_balance(real_balance="100")]
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="1", status="FILLED", cum_qty="0.05", avg_fill_px="2000"
    )
    return patch_client


async def refused(command, invocation, capsys) -> dict:
    with pytest.raises(SystemExit) as exit_info:
        await command(invocation)
    assert exit_info.value.code == 1  # a refusal exits 1, never 0 or 2
    return parse_stdout(capsys)


# --------------------------------------------------------------------------
# Limit-price band
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price,is_buy,blocked",
    [
        ("2100", True, False),  # exactly 5% above: allowed
        ("2100.01", True, True),
        ("1900", False, False),  # exactly 5% below: allowed
        ("1899.99", False, True),
        ("100", True, False),  # passive side is never refused
        ("40000", False, False),
    ],
)
def test_band_applies_only_through_the_mark(price, is_buy, blocked):
    refusal = limit_price_band_refusal(Decimal(price), Decimal("2000"), is_buy, force=False)
    assert (refusal is not None) is blocked
    if refusal:
        assert refusal["override"] == "--force" and "through the mark" in refusal["message"]


async def test_fat_fingered_limit_buy_is_refused_before_sending(account, capsys):
    result = await refused(trade.cmd_order_limit, limit("20000", qty="0.01"), capsys)
    assert result["refused_by"][0]["check"] == "limit_price_band"
    assert "900.00% above the mark 2000" in result["error"]
    assert "client_order_id" not in result
    account.create_limit_order.assert_not_called()


async def test_force_allows_a_wide_marketable_limit_but_not_beyond_half(account, capsys):
    await trade.cmd_order_limit(limit("2200", "--force", "--qty", "0.01"))
    sent = account.create_limit_order.call_args.args[0]
    assert sent.limit_px == "2200" and sent.is_buy is True
    assert parse_stdout(capsys)["limit_vs_mark_pct"] == 10.0
    account.create_limit_order.reset_mock()
    result = await refused(trade.cmd_order_limit, limit("3001", "--force", "--qty", "0.01"), capsys)
    assert result["refused_by"][0]["override"] is None and "even with --force" in result["error"]
    account.create_limit_order.assert_not_called()


async def test_limit_sell_far_below_mark_is_refused(account, capsys):
    result = await refused(trade.cmd_order_limit, limit("180", side="short", qty="0.01"), capsys)
    assert result["refused_by"][0]["check"] == "limit_price_band" and "below the mark" in result["error"]
    account.create_limit_order.assert_not_called()


# --------------------------------------------------------------------------
# Per-order notional cap and leverage
# --------------------------------------------------------------------------


async def test_a_leveraged_order_is_sent_without_an_override(account, capsys):
    """A tester's 4.6x order passed the engine's margin check but the kit refused it at 1x the account value."""
    await trade.cmd_order_market(market(qty="0.5"))  # 1,000 USDC on a 100 USDC account: 10x
    account.create_limit_order.assert_called_once()
    result = parse_stdout(capsys)
    assert "error" not in result and "refused_by" not in result and result["est_leverage"] == 10.0
    assert result["market_max_leverage"] == 25.0 and result["margin"]["engine_check"] == "ok"
    # No kit limit unless the user sets one in .env.
    assert result["max_leverage"] is None and result["max_order_notional_usd"] is None
    assert result["max_order_notional_source"] is None


async def test_the_markets_own_maximum_is_the_only_default_limit(account, capsys):
    result = await refused(trade.cmd_order_market, market(qty="1.5"), capsys)  # 3,000 USDC: 30x on a 25x market
    assert [r["check"] for r in result["refused_by"]] == ["engine_margin"]
    assert result["refused_by"][0]["override"] == "--skip_margin_check" and result["market_max_leverage"] == 25.0
    account.create_limit_order.assert_not_called()


def exit_status(monkeypatch, *argv) -> int | str | None:
    """Run `trade.main()` as the shell does and return its exit status.

    In its own thread: `main()` calls asyncio.run, which must not close the tests' event loop.
    """
    monkeypatch.setattr(sys, "argv", ["trade", *argv])
    status: list = []

    def run():
        try:
            trade.main()
            status.append(0)
        except SystemExit as exc:
            status.append(exc.code)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return status[0]


def test_the_cli_exits_0_within_the_markets_limit_and_1_above_it(account, monkeypatch, capsys):
    command = ["order", "market", "eth", "--side", "long", "--qty"]
    assert exit_status(monkeypatch, *command, "0.5") == 0  # 10x
    assert parse_stdout(capsys)["est_leverage"] == 10.0
    assert exit_status(monkeypatch, *command, "1.5") == 1  # 30x on a 25x market
    assert [r["check"] for r in parse_stdout(capsys)["refused_by"]] == ["engine_margin"]
    account.create_limit_order.assert_called_once()


async def test_a_leverage_limit_set_in_env_refuses_above_it(account, monkeypatch, capsys):
    monkeypatch.setenv("REYA_MAX_LEVERAGE", "5")
    result = await refused(trade.cmd_order_market, market(qty="0.26"), capsys)  # 520 USDC: 5.2x
    assert [r["check"] for r in result["refused_by"]] == ["max_leverage"] and result["max_leverage"] == 5.0
    assert "5.2x, above the 5x limit the user set (REYA_MAX_LEVERAGE in .env; ETHRUSDPERP itself allows up to 25x)" in (
        result["error"]
    )
    assert result["refused_by"][0]["override"] == "--allow_large"
    account.create_limit_order.assert_not_called()


async def test_allow_large_sends_an_order_above_a_limit_the_user_set(account, monkeypatch, capsys):
    monkeypatch.setenv("REYA_MAX_ORDER_NOTIONAL", "300")
    monkeypatch.setenv("REYA_MAX_LEVERAGE", "5")
    await trade.cmd_order_market(market("--allow_large", qty="0.3"))
    account.create_limit_order.assert_called_once()
    result = parse_stdout(capsys)
    assert result["est_leverage"] == 6.0 and "refused_by" not in result


async def test_a_cap_set_in_env_refuses_above_it(account, monkeypatch, capsys):
    monkeypatch.setenv("REYA_MAX_ORDER_NOTIONAL", "300")
    await trade.cmd_order_market(market(qty="0.12"))  # 240 USDC: above the balance, inside the configured cap
    sent = parse_stdout(capsys)
    assert sent["max_order_notional_usd"] == 300.0 and sent["max_order_notional_source"] == "REYA_MAX_ORDER_NOTIONAL"
    monkeypatch.setenv("REYA_MAX_ORDER_NOTIONAL", "50")
    result = await refused(trade.cmd_order_market, market(qty="0.03"), capsys)
    assert [r["check"] for r in result["refused_by"]] == ["max_order_notional"]
    assert "50.00 USDC the user set (REYA_MAX_ORDER_NOTIONAL in .env)" in result["error"]
    account.create_limit_order.assert_called_once()


async def test_both_limits_set_in_env_each_refuse(account, monkeypatch, capsys):
    monkeypatch.setenv("REYA_MAX_ORDER_NOTIONAL", "300")
    monkeypatch.setenv("REYA_MAX_LEVERAGE", "5")
    result = await refused(trade.cmd_order_market, market(qty="0.3"), capsys)  # 600 USDC: 6x
    assert [r["check"] for r in result["refused_by"]] == ["max_order_notional", "max_leverage"]
    account.create_limit_order.assert_not_called()


async def test_post_trade_leverage_above_limit_is_refused(account, monkeypatch, capsys):
    monkeypatch.setenv("REYA_MAX_LEVERAGE", "5")
    account.get_positions.return_value = [make_position(qty="0.2", avg_entry_price="2000")]  # 400 USDC: 4x
    result = await refused(trade.cmd_order_market, market(qty="0.1"), capsys)
    assert [r["check"] for r in result["refused_by"]] == ["max_leverage"]
    assert result["current_leverage"] == 4.0 and result["est_leverage"] == 6.0 and result["max_leverage"] == 5.0
    assert "6.0x, above the 5x limit the user set" in result["error"]
    account.create_limit_order.assert_not_called()
    monkeypatch.setenv("REYA_MAX_LEVERAGE", "10")
    await trade.cmd_order_market(market(qty="0.1"))
    account.create_limit_order.assert_called_once()


@pytest.mark.parametrize("flag", [[], ["--reduce_only"]])
async def test_an_order_that_only_reduces_is_never_refused_for_size(account, capsys, flag):
    account.get_positions.return_value = [make_position(qty="0.5", avg_entry_price="2000")]  # 10x already
    await trade.cmd_order_market(market(*flag, side="short", qty="0.2"))
    account.create_limit_order.assert_called_once()
    result = parse_stdout(capsys)
    assert result["reduces_position"] is True
    assert result["current_leverage"] == 10.0 and result["est_leverage"] == 6.0


async def test_reduce_only_without_a_position_is_not_reported_as_reducing(account, capsys):
    """Still exempt from size checks (the venue refuses it), but the figures must not claim a reduction."""
    await trade.cmd_order_market(market("--preview", "--reduce_only", side="short", qty="0.2"))
    result = parse_stdout(capsys)
    assert result["reduces_position"] is False and result["refused_by"] == [] and result["would_send"] is True
    assert result["est_leverage"] == result["current_leverage"] == 0.0


async def test_flipping_through_the_position_counts_as_new_exposure(account, monkeypatch, capsys):
    monkeypatch.setenv("REYA_MAX_ORDER_NOTIONAL", "500")
    account.get_positions.return_value = [make_position(qty="0.2")]  # 4x long
    # 0.3 short is 600 USDC, above the 500 USDC cap, though it ends at 2x short.
    result = await refused(trade.cmd_order_market, market(side="short", qty="0.3"), capsys)
    assert result["reduces_position"] is False and result["est_leverage"] == 2.0
    assert [r["check"] for r in result["refused_by"]] == ["max_order_notional"]
    account.create_limit_order.assert_not_called()


async def test_account_value_includes_other_collateral_and_unrealized_pnl(account, capsys):
    account.get_account_balances.return_value = [
        make_balance(real_balance="50"),
        make_balance(asset="ETH", real_balance="0.05"),
        make_balance(account_id=999, real_balance="1000000"),  # another account's balance is not this one's
    ]
    account.reference.get_asset_definitions.return_value = [make_asset_def(asset="ETH", price_haircut="0.1")]
    account.markets.get_asset_oracle_prices.return_value = [make_asset_price("ETH", "2000")]
    account.get_positions.return_value = [make_position(qty="0.01", avg_entry_price="1000")]  # +10 at mark 2000
    await trade.cmd_order_market(market("--preview", qty="0.01"))
    result = parse_stdout(capsys)
    # 50 USDC + 0.05 ETH at 2000 after its 10% haircut (90) + 10 unrealized PnL.
    assert result["account_value_usd"] == 150.0 and result["max_order_notional_usd"] is None
    assert result["current_leverage"] == 0.13 and result["est_leverage"] == 0.27  # 20/150 and 40/150


async def test_other_market_positions_count_toward_leverage(account, monkeypatch, capsys):
    from agent_kit.tests.conftest import make_price

    monkeypatch.setenv("REYA_MAX_LEVERAGE", "5")
    account.get_positions.return_value = [
        make_position(symbol="BTCRUSDPERP", side=Side.A, qty="0.005", avg_entry_price="80000")
    ]
    account.markets.get_perp_markets_summary.return_value = [make_price(symbol="BTCRUSDPERP", mark_price="80000")]
    # A 400 USDC short in BTC is 4x already; 120 USDC more of ETH makes 5.2x.
    result = await refused(trade.cmd_order_market, market(qty="0.06"), capsys)
    assert (result["current_leverage"], result["est_leverage"]) == (4.0, 5.2)
    assert [r["check"] for r in result["refused_by"]] == ["max_leverage"]
    await trade.cmd_order_market(market(qty="0.05"))  # exactly 5x is allowed
    account.create_limit_order.assert_called_once()


async def test_zero_value_account_cannot_open_exposure(account, capsys):
    account.get_account_balances.return_value = []
    result = await refused(trade.cmd_order_market, market("--allow_large", qty="0.01"), capsys)
    assert result["refused_by"][0] == {
        "check": "account_value",
        "message": "The account's value is 0.00 USDC; deposit before opening a position.",
        "override": None,
    }
    # The engine would refuse it too, for initial margin; --allow_large does not override that.
    assert [r["check"] for r in result["refused_by"]] == ["account_value", "engine_margin"]
    account.create_limit_order.assert_not_called()


async def test_unreadable_account_blocks_opening_but_not_a_reduce_only_close(account, capsys):
    account.get_account_balances.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await trade.cmd_order_market(market(qty="0.01"))
    account.create_limit_order.assert_not_called()
    await trade.cmd_order_market(market("--reduce_only", side="short", qty="0.01"))
    account.create_limit_order.assert_called_once()
    assert "risk_unavailable" in parse_stdout(capsys)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "NaN"])
async def test_invalid_limits_in_env_are_errors(account, monkeypatch, value):
    monkeypatch.setenv("REYA_MAX_LEVERAGE", value)
    with pytest.raises(ValueError, match="REYA_MAX_LEVERAGE must be a positive number"):
        await trade.cmd_order_market(market(qty="0.01"))
    account.create_limit_order.assert_not_called()


# --------------------------------------------------------------------------
# --preview
# --------------------------------------------------------------------------


async def test_market_preview_states_size_leverage_and_distance_without_sending(account, capsys):
    await trade.cmd_order_market(market("--preview", "--slippage", "0.02", qty="0.02"))
    account.create_limit_order.assert_not_called()
    result = parse_stdout(capsys)
    assert result["preview"] is True and result["would_send"] is True and result["refused_by"] == []
    assert (result["qty"], result["notional_usd"], result["limit_px_usd"]) == ("0.02", 40.0, "2040")
    assert result["limit_vs_mark_pct"] == 2.0 and result["est_leverage"] == 0.4
    assert "client_order_id" not in result and "order_id" not in result


async def test_limit_preview_reports_refusals_and_exits_zero(account, capsys):
    await trade.cmd_order_limit(limit("2500", "--preview", qty="0.3"))  # leverage is valued at mark: 600/100
    account.create_limit_order.assert_not_called()
    result = parse_stdout(capsys)
    assert result["would_send"] is False
    assert [r["check"] for r in result["refused_by"]] == ["limit_price_band", "engine_margin"]
    assert result["notional_usd"] == 750.0 and result["limit_vs_mark_pct"] == 25.0 and result["est_leverage"] == 6.0


# --------------------------------------------------------------------------
# order modify
# --------------------------------------------------------------------------


def resting(cum_qty="0"):
    return make_order(order_id="123").model_copy(update={"client_order_id": "7", "post_only": True, "cum_qty": cum_qty})


async def test_modify_to_a_price_through_the_mark_needs_force(account, capsys):
    account.get_open_orders.return_value = [resting()]
    account.modify_order = AsyncMock(
        return_value=SimpleNamespace(
            order_id="123",
            client_order_id="7",
            status="OPEN",
            cum_qty="0",
            exec_qty="0",
            first_fill_id=None,
            fill_count=0,
            avg_fill_px=None,
            cancel_reason=None,
            cancel_reason_message=None,
        )
    )
    command = ["order", "modify", "--order_id", "123", "--qty", "0.01", "--price", "2500"]
    result = await refused(trade.cmd_order_modify, args(*command), capsys)
    assert result["refused_by"][0]["check"] == "limit_price_band" and result["order_id"] == "123"
    account.modify_order.assert_not_called()
    await trade.cmd_order_modify(args(*command, "--force"))
    account.modify_order.assert_called_once()


async def test_modify_size_checks_count_only_the_unfilled_remainder(account, monkeypatch, capsys):
    monkeypatch.setenv("REYA_MAX_ORDER_NOTIONAL", "100")
    account.get_open_orders.return_value = [resting(cum_qty="0.04")]
    account.modify_order = AsyncMock(side_effect=AssertionError("must not send"))
    # 0.1 total at 1990 with 0.04 filled: 0.06 remaining = 119.40 USDC against a 100 USDC cap.
    result = await refused(
        trade.cmd_order_modify, args("order", "modify", "--order_id", "123", "--qty", "0.1", "--price", "1990"), capsys
    )
    assert "119.40 USDC is above the per-order cap" in result["error"]
    account.modify_order.assert_not_called()


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------


def test_new_flags_parse_and_default_off():
    parsed = market()
    assert (parsed.preview, parsed.allow_large, parsed.force) == (False, False, False)
    parsed = limit("2000", "--preview", "--allow_large", "--force")
    assert (parsed.preview, parsed.allow_large, parsed.force) == (True, True, True)
    parsed = args("order", "modify", "--order_id", "1", "--qty", "1", "--price", "1", "--allow_large", "--force")
    assert parsed.allow_large and parsed.force
