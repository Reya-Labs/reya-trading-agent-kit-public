"""Regression tests for bounded history and exact public-account semantics."""

from types import SimpleNamespace

from unittest.mock import AsyncMock

import pytest

from agent_kit.scripts import data, history
from agent_kit.scripts._reconcile import account_projection, order_reconciliation
from agent_kit.scripts._v2 import V2Client, V2StatusError, error_code, identifier, parse_json
from agent_kit.tests.conftest import make_account_summary, make_position_risk


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
    # Reference scenario: upstream cash 100 + funding -5 + ADL cash 10 = realBalance 105; base 10 * current/stored
    # multiplier .8/1 = public qty 8. The kit receives only the public records and the API's valuation of them,
    # and adds nothing of its own.
    position = {
        "accountId": 7,
        "exchangeId": 2,
        "symbol": "ETHRUSDPERP",
        "qty": "8",
        "side": "B",
        "avgEntryPrice": "100",
        "avgEntryFundingValue": "0",
        "lastTradeSequenceNumber": 41,
    }
    risk = make_position_risk(
        account_id=7,
        symbol="ETHRUSDPERP",
        qty="8",
        avg_entry_price="100",
        last_trade_sequence_number=41,
        mark_price="102",
        funding_since_change="-5",
        liquidation_price="88.1",
    )
    return (
        [{"accountId": 7, "asset": "RUSD", "realBalance": "105.000001", "balanceDEPRECATED": "100"}],
        [position],
        [make_account_summary(account_id=7, collateral_value="105.000001", unrealized_pnl="16")],
        [risk],
    )


def test_state_shows_the_api_valuation_beside_the_records():
    result = account_projection(7, *account_inputs())
    assert result["effective_collateral_usd"] == "105.000001"
    assert result["estimated_marked_pnl_usd"] == "16"
    assert result["estimated_equity_usd"] == "121.000001"  # equity: rUSD only, so the margin balance
    assert result["margin_balance_usd"] == result["account_summary"]["marginBalance"] == "121.000001"
    assert result["balances"] == [
        {
            "accountId": 7,
            "asset": "RUSD",
            "realBalance": "105.000001",
            "balanceDEPRECATED": "100",
            "adjustments_applied_by_kit": False,
        }
    ]  # no per-asset value: the API values the account's total only
    (position,) = result["positions"]
    assert position["estimated_marked_pnl_usd"] == "16" and position["mark_price"] == "102"
    assert (
        position["position_risk"]["liquidationPrice"] == "88.1"
        and position["position_risk"]["fundingSinceChange"] == "-5"
    )
    assert "valuation_unavailable_reason" not in result and "valuation_unavailable_reason" not in position
    assert result["safe_for_order_admission"] is False
    assert result["state"] == "incomplete"  # no shared watermark or in-flight exposure


def test_state_before_the_api_sends_equity_leaves_it_null():
    """Until the API's 3.7.7 is deployed: the equity is null, never the margin balance, and the rest is shown."""
    balances, positions, summaries, risk = account_inputs()
    del summaries[0]["equity"]
    del risk[0]["fundingSinceChange"]
    result = account_projection(7, balances, positions, summaries, risk)
    assert result["estimated_equity_usd"] is None and result["margin_balance_usd"] == "121.000001"
    assert "valuation_unavailable_reason" not in result and result["positions"][0]["mark_price"] == "102"


def test_state_without_a_valuation_keeps_the_records_and_says_why():
    balances, positions, _, _ = account_inputs()
    result = account_projection(7, balances, positions, unavailable="The API could not value this account right now")
    assert result["estimated_equity_usd"] is None and result["account_summary"] is None
    assert result["valuation_unavailable_reason"] == "The API could not value this account right now"
    assert result["balances"][0]["realBalance"] == "105.000001"
    (position,) = result["positions"]
    assert position["qty"] == "8" and position["position_risk"] is None and position["estimated_marked_pnl_usd"] is None


@pytest.mark.parametrize("raced", ["another fill", "closed between the reads", "opened between the reads"])
def test_state_never_shows_a_valuation_of_another_fill(raced):
    balances, positions, summaries, risk = account_inputs()
    if raced == "another fill":
        risk[0] = {**risk[0], "qty": "7", "lastTradeSequenceNumber": 40}
    elif raced == "closed between the reads":
        risk = []
    else:
        risk.append(make_position_risk(account_id=7, symbol="BTCRUSDPERP"))
    result = account_projection(7, balances, positions, summaries, risk)
    # The account's figures are of the same fill as the position's, so they are left out with it.
    assert result["estimated_equity_usd"] is None and result["account_summary"] is None
    assert "do not match" in result["valuation_unavailable_reason"]
    (position,) = result["positions"]
    assert position["qty"] == "8" and position["position_risk"] is None and position["mark_price"] is None


def test_state_of_an_account_the_api_does_not_value_is_null_not_zero():
    balances, positions, summaries, risk = account_inputs()
    result = account_projection(7, balances, positions, [make_account_summary(account_id=8)], [])
    assert (
        result["estimated_equity_usd"] is None
        and "no valuation for account 7" in result["valuation_unavailable_reason"]
    )


