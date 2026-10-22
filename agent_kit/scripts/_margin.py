"""Pre-trade margin as the matching engine computes it, rebuilt from public V2 data.

The projection and the verdict are ported from the matching engine's risk engine
(`matching-engine/src/base/risk_engine`, source revision 4d8ed012):
`compute.rs` (`project_account_with_lots`, `finish_projection`) and
`checks/taker_margin.rs` (`admit_taker`). The tests replay vectors from the engine's
own test suite. docs/margin.md describes the model, its inputs and what V2 does not
expose (in-flight fills, the last settled funding value, ADL multipliers).

All amounts are rUSD. Like the engine, cash is floored and requirements are rounded up
to rUSD's 6 decimals per component, and E18 steps are reproduced where it rounds.
"""

from typing import Iterable, Iterator

import asyncio
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext

from agent_kit.scripts._common import client_hook, require_price

RUSD_DECIMALS = 6
# The engine's V1 policy constant (`[risk] reserve_rate`), applied once to the notional of
# the order and of the account's matched-but-unsettled fills. V2 does not publish it.
SETTLEMENT_RESERVE_RATE = Decimal("0.01")
HEADROOM_VAR = "REYA_MARGIN_HEADROOM"
# Fraction of the order market's position notional after the fill that sizing leaves spare,
# for the mark moving between the kit's read and the engine's check.
DEFAULT_HEADROOM = Decimal("0.002")
MAX_HEADROOM = Decimal("0.1")
# Fee tiers only refine estimates, so a slow read gives up rather than hold up an order.
FEE_TIERS_TIMEOUT_S = 3.0

LIQUIDATION_THRESHOLD = "below_liquidation_threshold"
INITIAL_MARGIN = "below_initial_margin"
SETTLEMENT_RESERVE = "below_settlement_reserve"

# Echoed by `query margin` so a reader knows what the figures leave out (docs/margin.md).
MARGIN_ASSUMPTIONS = [
    "Kit estimate of the matching engine's margin from public V2 data; the engine's own figures can differ.",
    "Equity = collateral after haircuts (withdrawal-only collateral counts 0) + unrealized PnL at mark "
    "+ unsettled funding, estimated as a debit only (V2 has no last settled funding value).",
    "Liquidation happens below the liquidation margin; margin_usage = liquidation margin / equity (1 = liquidation).",
    "Liquidation price per position: cross margin, every other mark, collateral price and funding unchanged.",
    "Not visible: matched fills that have not settled yet (seconds) and ADL adjustments.",
]

# What the order preview's `*_after` figures assume (docs/margin.md).
AFTER_FILL_BASIS = (
    "full fill at the limit price: the worst case for an order that opens or adds to a position. For such an "
    "order, a fill at a better price or a partial fill leaves margin usage lower and the liquidation price further "
    "from the mark."
)

_E18 = Decimal("1e-18")
_SIZING_STEPS_LIMIT = 256


@contextmanager
def _precise() -> Iterator[None]:
    with localcontext() as ctx:
        ctx.prec = 100
        yield


def _round(value: Decimal, unit: Decimal, rounding: str) -> Decimal:
    return (value / unit).to_integral_value(rounding=rounding) * unit


def _floor(value: Decimal, unit: Decimal = _E18) -> Decimal:
    return _round(value, unit, ROUND_FLOOR)


def _ceil(value: Decimal, unit: Decimal = _E18) -> Decimal:
    return _round(value, unit, ROUND_CEILING)


def _native(decimals: int) -> Decimal:
    return _E18 if decimals >= 18 else Decimal(1).scaleb(-decimals)


# --------------------------------------------------------------------------
# Engine port
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketRisk:
    """One market as the projection reads it (`PositionRiskInputs`)."""

    base: Decimal  # signed settled base after ADL rescaling; 0 for a market without a position
    mark: Decimal
    margin_rate: Decimal  # liquidation-margin rate s = V2 `liquidation_margin_parameter`
    entry: Decimal  # average entry price (Core `lastPrice`); the mark for a flat market


