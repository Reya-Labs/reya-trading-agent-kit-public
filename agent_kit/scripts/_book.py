"""Order-book context for an IOC: one bounded V2 depth read that never changes the order's outcome.

An IOC that fills nothing or only part comes back CANCELLED (`NO_LIQUIDITY` / `IOC_REMAINDER`), and the
obvious next move, resending at the same level, prices from a book that has not moved on. The `book`
block shows the touch against the order's limit and what the visible book held inside it, timestamped
by the venue, so the caller can wait for a newer book instead.
"""

import asyncio
import time
from decimal import Decimal

from agent_kit.scripts._common import exception_result
from agent_kit.scripts._paper import book_levels, clock_offset, clock_report
from agent_kit.scripts._v2 import V2Client, identifier, integer, rows

BOOK_LEVELS = 1000  # the most V2 serves per side; fewer could hide depth inside a wide limit
BOOK_READ_TIMEOUT_S = 2.0
# Cancel reasons that mean the book held too little inside the limit; others (self-trade prevention,
# risk) are about the account, so the resend advice does not apply.
LIQUIDITY_REASONS = (None, "NO_LIQUIDITY", "IOC_REMAINDER")


async def read_book(api_url: str | None, symbol: str) -> tuple[dict, dict | None]:
    """The raw depth snapshot and the clock estimate from its response (paper's `clock_offset`).

    `api_url` None reads the configured deployment."""
    async with V2Client(api_url) as client:
        book = await client.get(f"/market/{identifier(symbol)}/depth", limit=BOOK_LEVELS)
        return book, clock_offset(getattr(client, "server_date", None))


def _pct(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def book_context(
    book: dict,
    clock: dict | None,
    *,
    symbol: str,
    is_buy: bool,
    limit_px: Decimal,
    tick: Decimal,
    step: Decimal,
    now_ms: int,
) -> dict:
    """The touch against `limit_px` and the visible quantity an order on this side could take within it."""
    levels = book_levels(book, symbol, tick, step)
    updated_at = integer(book.get("updatedAt"), f"{symbol} order book updatedAt")
    take_side = "asks" if is_buy else "bids"
    taking = levels[take_side]

    def inside(px: Decimal) -> bool:
        return px <= limit_px if is_buy else px >= limit_px

    within = sum((amount for px, amount in taking if inside(px)), Decimal(0))
    # V2 serves at most BOOK_LEVELS per side: when every served level is inside the limit, more may sit beyond.
    complete = len(rows(book.get(take_side))) < BOOK_LEVELS or not taking or not inside(taking[-1][0])
    touch = taking[0][0] if taking else None
    # Positive: the best opposite price is past the limit, so nothing could fill.
    past = None if touch is None else (touch - limit_px if is_buy else limit_px - touch) / limit_px * 100
    offset = 0 if clock is None else clock["offset_ms"]
    context = {
        "updated_at_ms": updated_at,
        # By the server's clock when the response's Date header allows; this computer's otherwise.
        "age_ms": now_ms - offset - updated_at,
        "best_bid": _level(levels["bids"]),
        "best_ask": _level(levels["asks"]),
        "limit_px": _plain(limit_px),
        "touch_past_limit_pct": None if past is None else _pct(past),
        "qty_within_limit": _plain(within),
        "qty_within_limit_complete": complete,
    }
    warning = clock_report(clock).get("clock_warning")
    if warning:
        context["clock_warning"] = warning
    return context


def _level(side: list) -> dict | None:
    return {"px": _plain(side[0][0]), "qty": _plain(side[0][1])} if side else None


async def ioc_book(
    api_url: str | None,
    symbol: str,
    *,
    is_buy: bool,
    limit_px: Decimal,
    tick: Decimal,
    step: Decimal,
    timeout: float | None = None,
) -> dict:
    """`book_context` from one depth read, or `{"unavailable": reason}`: never raises (except cancellation)."""
    timeout = BOOK_READ_TIMEOUT_S if timeout is None else timeout
    try:
        book, clock = await asyncio.wait_for(read_book(api_url, symbol), timeout=timeout)
        return book_context(
            book,
            clock,
            symbol=symbol,
            is_buy=is_buy,
            limit_px=limit_px,
            tick=tick,
            step=step,
            now_ms=int(time.time() * 1000),
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return {"unavailable": f"The order book read timed out after {timeout:g} s"}
    except Exception as exc:
        return {"unavailable": exception_result(exc)["error"]}


def shortfall_warning(book: dict, qty: Decimal, is_buy: bool) -> str | None:
    """A preview's warning when the visible book holds less than `qty` inside the limit."""
    if "qty_within_limit" not in book:
        return None
    within = Decimal(book["qty_within_limit"])
    if within >= qty or not book["qty_within_limit_complete"]:
        return None
    side = "asks" if is_buy else "bids"
    if within == 0:
        if (book["best_ask"] if is_buy else book["best_bid"]) is None:
            found = f"The book has no {side} at all (one-sided)"
        else:
            found = f"No {side} sit at or inside the limit {book['limit_px']}"
        return f"{found}, so this order would likely fill nothing. Offer a smaller size or a resting limit instead."
    return (
        f"Only {_plain(within)} sits in the {side} at or inside the limit {book['limit_px']}, less than the "
        f"{_plain(qty)} requested, "
        "so this order would likely fill only part. Offer a smaller size or a resting limit instead."
    )


def unfilled(result: dict) -> bool:
    """An acknowledged IOC that ended with part or all of its quantity unfilled (a definitive venue answer)."""
    status = getattr(result.get("status"), "value", result.get("status"))
    filled = Decimal(result.get("filled_qty") or "0")
    return status == "CANCELLED" and filled < Decimal(result["qty"])


def unfilled_message(result: dict, symbol: str) -> str | None:
    """The error for an IOC that the book could not fill completely, or None to keep the generic one."""
    reason = getattr(result.get("cancel_reason"), "value", result.get("cancel_reason"))
    if not unfilled(result) or reason not in LIQUIDITY_REASONS:
        return None
    limit = result.get("limit_px_usd")
    filled = Decimal(result.get("filled_qty") or "0")
    if filled == 0:
        text = f"Nothing filled at the limit {limit}"
    else:
        text = f"Filled {_plain(filled)} of {result['qty']}; the rest was cancelled at the limit {limit}"
    if result.get("cancel_reason_message"):
        text += f" ({result['cancel_reason_message'].rstrip('.')})"
    book = result.get("book") or {}
    if "updated_at_ms" in book:
        text += ". `book` shows the market as of its `updated_at_ms`."
    else:
        text += f". The order book could not be read; `data market book --symbol {symbol}` reads it."
    return (
        text + " Do not resend at the same or a nearby price until the book's `updated_at_ms` has moved on, and "
        "never widen the slippage without the user's yes; on a thin book offer a smaller size or a resting limit."
    )
