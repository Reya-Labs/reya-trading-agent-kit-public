"""Paper invariants: exact accounting, engine margin, liquidation, TP/SL, failure atomicity, retries and isolation."""

import copy
import sqlite3
from decimal import ROUND_CEILING, Decimal
from unittest.mock import AsyncMock

import pytest

from agent_kit.scripts import paper
from agent_kit.scripts._margin import DEFAULT_HEADROOM, build_snapshot, check_order, order_margin, position_risk
from agent_kit.scripts._paper import (
    FUTURE_TOLERANCE_MS,
    MAX_AGE_MS,
    SCHEMA,
    PaperRefusal,
    PaperStore,
    arm_trigger,
    cancel_triggers,
    clock_offset,
    clock_report,
    due_triggers,
    initialize,
    observe,
    simulate,
    upgrade,
    valuation,
)
from agent_kit.scripts._v2 import V2Client
from agent_kit.tests.conftest import make_balance, make_market_def, make_market_summary, make_position

NOW = 100000
SYMBOL = "ETHRUSDPERP"
# Mainnet SPY parameters: a 25x market, IM = 1.3 x LM (the 0.8.0 engine vectors).
SPY_LM = "0.030773365106858238"
SPY_IM = "0.04000537463891571"


def definition(**fields) -> dict:
    return {
        "symbol": SYMBOL,
        "qtyStepSize": ".1",
        "tickSize": "1",
        "minOrderQty": ".1",
        "liquidationMarginParameter": "0.1",
        "initialMarginParameter": "0.13",
        **fields,
    }


def leveraged_definition(**fields) -> dict:
    return definition(
        **{"tickSize": ".01", "liquidationMarginParameter": SPY_LM, "initialMarginParameter": SPY_IM, **fields}
    )


def summary(mark="100", funding="0", at=NOW, **fields) -> dict:
    return {
        "symbol": SYMBOL,
        "markPrice": mark,
        "oraclePrice": mark,
        "pricesUpdatedAt": at,
        "longFundingValue": funding,
        "shortFundingValue": funding,
        **fields,
    }


def book(bids=(), asks=(), at=NOW) -> dict:
    return {
        "symbol": SYMBOL,
        "type": "SNAPSHOT",
        "updatedAt": at,
        "bids": [{"px": px, "qty": qty} for px, qty in bids],
        "asks": [{"px": px, "qty": qty} for px, qty in asks],
    }


def inputs() -> tuple[list[dict], dict, list[dict]]:
    return [definition()], book(bids=[("99", "2")], asks=[("101", "1"), ("102", "2")]), [summary()]


def request(**kwargs):
    return {
        "request_id": "one",
        "symbol": SYMBOL,
        "side": "buy",
        "kind": "ioc",
        "qty": "2",
        "price": "101",
        "slippage": ".01",
        "reduce_only": False,
        **kwargs,
    }


def state(collateral="1000", fee="10"):
    return initialize(collateral, fee, paper.APIS[0], NOW)


def leveraged_long(qty="100", collateral="1000", funding="0", **extra):
    """A long of `qty` at 100 on a 25x market: 100 x 100 on 1,000 is 10x."""
    s, result = simulate(
        state(collateral=collateral, fee="0"),
        request(qty=qty, price="100", **extra),
        [leveraged_definition()],
        book(bids=[("99.99", "1000")], asks=[("100", "1000")]),
        [summary(funding=funding)],
        NOW,
    )
    return s, result


def test_partial_ioc_cancels_remainder_and_charges_taker_fee_once():
    before = state()
    after, result = simulate(before, request(), *inputs(), NOW)
    assert result["status"] == "partially_filled_cancelled"
    assert Decimal(result["filled_qty"]) == 1
    assert Decimal(result["cancelled_qty"]) == 1
    assert Decimal(result["fees"]) == Decimal(".1")  # 10 bps of 1 x mark 100, not the 101 fill
    assert Decimal(after["cash"]) == Decimal("999.9")
    assert before["positions"] == {}  # pure transition
    value = valuation(after, inputs()[2], NOW)
    assert Decimal(value["unrealized_pnl"]) == -1
    assert Decimal(value["equity"]) == Decimal("998.9")
    assert value["funding"] == "0" and value["margin"] is None  # no market definitions passed


@pytest.mark.parametrize(
    "side, fill, mark, fee",
    [
        # devnet1 account 546 ETHRUSDPERP round trip, 11 Oct 2026, tier 0 = 3 bps; marks are the
        # on-chain MarketDataUpdatedV2 values used by settlement (txs 0xe2ac10cc..., 0x3483967f...).
        ("buy", "2530.371", "2528.001916424", "0.007584"),
        ("sell", "2525.673", "2527.981937425", "0.007583"),
    ],
)
def test_taker_fee_uses_mark_notional_with_settlement_rounding(side, fill, mark, fee):
    definitions = [definition(qtyStepSize=".001", tickSize=".001", minOrderQty=".001")]
    snapshot = book(bids=[(fill, "1")], asks=[(fill, "1")])
    if side == "buy":
        snapshot["bids"] = [{"px": "2500", "qty": "1"}]
    else:
        snapshot["asks"] = [{"px": "2600", "qty": "1"}]
    _, result = simulate(
        state(collateral="1000", fee="3"),
        request(side=side, qty=".01", price=fill),
        definitions,
        snapshot,
        [summary(mark=mark)],
        NOW,
    )
    assert result["fills"][0]["price"] == fill
    assert Decimal(result["fees"]) == Decimal(fee)
    assert result["fee_mark_price"] == mark


