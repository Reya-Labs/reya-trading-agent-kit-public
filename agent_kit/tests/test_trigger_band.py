"""The venue's TP/SL limit band: the kit refuses what the engine would reject, and explains its rejections."""

import argparse
import json
from decimal import Decimal

import pytest
from sdk.open_api.exceptions import ApiException
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.side import Side

from agent_kit.scripts import _trigger_band, runner, trade
from agent_kit.scripts._paper import PaperRefusal, arm_trigger
from agent_kit.tests.conftest import FakeClient, make_market_def, make_position, make_price, parse_stdout
from agent_kit.tests.test_paper import NOW, SYMBOL, leveraged_definition, leveraged_long, summary
from agent_kit.tests.test_runner import client  # noqa: F401

OUTSIDE = "Trigger limit_px is outside the market's fill-collar band around trigger_price"
UNCONFIGURED = "Trigger limit band is unconfigured for this market (no fill_price_max_deviation_bps)"
API_FALLBACK = (
    "limitPx cannot be admitted against this market's trigger band: either it sits too far from triggerPx, in "
    "which case move limitPx closer to triggerPx and re-sign, or the market has no trigger band configured, in "
    "which case stops are unavailable on this market and no limitPx is admissible"
)


def _rejected(message: str) -> ApiException:
    return ApiException(status=400, body=json.dumps({"error": "TRIGGER_LIMIT_OUTSIDE_BAND_ERROR", "message": message}))


def _args(**overrides) -> argparse.Namespace:
    values = dict(
        symbol="ETHRUSDPERP",
        side="long",
        trigger_px=Decimal("1900"),
        limit_px=Decimal("1862"),  # 200 bps below the trigger
        time_in_force="IOC",
        force=False,
        group="order",
        action="sl",
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _market(fake: FakeClient, tick="0.01", side=Side.B) -> None:
    fake.reference.get_perp_market_definitions.return_value = [make_market_def(tick_size=tick)]
    fake.get_positions.return_value = [make_position(side=side)]
    fake.markets.get_perp_market_summary.return_value = make_price(oracle_price="2000")
    fake.create_trigger_order.return_value = CreateOrderResponse(order_id="t-1", status="OPEN")


async def _run(command, args, capsys) -> dict:
    with pytest.raises(SystemExit) as exit_info:
        await command(args)
    assert exit_info.value.code == 1
    return parse_stdout(capsys)


# --------------------------------------------------------------------------
# The engine's rule: |limit - trigger| <= floor(trigger_e9 x 300 / 10000), in E9 integers
# --------------------------------------------------------------------------


def test_the_band_is_one_constant_for_the_guard_and_for_trigger_slippage():
    assert _trigger_band.TRIGGER_BAND_BPS == 300 and _trigger_band.TRIGGER_BAND == Decimal("0.03")
    assert trade.MAX_TRIGGER_SLIPPAGE == _trigger_band.TRIGGER_BAND
    assert trade.trigger_slippage_arg("0.03") == Decimal("0.03")
    with pytest.raises(argparse.ArgumentTypeError, match="at most 0.03"):
        trade.trigger_slippage_arg("0.0301")


@pytest.mark.parametrize(
    "trigger,limit,admitted",
    [
        # 3% of 2000 is exactly 60: both edges are admitted, one tick further is not.
        ("2000", "1940", True),
        ("2000", "1939.99", False),
        ("2000", "2060", True),
        ("2000", "2060.01", False),
        # 3% of 1.234567891 is 0.03703703673 in E9 units: the engine floors it to 37037036.
        ("1.234567891", "1.197530855", True),
        ("1.234567891", "1.197530854", False),
        ("1.234567891", "1.271604927", True),
        ("1.234567891", "1.271604928", False),
    ],
)
def test_admits_matches_the_engine_at_the_boundary(trigger, limit, admitted):
    assert _trigger_band.admits(Decimal(trigger), Decimal(limit)) is admitted


def test_a_price_beyond_nine_decimals_is_left_to_the_api():
    assert _trigger_band.admits(Decimal("2000"), Decimal("1999.0000000001")) is None
    assert _trigger_band.refusal("stop-loss", Decimal("2000"), Decimal("1999.0000000001"), Decimal("1e-10")) is None


def test_admissible_limits_are_on_tick_and_inside_the_band():
    bounds = _trigger_band.admissible_limits(Decimal("1.234567891"), Decimal("0.0001"))
    assert bounds is not None
    low, high = bounds
    assert (low, high) == (Decimal("1.1976"), Decimal("1.2716"))
    assert _trigger_band.admits(Decimal("1.234567891"), low) and _trigger_band.admits(Decimal("1.234567891"), high)


@pytest.mark.parametrize("slippage", ["0.0001", "0.01", "0.025", "0.03"])
@pytest.mark.parametrize("position_side", ["long", "short"])
@pytest.mark.parametrize("reference,tick", [("2000", "0.01"), ("0.1234567", "0.0000001"), ("97123.4", "0.1")])
def test_protected_entry_legs_always_fit_the_band(slippage, position_side, reference, tick):
    """The kit's own --sl/--tp legs, up to the largest --trigger_slippage, are never refused."""
    for flag, trigger_type in (("sl", OrderType.STOP_LOSS), ("tp", OrderType.TAKE_PROFIT)):
        level = trade.trigger_level_arg("2%")
        trigger, limit = trade._bracket_prices(
            level, trigger_type, position_side, Decimal(reference), Decimal(tick), Decimal(slippage)
        )
        assert _trigger_band.admits(trigger, limit), (flag, trigger, limit)


# --------------------------------------------------------------------------
# order sl / order tp: refused before anything is read or signed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,side,position,trigger,limit,closest",
    [
        (trade.cmd_order_sl, "long", Side.B, "1900", "1842.99", "1843"),  # 3% of 1900 is 57
        (trade.cmd_order_sl, "short", Side.A, "2100", "2163.01", "2163"),  # 3% of 2100 is 63
        (trade.cmd_order_tp, "long", Side.B, "2100", "2036.99", "2037"),
        (trade.cmd_order_tp, "short", Side.A, "1900", "1957.01", "1957"),
    ],
)
async def test_a_limit_outside_the_band_is_refused_and_nothing_is_sent(
    patch_client: FakeClient, capsys, command, side, position, trigger, limit, closest
):
    _market(patch_client, side=position)
    output = await _run(command, _args(side=side, trigger_px=Decimal(trigger), limit_px=Decimal(limit)), capsys)
    assert patch_client.create_trigger_order.await_count == 0
    assert patch_client.get_positions.await_count == 0  # refused before the position wait
    [refusal] = output["refused_by"]
    assert refusal["check"] == "trigger_band" and refusal["override"] == "--force"
    assert refusal["band_bps"] == 300 and refusal["closest_limit_px"] == closest
    assert Decimal(refusal["distance_bps"]) > 300
    assert "nothing was sent" in output["error"] and f"the closest is {closest}" in output["error"]
    assert "client_order_id" not in output  # never signed


