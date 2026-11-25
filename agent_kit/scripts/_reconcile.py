"""Exact read-only account projections; never an order-admission oracle."""

from decimal import Decimal, localcontext

from agent_kit.scripts import _valuation as valuation
from agent_kit.scripts._v2 import decimal, integer, rows


async def current_state(v2, wallet: str, account_id: int, balances, positions) -> dict:
    """`data state` and `history --include-current-state`: one account's records beside the API's valuation of it,
    read for that account alone, so an account the API cannot value leaves the others' figures in place."""
    try:
        summaries, risk = await valuation.read(v2, wallet, account_id)
    except valuation.ValuationUnavailable as exc:
        return account_projection(account_id, balances, positions, unavailable=exc.reason)
    return account_projection(account_id, balances, positions, summaries, risk)


def account_projection(
    account_id: int, balances, positions, summaries=None, risk=None, *, unavailable: str | None = None
) -> dict:
    """The account's balances and positions as V2 records them, beside the API's own valuation of it
    (`summaries` from accountSummaries, `risk` from positionRisk). The kit values nothing itself; without a
    valuation of exactly these positions (`unavailable` is the reason when the read failed) the figures are null
    and `valuation_unavailable_reason` says why."""
    balance_view = []
    records = []
    seen_balances = set()
    seen_positions = set()
    for row in rows(balances):
        if integer(row.get("accountId"), "accountId") != account_id:
            continue
        asset = row["asset"]
        if asset in seen_balances:
            raise ValueError("Duplicate account/asset balance")
        seen_balances.add(asset)
        decimal(row.get("realBalance"), "realBalance")
        balance_view.append({**row, "adjustments_applied_by_kit": False})
    for row in rows(positions):
        if integer(row.get("accountId"), "accountId") != account_id:
            continue
        position_key = (row["exchangeId"], row["symbol"])
        if position_key in seen_positions:
            raise ValueError("Duplicate account/exchange/market position")
        seen_positions.add(position_key)
        quantity = decimal(row.get("qty"), "position qty")
        if quantity < 0 or row.get("side") not in ("A", "B"):
            raise ValueError("Invalid public position quantity or side")
        records.append(row)
    summary: dict | None = None
    values: dict[str, dict] = {}
    reason = unavailable
    if reason is None:
        try:
            summary, values = valuation.for_account(summaries, risk, account_id)
            keys = [(r["symbol"], r["side"], r["qty"], r.get("lastTradeSequenceNumber")) for r in records]
            reason = valuation.mismatch(values, keys)
        except valuation.ValuationUnavailable as exc:
            reason = exc.reason
    if reason:
        summary, values = None, {}
    position_view = []
    for row in records:
        value = values.get(row["symbol"])
        position_view.append(
            {
                **row,
                "estimated_marked_pnl_usd": value["unrealizedPnl"] if value else None,
                "mark_price": value["markPrice"] if value else None,
                "prices_updated_at": value["pricesUpdatedAt"] if value else None,
                "position_risk": value,
            }
        )
    return {
        "account_id": account_id,
        "balances": balance_view,
        "positions": position_view,
        "account_summary": summary,
        "effective_collateral_usd": summary["collateralValue"] if summary else None,
        "estimated_marked_pnl_usd": summary["unrealizedPnl"] if summary else None,
        "estimated_equity_usd": summary.get("equity") if summary else None,
        "margin_balance_usd": summary["marginBalance"] if summary else None,
        **({"valuation_unavailable_reason": reason} if reason else {}),
        "safe_for_order_admission": False,
        "state": "incomplete",
        "limitations": [
            "The figures are the API's own valuation (accountSummaries, positionRisk); the kit adds no figure of its "
            "own and no funding/ADL adjustment.",
            "Separate REST reads are not an atomic snapshot: lastTradeSequenceNumber ties the valuation to the "
            "position records, and a valuation of another fill is left out.",
            "Settled positions only: matched fills that have not settled yet are not valued.",
            "Not an order-admission check: the settlement reserve and in-flight fills are not included.",
        ],
    }


def order_reconciliation(account_id: int, orders, executions, busts) -> list[dict]:
    with localcontext() as context:
        context.prec = 512
        return _order_reconciliation(account_id, orders, executions, busts)


def _order_reconciliation(account_id: int, orders, executions, busts) -> list[dict]:
    """Join account + symbol + order ID; execution and bust sequence spaces differ."""
    latest: dict[tuple, dict] = {}
    for order in rows(orders):
        if integer(order.get("accountId"), "accountId") != account_id:
            continue
        key = (order["symbol"], order["orderId"])
        previous = latest.get(key)
        rank = (order.get("lastUpdateAt", -1), order.get("sequenceNumber", -1))
        if previous is None or rank > (previous.get("lastUpdateAt", -1), previous.get("sequenceNumber", -1)):
            latest[key] = order
    evidence: dict[tuple, dict] = {}
    for kind, source in (("settled", executions), ("busted", busts)):
        seen: dict[int, dict] = {}
        for row in rows(source):
            seq = integer(row.get("sequenceNumber"))
            unidentified = kind == "settled" and seq == 0
            if not unidentified and seq in seen:
                if seen[seq] != row:
                    raise ValueError("Conflicting settlement evidence")
                continue
            if not unidentified:
                seen[seq] = row
            for role in ("taker", "maker"):
                if row.get(role + "AccountId") != account_id or not row.get(role + "OrderId"):
                    continue
                key = (row["symbol"], row[role + "OrderId"])
                value = evidence.setdefault(key, {"settled": Decimal(0), "busted": Decimal(0), "unidentified": []})
                quantity = decimal(row.get("qty"), "execution qty")
                if quantity < 0:
                    raise ValueError("Execution quantity cannot be negative")
                if unidentified:
                    value["unidentified"].append(row)
                else:
                    value[kind] += quantity
    output = []
    for key in sorted(set(latest) | set(evidence)):
        matched_order = latest.get(key)
        values = evidence.get(key, {"settled": Decimal(0), "busted": Decimal(0)})
        cumulative = decimal(matched_order["cumQty"], "cumQty") if matched_order and "cumQty" in matched_order else None
        unresolved = (
            max(Decimal(0), cumulative - values["settled"] - values["busted"]) if cumulative is not None else None
        )
        contradictory = cumulative is not None and values["settled"] + values["busted"] > cumulative
        unidentified_rows = values.get("unidentified", [])
        output.append(
            {
                "symbol": key[0],
                "order_id": key[1],
                "order": matched_order,
                "settled_qty": None if unidentified_rows else str(values["settled"]),
                "unidentified_execution_observations": unidentified_rows,
                "busted_qty": str(values["busted"]),
                "unresolved_executed_qty": (
                    str(unresolved) if unresolved is not None and not unidentified_rows else None
                ),
                "outcome": (
                    "unknown_legacy_identity"
                    if unidentified_rows
                    else (
                        "inconsistent_or_concurrent"
                        if contradictory
                        else (
                            "unknown"
                            if cumulative is None
                            else ("settling_or_missing_history" if unresolved else "observed_evidence")
                        )
                    )
                ),
                "complete_history": False,
            }
        )
    return output
