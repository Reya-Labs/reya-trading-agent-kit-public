"""Engine-mirrored margin: vectors from the matching engine's risk-engine tests, then the kit's use of them.

Engine vectors come from the matching engine's risk-engine tests (`risk_engine/tests.rs`,
source revision 4d8ed012); each test names the engine test it reproduces. `permissive` is the engine's
`permissive_risk_state` governance: 18-decimal settlement, margin rate 1, IM multiplier 1, reserve 1%.
"""

from types import SimpleNamespace

import json
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest
from sdk.open_api.exceptions import ApiException
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.side import Side

from agent_kit.scripts import query, runner, trade
from agent_kit.scripts._margin import (
    AFTER_FILL_BASIS,
    INITIAL_MARGIN,
    LIQUIDATION_THRESHOLD,
    SETTLEMENT_RESERVE,
    AccountMargin,
    Lot,
    MarketRisk,
    Projection,
    admit,
    apply_fill,
    build_snapshot,
    im_multiplier_from,
    liquidation_price,
    project,
    rounding_allowance,
)
from agent_kit.scripts._paper import valuation
from agent_kit.tests.conftest import (
    FakeClient,
    make_asset_def,
    make_asset_price,
    make_balance,
    make_market_def,
    make_market_summary,
    make_order,
    make_position,
    make_price,
    parse_stdout,
)

WEI = D("1e-18")
NATIVE = D("0.000001")


def account(mb, rb, base=0, mark=100, rate=1, entry=100, market="M"):
    return AccountMargin(D(rb), D(mb), {market: MarketRisk(D(base), D(mark), D(rate), D(entry))})


def permissive(margin, candidates=(), in_flight=()):
    return project(margin, candidates, in_flight=in_flight, im_multiplier=D(1), decimals=18)


def code(verdict):
    assert verdict is not None
    return verdict.code


def projection(mb, rb, lm, im, reserve):
    h = min(D(mb) - D(lm), D(rb))
    return Projection(D(mb), D(rb), D(lm), D(im), D(reserve), h, h - D(reserve))


# --------------------------------------------------------------------------
# Engine vectors
# --------------------------------------------------------------------------


@pytest.mark.parametrize("qty", ["-1", "1"])
def test_settlement_reserve_equality_accepts_and_one_native_unit_below_rejects_on_both_health_branches(qty):
    # Margin rate 0.1, reserve 1%, 6-decimal rUSD, mark 100: LM 10, reserve 1, one native unit of allowance.
    candidate = [Lot("M", D(qty), D(100))]

    def check(mb, rb):
        margin = account(mb, rb, rate="0.1")
        p0 = project(margin, im_multiplier=D(1))
        pq = project(margin, candidate, im_multiplier=D(1))
        return pq, admit(p0, pq, False)

    rb_equal, verdict = check("100", D(1) + NATIVE)
    assert rb_equal.h == rb_equal.reserve == rb_equal.rb_floor and rb_equal.c == 0 and verdict is None
    rb_below, verdict = check("100", "1")
    assert rb_below.c == -NATIVE and code(verdict) == SETTLEMENT_RESERVE and verdict.shortfall == NATIVE

    mb_equal, verdict = check(D(11) + NATIVE, "100")
    assert mb_equal.h == mb_equal.reserve == mb_equal.mb_floor - mb_equal.lm and mb_equal.c == 0 and verdict is None
    mb_below, verdict = check("11", "100")
    assert mb_below.c == -NATIVE and code(verdict) == SETTLEMENT_RESERVE


def test_exact_taker_error_precedence_is_71_72_85_and_strict_reduce_only_stops_at_71():
    assert code(admit(projection(9, 100, 10, 20, 0), projection(100, 100, 10, 20, 0), True)) == LIQUIDATION_THRESHOLD
    assert code(admit(projection(100, 100, 10, 20, 0), projection(19, 100, 10, 20, 100), False)) == INITIAL_MARGIN
    assert code(admit(projection(100, 100, 10, 20, 0), projection(20, 5, 10, 20, 6), False)) == SETTLEMENT_RESERVE
    assert admit(projection(15, 20, 10, 20, 0), projection(13, 20, 9, 20, 0), True) is None
    assert admit(projection(100, 100, 10, 20, 0), projection(20, 5, 10, 20, 6), True) is None
    assert admit(projection(100, 100, 10, 20, 0), projection(-50, -50, 0, 0, 6), True) is None
    assert code(admit(projection(15, 20, 10, 20, 0), projection(30, 30, 9, 20, 0), False)) == INITIAL_MARGIN


