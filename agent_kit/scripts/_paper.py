"""Local-only simulator: no signing, account API, SDK client or transaction imports.

Admission, margin figures and liquidation prices come from the kit's port of the matching
engine's risk engine (`_margin`), applied to the paper ledger (docs/paper-trading.md).
"""

from types import SimpleNamespace

import copy
import json
import os
import re
import sqlite3
import stat
import uuid
from datetime import timezone
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from email.utils import parsedate_to_datetime
from pathlib import Path

from agent_kit.scripts._margin import (
    DEFAULT_HEADROOM,
    AccountMargin,
    MarginSnapshot,
    MarketRisk,
    account_figures,
    adds_exposure,
    check_order,
    im_multiplier_from,
    liquidation_price,
    margin_usage,
    order_margin,
    position_risk,
    size_order,
)
from agent_kit.scripts._v2 import decimal, integer, rows

UNIT = Decimal("0.000000000000000001")
RUSD_UNIT = Decimal("0.000001")
MAX_EVENTS = 10000
MAX_AGE_MS = 30000
# The runner's band: data may be up to 5 s ahead of this computer's clock (skew) and 30 s old.
FUTURE_TOLERANCE_MS = 5000
CLOCK_WARNING_MS = 2000
SCHEMA = 2
DEFAULT_TRIGGER_SLIPPAGE = Decimal("0.01")
MAX_TRIGGER_SLIPPAGE = Decimal("0.03")
# Stop-loss first: it is the leg that protects.
TRIGGER_LEGS = (("stop_loss", "sl"), ("take_profit", "tp"))
RISK_MODEL = "reya_engine_margin"
SYNC_CLOCK = (
    "Sync this computer's clock and retry: on WSL run `sudo hwclock -s` (or `wsl --shutdown` from Windows, then "
    "reopen the terminal); elsewhere turn on automatic date and time (NTP). The live runner refuses the same skew."
)
LIQUIDATION_MODEL = (
    "backstop: every position closed at the observed marks and the whole margin balance taken by the pool; "
    "live usually liquidates part of the exposure first"
)


class PaperRefusal(ValueError):
    """A refusal with structured details (the engine's verdict) for the error output."""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


def exact(value, label):
    number = decimal(value, label)
    if number.copy_abs() > Decimal("1e20") or int(number.as_tuple().exponent) < -18:
        raise ValueError(f"{label} exceeds paper precision/range (18 decimals, 1e20)")
    return number


def money(value, rounding=ROUND_FLOOR):
    return value.quantize(UNIT, rounding=rounding)


def _text(value: Decimal) -> str:
    return format(value.quantize(UNIT).normalize(), "f")


# --------------------------------------------------------------------------
# Clock
# --------------------------------------------------------------------------


def clock_offset(sample) -> dict | None:
    """This computer's clock minus the server's, from one response's HTTP `Date` header.

    `sample` is (sent, received, Date) with local times in seconds. Date has whole seconds, so the
    server's time lies in [Date, Date + 1 s): the estimate is good to half a second plus half the
    round trip. None when there is no usable header."""
    if not sample or not sample[2]:
        return None
    sent, received, header = sample
    try:
        server = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if server.tzinfo is None:
        server = server.replace(tzinfo=timezone.utc)
    local = (sent + received) / 2
    return {
        "offset_ms": round((local - server.timestamp() - 0.5) * 1000),
        "uncertainty_ms": round((0.5 + max(received - sent, 0) / 2) * 1000),
        "source": "http_date",
    }


def clock_report(clock: dict | None) -> dict:
    """The `clock` fields of a result: the offset, and a warning once it is clearly more than a second."""
    report: dict = {"clock": clock}
    if clock is not None and abs(clock["offset_ms"]) >= CLOCK_WARNING_MS:
        relation = "ahead of" if clock["offset_ms"] > 0 else "behind"
        report["clock_warning"] = (
            f"This computer's clock is about {_seconds(clock['offset_ms'])} s {relation} Reya's server clock "
            f"(paper and the runner accept {FUTURE_TOLERANCE_MS // 1000} s). {SYNC_CLOCK}"
        )
    return report


def _seconds(ms: int) -> str:
    return f"{abs(ms) / 1000:.1f}"


def fresh(timestamp, now, label, clock=None):
    timestamp = integer(timestamp, label)
    age = now - timestamp
    if not -FUTURE_TOLERANCE_MS <= age <= MAX_AGE_MS:
        raise ValueError(_stale(label, age, clock))


def _stale(label: str, age: int, clock: dict | None) -> str:
    if age < 0:
        text = (
            f"The {label} is timestamped {_seconds(age)} s ahead of this computer's clock "
            f"(paper accepts {FUTURE_TOLERANCE_MS // 1000} s)."
        )
    else:
        text = f"The {label} is {_seconds(age)} s old by this computer's clock (paper accepts {MAX_AGE_MS // 1000} s)."
    offset = None if clock is None else clock["offset_ms"]
    if offset is not None and abs(offset) >= CLOCK_WARNING_MS:
        relation = "ahead" if offset > 0 else "behind"
        text += f" Reya's server clock puts this computer about {_seconds(offset)} s {relation}."
    # Servers do not timestamp the future; old data is the clock only if the server's clock says it is fresh.
    if age < 0 or (offset is not None and -FUTURE_TOLERANCE_MS <= age - offset <= MAX_AGE_MS):
        fix = SYNC_CLOCK
    elif offset is not None:
        fix = "The API returned data that has not changed for that long: retry, or check the market is active."
    else:
        fix = "Check this computer's clock, then retry: the market may also be inactive."
    return f"{text} {fix} The paper ledger is unchanged."


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------


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
            if self.state is not None and (
                self.state.get("schema") not in (1, SCHEMA) or self.state.get("mode") != "paper"
            ):
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
        "schema": SCHEMA,
        "mode": "paper",
        "ledger_id": str(uuid.uuid4()),
        "api_url": api_url,
        "created_at_ms": now,
        "initial_collateral": str(collateral),
        "cash": str(collateral),
        "taker_fee_bps": str(fee),
        "fee_source": "user_supplied_assumption_not_live_tier",
        "positions": {},
        "triggers": {},
        "requests": {},
        "history": [],
        "snapshots": [],
        "realized_pnl": "0",
        "fees": "0",
        "funding": "0",
        "liquidation_penalties": "0",
        "uncovered_losses": "0",
        "last_observation_ms": None,
    }