@dataclass(frozen=True)
class AccountMargin:
    """Settled account state (`AccountMargin`): real balance, margin balance (equity), markets."""

    real_balance: Decimal
    margin_balance: Decimal
    markets: dict[str, MarketRisk]


@dataclass(frozen=True)
class Lot:
    """A fill the projection considers: an unsettled one, or the order itself at its limit price."""

    market: str
    qty: Decimal  # signed: positive buys
    price: Decimal


@dataclass(frozen=True)
class Projection:
    mb_floor: Decimal
    rb_floor: Decimal
    lm: Decimal
    im: Decimal
    reserve: Decimal
    h: Decimal  # health: min(mb_floor - lm, rb_floor)
    c: Decimal  # h - reserve


@dataclass(frozen=True)
class Rejection:
    code: str
    error_code: str
    shortfall: Decimal
    engine_message: str


@dataclass
class _Fold:
    buys: Decimal = Decimal(0)
    sells: Decimal = Decimal(0)  # summed signed, so never positive
    instant_loss: Decimal = Decimal(0)
    reserve_notional: Decimal = Decimal(0)
    transitions: int = 0

    def add(self, lot: Lot, mark: Decimal, unit: Decimal) -> None:
        if lot.qty > 0:
            self.buys += lot.qty
        else:
            self.sells += lot.qty
        # A fill priced worse than the mark loses at once; a better one is never credited.
        self.instant_loss += _floor(min(_floor(lot.qty * (mark - lot.price)), Decimal(0)), unit)
        self.reserve_notional += _floor(abs(lot.qty) * lot.price)
        if lot.qty != 0:
            self.transitions += 1

    def merged(self, other: "_Fold") -> "_Fold":
        return _Fold(
            self.buys + other.buys,
            self.sells + other.sells,
            self.instant_loss + other.instant_loss,
            self.reserve_notional + other.reserve_notional,
            self.transitions + other.transitions,
        )


def rounding_allowance(worst_base: Decimal, transitions: int, decimals: int = RUSD_DECIMALS) -> Decimal:
    """Core's average-entry/PnL truncation bound: one native unit per transition for any realistic size."""
    if transitions == 0:
        return Decimal(0)
    drift = (worst_base.to_integral_value(rounding=ROUND_CEILING) + 2) * _E18
    return _ceil(drift, _native(decimals)) * transitions


def project(
    margin: AccountMargin,
    candidates: Iterable[Lot] = (),
    *,
    im_multiplier: Decimal,
    in_flight: Iterable[Lot] = (),
    reserve_rate: Decimal = SETTLEMENT_RESERVE_RATE,
    decimals: int = RUSD_DECIMALS,
) -> Projection:
    """The engine's `project_account_with_lots`: settled state plus unsettled lots plus candidates.

    The kit cannot see unsettled fills, so live callers pass none (docs/margin.md).
    """
    candidates, in_flight = list(candidates), list(in_flight)
    with _precise():
        unit = _native(decimals)
        mb_floor, rb_floor = margin.margin_balance, margin.real_balance
        lm = reserve_notional = Decimal(0)
        for market in sorted(set(margin.markets) | {lot.market for lot in in_flight + candidates}):
            risk = margin.markets.get(market)
            if risk is None:
                raise ValueError(f"No mark or margin rate for {market}")
            optional, candidate = _Fold(), _Fold()
            for lot in in_flight:
                if lot.market == market:
                    optional.add(lot, risk.mark, unit)
            candidate_qty = Decimal(0)
            for lot in candidates:
                if lot.market == market:
                    candidate.add(lot, risk.mark, unit)
                    candidate_qty += lot.qty
            possible = optional.merged(candidate)
            settled = risk.base
            worst = max(abs(settled), abs(settled + possible.buys), abs(settled + possible.sells))
            delta = possible.instant_loss - rounding_allowance(
                worst, int(settled != 0) + possible.transitions, decimals
            )
            mb_floor += delta
            rb_floor += delta
            # Exposure is the worst settle/bust corner of the unsettled lots, shifted by the candidate.
            shifted = settled + candidate_qty
            exposure_base = max(abs(shifted), abs(shifted + optional.buys), abs(shifted + optional.sells))
            lm += _ceil(_ceil(risk.margin_rate * _ceil(exposure_base * risk.mark)), unit)
            if settled != 0:
                # Closing part of the position realizes its loss at mark into the real balance.
                opposite = -possible.sells if settled > 0 else possible.buys
                closable = min(abs(settled), opposite)
                signed = closable if settled > 0 else -closable
                rb_floor += _floor(min(_floor(signed * (risk.mark - risk.entry)), Decimal(0)), unit)
            reserve_notional += possible.reserve_notional
        im = _ceil(_ceil(im_multiplier * lm), unit)
        reserve = _ceil(reserve_rate * reserve_notional, unit)
        h = min(mb_floor - lm, rb_floor)
        return Projection(mb_floor, rb_floor, lm, im, reserve, h, h - reserve)


