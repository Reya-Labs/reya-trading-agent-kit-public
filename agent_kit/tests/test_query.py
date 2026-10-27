"""Unit tests for `agent_kit/scripts/query.py`.

Strategy: drive each async cmd function directly with a constructed args namespace,
mock the SDK / OpenAPI clients via the fixtures in conftest, capture stdout, assert
the parsed JSON.
"""

import argparse
from unittest.mock import AsyncMock

import pytest
from sdk.open_api.models.side import Side

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
# margin  (auth, scoped; engine margin model; fails fast on missing metadata)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_margin_happy_path_collateral_only(patch_client: FakeClient, capsys: pytest.CaptureFixture) -> None:
    """Collateral-only with no positions: gross = sum(balance × price); effective applies the haircut."""
    patch_client.get_account_balances.return_value = [
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="RUSD", real_balance="100"),
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="ETH", real_balance="0.05"),
    ]
    patch_client.reference.get_asset_definitions.return_value = [
        make_asset_def(asset="RUSD", spot_market_symbol=None, price_haircut="0"),
        make_asset_def(asset="ETH", spot_market_symbol="WETHRUSD", price_haircut="0.1"),
    ]
    patch_client.markets.get_asset_oracle_prices.return_value = [make_asset_price()]
    patch_client.get_positions.return_value = []
    patch_client.reference.get_perp_market_definitions.return_value = []

    await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)

    # RUSD: 100 × 1.0 × (1 - 0) = 100.  ETH: 0.05 × 2000 × (1 - 0.1) = 90.
    assert output["account_id"] == DEFAULT_ACCOUNT_ID
    assert output["collateral_value_usd"] == 200.0  # 100 + 0.05*2000
    assert output["effective_collateral_usd"] == 190.0  # 100 + 90
    assert output["liquidation_margin_req_usd"] == 0.0
    assert output["margin_buffer_usd"] == 190.0
    assert output["positions"] == []
    assert len(output["collateral_breakdown"]) == 2


@pytest.mark.asyncio
async def test_margin_quote_currency_priced_at_one_dollar(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """RUSD has no spot_market_symbol but is treated as 1 USD (quote currency)."""
    patch_client.get_account_balances.return_value = [
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="RUSD", real_balance="123.45"),
    ]
    patch_client.reference.get_asset_definitions.return_value = [
        make_asset_def(asset="RUSD", spot_market_symbol=None, price_haircut="0"),
    ]
    patch_client.markets.get_perp_markets_summary.return_value = []
    patch_client.get_positions.return_value = []
    patch_client.reference.get_perp_market_definitions.return_value = []

    await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)
    assert output["collateral_value_usd"] == 123.45


@pytest.mark.asyncio
async def test_margin_fails_fast_on_missing_asset_definition(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """If a balance asset has no matching asset def (the live BTC flake we hit), raise — don't silently treat as zero."""
    patch_client.get_account_balances.return_value = [
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="BTC", real_balance="0.001"),
    ]
    patch_client.reference.get_asset_definitions.return_value = []  # BTC missing
    patch_client.markets.get_perp_markets_summary.return_value = []
    patch_client.get_positions.return_value = []
    patch_client.reference.get_perp_market_definitions.return_value = []

    with pytest.raises(ValueError, match="BTC.*missing from get_asset_definitions"):
        await cmd_margin(argparse.Namespace())


@pytest.mark.asyncio
async def test_margin_fails_fast_on_missing_asset_price(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """Missing collateral oracle data must fail even when a spot market exists."""
    patch_client.get_account_balances.return_value = [
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="ETH", real_balance="0.05"),
    ]
    patch_client.reference.get_asset_definitions.return_value = [
        make_asset_def(asset="ETH", spot_market_symbol="WETHRUSD", price_haircut="0.1"),
    ]
    patch_client.markets.get_perp_markets_summary.return_value = []  # WETHRUSD missing
    patch_client.get_positions.return_value = []
    patch_client.reference.get_perp_market_definitions.return_value = []

    with pytest.raises(ValueError, match="ETH collateral oracle price is unavailable"):
        await cmd_margin(argparse.Namespace())