def upgrade(state, now):
    """Continue a ledger made before 0.10.0 in place: same cash, positions, history and request IDs.
    Its positions carry no funding anchor, so funding accrues from their first observation."""
    if state["schema"] == SCHEMA:
        return state
    return {
        **state,
        "schema": SCHEMA,
        "triggers": {},
        "funding": "0",
        "liquidation_penalties": "0",
        "uncovered_losses": "0",
        "last_observation_ms": None,
        "upgraded_at_ms": now,
    }


def _record(state, event):
    if len(state["history"]) >= MAX_EVENTS:
        raise ValueError("Paper ledger event bound reached; export history and explicitly reset")
    state["history"].append(event)


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


def _markets(summaries) -> dict:
    return {r["symbol"]: r for r in rows(summaries)}


def _definitions(definitions) -> dict:
    return {r["symbol"]: r for r in rows(definitions)}


def _mark(markets, symbol, now, clock):
    market = markets.get(symbol)
    if market is None:
        raise ValueError(f"Missing market summary for {symbol}")
    fresh(market.get("pricesUpdatedAt"), now, f"{symbol} mark price", clock)
    mark = exact(market.get("markPrice"), "mark")
    if mark <= 0:
        raise ValueError("Mark must be positive")
    return mark


def _funding_value(markets, symbol, long):
    if symbol not in markets:
        raise ValueError(f"Missing market summary for {symbol}")
    key = "longFundingValue" if long else "shortFundingValue"
    return decimal(markets[symbol].get(key), f"{symbol} {key}")


def _unsettled_funding(position, markets, symbol):
    """Funding since the position's last fill (Core settles it into realized PnL at every trade), from
    V2's cumulative funding value per unit of base; rounded down to rUSD's 6 decimals. Credit positive."""
    anchor = position.get("funding_value")
    if anchor is None:
        return Decimal(0)
    qty = Decimal(position["qty"])
    value = _funding_value(markets, symbol, qty > 0)
    return (-(value - Decimal(anchor)) * qty).quantize(RUSD_UNIT, rounding=ROUND_FLOOR)


def _levels(book, symbol, tick, step, now, clock):
    if book.get("symbol") != symbol or book.get("type") != "SNAPSHOT":
        raise ValueError("Mismatched market or missing book snapshot")
    fresh(book.get("updatedAt"), now, f"{symbol} order book", clock)
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
    return levels


def _grid(definition):
    step, tick, minimum = [exact(definition.get(k), k) for k in ("qtyStepSize", "tickSize", "minOrderQty")]
    if min(step, tick, minimum) <= 0:
        raise ValueError("Quantity must meet the V2 minimum and step exactly")
    return step, tick, minimum


# --------------------------------------------------------------------------
# Margin: the engine's view of the paper ledger
# --------------------------------------------------------------------------


def _snapshot(state, markets, by_symbol, now, clock, symbols=()) -> MarginSnapshot:
    """The paper account as the engine reads an account: rUSD cash (no haircut, floored to 6 decimals)
    plus unsettled funding is the real balance; positions are valued at the V2 mark."""
    risk_definitions = {
        symbol: SimpleNamespace(
            symbol=symbol,
            liquidation_margin_parameter=d.get("liquidationMarginParameter"),
            initial_margin_parameter=d.get("initialMarginParameter"),
            tick_size=d.get("tickSize"),
        )
        for symbol, d in by_symbol.items()
    }
    funding = upnl = Decimal(0)
    risk = {}
    for symbol in sorted(set(state["positions"]) | set(symbols)):
        definition = by_symbol.get(symbol)
        if definition is None:
            raise ValueError(f"Missing market definition for {symbol}")
        rate = exact(definition.get("liquidationMarginParameter"), f"{symbol} liquidation margin parameter")
        if not 0 < rate < 1:
            raise ValueError(f"{symbol} liquidation margin parameter must be between 0 and 1")
        exact(definition.get("tickSize"), "tick size")
        mark = _mark(markets, symbol, now, clock)
        position = state["positions"].get(symbol)
        if position is None:
            risk[symbol] = MarketRisk(Decimal(0), mark, rate, mark)
            continue
        qty = Decimal(position["qty"])
        entry = Decimal(position["basis"]) / qty
        upnl += (qty * (mark - entry)).quantize(RUSD_UNIT, rounding=ROUND_FLOOR)
        funding += _unsettled_funding(position, markets, symbol)
        risk[symbol] = MarketRisk(qty, mark, rate, entry)
    multiplier = im_multiplier_from(risk_definitions.values())
    if multiplier is None and risk:
        raise ValueError("No perp market definition publishes margin parameters; cannot compute initial margin")
    real = Decimal(state["cash"]).quantize(RUSD_UNIT, rounding=ROUND_FLOOR) + funding
    return MarginSnapshot(
        account_id=0,
        margin=AccountMargin(real, real + upnl, risk),
        im_multiplier=multiplier or Decimal(1),
        definitions=risk_definitions,
        collateral=[],
        positions=[],
        unrealized_pnl=upnl,
        funding_estimate=funding,
    )