def test_close_and_flip_conserve_cost_basis_and_cash():
    s, _ = simulate(state(fee="0"), request(qty="1"), *inputs(), NOW)
    definitions = [definition()]
    snapshot = book(bids=[("110", "2")], asks=[("111", "2")])
    summaries = [summary(mark="110")]
    s, result = simulate(
        s, request(request_id="flip", side="sell", qty="2", price="110"), definitions, snapshot, summaries, NOW
    )
    assert Decimal(result["realized_pnl"]) == 9
    assert {k: s["positions"][SYMBOL][k] for k in ("qty", "basis")} == {"qty": "-1", "basis": "-110"}
    snapshot = book(bids=[("99", "2")], asks=[("100", "2")])
    s, result = simulate(
        s,
        request(request_id="close", qty="1", price="100", reduce_only=True),
        definitions,
        snapshot,
        summaries,
        NOW,
    )
    assert s["positions"] == {}
    assert Decimal(s["realized_pnl"]) == 19
    assert Decimal(s["cash"]) == 1019


def test_fractional_close_does_not_lose_basis_dust():
    s = state(fee="0")
    definitions, _, summaries = inputs()
    snapshot = book(bids=[("99", "2")], asks=[("101", ".1"), ("102", ".2")])
    s, _ = simulate(s, request(qty=".3", price="102"), definitions, snapshot, summaries, NOW)
    for i in range(3):
        s, _ = simulate(
            s,
            request(request_id=f"close{i}", side="sell", qty=".1", price="99", reduce_only=True),
            definitions,
            snapshot,
            summaries,
            NOW,
        )
    assert not s["positions"]
    assert Decimal(s["cash"]) == Decimal("999.2")


@pytest.mark.parametrize(
    "change",
    [
        {"qty": ".01"},
        {"qty": ".15"},
        {"qty": "-1"},
        {"qty": "NaN"},
        {"qty": "1e100"},
        {"qty": "0.0000000000000000001"},
        {"price": "101.5"},
        {"kind": "limit"},
        {"kind": "stop"},
        {"side": "other"},
        {"kind": "market", "slippage": ".1"},
        {"reduce_only": True},
        {"qty": None},
        {"max_size": True},
        {"qty": None, "usage": "1"},
        {"qty": None, "max_size": True, "reduce_only": True},
        {"sl": "1%"},
        {"kind": "market", "price": None, "sl": "1%", "reduce_only": True},
        {"kind": "market", "price": None, "sl": "50%"},
        {"kind": "market", "price": None, "tp": "1%", "trigger_slippage": ".04"},
    ],
)
def test_invalid_and_unsupported_commands_do_not_mutate(change):
    s = state()
    original = copy.deepcopy(s)
    with pytest.raises((ValueError, TypeError)):
        simulate(s, request(**change), *inputs(), NOW)
    assert s == original


@pytest.mark.parametrize(
    "bad", ["stale_book", "future_book", "stale_mark", "future_mark", "crossed", "duplicate", "offgrid"]
)
def test_bad_market_inputs_fail_closed(bad):
    definitions, snapshot, summaries = inputs()
    if bad == "stale_book":
        snapshot["updatedAt"] = NOW - MAX_AGE_MS - 1
    if bad == "future_book":
        snapshot["updatedAt"] = NOW + FUTURE_TOLERANCE_MS + 1
    if bad == "stale_mark":
        summaries[0]["pricesUpdatedAt"] = 0
    if bad == "future_mark":
        summaries[0]["pricesUpdatedAt"] = NOW + FUTURE_TOLERANCE_MS + 1
    if bad == "crossed":
        snapshot["bids"][0]["px"] = "102"
    if bad == "duplicate":
        snapshot["asks"].append(snapshot["asks"][0])
    if bad == "offgrid":
        snapshot["asks"][0]["qty"] = ".15"
    with pytest.raises(ValueError):
        simulate(state(), request(), definitions, snapshot, summaries, NOW)


# --------------------------------------------------------------------------
# Clock skew
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ahead_ms", [1000, FUTURE_TOLERANCE_MS])
@pytest.mark.parametrize("field", ["book", "mark"])
def test_data_up_to_five_seconds_ahead_of_this_clock_is_accepted(ahead_ms, field):
    # The tester's book was about 1 s ahead of local time (WSL); the runner accepts 5 s.
    definitions, snapshot, summaries = inputs()
    if field == "book":
        snapshot["updatedAt"] = NOW + ahead_ms
    else:
        summaries[0]["pricesUpdatedAt"] = NOW + ahead_ms
    _, result = simulate(state(), request(), definitions, snapshot, summaries, NOW)
    assert result["status"] == "partially_filled_cancelled"


@pytest.mark.parametrize("age_ms", [MAX_AGE_MS, 0, -FUTURE_TOLERANCE_MS])
def test_the_band_edges_are_inclusive(age_ms):
    definitions, snapshot, summaries = inputs()
    snapshot["updatedAt"] = summaries[0]["pricesUpdatedAt"] = NOW - age_ms
    simulate(state(), request(), definitions, snapshot, summaries, NOW)


def test_data_ahead_of_the_band_names_the_skew_and_the_fix():
    definitions, snapshot, summaries = inputs()
    snapshot["updatedAt"] = NOW + 7400
    clock = {"offset_ms": -7000, "uncertainty_ms": 520, "source": "http_date"}
    with pytest.raises(ValueError) as error:
        simulate(state(), request(), definitions, snapshot, summaries, NOW, clock)
    message = str(error.value)
    assert "order book is timestamped 7.4 s ahead of this computer's clock (paper accepts 5 s)" in message
    assert "Reya's server clock puts this computer about 7.0 s behind" in message
    assert "sudo hwclock -s" in message and "The paper ledger is unchanged." in message


