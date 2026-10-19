"""Conservative read-only reconciliation of this session's acknowledged orders.

A matched order is not necessarily settled. A clean position read cannot replace
positive per-fill settlement/bust evidence and a flat net settled quantity.
"""

import asyncio
from decimal import Decimal, localcontext

from agent_kit.scripts._v2 import decimal, integer, rows


def reconcile_acknowledgements(acknowledgements, pages, account_id, exchange_id):
    with localcontext() as context:
        context.prec = 512
        return _reconcile(acknowledgements, pages, account_id, exchange_id)


def _reconcile(acknowledgements, pages, account_id, exchange_id):
    orders: dict[tuple, dict] = {}
    for ack in acknowledgements:
        key = (ack["symbol"], ack["order_id"])
        if not key[1]:
            raise ValueError("Acknowledged order has no durable venue identity")
        previous = orders.get(key)
        if previous and previous["side"] != ack["side"]:
            raise ValueError("Acknowledged order direction changed")
        orders[key] = ack
    evidence = []
    net: dict[str, Decimal] = {}
    for (symbol, order_id), ack in orders.items():
        expected = (
            decimal(ack["filled_qty"], "acknowledged filled quantity") if ack.get("filled_qty") is not None else None
        )
        # An OPEN acknowledgement may fill after it was returned. Cancellation
        # does not supply its final cumulative quantity: require terminal history.
        if ack["status"] not in ("FILLED", "CANCELLED") or expected is None:
            history = [
                r
                for r in rows(pages["orderHistory"])
                if r.get("accountId") == account_id
                and r.get("exchangeId") == exchange_id
                and r.get("symbol") == symbol
                and r.get("orderId") == order_id
            ]
            if not history:
                raise ValueError(f"Terminal order history unavailable for {symbol}/{order_id}")
            versions: dict[int, dict] = {}
            for row in history:
                seq = integer(row.get("sequenceNumber"))
                if seq == 0 or (seq in versions and versions[seq] != row):
                    raise ValueError("Order history identity is unavailable or conflicting")
                versions[seq] = row
            latest = versions[max(versions)]
            if latest.get("status") not in ("FILLED", "CANCELLED"):
                raise ValueError(f"Order is not terminal in history: {symbol}/{order_id}")
            final_qty = decimal(latest.get("cumQty"), "terminal filled quantity")
            if expected is not None and final_qty < expected:
                raise ValueError("Terminal history is behind the acknowledgement")
            expected = final_qty
        if expected < 0:
            raise ValueError("Invalid acknowledged fill quantity")
        fills: dict[str, tuple] = {}
        sequences: dict[tuple, dict] = {}
        for feed in ("perpExecutions", "executionBusts"):
            for row in rows(pages[feed]):
                if row.get("exchangeId") != exchange_id or row.get("symbol") != symbol:
                    continue
                for role in ("taker", "maker"):
                    if row.get(role + "AccountId") != account_id or row.get(role + "OrderId") != order_id:
                        continue
                    fill_id = row.get("fillId")
                    sequence = integer(row.get("sequenceNumber"))
                    if not isinstance(fill_id, str) or not fill_id or sequence == 0:
                        raise ValueError("Fill identity unavailable; operator reconciliation required")
                    identity = (feed, sequence)
                    if identity in sequences and sequences[identity] != row:
                        raise ValueError("Conflicting settlement event identity")
                    sequences[identity] = row
                    side = row.get("side") if role == "taker" else {"B": "A", "A": "B"}.get(row.get("side") or "")
                    qty = decimal(row.get("qty"), "fill quantity")
                    if side != ack["side"] or qty <= 0:
                        raise ValueError("Fill direction/quantity differs from acknowledged order")
                    value = (feed, qty, side)
                    if fill_id in fills and fills[fill_id] != value:
                        raise ValueError("Conflicting settlement/bust evidence")
                    fills[fill_id] = value
        accounted = sum((v[1] for v in fills.values()), Decimal(0))
        if accounted != expected or (ack.get("first_fill_id") and ack["first_fill_id"] not in fills):
            raise ValueError(f"Settlement/bust evidence incomplete for {symbol}/{order_id}")
        settled = sum((v[1] for v in fills.values() if v[0] == "perpExecutions"), Decimal(0))
        net[symbol] = net.get(symbol, Decimal(0)) + settled * (1 if ack["side"] == "B" else -1)
        evidence.append(
            {
                "symbol": symbol,
                "order_id": order_id,
                "matched_qty": str(expected),
                "settled_qty": str(settled),
                "busted_qty": str(accounted - settled),
                "fill_ids": list(fills),
            }
        )
    return {
        "state": "settled_flat" if all(v == 0 for v in net.values()) else "settled_exposure",
        "orders": evidence,
        "net_settled_qty": {k: str(v) for k, v in net.items()},
        "scope": "session_orders_from_initially_flat_exclusively_used_account",
    }


async def read_settlement(client, acknowledgements):
    if not acknowledgements:
        return {"state": "settled_flat", "orders": [], "net_settled_qty": {}}
    # Three bounded snapshots, never a write/retry. A capped page is sufficient
    # only when every acknowledged order/fill positively reconciles within it.
    result = {"state": "unresolved", "reason": "Supported settlement evidence unavailable"}
    for attempt in range(3):
        try:
            feeds = ("orderHistory", "perpExecutions", "executionBusts")
            responses = await asyncio.gather(
                *(
                    getattr(client.wallet, method)(address=client.config.owner_wallet_address)
                    for method in (
                        "get_wallet_order_history",
                        "get_wallet_perp_executions",
                        "get_wallet_execution_busts",
                    )
                )
            )
            pages = {feed: [r.to_dict() for r in response.data] for feed, response in zip(feeds, responses)}
            result = reconcile_acknowledgements(
                acknowledgements, pages, client.config.account_id, client.config.dex_id_override
            )
            if result["state"] == "settled_flat":
                return result
        except ValueError as exc:
            result = {"state": "unresolved", "reason": str(exc)}
        except Exception:
            result = {"state": "unresolved", "reason": "Supported settlement reads failed; no submission repeated"}
        if attempt < 2:
            await asyncio.sleep(0.5)
    return result