def test_engine_messages_match_the_wire_text():
    verdict = admit(projection(100, 100, 10, 20, 0), projection(20, D("5.42"), 10, 20, 6), False)
    assert verdict is not None
    assert verdict.error_code == "ACCOUNT_BELOW_INITIAL_MARGIN_ERROR"
    assert verdict.engine_message == (
        "Insufficient margin: this order would leave the account 0.58 rUSD below its settlement reserve. "
        "Reduce the order size or add collateral."
    )
    precondition = admit(projection(9, 100, 10, 20, 0), projection(9, 100, 10, 20, 0), True)
    assert precondition is not None and precondition.error_code == "ACCOUNT_BELOW_LIQUIDATION_MARGIN_ERROR"


def test_fractional_im_rounding_is_exact_and_equality_is_admitted():
    # 18 decimals, margin rate 0.1, IM multiplier 1.3, base 1 + 1 wei at mark 10: LM = 1 + 1 wei, IM = 1.3 + 2 wei.
    expected_im = D("1.3") + 2 * WEI

    def margin(mb):
        return AccountMargin(D(100), mb, {"M": MarketRisk(D(1) + WEI, D(10), D("0.1"), D(10))})

    allowance = 4 * WEI
    equal = project(margin(expected_im + allowance), im_multiplier=D("1.3"), decimals=18)
    assert equal.lm == D(1) + WEI and equal.im == expected_im
    assert admit(equal, equal, False) is None
    below = project(margin(expected_im + allowance - WEI), im_multiplier=D("1.3"), decimals=18)
    assert below.im == expected_im and code(admit(below, below, False)) == INITIAL_MARGIN


def test_reserve_is_gross_notional_and_applied_exactly_once():
    p = permissive(account(1000, 1000), [Lot("M", D(1), D(200))], [Lot("M", D(2), D(100))])
    assert p.reserve == 4  # 1% of 2 x 100 in flight plus 1 x 200 candidate
    assert p.c == p.h - p.reserve
    assert p.mb_floor == D(900) - 10 * WEI


def test_profitable_optional_lot_never_credits_losing_lot():
    p = permissive(account(1000, 1000), in_flight=[Lot("M", D(1), D(90)), Lot("M", D(1), D(110))])
    assert p.mb_floor == p.rb_floor == D(990) - 8 * WEI


def test_candidate_shifts_every_existing_exposure_corner():
    p = permissive(account(1000, 1000), [Lot("M", D(1), D(100))], [Lot("M", D(2), D(100)), Lot("M", D(-3), D(100))])
    assert p.lm == 300


def test_common_pivot_caps_closable_quantity_when_gross_opposite_is_smaller():
    p = permissive(account(1000, 500, base=5, mark=80, entry=100), [Lot("M", D(4), D(100))], [Lot("M", D(-2), D(100))])
    assert p.rb_floor == D(380) - 33 * WEI


def test_prb_average_entry_rounding_allowance_debits_mb_and_rb_per_considered_transition():
    margin = AccountMargin(D(0), D("-0.2"), {"M": MarketRisk(D(-2), D("0.2"), D(0), D("0.1"))})
    p = project(margin, [Lot("M", D(-1), D("0.2"))], im_multiplier=D(1), reserve_rate=D(0))
    assert p.mb_floor == D("-0.200002")  # one settled position plus one future fill each debit a native unit
    assert p.rb_floor == D("-0.000002")


def test_rounding_allowance_native_boundary_is_exact():
    assert rounding_allowance(D(10**12 - 2), 1) == NATIVE
    assert rounding_allowance(D(10**12 - 1), 1) == 2 * NATIVE
    assert rounding_allowance(D(10**12 - 1), 3) == 6 * NATIVE  # every possible position/lot transition is charged