def test_old_data_blames_the_clock_only_when_the_server_clock_says_it_is_fresh():
    definitions, snapshot, summaries = inputs()
    snapshot["updatedAt"] = NOW - 40500
    ahead = {"offset_ms": 40000, "uncertainty_ms": 510, "source": "http_date"}
    with pytest.raises(ValueError, match="about 40.0 s ahead.*hwclock"):
        simulate(state(), request(), definitions, snapshot, summaries, NOW, ahead)
    synced = {"offset_ms": 100, "uncertainty_ms": 510, "source": "http_date"}
    with pytest.raises(ValueError, match="has not changed for that long") as error:
        simulate(state(), request(), definitions, snapshot, summaries, NOW, synced)
    assert "hwclock" not in str(error.value)


def test_clock_offset_from_the_date_header():
    # Date has whole seconds: the server's time is taken as the middle of that second.
    server = 1792369149.0  # Mon, 19 Oct 2026 00:19:09 GMT
    clock = clock_offset((server + 2.4, server + 2.6, "Mon, 19 Oct 2026 00:19:09 GMT"))
    assert clock == {"offset_ms": 2000, "uncertainty_ms": 600, "source": "http_date"}
    assert clock_offset((server, server, None)) is None
    assert clock_offset((server, server, "not a date")) is None
    assert clock_offset(None) is None


def test_clock_report_warns_from_two_seconds():
    assert clock_report(None) == {"clock": None}
    quiet = {"offset_ms": -1900, "uncertainty_ms": 600, "source": "http_date"}
    assert "clock_warning" not in clock_report(quiet)
    loud = {**quiet, "offset_ms": -2500}
    assert "about 2.5 s behind Reya's server clock" in clock_report(loud)["clock_warning"]


async def test_cli_skew_refusal_leaves_the_ledger_unchanged_and_results_carry_the_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(paper.time, "time", lambda: NOW / 1000)
    parser = paper.build_parser()
    base = ["--directory", str(tmp_path / "paper")]
    await paper.run(parser.parse_args(base + ["init", "--collateral", "1000", "--fee-bps", "0"]))
    # The server's Date says this computer is about 8 s behind.
    monkeypatch.setattr(
        V2Client, "server_date", (NOW / 1000, NOW / 1000, "Thu, 01 Jan 1970 00:01:48 GMT"), raising=False
    )
    definitions, snapshot, summaries = inputs()
    snapshot["updatedAt"] = NOW + 8000
    monkeypatch.setattr(V2Client, "get", AsyncMock(side_effect=[summaries, definitions, snapshot]))
    order = base + ["order", "market", "eth", "--side", "long", "--qty", "1", "--client_order_id", "1"]
    with pytest.raises(ValueError, match="8.0 s ahead"):
        await paper.run(parser.parse_args(order))
    with PaperStore(tmp_path / "paper") as store:
        assert not store.state["requests"] and not store.state["history"]
    monkeypatch.setattr(V2Client, "get", AsyncMock(return_value=summaries))
    result = await paper.run(parser.parse_args(base + ["state"]))
    assert result["clock"]["offset_ms"] == -8500 and "behind" in result["clock_warning"]


# --------------------------------------------------------------------------
# Engine margin admission
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "qty, verdict",
    [
        # Equity 1,000, limit at the mark 100: IM allows 24.99x, the 1% reserve only 24.53x.
        ("245", None),
        ("247", "below_settlement_reserve"),
        ("250", "below_initial_margin"),
    ],
)
def test_engine_vectors_on_a_25x_market(qty, verdict):
    definitions = [leveraged_definition(qtyStepSize="1", minOrderQty="1")]
    snapshot = book(bids=[("99.99", "1000")], asks=[("100", "1000")])
    s = state(fee="0")
    if verdict is None:
        after, result = simulate(s, request(qty=qty, price="100"), definitions, snapshot, [summary()], NOW)
        assert result["status"] == "filled" and result["margin"]["engine_check"] in ("ok", "tight")
        assert after["positions"][SYMBOL]["qty"] == qty
        return
    with pytest.raises(PaperRefusal) as error:
        simulate(s, request(qty=qty, price="100"), definitions, snapshot, [summary()], NOW)
    details = error.value.details
    assert details["error_code"] == "ACCOUNT_BELOW_INITIAL_MARGIN_ERROR"
    assert details["margin"]["engine_check"] == verdict
    assert Decimal(details["margin"]["max_qty"]) < 245 and details["note"].startswith("Nothing was simulated")
    assert s["requests"] == {} and s["positions"] == {} and s["history"] == []


def test_paper_refusal_matches_the_live_preview_for_the_testers_order():
    # The tester's case: 0.8895 SPY (675 USDC) on 34 USDC with 1% slippage fails the settlement reserve.
    spy = make_market_def(
        symbol=SYMBOL,
        min_order_qty="0.015",
        qty_step_size="0.0001",
        tick_size="0.01",
        liquidation_margin_parameter=SPY_LM,
        initial_margin_parameter=SPY_IM,
    )
    live = build_snapshot(
        1,
        balances=[make_balance(account_id=1, real_balance="34")],
        positions=[],
        definitions=[spy],
        summaries={SYMBOL: make_market_summary(symbol=SYMBOL, mark_price="758.82")},
        symbols=[SYMBOL],
    )
    check = check_order(
        live,
        symbol=SYMBOL,
        is_buy=True,
        qty=Decimal("0.8895"),
        price=Decimal("766.41"),
        reduce_only=False,
        headroom_rate=DEFAULT_HEADROOM,
        fee_rate=Decimal(0),
    )
    expected = order_margin(live, check, symbol=SYMBOL, reduce_only=False)
    definitions = [
        definition(
            qtyStepSize="0.0001",
            tickSize="0.01",
            minOrderQty="0.015",
            liquidationMarginParameter=SPY_LM,
            initialMarginParameter=SPY_IM,
        )
    ]
    with pytest.raises(PaperRefusal, match="below its settlement reserve") as error:
        simulate(
            state(collateral="34", fee="0"),
            request(kind="market", qty="0.8895", price=None),
            definitions,
            book(bids=[("758.8", "10")], asks=[("758.9", "10")]),
            [summary(mark="758.82")],
            NOW,
        )
    margin = error.value.details["margin"]
    assert {key: margin[key] for key in expected} == expected
    assert error.value.details["limit_price"] == "766.41"


