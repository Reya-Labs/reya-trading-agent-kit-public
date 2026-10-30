"""Unit tests for `agent_kit/scripts/query.py`.

Strategy: drive each async cmd function directly with a constructed args namespace,
mock the SDK / OpenAPI clients via the fixtures in conftest, capture stdout, assert
the parsed JSON.
"""

import argparse
from unittest.mock import AsyncMock

import pytest
from sdk.open_api.models.side import Side

from agent_kit.scripts import query
from agent_kit.scripts._common import exception_result
from agent_kit.scripts._http import RegionUnavailableError
from agent_kit.scripts._v2 import V2StatusError
from agent_kit.scripts.query import (
    cmd_balances,
    cmd_margin,
    cmd_market_funding,
    cmd_market_info,
    cmd_orders,
    cmd_positions,
    cmd_prices,
)
from agent_kit.tests.conftest import (
    DEFAULT_ACCOUNT_ID,
    FakeClient,
    make_account_summary,
    make_balance,
    make_market_def,
    make_market_summary,
    make_order,
    make_position,
    make_position_risk,
    make_price,
    parse_stdout,
)

# --------------------------------------------------------------------------
# prices  (no auth)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prices_single_symbol(patch_market_api: AsyncMock, capsys: pytest.CaptureFixture) -> None:
    patch_market_api.get_perp_market_summary.return_value = make_price(
        symbol="ETHRUSDPERP", oracle_price="2100.5", mark_price="2100.7", updated_at=1779000000000
    )
    await cmd_prices(argparse.Namespace(symbol="ETHRUSDPERP"))
    output = parse_stdout(capsys)
    assert output == {
        "symbol": "ETHRUSDPERP",
        "oracle_price_usd": 2100.5,
        "updated_at": 1779000000000,
        "mark_price_usd": 2100.7,
    }


@pytest.mark.asyncio
async def test_prices_all_symbols(patch_market_api: AsyncMock, capsys: pytest.CaptureFixture) -> None:
    patch_market_api.get_perp_markets_summary.return_value = [
        make_price(symbol="ETHRUSDPERP", oracle_price="2100"),
        make_price(symbol="BTCRUSDPERP", oracle_price="71000", mark_price=None),
    ]
    await cmd_prices(argparse.Namespace(symbol=None))
    output = parse_stdout(capsys)
    assert isinstance(output, list)
    assert len(output) == 2
    # Unavailable prices remain explicitly null.
    btc = next(o for o in output if o["symbol"] == "BTCRUSDPERP")
    assert btc["mark_price_usd"] is None


# --------------------------------------------------------------------------
# positions  (auth, scoped to PERP_ACCOUNT_ID_1)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positions_filters_to_configured_account(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B, qty="0.05"),
        make_position(account_id=8010, symbol="ETHRUSDPERP", side=Side.B, qty="0.5"),  # other account
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="BTCRUSDPERP", side=Side.A, qty="0.001"),
    ]
    await cmd_positions(argparse.Namespace(symbol=None))
    output = parse_stdout(capsys)
    assert isinstance(output, list)
    assert len(output) == 2
    symbols = {o["symbol"] for o in output}
    assert symbols == {"ETHRUSDPERP", "BTCRUSDPERP"}
    # Side.B (buy) → long, Side.A (ask) → short
    eth = next(o for o in output if o["symbol"] == "ETHRUSDPERP")
    btc = next(o for o in output if o["symbol"] == "BTCRUSDPERP")
    assert eth["side"] == "long"
    assert btc["side"] == "short"
    # Symbol validation only runs when a --symbol filter is given; the unfiltered
    # path reads market definitions once, for the liquidation prices.
    assert patch_client.reference.get_perp_market_definitions.await_count == 1


@pytest.mark.asyncio
async def test_positions_symbol_filter(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP"),
        make_market_def(symbol="BTCRUSDPERP", market_id=2),
    ]
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP"),
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="BTCRUSDPERP"),
    ]
    await cmd_positions(argparse.Namespace(symbol="BTCRUSDPERP"))
    output = parse_stdout(capsys)
    assert len(output) == 1
    assert output[0]["symbol"] == "BTCRUSDPERP"


@pytest.mark.asyncio
async def test_positions_unknown_symbol_raises(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """A typo'd --symbol errors instead of silently returning [] (which reads as 'no positions')."""
    patch_client.reference.get_perp_market_definitions.return_value = [make_market_def(symbol="ETHRUSDPERP")]
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP"),
    ]
    with pytest.raises(ValueError, match="Unknown symbol 'DOGERUSDPERP'"):
        await cmd_positions(argparse.Namespace(symbol="DOGERUSDPERP"))