@pytest.mark.asyncio
async def test_margin_includes_liquidation_requirement_from_positions(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """Open positions add to liquidation_margin_req_usd via notional × liq_margin_param."""
    patch_client.get_account_balances.return_value = [
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="RUSD", real_balance="500"),
    ]
    patch_client.reference.get_asset_definitions.return_value = [
        make_asset_def(asset="RUSD", spot_market_symbol=None, price_haircut="0"),
    ]
    patch_client.markets.get_perp_markets_summary.return_value = [
        make_price(symbol="ETHRUSDPERP", oracle_price="1500", mark_price="2000"),
    ]
    patch_client.get_positions.return_value = [
        # Entry funding value equal to the market's: no unsettled funding.
        make_position(
            account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B, qty="0.05", avg_entry_funding_value="1000"
        ),
    ]
    patch_client.reference.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", liquidation_margin_parameter="0.03"),
    ]

    await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)

    # Position notional = 0.05 × mark 2000 (the 1500 oracle is ignored) = 100. Liq req = 100 × 0.03 = 3.
    assert output["liquidation_margin_req_usd"] == 3.0
    assert output["effective_collateral_usd"] == 500.0
    assert output["margin_buffer_usd"] == 497.0
    assert len(output["positions"]) == 1
    assert output["positions"][0]["mark_price_usd"] == 2000.0
    assert output["positions"][0]["notional_usd"] == 100.0
    assert output["positions"][0]["liq_margin_req_usd"] == 3.0
    # Equity 500 includes the zero PnL; usage = 3 / 500.
    assert output["equity_usd"] == 500.0
    assert output["margin_usage"] == 0.006
    # Long 0.05 liquidates where 500 + 0.05 (p - 2000) = 0.03 x 0.05 p: p = (100 - 500) / (0.05 x 0.97) < 0.
    assert output["positions"][0]["liquidation_price_usd"] is None


@pytest.mark.asyncio
async def test_margin_negative_balance_counts_at_full_value(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    """A debt counts as Core counts it: at price / (1 - haircut), more than its value."""
    patch_client.get_account_balances.return_value = [
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="RUSD", real_balance="100"),
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="ETH", real_balance="-0.01"),
    ]
    patch_client.reference.get_asset_definitions.return_value = [
        make_asset_def(asset="RUSD", spot_market_symbol=None, price_haircut="0"),
        make_asset_def(asset="ETH", spot_market_symbol="WETHRUSD", price_haircut="0.1"),
    ]
    patch_client.markets.get_asset_oracle_prices.return_value = [make_asset_price()]
    patch_client.get_positions.return_value = []
    patch_client.reference.get_perp_market_definitions.return_value = []

    await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)

    # ETH debt: -0.01 × 2000 = -20 in value; Core counts -20 / (1 - 0.1) = -22.222223 (PriceHelpers.sol).
    eth_row = next(r for r in output["collateral_breakdown"] if r["asset"] == "ETH")
    assert eth_row["value_usd"] == -20.0
    assert eth_row["effective_value_usd"] == -22.2222
    assert output["collateral_value_usd"] == 80.0
    assert output["effective_collateral_usd"] == 77.7778


async def test_margin_marks_collateral_the_engine_check_does_not_credit(
    patch_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    patch_client.get_account_balances.return_value = [
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="RUSD", real_balance="10"),
        make_balance(account_id=DEFAULT_ACCOUNT_ID, asset="RAMBER", real_balance="100"),
    ]
    patch_client.reference.get_asset_definitions.return_value = [
        make_asset_def(asset="RAMBER", price_haircut="0.075", status="WITHDRAWAL_ONLY"),
    ]
    patch_client.markets.get_asset_oracle_prices.return_value = [make_asset_price("RAMBER", "1")]
    patch_client.get_positions.return_value = []
    patch_client.reference.get_perp_market_definitions.return_value = []

    await cmd_margin(argparse.Namespace())
    output = parse_stdout(capsys)

    row = next(r for r in output["collateral_breakdown"] if r["asset"] == "RAMBER")
    assert row["effective_value_usd"] == 92.5 and row["engine_credit_usd"] == 0 and "credit_note" in row
    assert output["equity_usd"] == 102.5 and output["health_usd"] == 10


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