def test_leveraged_open_reports_margin_and_the_live_liquidation_price():
    s, result = leveraged_long()
    assert result["status"] == "filled" and result["margin"]["engine_check"] == "ok"
    after = result["account_after"]
    s_lm = Decimal(SPY_LM)
    assert after["margin"]["margin_usage"] == float((s_lm * 10000 / 1000).quantize(Decimal("0.0001"), ROUND_CEILING))
    # Long: equity meets LM where 1000 + 100 (P - 100) = s x 100 x P; rounded onto the tick toward the mark.
    price = (Decimal(9000) / (100 * (1 - s_lm))).quantize(Decimal(".01"), ROUND_CEILING)
    assert after["position"]["liquidation_price"] == str(price) == "92.86"
    # The live model on the same account (1,000 USDC, long 100 at 100) gives the same figures.
    live = build_snapshot(
        1,
        balances=[make_balance(account_id=1, real_balance="1000")],
        positions=[make_position(account_id=1, symbol=SYMBOL, qty="100", avg_entry_price="100")],
        definitions=[make_market_def(symbol=SYMBOL, tick_size="0.01", liquidation_margin_parameter=SPY_LM)],
        summaries={SYMBOL: make_market_summary(symbol=SYMBOL, mark_price="100")},
    )
    assert position_risk(live, SYMBOL)["liquidation_price_usd"] == after["position"]["liquidation_price"]
    assert valuation(s, [summary()], NOW, [leveraged_definition()])["margin"] == after["margin"]


def test_max_size_and_usage_size_like_live():
    definitions = [leveraged_definition(qtyStepSize="1", minOrderQty="1")]
    snapshot = book(bids=[("99.99", "1000")], asks=[("100", "1000")])
    s = state(fee="0")
    sized_state, sized = simulate(
        s, request(qty=None, max_size=True, price="100"), definitions, snapshot, [summary()], NOW
    )
    qty = Decimal(sized["requested_qty"])
    assert sized["sizing"]["size_limited_by"] == "engine_margin" and sized["margin"]["max_qty"] == str(qty)
    assert qty < 245  # the 0.2% headroom keeps it below the engine's limit of 245
    with pytest.raises(PaperRefusal):
        simulate(s, request(qty="246", price="100"), definitions, snapshot, [summary()], NOW)
    _, half = simulate(s, request(qty=None, usage="0.5", price="100"), definitions, snapshot, [summary()], NOW)
    assert half["sizing"]["size_limited_by"] == "usage"
    assert 0.45 < half["account_after"]["margin"]["margin_usage"] <= 0.5
    with pytest.raises(ValueError, match="this side reduces it"):
        simulate(
            sized_state,
            request(request_id="two", side="sell", qty=None, max_size=True, price="99.99"),
            definitions,
            snapshot,
            [summary()],
            NOW,
        )


def test_reduce_only_owes_only_the_liquidation_threshold_and_an_opposing_order_is_ordinary():
    s, _ = leveraged_long(qty="240")
    snapshot = book(bids=[("99", "1000")], asks=[("100", "1000")])
    summaries = [summary(mark="99.5")]  # equity 880: above the liquidation margin (735), below initial margin (955)
    ordinary = request(request_id="cut", side="sell", qty="10", price="99")
    with pytest.raises(PaperRefusal) as error:
        simulate(s, ordinary, [leveraged_definition()], snapshot, summaries, NOW)
    assert error.value.details["margin"]["engine_check"] == "below_initial_margin"
    s, result = simulate(s, {**ordinary, "reduce_only": True}, [leveraged_definition()], snapshot, summaries, NOW)
    assert result["status"] == "filled" and result["margin"]["engine_check"] == "ok"


def test_below_the_liquidation_threshold_every_order_is_refused_closes_included():
    # Cross margin: a negative real balance with equity still above LM blocks every order, as in the engine.
    s, _ = leveraged_long(qty="10")
    s["cash"] = "-100"  # at mark 130: equity -100 + 10 x 30 = 200, liquidation margin 40, real balance -100
    summaries = [summary(mark="130")]
    snapshot = book(bids=[("129", "100")], asks=[("131", "100")])
    with pytest.raises(PaperRefusal, match="below its liquidation safety threshold") as error:
        simulate(
            s,
            request(request_id="exit", side="sell", qty="10", price="129", reduce_only=True),
            [leveraged_definition()],
            snapshot,
            summaries,
            NOW,
        )
    assert error.value.details["error_code"] == "ACCOUNT_BELOW_LIQUIDATION_MARGIN_ERROR"
    observed, events = observe(s, summaries, [leveraged_definition()], {}, NOW)
    assert events == [] and observed["positions"]  # not liquidatable: equity is above LM


def test_insufficient_margin_rolls_back_and_zero_fill_is_recorded():
    s = state(collateral="10")
    with pytest.raises(PaperRefusal, match="initial margin requirement"):
        simulate(s, request(), *inputs(), NOW)
    assert s["requests"] == {}
    s = state(collateral="100")
    s, result = simulate(s, request(price="98"), *inputs(), NOW)
    assert result["status"] == "cancelled"
    assert s["cash"] == "100.000000000000000000"


def test_market_uses_mark_slippage_bounded_ioc():
    definitions, snapshot, summaries = inputs()
    summaries[0]["oraclePrice"] = "90"  # the oracle index is not the execution reference
    s, result = simulate(state(), request(kind="market", qty="2", price=None), definitions, snapshot, summaries, NOW)
    assert Decimal(result["limit_price"]) == 101  # mark 100 × 1.01
    assert Decimal(result["filled_qty"]) == 1
    assert len(s["history"]) == 1