@pytest.mark.asyncio
async def test_positions_empty_state(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.get_positions.return_value = []
    await cmd_positions(argparse.Namespace(symbol=None))
    assert parse_stdout(capsys) == []


# --------------------------------------------------------------------------
# orders  (auth, scoped)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orders_filters_to_configured_account(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.get_open_orders.return_value = [
        make_order(order_id="o1", account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP"),
        make_order(order_id="o2", account_id=8010, symbol="ETHRUSDPERP"),  # other account
    ]
    await cmd_orders(argparse.Namespace(symbol=None))
    output = parse_stdout(capsys)
    assert len(output) == 1
    assert output[0]["order_id"] == "o1"


@pytest.mark.asyncio
async def test_orders_symbol_filter(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP"),
        make_market_def(symbol="BTCRUSDPERP", market_id=2),
    ]
    patch_client.get_open_orders.return_value = [
        make_order(order_id="o1", symbol="ETHRUSDPERP"),
        make_order(order_id="o2", symbol="BTCRUSDPERP"),
    ]
    await cmd_orders(argparse.Namespace(symbol="ETHRUSDPERP"))
    output = parse_stdout(capsys)
    assert len(output) == 1
    assert output[0]["order_id"] == "o1"


@pytest.mark.asyncio
async def test_orders_unknown_symbol_raises(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """A typo'd --symbol errors instead of silently returning [] (which reads as 'no orders')."""
    patch_client.reference.get_perp_market_definitions.return_value = [make_market_def(symbol="ETHRUSDPERP")]
    patch_client.get_open_orders.return_value = [make_order(order_id="o1", symbol="ETHRUSDPERP")]
    with pytest.raises(ValueError, match="Unknown symbol 'DOGERUSDPERP'"):
        await cmd_orders(argparse.Namespace(symbol="DOGERUSDPERP"))


# --------------------------------------------------------------------------
# balances  (auth, scoped)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_balances_filters_to_configured_account(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """`balances` returns only rows for PERP_ACCOUNT_ID_1, never the wallet's other accounts."""
    patch_client.get_account_balances.return_value = [
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="RUSD", real_balance="100"),
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="SRUSD", real_balance="25"),
        make_balance(account_id=8010, asset="RUSD", real_balance="500"),
        make_balance(account_id=10000000001, asset="REYA", real_balance="900"),
    ]
    await cmd_balances(argparse.Namespace())
    output = parse_stdout(capsys)
    assert len(output) == 2
    assert {row["asset"] for row in output} == {"RUSD", "SRUSD"}
    assert all(row["account_id"] == DEFAULT_ACCOUNT_ID for row in output)