# --------------------------------------------------------------------------
# What the vectors mean on mainnet parameters
# --------------------------------------------------------------------------

SPY_LM = "0.030773365106858238"
SPY_IM = "0.04000537463891571"


def spy_definition(**overrides):
    fields = dict(
        symbol="SPYRUSDPERP",
        market_id=9,
        min_order_qty="0.015",
        qty_step_size="0.0001",
        tick_size="0.01",
        liquidation_margin_parameter=SPY_LM,
        initial_margin_parameter=SPY_IM,
        max_leverage=25,
    )
    return make_market_def(**{**fields, **overrides})


def test_im_multiplier_reads_one_point_three_from_float_parameters():
    btc = make_market_def(
        symbol="BTCRUSDPERP",
        liquidation_margin_parameter="0.019235384061671346",
        initial_margin_parameter="0.02500599928017275",
    )
    assert im_multiplier_from([spy_definition(), btc]) == D("1.3")


def test_published_max_leverage_is_rounded_up_and_fails_initial_margin():
    # V2 publishes maxLeverage = round(1 / IM) = 25, but 1 / IM is 24.997: 25x at the mark fails IM.
    margin = AccountMargin(D(1000), D(1000), {"S": MarketRisk(D(0), D(100), D(SPY_LM), D(100))})
    p0 = project(margin, im_multiplier=D("1.3"))
    pq = project(margin, [Lot("S", D(250), D(100))], im_multiplier=D("1.3"))
    assert code(admit(p0, pq, False)) == INITIAL_MARGIN


def test_reserve_binds_before_initial_margin_on_a_25x_market():
    # Equity 1000, limit at the mark: IM allows 24.99x, the reserve only 24.53x (1 / (s + 1%)).
    margin = AccountMargin(D(1000), D(1000), {"S": MarketRisk(D(0), D(100), D(SPY_LM), D(100))})
    p0 = project(margin, im_multiplier=D("1.3"))
    pq = project(margin, [Lot("S", D(247), D(100))], im_multiplier=D("1.3"))  # 24.7x
    assert pq.mb_floor >= pq.im
    assert code(admit(p0, pq, False)) == SETTLEMENT_RESERVE
    fits = project(margin, [Lot("S", D(245), D(100))], im_multiplier=D("1.3"))  # 24.5x
    assert admit(p0, fits, False) is None


# --------------------------------------------------------------------------
# Liquidation price (cross margin)
# --------------------------------------------------------------------------


def equity_minus_lm(margin, market, price):
    """MB - LM with `market` repriced and everything else unchanged."""
    total = margin.margin_balance
    lm = D(0)
    for name, risk in margin.markets.items():
        mark = price if name == market else risk.mark
        total += risk.base * (mark - risk.mark)
        lm += risk.margin_rate * abs(risk.base) * mark
    return total - lm


@pytest.mark.parametrize("base", ["2", "-2"])
def test_liquidation_price_is_where_equity_meets_liquidation_margin(base):
    margin = AccountMargin(D(100), D(100), {"M": MarketRisk(D(base), D(500), D("0.05"), D(500))})
    lm = project(margin, im_multiplier=D(1)).lm
    price = liquidation_price(margin, "M", lm)
    assert price is not None
    assert (price < 500) if D(base) > 0 else (price > 500)
    assert abs(equity_minus_lm(margin, "M", price)) < D("1e-20")


def test_cross_margin_liquidation_price_holds_other_markets_fixed():
    margin = AccountMargin(
        D(300),
        D(310),
        {
            "A": MarketRisk(D(1), D(2000), D("0.03"), D(1990)),
            "B": MarketRisk(D(-0.5), D(4000), D("0.02"), D(4000)),
        },
    )
    lm = project(margin, im_multiplier=D(1)).lm
    for market in ("A", "B"):
        price = liquidation_price(margin, market, lm)
        assert price is not None and abs(equity_minus_lm(margin, market, price)) < D("1e-20")


