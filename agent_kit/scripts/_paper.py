"""Local-only simulator: no signing, account API, SDK client or transaction imports."""

import copy
import json
import os
import re
import sqlite3
import stat
import uuid
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal, localcontext
from pathlib import Path

from agent_kit.scripts._v2 import decimal, integer, rows

UNIT = Decimal("0.000000000000000001")
RUSD_UNIT = Decimal("0.000001")
MAX_EVENTS = 10000
MAX_AGE_MS = 30000


def exact(value, label):
    number = decimal(value, label)
    if number.copy_abs() > Decimal("1e20") or int(number.as_tuple().exponent) < -18:
        raise ValueError(f"{label} exceeds paper precision/range (18 decimals, 1e20)")
    return number


def money(value, rounding=ROUND_FLOOR):
    return value.quantize(UNIT, rounding=rounding)


def fresh(timestamp, now, label):
    timestamp = integer(timestamp, label)
    if not 0 <= now - timestamp <= MAX_AGE_MS:
        raise ValueError(f"{label} is stale or in the future")


class PaperStore:
    """One SQLite transaction includes state and request outcomes; crashes roll back both."""

    def __init__(self, directory):
        self.directory = Path(directory).expanduser()

    def __enter__(self):
        if not self.directory.is_absolute():
            raise ValueError("Paper directory must be absolute")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Paper directory must be owner-only (0700), not a symlink")
        path = self.directory / "paper.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Paper database must be a regular owner-only file (0600)")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=0)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY CHECK (id=1), state TEXT NOT NULL)"
            )
            record = self.db.execute("SELECT state FROM ledger WHERE id=1").fetchone()
            self.state = json.loads(record[0]) if record else None
            if self.state is not None and (self.state.get("schema") != 1 or self.state.get("mode") != "paper"):
                raise ValueError("Unsupported paper state schema")
            return self
        except BaseException:
            self.db.close()
            raise

    def save(self, state):
        self.db.execute("INSERT OR REPLACE INTO ledger VALUES (1, ?)", (json.dumps(state, allow_nan=False),))
        self.state = state

    def __exit__(self, exc_type, *args):
        try:
            self.db.rollback() if exc_type else self.db.commit()
        finally:
            self.db.close()


def initialize(collateral, fee_bps, api_url, now):
    collateral = exact(collateral, "collateral")
    fee = exact(fee_bps, "fee bps")
    if collateral <= 0 or not 0 <= fee <= 100:
        raise ValueError("Positive collateral and explicit fee assumption of 0..100 bps required")
    return {
        "schema": 1,
        "mode": "paper",
        "ledger_id": str(uuid.uuid4()),
        "api_url": api_url,
        "created_at_ms": now,
        "initial_collateral": str(collateral),
        "cash": str(collateral),
        "taker_fee_bps": str(fee),
        "fee_source": "user_supplied_assumption_not_live_tier",
        "positions": {},
        "requests": {},
        "history": [],
        "snapshots": [],
        "realized_pnl": "0",
        "fees": "0",
    }


def valuation(state, summaries, now):
    with localcontext() as ctx:
        ctx.prec = 96
        markets = {r["symbol"]: r for r in rows(summaries)}
        positions = []
        pnl, notional = Decimal(0), Decimal(0)
        for symbol, p in state["positions"].items():
            market = markets.get(symbol)
            if market is None:
                raise ValueError(f"Missing mark for {symbol}")
            fresh(market.get("pricesUpdatedAt"), now, "mark timestamp")
            mark = exact(market.get("markPrice"), "mark")
            if mark <= 0:
                raise ValueError("Mark must be positive")
            qty, basis = Decimal(p["qty"]), Decimal(p["basis"])
            value = money(qty * mark - basis)
            pnl += value
            notional += abs(qty * mark)
            positions.append({**p, "symbol": symbol, "mark": str(mark), "unrealized_pnl": str(value)})
        equity = money(Decimal(state["cash"]) + pnl)
        return {
            "mode": "paper",
            "ledger_id": state["ledger_id"],
            "observed_at_ms": now,
            "cash": state["cash"],
            "initial_collateral": state["initial_collateral"],
            "positions": positions,
            "unrealized_pnl": str(pnl),
            "equity": str(equity),
            "realized_pnl": state["realized_pnl"],
            "fees": state["fees"],
            "gross_notional": str(notional),
            "virtual_1x_headroom": str(equity - notional),
            "funding": None,
            "adl": None,
            "liquidation_price": None,
            "risk_model": "conservative_virtual_1x_not_reya_margin",
            "fee_source": state["fee_source"],
            "taker_fee_bps": state["taker_fee_bps"],
            "provenance": "local_simulation_live_V2_marks",
        }