@pytest.mark.parametrize(
    "command,side,position,trigger,limit",
    [
        (trade.cmd_order_sl, "long", Side.B, "1900", "1843"),
        (trade.cmd_order_sl, "short", Side.A, "2100", "2163"),
        (trade.cmd_order_tp, "long", Side.B, "2100", "2037"),
        (trade.cmd_order_tp, "short", Side.A, "1900", "1957"),
    ],
)
async def test_a_limit_exactly_at_the_band_edge_is_sent(
    patch_client: FakeClient, capsys, command, side, position, trigger, limit
):
    _market(patch_client, side=position)
    await command(_args(side=side, trigger_px=Decimal(trigger), limit_px=Decimal(limit)))
    output = parse_stdout(capsys)
    assert patch_client.create_trigger_order.await_count == 1
    assert output["order_id"] == "t-1" and "warning" not in output


async def test_the_guard_checks_the_limit_as_rounded_to_the_tick(patch_client: FakeClient, capsys):
    # A long's sell child rounds up to the tick: 1842.991 becomes 1843.00, exactly at the edge.
    _market(patch_client)
    await trade.cmd_order_sl(_args(limit_px=Decimal("1842.991")))
    assert patch_client.create_trigger_order.call_args.args[0].limit_px == "1843"


async def test_force_sends_an_out_of_band_limit_with_a_warning(patch_client: FakeClient, capsys):
    _market(patch_client)
    await trade.cmd_order_sl(_args(limit_px=Decimal("1805"), force=True))
    output = parse_stdout(capsys)
    assert patch_client.create_trigger_order.await_count == 1
    assert "500.00 bps from the trigger" in output["warning"] and "the venue decides" in output["warning"]


RUNNER_SL = ["order", "sl", "ETHRUSDPERP", "--side", "long", "--trigger_px", "1900", "--time_in_force", "IOC"]


async def test_inside_the_runner_the_refusal_writes_nothing_so_the_session_continues(client):  # noqa: F811
    before = client.write_attempts
    result = await runner.execute_request(client, {"id": "r1", "command": [*RUNNER_SL, "--limit_px", "1800"]})
    assert result["refused_by"][0]["check"] == "trigger_band"
    client.orders.create_order.assert_not_awaited()
    assert client.write_attempts == before  # the runner halts only after a write


async def test_force_stays_unavailable_inside_the_runner(client):  # noqa: F811
    with pytest.raises(ValueError, match="overrides are unavailable"):
        await runner.execute_request(client, {"id": "r2", "command": [*RUNNER_SL, "--limit_px", "1800", "--force"]})
    client.orders.create_order.assert_not_awaited()


# --------------------------------------------------------------------------
# The venue's two causes behind TRIGGER_LIMIT_OUTSIDE_BAND_ERROR
# --------------------------------------------------------------------------


def test_classify_rejection_reads_the_engine_message():
    assert _trigger_band.classify_rejection(UNCONFIGURED) == "no_band"
    assert _trigger_band.classify_rejection(OUTSIDE) == "outside_band"
    assert _trigger_band.classify_rejection(API_FALLBACK) == "unknown"
    assert _trigger_band.classify_rejection(None) == "unknown"