def test_a_long_that_no_positive_price_liquidates_has_no_liquidation_price():
    margin = AccountMargin(D(1000), D(1000), {"M": MarketRisk(D(1), D(100), D("0.03"), D(100))})
    assert liquidation_price(margin, "M", project(margin, im_multiplier=D(1)).lm) is None


def test_apply_fill_follows_core_position_updates():
    margin = AccountMargin(D(100), D(110), {"M": MarketRisk(D(1), D(110), D("0.1"), D(100))})
    extended = apply_fill(margin, Lot("M", D(1), D(120)))
    assert extended.markets["M"].entry == 110 and extended.margin_balance == 100  # bought 10 above the mark
    reduced = apply_fill(margin, Lot("M", D("-0.5"), D(110)))
    assert reduced.markets["M"].entry == 100 and reduced.real_balance == 105 and reduced.margin_balance == 110
    flipped = apply_fill(margin, Lot("M", D(-3), D(110)), fee=D(1))
    assert flipped.markets["M"].base == -2 and flipped.markets["M"].entry == 110
    assert flipped.real_balance == 109 and flipped.margin_balance == 109


# --------------------------------------------------------------------------
# V2 assembly: collateral, funding
# --------------------------------------------------------------------------


def test_withdrawal_only_collateral_gets_no_margin_credit():
    snapshot = build_snapshot(
        8009,
        balances=[make_balance(real_balance="10"), make_balance(asset="USDE", real_balance="100")],
        positions=[],
        definitions=[make_market_def()],
        summaries={},
        asset_definitions=[make_asset_def(asset="USDE", price_haircut="0.075", status="WITHDRAWAL_ONLY")],
        asset_prices={"USDE": "1"},
    )
    assert snapshot.margin.real_balance == 10
    row = next(r for r in snapshot.collateral if r["asset"] == "USDE")
    assert row["margin_credit_usd"] == 0 and "WITHDRAWAL_ONLY" in row["credit_note"]


@pytest.mark.parametrize(
    "side,market_fv,expected",
    [
        (Side.B, "1010", D("-10")),  # long paid 10 per unit since entry: debited
        (Side.A, "1010", D("0")),  # short received it: never credited
        (Side.A, "990", D("-10")),  # short paid
    ],
)
def test_unsettled_funding_is_estimated_as_a_debit_only(side, market_fv, expected):
    snapshot = build_snapshot(
        8009,
        balances=[make_balance(real_balance="1000")],
        positions=[make_position(side=side, qty="1", avg_entry_funding_value="1000")],
        definitions=[make_market_def()],
        summaries={"ETHRUSDPERP": make_market_summary(long_funding_value=market_fv, short_funding_value=market_fv)},
    )
    assert snapshot.funding_estimate == expected
    assert snapshot.margin.real_balance == 1000 + expected


# --------------------------------------------------------------------------
# trade: preview, refusal, sizing
# --------------------------------------------------------------------------


def args(*command):
    return trade.build_parser().parse_args(list(command))


def spy(*extra, side="long"):
    return args("order", "market", "SPYRUSDPERP", "--side", side, *extra)


@pytest.fixture
def spy_account(patch_client: FakeClient, monkeypatch) -> FakeClient:
    """34 USDC, SPY at 758.82 on mainnet parameters, the kit's own size guards out of the way."""
    monkeypatch.setenv("REYA_MAX_ORDER_NOTIONAL", "100000")
    monkeypatch.setenv("REYA_MAX_LEVERAGE", "100")
    monkeypatch.setenv("REYA_MARGIN_HEADROOM", "0")
    patch_client.get_account_balances.return_value = [make_balance(real_balance="34")]
    patch_client.reference.get_perp_market_definitions.return_value = [spy_definition()]
    patch_client.markets.get_perp_market_summary.return_value = make_price(symbol="SPYRUSDPERP", mark_price="758.82")
    patch_client.create_limit_order.return_value = CreateOrderResponse(order_id="1", status="FILLED", cum_qty="0.1")
    return patch_client


