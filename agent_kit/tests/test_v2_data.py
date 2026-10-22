"""Regression tests for bounded history and exact public-account semantics."""

from unittest.mock import AsyncMock

import pytest

from agent_kit.scripts import data
from agent_kit.scripts._reconcile import account_projection, order_reconciliation
from agent_kit.scripts._v2 import V2Client, identifier, parse_json


def page(entries, **meta):
    return {"data": entries, "meta": {"count": len(entries), "limit": 100, **meta}}


async def test_cursor_history_merges_overlap_without_losing_timestamp_ties(monkeypatch):
    client = V2Client("https://example.invalid/v2")
    mock_get = AsyncMock(
        side_effect=[
            page([{"sequenceNumber": 10}, {"sequenceNumber": 9}], nextCursor="next"),
            page([{"sequenceNumber": 9}, {"sequenceNumber": 8}]),
        ]
    )
    monkeypatch.setattr(client, "get", mock_get)
    result = await client.history("/wallet/0x123/transfers")
    assert [row["sequenceNumber"] for row in result["data"]] == [10, 9, 8]
    assert result["coverage"]["reason"] == "available_history_exhausted"
    assert result["coverage"]["retention_complete"] is False
    assert mock_get.call_args_list[1].kwargs["cursor"] == "next"


async def test_time_pagination_preserves_inclusive_boundary_and_detects_saturation(monkeypatch):
    client = V2Client("https://example.invalid/v2")
    entries = [{"sequenceNumber": n + 1, "timestamp": 500} for n in range(100)]
    mock_get = AsyncMock(return_value=page(entries, endTime=500))
    monkeypatch.setattr(client, "get", mock_get)
    result = await client.history("/wallet/0x123/perpExecutions", end_time=1000)
    assert mock_get.call_args_list[1].kwargs["endTime"] == 500
    assert result["coverage"]["reason"] == "timestamp_boundary_saturated"
    assert result["coverage"]["state"] == "incomplete"
    assert len(result["data"]) == 100


async def test_budget_does_not_claim_complete(monkeypatch):
    client = V2Client("https://example.invalid/v2")
    mock_get = AsyncMock(return_value=page([{"sequenceNumber": 1}], nextCursor="more"))
    monkeypatch.setattr(client, "get", mock_get)
    result = await client.history("/wallet/0x123/transfers", max_pages=1)
    assert result["coverage"]["state"] == "incomplete"
    assert result["coverage"]["next_cursor"] == "more"


@pytest.mark.parametrize(
    "pages",
    [
        [page([{"sequenceNumber": 1, "amount": "1"}], nextCursor="x"), page([{"sequenceNumber": 1, "amount": "2"}])],
        [page([], nextCursor="x"), page([], nextCursor="x")],
        [{"data": [], "meta": None}],
        [page([{"sequenceNumber": 2**53}])],
    ],
)
async def test_invalid_history_fails_closed(pages, monkeypatch):
    client = V2Client("https://example.invalid/v2")
    mock_get = AsyncMock(side_effect=pages)
    monkeypatch.setattr(client, "get", mock_get)
    with pytest.raises(ValueError):
        await client.history("/wallet/0x123/transfers")


@pytest.mark.parametrize("value", ["ETH/../wallet", "ETH?token=secret", "", "ETH%2FUSD"])
def test_symbols_cannot_change_route(value):
    with pytest.raises(ValueError):
        identifier(value)


def test_json_numbers_do_not_round_through_float():
    assert parse_json('{"x":1.1234567890123456789,"n":9007199254740991}') == {
        "x": "1.1234567890123456789",
        "n": 9007199254740991,
    }
    with pytest.raises(ValueError):
        parse_json('{"x":NaN}')


def account_inputs():
    # Reference scenario: upstream cash 100 + funding -5 + ADL cash 10 =
    # realBalance 105; base 10 * current/stored multiplier .8/1 = public qty 8.
    # The kit receives ONLY the derived public fields and must not add again.
    return (
        [{"accountId": 7, "asset": "RUSD", "realBalance": "105.000001", "balanceDEPRECATED": "100"}],
        [
            {
                "accountId": 7,
                "exchangeId": 2,
                "symbol": "ETHRUSDPERP",
                "qty": "8",
                "side": "B",
                "avgEntryPrice": "100",
                "avgEntryFundingValue": "0",
            }
        ],
        [{"symbol": "ETHRUSDPERP", "markPrice": "102", "pricesUpdatedAt": 1000}],
        [{"asset": "RUSD", "priceHaircut": "0"}],
        [],
    )