def test_reduce_only_cannot_flip():
    s, _ = simulate(state(), request(qty="1"), *inputs(), NOW)
    with pytest.raises(ValueError, match="Reduce-only"):
        simulate(s, request(request_id="bad", side="sell", reduce_only=True), *inputs(), NOW)


# --------------------------------------------------------------------------
# Funding
# --------------------------------------------------------------------------


@pytest.mark.parametrize("side, sign", [("buy", -1), ("sell", 1)])
def test_funding_accrues_from_the_fill_and_settles_at_the_next_fill(side, sign):
    definitions = [leveraged_definition()]
    at_ask = book(bids=[("99.99", "100")], asks=[("100", "100")])
    at_bid = book(bids=[("100", "100")], asks=[("100.01", "100")])
    s, _ = simulate(
        state(fee="0"),
        request(side=side, qty="10", price="100"),
        definitions,
        at_ask if side == "buy" else at_bid,
        [summary(funding="5")],
        NOW,
    )
    assert s["positions"][SYMBOL]["funding_value"] == "5" and s["positions"][SYMBOL]["funding_since_ms"] == NOW
    later = [summary(funding="5.5")]  # funding value up 0.5 per unit: longs pay, shorts receive
    value = valuation(s, later, NOW, definitions)
    assert Decimal(value["unsettled_funding"]) == sign * 5
    assert Decimal(value["equity"]) == 1000 + sign * 5
    assert value["margin"]["equity_usd"] == 1000 + sign * 5
    close = request(request_id="close", side="sell" if side == "buy" else "buy", qty="10", price="100")
    s, result = simulate(
        s, {**close, "reduce_only": True}, definitions, at_bid if side == "buy" else at_ask, later, NOW
    )
    assert Decimal(result["funding_settled"]) == sign * 5
    assert Decimal(s["cash"]) == 1000 + sign * 5 and Decimal(s["funding"]) == sign * 5


def test_an_upgraded_ledger_keeps_its_positions_and_accrues_funding_from_the_first_observation():
    legacy = state(fee="0")
    for key in ("triggers", "funding", "liquidation_penalties", "uncovered_losses", "last_observation_ms"):
        legacy.pop(key)
    legacy.update(schema=1, positions={SYMBOL: {"qty": "1", "basis": "100"}})
    legacy["requests"] = {"old": {"request": request(request_id="old"), "result": {"status": "filled"}}}
    s = upgrade(legacy, NOW)
    assert s["schema"] == SCHEMA and s["upgraded_at_ms"] == NOW and s["positions"] == legacy["positions"]
    s, events = observe(s, [summary(funding="7")], [definition()], {}, NOW)
    assert events == [] and s["positions"][SYMBOL]["funding_value"] == "7"
    assert valuation(s, [summary(funding="9")], NOW)["unsettled_funding"] == "-2.000000"
    # A request recorded before the upgrade still replays.
    assert simulate(s, request(request_id="old"), [], {}, [], NOW)[1]["replayed"]


def test_paper_order_arguments_add_new_keys_only_when_used():
    parser = paper.build_parser()
    args = parser.parse_args(["order", "market", "eth", "--side", "long", "--qty", "1", "--client_order_id", "7"])
    assert set(paper._paper_request(args)) == set(request())
    args = parser.parse_args(
        ["order", "market", "eth", "--side", "long", "--usage", "0.5", "--sl", "2%", "--client_order_id", "7"]
    )
    assert paper._paper_request(args) == {
        **request(request_id="7", kind="market", qty=None, price=None, slippage="0.01"),
        "usage": "0.5",
        "sl": "2%",
        "tp": None,
        "trigger_slippage": "0.01",
    }


# --------------------------------------------------------------------------
# Liquidation
# --------------------------------------------------------------------------


def test_equity_below_the_liquidation_margin_liquidates_the_whole_account():
    s, _ = leveraged_long()
    s, events = observe(s, [summary(mark="92.9")], [leveraged_definition()], {}, NOW + 1)
    assert events == [] and s["last_observation_ms"] == NOW + 1  # equity 290 vs LM 285.9: safe
    s, events = observe(s, [summary(mark="92.8")], [leveraged_definition()], {}, NOW + 2)
    [event] = events
    assert event["event"] == "liquidation" and event["last_safe_observation_ms"] == NOW + 1
    assert Decimal(event["equity"]) == 280 and Decimal(event["liquidation_margin"]) > 280
    assert Decimal(event["realized_pnl"]) == -720 and Decimal(event["penalty_to_pool"]) == 280
    assert Decimal(event["uncovered_loss"]) == 0 and event["cash_after"] == "0"
    assert s["positions"] == {} and s["cash"] == "0" and s["history"][-1] == event
    value = valuation(s, [summary(mark="92.8")], NOW + 2)
    assert Decimal(value["liquidation_penalties"]) == 280 and Decimal(value["equity"]) == 0
    # cash = initial + realized - fees + funding - penalties + uncovered
    assert Decimal(s["initial_collateral"]) + Decimal(s["realized_pnl"]) - Decimal(s["liquidation_penalties"]) == 0


def test_an_insolvent_liquidation_never_leaves_a_negative_balance():
    s, _ = leveraged_long()
    s, [event] = observe(s, [summary(mark="85")], [leveraged_definition()], {}, NOW)
    assert Decimal(event["equity"]) == -500 and Decimal(event["uncovered_loss"]) == 500
    assert Decimal(event["penalty_to_pool"]) == 0 and s["cash"] == "0" and Decimal(s["uncovered_losses"]) == 500