MARGIN_KEYS = (
    "equity_usd",
    "liquidation_margin_req_usd",
    "initial_margin_req_usd",
    "margin_usage",
    "margin_usage_pct",
    "liquidatable",
    "margin_buffer_usd",
    "initial_margin_buffer_usd",
    "health_usd",
    "im_multiplier",
    "settlement_reserve_rate",
)


def _margin_figures(snapshot, positions):
    figures = account_figures(snapshot)
    lm = snapshot.settled().lm
    for p in positions:
        risk = position_risk(snapshot, p["symbol"], lm)
        p["liquidation_price"] = risk["liquidation_price_usd"]
        p["distance_to_liquidation_pct"] = risk["distance_to_liquidation_pct"]
    return {key: figures[key] for key in MARGIN_KEYS}


def valuation(state, summaries, now, definitions=None, clock=None):
    """Mark-to-market the paper account; with V2 market definitions, also the engine's margin view of it."""
    with localcontext() as ctx:
        ctx.prec = 96
        markets = _markets(summaries)
        positions = []
        pnl, notional, funding = Decimal(0), Decimal(0), Decimal(0)
        for symbol, p in sorted(state["positions"].items()):
            mark = _mark(markets, symbol, now, clock)
            qty, basis = Decimal(p["qty"]), Decimal(p["basis"])
            value = money(qty * mark - basis)
            accrued = _unsettled_funding(p, markets, symbol)
            pnl += value
            funding += accrued
            notional += abs(qty * mark)
            positions.append(
                {
                    **p,
                    "symbol": symbol,
                    "side": "long" if qty > 0 else "short",
                    "avg_entry_price": _text(basis / qty),
                    "mark": str(mark),
                    "unrealized_pnl": str(value),
                    "unsettled_funding": str(accrued),
                    "triggers": state["triggers"].get(symbol),
                }
            )
        equity = money(Decimal(state["cash"]) + pnl + funding)
        margin = None
        if definitions is not None and positions:
            snapshot = _snapshot(state, markets, _definitions(definitions), now, clock)
            margin = _margin_figures(snapshot, positions)
        return {
            "mode": "paper",
            "ledger_id": state["ledger_id"],
            "observed_at_ms": now,
            "cash": state["cash"],
            "initial_collateral": state["initial_collateral"],
            "positions": positions,
            "unrealized_pnl": str(pnl),
            "unsettled_funding": str(funding),
            "equity": str(equity),
            "realized_pnl": state["realized_pnl"],
            "fees": state["fees"],
            "funding": state["funding"],
            "liquidation_penalties": state["liquidation_penalties"],
            "uncovered_losses": state["uncovered_losses"],
            "gross_notional": str(notional),
            "adl": None,
            "margin": margin,
            "risk_model": RISK_MODEL,
            "fee_source": state["fee_source"],
            "taker_fee_bps": state["taker_fee_bps"],
            "provenance": "local_simulation_live_V2_marks",
        }


# --------------------------------------------------------------------------
# Fills
# --------------------------------------------------------------------------


def _fill(state, symbol, side, qty, limit, levels, fee_mark, markets, now):
    """Walk the opposing depth within the limit and settle the fills as Core does: funding first, then
    each fill's price PnL and taker fee. Mutates `state`."""
    direction = Decimal(1 if side == "buy" else -1)
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
    pos = state["positions"].get(symbol, {"qty": "0", "basis": "0"})
    old_qty = position_qty = Decimal(pos["qty"])
    basis = Decimal(pos["basis"])
    realized, fees = Decimal(0), Decimal(0)
    funding = _unsettled_funding(pos, markets, symbol) if fills and position_qty else Decimal(0)
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
    dropped = []
    if fills:
        if position_qty:
            same_side = old_qty * position_qty > 0
            state["positions"][symbol] = {
                "qty": str(position_qty),
                "basis": str(basis),
                "funding_value": str(_funding_value(markets, symbol, position_qty > 0)),
                "funding_since_ms": pos.get("funding_since_ms", now) if same_side else now,
            }
        else:
            state["positions"].pop(symbol, None)
        # TP/SL close the whole position: the venue cancels them once it is flat, and a flip leaves
        # them on the wrong side.
        if old_qty and old_qty * position_qty <= 0 and symbol in state["triggers"]:
            dropped = [{"kind": kind, **leg} for kind, leg in state["triggers"].pop(symbol).items()]
    state["cash"] = str(money(Decimal(state["cash"]) + realized - fees + funding))
    state["realized_pnl"] = str(Decimal(state["realized_pnl"]) + realized)
    state["fees"] = str(Decimal(state["fees"]) + fees)
    state["funding"] = str(Decimal(state["funding"]) + funding)
    status = "filled" if not remaining else ("partially_filled_cancelled" if fills else "cancelled")
    return SimpleNamespace(
        fills=fills,
        remaining=remaining,
        realized=realized,
        fees=fees,
        funding=funding,
        status=status,
        dropped=dropped,
    )


# --------------------------------------------------------------------------
# TP/SL
# --------------------------------------------------------------------------


def _label(kind):
    return "take-profit" if kind == "take_profit" else "stop-loss"


def _above(kind, long):
    """A long's take-profit and a short's stop-loss sit above the price; the other two below."""
    return (kind == "take_profit") == long


def _reached(kind, long, trigger, mark):
    return mark >= trigger if _above(kind, long) else mark <= trigger