@pytest.mark.asyncio
async def test_balances_empty_state(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    patch_client.get_account_balances.return_value = []
    await cmd_balances(argparse.Namespace())
    assert parse_stdout(capsys) == []


# --------------------------------------------------------------------------
# margin  (auth, scoped; the API's valuation: accountSummaries + positionRisk)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_margin_maps_the_api_valuation_to_the_kit_fields(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """Every account figure is the API's, under the kit's names and roundings, or the kit's arithmetic on them."""
    patch_client.get_account_summaries.return_value = [
        make_account_summary(account_id=8010, collateral_value="1"),  # another account of the wallet
        make_account_summary(
            collateral_value="900.1234567",
            unrealized_pnl="100",
            initial_margin="65.4321",
            liquidation_margin="50.333333",
            equity="1012.567",  # before haircuts: above the margin balance
        ),
    ]
    patch_client.get_account_balances.return_value = [
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="RUSD", real_balance="100"),
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="ETH", real_balance="0.4"),
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="SRUSD", real_balance="0"),
        make_balance(account_id=8010, asset="RUSD", real_balance="5"),
    ]

    await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)

    assert output["account_id"] == DEFAULT_ACCOUNT_ID
    assert output["equity_usd"] == 1012.56  # equity, rounded down to the cent
    assert output["margin_balance_usd"] == 1000.12  # marginBalance
    assert output["real_balance_usd"] == 900.12  # collateralValue
    assert output["effective_collateral_usd"] == 900.1235
    assert output["unrealized_pnl_usd"] == 100.0
    assert output["unsettled_funding_estimate_usd"] == 0.0
    assert output["initial_margin_req_usd"] == 65.4321
    assert output["liquidation_margin_req_usd"] == 50.333333
    assert output["margin_usage"] == 0.0504  # 50.333333 / 1000.1234567, rounded up
    assert output["margin_usage_pct"] == 5.04
    assert output["liquidatable"] is False
    assert output["margin_buffer_usd"] == 949.79  # marginBalance - LM, not equity - LM
    assert output["initial_margin_buffer_usd"] == 934.69  # freeCollateral
    assert output["health_usd"] == 900.12  # min(marginBalance - LM, collateral)
    assert output["updated_at"] == 1779000000500 and output["prices_updated_at"] == 1779000000000
    # What the account holds; the per-asset value and credit are not published.
    assert output["collateral_breakdown"] == [{"asset": "RUSD", "balance": "100"}, {"asset": "ETH", "balance": "0.4"}]
    assert output["positions"] == []
    assert output["assumptions"] and "the kit's arithmetic on the API's figures" in output["assumptions"][0]
    for gone in ("collateral_value_usd", "im_multiplier", "settlement_reserve_rate"):
        assert gone not in output
    # Read for the configured account only, and nothing the kit would value itself.
    patch_client.get_account_summaries.assert_awaited_once_with(DEFAULT_ACCOUNT_ID)
    patch_client.get_position_risk.assert_awaited_once_with(DEFAULT_ACCOUNT_ID)
    patch_client.reference.get_asset_definitions.assert_not_awaited()
    patch_client.markets.get_asset_oracle_prices.assert_not_awaited()
    patch_client.markets.get_perp_markets_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_margin_positions_are_the_api_position_risk(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    patch_client.get_account_summaries.return_value = [
        make_account_summary(collateral_value="500", unrealized_pnl="2.5", liquidation_margin="3", initial_margin="4")
    ]
    patch_client.get_position_risk.return_value = [
        make_position_risk(
            symbol="ETHRUSDPERP",
            qty="0.05",
            avg_entry_price="1950",
            mark_price="2000",
            funding_since_change="-0.25",
            liquidation_margin="3",
            liquidation_price="1500.000",
        ),
        make_position_risk(
            symbol="BTCRUSDPERP",
            side="A",
            qty="0.001",
            avg_entry_price="60000",
            mark_price="60000",
            liquidation_price=None,
            has_correlated_collateral=True,
        ),
        make_position_risk(account_id=8010, symbol="SOLRUSDPERP"),  # another account's
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", liquidation_margin_parameter="0.03")
    ]

    await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)

    eth, btc = output["positions"]
    assert eth == {
        "symbol": "ETHRUSDPERP",
        "side": "long",
        "qty": "0.05",
        "mark_price_usd": 2000.0,
        "notional_usd": 100.0,
        "avg_entry_price": "1950",
        "unrealized_pnl_usd": 2.5,
        "funding_since_change_usd": -0.25,
        "unsettled_funding_estimate_usd": 0.0,
        "liq_margin_param": 0.03,
        "liq_margin_req_usd": 3.0,
        "liquidation_price_usd": "1500",
        "distance_to_liquidation_pct": 25.0,  # (2000 - 1500) / 2000
        "has_correlated_collateral": False,
    }
    assert (
        btc["side"] == "short" and btc["liquidation_price_usd"] is None and btc["distance_to_liquidation_pct"] is None
    )
    assert btc["has_correlated_collateral"] is True
    assert btc["liq_margin_param"] is None  # its definition was not served: unknown, never guessed


@pytest.mark.asyncio
async def test_margin_short_distance_and_an_account_past_the_line(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    patch_client.get_account_summaries.return_value = [
        make_account_summary(
            collateral_value="10", unrealized_pnl="-12", liquidation_margin="3", initial_margin="4", equity="5"
        )
    ]
    patch_client.get_position_risk.return_value = [
        make_position_risk(side="A", qty="1", mark_price="100", avg_entry_price="88", liquidation_price="95")
    ]
    await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)
    # Liquidatable on the margin balance (-2 < 3), though the equity before haircuts (5) is above the LM.
    assert output["margin_balance_usd"] == -2.0 and output["equity_usd"] == 5.0 and output["liquidatable"] is True
    assert output["margin_usage"] is None and output["margin_usage_pct"] is None  # margin balance not positive
    assert output["health_usd"] == -5.01  # min(-2 - 3, 10), less the engine's 0.000001 for the position
    # A short past its liquidation price: the distance is negative.
    assert output["positions"][0]["distance_to_liquidation_pct"] == -5.0


