"""Account-period accounting preserves record provenance and incomplete coverage."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from agent_kit.scripts import history
from agent_kit.scripts._performance import performance
from agent_kit.scripts._v2 import HISTORY_FEEDS, V2Client


def pages(**records):
    return {
        feed: {"data": records.get(feed, []), "coverage": {"state": "bounded", "retention_complete": False}}
        for feed in HISTORY_FEEDS
    }


def fill(**kw):
    return {
        "sequenceNumber": 1,
        "exchangeId": 2,
        "fillId": "100",
        "timestamp": 500,
        "symbol": "ETHRUSDPERP",
        "type": "TRADE",
        "takerAccountId": 7,
        "makerAccountId": 8,
        "takerFee": "2",
        "makerFee": "0",
        "takerRealizedPnl": "22",  # price variation + funding; the API excludes fees
        "takerPriceVariationPnl": "25",
        "takerFundingPnl": "-3",
        "takerOpeningFee": "1",
        "takerRebateCredit": ".1",
        **kw,
    }


def transfer(seq, account, amount, kind="TRANSFER", **kw):
    return {
        "sequenceNumber": seq,
        "accountId": account,
        "amount": amount,
        "type": kind,
        "asset": "RUSD",
        "timestamp": 500,
        "transactionHash": "0xabc",
        **kw,
    }


def report(data, accounts=(7,), **kw):
    return performance(data, accounts, start_time=0, end_time=1000, **kw)


def test_scoped_fill_ids_overlap_funding_fees_and_busts_are_separate():
    f = fill()
    other = fill(sequenceNumber=2, symbol="BTCRUSDPERP")
    overlap = fill(sequenceNumber=3)
    result = report(pages(perpExecutions=[f, f, other, overlap], executionBusts=[f, f]))
    metrics = result["execution_metrics_rusd"]
    assert Decimal(metrics["fee"]["observed_sum"]) == 4
    assert Decimal(metrics["funding_pnl"]["observed_sum"]) == -6
    assert Decimal(metrics["price_variation_pnl"]["observed_sum"]) == 50
    assert Decimal(metrics["reported_realized_pnl"]["observed_sum"]) == 44
    assert len(result["execution_records"]) == 2
    assert len(result["execution_busts"]) == 1  # independent namespace, no subtraction
    assert result["net_trading_return"] is None


def test_optional_legacy_maker_pnl_and_empty_history_are_not_zero():
    result = report(pages(perpExecutions=[fill()]), accounts=(8,))
    metric = result["execution_metrics_rusd"]["reported_realized_pnl"]
    assert metric["observed_sum"] is None and metric["state"] == "unavailable_or_partial"
    assert report(pages())["execution_metrics_rusd"]["fee"]["observed_sum"] is None
    assert result["equity_time_series"] is None


def test_missing_some_values_preserves_partial_sum_and_count():
    result = report(pages(perpExecutions=[fill(), fill(sequenceNumber=2, fillId="101", takerFundingPnl=None)]))
    metric = result["execution_metrics_rusd"]["funding_pnl"]
    assert metric["observed_sum"] == "-3"
    assert metric["records_with_value"] == 1 and metric["source_records"] == 2
    assert metric["state"] == "unavailable_or_partial"


def test_internal_transfer_two_sides_once_and_repeated_equal_amounts_distinct():
    entries = [
        transfer(20, 7, "-10", counterpartyAccountId=8),
        transfer(21, 8, "10", counterpartyAccountId=7),
        transfer(822, 7, "-10", counterpartyAccountId=8, transactionHash="0xdef"),
        transfer(143, 8, "10", counterpartyAccountId=7, transactionHash="0xdef"),
        transfer(24, 7, "100", "DEPOSIT"),
        transfer(26, 7, "-5", "WITHDRAWAL"),
    ]
    wallet = report(pages(transfers=entries + entries), accounts=(7, 8))
    assert len(wallet["internal_transfer_legs"]) == 2
    assert all(r["state"] == "balanced_observations" for r in wallet["internal_transfer_legs"])
    assert wallet["observed_external_cashflows_by_asset"] == {"RUSD": "95"}
    account = report(pages(transfers=entries))
    assert account["observed_external_cashflows_by_asset"] == {"RUSD": "75"}
    assert account["net_trading_return"] is None


def test_incomplete_transfer_pair_is_never_reported_as_balanced():
    result = report(pages(transfers=[transfer(20, 7, "-10", counterpartyAccountId=8)]), accounts=(7, 8))
    leg = result["internal_transfer_legs"][0]
    assert leg["state"] == "incomplete_or_ambiguous" and leg["net_wallet_cashflow"] is None


def test_unknown_counterparty_and_other_cashflows_do_not_become_deposits():
    result = report(
        pages(transfers=[transfer(1, 7, "100"), transfer(2, 7, "-2", "PERP_TAKER_FEE"), transfer(3, 7, "25", "OTHER")])
    )
    assert not result["observed_external_cashflows_by_asset"]
    assert len(result["transfers_by_asset_and_type"]) == 3


def test_order_last_update_interval_and_symbol_filter():
    order = {"sequenceNumber": 1, "accountId": 7, "symbol": "ETHRUSDPERP", "createdAt": 1, "lastUpdateAt": 500}
    result = performance(
        pages(orderHistory=[order], perpExecutions=[fill(), fill(sequenceNumber=2, symbol="BTCRUSDPERP")]),
        [7],
        start_time=400,
        end_time=600,
        symbol="ETHRUSDPERP",
    )
    assert result["order_records"] == [order]
    assert len(result["execution_records"]) == 1


def test_conflicting_sequence_or_fill_identity_fails_closed():
    with pytest.raises(ValueError, match="Conflicting"):
        report(pages(perpExecutions=[fill(), fill(takerFee="9")]))
    with pytest.raises(ValueError, match="Conflicting scoped"):
        report(pages(perpExecutions=[fill(), fill(sequenceNumber=2, takerFee="9")]))


def test_adl_is_visible_without_claiming_full_tracker_pnl():
    result = report(pages(perpExecutions=[fill(type="ADL", makerAccountId=None)]))
    assert len(result["adl_execution_records"]) == 1
    assert result["execution_metrics_rusd"]["reported_realized_pnl"]["period_total_verified"] is False


async def test_report_recovers_available_feeds_but_labels_failed_endpoint(monkeypatch):
    monkeypatch.setattr(V2Client, "get", AsyncMock(return_value=[{"accountId": 7}]))

    async def get_history(path, **kwargs):
        if path.endswith("/transfers"):
            raise ValueError("unsupported environment")
        return pages(perpExecutions=[fill()])[path.split("/")[-1]]

    monkeypatch.setattr(V2Client, "history", get_history_wrapper := AsyncMock(side_effect=get_history))
    args = history.build_parser().parse_args(
        ["--wallet", "0x" + "1" * 40, "--account-id", "7", "--start-time", "0", "--end-time", "1000"]
    )
    result = await history.run(args)
    assert result["data"]["coverage"]["transfers"]["state"] == "unavailable"
    assert result["data"]["execution_metrics_rusd"]["fee"]["observed_sum"] == "2"
    assert get_history_wrapper.await_count == 4


async def test_not_owned_account_is_rejected_before_history_reads(monkeypatch):
    monkeypatch.setattr(V2Client, "get", AsyncMock(return_value=[{"accountId": 8}]))
    mock = AsyncMock()
    monkeypatch.setattr(V2Client, "history", mock)
    args = history.build_parser().parse_args(["--wallet", "0x" + "1" * 40, "--account-id", "7", "--start-time", "0"])
    with pytest.raises(ValueError, match="owned"):
        await history.run(args)
    mock.assert_not_called()


@pytest.mark.parametrize("error", [KeyError("bug"), TypeError("bug"), AttributeError("bug")])
async def test_programming_errors_are_not_hidden_as_unavailable_feed(monkeypatch, error):
    monkeypatch.setattr(V2Client, "get", AsyncMock(return_value=[{"accountId": 7}]))
    monkeypatch.setattr(V2Client, "history", AsyncMock(side_effect=error))
    args = history.build_parser().parse_args(["--wallet", "0x" + "1" * 40, "--account-id", "7", "--start-time", "0"])
    with pytest.raises(type(error), match="bug"):
        await history.run(args)


@pytest.mark.parametrize("error", [history.aiohttp.ClientError("connection lost"), TimeoutError()])
async def test_expected_transport_errors_remain_unavailable_feed(monkeypatch, error):
    monkeypatch.setattr(V2Client, "get", AsyncMock(return_value=[{"accountId": 7}]))
    monkeypatch.setattr(V2Client, "history", AsyncMock(side_effect=error))
    args = history.build_parser().parse_args(["--wallet", "0x" + "1" * 40, "--account-id", "7", "--start-time", "0"])
    result = await history.run(args)
    assert all(feed["state"] == "unavailable" for feed in result["data"]["coverage"].values())


def test_fills_without_their_orders_or_fee_transfers_downgrade_bounded_coverage():
    # Observed on a mainnet account: fills older than the API's served order history and fee transfers
    # were reported as bounded/available_history_exhausted.
    data = pages(
        perpExecutions=[fill(takerOrderId="11", makerOrderId="12", timestamp=400)],
        transfers=[transfer(5, 7, "-2", kind="PERP_TAKER_FEE", timestamp=600)],
    )
    coverage = report(data)["coverage"]
    assert coverage["orderHistory"]["state"] == "incomplete"
    assert coverage["orderHistory"]["reason"] == "orders_missing_for_fills"
    assert coverage["orderHistory"]["orders_missing_for_fills"] == 1
    assert coverage["transfers"]["state"] == "incomplete"
    assert coverage["transfers"]["reason"] == "fee_transfers_missing_for_fills"
    assert coverage["transfers"]["fills_without_fee_transfers"] == 1
    assert report(data)["cashflow_totals_state"] == "incomplete"


def test_matching_order_and_fee_transfer_keep_bounded_coverage():
    order = {"accountId": 7, "symbol": "ETHRUSDPERP", "orderId": "11", "sequenceNumber": 3, "lastUpdateAt": 500}
    data = pages(
        perpExecutions=[fill(takerOrderId="11", makerOrderId="12")],
        transfers=[transfer(5, 7, "-2", kind="PERP_TAKER_FEE", fillId="100")],
        orderHistory=[order],
    )
    result = report(data)
    assert result["coverage"]["orderHistory"]["state"] == "bounded"
    assert result["coverage"]["transfers"]["state"] == "bounded"
    assert result["cashflow_totals_state"] == "available_for_observed_records"


def test_cashflow_totals_carry_transfer_completeness():
    # Devnet 503: an incomplete transfer window returned {} cashflows that read as zero.
    data = pages()
    data["transfers"]["coverage"] = {"state": "incomplete", "reason": "page_budget"}
    result = report(data)
    assert result["observed_external_cashflows_by_asset"] == {}
    assert result["cashflow_totals_state"] == "incomplete"
    assert report(pages(), symbol="ETHRUSDPERP")["cashflow_totals_state"] == "partial_symbol_filter"