async def refused(command, invocation, capsys) -> dict:
    with pytest.raises(SystemExit):
        await command(invocation)
    return parse_stdout(capsys)


async def test_order_between_im_and_reserve_is_refused_with_the_settlement_reserve_reason(spy_account, capsys):
    # The tester's case: 675 USDC of SPY on 34 USDC with 1% slippage passes initial margin but not the reserve.
    result = await refused(trade.cmd_order_market, spy("--notional", "675"), capsys)
    assert [r["check"] for r in result["refused_by"]] == ["engine_margin"]
    assert result["margin"]["engine_check"] == "below_settlement_reserve"
    assert "below its settlement reserve" in result["error"]
    spy_account.create_limit_order.assert_not_called()


async def test_max_size_fits_and_one_more_step_does_not(spy_account, capsys):
    await trade.cmd_order_market(spy("--max_size", "--preview"))
    preview = parse_stdout(capsys)
    qty = D(preview["qty"])
    assert preview["would_send"] is True and preview["margin"]["engine_check"] == "ok"
    assert preview["sizing"]["size_limited_by"] == "engine_margin"
    assert preview["margin"]["max_qty"] == preview["qty"]
    await trade.cmd_order_market(spy("--qty", str(qty + D("0.0001")), "--preview"))
    assert parse_stdout(capsys)["margin"]["engine_check"] == "below_settlement_reserve"


async def test_headroom_keeps_max_size_below_the_engine_limit(spy_account, monkeypatch, capsys):
    await trade.cmd_order_market(spy("--max_size", "--preview"))
    exact = D(parse_stdout(capsys)["qty"])
    monkeypatch.setenv("REYA_MARGIN_HEADROOM", "0.002")
    await trade.cmd_order_market(spy("--max_size", "--preview"))
    padded = parse_stdout(capsys)
    assert D(padded["qty"]) < exact and padded["margin"]["reserve_headroom_usd"] >= 0
    await trade.cmd_order_market(spy("--qty", str(exact), "--preview"))
    assert parse_stdout(capsys)["margin"]["engine_check"] == "tight"


async def test_usage_sizes_to_the_target_after_the_fill(spy_account, capsys):
    await trade.cmd_order_market(spy("--usage", "0.5", "--preview"))
    preview = parse_stdout(capsys)
    assert preview["sizing"]["size_limited_by"] == "usage"
    assert preview["margin"]["margin_usage_after"] <= 0.5
    await trade.cmd_order_market(spy("--qty", str(D(preview["qty"]) + D("0.0001")), "--preview"))
    assert parse_stdout(capsys)["margin"]["margin_usage_after"] > 0.5


async def test_sizing_stays_within_the_kit_guards_unless_allow_large(spy_account, monkeypatch, capsys):
    monkeypatch.delenv("REYA_MAX_ORDER_NOTIONAL")  # default cap: the account value
    await trade.cmd_order_market(spy("--max_size", "--preview"))
    capped = parse_stdout(capsys)
    assert capped["sizing"]["size_limited_by"] == "max_order_notional" and capped["refused_by"] == []
    await trade.cmd_order_market(spy("--max_size", "--allow_large", "--preview"))
    assert parse_stdout(capsys)["sizing"]["size_limited_by"] == "engine_margin"


async def test_sizing_refuses_the_reducing_side_and_reduce_only(spy_account, capsys):
    spy_account.get_positions.return_value = [make_position(symbol="SPYRUSDPERP", qty="0.02", avg_entry_price="758.82")]
    spy_account.markets.get_perp_markets_summary.return_value = [make_price(symbol="SPYRUSDPERP", mark_price="758.82")]
    with pytest.raises(ValueError, match="opens or adds to a position"):
        await trade.cmd_order_market(spy("--max_size", side="short"))
    with pytest.raises(ValueError, match="not a reduce-only one"):
        await trade.cmd_order_market(spy("--max_size", "--reduce_only", side="short"))


def test_usage_must_be_a_fraction_below_one(capsys):
    with pytest.raises(SystemExit):
        spy("--usage", "1")