@pytest.mark.asyncio
async def test_health_is_below_zero_at_the_liquidation_line(patch_client: FakeClient, capsys) -> None:
    """At margin balance - LM exactly 0 with one position, the engine's health is -0.000001 (its rounding allowance
    per position): it refuses every order, closes included. Rounded down to the cent, the kit shows it below 0."""
    patch_client.get_account_summaries.return_value = [
        make_account_summary(collateral_value="10", unrealized_pnl="-7", liquidation_margin="3", initial_margin="4")
    ]
    patch_client.get_position_risk.return_value = [make_position_risk(qty="1", mark_price="100", avg_entry_price="107")]
    await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)
    assert output["margin_buffer_usd"] == 0.0 and output["liquidatable"] is False
    assert output["health_usd"] == -0.01


@pytest.mark.asyncio
async def test_without_equity_or_funding_since_change_the_margin_figures_still_show(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """Until the API's 3.7.7 is deployed it sends no `equity`, and `fundingPnl` (funding since entry) in place of
    `fundingSinceChange`. Those fields are null, never the margin balance or the earlier funding."""
    summary = make_account_summary(collateral_value="500", unrealized_pnl="2.5", liquidation_margin="3")
    del summary["equity"]
    eth = make_position(avg_entry_price="1950")
    risk = make_position_risk(position=eth, mark_price="2000", liquidation_price="1500")
    del risk["fundingSinceChange"]
    risk["fundingPnl"] = "-0.25"
    patch_client.get_account_summaries.return_value = [summary]
    patch_client.get_position_risk.return_value = [risk]
    patch_client.get_positions.return_value = [eth]

    await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)
    assert output["equity_usd"] is None and output["margin_balance_usd"] == 502.5
    assert output["margin_buffer_usd"] == 499.5 and output["health_usd"] == 499.49 and output["liquidatable"] is False
    assert output["positions"][0]["funding_since_change_usd"] is None

    await cmd_positions(argparse.Namespace(symbol=None))
    (position,) = parse_stdout(capsys)
    assert position["funding_since_change_usd"] is None and position["liquidation_price_usd"] == "1500"
    assert "margin_unavailable_reason" not in position


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,cause",
    [("SERVICE_UNAVAILABLE_ERROR", "a price or market input it needs is missing or stale"), (None, "unavailable")],
)
async def test_margin_is_unavailable_when_the_api_cannot_value_the_account(
    patch_client: FakeClient, capsys: pytest.CaptureFixture, code, cause
) -> None:
    """A 503 is figures unavailable with the reason: never a stale or locally computed number. Only the
    valuation's own 503 (its error code) blames a price; a gateway's or a maintenance page's has none."""
    patch_client.get_position_risk.side_effect = V2StatusError("/wallet/0x1/positionRisk", 503, code)

    with pytest.raises(SystemExit) as exit_info:
        await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)

    assert exit_info.value.code == 1
    assert output["error_code"] == "VALUATION_UNAVAILABLE"
    assert output["http_status"] == 503 and output.get("api_error_code") == code
    assert "could not value this account" in output["error"] and "no figures of its own" in output["error"]
    assert cause in output["error"] and ("price" in output["error"]) == (code is not None)
    assert output["account_id"] == DEFAULT_ACCOUNT_ID
    assert "equity_usd" not in output and "positions" not in output
    patch_client.get_account_balances.assert_not_awaited()


@pytest.mark.asyncio
async def test_margin_for_an_account_the_api_does_not_value(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """No summary row for the account (a spot account, or one the wallet no longer owns) is no valuation, not zero."""
    patch_client.get_account_summaries.return_value = [make_account_summary(account_id=8010)]
    with pytest.raises(SystemExit):
        await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)
    assert output["error_code"] == "VALUATION_UNAVAILABLE"
    assert f"no valuation for account {DEFAULT_ACCOUNT_ID}" in output["error"]


@pytest.mark.asyncio
async def test_margin_for_a_wallet_without_accounts(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """The API answers 400 NO_ACCOUNTS_FOUND_ERROR for a wallet with no accounts (an account read racing it)."""
    no_accounts = V2StatusError("/wallet/0x1/accountSummaries", 400, "NO_ACCOUNTS_FOUND_ERROR")
    patch_client.get_account_summaries.side_effect = no_accounts
    patch_client.get_position_risk.side_effect = no_accounts
    with pytest.raises(SystemExit):
        await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)
    assert output["error_code"] == "VALUATION_UNAVAILABLE" and output["api_error_code"] == "NO_ACCOUNTS_FOUND_ERROR"
    assert "no accounts for this wallet" in output["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["a float equity", "no entry price"])
async def test_margin_rejects_a_malformed_valuation(
    patch_client: FakeClient, capsys: pytest.CaptureFixture, malformed
) -> None:
    summary, position = make_account_summary(), make_position_risk()
    if malformed == "a float equity":
        summary["equity"] = 1000.5  # not the API's exact decimal string: optional, but checked when sent
    else:
        del position["avgEntryPrice"]
    patch_client.get_account_summaries.return_value = [summary]
    patch_client.get_position_risk.return_value = [position]
    with pytest.raises(SystemExit):
        await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)
    assert output["error_code"] == "VALUATION_UNAVAILABLE" and "could not be read" in output["error"]