def simulate(state, request, definition, book, summaries, now):
    """Pure transition; failures never mutate persisted state. Idempotency precedes reads."""
    with localcontext() as ctx:
        ctx.prec = 96
        return _simulate(state, request, definition, book, summaries, now)


def replay(state, request):
    key = request["request_id"]
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
        raise ValueError("request-id must contain 1..100 letters/digits/underscore/hyphen")
    old = state["requests"].get(key)
    if old:
        if old["request"] != request:
            raise ValueError("Request ID already used with different parameters")
        return {**old["result"], "replayed": True}
    return None


def _simulate(state, request, definition, book, summaries, now):
    old = replay(state, request)
    if old:
        return state, old
    if len(state["history"]) >= MAX_EVENTS:
        raise ValueError("Paper ledger event bound reached; export history and explicitly reset")
    symbol, side = request["symbol"], request["side"]
    if request["kind"] not in ("market", "ioc") or side not in ("buy", "sell"):
        raise ValueError("Paper supports only market and IOC buy/sell orders")
    if definition.get("symbol") != symbol or book.get("symbol") != symbol or book.get("type") != "SNAPSHOT":
        raise ValueError("Mismatched market or missing book snapshot")
    fresh(book.get("updatedAt"), now, "book timestamp")
    qty = exact(request["qty"], "qty")
    step, tick, minimum = [exact(definition.get(k), k) for k in ("qtyStepSize", "tickSize", "minOrderQty")]
    pos = state["positions"].get(symbol, {"qty": "0", "basis": "0"})
    position_qty = Decimal(pos["qty"])
    direction = Decimal(1 if side == "buy" else -1)
    if request["reduce_only"] and (position_qty * direction >= 0 or qty > abs(position_qty)):
        raise ValueError("Reduce-only quantity must reduce an existing position without flipping")
    # The API rejects every order below the minimum, reduce-only included
    # (PRICE_QTY_BOUNDS_ERROR, observed on devnet1); the ME exemption is unreachable.
    if min(step, tick, minimum) <= 0 or qty <= 0 or qty < minimum or qty % step:
        raise ValueError("Quantity must meet the V2 minimum and step exactly")
    before = valuation(state, summaries, now)
    market = next((r for r in rows(summaries) if r.get("symbol") == symbol), None)
    if market is None:
        raise ValueError(f"Missing market summary for {symbol}")
    fresh(market.get("pricesUpdatedAt"), now, "order price timestamp")
    # The mark is both the fee basis and the market-order bound; the book sets fills.
    fee_mark = exact(market.get("markPrice"), "mark")
    if fee_mark <= 0:
        raise ValueError("Mark must be positive")
    if request["kind"] == "market" and request.get("price") is not None:
        raise ValueError("Market paper orders use --slippage, not --price")
    if request["kind"] == "ioc":
        limit = exact(request["price"], "price")
        if limit <= 0 or limit % tick:
            raise ValueError("IOC price must be positive and on the V2 tick")
    else:
        slippage = exact(request["slippage"], "slippage")
        if not 0 <= slippage <= Decimal(".05"):
            raise ValueError("Paper market slippage must be between 0 and 0.05")
        raw = fee_mark * (1 + slippage if side == "buy" else 1 - slippage)
        limit = (raw / tick).to_integral_value(rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR) * tick
    levels = {}
    for book_side in ("asks", "bids"):
        parsed = []
        for row in rows(book.get(book_side)):
            px, amount = exact(row.get("px"), "book px"), exact(row.get("qty"), "book qty")
            if px <= 0 or amount < 0 or px % tick or amount % step:
                raise ValueError("Invalid book price/quantity grid")
            if amount:
                parsed.append((px, amount))
        if len({px for px, _ in parsed}) != len(parsed):
            raise ValueError("Duplicate book level")
        levels[book_side] = sorted(parsed, reverse=book_side == "bids")
    if levels["bids"] and levels["asks"] and levels["bids"][0][0] >= levels["asks"][0][0]:
        raise ValueError("Crossed book snapshot")
    remaining, fills = qty, []
    for px, amount in levels["asks" if side == "buy" else "bids"]:
        if (side == "buy" and px > limit) or (side == "sell" and px < limit):
            break
        take = min(remaining, amount)
        if take:
            fills.append({"qty": str(take), "price": str(px)})
        remaining -= take
        if remaining == 0:
            break
    state = copy.deepcopy(state)
    realized, fees = Decimal(0), Decimal(0)
    basis = Decimal(pos["basis"])
    for fill in fills:
        amount, px = Decimal(fill["qty"]), Decimal(fill["price"])
        delta = amount * direction
        # Live settlement charges |base| x on-chain mark, not the fill price: notional rounds away from
        # zero to rUSD 6 decimals, then the fee rounds down. Paper can only use the observed V2 mark.
        notional = (amount * fee_mark).quantize(RUSD_UNIT, rounding=ROUND_CEILING)
        fee = (notional * Decimal(state["taker_fee_bps"]) / 10000).quantize(RUSD_UNIT, rounding=ROUND_DOWN)
        gain = Decimal(0)
        if position_qty * delta < 0:
            closing = min(abs(position_qty), amount)
            # Signed cost basis tracks remaining exposure; rounding is part of the contract.
            released = basis if closing == abs(position_qty) else money(basis * closing / abs(position_qty), ROUND_DOWN)
            gain = money(closing * px * (1 if position_qty > 0 else -1) - released)
            basis -= released
            if amount > closing:
                basis += (amount - closing) * direction * px
        else:
            basis += delta * px
        position_qty += delta
        realized += gain
        fees += fee
        fill.update(fee=str(fee), realized_pnl=str(gain))
    if position_qty:
        state["positions"][symbol] = {"qty": str(position_qty), "basis": str(basis)}
    else:
        state["positions"].pop(symbol, None)
    state["cash"] = str(money(Decimal(state["cash"]) + realized - fees))
    state["realized_pnl"] = str(Decimal(state["realized_pnl"]) + realized)
    state["fees"] = str(Decimal(state["fees"]) + fees)
    after = valuation(state, summaries, now)
    # Full de-risking remains possible even after losses put the virtual account under water.
    strictly_reducing = position_qty * Decimal(pos["qty"]) >= 0 and abs(position_qty) < abs(Decimal(pos["qty"]))
    if fills and not strictly_reducing and Decimal(after["virtual_1x_headroom"]) < 0:
        raise ValueError("Insufficient virtual collateral under the paper 1x gross-notional rule")
    result = {
        "mode": "paper",
        "ledger_id": state["ledger_id"],
        "request_id": request["request_id"],
        "symbol": symbol,
        "side": side,
        "timestamp": now,
        "requested_qty": str(qty),
        "filled_qty": str(qty - remaining),
        "cancelled_qty": str(remaining),
        "limit_price": str(limit),
        "fee_mark_price": str(fee_mark),
        "status": "filled" if not remaining else ("partially_filled_cancelled" if fills else "cancelled"),
        "fills": fills,
        "realized_pnl": str(realized),
        "fees": str(fees),
        "cash_before": before["cash"],
        "cash_after": after["cash"],
        "replayed": False,
        "book_updated_at_ms": book["updatedAt"],
        "provenance": "local_simulation_V2_snapshot",
    }
    state["history"].append(result)
    state["requests"][request["request_id"]] = {"request": request, "result": result}
    return state, result