async def test_reduce_only_is_never_refused_even_below_the_liquidation_threshold(spy_account, capsys):
    # 1 SPY long bought at 800, now 758.82: equity -7.18, under the liquidation margin.
    spy_account.get_positions.return_value = [make_position(symbol="SPYRUSDPERP", qty="1", avg_entry_price="800")]
    await trade.cmd_order_market(spy("--qty", "1", "--reduce_only", side="short"))
    result = parse_stdout(capsys)
    assert result["margin"]["engine_check"] == "below_liquidation_threshold"
    assert "sends this reduce-only order anyway" in result["margin"]["note"]
    spy_account.create_limit_order.assert_awaited_once()


async def test_a_reducing_order_without_reduce_only_gets_the_engine_check_and_a_hint(spy_account, capsys):
    # 1 SPY at 758.82 on 27 USDC: above the liquidation margin (23.35) but below initial margin (30.36).
    spy_account.get_account_balances.return_value = [make_balance(real_balance="27")]
    spy_account.get_positions.return_value = [make_position(symbol="SPYRUSDPERP", qty="1", avg_entry_price="758.82")]
    result = await refused(trade.cmd_order_market, spy("--qty", "0.5", side="short"), capsys)
    assert result["margin"]["engine_check"] == "below_initial_margin"
    assert "Add --reduce_only" in result["margin"]["reduce_only_hint"]


async def test_skip_margin_check_sends_a_predicted_reject(spy_account, capsys):
    await trade.cmd_order_market(spy("--notional", "675", "--skip_margin_check"))
    assert parse_stdout(capsys)["margin"]["engine_check"] == "below_settlement_reserve"
    spy_account.create_limit_order.assert_awaited_once()


def venue_margin_reject() -> ApiException:
    message = "Insufficient margin: this order would leave the account 3.027139 rUSD below its settlement reserve."
    return ApiException(
        status=400, body=json.dumps({"error": "ACCOUNT_BELOW_INITIAL_MARGIN_ERROR", "message": message})
    )


async def failed_send(command, invocation) -> dict:
    """What main() prints when the send raises: the error result, exiting 1."""
    with pytest.raises(ApiException) as raised:
        await command(invocation)
    result = trade._error_result(invocation, raised.value)
    with pytest.raises(SystemExit) as exited:
        trade.output_result(result)
    assert exited.value.code == 1
    return result


@pytest.mark.parametrize(
    "command, invocation",
    [
        (trade.cmd_order_market, ("order", "market", "SPYRUSDPERP", "--side", "long", "--notional", "675")),
        (
            trade.cmd_order_limit,
            ("order", "limit", "SPYRUSDPERP", "--side", "long", "--price", "758", "--qty", "2"),
        ),
    ],
)
async def test_a_venue_reject_after_skip_margin_check_keeps_the_kit_prediction(
    spy_account, capsys, command, invocation
):
    spy_account.create_limit_order.side_effect = venue_margin_reject()
    result = await failed_send(command, args(*invocation, "--skip_margin_check"))
    capsys.readouterr()
    # The engine's reply and the kit's prediction side by side.
    assert result["error_code"] == "ACCOUNT_BELOW_INITIAL_MARGIN_ERROR"
    assert result["error"] == result["message"] and "settlement reserve" in result["message"]
    assert result["margin"]["engine_check"] not in ("ok", "tight")
    assert result["margin"]["shortfall_usd"] > 0 and result["margin"]["engine_message"]
    assert result["mark_price_usd"] == 758.82 and result["qty"] and result["limit_px_usd"]
    assert "refused_by" not in result
    # Unchanged: the client ID that finds the order if it did reach the venue.
    assert result["client_order_id"] == str(spy_account.create_limit_order.call_args.args[0].client_order_id)


async def test_a_lost_reply_keeps_the_plan_too(spy_account, capsys):
    spy_account.create_limit_order.side_effect = TimeoutError()
    invocation = spy("--qty", "0.02")
    with pytest.raises(TimeoutError) as raised:
        await trade.cmd_order_market(invocation)
    result = trade._error_result(invocation, raised.value)
    assert result["error"] == "TimeoutError: " and result["margin"]["engine_check"] == "ok"
    assert result["client_order_id"] == str(invocation.client_order_id)