@pytest.mark.parametrize("command", ["margin", "positions"])
@pytest.mark.parametrize(
    "failure,code,status",
    [
        (V2StatusError("/wallet/0x1/positionRisk", 429), "EDGE_RATE_LIMITED", 429),
        (RegionUnavailableError(), "REGION_UNAVAILABLE", 403),
    ],
)
async def test_an_edge_ban_or_a_regional_block_keeps_its_code(
    patch_client: FakeClient, command, failure, code, status
) -> None:
    """Not a valuation failure: it reaches `main` unchanged, which reports it with the code any other read gives
    (docs/error-codes.md), not VALUATION_UNAVAILABLE and its advice to read again in a few seconds."""
    patch_client.get_positions.return_value = [make_position()]
    patch_client.get_position_risk.side_effect = failure
    with pytest.raises(type(failure)) as raised:
        await getattr(query, "cmd_" + command)(query.build_parser().parse_args([command]))
    assert raised.value is failure
    result = exception_result(raised.value)
    assert (result["error_code"], result["http_status"]) == (code, status)


# --------------------------------------------------------------------------
# market info  (no auth)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_market_info_single_symbol(patch_reference_api: AsyncMock, capsys: pytest.CaptureFixture) -> None:
    patch_reference_api.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP"),
        make_market_def(symbol="BTCRUSDPERP", market_id=2, max_leverage=40),
    ]
    await cmd_market_info(argparse.Namespace(symbol="BTCRUSDPERP"))
    output = parse_stdout(capsys)
    assert output["symbol"] == "BTCRUSDPERP"
    assert output["market_id"] == 2
    assert output["max_leverage"] == 40


@pytest.mark.asyncio
async def test_market_info_unknown_symbol_raises(patch_reference_api: AsyncMock, capsys: pytest.CaptureFixture) -> None:
    patch_reference_api.get_perp_market_definitions.return_value = [make_market_def(symbol="ETHRUSDPERP")]
    with pytest.raises(ValueError, match="not found in market definitions"):
        await cmd_market_info(argparse.Namespace(symbol="DOGERUSDPERP"))


@pytest.mark.asyncio
async def test_market_info_all_markets(patch_reference_api: AsyncMock, capsys: pytest.CaptureFixture) -> None:
    patch_reference_api.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP"),
        make_market_def(symbol="BTCRUSDPERP", market_id=2),
    ]
    await cmd_market_info(argparse.Namespace(symbol=None))
    output = parse_stdout(capsys)
    assert isinstance(output, list)
    assert len(output) == 2


# --------------------------------------------------------------------------
# market funding  (no auth)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_market_funding_single_symbol(patch_market_api: AsyncMock, capsys: pytest.CaptureFixture) -> None:
    patch_market_api.get_perp_market_summary.return_value = make_market_summary(
        symbol="ETHRUSDPERP", funding_rate="0.0001", long_funding_value="1000"
    )
    await cmd_market_funding(argparse.Namespace(symbol="ETHRUSDPERP"))
    output = parse_stdout(capsys)
    assert output["symbol"] == "ETHRUSDPERP"
    assert output["funding_rate"] == "0.0001"
    assert output["funding_text"] == "0.0001%/h (0.88% a year)"
    assert output["long_funding_value"] == "1000"
    # Funding-focused shape excludes OI cap and price-related fields.
    assert "oi_cap" not in output


@pytest.mark.asyncio
async def test_market_funding_all_markets(patch_market_api: AsyncMock, capsys: pytest.CaptureFixture) -> None:
    patch_market_api.get_perp_markets_summary.return_value = [
        make_market_summary(symbol="ETHRUSDPERP"),
        make_market_summary(symbol="BTCRUSDPERP"),
    ]
    await cmd_market_funding(argparse.Namespace(symbol=None))
    output = parse_stdout(capsys)
    assert isinstance(output, list)
    assert len(output) == 2