async def test_an_order_that_finds_the_account_liquidatable_returns_the_liquidation(tmp_path, monkeypatch):
    monkeypatch.setattr(paper.time, "time", lambda: NOW / 1000)
    directory = tmp_path / "paper"
    with PaperStore(directory) as store:
        store.save(leveraged_long()[0])
    parser = paper.build_parser()
    gets = [[summary(mark="90")], [leveraged_definition()], book(bids=[("89", "10")], asks=[("91", "10")])]
    monkeypatch.setattr(V2Client, "get", AsyncMock(side_effect=gets))
    args = parser.parse_args(
        [
            "--directory",
            str(directory),
            "order",
            "market",
            "eth",
            "--side",
            "long",
            "--qty",
            "1",
            "--client_order_id",
            "2",
        ]
    )
    result = await paper.run(args)
    assert result["status"] == "not_simulated" and "liquidated" in result["error"]
    assert [e["event"] for e in result["events"]] == ["liquidation"]
    with PaperStore(directory) as store:
        assert store.state["positions"] == {} and "2" not in store.state["requests"]


# --------------------------------------------------------------------------
# TP/SL
# --------------------------------------------------------------------------


def bracketed(**levels):
    """Long 10 at 100 with `order market --sl/--tp` (market order: limit 101, fills the 100 ask)."""
    return simulate(
        state(fee="0"),
        request(kind="market", qty="10", price=None, trigger_slippage="0.01", **levels),
        [leveraged_definition()],
        book(bids=[("99.99", "100")], asks=[("100", "100")]),
        [summary()],
        NOW,
    )


def test_order_market_arms_the_bracket_from_the_average_fill_price():
    s, result = bracketed(sl="2%", tp="3%")
    protection = result["protection"]
    assert protection["state"] == "protected" and protection["reference_px_usd"] == "100"
    assert protection["stop_loss"]["trigger_px_usd"] == "98" and protection["stop_loss"]["limit_px_usd"] == "97.02"
    assert protection["take_profit"]["trigger_px_usd"] == "103"
    assert protection["take_profit"]["limit_px_usd"] == "101.97"
    assert set(s["triggers"][SYMBOL]) == {"stop_loss", "take_profit"}
    assert "error" not in result


def test_a_stop_loss_fires_on_the_mark_and_closes_the_whole_position():
    s, _ = bracketed(sl="2%", tp="3%")
    marks = [summary(mark="97.5")]
    assert due_triggers(s, marks, NOW) == [SYMBOL]
    books = {SYMBOL: book(bids=[("97.6", "20")], asks=[("97.7", "20")])}
    s, [event] = observe(s, marks, [leveraged_definition()], books, NOW)
    assert event["event"] == "stop_loss" and event["status"] == "filled"
    assert event["fills"][0]["price"] == "97.6" and Decimal(event["realized_pnl"]) == -24
    assert s["positions"] == {} and s["triggers"] == {}  # flat: the take-profit is dropped too
    assert event["dropped_triggers"][0]["kind"] == "take_profit"
    assert Decimal(s["cash"]) == 976


def test_a_take_profit_fires_when_the_mark_reaches_it():
    s, _ = bracketed(sl="2%", tp="3%")
    books = {SYMBOL: book(bids=[("103.2", "20")], asks=[("103.3", "20")])}
    s, [event] = observe(s, [summary(mark="103.5")], [leveraged_definition()], books, NOW)
    assert event["event"] == "take_profit" and Decimal(event["realized_pnl"]) == 32


def test_a_fired_child_beyond_the_book_does_not_fill_and_is_consumed():
    s, _ = bracketed(sl="2%", tp="3%")
    books = {SYMBOL: book(bids=[("96", "20")], asks=[("96.1", "20")])}
    s, [event] = observe(s, [summary(mark="96.05")], [leveraged_definition()], books, NOW)
    assert event["status"] == "cancelled" and event["remaining_position"] == "10"
    assert set(s["triggers"][SYMBOL]) == {"take_profit"}


def test_a_stop_beyond_the_liquidation_price_loses_to_the_liquidation():
    s, _ = leveraged_long()  # liquidation price 92.86
    definitions = [leveraged_definition()]
    s, _ = arm_trigger(s, SYMBOL, "stop_loss", "long", "90", "89", definitions, [summary()], NOW, time_in_force="IOC")
    books = {SYMBOL: book(bids=[("89.5", "1000")], asks=[("89.6", "1000")])}
    _, events = observe(s, [summary(mark="89.5")], definitions, books, NOW)
    assert [e["event"] for e in events] == ["liquidation"]
    # A stop above the liquidation price fired first on the way down, and its fill can save the account.
    s, _ = leveraged_long()
    s, _ = arm_trigger(s, SYMBOL, "stop_loss", "long", "95", "94", definitions, [summary()], NOW, time_in_force="IOC")
    books = {SYMBOL: book(bids=[("94.5", "1000")], asks=[("94.6", "1000")])}
    s, events = observe(s, [summary(mark="92")], definitions, books, NOW)
    assert [e["event"] for e in events] == ["stop_loss"] and Decimal(s["cash"]) == 450


def test_bracket_refusals_match_live():
    s, _ = bracketed(sl="2%")
    snapshot = book(bids=[("99.99", "100")], asks=[("100", "100")])
    again = request(request_id="two", kind="market", qty="1", price=None, sl="3%")
    with pytest.raises(PaperRefusal, match="already armed") as error:
        simulate(s, again, [leveraged_definition()], snapshot, [summary()], NOW)
    assert error.value.details["refused_by"][0]["check"] == "protection_exists"
    with pytest.raises(PaperRefusal) as error:
        simulate(
            state(fee="0"),
            request(kind="market", qty="1", price=None, tp="99"),
            [leveraged_definition()],
            snapshot,
            [summary()],
            NOW,
        )
    assert error.value.details["refused_by"][0]["check"] == "protection_fires_at_once"
    with pytest.raises(PaperRefusal) as error:
        simulate(
            s,
            request(request_id="three", side="sell", kind="market", qty="1", price=None, tp="5%"),
            [leveraged_definition()],
            snapshot,
            [summary()],
            NOW,
        )
    assert error.value.details["refused_by"][0]["check"] == "protection_opposite_position"


