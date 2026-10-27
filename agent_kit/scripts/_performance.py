"""Explain observed source records without deriving profit from cash movements."""

from decimal import Decimal, localcontext

from agent_kit.scripts._executions import net_fee
from agent_kit.scripts._v2 import decimal, integer, rows


def distinct(records, feed):
    """Feed sequence namespaces are independent; conflicting duplicates fail closed."""
    found: dict[int, dict] = {}
    unidentified = []
    for row in rows(records):
        key = integer(row.get("sequenceNumber"))
        if feed == "perpExecutions" and key == 0:
            unidentified.append(row)
            continue
        if key in found and found[key] != row:
            raise ValueError(f"Conflicting {feed} record")
        found[key] = row
    return [found[key] for key in sorted(found)] + unidentified


def metric(records, field):
    values = [decimal(r[field], field) for r in records if r.get(field) is not None]
    uncertain_identity = any(r.get("identity") is None for r in records)
    return {
        "observed_sum": str(sum(values, Decimal(0))) if values and not uncertain_identity else None,
        "source_records": len(records),
        "records_with_value": len(values),
        "state": (
            "unavailable_identity_uncertain"
            if uncertain_identity
            else (
                "available_for_observed_records"
                if records and len(values) == len(records)
                else "unavailable_or_partial"
            )
        ),
        "period_total_verified": False,
    }


def _downgrade(coverage: dict, reason: str, **evidence) -> None:
    coverage.update(evidence)
    if coverage.get("state") == "bounded":
        coverage.update(state="incomplete", reason=reason)


def _cross_checked_coverage(pages, filtered, account_ids, executions, transfers, orders) -> dict:
    """Exhausting a feed is not completeness: other feeds can prove served history is missing."""
    coverage = {feed: dict(page["coverage"]) for feed, page in pages.items()}
    # Every scoped fill belongs to one of the account's own orders; order IDs are per market.
    fill_orders = {
        (row["symbol"], str(row[role + "OrderId"]))
        for row in filtered.get("perpExecutions", [])
        for role in ("taker", "maker")
        if row.get(role + "AccountId") in account_ids and row.get(role + "OrderId") is not None
    }
    missing_orders = fill_orders - {(r.get("symbol"), str(r.get("orderId"))) for r in orders}
    if missing_orders and "orderHistory" in coverage:
        _downgrade(
            coverage["orderHistory"],
            "orders_missing_for_fills",
            orders_missing_for_fills=len(missing_orders),
            missing_orders_note="Orders last updated after end_time are excluded by the time filter",
        )
    # A taker fee posts a ledger row at the fill's timestamp, so a fee-paying fill older
    # than the account's earliest served transfer proves the ledger is missing rows.
    earliest: dict[int, int] = {}
    for row in transfers:
        account, stamp = row["accountId"], integer(row.get("timestamp"), "timestamp")
        earliest[account] = min(earliest.get(account, stamp), stamp)
    unmatched = [
        e
        for e in executions
        if e["role"] == "taker"
        and e.get("fee") is not None
        and decimal(e["fee"], "fee") > 0
        and (e["account_id"] not in earliest or integer(e["timestamp"], "timestamp") < earliest[e["account_id"]])
    ]
    if unmatched and "transfers" in coverage:
        _downgrade(coverage["transfers"], "fee_transfers_missing_for_fills", fills_without_fee_transfers=len(unmatched))
    return coverage


def performance(pages, account_ids, *, start_time, end_time, symbol=None):
    with localcontext() as ctx:
        ctx.prec = 512
        return _performance(pages, set(account_ids), start_time, end_time, symbol)