async def test_a_market_with_no_band_says_it_accepts_no_tp_sl(patch_client: FakeClient, capsys):
    _market(patch_client)
    patch_client.create_trigger_order.side_effect = _rejected(UNCONFIGURED)
    output = await _run(trade.cmd_order_sl, _args(), capsys)
    assert output["error_code"] == "TRIGGER_LIMIT_OUTSIDE_BAND_ERROR" and output["message"] == UNCONFIGURED
    assert output["trigger_band"]["reason"] == "no_band"
    assert "ETHRUSDPERP accepts no TP/SL" in output["error"]
    assert "Do not open a protected entry" in output["error"] and "positions close_all --symbol" in output["error"]
    assert output["client_order_id"]  # it was signed and sent; the venue refused it


async def test_a_limit_the_venue_finds_outside_its_band_gets_the_range(patch_client: FakeClient, capsys):
    # The kit's 300 bps admits 1862, so the venue's band must have narrowed.
    _market(patch_client)
    patch_client.create_trigger_order.side_effect = _rejected(OUTSIDE)
    output = await _run(trade.cmd_order_sl, _args(), capsys)
    band = output["trigger_band"]
    assert band["reason"] == "outside_band" and band["distance_bps"] == "200.00"
    assert "outside the venue's trigger band" in output["error"]
    assert "the venue's band is now narrower" in output["error"]


async def test_an_unexplained_band_rejection_names_both_causes(patch_client: FakeClient, capsys):
    _market(patch_client)
    patch_client.create_trigger_order.side_effect = _rejected(API_FALLBACK)
    output = await _run(trade.cmd_order_sl, _args(), capsys)
    assert output["trigger_band"]["reason"] == "unknown"
    assert "a --limit_px from 1843 to 1957" in output["error"]
    assert "or ETHRUSDPERP accepts no TP/SL at all" in output["error"]


async def test_other_trigger_rejections_are_reported_as_before(patch_client: FakeClient, capsys):
    _market(patch_client)
    patch_client.create_trigger_order.side_effect = ApiException(
        status=400, body=json.dumps({"error": "TRIGGER_ALREADY_EXISTS_ERROR", "message": "already armed"})
    )
    with pytest.raises(ApiException):  # reported by the CLI's usual error path, unchanged
        await trade.cmd_order_sl(_args())
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------
# A protected entry whose stop-loss meets a market with no band
# --------------------------------------------------------------------------


async def test_a_protected_entry_on_a_market_with_no_band_says_to_close_not_to_arm(patch_client: FakeClient, capsys):
    from agent_kit.tests.test_entry_protection import _arm, _market_args, _run_expecting_exit

    rejection = ApiException(
        status=400, body=json.dumps({"error": "TRIGGER_LIMIT_OUTSIDE_BAND_ERROR", "message": UNCONFIGURED})
    )
    _arm(patch_client, trigger_errors={OrderType.STOP_LOSS: rejection, OrderType.TAKE_PROFIT: rejection})
    output = await _run_expecting_exit(_market_args(), capsys)
    protection = output["protection"]
    assert protection["state"] == "unprotected"
    assert protection["stop_loss"]["trigger_band"]["reason"] == "no_band"
    assert "TRIGGER_LIMIT_OUTSIDE_BAND_ERROR: " + UNCONFIGURED in output["error"]
    assert "No TP/SL can be armed on ETHRUSDPERP" in output["error"]
    assert "Arm it with" not in output["error"]
    assert patch_client.create_limit_order.await_count == 1  # the entry was never resent


# --------------------------------------------------------------------------
# Paper refuses the same way
# --------------------------------------------------------------------------


def test_paper_refuses_an_out_of_band_limit_as_live_does():
    s, _ = leveraged_long(qty="10")
    definitions = [leveraged_definition()]
    with pytest.raises(PaperRefusal) as refused:
        arm_trigger(s, SYMBOL, "stop_loss", "long", "95", "92.14", definitions, [summary()], NOW, time_in_force="IOC")
    [refusal] = refused.value.details["refused_by"]
    assert refusal["check"] == "trigger_band" and refusal["closest_limit_px"] == "92.15"
    assert "nothing was sent" in str(refused.value)
    assert SYMBOL not in s["triggers"]
    # At the edge it arms, as live sends it.
    s, result = arm_trigger(
        s, SYMBOL, "stop_loss", "long", "95", "92.15", definitions, [summary()], NOW, time_in_force="IOC"
    )
    assert result["status"] == "armed"


def test_paper_with_force_answers_as_the_venue_would():
    s, _ = leveraged_long(qty="10")
    with pytest.raises(PaperRefusal) as refused:
        arm_trigger(
            s,
            SYMBOL,
            "take_profit",
            "long",
            "105",
            "100",
            [leveraged_definition()],
            [summary()],
            NOW,
            time_in_force="IOC",
            force=True,
        )
    details = refused.value.details
    assert details["error_code"] == "TRIGGER_LIMIT_OUTSIDE_BAND_ERROR"
    assert details["trigger_band"]["reason"] == "outside_band"
    assert SYMBOL not in s["triggers"]