def _on_tick(value, tick, rounding):
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _level(text):
    """--sl/--tp: a percent of the entry's average fill price ("0.35%", below 50%) or a price."""
    if not isinstance(text, str):
        raise ValueError("--sl/--tp must be a percent (0.35%) or a price")
    if text.endswith("%"):
        percent = exact(text[:-1], "--sl/--tp percent")
        if not 0 < percent < 50:
            raise ValueError(f"A percent level must be above 0 and below 50%, got '{text}'")
        return percent / 100, None
    price = exact(text, "--sl/--tp price")
    if price <= 0:
        raise ValueError(f"A price level must be positive, got '{text}'")
    return None, price


def _trigger_slippage(request):
    slippage = exact(request.get("trigger_slippage") or str(DEFAULT_TRIGGER_SLIPPAGE), "trigger slippage")
    if not 0 < slippage <= MAX_TRIGGER_SLIPPAGE:
        raise ValueError(f"Trigger slippage must be above 0 and at most {MAX_TRIGGER_SLIPPAGE} (the venue's band)")
    return slippage


def _bracket_prices(text, kind, long, reference, tick, slippage):
    """Trigger and child limit for one leg, rounded as the live `order market --sl/--tp` rounds them."""
    fraction, price = _level(text)
    if price is None:
        price = reference * (1 + fraction) if _above(kind, long) else reference * (1 - fraction)
    trigger = _on_tick(price, tick, ROUND_HALF_UP)
    # The fired child closes the position: a sell below the trigger for a long, a buy above it for a short.
    if long:
        limit = _on_tick(trigger * (1 - slippage), tick, ROUND_CEILING)
    else:
        limit = _on_tick(trigger * (1 + slippage), tick, ROUND_FLOOR)
    if trigger <= 0 or limit <= 0:
        raise ValueError(f"The {_label(kind)} level {text} rounds to a zero price")
    return trigger, limit


def _take_profit_warning(long, limit, entry, fix):
    """The live warning for a take-profit whose child limit is past the entry, so it can close at a loss."""
    if entry <= 0 or (limit >= entry if long else limit <= entry):
        return None
    side = "long" if long else "short"
    distance = abs((limit - entry) / entry * 100).quantize(Decimal("0.01"))
    relation = "below" if long else "above"
    return (
        f"The take-profit's child limit {_text(limit)} is {distance}% {relation} the {side}'s entry {_text(entry)}. "
        "It fires on the mark but fills against the order book at any price up to that limit, so on a thin book "
        f"it can close the position at a loss. {fix}"
    )


def _arm_command(symbol, kind, long, trigger, limit):
    flag = "tp" if kind == "take_profit" else "sl"
    return (
        f"paper order {flag} {symbol} --side {'long' if long else 'short'} --trigger_px {_text(trigger)} "
        f"--limit_px {_text(limit)} --time_in_force IOC"
    )


def _bracket_refusals(state, request, symbol, is_buy, mark, tick):
    """The live `order market --sl/--tp` checks that stop an entry before it is sent."""
    refusals = []
    side = "long" if is_buy else "short"
    position = state["positions"].get(symbol)
    if position is not None and (Decimal(position["qty"]) > 0) != is_buy:
        held = "short" if is_buy else "long"
        refusals.append(
            {
                "check": "protection_opposite_position",
                "message": (
                    f"The open {symbol} paper position is {held}, so this {side} entry would reduce or flip it; "
                    "--sl/--tp protect only a new or larger position. Close the position first, or enter without "
                    "--sl/--tp."
                ),
            }
        )
    slippage = _trigger_slippage(request)
    armed = state["triggers"].get(symbol, {})
    for kind, flag in TRIGGER_LEGS:
        text = request.get(flag)
        if text is None:
            continue
        if kind in armed:
            refusals.append(
                {
                    "check": "protection_exists",
                    "message": (
                        f"A {_label(kind)} is already armed on {symbol}. It closes the whole position, this entry "
                        f"included, and the venue allows one per position. Enter without --{flag}, or cancel the "
                        f"armed {_label(kind)} first (paper order cancel_all --symbol {symbol})."
                    ),
                }
            )
        trigger, _ = _bracket_prices(text, kind, is_buy, mark, tick, slippage)
        if _reached(kind, is_buy, trigger, mark):
            relation = "above" if _above(kind, is_buy) else "below"
            refusals.append(
                {
                    "check": "protection_fires_at_once",
                    "message": (
                        f"A {_label(kind)} for a {side} must be {relation} the mark: {_text(trigger)} vs "
                        f"{_text(mark)}. It would fire as soon as it is placed."
                    ),
                }
            )
    return refusals