def _performance(pages, account_ids, start_time, end_time, symbol):
    filtered = {}
    for feed, page in pages.items():
        filtered[feed] = [
            r
            for r in distinct(page["data"], feed)
            if start_time <= integer(r.get("timestamp", r.get("lastUpdateAt")), "timestamp") <= end_time
            and (symbol is None or r.get("symbol") == symbol)
        ]
    executions = []
    for row in filtered.get("perpExecutions", []):
        for role in ("taker", "maker"):
            if row.get(role + "AccountId") not in account_ids:
                continue
            executions.append(
                {
                    "account_id": row[role + "AccountId"],
                    "role": role,
                    "symbol": row["symbol"],
                    "identity": (
                        [
                            row.get("exchangeId"),
                            row["symbol"],
                            row.get("fillId") or row["sequenceNumber"],
                            role,
                        ]
                        if row["sequenceNumber"] != 0
                        else None
                    ),
                    "sequence_number": row["sequenceNumber"],
                    "type": row.get("type"),
                    "qty": row.get("qty"),
                    "price": row.get("price"),
                    "timestamp": row.get("timestamp"),
                    "side": (row.get("side") if role == "taker" else {"B": "A", "A": "B"}.get(row.get("side"))),
                    "taker_side": row.get("side"),
                    **{
                        name: row.get(role + source)
                        for name, source in (
                            ("fee", "Fee"),
                            ("opening_fee", "OpeningFee"),
                            ("reported_realized_pnl", "RealizedPnl"),
                            ("price_variation_pnl", "PriceVariationPnl"),
                            ("funding_pnl", "FundingPnl"),
                        )
                    },
                    "rebate_credit": row.get("takerRebateCredit") if role == "taker" else None,
                    "net_fee": net_fee(
                        row.get(role + "Fee"), row.get("takerRebateCredit") if role == "taker" else None, role
                    ),
                }
            )
    stable_fills: dict[tuple, dict] = {}
    unidentified_executions = []
    for execution in executions:
        if execution["identity"] is None:
            unidentified_executions.append(execution)
            continue
        key = tuple(execution["identity"])
        if key in stable_fills:
            previous = {k: v for k, v in stable_fills[key].items() if k != "sequence_number"}
            current = {k: v for k, v in execution.items() if k != "sequence_number"}
            if previous != current:
                raise ValueError("Conflicting scoped fill identity")
        else:
            stable_fills[key] = execution
    executions = list(stable_fills.values()) + unidentified_executions
    transfers = [r for r in filtered.get("transfers", []) if r.get("accountId") in account_ids]
    groups: dict[tuple, Decimal] = {}
    internal: dict[tuple, list[dict]] = {}
    for row in transfers:
        integer(row["accountId"], "accountId")
        if row.get("counterpartyAccountId") is not None:
            integer(row["counterpartyAccountId"], "counterpartyAccountId")
        asset, kind = row["asset"], row["type"]
        value = decimal(row.get("amount"), "transfer amount")
        key = (asset, kind)
        groups[key] = groups.get(key, Decimal(0)) + value
        if kind == "TRANSFER" and row.get("counterpartyAccountId") in account_ids:
            # Sequence numbers are opaque. Equal transfers in the same transaction
            # cannot be attributed to individual pairs without an explicit API leg ID.
            accounts = tuple(sorted((row["accountId"], row["counterpartyAccountId"])))
            internal.setdefault((row.get("transactionHash"), accounts, asset, abs(value)), []).append(row)
    internal_legs = []
    for (tx_hash, accounts, asset, amount), entries in internal.items():
        balanced = (
            len(entries) == 2
            and accounts[0] != accounts[1]
            and amount > 0
            and entries[0].get("counterpartyAccountId") == entries[1]["accountId"]
            and entries[1].get("counterpartyAccountId") == entries[0]["accountId"]
            and isinstance(entries[0].get("transactionHash"), str)
            and bool(entries[0]["transactionHash"])
            and entries[0]["transactionHash"] == entries[1].get("transactionHash")
            and sum((decimal(r["amount"], "amount") for r in entries), Decimal(0)) == 0
        )
        internal_legs.append(
            {
                "transaction_hash": tx_hash,
                "asset": asset,
                "state": "balanced_observations" if balanced else "incomplete_or_ambiguous",
                "leg_identity_verified": False,
                "entries": entries,
                "observed_net_wallet_cashflow": "0" if balanced else None,
                "net_wallet_cashflow": None,
            }
        )
    cashflows: dict[str, Decimal] = {}
    for row in transfers:
        if row["type"] in ("DEPOSIT", "WITHDRAWAL") or (
            row["type"] == "TRANSFER"
            and row.get("counterpartyAccountId") is not None
            and row["counterpartyAccountId"] not in account_ids
        ):
            cashflows[row["asset"]] = cashflows.get(row["asset"], Decimal(0)) + decimal(row["amount"], "amount")
    busts = [
        r
        for r in filtered.get("executionBusts", [])
        if r.get("takerAccountId") in account_ids or r.get("makerAccountId") in account_ids
    ]
    orders = [r for r in filtered.get("orderHistory", []) if r.get("accountId") in account_ids]
    coverage = _cross_checked_coverage(pages, filtered, account_ids, executions, transfers, orders)
    transfer_state = coverage.get("transfers", {}).get("state")
    totals_state = (
        "partial_symbol_filter"
        if symbol
        else {"bounded": "available_for_observed_records", "incomplete": "incomplete"}.get(
            transfer_state, "unavailable"
        )
    )
    return {
        "interval": {"start_time": start_time, "end_time": end_time, "inclusive": True},
        "account_ids": sorted(account_ids),
        "symbol_filter": symbol,
        "coverage": coverage,
        "provenance": "supported_V2_server_records_observed_now",
        "execution_metrics_rusd": {
            field: metric(executions, field)
            for field in (
                "fee",
                "opening_fee",
                "rebate_credit",
                "net_fee",
                "reported_realized_pnl",
                "price_variation_pnl",
                "funding_pnl",
            )
        },
        "execution_records": executions,
        "adl_execution_records": [r for r in executions if r["type"] == "ADL"],
        # Closes the protocol made when a market was delisted or wound down: not liquidations, not the account's orders.
        "market_close_execution_records": [r for r in executions if r["type"] == "MARKET_CLOSE"],
        "execution_busts": busts,
        "transfers_by_asset_and_type": [
            {"asset": a, "type": t, "signed_amount": str(v)} for (a, t), v in sorted(groups.items())
        ],
        "observed_external_cashflows_by_asset": {a: str(v) for a, v in cashflows.items()},
        # Applies to both transfer totals: an empty or partial ledger must not read as zero.
        "cashflow_totals_state": totals_state,
        "internal_transfer_legs": internal_legs,
        "transfer_records": transfers,
        "order_records": orders,
        "equity_time_series": None,
        "unrealized_pnl_time_series": None,
        "net_trading_return": None,
        "limitations": [
            "Available server history can be incomplete; no metric is a verified full-period total.",
            "Legacy zero-sequence executions remain raw observations; unknown overlap identity suppresses execution sums.",
            "Transfer IDs are opaque. Reciprocal observations may balance, but capped history cannot prove individual leg pairing.",
            "Execution PnL components and transfer ledger amounts are separate views, never added together.",
            "Opening fees and rebates are reported separately; net_fee is the fee minus the taker's instant rebate "
            "(a maker fee is already net). No inferred fee-adjusted return.",
            "ADL records do not establish the full effect of unsettled tracker adjustments.",
            "MARKET_CLOSE records are closes made when a market was delisted or wound down, not liquidations.",
            "Busts describe rejected settlement, not settled PnL; they are not subtracted from settled fills.",
            "No supported equity/PnL history endpoint integrated; no pre-installation or interpolated series fabricated.",
            "A symbol filter excludes transfers without that symbol and cannot produce full account cashflow totals.",
        ],
    }
