"""Exact read-only account projections; never an order-admission oracle."""

from decimal import Decimal, localcontext

from agent_kit.scripts._v2 import decimal, integer, rows


def account_projection(account_id: int, balances, positions, summaries, assets, asset_prices) -> dict:
    """Consume public derived balances/quantities once; do not reapply internal trackers."""
    with localcontext() as context:
        context.prec = 512
        return _account_projection(account_id, balances, positions, summaries, assets, asset_prices)


def _account_projection(account_id, balances, positions, summaries, assets, asset_prices):
    markets = {r["symbol"]: r for r in rows(summaries)}
    definitions = {r["asset"]: r for r in rows(assets)}
    prices = {r["asset"]: r for r in rows(asset_prices)}
    collateral = Decimal(0)
    pnl = Decimal(0)
    balance_view = []
    position_view = []
    seen_balances = set()
    seen_positions = set()
    for row in rows(balances):
        if integer(row.get("accountId"), "accountId") != account_id:
            continue
        asset = row["asset"]
        if asset in seen_balances:
            raise ValueError("Duplicate account/asset balance")
        seen_balances.add(asset)
        balance = decimal(row.get("realBalance"), "realBalance")
        haircut = decimal(definitions[asset].get("priceHaircut"), "priceHaircut")
        if not 0 <= haircut <= 1:
            raise ValueError("Collateral haircut is outside [0, 1]")
        price = Decimal(1) if asset == "RUSD" else decimal(prices[asset].get("oraclePrice"), "asset oracle")
        if price <= 0:
            raise ValueError("Collateral oracle must be positive")
        value = balance * price
        effective = value * (1 - haircut) if value >= 0 else value
        collateral += effective
        balance_view.append({**row, "effective_value_usd": str(effective), "adjustments_applied_by_kit": False})
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
        market = markets[row["symbol"]]
        mark = decimal(market.get("markPrice"), "markPrice")
        entry = decimal(row.get("avgEntryPrice"), "avgEntryPrice")
        if mark <= 0:
            raise ValueError("Mark price must be positive")
        marked = quantity * (mark - entry) * (1 if row["side"] == "B" else -1)
        pnl += marked
        position_view.append(
            {
                **row,
                "estimated_marked_pnl_usd": str(marked),
                "mark_price": str(mark),
                "prices_updated_at": market.get("pricesUpdatedAt"),
            }
        )
    return {
        "account_id": account_id,
        "balances": balance_view,
        "positions": position_view,
        "effective_collateral_usd": str(collateral),
        "estimated_marked_pnl_usd": str(pnl),
        "estimated_equity_usd": str(collateral + pnl),
        "safe_for_order_admission": False,
        "state": "incomplete",
        "limitations": [
            "Public realBalance and qty are already derived; no funding/ADL adjustment is applied again.",
            "Separate REST reads are not an atomic snapshot and carry no shared risk-engine watermark.",
            "Pending exposure, collateral conversion, engine fixed-point rounding and admission checks are not reproduced.",
            "Missing raw ADL/base-multiplier trackers prevent an independent exact risk-engine reconstruction.",
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