def test_funding_adl_and_rescaled_quantity_are_not_double_counted():
    result = account_projection(7, *account_inputs())
    assert result["effective_collateral_usd"] == "105.000001"
    assert result["estimated_marked_pnl_usd"] == "16"
    assert result["estimated_equity_usd"] == "121.000001"
    assert result["balances"][0]["adjustments_applied_by_kit"] is False
    assert result["safe_for_order_admission"] is False
    assert result["state"] == "incomplete"  # no shared watermark or in-flight exposure


def test_negative_collateral_is_not_discounted_and_decimal_dust_survives():
    balances, positions, markets, assets, prices = account_inputs()
    balances[0]["realBalance"] = "-100.00000000000000000001"
    assets[0]["priceHaircut"] = "0.5"
    result = account_projection(7, balances, [], markets, assets, prices)
    assert result["effective_collateral_usd"] == "-100.00000000000000000001"


def test_missing_account_risk_inputs_do_not_become_zero():
    balances, positions, markets, assets, prices = account_inputs()
    markets[0].pop("markPrice")
    with pytest.raises(ValueError):
        account_projection(7, balances, positions, markets, assets, prices)


def test_order_ids_are_symbol_scoped_and_bust_sequences_are_independent():
    orders = [
        {"accountId": 7, "symbol": symbol, "orderId": "123", "sequenceNumber": n, "cumQty": "3"}
        for n, symbol in enumerate(["ETHRUSDPERP", "BTCRUSDPERP"])
    ]
    fill = {"sequenceNumber": 42, "symbol": "ETHRUSDPERP", "takerAccountId": 7, "takerOrderId": "123", "qty": "1"}
    bust = {**fill, "qty": "0.5"}
    result = order_reconciliation(7, orders, [fill, fill], [bust, bust])
    btc, eth = result
    assert btc["unresolved_executed_qty"] == "3"
    assert eth["settled_qty"] == "1"
    assert eth["busted_qty"] == "0.5"
    assert eth["unresolved_executed_qty"] == "1.5"
    assert eth["outcome"] == "settling_or_missing_history"


def test_adl_execution_without_order_id_is_not_attributed_to_an_order():
    assert (
        order_reconciliation(
            7, [], [{"sequenceNumber": 1, "symbol": "ETHRUSDPERP", "takerAccountId": 7, "qty": "1"}], []
        )
        == []
    )


async def test_candles_supply_required_end_time_without_credentials(monkeypatch):
    get = AsyncMock(return_value={"data": []})
    monkeypatch.setattr(V2Client, "get", get)
    args = data.build_parser().parse_args(["market", "candles", "--symbol", "ETHRUSDPERP"])
    result = await data.run(args)
    assert get.call_args.kwargs["endTime"] > 0
    assert result["api_url"].endswith("/v2")


def test_current_order_snapshot_can_be_newer_than_history_without_sequence():
    historical = {
        "accountId": 7,
        "symbol": "ETHRUSDPERP",
        "orderId": "1",
        "sequenceNumber": 10,
        "lastUpdateAt": 100,
        "cumQty": "1",
    }
    current = {**historical, "lastUpdateAt": 200, "cumQty": "2"}
    current.pop("sequenceNumber")
    result = order_reconciliation(7, [current, historical], [], [])
    assert result[0]["unresolved_executed_qty"] == "2"


def test_missing_cumulative_quantity_is_unknown_not_zero():
    order = {"accountId": 7, "symbol": "ETHRUSDPERP", "orderId": "1"}
    assert order_reconciliation(7, [order], [], [])[0]["outcome"] == "unknown"


def test_concurrent_receipts_ahead_of_order_view_are_not_confirmed():
    order = {"accountId": 7, "symbol": "ETHRUSDPERP", "orderId": "1", "cumQty": "1"}
    fill = {"symbol": "ETHRUSDPERP", "takerAccountId": 7, "takerOrderId": "1", "sequenceNumber": 1, "qty": "2"}
    assert order_reconciliation(7, [order], [fill], [])[0]["outcome"] == "inconsistent_or_concurrent"