def _arm_bracket(state, request, symbol, is_buy, fill, mark, tick, now):
    """Arm the entry's stop-loss, then take-profit, measured from its average fill price."""
    side = "long" if is_buy else "short"
    filled = sum((Decimal(f["qty"]) for f in fill.fills), Decimal(0))
    if not filled:
        return {"state": "not_placed", "message": "The entry did not fill, so no TP/SL was placed."}, None
    reference = sum((Decimal(f["qty"]) * Decimal(f["price"]) for f in fill.fills), Decimal(0)) / filled
    slippage = _trigger_slippage(request)
    position = state["positions"][symbol]
    entry = Decimal(position["basis"]) / Decimal(position["qty"])
    protection: dict = {
        "reference": "avg_fill_px",
        "reference_px_usd": _text(reference),
        "time_in_force": "IOC",
        "trigger_slippage": float(slippage),
        "stop_loss": None,
        "take_profit": None,
    }
    legs = []
    for kind, flag in TRIGGER_LEGS:
        text = request.get(flag)
        if text is None:
            continue
        trigger, limit = _bracket_prices(text, kind, is_buy, reference, tick, slippage)
        leg: dict = {
            "level": text,
            "trigger_px_usd": _text(trigger),
            "limit_px_usd": _text(limit),
            "time_in_force": "IOC",
            "armed": False,
            "command": _arm_command(symbol, kind, is_buy, trigger, limit),
        }
        if _reached(kind, is_buy, trigger, mark):
            leg["error"] = (
                f"The mark {_text(mark)} has already passed the {_label(kind)} level {_text(trigger)}; it would "
                "fire at once, so it was not placed"
            )
        else:
            state["triggers"].setdefault(symbol, {})[kind] = {
                "trigger_px": str(trigger),
                "limit_px": str(limit),
                "level": text,
                "armed_at_ms": now,
                "source": "order market",
            }
            leg["armed"] = True
            if kind == "take_profit":
                warning = _take_profit_warning(
                    is_buy,
                    limit,
                    entry,
                    "To take only a profit, set --tp further from the entry than --trigger_slippage (the child "
                    "limit sits that far past the trigger); it may then not fill.",
                )
                if warning:
                    leg["warning"] = warning
        protection[kind] = leg
        legs.append((kind, leg))
    armed = ", ".join(
        f"{_label(k)} at {leg['trigger_px_usd']} (limit {leg['limit_px_usd']})" for k, leg in legs if leg["armed"]
    )
    missing = [(k, leg) for k, leg in legs if not leg["armed"]]
    position_text = f"{side} paper position ({position['qty']} {symbol})"
    if not missing:
        protection.update(
            state="protected", message=f"Armed: {armed}. They close the whole {position_text} when they fire."
        )
        return protection, None
    stop_missing = missing[0][0] == "stop_loss"
    protection["state"] = "unprotected" if stop_missing or not armed else "partial"
    error = (
        f"The {position_text} is UNPROTECTED: the stop-loss was not armed ({missing[0][1]['error']})."
        if stop_missing
        else f"The take-profit was not armed ({missing[0][1]['error']})."
    )
    error += " Arm it with: " + "; ".join(leg["command"] for _, leg in missing) + "."
    protection["message"] = (f"Armed: {armed}. " if armed else "") + error
    return protection, error


def arm_trigger(
    state,
    symbol,
    kind,
    position_side,
    trigger_px,
    limit_px,
    definitions,
    summaries,
    now,
    *,
    time_in_force,
    force=False,
    client_order_id=None,
    clock=None,
):
    """`paper order sl|tp`: arm a whole-position trigger on an existing paper position, as live does."""
    with localcontext() as ctx:
        ctx.prec = 96
        if time_in_force != "IOC":
            raise ValueError(
                "Paper fires TP/SL children as IOC only: a GTC child would rest, which paper cannot simulate"
            )
        long = position_side in ("long", "buy")
        side = "long" if long else "short"
        position = state["positions"].get(symbol)
        if position is None or (Decimal(position["qty"]) > 0) != long:
            raise ValueError(
                f"There is no {side} paper position on {symbol}. A TP/SL closes the whole position, so there is "
                "nothing to protect; no trigger was armed."
            )
        definition = _definitions(definitions).get(symbol)
        if definition is None:
            raise ValueError("Unknown perpetual market")
        tick = exact(definition.get("tickSize"), "tickSize")
        if tick <= 0:
            raise ValueError("tickSize must be positive")
        trigger = _on_tick(exact(trigger_px, "--trigger_px"), tick, ROUND_HALF_UP)
        # The child sells to close a long and buys to close a short: its limit rounds toward the mark.
        limit = _on_tick(exact(limit_px, "--limit_px"), tick, ROUND_CEILING if long else ROUND_FLOOR)
        if trigger <= 0 or limit <= 0:
            raise ValueError("--trigger_px and --limit_px must not round to zero")
        mark = _mark(_markets(summaries), symbol, now, clock)
        leg = {
            "trigger_px": str(trigger),
            "limit_px": str(limit),
            "level": None,
            "armed_at_ms": now,
            "source": f"order {'tp' if kind == 'take_profit' else 'sl'}",
        }
        if client_order_id is not None:
            leg["client_order_id"] = str(client_order_id)
        result = {
            "mode": "paper",
            "action": "order_tp" if kind == "take_profit" else "order_sl",
            "symbol": symbol,
            "position_side": side,
            "close_side": "sell" if long else "buy",
            "quantity_mode": "whole_position",
            "trigger_px_usd": _text(trigger),
            "limit_px_usd": _text(limit),
            "mark_price_usd": str(mark),
            "time_in_force": "IOC",
            "entry_px_usd": _text(Decimal(position["basis"]) / Decimal(position["qty"])),
            "provenance": "local_simulation_trigger",
        }
        armed = state["triggers"].get(symbol, {}).get(kind)
        if armed is not None:
            if Decimal(armed["trigger_px"]) == trigger and Decimal(armed["limit_px"]) == limit:
                return state, {**result, "status": "armed", "already_armed": True, "armed_at_ms": armed["armed_at_ms"]}
            raise ValueError(
                f"A {_label(kind)} is already armed on {symbol} at {_text(Decimal(armed['trigger_px']))} "
                f"(limit {_text(Decimal(armed['limit_px']))}); the venue allows one per position. Cancel it first: "
                f"paper order cancel_all --symbol {symbol}"
            )
        warnings = []
        if _reached(kind, long, trigger, mark):
            relation = "above" if _above(kind, long) else "below"
            message = (
                f"A {_label(kind)} for a {side} position must be {relation} the current mark price "
                f"({_text(trigger)} vs {_text(mark)}); it would fire at the next observation and close the whole "
                "position at the chosen limit if liquidity is available."
            )
            if not force:
                raise ValueError(f"{message} Re-run with --force if this is intentional.")
            warnings.append(message)
        if kind == "take_profit":
            better = "above" if long else "below"
            warning = _take_profit_warning(
                long,
                limit,
                Decimal(position["basis"]) / Decimal(position["qty"]),
                f"To take only a profit, set --limit_px at or {better} the entry; it may then not fill.",
            )
            if warning:
                warnings.append(warning)
        state = copy.deepcopy(state)
        state["triggers"].setdefault(symbol, {})[kind] = leg
        result.update(status="armed", armed_at_ms=now)
        if warnings:
            result["warning"] = " ".join(warnings)
        return state, result