async def test_data_state_reads_the_valuation_for_the_selected_account(monkeypatch):
    wallet = "0x" + "ab" * 20
    balances, positions, summaries, risk = account_inputs()
    served = {
        f"/wallet/{wallet}/accounts": [{"accountId": 7}],
        f"/wallet/{wallet}/accountBalances": balances,
        f"/wallet/{wallet}/positions": positions,
        f"/wallet/{wallet}/accountSummaries": summaries,
        f"/wallet/{wallet}/positionRisk": risk,
    }
    seen = []

    async def get(self, path, **params):
        seen.append((path, params))
        return served[path]

    monkeypatch.setattr(V2Client, "get", get)
    monkeypatch.setattr(
        data,
        "get_trading_config",
        lambda include_private_key: SimpleNamespace(owner_wallet_address=wallet, account_id=7),
    )
    result = await data.run(data.build_parser().parse_args(["state"]))
    assert result["data"]["estimated_equity_usd"] == "121.000001"
    assert (f"/wallet/{wallet}/accountSummaries", {"accountId": 7}) in seen
    assert (f"/wallet/{wallet}/positionRisk", {"accountId": 7}) in seen
    assert not any(path in ("/perpMarkets/summary", "/assetDefinitions", "/assetOraclePrices") for path, _ in seen)

    async def unavailable(self, path, **params):
        if path.endswith(("/accountSummaries", "/positionRisk")):
            raise V2StatusError(path, 503, "SERVICE_UNAVAILABLE_ERROR")
        return served[path]

    monkeypatch.setattr(V2Client, "get", unavailable)
    result = await data.run(data.build_parser().parse_args(["state"]))
    assert result["data"]["estimated_equity_usd"] is None and result["data"]["positions"][0]["qty"] == "8"
    assert "HTTP 503" in result["data"]["valuation_unavailable_reason"]


@pytest.mark.parametrize("scope", [["--account-id", "7"], ["--whole-wallet"]])
async def test_history_current_state_values_each_account_in_scope_on_its_own(monkeypatch, scope):
    wallet = "0x" + "ab" * 20
    balances, positions, summaries, risk = account_inputs()
    balances = balances + [
        {"accountId": 9, "asset": "RUSD", "realBalance": "5"},
        {"accountId": 8, "asset": "RUSD", "realBalance": "1"},
    ]
    served = {
        "accounts": [{"accountId": 7}, {"accountId": 8}, {"accountId": 9}],  # 9 is a spot account
        "accountBalances": balances,
        "positions": positions,
    }
    seen = []

    async def get(self, path, **params):
        feed = path.rsplit("/", 1)[-1]
        seen.append((feed, params.get("accountId")))
        if feed == "accountSummaries" and params["accountId"] == 8:
            raise V2StatusError(path, 503, "SERVICE_UNAVAILABLE_ERROR")
        if feed in ("accountSummaries", "positionRisk"):
            # The API values perp accounts only: a spot account's read is empty, as is another account's.
            return {"accountSummaries": summaries, "positionRisk": risk}[feed] if params["accountId"] == 7 else []
        return served[feed]

    monkeypatch.setattr(V2Client, "get", get)
    monkeypatch.setattr(V2Client, "history", AsyncMock(return_value={"data": [], "coverage": {"state": "bounded"}}))
    args = ["--wallet", wallet, *scope, "--start-time", "0", "--end-time", "1000", "--include-current-state"]
    states = (await history.run(history.build_parser().parse_args(args)))["data"]["current_account_state"]
    by_account = {state["account_id"]: state for state in states}
    assert sorted(by_account) == ([7] if scope[0] == "--account-id" else [7, 8, 9])
    assert by_account[7]["estimated_equity_usd"] == "121.000001"
    assert by_account[7]["positions"][0]["position_risk"]["liquidationPrice"] == "88.1"
    assert "valuation_unavailable_reason" not in by_account[7]
    if scope[0] == "--whole-wallet":
        # Each account is read on its own, so the one the API cannot value leaves the others' figures in place.
        assert (
            by_account[8]["estimated_equity_usd"] is None
            and "HTTP 503" in by_account[8]["valuation_unavailable_reason"]
        )
        assert by_account[9]["estimated_equity_usd"] is None
        assert "no valuation for account 9" in by_account[9]["valuation_unavailable_reason"]
        assert by_account[9]["balances"][0]["realBalance"] == "5"
    assert {account for feed, account in seen if feed == "positionRisk"} == set(by_account)
    assert all(account is not None for feed, account in seen if feed in ("accountSummaries", "positionRisk"))


def test_error_bodies_give_their_code_and_never_their_text():
    assert error_code(b'{"error":"NO_ACCOUNTS_FOUND_ERROR","message":"No accounts found"}') == "NO_ACCOUNTS_FOUND_ERROR"
    assert error_code(b'{"error":"not a code"}') is None
    assert error_code(b"<html>bad gateway</html>") is None
    assert "NO_ACCOUNTS_FOUND_ERROR" in str(V2StatusError("/wallet/x/accountSummaries", 400, "NO_ACCOUNTS_FOUND_ERROR"))


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


@pytest.mark.parametrize(
    "answer",
    [
        [{"symbol": "HYPERUSDPERP", "fundingRate": "-0.000082033333333333"}, {"symbol": "XRUSDPERP"}],
        {"symbol": "HYPERUSDPERP", "fundingRate": "-0.000082033333333333"},
    ],
)
async def test_market_summary_keeps_the_exact_funding_and_adds_text_to_quote(monkeypatch, answer):
    monkeypatch.setattr(V2Client, "get", AsyncMock(return_value=answer))
    args = data.build_parser().parse_args(["market", "summary"])
    result = await data.run(args)
    rows = result["data"] if isinstance(result["data"], list) else [result["data"]]
    assert rows[0]["fundingRate"] == "-0.000082033333333333"
    assert rows[0]["funding_text"] == "-0.0001%/h (-0.72% a year)"
    assert all("funding_text" not in row for row in rows[1:])  # no rate, no text


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