async def test_a_failure_before_sending_carries_no_plan(spy_account):
    spy_account.markets.get_perp_market_summary.side_effect = ApiException(status=503, body="down")
    invocation = spy("--qty", "0.01")
    with pytest.raises(ApiException) as raised:
        await trade.cmd_order_market(invocation)
    assert "margin" not in trade._error_result(invocation, raised.value)


async def test_a_reused_invocation_never_reports_the_earlier_plan(spy_account):
    invocation = spy("--qty", "0.02")
    spy_account.create_limit_order.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await trade.cmd_order_market(invocation)
    assert invocation.order_plan is not None
    # The same args again, failing this time before any plan is built.
    spy_account.markets.get_perp_market_summary.side_effect = ApiException(status=503, body="down")
    with pytest.raises(ApiException) as raised:
        await trade.cmd_order_market(invocation)
    assert "margin" not in trade._error_result(invocation, raised.value)


async def test_a_failed_modify_keeps_the_plan_without_a_client_id(spy_account, capsys):
    spy_account.get_open_orders.return_value = [
        make_order(order_id="123", symbol="SPYRUSDPERP", qty="0.02", limit_px="750").model_copy(
            update={"client_order_id": "7", "post_only": True, "cum_qty": "0"}
        )
    ]
    spy_account.modify_order = AsyncMock(side_effect=venue_margin_reject())
    invocation = args("order", "modify", "--order_id", "123", "--qty", "2", "--price", "758", "--skip_margin_check")
    result = await failed_send(trade.cmd_order_modify, invocation)
    assert result["action"] == "order_modify" and result["order_id"] == "123"
    assert result["margin"]["engine_check"] not in ("ok", "tight")
    assert result["error_code"] == "ACCOUNT_BELOW_INITIAL_MARGIN_ERROR"
    assert "client_order_id" not in result and "refused_by" not in result


async def test_after_fill_figures_are_the_worst_case_full_fill_at_the_limit(spy_account, capsys):
    # The same size as a market order (charged its 1% slippage) and as a limit at the mark.
    await trade.cmd_order_market(spy("--qty", "0.5", "--preview"))
    market = parse_stdout(capsys)["margin"]
    await trade.cmd_order_limit(
        args("order", "limit", "SPYRUSDPERP", "--side", "long", "--price", "758.82", "--qty", "0.5", "--preview")
    )
    at_mark = parse_stdout(capsys)["margin"]
    assert market["after_fill_basis"] == at_mark["after_fill_basis"] == AFTER_FILL_BASIS
    assert "worst case" in AFTER_FILL_BASIS
    # A better fill price leaves less usage and a liquidation price further away.
    assert at_mark["margin_usage_after"] < market["margin_usage_after"]
    assert D(at_mark["liquidation_price_after_usd"]) < D(market["liquidation_price_after_usd"])


def test_runner_refuses_skip_margin_check():
    assert "--skip_margin_check" in runner.OVERRIDE_FLAGS


async def test_limit_preview_says_when_the_engine_checks_it(spy_account, capsys):
    await trade.cmd_order_limit(
        args("order", "limit", "SPYRUSDPERP", "--side", "long", "--price", "750", "--qty", "0.02", "--preview")
    )
    margin = parse_stdout(capsys)["margin"]
    assert margin["engine_check"] == "ok" and "crosses the book" in margin["engine_checks_at"]


# --------------------------------------------------------------------------
# query: margin and positions
# --------------------------------------------------------------------------


