"""A dollar-sized order states what it really sends: the requested and effective notional and the next step up."""

import argparse
from decimal import Decimal

import pytest
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.side import Side

from agent_kit.scripts import _workflow as wf
from agent_kit.scripts import workflow
from agent_kit.scripts._sizing import below_minimum_message, minimum_notional, notional_sizing
from agent_kit.scripts.trade import cmd_order_limit, cmd_order_market
from agent_kit.scripts.workflow import market_step as real_market_step
from agent_kit.tests.conftest import FakeClient, make_market_def, make_position, make_price, parse_stdout


def _serve(patch_client: FakeClient, symbol: str, mark: str, step: str, minimum: str, tick: str = "0.01") -> None:
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol=symbol, qty_step_size=step, tick_size=tick, min_order_qty=minimum),
    ]
    patch_client.markets.get_perp_market_summary.return_value = make_price(
        symbol=symbol, oracle_price=mark, mark_price=mark
    )
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="ioc-id", status="FILLED", cum_qty="0", avg_fill_px=mark
    )


def _market(symbol: str, **overrides) -> argparse.Namespace:
    values = dict(
        symbol=symbol,
        side="long",
        qty=None,
        notional=Decimal("30"),
        slippage=Decimal("0.01"),
        reduce_only=False,
        force=False,
        preview=False,
        fees_wait_s=0,
        client_order_id=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


# --------------------------------------------------------------------------
# The sizing block
# --------------------------------------------------------------------------


def test_thirty_dollars_of_sol_states_the_real_size_and_the_next_step() -> None:
    sizing = notional_sizing(
        Decimal("30"), Decimal("0.27"), Decimal("108.15"), Decimal("0.01"), symbol="SOLRUSDPERP", priced_at="mark"
    )
    assert sizing == {
        "sized_by": "notional",
        "requested_notional_usd": 30.0,
        "effective_notional_usd": 29.2,
        "shortfall_usd": 0.8,
        "shortfall_pct": 2.67,
        "priced_at": "mark",
        "price_usd": "108.15",
        "qty_step": "0.01",
        "next_step_up": {"qty": "0.28", "notional_usd": 30.28},
        "message": (
            "$30.00 at the mark 108.15 rounds down to 0.27 SOL (steps of 0.01) ≈ $29.20, 2.67% under. "
            "The next step up is 0.28 SOL ≈ $30.28."
        ),
    }


def test_an_amount_that_fits_the_step_offers_no_step_up() -> None:
    sizing = notional_sizing(
        Decimal("25"), Decimal("0.01"), Decimal("2500"), Decimal("0.001"), symbol="ETHRUSDPERP", priced_at="mark"
    )
    assert sizing["effective_notional_usd"] == 25.0 and sizing["shortfall_usd"] == 0.0
    assert "next_step_up" not in sizing and "message" not in sizing


def test_the_minimum_in_dollars_is_rounded_up_to_the_cent() -> None:
    # 0.01 × 2,345.001 = 23.45001: $23.45 would buy 0.009, so the least that reaches the minimum is $23.46.
    assert minimum_notional(Decimal("0.01"), Decimal("2345.001")) == Decimal("23.46")
    assert minimum_notional(Decimal("0.01"), Decimal("2345")) == Decimal("23.45")
    assert below_minimum_message(
        Decimal("20"), Decimal("0.008"), Decimal("2345"), Decimal("0.01"), symbol="ETHRUSDPERP", priced_at="mark"
    ) == (
        "$20.00 at the mark 2345 is 0.008 ETH, below minimum order qty 0.01 ETH (≈ $23.45 at this price). "
        "Use --notional 23.45 or more, or --qty 0.01."
    )


# --------------------------------------------------------------------------
# order market / order limit
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_of_thirty_dollars_of_eth_says_it_sends_less(patch_client: FakeClient, capsys) -> None:
    _serve(patch_client, "ETHRUSDPERP", mark="2491.67", step="0.001", minimum="0.01")
    await cmd_order_market(_market("ETHRUSDPERP", preview=True))
    output = parse_stdout(capsys)
    patch_client.create_limit_order.assert_not_awaited()
    assert output["preview"] is True and output["qty"] == "0.012" and output["notional_usd"] == 29.9
    sizing = output["sizing"]
    assert (sizing["requested_notional_usd"], sizing["effective_notional_usd"]) == (30.0, 29.9)
    assert sizing["effective_notional_usd"] == output["notional_usd"], "the block agrees with the plan"
    assert sizing["next_step_up"] == {"qty": "0.013", "notional_usd": 32.39}
    assert sizing["message"].startswith("$30.00 at the mark 2491.67 rounds down to 0.012 ETH (steps of 0.001)")


@pytest.mark.asyncio
async def test_a_live_dollar_order_sends_the_floored_qty_and_reports_it(patch_client: FakeClient, capsys) -> None:
    _serve(patch_client, "SOLRUSDPERP", mark="108.15", step="0.01", minimum="0.1")
    await cmd_order_market(_market("SOLRUSDPERP"))
    assert patch_client.create_limit_order.call_args.args[0].qty == "0.27"
    sizing = parse_stdout(capsys)["sizing"]
    assert (sizing["effective_notional_usd"], sizing["shortfall_pct"]) == (29.2, 2.67)
    assert sizing["next_step_up"] == {"qty": "0.28", "notional_usd": 30.28}


@pytest.mark.asyncio
async def test_a_qty_order_carries_no_sizing_block(patch_client: FakeClient, capsys) -> None:
    _serve(patch_client, "SOLRUSDPERP", mark="108.15", step="0.01", minimum="0.1")
    await cmd_order_market(_market("SOLRUSDPERP", qty=Decimal("0.28"), notional=None, preview=True))
    output = parse_stdout(capsys)
    assert output["qty"] == "0.28" and "sizing" not in output


@pytest.mark.asyncio
async def test_below_the_minimum_names_it_in_dollars(patch_client: FakeClient) -> None:
    _serve(patch_client, "ETHRUSDPERP", mark="2345", step="0.001", minimum="0.01")
    with pytest.raises(ValueError) as exc:
        await cmd_order_market(_market("ETHRUSDPERP", notional=Decimal("20")))
    assert str(exc.value) == (
        "$20.00 at the mark 2345 is 0.008 ETH, below minimum order qty 0.01 ETH (≈ $23.45 at this price). "
        "Use --notional 23.45 or more, or --qty 0.01."
    )
    patch_client.create_limit_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_reduce_only_dollar_order_is_still_exempt_from_the_minimum(patch_client: FakeClient, capsys) -> None:
    _serve(patch_client, "ETHRUSDPERP", mark="2345", step="0.001", minimum="0.01")
    patch_client.get_positions.return_value = [make_position(symbol="ETHRUSDPERP", side=Side.B, qty="0.008")]
    await cmd_order_market(_market("ETHRUSDPERP", side="short", notional=Decimal("20"), reduce_only=True))
    params = patch_client.create_limit_order.call_args.args[0]
    assert (params.qty, params.reduce_only) == ("0.008", True)
    assert parse_stdout(capsys)["sizing"]["effective_notional_usd"] == 18.76


@pytest.mark.asyncio
async def test_a_dollar_limit_order_is_sized_at_its_limit(patch_client: FakeClient, capsys) -> None:
    _serve(patch_client, "SOLRUSDPERP", mark="110", step="0.01", minimum="0.1")
    args = argparse.Namespace(
        symbol="SOLRUSDPERP",
        side="long",
        price=Decimal("108.15"),
        qty=None,
        notional=Decimal("30"),
        post_only=False,
        force=False,
        preview=True,
    )
    await cmd_order_limit(args)
    output = parse_stdout(capsys)
    assert output["qty"] == "0.27" and output["sizing"]["priced_at"] == "limit"
    assert output["sizing"]["price_usd"] == "108.15" and output["sizing"]["next_step_up"]["qty"] == "0.28"


# --------------------------------------------------------------------------
# workflow preview
# --------------------------------------------------------------------------


def _preview(qty: str, limit_px: str, reserved: str) -> dict:
    return {
        "market": {"symbol": "ETHRUSDPERP", "mark": "2520", "mark_at": "1792281600"},
        "trigger": {"level": "2506", "direction": "down", "distance_bps": "56", "side": "below"},
        "order": {
            "side": "buy",
            "qty": qty,
            "limit_px": limit_px,
            "reserved_usd": reserved,
            "bound_bps": "25",
            "orders_covered": "2",
        },
        "order_error": None,
        "funding": {"samples": "3", "latest": None, "average_15m_pct_per_hour": None},
        "lines": [],
    }


DOC = {
    "market": "ETHRUSDPERP",
    "steps": [
        {
            "trigger": {"kind": "price_cross", "source": "mark", "direction": "down", "level": "2506"},
            "conditions": [],
            "action": {"kind": "buy", "notional_usd": "27"},
            "repeat": "each_crossing",
        }
    ],
    "limits": {"allocation_usd": "55", "expires_at": 1792886400},
}


def test_a_workflow_preview_states_the_real_size_per_firing_and_what_reaches_the_next_step() -> None:
    # $27 at the limit 2,512.188 is 0.010747 ETH: the service floors it to 0.010 (about $25.12).
    view = wf.preview_view(_preview("0.01", "2512.188", "25.12188"), DOC, Decimal("0.001"))
    sizing = view["sizing"]
    assert (sizing["requested_notional_usd"], sizing["effective_notional_usd"], sizing["shortfall_pct"]) == (
        27.0,
        25.12,
        6.96,
    )
    assert sizing["priced_at"] == "limit"
    # 0.011 × 2,512.188 = 27.634068: --notional 27.64 is the least that the service floors to 0.011.
    assert sizing["next_step_up"] == {"qty": "0.011", "notional_usd": 27.63, "draft_notional": "27.64"}
    assert view["order_text"].endswith(
        "the allocation covers 2 such orders. $27.00 at the limit 2512.188 rounds down to 0.01 ETH (steps of "
        "0.001) ≈ $25.12, 6.96% under. The next step up is 0.011 ETH ≈ $27.63. Draft with --notional 27.64 to send it."
    )


def test_a_workflow_preview_without_the_lot_still_compares_the_amounts() -> None:
    view = wf.preview_view(_preview("0.01", "2512.188", "25.12188"), DOC)
    assert view["sizing"]["effective_notional_usd"] == 25.12 and view["sizing"]["qty_step"] is None
    assert "next_step_up" not in view["sizing"] and view["order_text"].endswith("the allocation covers 2 such orders")


def test_a_workflow_preview_without_an_order_has_no_sizing() -> None:
    preview = {**_preview("0.01", "2512.188", "25.12188"), "order": None}
    preview["order_error"] = {"code": "order_below_minimum", "message": "below the market minimum"}
    assert wf.preview_view(preview, DOC, Decimal("0.001"))["sizing"] is None


@pytest.mark.asyncio
async def test_the_market_step_comes_from_the_public_definitions(monkeypatch) -> None:
    class Definitions:
        def __init__(self, *args) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def get(self, path: str):
            assert path == "/perpMarketDefinitions"
            return [
                {"symbol": "BTCRUSDPERP", "qtyStepSize": "0.0001"},
                {"symbol": "ETHRUSDPERP", "qtyStepSize": "0.001"},
            ]

    monkeypatch.setattr(workflow, "V2Client", Definitions)
    assert await real_market_step("ETHRUSDPERP") == Decimal("0.001")
    assert await real_market_step("SOLRUSDPERP") is None


@pytest.mark.asyncio
async def test_a_failed_step_read_never_fails_the_preview(monkeypatch) -> None:
    class Broken:
        def __init__(self, *args) -> None:
            raise ValueError("unreachable")

    monkeypatch.setattr(workflow, "V2Client", Broken)
    assert await real_market_step("ETHRUSDPERP") is None