def cancel_triggers(state, symbol=None, everything=False):
    """`paper order cancel_all --symbol S | --all`: TP/SL are paper's only resting orders."""
    if bool(symbol) == bool(everything):
        raise ValueError("Choose exactly one of --symbol or --all for cleanup")
    state = copy.deepcopy(state)
    symbols = list(state["triggers"]) if everything else [symbol]
    cancelled = []
    for name in symbols:
        for kind, leg in state["triggers"].pop(name, {}).items():
            cancelled.append(
                {"symbol": name, "kind": kind, "trigger_px": leg["trigger_px"], "limit_px": leg["limit_px"]}
            )
    return state, {
        "mode": "paper",
        "action": "cancel_all",
        "scope": "all" if everything else symbol,
        "cancelled": cancelled,
        "cancelled_count": len(cancelled),
    }


def due_triggers(state, summaries, now, clock=None):
    """Markets whose armed TP/SL the current mark has reached; paper reads their books before observe()."""
    markets = _markets(summaries)
    due = []
    for symbol, legs in sorted(state["triggers"].items()):
        position = state["positions"].get(symbol)
        if position is None or not legs:
            continue
        long = Decimal(position["qty"]) > 0
        mark = _mark(markets, symbol, now, clock)
        if any(_reached(kind, long, Decimal(leg["trigger_px"]), mark) for kind, leg in legs.items()):
            due.append(symbol)
    return due


def _fire(state, symbol, markets, by_symbol, books, now, clock):
    position = state["positions"][symbol]
    long = Decimal(position["qty"]) > 0
    mark = _mark(markets, symbol, now, clock)
    legs = state["triggers"][symbol]
    kind = next(k for k, _ in TRIGGER_LEGS if k in legs and _reached(k, long, Decimal(legs[k]["trigger_px"]), mark))
    leg = legs[kind]
    trigger, limit = Decimal(leg["trigger_px"]), Decimal(leg["limit_px"])
    if kind == "stop_loss":
        snapshot = _snapshot(state, markets, by_symbol, now, clock)
        lm = snapshot.settled().lm
        if snapshot.margin.margin_balance < lm:
            price = liquidation_price(snapshot.margin, symbol, lm)
            # On the way here the mark met the liquidation price before the stop: liquidation came first.
            if price is not None and (trigger < price if long else trigger > price):
                return None
    book = books.get(symbol)
    if book is None:
        raise ValueError(f"Missing order book for the triggered {symbol} {_label(kind)}")
    step, tick, _ = _grid(by_symbol[symbol])
    levels = _levels(book, symbol, tick, step, now, clock)
    qty = abs(Decimal(position["qty"]))
    cash_before = state["cash"]
    del legs[kind]
    if not legs:
        state["triggers"].pop(symbol)
    fill = _fill(state, symbol, "sell" if long else "buy", qty, limit, levels, mark, markets, now)
    event = {
        "event": kind,
        "mode": "paper",
        "symbol": symbol,
        "timestamp": now,
        "position_side": "long" if long else "short",
        "trigger_px": str(trigger),
        "limit_px": str(limit),
        "mark_price": str(mark),
        "requested_qty": str(qty),
        "filled_qty": str(qty - fill.remaining),
        "cancelled_qty": str(fill.remaining),
        "status": fill.status,
        "fills": fill.fills,
        "realized_pnl": str(fill.realized),
        "fees": str(fill.fees),
        "funding_settled": str(fill.funding),
        "cash_before": cash_before,
        "cash_after": state["cash"],
        "book_updated_at_ms": book["updatedAt"],
        "note": (
            "Fired at this observation: the venue fires at the first mark through the trigger, paper only when a "
            "command reads the mark."
        ),
        "provenance": "local_simulation_trigger_at_observation",
    }
    if fill.remaining:
        event["remaining_position"] = state["positions"].get(symbol, {}).get("qty")
    if fill.dropped:
        event["dropped_triggers"] = fill.dropped
    _record(state, event)
    return event