async def test_margin_reports_equity_with_unrealized_pnl_usage_and_liquidation(patch_client: FakeClient, capsys):
    patch_client.get_account_balances.return_value = [make_balance(real_balance="100")]
    patch_client.get_positions.return_value = [make_position(qty="0.5", avg_entry_price="1900")]  # +50 at 2000
    patch_client.markets.get_perp_markets_summary.return_value = [make_price(mark_price="2000")]
    await query.cmd_margin(query.build_parser().parse_args(["margin"]))
    result = parse_stdout(capsys)
    assert result["equity_usd"] == 150 and result["unrealized_pnl_usd"] == 50
    assert result["liquidation_margin_req_usd"] == 30 and result["margin_usage"] == 0.2  # 0.03 x 1000 / 150
    assert result["margin_buffer_usd"] == 120  # equity - LM, not collateral - LM
    assert result["initial_margin_req_usd"] == 40  # 1.3333... x 30, from the fixture's 0.04 / 0.03
    # 150 + 0.5 (p - 2000) = 0.03 x 0.5 p  ->  p = 1700 / 0.485
    position = result["positions"][0]
    assert position["liquidation_price_usd"] == "1752.578"
    assert position["distance_to_liquidation_pct"] == 12.37


async def test_positions_show_liquidation_price_and_account_usage(patch_client: FakeClient, capsys):
    patch_client.get_account_balances.return_value = [make_balance(real_balance="100")]
    patch_client.get_positions.return_value = [make_position(qty="0.5", avg_entry_price="1900")]
    patch_client.markets.get_perp_markets_summary.return_value = [make_price(mark_price="2000")]
    await query.cmd_positions(query.build_parser().parse_args(["positions"]))
    row = parse_stdout(capsys)[0]
    assert row["liquidation_price_usd"] == "1752.578" and row["account_margin_usage"] == 0.2


async def test_positions_survive_a_failed_margin_read(patch_client: FakeClient, capsys):
    patch_client.get_positions.return_value = [make_position(qty="0.5")]
    patch_client.markets.get_perp_markets_summary.return_value = [make_price(mark_price="2000")]
    patch_client.reference.get_perp_market_definitions.return_value = []
    await query.cmd_positions(query.build_parser().parse_args(["positions"]))
    row = parse_stdout(capsys)[0]
    assert row["qty"] == "0.5" and row["liquidation_price_usd"] is None and row["margin_unavailable_reason"]


async def test_margin_values_collateral_with_its_haircut(patch_client: FakeClient, capsys):
    patch_client.get_account_balances.return_value = [make_balance(asset="ETH", real_balance="0.1")]
    patch_client.reference.get_asset_definitions.return_value = [make_asset_def(asset="ETH", price_haircut="0.1")]
    patch_client.markets.get_asset_oracle_prices.return_value = [make_asset_price("ETH", "2000")]
    await query.cmd_margin(query.build_parser().parse_args(["margin"]))
    assert parse_stdout(capsys)["equity_usd"] == 180


# --------------------------------------------------------------------------
# paper
# --------------------------------------------------------------------------


def test_paper_valuation_shows_the_live_margin_view():
    now = 1779000000000
    state = {
        "ledger_id": "x",
        "cash": "100",
        "initial_collateral": "100",
        "positions": {"ETHRUSDPERP": {"qty": "0.5", "basis": "950"}},
        "realized_pnl": "0",
        "fees": "0",
        "fee_source": "assumed",
        "taker_fee_bps": "0",
    }
    summaries = [{"symbol": "ETHRUSDPERP", "markPrice": "2000", "pricesUpdatedAt": now}]
    definitions = [
        {
            "symbol": "ETHRUSDPERP",
            "liquidationMarginParameter": "0.03",
            "initialMarginParameter": "0.04",
            "tickSize": "0.001",
        }
    ]
    value = valuation(state, summaries, now, definitions)
    assert value["margin"]["margin_usage"] == 0.2
    assert value["positions"][0]["liquidation_price"] == "1752.578"
    assert value["risk_model"] == "conservative_virtual_1x_not_reya_margin"


def test_snapshot_without_markets_needs_no_definitions():
    snapshot = build_snapshot(
        8009,
        balances=[make_balance(real_balance="5")],
        positions=[],
        definitions=[],
        summaries={},
    )
    assert snapshot.settled().lm == 0 and snapshot.margin.margin_balance == 5


def test_namespace_definitions_are_accepted():
    definition = SimpleNamespace(symbol="X", liquidation_margin_parameter="0.02", initial_margin_parameter="0.026")
    assert im_multiplier_from([definition]) == D("1.3")