def _shortfall_text(delta: Decimal) -> str:
    return format(abs(delta).normalize(), "f")


def admit(p0: Projection, pq: Projection, strict_reduce_only: bool) -> Rejection | None:
    """The engine's `admit_taker`: the liquidation-threshold precondition for every request, then
    initial margin before and after and the settlement reserve after, for an ordinary one only.

    `strict_reduce_only` is the reduce-only flag or a TP/SL; an order that merely opposes the
    position is ordinary. The messages are the engine's own.
    """
    if p0.h < 0:
        return Rejection(
            LIQUIDATION_THRESHOLD,
            "ACCOUNT_BELOW_LIQUIDATION_MARGIN_ERROR",
            -p0.h,
            f"This account is {_shortfall_text(p0.h)} rUSD below its liquidation safety threshold. "
            "Add collateral before placing another order.",
        )
    if strict_reduce_only:
        return None
    for projection in (p0, pq):
        if projection.mb_floor < projection.im:
            delta = projection.mb_floor - projection.im
            return Rejection(
                INITIAL_MARGIN,
                "ACCOUNT_BELOW_INITIAL_MARGIN_ERROR",
                -delta,
                f"Insufficient margin: the account is {_shortfall_text(delta)} rUSD below the initial margin "
                "requirement for this order. Add collateral, reduce the order size, or submit a reduce-only order.",
            )
    if pq.h < pq.reserve:
        delta = pq.h - pq.reserve
        return Rejection(
            SETTLEMENT_RESERVE,
            "ACCOUNT_BELOW_INITIAL_MARGIN_ERROR",
            -delta,
            f"Insufficient margin: this order would leave the account {_shortfall_text(delta)} rUSD below its "
            "settlement reserve. Reduce the order size or add collateral.",
        )
    return None


# --------------------------------------------------------------------------
# Account figures
# --------------------------------------------------------------------------


def margin_usage(margin_balance: Decimal, lm: Decimal) -> Decimal | None:
    """Liquidation margin / equity; 1 is the liquidation line. None when equity is not positive."""
    if margin_balance <= 0:
        return None
    with _precise():
        return lm / margin_balance


def liquidation_price(margin: AccountMargin, market: str, lm: Decimal) -> Decimal | None:
    """Cross margin: the price in `market` at which equity equals the liquidation margin, every other
    mark, collateral price and funding unchanged. None for a long that no positive price liquidates."""
    risk = margin.markets[market]
    if risk.base == 0:
        return None
    with _precise():
        buffer = margin.margin_balance - lm
        if risk.base > 0:
            if risk.margin_rate >= 1:
                return None
            price = risk.mark - buffer / (risk.base * (1 - risk.margin_rate))
            return price if price > 0 else None
        return risk.mark + buffer / (-risk.base * (1 + risk.margin_rate))


def distance_to_liquidation(risk: MarketRisk, price: Decimal | None) -> Decimal | None:
    """How far the mark must move against the position to reach `price`, as a fraction of the mark.

    Negative when the account is already past the liquidation line."""
    if price is None or risk.base == 0:
        return None
    with _precise():
        return (risk.mark - price) / risk.mark if risk.base > 0 else (price - risk.mark) / risk.mark