def test_a_leg_already_through_the_mark_after_the_fill_leaves_the_position_unprotected():
    # Filled at 101 against a mark of 100: a 0.5% stop (100.495) is already through the mark.
    s, result = simulate(
        state(fee="0"),
        request(kind="market", qty="1", price=None, sl="0.5%", tp="3%"),
        [definition(tickSize=".001", qtyStepSize=".1")],
        book(bids=[("99", "10")], asks=[("101", "10")]),
        [summary()],
        NOW,
    )
    assert result["protection"]["state"] == "unprotected" and "UNPROTECTED" in result["error"]
    assert result["protection"]["take_profit"]["armed"] and "paper order sl" in result["error"]
    assert set(s["triggers"][SYMBOL]) == {"take_profit"}


def test_order_sl_tp_arms_on_a_matching_position_and_cancel_all_removes_them():
    s, _ = leveraged_long(qty="10")
    definitions = [leveraged_definition()]
    with pytest.raises(ValueError, match="no short paper position"):
        arm_trigger(s, SYMBOL, "stop_loss", "short", "105", "106", definitions, [summary()], NOW, time_in_force="IOC")
    with pytest.raises(ValueError, match="IOC only"):
        arm_trigger(s, SYMBOL, "stop_loss", "long", "95", "94", definitions, [summary()], NOW, time_in_force="GTC")
    with pytest.raises(ValueError, match="must be below the current mark"):
        arm_trigger(s, SYMBOL, "stop_loss", "long", "101", "100", definitions, [summary()], NOW, time_in_force="IOC")
    s, result = arm_trigger(
        s, SYMBOL, "take_profit", "long", "99", "98", definitions, [summary()], NOW, time_in_force="IOC", force=True
    )
    assert result["status"] == "armed" and "next observation" in result["warning"]
    assert "below the long's entry" in result["warning"]
    s, result = arm_trigger(
        s, SYMBOL, "stop_loss", "long", "95", "94", definitions, [summary()], NOW, time_in_force="IOC"
    )
    again, repeat = arm_trigger(
        s, SYMBOL, "stop_loss", "long", "95", "94", definitions, [summary()], NOW + 5, time_in_force="IOC"
    )
    assert repeat["already_armed"] and again == s
    with pytest.raises(ValueError, match="already armed"):
        arm_trigger(s, SYMBOL, "stop_loss", "long", "96", "95", definitions, [summary()], NOW, time_in_force="IOC")
    with pytest.raises(ValueError, match="exactly one"):
        cancel_triggers(s)
    s, cancelled = cancel_triggers(s, SYMBOL)
    assert cancelled["cancelled_count"] == 2 and s["triggers"] == {}


def test_a_flip_drops_the_triggers():
    s, _ = bracketed(sl="2%", tp="3%")
    s, result = simulate(
        s,
        request(request_id="flip", side="sell", qty="20", price="99.99"),
        [leveraged_definition()],
        book(bids=[("99.99", "100")], asks=[("100", "100")]),
        [summary()],
        NOW,
    )
    assert s["triggers"] == {} and {t["kind"] for t in result["dropped_triggers"]} == {"stop_loss", "take_profit"}


async def test_cli_order_sl_and_cancel_all(tmp_path, monkeypatch):
    monkeypatch.setattr(paper.time, "time", lambda: NOW / 1000)
    directory = tmp_path / "paper"
    with PaperStore(directory) as store:
        store.save(leveraged_long(qty="10")[0])
    parser = paper.build_parser()
    base = ["--directory", str(directory), "order"]
    monkeypatch.setattr(V2Client, "get", AsyncMock(side_effect=[[summary()], [leveraged_definition()]]))
    result = await paper.run(
        parser.parse_args(
            base + ["sl", "eth", "--side", "long", "--trigger_px", "95", "--limit_px", "94", "--time_in_force", "IOC"]
        )
    )
    assert result["action"] == "order_sl" and result["status"] == "armed" and result["events"] == []
    monkeypatch.setattr(V2Client, "get", AsyncMock(side_effect=AssertionError("no network for cancel_all")))
    result = await paper.run(parser.parse_args(base + ["cancel_all", "--symbol", "eth"]))
    assert result["cancelled_count"] == 1


# --------------------------------------------------------------------------
# Ledger durability and isolation
# --------------------------------------------------------------------------


def test_store_restart_duplicate_and_failure_atomicity(tmp_path):
    directory = tmp_path / "paper"
    with PaperStore(directory) as store:
        store.save(state())
    with PaperStore(directory) as store:
        s, first = simulate(store.state, request(), *inputs(), NOW)
        store.save(s)
    with PaperStore(directory) as store:
        s, retry = simulate(store.state, request(), [], {}, [], NOW + 999999)
        assert retry == {**first, "replayed": True}
        assert len(s["history"]) == 1
        with pytest.raises(ValueError, match="different parameters"):
            simulate(s, request(qty="1"), *inputs(), NOW)
    with pytest.raises(RuntimeError):
        with PaperStore(directory) as store:
            store.state["cash"] = "1"
            store.save(store.state)
            raise RuntimeError("crash before commit")
    with PaperStore(directory) as store:
        assert Decimal(store.state["cash"]) == Decimal("999.9")


def test_concurrent_writers_fail_without_partial_state(tmp_path):
    directory = tmp_path / "paper"
    with PaperStore(directory) as first:
        first.save(state())
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with PaperStore(directory):
                pytest.fail("second writer acquired the transaction")
    with PaperStore(directory) as store:
        assert store.state["cash"] == "1000"


def test_symlink_state_rejected(tmp_path):
    directory = tmp_path / "paper"
    directory.mkdir(mode=0o700)
    (directory / "paper.sqlite3").symlink_to(tmp_path / "foreign")
    with pytest.raises(OSError):
        with PaperStore(directory):
            pass