def _liquidate(state, snapshot, lm, markets, now):
    """Close every position at the observed marks; the pool takes the whole remaining margin balance."""
    closed, realized, funding = [], Decimal(0), Decimal(0)
    for symbol, p in sorted(state["positions"].items()):
        risk = snapshot.margin.markets[symbol]
        qty, basis = Decimal(p["qty"]), Decimal(p["basis"])
        gain = money(qty * risk.mark - basis)
        accrued = _unsettled_funding(p, markets, symbol)
        realized += gain
        funding += accrued
        closed.append(
            {
                "symbol": symbol,
                "qty": p["qty"],
                "avg_entry_price": _text(basis / qty),
                "mark_price": str(risk.mark),
                "realized_pnl": str(gain),
                "funding_settled": str(accrued),
            }
        )
    cash_before = Decimal(state["cash"])
    remaining = money(cash_before + realized + funding)
    penalty, uncovered = max(remaining, Decimal(0)), max(-remaining, Decimal(0))
    usage = margin_usage(snapshot.margin.margin_balance, lm)
    event = {
        "event": "liquidation",
        "mode": "paper",
        "timestamp": now,
        "equity": str(snapshot.margin.margin_balance),
        "liquidation_margin": str(lm),
        "margin_usage": None if usage is None else float(usage.quantize(Decimal("0.0001"), ROUND_CEILING)),
        "positions": closed,
        "cash_before": str(cash_before),
        "cash_after": "0",
        "realized_pnl": str(realized),
        "funding_settled": str(funding),
        "penalty_to_pool": str(penalty),
        "uncovered_loss": str(uncovered),
        "last_safe_observation_ms": state.get("last_observation_ms"),
        "dropped_triggers": state["triggers"],
        "model": LIQUIDATION_MODEL,
        "provenance": "local_simulation_liquidation_at_observation",
    }
    state["realized_pnl"] = str(Decimal(state["realized_pnl"]) + realized)
    state["funding"] = str(Decimal(state["funding"]) + funding)
    state["liquidation_penalties"] = str(Decimal(state["liquidation_penalties"]) + penalty)
    state["uncovered_losses"] = str(Decimal(state["uncovered_losses"]) + uncovered)
    state["cash"] = "0"
    state["positions"], state["triggers"] = {}, {}
    _record(state, event)
    return event


def observe(state, summaries, definitions, books, now, clock=None):
    """What happens to the ledger at this observation of the marks: funding anchors for positions that
    lack one, TP/SL the mark has reached (stop-loss first), then liquidation below the liquidation margin.
    Pure transition; returns the new state and the events."""
    with localcontext() as ctx:
        ctx.prec = 96
        state = copy.deepcopy(state)
        markets = _markets(summaries)
        events: list[dict] = []
        for symbol in [s for s in state["triggers"] if s not in state["positions"]]:
            del state["triggers"][symbol]
        for symbol, p in state["positions"].items():
            if p.get("funding_value") is None:
                p["funding_value"] = str(_funding_value(markets, symbol, Decimal(p["qty"]) > 0))
                p["funding_since_ms"] = now
        if not state["positions"]:
            state["last_observation_ms"] = now
            return state, events
        if definitions is None:
            raise ValueError("Market definitions are required to value paper positions")
        by_symbol = _definitions(definitions)
        for symbol in due_triggers(state, summaries, now, clock):
            event = _fire(state, symbol, markets, by_symbol, books, now, clock)
            if event is not None:
                events.append(event)
        if state["positions"]:
            snapshot = _snapshot(state, markets, by_symbol, now, clock)
            lm = snapshot.settled().lm
            if snapshot.margin.margin_balance < lm:
                events.append(_liquidate(state, snapshot, lm, markets, now))
                return state, events
        state["last_observation_ms"] = now
        return state, events


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


def simulate(state, request, definitions, book, summaries, now, clock=None):
    """Pure transition; failures never mutate persisted state. Idempotency precedes reads."""
    with localcontext() as ctx:
        ctx.prec = 96
        return _simulate(state, request, definitions, book, summaries, now, clock)


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


def _usage(text):
    usage = exact(text, "--usage")
    if not 0 < usage < 1:
        raise ValueError("--usage is a fraction above 0 and below 1 (0.5 = half way to liquidation)")
    return usage


def _size(snapshot, request, symbol, is_buy, limit, step, minimum, fee_rate):
    """--max_size / --usage, as live sizes them; paper has no live order-size guards to apply."""
    if request["reduce_only"]:
        raise ValueError("--max_size and --usage size an order that opens or adds to a position, not a reduce-only one")
    if not adds_exposure(snapshot, symbol, is_buy):
        raise ValueError(
            "--max_size and --usage size an order that opens or adds to a position; this side reduces it. "
            "Close with --reduce_only."
        )
    usage = None if request.get("usage") is None else _usage(request["usage"])

    def largest(target=None):
        return size_order(
            snapshot,
            symbol=symbol,
            is_buy=is_buy,
            price=limit,
            step=step,
            headroom_rate=DEFAULT_HEADROOM,
            fee_rate=fee_rate,
            target_usage=target,
        )

    limits = {"engine_margin": largest()}
    if usage is not None:
        limits["usage"] = largest(usage)
    limited_by = min(limits, key=lambda name: limits[name])
    qty = limits[limited_by]
    sizing = {
        "sized_by": "max_size" if request.get("max_size") else "usage",
        "target_usage": None if usage is None else float(usage),
        "size_limited_by": limited_by,
        "qty_by_limit": {name: _text(value) for name, value in limits.items()},
        "headroom_rate": float(DEFAULT_HEADROOM),
    }
    if qty < minimum:
        raise ValueError(
            f"Nothing fits: sizing by {sizing['sized_by']} gives {_text(qty)}, below the market minimum "
            f"{_text(minimum)} (limited by {limited_by}). Reduce other exposure"
            + (", or raise the target usage" if limited_by == "usage" else "")
            + ", or reset the paper ledger with more collateral."
        )
    return qty, sizing, limits["engine_margin"]