def apply_fill(margin: AccountMargin, lot: Lot, fee: Decimal = Decimal(0)) -> AccountMargin:
    """The account after `lot` fills in full at its price, settled the way Core updates a position."""
    risk = margin.markets[lot.market]
    with _precise():
        base, entry, realized = risk.base, risk.entry, Decimal(0)
        total = base + lot.qty
        if base == 0 or base * lot.qty > 0:
            entry = (base * entry + lot.qty * lot.price) / total
        elif base * total >= 0:
            realized = -lot.qty * (lot.price - entry)
        else:
            realized = base * (lot.price - entry)
            entry = lot.price
        if total == 0:
            entry = risk.mark
        old_upnl = base * (risk.mark - risk.entry)
        new_upnl = total * (risk.mark - entry)
        real_balance = margin.real_balance + realized - fee
        markets = {**margin.markets, lot.market: replace(risk, base=total, entry=entry)}
        return AccountMargin(real_balance, margin.margin_balance + realized - fee - old_upnl + new_upnl, markets)


# --------------------------------------------------------------------------
# V2 assembly
# --------------------------------------------------------------------------


def _decimal(value, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{label} is not a number: {value!r}") from None
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _margin_rate(definition) -> Decimal:
    rate = _decimal(definition.liquidation_margin_parameter, f"{definition.symbol} liquidation margin parameter")
    if not 0 < rate < 1:
        raise ValueError(f"{definition.symbol} liquidation margin parameter must be between 0 and 1")
    return rate


def _side_sign(position) -> int:
    return 1 if getattr(position.side, "value", position.side) == "B" else -1


def im_multiplier_from(definitions: Iterable) -> Decimal | None:
    """The collateral pool's IM multiplier, as V2 publishes it per market (IM = LM rate x multiplier).

    V2 computes both parameters in floating point, so the ratio is rounded to 12 places; markets that
    disagree resolve to the largest. A market without positive parameters is skipped here; one an
    account trades or holds fails on its own rate instead."""
    ratios = set()
    with _precise():
        for md in definitions:
            try:
                lm = _decimal(md.liquidation_margin_parameter, f"{md.symbol} liquidation margin parameter")
                im = _decimal(md.initial_margin_parameter, f"{md.symbol} initial margin parameter")
            except ValueError:
                continue
            if lm > 0 and im > 0:
                ratios.add((im / lm).quantize(Decimal("1e-12")))
    return max(ratios) if ratios else None


@dataclass
class MarginSnapshot:
    """One account's margin as the kit reads it from V2, plus what the figures leave out."""

    account_id: int
    margin: AccountMargin
    im_multiplier: Decimal
    definitions: dict
    collateral: list[dict]
    positions: list[dict]
    unrealized_pnl: Decimal
    funding_estimate: Decimal

    def settled(self) -> Projection:
        return project(self.margin, im_multiplier=self.im_multiplier)


def _funding_estimate(position, signed_qty: Decimal, summary, unit: Decimal) -> Decimal:
    """Unsettled funding, debit only. Core settles funding into realized PnL at every trade and V2
    publishes only the average entry funding value, so this never credits funding and can overstate
    a debit (docs/margin.md). Base multipliers are taken as 1 (no ADL)."""
    market_value = summary.long_funding_value if signed_qty > 0 else summary.short_funding_value
    accrued = -(
        _decimal(market_value, f"{position.symbol} funding value")
        - _decimal(position.avg_entry_funding_value, f"{position.symbol} entry funding value")
    )
    return min(_floor(accrued * signed_qty, unit), Decimal(0))


def build_snapshot(
    account_id: int,
    *,
    balances: Iterable,
    positions: Iterable,
    definitions: Iterable,
    summaries: dict,
    asset_definitions: Iterable = (),
    asset_prices: dict | None = None,
    symbols: Iterable[str] = (),
) -> MarginSnapshot:
    """Assemble the engine's inputs from V2 models. Missing data raises instead of counting as zero.

    `summaries` maps symbol to market summary (mark and funding values); `symbols` adds markets
    without a position, such as the one an order is for.
    """
    definitions = list(definitions)
    by_symbol = {md.symbol: md for md in definitions}
    assets = {a.asset: a for a in asset_definitions}
    asset_prices = asset_prices or {}
    unit = _native(RUSD_DECIMALS)
    with _precise():
        collateral, credit_total = [], Decimal(0)
        for balance in balances:
            if balance.account_id != account_id:
                continue
            amount = _decimal(balance.real_balance, f"{balance.asset} balance")
            if amount == 0:
                continue
            row: dict = {"asset": balance.asset, "balance": balance.real_balance}
            if balance.asset == "RUSD":
                credit = _floor(amount, unit)
                row.update(price_usd=1.0, haircut=0.0, value_usd=float(amount), margin_credit_usd=float(credit))
            else:
                definition = assets.get(balance.asset)
                if definition is None:
                    raise ValueError(
                        f"Asset '{balance.asset}' has a balance but is missing from get_asset_definitions(). "
                        "This is usually a transient API issue: retry the command."
                    )
                price = require_price(asset_prices.get(balance.asset), f"{balance.asset} collateral oracle price")
                haircut = _decimal(definition.price_haircut, f"{balance.asset} haircut")
                value = amount * price
                status = getattr(definition, "status", None)
                if amount < 0:
                    # A debt counts in full; discounting it would overstate the account.
                    credit = _floor(value, unit)
                elif status == "ENABLED":
                    credit = _floor(value * (1 - haircut), unit)
                else:
                    # The engine values most withdrawal-only collateral at zero; the kit credits none of it.
                    credit = Decimal(0)
                    row["credit_note"] = (
                        f"{balance.asset} is {status}: the matching engine gives most such collateral no "
                        "margin credit, so the kit counts none"
                    )
                row.update(
                    price_usd=float(price),
                    haircut=float(haircut),
                    value_usd=float(value),
                    margin_credit_usd=float(credit),
                )
            credit_total += credit
            collateral.append(row)

        markets: dict[str, MarketRisk] = {}
        details, upnl_total, funding_total = [], Decimal(0), Decimal(0)
        for position in positions:
            if position.account_id != account_id:
                continue
            qty = _decimal(position.qty, f"{position.symbol} position qty") * _side_sign(position)
            if qty == 0:
                continue
            definition = by_symbol.get(position.symbol)
            if definition is None:
                raise ValueError(f"Position symbol '{position.symbol}' is missing from get_perp_market_definitions().")
            summary = summaries.get(position.symbol)
            if summary is None:
                raise ValueError(f"Position symbol '{position.symbol}' is missing from get_perp_markets_summary().")
            mark = require_price(summary.mark_price, f"{position.symbol} mark price")
            entry = _decimal(position.avg_entry_price, f"{position.symbol} entry price")
            rate = _margin_rate(definition)
            upnl = _floor(_floor(qty * (mark - entry)), unit)
            funding = _funding_estimate(position, qty, summary, unit)
            upnl_total += upnl
            funding_total += funding
            markets[position.symbol] = MarketRisk(qty, mark, rate, entry)
            details.append(
                {
                    "symbol": position.symbol,
                    "qty": qty,
                    "qty_text": position.qty,
                    "mark": mark,
                    "unrealized_pnl": upnl,
                    "funding_estimate": funding,
                }
            )

        for symbol in symbols:
            if symbol in markets:
                continue
            definition = by_symbol.get(symbol)
            if definition is None:
                raise ValueError(f"Unknown symbol '{symbol}'. Run: query.py prices to see available symbols.")
            summary = summaries.get(symbol)
            if summary is None:
                raise ValueError(f"'{symbol}' is missing from the market summaries.")
            mark = require_price(summary.mark_price, f"{symbol} mark price")
            rate = _margin_rate(definition)
            markets[symbol] = MarketRisk(Decimal(0), mark, rate, mark)

        multiplier = im_multiplier_from(definitions)
        if multiplier is None and markets:
            raise ValueError("No perp market definition publishes margin parameters; cannot compute initial margin")
        real_balance = credit_total + funding_total
        margin = AccountMargin(real_balance, real_balance + upnl_total, markets)
        return MarginSnapshot(
            account_id=account_id,
            margin=margin,
            # Without markets there is no margin requirement for a multiplier to scale.
            im_multiplier=multiplier or Decimal(1),
            definitions=by_symbol,
            collateral=collateral,
            positions=details,
            unrealized_pnl=upnl_total,
            funding_estimate=funding_total,
        )


async def load_snapshot(client, account_id: int, *, symbols: Iterable[str] = (), summaries: dict | None = None):
    """Read everything `build_snapshot` needs. Market summaries are fetched only when a needed one is missing.

    The runner's client serves balances, positions, marks and collateral prices from its warm
    state when they are within its bounds (runner.RunnerClient.warm_margin_inputs); whatever it
    cannot serve is read here, as for every other client.
    """
    symbols = list(symbols)
    hook = client_hook(client, "warm_margin_inputs")
    warm = hook(account_id, symbols) if hook is not None else None
    if warm is None:
        balances = [b for b in await client.get_account_balances() if b.account_id == account_id]
        positions = [p for p in await client.get_positions() if p.account_id == account_id and Decimal(p.qty) != 0]
        warm_summaries, warm_prices = {}, None
    else:
        balances = [b for b in warm["balances"] if b.account_id == account_id]
        positions = [p for p in warm["positions"] if p.account_id == account_id and Decimal(p.qty) != 0]
        warm_summaries, warm_prices = warm["summaries"], warm["asset_prices"]
    definitions = await client.reference.get_perp_market_definitions()
    summaries = {**warm_summaries, **(summaries or {})}
    if any(symbol not in summaries for symbol in [p.symbol for p in positions] + symbols):
        for summary in await client.markets.get_perp_markets_summary():
            summaries.setdefault(summary.symbol, summary)
    asset_definitions: list = []
    asset_prices: dict = {}
    collateral = [b.asset for b in balances if b.asset != "RUSD" and Decimal(b.real_balance) != 0]
    if collateral:
        asset_definitions = await client.reference.get_asset_definitions()
        if warm_prices is not None and all(asset in warm_prices for asset in collateral):
            asset_prices = warm_prices
        else:
            asset_prices = {p.asset: p.oracle_price for p in await client.markets.get_asset_oracle_prices()}
    return build_snapshot(
        account_id,
        balances=balances,
        positions=positions,
        definitions=definitions,
        summaries=summaries,
        asset_definitions=asset_definitions,
        asset_prices=asset_prices,
        symbols=symbols,
    )


async def taker_fee_rate(client) -> Decimal | None:
    """The highest taker fee across the fee tiers: an upper bound for the account's own rate. None if unreadable."""
    try:
        tiers = await asyncio.wait_for(client.reference.get_fee_tier_parameters(), timeout=FEE_TIERS_TIMEOUT_S)
    except Exception:  # The fee only refines the after-fill estimate; it never gates or delays an order.
        return None
    return max_taker_fee(tiers)


def max_taker_fee(tiers) -> Decimal | None:
    try:
        rates = [Decimal(str(t.taker_fee)) for t in tiers]
    except Exception:
        return None
    rates = [r for r in rates if r.is_finite() and r >= 0]
    return max(rates) if rates else None


def margin_headroom() -> Decimal:
    raw = os.environ.get(HEADROOM_VAR, "").strip()
    if not raw:
        return DEFAULT_HEADROOM
    try:
        value = Decimal(raw)
    except InvalidOperation:
        value = Decimal("NaN")
    if not value.is_finite() or not 0 <= value < MAX_HEADROOM:
        raise ValueError(
            f"{HEADROOM_VAR} must be a fraction from 0 to below {MAX_HEADROOM} (default {DEFAULT_HEADROOM})"
        )
    return value


# --------------------------------------------------------------------------
# Order check and sizing
# --------------------------------------------------------------------------


def _usd(value: Decimal | None) -> float | None:
    return None if value is None else float(value.quantize(Decimal("0.01"), rounding=ROUND_FLOOR))


def _ratio(value: Decimal | None, places: str = "0.0001") -> float | None:
    return None if value is None else float(value.quantize(Decimal(places), rounding=ROUND_CEILING))


def _toward_mark(price: Decimal | None, risk: MarketRisk, tick: Decimal) -> str | None:
    """A liquidation price rounded onto the tick toward the mark, so it never reads farther away."""
    if price is None:
        return None
    rounded = _round(price, tick, ROUND_CEILING if risk.base > 0 else ROUND_FLOOR)
    return format(rounded.normalize(), "f")


def _distance_pct(distance: Decimal | None) -> float | None:
    return None if distance is None else float((distance * 100).quantize(Decimal("0.01"), rounding=ROUND_FLOOR))


def position_risk(snapshot: MarginSnapshot, symbol: str, lm: Decimal | None = None) -> dict:
    """Liquidation price and distance for one held market, on the settled account."""
    margin = snapshot.margin
    lm = snapshot.settled().lm if lm is None else lm
    risk = margin.markets[symbol]
    price = liquidation_price(margin, symbol, lm)
    tick = _decimal(snapshot.definitions[symbol].tick_size, f"{symbol} tick size")
    with _precise():
        own_lm = _ceil(_ceil(risk.margin_rate * _ceil(abs(risk.base) * risk.mark)), _native(RUSD_DECIMALS))
    return {
        "liquidation_margin_req_usd": float(own_lm),
        "liquidation_price_usd": _toward_mark(price, risk, tick),
        "distance_to_liquidation_pct": _distance_pct(distance_to_liquidation(risk, price)),
    }


def account_figures(snapshot: MarginSnapshot) -> dict:
    """Account-level equity, requirements, usage and buffers on the settled account."""
    p0 = snapshot.settled()
    equity = snapshot.margin.margin_balance
    usage = margin_usage(equity, p0.lm)
    return {
        "equity_usd": _usd(equity),
        "real_balance_usd": _usd(snapshot.margin.real_balance),
        "unrealized_pnl_usd": _usd(snapshot.unrealized_pnl),
        "unsettled_funding_estimate_usd": _usd(snapshot.funding_estimate),
        "liquidation_margin_req_usd": float(p0.lm),
        "initial_margin_req_usd": float(p0.im),
        "margin_usage": _ratio(usage),
        "margin_usage_pct": None if usage is None else float((usage * 100).quantize(Decimal("0.01"), ROUND_CEILING)),
        "liquidatable": equity < p0.lm,
        "margin_buffer_usd": _usd(equity - p0.lm),
        "initial_margin_buffer_usd": _usd(equity - p0.im),
        "health_usd": _usd(p0.h),
        "im_multiplier": float(snapshot.im_multiplier),
        "settlement_reserve_rate": float(SETTLEMENT_RESERVE_RATE),
    }


@dataclass(frozen=True)
class OrderCheck:
    p0: Projection
    pq: Projection
    rejection: Rejection | None
    after: AccountMargin
    headroom: Decimal

    @property
    def tight(self) -> bool:
        return self.rejection is None and self.pq.c < self.headroom


def check_order(
    snapshot: MarginSnapshot,
    *,
    symbol: str,
    is_buy: bool,
    qty: Decimal,
    price: Decimal,
    reduce_only: bool,
    headroom_rate: Decimal,
    fee_rate: Decimal | None,
) -> OrderCheck:
    """The engine's verdict on this order filling in full at `price` (its limit), and the account after."""
    signed = qty if is_buy else -qty
    lot = Lot(symbol, signed, price)
    p0 = project(snapshot.margin, im_multiplier=snapshot.im_multiplier)
    pq = project(snapshot.margin, [lot], im_multiplier=snapshot.im_multiplier)
    risk = snapshot.margin.markets[symbol]
    with _precise():
        fee = _ceil(qty * risk.mark * (fee_rate or 0), _native(RUSD_DECIMALS))
        after = apply_fill(snapshot.margin, lot, fee)
        headroom = headroom_rate * abs(risk.base + signed) * risk.mark
    return OrderCheck(p0, pq, admit(p0, pq, reduce_only), after, headroom)


def adds_exposure(snapshot: MarginSnapshot, symbol: str, is_buy: bool) -> bool:
    base = snapshot.margin.markets[symbol].base
    return base == 0 or (base > 0) == is_buy


def size_order(
    snapshot: MarginSnapshot,
    *,
    symbol: str,
    is_buy: bool,
    price: Decimal,
    step: Decimal,
    headroom_rate: Decimal,
    fee_rate: Decimal | None,
    target_usage: Decimal | None = None,
) -> Decimal:
    """The largest qty on the step grid that the engine would admit with headroom to spare, and, with
    `target_usage`, that leaves margin usage at or below it after the fill. 0 when nothing fits.

    Only for an order that opens or adds to a position: every requirement then grows with the size.
    """
    if not adds_exposure(snapshot, symbol, is_buy):
        raise ValueError("Sizing by margin is for orders that open or add to a position; close with --reduce_only")

    def fits(steps: int) -> bool:
        check = check_order(
            snapshot,
            symbol=symbol,
            is_buy=is_buy,
            qty=step * steps,
            price=price,
            reduce_only=False,
            headroom_rate=headroom_rate,
            fee_rate=fee_rate,
        )
        if check.rejection is not None or check.tight:
            return False
        if target_usage is None:
            return True
        after_lm = project(check.after, im_multiplier=snapshot.im_multiplier).lm
        usage = margin_usage(check.after.margin_balance, after_lm)
        return usage is not None and usage <= target_usage

    if not fits(1):
        return Decimal(0)
    low, high = 1, 2
    for _ in range(_SIZING_STEPS_LIMIT):
        if not fits(high):
            break
        low, high = high, high * 2
    else:
        raise ValueError("Could not bound the order size; check the account and market data")
    while high - low > 1:
        middle = (low + high) // 2
        if fits(middle):
            low = middle
        else:
            high = middle
    return step * low


def order_margin(snapshot: MarginSnapshot, check: OrderCheck, *, symbol: str, reduce_only: bool) -> dict:
    """The preview's `margin` block for one order."""
    after_lm = project(check.after, im_multiplier=snapshot.im_multiplier).lm
    risk_after = check.after.markets[symbol]
    price_after = liquidation_price(check.after, symbol, after_lm)
    tick = _decimal(snapshot.definitions[symbol].tick_size, f"{symbol} tick size")
    if check.rejection is not None:
        status = check.rejection.code
    elif reduce_only:
        status = "ok"
    else:
        status = "tight" if check.tight else "ok"
    return {
        "engine_check": status,
        "engine_error_code": check.rejection.error_code if check.rejection else None,
        "engine_message": check.rejection.engine_message if check.rejection else None,
        "shortfall_usd": float(check.rejection.shortfall) if check.rejection else None,
        "equity_before_usd": _usd(snapshot.margin.margin_balance),
        "equity_after_usd": _usd(check.after.margin_balance),
        "margin_usage_before": _ratio(margin_usage(snapshot.margin.margin_balance, check.p0.lm)),
        "margin_usage_after": _ratio(margin_usage(check.after.margin_balance, after_lm)),
        "liquidation_margin_after_usd": float(after_lm),
        "initial_margin_after_usd": float(check.pq.im),
        "settlement_reserve_usd": float(check.pq.reserve),
        "health_after_usd": _usd(check.pq.h),
        "reserve_headroom_usd": _usd(check.pq.c),
        "liquidation_price_after_usd": _toward_mark(price_after, risk_after, tick),
        "distance_to_liquidation_after_pct": _distance_pct(distance_to_liquidation(risk_after, price_after)),
        "after_fill_basis": AFTER_FILL_BASIS,
    }