async def test_cli_credentials_ignored_and_only_public_gets_used(tmp_path, monkeypatch):
    monkeypatch.setenv("REYA_MODE", "live")
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", "must-never-be-loaded")
    monkeypatch.setenv("PERP_PRIVATE_KEY_FILE", "/must/not/read")
    monkeypatch.setenv("REYA_API_URL", "https://malicious.invalid/v2")
    monkeypatch.setattr(paper.time, "time", lambda: NOW / 1000)
    parser = paper.build_parser()
    base = ["--directory", str(tmp_path / "paper")]
    await paper.run(parser.parse_args(base + ["init", "--collateral", "1000", "--fee-bps", "10"]))
    definitions, snapshot, summaries = inputs()
    mock = AsyncMock(side_effect=[summaries, definitions, snapshot])
    monkeypatch.setattr(V2Client, "get", mock)
    args = parser.parse_args(base + ["order", "--symbol", SYMBOL, "--side", "buy", "--qty", "1", "--request-id", "one"])
    result = await paper.run(args)
    assert result["mode"] == "paper" and result["events"] == [] and result["clock"] is None
    assert [call.args[0] for call in mock.call_args_list] == [
        "/perpMarkets/summary",
        "/perpMarketDefinitions",
        f"/market/{SYMBOL}/depth",
    ]
    assert (await paper.run(args))["replayed"]
    assert mock.await_count == 3
    history = await paper.run(parser.parse_args(base + ["history"]))
    assert history["coverage"]["total_records"] == 1
    with pytest.raises(ValueError, match="ledger ID"):
        await paper.run(
            parser.parse_args(base + ["reset", "--collateral", "1", "--fee-bps", "0", "--confirm-ledger-id", "wrong"])
        )


def test_paper_module_has_no_live_submission_imports():
    import ast
    from pathlib import Path

    for name in ("paper.py", "_paper.py"):
        source = Path(paper.__file__).with_name(name).read_text()
        tree = ast.parse(source)
        modules = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert all(
            not (m or "").startswith(
                (
                    "sdk",
                    "web3",
                    "eth_",
                    "agent_kit.scripts.trade",
                    "agent_kit.scripts.account",
                    "agent_kit.scripts._writer",
                )
            )
            for m in modules
        )


async def test_refresh_series_restart_reset_and_history_pagination(tmp_path, monkeypatch):
    monkeypatch.setattr(paper.time, "time", lambda: NOW / 1000)
    parser = paper.build_parser()
    base = ["--directory", str(tmp_path / "ledger")]

    async def invoke(*args):
        return await paper.run(parser.parse_args(base + list(args)))

    init = await invoke("init", "--collateral", "100", "--fee-bps", "0")
    monkeypatch.setattr(V2Client, "get", AsyncMock(return_value=inputs()[2]))
    await invoke("state")
    assert (await invoke("history", "--snapshots"))["coverage"]["total_records"] == 0
    await invoke("refresh")
    await invoke("refresh")
    assert (await invoke("history", "--snapshots", "--limit", "1"))["coverage"]["next_offset"] == 1
    assert len((await invoke("history", "--snapshots", "--offset", "1"))["data"]) == 1
    reset = await invoke("reset", "--confirm-ledger-id", init["ledger_id"], "--collateral", "50", "--fee-bps", "1")
    assert reset["ledger_id"] != init["ledger_id"]
    assert (await invoke("history", "--snapshots"))["coverage"]["total_records"] == 0
    assert reset["cash"] == "50"


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("quantity", [".1", "1", "1.9"])
def test_roundtrip_spread_loss_and_fee_conservation(side, quantity):
    definitions, snapshot, summaries = inputs()
    buy = side == "buy"
    s, first = simulate(
        state(), request(side=side, qty=quantity, price="102" if buy else "99"), definitions, snapshot, summaries, NOW
    )
    s, second = simulate(
        s,
        request(
            request_id="close",
            side="sell" if buy else "buy",
            qty=quantity,
            price="99" if buy else "102",
            reduce_only=True,
        ),
        definitions,
        snapshot,
        summaries,
        NOW,
    )
    paid = sum(Decimal(f["qty"]) * Decimal(f["price"]) for f in (first if buy else second)["fills"])
    received = sum(Decimal(f["qty"]) * Decimal(f["price"]) for f in (second if buy else first)["fills"])
    fees = Decimal(first["fees"]) + Decimal(second["fees"])
    assert not s["positions"]
    assert Decimal(s["cash"]) == Decimal("1000") + received - paid - fees
    assert Decimal(s["realized_pnl"]) == received - paid


@pytest.mark.parametrize("kind, change", [("market", {"price": "100"}), ("ioc", {"price": None})])
def test_incompatible_or_missing_order_price_rejected(kind, change):
    with pytest.raises(ValueError):
        simulate(state(), request(kind=kind, **change), *inputs(), NOW)


async def test_new_symbol_missing_summary_reports_symbol_and_preserves_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(paper.time, "time", lambda: NOW / 1000)
    parser = paper.build_parser()
    base = ["--directory", str(tmp_path / "ledger")]
    await paper.run(parser.parse_args(base + ["init", "--collateral", "1000", "--fee-bps", "10"]))
    definitions, snapshot, _ = inputs()
    monkeypatch.setattr(V2Client, "get", AsyncMock(side_effect=[[], definitions, snapshot]))
    args = parser.parse_args(
        base + ["order", "--symbol", SYMBOL, "--side", "buy", "--qty", "1", "--request-id", "missing"]
    )
    with pytest.raises(ValueError, match=f"Missing market summary for {SYMBOL}"):
        await paper.run(args)
    with PaperStore(tmp_path / "ledger") as store:
        assert store.state["cash"] == "1000"
        assert not store.state["positions"]
        assert not store.state["requests"]
        assert not store.state["history"]