def _simulate(state, request, definitions, book, summaries, now, clock):
    old = replay(state, request)
    if old:
        return state, old
    if len(state["history"]) >= MAX_EVENTS:
        raise ValueError("Paper ledger event bound reached; export history and explicitly reset")
    symbol, side = request["symbol"], request["side"]
    if request["kind"] not in ("market", "ioc") or side not in ("buy", "sell"):
        raise ValueError("Paper supports only market and IOC buy/sell orders")
    by_symbol = _definitions(definitions)
    definition = by_symbol.get(symbol)
    if definition is None or book.get("symbol") != symbol or book.get("type") != "SNAPSHOT":
        raise ValueError("Mismatched market or missing book snapshot")
    fresh(book.get("updatedAt"), now, f"{symbol} order book", clock)
    step, tick, minimum = _grid(definition)
    bracket = request.get("sl") is not None or request.get("tp") is not None
    sized = bool(request.get("max_size")) or request.get("usage") is not None
    if bracket and request["kind"] != "market":
        raise ValueError("--sl/--tp attach only to `order market`")
    if bracket and request["reduce_only"]:
        raise ValueError("--sl/--tp protect a new or larger position; drop them for a --reduce_only order")
    if sized == (request.get("qty") is not None):
        raise ValueError("Give exactly one of --qty, --max_size or --usage")
    pos = state["positions"].get(symbol, {"qty": "0", "basis": "0"})
    position_qty = Decimal(pos["qty"])
    is_buy = side == "buy"
    direction = Decimal(1 if is_buy else -1)
    markets = _markets(summaries)
    # The mark is the fee basis, the market-order bound and the engine's valuation; the book sets fills.
    fee_mark = _mark(markets, symbol, now, clock)
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
        raw = fee_mark * (1 + slippage if is_buy else 1 - slippage)
        limit = (raw / tick).to_integral_value(rounding=ROUND_CEILING if is_buy else ROUND_FLOOR) * tick
    levels = _levels(book, symbol, tick, step, now, clock)
    snapshot = _snapshot(state, markets, by_symbol, now, clock, symbols=[symbol])
    fee_rate = Decimal(state["taker_fee_bps"]) / 10000
    sizing, max_qty = None, None
    if sized:
        qty, sizing, max_qty = _size(snapshot, request, symbol, is_buy, limit, step, minimum, fee_rate)
    else:
        qty = exact(request["qty"], "qty")
    if request["reduce_only"] and (position_qty * direction >= 0 or qty > abs(position_qty)):
        raise ValueError("Reduce-only quantity must reduce an existing position without flipping")
    # The API rejects every order below the minimum, reduce-only included
    # (PRICE_QTY_BOUNDS_ERROR, observed on devnet1); the ME exemption is unreachable.
    if qty <= 0 or qty < minimum or qty % step:
        raise ValueError("Quantity must meet the V2 minimum and step exactly")
    check = check_order(
        snapshot,
        symbol=symbol,
        is_buy=is_buy,
        qty=qty,
        price=limit,
        reduce_only=request["reduce_only"],
        headroom_rate=DEFAULT_HEADROOM,
        fee_rate=fee_rate,
    )
    margin = order_margin(snapshot, check, symbol=symbol, reduce_only=request["reduce_only"])
    if not request["reduce_only"] and adds_exposure(snapshot, symbol, is_buy):
        if max_qty is None:
            max_qty = size_order(
                snapshot,
                symbol=symbol,
                is_buy=is_buy,
                price=limit,
                step=step,
                headroom_rate=DEFAULT_HEADROOM,
                fee_rate=fee_rate,
            )
        margin["max_qty"] = _text(max_qty)
    margin.update(headroom_rate=float(DEFAULT_HEADROOM), taker_fee_rate_assumed=float(fee_rate))
    if check.rejection is not None:
        raise PaperRefusal(
            check.rejection.engine_message,
            {
                "error_code": check.rejection.error_code,
                "refused_by": [{"check": "engine_margin", "message": check.rejection.engine_message}],
                "request_id": request["request_id"],
                "symbol": symbol,
                "qty": _text(qty),
                "limit_price": str(limit),
                "margin": margin,
                **({"sizing": sizing} if sizing else {}),
                "note": "Nothing was simulated and the request ID was not used.",
            },
        )
    if bracket:
        refusals = _bracket_refusals(state, request, symbol, is_buy, fee_mark, tick)
        if refusals:
            raise PaperRefusal(
                refusals[0]["message"],
                {"refused_by": refusals, "request_id": request["request_id"], "symbol": symbol},
            )
    state = copy.deepcopy(state)
    cash_before = state["cash"]
    fill = _fill(state, symbol, side, qty, limit, levels, fee_mark, markets, now)
    protection, protection_error = (None, None)
    if bracket:
        protection, protection_error = _arm_bracket(state, request, symbol, is_buy, fill, fee_mark, tick, now)
    after = valuation(state, summaries, now, definitions, clock)
    result = {
        "mode": "paper",
        "ledger_id": state["ledger_id"],
        "request_id": request["request_id"],
        "symbol": symbol,
        "side": side,
        "timestamp": now,
        "requested_qty": str(qty),
        "filled_qty": str(qty - fill.remaining),
        "cancelled_qty": str(fill.remaining),
        "limit_price": str(limit),
        "fee_mark_price": str(fee_mark),
        "status": fill.status,
        "fills": fill.fills,
        "realized_pnl": str(fill.realized),
        "fees": str(fill.fees),
        "funding_settled": str(fill.funding),
        "cash_before": cash_before,
        "cash_after": state["cash"],
        # The engine's check, for a full fill at the limit price (the worst case, as in a live preview).
        "margin": margin,
        # The ledger after the simulated fill, at the same marks.
        "account_after": {
            "equity": after["equity"],
            "margin": after["margin"],
            "position": next((p for p in after["positions"] if p["symbol"] == symbol), None),
        },
        "replayed": False,
        "book_updated_at_ms": book["updatedAt"],
        "provenance": "local_simulation_V2_snapshot",
    }
    if sizing:
        result["sizing"] = sizing
    if protection is not None:
        result["protection"] = protection
    if protection_error:
        result["error"] = protection_error
    if fill.dropped:
        result["dropped_triggers"] = fill.dropped
    _record(state, result)
    state["requests"][request["request_id"]] = {"request": request, "result": result}
    return state, result
