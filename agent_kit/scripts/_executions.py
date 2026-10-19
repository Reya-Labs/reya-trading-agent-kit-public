"""Account-scoped settled executions, separate from order acknowledgements."""

import asyncio
import time
from decimal import Decimal


def account_executions(page, account_id: int, exchange_id: int) -> list[dict]:
    rows = []
    seen: dict[tuple, dict] = {}
    sequences: dict[int, dict] = {}
    for execution in page.data:
        legacy_unknown_exchange = execution.exchange_id == 0 and execution.sequence_number == 0
        if execution.exchange_id != exchange_id and not legacy_unknown_exchange:
            continue
        source = execution.to_dict()
        if execution.sequence_number != 0:
            if execution.sequence_number in sequences and sequences[execution.sequence_number] != source:
                raise ValueError("Conflicting execution sequence identity")
            sequences[execution.sequence_number] = source
        for role in ("taker", "maker"):
            if getattr(execution, f"{role}_account_id") != account_id:
                continue
            identity = (execution.symbol, execution.fill_id or execution.sequence_number, role)
            identity_verified = execution.sequence_number != 0
            if identity_verified and identity in seen:
                if seen[identity] != {k: v for k, v in source.items() if k != "sequenceNumber"}:
                    raise ValueError("Conflicting execution fill identity")
                continue
            if identity_verified:
                seen[identity] = {k: v for k, v in source.items() if k != "sequenceNumber"}
            rows.append(
                {
                    "fill_id": execution.fill_id,
                    "sequence_number": execution.sequence_number,
                    "identity_verified": identity_verified,
                    "exchange_id": execution.exchange_id,
                    "exchange_scope_verified": not legacy_unknown_exchange,
                    "order_id": getattr(execution, f"{role}_order_id"),
                    "symbol": execution.symbol,
                    "role": role,
                    "qty": execution.qty,
                    "price": execution.price,
                    "fee_rusd": getattr(execution, f"{role}_fee"),
                    "pnl_opening_fee_rusd": getattr(execution, f"{role}_opening_fee"),
                    "taker_rebate_credit_rusd": execution.taker_rebate_credit if role == "taker" else None,
                    "type": execution.type.value,
                    "timestamp": execution.timestamp,
                }
            )
    return rows


def execution_page(page) -> dict:
    """Bounds belong to the unfiltered wallet page, in descending time order."""
    return {
        **page.meta.to_dict(),
        "order": "newest_first",
        "newest_timestamp_ms": page.meta.start_time,
        "oldest_timestamp_ms": page.meta.end_time,
        "scope": "wallet_before_local_filters",
    }


async def add_settled_fees(client, result: dict, *, wait_s: float = 0) -> None:
    """Optionally poll reads within a total deadline; never repeat the trading write."""
    result.update(fees_status="pending", fees_rusd=None, executions=[])
    deadline = time.monotonic() + (wait_s or 2)
    while True:
        try:
            await asyncio.wait_for(_read_settled_fees(client, result), timeout=max(0, deadline - time.monotonic()))
        except TimeoutError:
            result.update(fees_status="unavailable", fees_rusd=None)
            return
        remaining = deadline - time.monotonic()
        if not wait_s or result["fees_status"] in ("complete", "not_filled") or remaining <= 0.5:
            return
        await asyncio.sleep(0.5)


async def _read_settled_fees(client, result: dict) -> None:
    """One bounded read after a write; failure must never hide the write outcome.

    The execution feed is capped and eventually consistent. Only report a fee
    total when account/order/symbol and filled quantity reconcile. Unknown fees
    remain null. Opening fees and rebate credits are kept separately as reported.
    """
    result.update(fees_status="pending", fees_rusd=None, executions=[])
    result.pop("settled_qty", None)
    try:
        filled_qty = Decimal(result["filled_qty"]) if result.get("filled_qty") is not None else None
        if filled_qty == 0:
            result["fees_status"] = "not_filled"
            return
        if filled_qty is None or not result.get("order_id"):
            return
        page = await asyncio.wait_for(
            client.wallet.get_wallet_perp_executions(address=client.config.owner_wallet_address), timeout=2.0
        )
        rows = [
            row
            for row in account_executions(page, client.config.account_id, client.config.dex_id_override)
            if row["order_id"] == result["order_id"] and row["symbol"] == result["symbol"]
        ]
        qty = sum((Decimal(row["qty"]) for row in rows), Decimal(0))
        count_matches = result.get("fill_count") is None or len(rows) == result["fill_count"]
        first_matches = result.get("first_fill_id") is None or any(
            row["fill_id"] == result["first_fill_id"] for row in rows
        )
        if not qty.is_finite() or qty < 0:
            raise ValueError("Invalid settled quantity")
        complete = (
            bool(rows)
            and all(row["identity_verified"] for row in rows)
            and qty == filled_qty
            and count_matches
            and first_matches
        )
        known_fees = all(row["fee_rusd"] is not None for row in rows)
        fee = sum((Decimal(row["fee_rusd"]) for row in rows), Decimal(0)) if known_fees else None
        if fee is not None and not fee.is_finite():
            raise ValueError("Invalid settled fee")
        result.update(
            executions=rows,
            settled_qty=format(qty, "f") if all(row["identity_verified"] for row in rows) else None,
            fees_status="complete" if complete and known_fees else ("partial" if rows else "pending"),
            fees_rusd=format(fee, "f") if complete and fee is not None else None,
        )
    except Exception:
        # Preserve IDs/status even if the read times out or its data is malformed.
        # Do not echo API exceptions here: query executions can be retried independently.
        result["fees_status"] = "unavailable"
