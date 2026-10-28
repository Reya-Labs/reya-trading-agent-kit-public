#!/usr/bin/env python3
"""
Reya Agent Kit — Write commands (live execution).

All output is JSON to stdout. Errors are returned as {"error": "..."}.

Usage:
    python -m agent_kit.scripts.trade order market ETHRUSDPERP --side long --qty 0.01
    python -m agent_kit.scripts.trade order market ETHRUSDPERP --side long --notional 100
    python -m agent_kit.scripts.trade order limit ETHRUSDPERP --side long --price 2000 --qty 0.05
    python -m agent_kit.scripts.trade order limit ETHRUSDPERP --side long --price 2000 --notional 100
    python -m agent_kit.scripts.trade order market ETHRUSDPERP --side long --notional 100 --preview
    python -m agent_kit.scripts.trade order market ETHRUSDPERP --side long --qty 0.05 --sl 0.35% --tp 0.5% [--preview]
    python -m agent_kit.scripts.trade order cancel --order_id <id>
    python -m agent_kit.scripts.trade order cancel_all (--symbol ETHRUSDPERP | --all) [--preview]
    python -m agent_kit.scripts.trade order tp ETHRUSDPERP --side long --trigger_px 2300 --limit_px 2295 --time_in_force IOC [--force]
    python -m agent_kit.scripts.trade order sl ETHRUSDPERP --side long --trigger_px 2000 --limit_px 1995 --time_in_force IOC [--force]
    python -m agent_kit.scripts.trade positions close_all (--symbol ETHRUSDPERP | --all) [--slippage 0.01] [--preview] [--force]
"""

from typing import NamedTuple

import argparse
import asyncio
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar
from decimal import Decimal

from sdk.open_api.models.order_status import OrderStatus
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.time_in_force import TimeInForce
from sdk.reya_rest_api.models.orders import LimitOrderParameters, ModifyOrderParameters, TriggerOrderParameters

from agent_kit.scripts import _trigger_band
from agent_kit.scripts._book import ioc_book, shortfall_warning, unfilled, unfilled_message
from agent_kit.scripts._common import (
    DEFAULT_SLIPPAGE,
    MAX_LEVERAGE_VAR,
    MAX_ORDER_NOTIONAL_VAR,
    JsonArgumentParser,
    check_slippage_guardrail,
    client_hook,
    exception_result,
    limit_price_band_refusal,
    market_categories,
    order_size_limits,
    output_error,
    output_json,
    output_result,
    positive_decimal_arg,
    require_owned_account,
    require_price,
    retain_result,
    slippage_arg,
    symbol_arg,
    underlying_hours_warning,
)
from agent_kit.scripts._env import get_trading_config, load_env
from agent_kit.scripts._executions import add_settled_fees
from agent_kit.scripts._live import indexing_ceiling
from agent_kit.scripts._margin import (
    adds_exposure,
    check_order,
    load_snapshot,
    margin_headroom,
    order_margin,
    size_order,
    taker_fee_rate,
)
from agent_kit.scripts._sizing import below_minimum_message, notional_sizing
from agent_kit.scripts._writer import CoordinatedTradingClient as ReyaTradingClient

_shared_client: ContextVar[object | None] = ContextVar("shared_trading_client", default=None)


OFF_STEP_CLOSE_HINT = (
    "This position is not a multiple of the market's quantity step (ADL rescaling can leave one). The API rejects "
    "off-step quantities on every order, reduce-only included, so no order closes it exactly. To shrink it, run "
    "`order market <symbol> --side <opposite side> --qty <position qty> --reduce_only`: the kit rounds the "
    "quantity down to the step, and only the part below one step stays open."
)
# Whole-position triggers: they only ever close, and the venue retires them once the position is flat.
PROTECTIVE_ORDER_TYPES = ("TAKE_PROFIT", "STOP_LOSS")

# An order's fill is in its reply about a second before position reads show it, so a
# TP/SL placed straight after an entry waits until the position does (reads only): inside
# the runner on its verified WebSocket feed, otherwise on REST reads every POSITION_POLL_S.
POSITION_POLL_S = 0.5
# Both waits (after an `order market --sl/--tp` entry, and in `order sl` / `order tp`) end at the first
# evidence, within the deployment's indexing ceiling (_live.indexing_ceiling: 15 s on mainnet, 30 s
# elsewhere). REST reads stay one per 0.5 s poll, so their cap grows with the ceiling.
ENTRY_POSITION_READS_PER_S = 2
TRIGGER_READBACK_READS, TRIGGER_READBACK_WAIT_S = 5, 2
# An entry without --sl/--tp reads open orders once, after the send, to say whether a stop covers it; a
# read slower than this reports "unknown" rather than hold back the result (or a runner command's bound).
ARMED_READ_TIMEOUT_S = 2.0
DEFAULT_TRIGGER_SLIPPAGE = Decimal("0.01")
# A protected entry's child limit sits at most this far from its trigger: the venue's TP/SL band
# (_trigger_band: 300 bps for every market, verified 21 Oct 2026), so the kit's own legs always fit it.
MAX_TRIGGER_SLIPPAGE = _trigger_band.TRIGGER_BAND


@asynccontextmanager
async def trading_client():
    shared = _shared_client.get()
    if shared is not None:
        yield shared
    else:
        async with ReyaTradingClient(config=get_trading_config(require_private_key=True)) as client:
            yield client


async def _get_market_params(client: ReyaTradingClient, symbol: str) -> dict:
    """Fetch qty_step_size, tick_size, min_order_qty for a symbol, as exact Decimals, and its V2 categories."""
    market_defs = await client.reference.get_perp_market_definitions()
    market = next((m for m in market_defs if m.symbol == symbol), None)
    if market is None:
        raise ValueError(f"Unknown symbol '{symbol}'. Run: query.py prices to see available symbols.")
    return {
        "qty_step_size": Decimal(market.qty_step_size),
        "tick_size": Decimal(market.tick_size),
        "min_order_qty": Decimal(market.min_order_qty),
        "categories": market_categories(market),
    }


def _note_underlying_hours(plan: dict, params: dict, reduce_only: bool = False) -> None:
    """Add `market_hours_warning` to an order's plan (so its preview and result) when the underlying has hours.

    A reduce-only order only lowers exposure, so it carries no warning; the position's own reads still do.
    """
    warning = None if reduce_only else underlying_hours_warning(plan["symbol"], params.get("categories"))
    if warning:
        plan["market_hours_warning"] = warning


def _fmt(value: Decimal) -> str:
    """Fixed-point string for SDK payloads and response echoes — never scientific notation."""
    return format(value.normalize(), "f")


def _quantize(value: Decimal, step: Decimal, direction: str) -> Decimal:
    """Quantize value onto the step grid — exact for any step size, including non-powers of 10.

    direction: 'down' (floor), 'up' (ceil), or 'nearest' (ties round up, away from floor).
    """
    if value < 0:
        raise ValueError(f"Cannot quantize negative value {value}.")
    remainder = value % step
    if remainder == 0:
        return value
    floor_val = value - remainder
    if direction == "down":
        return floor_val
    if direction == "up":
        return floor_val + step
    return floor_val + step if remainder * 2 >= step else floor_val


def _round_qty(raw_qty: Decimal, qty_step: Decimal, min_qty: Decimal, *, reduce_only: bool = False) -> Decimal:
    """Round quantity down to the nearest step size. Raises if below minimum.

    A reduce-only order is exempt from the minimum, as at the API, so it can close a residual
    below it; it still never rounds to zero.
    """
    qty = _quantize(raw_qty, qty_step, "down")
    if reduce_only and qty <= 0:
        raise ValueError(f"Calculated qty {_fmt(qty)} rounds to zero at the step {_fmt(qty_step)}. Increase --qty.")
    if not reduce_only and qty < min_qty:
        raise ValueError(
            f"Calculated qty {_fmt(qty)} is below minimum order qty {_fmt(min_qty)}. " f"Increase --qty or --notional."
        )
    return qty


def _qty_for_size(
    args: argparse.Namespace, price: Decimal, qty_step: Decimal, min_qty: Decimal, *, priced_at: str
) -> tuple[Decimal, dict | None]:
    """The qty for --qty or --notional; for --notional also the `sizing` block: what the dollars really buy.

    `price` is what --notional divides by, named by `priced_at` (`mark` or `limit`).
    """
    reduce_only = bool(getattr(args, "reduce_only", False))
    if args.notional is None:
        return _round_qty(args.qty, qty_step, min_qty, reduce_only=reduce_only), None
    floored = _quantize(args.notional / price, qty_step, "down")
    if not reduce_only and floored < min_qty:
        raise ValueError(
            below_minimum_message(args.notional, floored, price, min_qty, symbol=args.symbol, priced_at=priced_at)
        )
    qty = _round_qty(args.notional / price, qty_step, min_qty, reduce_only=reduce_only)
    return qty, notional_sizing(args.notional, qty, price, qty_step, symbol=args.symbol, priced_at=priced_at)


def _round_price(price: Decimal, tick_size: Decimal, direction: str = "nearest") -> Decimal:
    """Round price to tick size. direction: 'up', 'down', or 'nearest'."""
    return _quantize(price, tick_size, direction)


def _trigger_above(trigger_type: OrderType, position_side: str) -> bool:
    """A long's take-profit and a short's stop-loss sit above the price; the other two below."""
    return (trigger_type == OrderType.TAKE_PROFIT) == (position_side == "long")


def _fires_at_once(trigger_type: OrderType, position_side: str, trigger_px: Decimal, mark_price: Decimal) -> bool:
    return trigger_px <= mark_price if _trigger_above(trigger_type, position_side) else trigger_px >= mark_price


def _take_profit_loss_warning(position_side: str, limit_px: Decimal, entry_px: Decimal, fix: str) -> str | None:
    """Warn when a take-profit's child limit is past the entry, so it can close the position at a loss.

    The trigger fires on the mark, but the child fills against the order book at any price up
    to its limit; on a thin book that can be the losing side of the entry. None otherwise.
    """
    if entry_px <= 0:
        return None
    if limit_px >= entry_px if position_side == "long" else limit_px <= entry_px:
        return None
    distance = abs((limit_px - entry_px) / entry_px * 100).quantize(Decimal("0.01"))
    relation = "below" if position_side == "long" else "above"
    return (
        f"The take-profit's child limit {_fmt(limit_px)} is {distance}% {relation} the {position_side}'s entry "
        f"{_fmt(entry_px)}. It fires on the mark but fills against the order book at any price up to that limit, "
        f"so on a thin book it can close the position at a loss. {fix}"
    )


def _check_trigger_plausibility(
    trigger_type: OrderType,
    position_side: str,
    trigger_px: Decimal,
    mark_price: Decimal,
    force: bool,
) -> str | None:
    """Refuse TP/SL triggers that would fire immediately at the current mark price.

    A take-profit must sit on the profitable side of the current price and a
    stop-loss on the losing side; anything else fires the moment it lands and
    attempts to close the whole position at the chosen limit. Returns None when plausible, raises
    ValueError when implausible, or returns a warning string when implausible
    but force=True.
    """
    relation = "above" if _trigger_above(trigger_type, position_side) else "below"
    kind = "take-profit" if trigger_type == OrderType.TAKE_PROFIT else "stop-loss"
    if not _fires_at_once(trigger_type, position_side, trigger_px, mark_price):
        return None
    message = (
        f"A {kind} for a {position_side} position must be {relation} the current mark price "
        f"({_fmt(trigger_px)} vs {_fmt(mark_price)}); it would fire immediately and close the "
        f"whole position at the chosen limit if liquidity is available. To close intentionally, use positions close_all --symbol. "
        f"Re-run with --force if this is intentional."
    )
    if force:
        return message
    raise ValueError(message)


def _check_position_match(
    symbol: str,
    position_side: str,
    actual_side: str | None,
    account_id: int,
    force: bool,
    wait_s: float,
) -> str | None:
    """Refuse TP/SL triggers that don't match an open position on the configured account.

    A trigger with no position behind it protects nothing — the agent believes
    the exposure is bracketed when it isn't — and a trigger whose --side
    disagrees with the actual position derives the wrong close direction and
    the wrong immediate-fire relation. Returns None when matched, raises
    ValueError when not, or returns a warning string when not matched but
    force=True. `wait_s` is how long the position was read for.
    """
    if actual_side == position_side:
        return None
    if actual_side is None:
        message = (
            f"No open position on {symbol} for account {account_id} after reading for "
            f"{wait_s:g} seconds; this trigger would protect nothing, so none was sent. Check "
            f"`query positions --symbol {symbol}` and the entry's filled_qty; never repeat the entry to get a "
            f"position. To open a position with its stop-loss and take-profit, use `order market --sl/--tp`."
        )
    else:
        message = (
            f"The open position on {symbol} is {actual_side}, but --side says {position_side}; the "
            f"trigger's close direction and immediate-fire check would both be wrong. Re-run with "
            f"--side {actual_side}, or with --force if this is intentional."
        )
    if force:
        return message
    raise ValueError(message)


def _position_side(position) -> str:
    return "long" if position.side.value == "B" else "short"


def _open_position(positions, account_id: int, symbol: str):
    return next(
        (p for p in positions if p.account_id == account_id and p.symbol == symbol and Decimal(p.qty) > 0),
        None,
    )


def _published_position(client, account_id: int, symbol: str):
    """Inside the runner, the position as its verified WebSocket feed published it; otherwise None."""
    warm = client_hook(client, "warm_position")
    return None if warm is None else warm(account_id, symbol)


async def _await_position(
    client, account_id: int, symbol: str, accept, *, reads: int, timeout: float, feed: bool = True
):
    """Wait until `accept(position)` holds, within `timeout` seconds.

    Inside the runner, while its WebSocket wallet state is verified (and `feed` allows it),
    the position is the one the feed published, checked now and at each update without a
    read. When that state is or becomes unavailable, REST reads take over for the rest of
    the bound: at most `reads` of them. Returns (last position seen or None, accepted).
    Only reads are repeated; nothing is sent. A failed read is retried within the bound;
    raises TimeoutError, or the last read error, when no position was seen.
    """
    seen: list = []
    errors: list = []
    warm = client_hook(client, "warm_position") if feed else None
    if warm is not None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (published := warm(account_id, symbol)) is not None:
            seen.append(published.position)
            if accept(published.position):
                return published.position, True
            if loop.time() >= deadline:
                return published.position, False
            await client.warm_wallet_changed(deadline - loop.time())
        timeout = deadline - loop.time()

    async def poll() -> bool:
        for attempt in range(reads):
            try:
                position = _open_position(await client.get_positions(), account_id, symbol)
            except Exception as exc:
                errors.append(exc)
            else:
                seen.append(position)
                if accept(position):
                    return True
            if attempt < reads - 1:
                await asyncio.sleep(POSITION_POLL_S)
        return False

    if timeout <= 0:  # the feed went away at the deadline: no time left to read
        if not seen:
            raise TimeoutError
        return seen[-1], False
    try:
        accepted = await asyncio.wait_for(poll(), timeout=timeout)
    except TimeoutError:
        if not seen:
            raise
        accepted = False
    if not seen:
        raise errors[-1]
    return seen[-1], accepted


def _usd(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _leverage(gross: Decimal, value: Decimal) -> float | None:
    return float((gross / value).quantize(Decimal("0.01"))) if value > 0 else None


def _vs_mark_pct(price: Decimal, mark: Decimal) -> float:
    """Signed distance of an order's price from the mark, in percent (positive: above)."""
    return float(((price - mark) / mark * 100).quantize(Decimal("0.01")))


def _exposure(snapshot, symbol: str) -> tuple[Decimal, Decimal, Decimal]:
    """(account equity, gross notional of positions in other markets, signed position qty in `symbol`).

    Equity is Core's margin balance as the kit estimates it: collateral after haircuts as Core
    counts it and unrealized PnL at mark; V2's real balance already includes funding
    (docs/margin.md). Fees are not included.
    """
    markets = snapshot.margin.markets
    other_gross = sum((abs(r.base) * r.mark for m, r in markets.items() if m != symbol), Decimal(0))
    return snapshot.margin.margin_balance, other_gross, markets[symbol].base


def _market_max_leverage(snapshot, symbol: str) -> Decimal | None:
    """The market's own maximum leverage as V2 publishes it (`maxLeverage`, 1 / initial margin rounded); None if
    unknown. The engine's margin check is what enforces it, with the settlement reserve."""
    value = getattr(snapshot.definitions.get(symbol), "max_leverage", None)
    return None if value is None else Decimal(value)


async def _order_plan(
    client,
    args: argparse.Namespace,
    *,
    account_id: int,
    symbol: str,
    is_buy: bool,
    qty: Decimal,
    price: Decimal,
    mark: Decimal,
    summary,
    qty_step: Decimal,
    limit_px: Decimal | None = None,
    resting: bool = False,
    reduce_only: bool = False,
    refusals: list | None = None,
    snapshot=None,
) -> dict:
    """Pre-trade figures, the engine's margin verdict and fat-finger refusals for one order. Reads only.

    `price` is the order's own notional basis (the mark for market orders, the limit
    price for limits); `limit_px` is the price the engine checks a full fill at (the
    slipped bound for market orders). Estimated leverage is gross position notional at
    mark after a full fill divided by the account equity; resting orders are not counted.
    An order that only reduces the position is never refused for size, leverage or margin.
    """
    refusals = list(refusals or [])
    limit_px = price if limit_px is None else limit_px
    if snapshot is None:
        snapshot = await load_snapshot(client, account_id, symbols=[symbol], summaries={symbol: summary})
    value, other_gross, position_qty = _exposure(snapshot, symbol)
    cap, max_leverage = order_size_limits()
    market_max = _market_max_leverage(snapshot, symbol)
    signed = qty if is_buy else -qty
    opposing = position_qty * signed < 0
    # Reported as reducing only when it can; --reduce_only alone is exempt from size checks below.
    reducing = opposing and (reduce_only or abs(signed) <= abs(position_qty))
    if reduce_only and not opposing:
        post_qty = position_qty  # the venue refuses a reduce-only order that cannot reduce
    elif reduce_only and abs(signed) > abs(position_qty):
        post_qty = Decimal(0)
    else:
        post_qty = position_qty + signed
    notional = qty * price
    pre_gross = other_gross + abs(position_qty) * mark
    post_gross = other_gross + abs(post_qty) * mark
    # Only limits the user set in .env refuse for size here; the engine's margin check below applies the market's.
    # Before 0.17.0 the kit capped every order at the account value and leverage at 5x by default.
    allow_large = getattr(args, "allow_large", False)
    if not (reduce_only or reducing):
        if value <= 0:
            refusals.append(
                {
                    "check": "account_value",
                    "message": f"The account's value is {_usd(value):,.2f} USDC; deposit before opening a position.",
                    "override": None,
                }
            )
        else:
            if cap is not None and notional > cap and not allow_large:
                refusals.append(
                    {
                        "check": "max_order_notional",
                        "message": (
                            f"Order notional {_usd(notional):,.2f} USDC is above the per-order cap of "
                            f"{_usd(cap):,.2f} USDC the user set ({MAX_ORDER_NOTIONAL_VAR} in .env). Confirm the "
                            "size with the user, then re-run with --allow_large."
                        ),
                        "override": "--allow_large",
                    }
                )
            if (
                max_leverage is not None
                and post_gross > max_leverage * value
                and post_gross > pre_gross
                and not allow_large
            ):
                allows = "" if market_max is None else f"; {symbol} itself allows up to {_fmt(market_max)}x"
                refusals.append(
                    {
                        "check": "max_leverage",
                        "message": (
                            f"Estimated leverage after this order is {_leverage(post_gross, value)}x, above the "
                            f"{_fmt(max_leverage)}x limit the user set ({MAX_LEVERAGE_VAR} in .env{allows}). Confirm "
                            "the leverage with the user, then re-run with --allow_large."
                        ),
                        "override": "--allow_large",
                    }
                )
    margin = await _margin_block(
        client,
        args,
        snapshot,
        symbol=symbol,
        is_buy=is_buy,
        qty=qty,
        limit_px=limit_px,
        qty_step=qty_step,
        resting=resting,
        reduce_only=reduce_only,
        reducing=reducing,
        refusals=refusals,
    )
    return {
        "account_value_usd": _usd(value),
        "current_leverage": _leverage(pre_gross, value),
        "est_leverage": _leverage(post_gross, value),
        "market_max_leverage": None if market_max is None else float(market_max),
        "max_leverage": None if max_leverage is None else float(max_leverage),
        "max_order_notional_usd": None if cap is None else _usd(cap),
        "max_order_notional_source": None if cap is None else MAX_ORDER_NOTIONAL_VAR,
        "reduces_position": reducing,
        "margin": margin,
        "refused_by": refusals,
    }


async def _margin_block(
    client,
    args: argparse.Namespace,
    snapshot,
    *,
    symbol: str,
    is_buy: bool,
    qty: Decimal,
    limit_px: Decimal,
    qty_step: Decimal,
    resting: bool,
    reduce_only: bool,
    reducing: bool,
    refusals: list,
) -> dict:
    """The engine's verdict on a full fill at `limit_px`; a predicted reject of an ordinary order is refused."""
    headroom, fee_rate = margin_headroom(), await taker_fee_rate(client)
    check = check_order(
        snapshot,
        symbol=symbol,
        is_buy=is_buy,
        qty=qty,
        price=limit_px,
        reduce_only=reduce_only,
        headroom_rate=headroom,
        fee_rate=fee_rate,
    )
    margin = order_margin(snapshot, check, symbol=symbol, reduce_only=reduce_only)
    margin["max_qty"] = (
        _fmt(
            size_order(
                snapshot,
                symbol=symbol,
                is_buy=is_buy,
                price=limit_px,
                step=qty_step,
                headroom_rate=headroom,
                fee_rate=fee_rate,
            )
        )
        if not reduce_only and adds_exposure(snapshot, symbol, is_buy)
        else None
    )
    margin["headroom_rate"] = float(headroom)
    margin["taker_fee_rate_assumed"] = None if fee_rate is None else float(fee_rate)
    if resting:
        margin["engine_checks_at"] = "placement if it crosses the book, otherwise each fill at its own price"
    if check.rejection is not None and reduce_only:
        margin["note"] = (
            "The engine rejects every order, closes included, while the account is below its liquidation safety "
            "threshold. The kit sends this reduce-only order anyway and the engine decides."
        )
    elif check.rejection is not None and not getattr(args, "skip_margin_check", False):
        hint = f" The largest size that fits is {margin['max_qty']}." if margin["max_qty"] not in (None, "0") else ""
        refusals.append(
            {
                "check": "engine_margin",
                "message": (
                    f"The matching engine would reject this order: {check.rejection.engine_message}{hint} "
                    "(Kit estimate from V2 data. Override with --skip_margin_check only when the kit is stricter "
                    "than the engine, for example a collateral the engine credits that this kit version does not.)"
                ),
                "override": "--skip_margin_check",
            }
        )
    if reducing and not reduce_only:
        margin["reduce_only_hint"] = (
            "This order only reduces the position but is not reduce-only, so the engine checks it like a new order "
            "(initial margin and settlement reserve). Add --reduce_only to close."
        )
    return margin


def _stop_before_sending(args: argparse.Namespace, plan: dict) -> bool:
    """Print the plan instead of sending for --preview or a refusal; True means nothing was sent."""
    if getattr(args, "preview", False):
        output_json({**plan, "preview": True, "would_send": not plan["refused_by"]})
        return True
    if plan["refused_by"]:
        output_result({**plan, "error": " ".join(r["message"] for r in plan["refused_by"])})
        return True
    del plan["refused_by"]
    args.order_plan = plan  # reported with the error if the send fails; see _error_result
    return False


CREATE_ACTIONS = ("market", "limit", "tp", "sl")


def _ensure_client_order_id(args) -> int:
    """Give every create a client ID so a lost reply can still be found among the account's orders.

    Random rather than time-based: unique across processes and never wrapping. Assigned only at
    the create call, so requests refused before sending carry none.
    """
    if getattr(args, "client_order_id", None) is None:
        args.client_order_id = secrets.randbelow(2**64 - 1) + 1
    return args.client_order_id


def _error_result(args: argparse.Namespace, exc: Exception) -> dict:
    result = exception_result(exc)
    plan = getattr(args, "order_plan", None)
    if plan is not None:
        # A failed send keeps the kit's figures, so its margin prediction sits next to the venue's reply.
        result = {**plan, **result}
    if getattr(args, "group", None) == "order" and getattr(args, "action", None) in CREATE_ACTIONS:
        if getattr(args, "client_order_id", None) is not None:
            # The order may still have reached the venue: this ID finds it.
            result["client_order_id"] = str(args.client_order_id)
    return result


def _order_result(response, sent_client_order_id: int | None = None) -> dict:
    echoed = response.client_order_id
    return {
        "order_id": response.order_id,
        # Keep the ID the kit sent when the venue does not echo it, so the order stays findable.
        "client_order_id": echoed if echoed is not None or sent_client_order_id is None else str(sent_client_order_id),
        "first_fill_id": response.first_fill_id,
        "fill_count": response.fill_count,
        "status": response.status,
        "filled_qty": response.cum_qty,
        "avg_fill_px": response.avg_fill_px,
        "cancel_reason": response.cancel_reason,
        "cancel_reason_message": response.cancel_reason_message,
    }


def _check_create_result(result: dict, *, require_filled: bool = False) -> None:
    if result["status"] == OrderStatus.UNKNOWN:
        result["error"] = "Unknown order status; query account state before taking further action"
    elif require_filled and result["status"] != OrderStatus.FILLED:
        result["error"] = "IOC did not fill completely; inspect filled_qty and account state before retrying"
    elif result["status"] == OrderStatus.CANCELLED:
        result["error"] = "Order was cancelled; inspect filled_qty and cancel_reason"


def _sizes_by_margin(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "max_size", False)) or getattr(args, "usage", None) is not None


async def _size_by_margin(
    client,
    args: argparse.Namespace,
    snapshot,
    *,
    symbol: str,
    is_buy: bool,
    limit_px: Decimal,
    basis: Decimal,
    step: Decimal,
    min_qty: Decimal,
) -> tuple[Decimal, dict]:
    """The qty for --max_size or --usage: the smallest of what the engine admits with headroom, the
    usage target and, without --allow_large, any per-order cap or leverage limit the user set in .env."""
    if getattr(args, "reduce_only", False):
        raise ValueError("--max_size and --usage size an order that opens or adds to a position, not a reduce-only one")
    if not adds_exposure(snapshot, symbol, is_buy):
        raise ValueError(
            "--max_size and --usage size an order that opens or adds to a position; this side reduces it. "
            "Close with --reduce_only or positions close_all."
        )
    headroom, fee_rate = margin_headroom(), await taker_fee_rate(client)

    def largest(target_usage: Decimal | None = None) -> Decimal:
        return size_order(
            snapshot,
            symbol=symbol,
            is_buy=is_buy,
            price=limit_px,
            step=step,
            headroom_rate=headroom,
            fee_rate=fee_rate,
            target_usage=target_usage,
        )

    limits = {"engine_margin": largest()}
    if args.usage is not None:
        limits["usage"] = largest(args.usage)
    cap, max_leverage = order_size_limits()
    if not getattr(args, "allow_large", False):
        value, other_gross, position_qty = _exposure(snapshot, symbol)
        if cap is not None:
            limits["max_order_notional"] = _quantize(cap / basis, step, "down")
        if max_leverage is not None:
            mark = snapshot.margin.markets[symbol].mark
            room = (max_leverage * value - other_gross) / mark - abs(position_qty)
            limits["max_leverage"] = _quantize(max(room, Decimal(0)), step, "down")
    limited_by = min(limits, key=lambda name: limits[name])
    qty = limits[limited_by]
    sizing = {
        "sized_by": "max_size" if args.max_size else "usage",
        "target_usage": None if args.usage is None else float(args.usage),
        "size_limited_by": limited_by,
        "qty_by_limit": {name: _fmt(value) for name, value in limits.items()},
    }
    if qty < min_qty:
        raise ValueError(
            f"Nothing fits: sizing by {sizing['sized_by']} gives {_fmt(qty)}, below the market minimum "
            f"{_fmt(min_qty)} (limited by {limited_by}). Add collateral, reduce other exposure"
            + (", or raise the target usage" if limited_by == "usage" else "")
            + ("" if limited_by.startswith("engine") or limited_by == "usage" else ", or confirm --allow_large")
            + "."
        )
    return qty, sizing


# --------------------------------------------------------------------------
# Entry protection: `order market --sl/--tp`
# --------------------------------------------------------------------------


class TriggerLevel(NamedTuple):
    text: str
    fraction: Decimal | None  # distance from the entry's average fill price ("0.35%" -> 0.0035)
    price: Decimal | None  # absolute trigger price


def trigger_level_arg(text: str) -> TriggerLevel:
    """argparse type for --sl/--tp: a percent of the entry's fill price ("0.35%") or a price ("5790")."""
    if text.endswith("%"):
        percent = positive_decimal_arg(text[:-1])
        if percent >= 50:
            raise argparse.ArgumentTypeError(f"a percent level must be below 50%, got '{text}'")
        return TriggerLevel(text, percent / 100, None)
    return TriggerLevel(text, None, positive_decimal_arg(text))


def trigger_slippage_arg(text: str) -> Decimal:
    value = positive_decimal_arg(text)
    if value > MAX_TRIGGER_SLIPPAGE:
        raise argparse.ArgumentTypeError(
            f"trigger slippage must be at most {MAX_TRIGGER_SLIPPAGE} (the venue's trigger band); it is a decimal "
            f"fraction (0.01 = 1%), got '{text}'"
        )
    return value


def protection_requested(args: argparse.Namespace) -> bool:
    return getattr(args, "sl", None) is not None or getattr(args, "tp", None) is not None


def entry_position_bounds(client) -> tuple[int, float]:
    """(REST read cap, ceiling in seconds) on a TP/SL's position wait, for the client's deployment.

    It bounds both waits: for an `order market --sl/--tp` entry's fill, and in `order sl` /
    `order tp` for the position with the expected side. The ceiling is the deployment's indexing
    ceiling, the bound the runner also keeps an own fill for. The configured API URL decides;
    REYA_MODE=live requires the mainnet URL and REYA_MODE=devnet refuses it, so the mode agrees.
    """
    wait_s = indexing_ceiling(str(getattr(getattr(client, "config", None), "api_url", "")))
    return int(wait_s * ENTRY_POSITION_READS_PER_S) + 1, wait_s


def _v2_url(client) -> str | None:
    """The trading client's V2 base URL for unsigned reads, or None for the configured default."""
    return getattr(getattr(client, "config", None), "api_url", None) or None


TP_ENTRY_FIX = (
    "To take only a profit, set --tp further from the entry than --trigger_slippage (the child limit sits that "
    "far past the trigger); it may then not fill."
)

# Stop-loss first: it is the leg that protects.
BRACKET_LEGS = (("stop_loss", "sl", OrderType.STOP_LOSS), ("take_profit", "tp", OrderType.TAKE_PROFIT))


def _trigger_kind(trigger_type: OrderType) -> str:
    return "take-profit" if trigger_type == OrderType.TAKE_PROFIT else "stop-loss"


def _bracket_prices(
    level: TriggerLevel,
    trigger_type: OrderType,
    position_side: str,
    reference: Decimal,
    tick_size: Decimal,
    slippage: Decimal,
) -> tuple[Decimal, Decimal]:
    """Trigger price and child limit for one leg; a percent level is measured from `reference`."""
    if level.price is not None:
        raw = level.price
    else:
        fraction = Decimal(level.fraction or 0)
        raw = reference * (1 + fraction) if _trigger_above(trigger_type, position_side) else reference * (1 - fraction)
    trigger_px = _round_price(raw, tick_size, "nearest")
    # The fired child closes the position: a sell below the trigger for a long, a buy above it for a short.
    if position_side == "long":
        limit_px = _round_price(trigger_px * (1 - slippage), tick_size, "up")
    else:
        limit_px = _round_price(trigger_px * (1 + slippage), tick_size, "down")
    if trigger_px <= 0 or limit_px <= 0:
        raise ValueError(f"The {_trigger_kind(trigger_type)} level {level.text} rounds to a zero price")
    return trigger_px, limit_px


def _trigger_command(symbol: str, flag: str, position_side: str, trigger_px: Decimal, limit_px: Decimal) -> str:
    return (
        f"order {flag} {symbol} --side {position_side} --trigger_px {_fmt(trigger_px)} "
        f"--limit_px {_fmt(limit_px)} --time_in_force IOC"
    )


async def _protection_reads(client, account_id: int) -> tuple[list, list]:
    """Positions and open orders for the --sl/--tp checks before an entry.

    Inside the runner, while its WebSocket wallet state is verified, both come from that
    state at one instant: positions with the runner's own acknowledged fills applied, and
    open orders including its acknowledged resting orders the feed has not shown yet. So a
    fill or a trigger that REST does not show yet still counts against the entry. Otherwise,
    or when either is unavailable, REST.
    """
    margin_inputs = client_hook(client, "warm_margin_inputs")
    open_orders = client_hook(client, "warm_open_orders")
    if margin_inputs is not None and open_orders is not None:
        wallet, orders = margin_inputs(account_id, []), open_orders()
        if wallet is not None and orders is not None:
            return wallet["positions"], orders
    positions, orders = await asyncio.gather(client.get_positions(), client.get_open_orders())
    return positions, orders


async def _protection_plan(
    client,
    args: argparse.Namespace,
    *,
    account_id: int,
    position_side: str,
    qty: Decimal,
    mark: Decimal,
    tick_size: Decimal,
) -> tuple[dict, list, object]:
    """Planned TP/SL for an entry, refusals that stop it before sending, and the position before it.

    Reads only. Estimates use the mark; once the entry fills, percent levels are measured
    from its average fill price instead.
    """
    positions, orders = await _protection_reads(client, account_id)
    before = _open_position(positions, account_id, args.symbol)
    armed = {
        o.order_type
        for o in orders
        if o.account_id == account_id
        and o.symbol == args.symbol
        and o.order_type in (OrderType.STOP_LOSS, OrderType.TAKE_PROFIT)
    }
    refusals = []
    if before is not None and _position_side(before) != position_side:
        refusals.append(
            {
                "check": "protection_opposite_position",
                "message": (
                    f"The open {args.symbol} position is {_position_side(before)}, so this {position_side} entry "
                    "would reduce or flip it; --sl/--tp protect only a new or larger position. Close the position "
                    "first, or enter without --sl/--tp."
                ),
                "override": None,
            }
        )
    slippage = getattr(args, "trigger_slippage", DEFAULT_TRIGGER_SLIPPAGE)
    plan: dict = {
        "reference": "avg_fill_px",
        "est_reference_px_usd": float(mark),
        "time_in_force": "IOC",
        "trigger_slippage": float(slippage),
    }
    for key, flag, trigger_type in BRACKET_LEGS:
        level = getattr(args, flag, None)
        if level is None:
            plan[key] = None
            continue
        kind = _trigger_kind(trigger_type)
        if trigger_type in armed:
            refusals.append(
                {
                    "check": "protection_exists",
                    "message": (
                        f"A {kind} is already armed on {args.symbol}. It closes the whole position, this entry "
                        f"included, and the venue allows one per position. Enter without --{flag}, or cancel or "
                        f"modify the armed {kind} first."
                    ),
                    "override": None,
                }
            )
        trigger_px, limit_px = _bracket_prices(level, trigger_type, position_side, mark, tick_size, slippage)
        if _fires_at_once(trigger_type, position_side, trigger_px, mark):
            relation = "above" if _trigger_above(trigger_type, position_side) else "below"
            refusals.append(
                {
                    "check": "protection_fires_at_once",
                    "message": (
                        f"A {kind} for a {position_side} must be {relation} the mark: {_fmt(trigger_px)} vs "
                        f"{_fmt(mark)}. It would fire as soon as it is placed."
                    ),
                    "override": None,
                }
            )
        direction = 1 if position_side == "long" else -1
        leg_warning = None
        if trigger_type == OrderType.TAKE_PROFIT and (before is None or _position_side(before) == position_side):
            held = Decimal(0) if before is None else Decimal(before.qty)
            est_entry = mark if before is None else (held * Decimal(before.avg_entry_price) + qty * mark) / (held + qty)
            est_entry = _round_price(est_entry, tick_size, "nearest")
            leg_warning = _take_profit_loss_warning(position_side, limit_px, est_entry, TP_ENTRY_FIX)
        plan[key] = {
            "level": level.text,
            "est_trigger_px_usd": _fmt(trigger_px),
            "est_limit_px_usd": _fmt(limit_px),
            "est_trigger_vs_mark_pct": _vs_mark_pct(trigger_px, mark),
            # Signed, for this entry's quantity, before fees: at the trigger, and at the child
            # limit (the worst price the fired child accepts).
            "est_pnl_at_trigger_usd": _usd(direction * qty * (trigger_px - mark)),
            "est_pnl_at_limit_usd": _usd(direction * qty * (limit_px - mark)),
        }
        if leg_warning:
            plan[key]["warning"] = leg_warning
    return plan, refusals, before


async def _armed_for_side(client, account_id: int, symbol: str, position_side: str) -> set | None:
    """The TP/SL types already armed on `symbol` for a `position_side` position, or None when unread.

    For an entry sent without --sl/--tp: a stop-loss armed earlier closes the whole position, the
    entry included. Inside the runner the open orders come from its warm state; otherwise, or when
    that is unavailable, one REST read. A failed read is None, never an error: it must not change
    the order's outcome.
    """
    try:
        warm = client_hook(client, "warm_open_orders")
        warm_orders = warm() if warm is not None else None
        orders: list = (
            warm_orders
            if warm_orders is not None
            else await asyncio.wait_for(client.get_open_orders(), timeout=ARMED_READ_TIMEOUT_S)
        )
        # A trigger's own side is the closing side: a sell protects a long, a buy a short.
        close_side = "B" if position_side == "short" else "A"
        return {
            o.order_type
            for o in orders
            if o.account_id == account_id
            and o.symbol == symbol
            and o.order_type in (OrderType.STOP_LOSS, OrderType.TAKE_PROFIT)
            and o.side.value == close_side
            and not o.triggered
        }
    except Exception:
        return None


def _unrequested_protection(symbol: str, position_side: str, armed: set | None, *, filled: bool | None = True) -> dict:
    """`protection` for an entry sent without --sl/--tp: what, if anything, protects the position on Reya.

    `filled`: False when the entry filled nothing, None when its outcome is unknown.
    """
    stop = None if armed is None else OrderType.STOP_LOSS in armed
    target = None if armed is None else OrderType.TAKE_PROFIT in armed
    if filled is False:
        message = "The entry did not fill, so it opened or added nothing."
    elif filled is None:
        if stop:
            then = f"the stop-loss already armed on {symbol} covers it"
        elif stop is False:
            then = f"it has no stop-loss on Reya: none was requested with it and none is armed on {symbol}"
        else:
            then = f"no stop-loss was requested with it: check `query orders --symbol {symbol}` for one on Reya"
        message = f"The entry's outcome is unknown. If `query positions --symbol {symbol}` shows it filled, {then}."
    elif stop:
        message = (
            f"No stop-loss or take-profit was requested with this order. The stop-loss already armed on {symbol} "
            f"closes the whole {position_side} position, this order included."
        )
    elif stop is False:
        message = (
            f"This {position_side} position has no stop-loss on Reya: none was requested with this order (--sl) and "
            f"none is armed on {symbol}."
        )
    else:
        message = (
            f"No stop-loss was requested with this order (--sl), and the kit could not read whether one is armed on "
            f"{symbol}: run `query orders --symbol {symbol}`."
        )
    return {
        "state": "not_requested",
        "stop_loss": None,  # null: not requested with this order
        "take_profit": None,
        "stop_loss_armed": stop,  # one already on the market for this side; null: could not be read
        "take_profit_armed": target,
        "message": message,
    }


async def _read_back_triggers(client, account_id: int, symbol: str, order_ids: set) -> set:
    """The IDs among `order_ids` that open-order reads show armed, within TRIGGER_READBACK_WAIT_S.

    Inside the runner, while its WebSocket wallet state is verified, the open orders are the
    ones the orderChanges feed published, checked now and at each update without a read.
    Never the runner's own acknowledged orders the feed has not shown: an acknowledgement is
    no evidence that the venue lists the order. When that state is or becomes unavailable,
    REST reads take over for the rest of the bound: at most TRIGGER_READBACK_READS of them.
    Only reads are repeated; nothing is sent.
    """
    visible: set = set()

    def shown(orders) -> bool:
        visible.update(
            o.order_id
            for o in orders
            if o.account_id == account_id and o.symbol == symbol and o.order_id in order_ids and not o.triggered
        )
        return visible >= order_ids

    loop = asyncio.get_running_loop()
    deadline = loop.time() + TRIGGER_READBACK_WAIT_S
    warm = client_hook(client, "warm_published_orders")
    if warm is not None:
        while (published := warm(account_id)) is not None:
            if shown(published) or loop.time() >= deadline:
                return visible
            await client.warm_wallet_changed(deadline - loop.time())

    async def poll() -> None:
        for attempt in range(TRIGGER_READBACK_READS):
            try:
                orders = await client.get_open_orders()
            except Exception:
                orders = []
            if shown(orders):
                return
            if attempt < TRIGGER_READBACK_READS - 1:
                await asyncio.sleep(POSITION_POLL_S)

    remaining = deadline - loop.time()
    if remaining <= 0:  # the feed went away at the deadline: no time left to read
        return visible
    try:
        await asyncio.wait_for(poll(), timeout=remaining)
    except TimeoutError:
        pass
    return visible


async def _protect_entry(
    client,
    args: argparse.Namespace,
    *,
    account_id: int,
    position_side: str,
    before,
    result: dict,
    tick_size: Decimal,
    protection: dict,
    baseline=None,
) -> None:
    """Arm the requested stop-loss, then take-profit, once the position includes the entry's fill.

    Evidence, not elapsed time: the position must show the entry's side with a later trade
    sequence number than before the entry. The wait ends at the first evidence, within the
    deployment's ceiling (entry_position_bounds); on a miss nothing is placed and the position
    is reported UNPROTECTED. Never resends the entry or retries a trigger. Fills in
    `protection` in place, so a runner timeout still reports how far it got.

    `baseline` is the runner's WebSocket view of the position just before the send (None
    outside the runner or while its feed is unverified). Only with it does the evidence
    come from the feed, so feed evidence is always compared with the feed's own past.
    """
    symbol = args.symbol
    filled = result.get("filled_qty")
    never_repeat = "Never repeat the entry."
    if result["status"] == OrderStatus.UNKNOWN or filled is None:
        protection.update(
            state="unknown",
            message=(
                f"The entry's outcome is unknown, so no TP/SL was placed. If `query positions --symbol {symbol}` "
                f"shows a {position_side} position, it is UNPROTECTED until you arm it. {never_repeat}"
            ),
        )
        return
    if Decimal(filled) == 0:
        protection.update(state="not_placed", message="The entry did not fill, so no TP/SL was placed.")
        return

    fill_px = result.get("avg_fill_px")
    reference = Decimal(fill_px) if fill_px is not None else Decimal(str(result["mark_price_usd"]))
    protection["reference"] = "avg_fill_px" if fill_px is not None else "mark_before_entry"
    protection["reference_px_usd"] = _fmt(reference)
    slippage = getattr(args, "trigger_slippage", DEFAULT_TRIGGER_SLIPPAGE)
    legs = []
    for key, flag, trigger_type in BRACKET_LEGS:
        level = getattr(args, flag, None)
        if level is None:
            continue
        trigger_px, limit_px = _bracket_prices(level, trigger_type, position_side, reference, tick_size, slippage)
        leg = {
            "level": level.text,
            "trigger_px_usd": _fmt(trigger_px),
            "limit_px_usd": _fmt(limit_px),
            "time_in_force": "IOC",
            "armed": False,
            "command": _trigger_command(symbol, flag, position_side, trigger_px, limit_px),
        }
        protection[key] = leg
        legs.append((trigger_type, trigger_px, limit_px, leg))

    # Before the entry: the plan's read (inside the runner, its warm view) and the feed just before the send.
    known = [] if before is None else [before.last_trade_sequence_number]
    if baseline is not None and baseline.sequence is not None:
        known.append(baseline.sequence)
    before_seq = max(known, default=None)

    def includes_entry(position) -> bool:
        return (
            position is not None
            and _position_side(position) == position_side
            and (before_seq is None or position.last_trade_sequence_number > before_seq)
        )

    reads, wait_s = entry_position_bounds(client)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        position, visible = await _await_position(
            client, account_id, symbol, includes_entry, reads=reads, timeout=wait_s, feed=baseline is not None
        )
    except Exception:
        position, visible = None, False
    protection["position_wait_s"] = round(loop.time() - started, 2)
    protection["position_wait_limit_s"] = wait_s
    if not visible:
        protection.update(
            state="unprotected",
            message=(
                f"Entry filled {filled} {symbol}, but position reads did not show it within "
                f"{wait_s:g} seconds, so no TP/SL was placed: the position is UNPROTECTED. Once "
                f"`query positions --symbol {symbol}` shows the {position_side} position, run: "
                + "; ".join(leg["command"] for *_, leg in legs)
                + f". Or close it with `positions close_all --symbol {symbol}`. {never_repeat}"
            ),
        )
        return
    protection["position"] = {
        "side": position_side,
        "qty": position.qty,
        "last_trade_sequence_number": position.last_trade_sequence_number,
    }
    for trigger_type, _, limit_px, leg in legs:
        if trigger_type == OrderType.TAKE_PROFIT:
            leg_warning = _take_profit_loss_warning(
                position_side, limit_px, Decimal(position.avg_entry_price), TP_ENTRY_FIX
            )
            if leg_warning:
                leg["warning"] = leg_warning

    # Triggers fire on the mark; one placed beyond it would fire at once.
    mark = None
    try:
        summary = await client.markets.get_perp_market_summary(symbol=symbol)
        mark = require_price(summary.mark_price, f"{symbol} mark price")
    except Exception:
        protection["warning"] = "The mark price was unavailable, so the triggers were not checked against it."
    for trigger_type, trigger_px, limit_px, leg in legs:
        kind = _trigger_kind(trigger_type)
        if mark is not None and _fires_at_once(trigger_type, position_side, trigger_px, mark):
            leg["error"] = (
                f"The mark {_fmt(mark)} has already passed the {kind} level {_fmt(trigger_px)}; it would fire at "
                "once, so it was not placed"
            )
            continue
        client_order_id = secrets.randbelow(2**64 - 1) + 1
        leg["client_order_id"] = str(client_order_id)
        try:
            response = await client.create_trigger_order(
                TriggerOrderParameters(
                    symbol=symbol,
                    is_buy=position_side == "short",
                    trigger_px=_fmt(trigger_px),
                    trigger_type=trigger_type,
                    limit_px=_fmt(limit_px),
                    time_in_force=TimeInForce.IOC,
                    client_order_id=client_order_id,
                )
            )
        except Exception as exc:
            leg.update(exception_result(exc))
            venue_message = leg.pop("message", None)
            if leg.get("error_code") not in (None, "API_ERROR"):
                leg["error"] = f"{leg['error_code']}: {leg['error']}"
            if leg.get("error_code") == _trigger_band.BAND_ERROR_CODE:
                band = _trigger_band.rejection(symbol, venue_message, trigger_px, limit_px, tick_size)
                leg["trigger_band"] = band
                leg["error"] += f". {band['explanation']}"
            if leg.get("http_status", 500) >= 500:
                # No definitive rejection: the request may still have reached the venue.
                leg["error"] += (
                    f"; it may still have reached the venue, so check `query orders --symbol {symbol}` for "
                    f"client_order_id {client_order_id} before arming again"
                )
            continue
        acknowledged = _order_result(response, client_order_id)
        leg.update({k: acknowledged[k] for k in ("order_id", "client_order_id", "status", "cancel_reason")})
        leg["armed"] = response.status == OrderStatus.OPEN
        if not leg["armed"]:
            leg["error"] = (
                f"The venue did not confirm the {kind} as armed (status {response.status.value}); check "
                f"`query orders --symbol {symbol}`"
            )

    armed_ids = {leg["order_id"] for *_, leg in legs if leg["armed"]}
    visible_ids = await _read_back_triggers(client, account_id, symbol, armed_ids) if armed_ids else set()
    for *_, leg in legs:
        if leg["armed"]:
            leg["visible"] = leg["order_id"] in visible_ids
    protection["verified"] = bool(armed_ids) and visible_ids >= armed_ids

    armed = ", ".join(
        f"{_trigger_kind(t)} at {leg['trigger_px_usd']} (limit {leg['limit_px_usd']})"
        for t, _, _, leg in legs
        if leg["armed"]
    )
    # Legs run stop-loss first, so a missing stop-loss is always missing[0].
    missing = [(t, leg) for t, _, _, leg in legs if not leg["armed"]]
    if not missing:
        protection.update(
            state="protected",
            message=(
                f"Armed: {armed}. They close the whole {position_side} position ({position.qty} {symbol}) when "
                "they fire."
            ),
        )
        if not protection["verified"]:
            protection["message"] += (
                f" The venue acknowledged them, but open-order reads do not show them yet; check "
                f"`query orders --symbol {symbol}`."
            )
        return
    failed_type, failed = missing[0]
    if failed_type == OrderType.STOP_LOSS or not armed:
        no_band = failed.get("trigger_band", {}).get("reason") == "no_band"
        next_step = (
            f"No TP/SL can be armed on {symbol}: close with `positions close_all --symbol {symbol}`, or keep the "
            "position knowing it has no stop. "
            if no_band
            else f"Arm it with `{failed['command']}`, or close with `positions close_all --symbol {symbol}`. "
        )
        protection.update(
            state="unprotected",
            message=(
                f"Entry filled {filled} {symbol}, but the {_trigger_kind(failed_type)} was not placed "
                f"({failed['error']}): the position is UNPROTECTED"
                + (f" (armed: {armed})" if armed else "")
                + ". "
                + next_step
                + never_repeat
            ),
        )
    else:
        protection.update(
            state="partial",
            message=(
                f"Armed: {armed}. The take-profit was not placed ({failed['error']}); to add it, run "
                f"`{failed['command']}`."
            ),
        )


async def cmd_order_market(args: argparse.Namespace) -> None:
    """
    Execute a market order.

    Internally this is an IOC limit order with an aggressively-slipped price
    to bound the execution price. It can fill partially or not at all. With
    --sl/--tp, the stop-loss and then the take-profit are armed once the
    position includes the fill; see _protect_entry.
    """
    load_env()
    args.order_plan = None  # never report a plan from an earlier call on these args

    is_buy = args.side in ("long", "buy")
    slippage = args.slippage
    check_slippage_guardrail(slippage, args.force)
    bracket = protection_requested(args)
    if bracket and args.reduce_only:
        raise ValueError("--sl/--tp protect a new or larger position; drop them for a --reduce_only order")
    position_side = "long" if is_buy else "short"

    async with trading_client() as client:
        await client.start()

        account_id = await require_owned_account(client)

        # Get market params for rounding
        params = await _get_market_params(client, args.symbol)
        qty_step = params["qty_step_size"]
        tick_size = params["tick_size"]
        min_qty = params["min_order_qty"]

        # The mark price is the execution reference: the order book sets the
        # fill and the venue bounds fills against its mark, not the oracle index.
        price_data = await client.markets.get_perp_market_summary(symbol=args.symbol)
        mark_price = require_price(price_data.mark_price, f"{price_data.symbol} mark price")

        # Compute aggressive limit price with slippage
        if is_buy:
            limit_px = _round_price(mark_price * (1 + slippage), tick_size, "up")
        else:
            limit_px = _round_price(mark_price * (1 - slippage), tick_size, "down")

        limit_px = max(limit_px, tick_size)

        snapshot, sizing = None, None
        if _sizes_by_margin(args):
            snapshot = await load_snapshot(
                client, account_id, symbols=[args.symbol], summaries={args.symbol: price_data}
            )
            qty, sizing = await _size_by_margin(
                client,
                args,
                snapshot,
                symbol=args.symbol,
                is_buy=is_buy,
                limit_px=limit_px,
                basis=mark_price,
                step=qty_step,
                min_qty=min_qty,
            )
        else:
            qty, sizing = _qty_for_size(args, mark_price, qty_step, min_qty, priced_at="mark")

        plan = {
            "action": "order_market",
            "symbol": args.symbol,
            "side": args.side,
            "qty": _fmt(qty),
            "notional_usd": float((qty * mark_price).quantize(Decimal("0.01"))),
            "mark_price_usd": float(mark_price),
            "limit_px_usd": _fmt(limit_px),
            "limit_vs_mark_pct": _vs_mark_pct(limit_px, mark_price),
            "slippage": float(slippage),
            **({"sizing": sizing} if sizing else {}),
        }
        refusals: list = []
        before = None
        if bracket:
            plan["protection_plan"], refusals, before = await _protection_plan(
                client,
                args,
                account_id=account_id,
                position_side=position_side,
                qty=qty,
                mark=mark_price,
                tick_size=tick_size,
            )
        try:
            plan.update(
                await _order_plan(
                    client,
                    args,
                    account_id=account_id,
                    symbol=args.symbol,
                    is_buy=is_buy,
                    qty=qty,
                    price=mark_price,
                    mark=mark_price,
                    summary=price_data,
                    qty_step=qty_step,
                    limit_px=limit_px,
                    reduce_only=args.reduce_only,
                    snapshot=snapshot,
                    refusals=refusals,
                )
            )
        except Exception as exc:
            if not args.reduce_only:
                raise
            # Size checks never apply to reduce-only orders; a failed account read must not block a close.
            plan.update(risk_unavailable=exception_result(exc)["error"], refused_by=[])
        _note_underlying_hours(plan, params, args.reduce_only)
        # An entry without --sl/--tp says whether anything on Reya protects the position. Not for a close.
        unprotected_entry = not bracket and not args.reduce_only and not plan.get("reduces_position")
        if getattr(args, "preview", False):
            # The only book read before a send, and only for a preview: what the visible book holds inside the limit.
            plan["book"] = await ioc_book(
                _v2_url(client), args.symbol, is_buy=is_buy, limit_px=limit_px, tick=tick_size, step=qty_step
            )
            warning = shortfall_warning(plan["book"], qty, is_buy)
            if warning:
                plan["warning"] = warning
            if unprotected_entry:
                armed = await _armed_for_side(client, account_id, args.symbol, position_side)
                plan["protection"] = _unrequested_protection(args.symbol, position_side, armed)
        if _stop_before_sending(args, plan):
            return

        baseline = _published_position(client, account_id, args.symbol) if bracket else None
        response = await client.create_limit_order(
            LimitOrderParameters(
                symbol=args.symbol,
                is_buy=is_buy,
                limit_px=_fmt(limit_px),
                qty=_fmt(qty),
                time_in_force=TimeInForce.IOC,
                reduce_only=args.reduce_only,
                client_order_id=_ensure_client_order_id(args),
            )
        )

        result = {**plan, **_order_result(response, args.client_order_id)}
        retain_result(result)
        # Only an IOC the book did not fill completely reads the book: started now, so its snapshot is close to
        # the send and arming TP/SL never waits for it. It never raises and is bounded by its own timeout.
        book_read = (
            asyncio.create_task(
                ioc_book(_v2_url(client), args.symbol, is_buy=is_buy, limit_px=limit_px, tick=tick_size, step=qty_step)
            )
            if unfilled(result)
            else None
        )
        # Read after the send, so it never delays the order; an IOC places no TP/SL, so the answer is the same.
        armed_read = (
            asyncio.create_task(_armed_for_side(client, account_id, args.symbol, position_side))
            if unprotected_entry
            else None
        )
        if bracket:
            # Estimates are replaced by the placed prices; protection comes before the fee read.
            result.pop("protection_plan", None)
            protection: dict = {
                "state": "pending",
                "message": (
                    "The command stopped while placing TP/SL; read `query orders` and `query positions` "
                    "before acting, and never repeat the entry."
                ),
                "stop_loss": None,  # null: not requested, or nothing was placed
                "take_profit": None,
            }
            result["protection"] = protection
            try:
                await _protect_entry(
                    client,
                    args,
                    account_id=account_id,
                    position_side=position_side,
                    before=before,
                    result=result,
                    tick_size=tick_size,
                    protection=protection,
                    baseline=baseline,
                )
            except Exception as exc:
                # Never lose the entry's outcome to a failure while protecting it.
                protection.update(
                    state="unprotected",
                    message=(
                        f"Protecting the entry failed ({exception_result(exc)['error']}): the position may be "
                        f"UNPROTECTED. Read `query positions --symbol {args.symbol}` and `query orders --symbol "
                        f"{args.symbol}` before arming it; never repeat the entry."
                    ),
                )
        await add_settled_fees(client, result, wait_s=getattr(args, "fees_wait_s", 0))
        if book_read is not None:
            result["book"] = await book_read
        if armed_read is not None:
            unknown = result["status"] == OrderStatus.UNKNOWN or result.get("filled_qty") is None
            filled = None if unknown else Decimal(result["filled_qty"]) > 0
            result["protection"] = _unrequested_protection(args.symbol, position_side, await armed_read, filled=filled)
        _check_create_result(result, require_filled=True)
        message = unfilled_message(result, args.symbol)
        if message:
            result["error"] = message
        if bracket and protection["state"] not in ("protected", "not_placed"):
            entry_error = result.get("error")
            result["error"] = protection["message"] + (f" Entry: {entry_error}" if entry_error else "")
        output_result(result)


async def cmd_order_limit(args: argparse.Namespace) -> None:
    """Place a GTC limit order."""
    load_env()
    args.order_plan = None  # never report a plan from an earlier call on these args
    if protection_requested(args):
        raise ValueError(
            "--sl/--tp attach only to `order market`: a resting limit can fill later or in parts, after this "
            "command has exited. Place the limit; once `query positions` shows the fill, run `order sl` and "
            "`order tp`."
        )

    is_buy = args.side in ("long", "buy")

    async with trading_client() as client:
        await client.start()

        account_id = await require_owned_account(client)

        # Get market params for rounding
        params = await _get_market_params(client, args.symbol)
        qty_step = params["qty_step_size"]
        tick_size = params["tick_size"]
        min_qty = params["min_order_qty"]

        # Round price to tick size
        limit_px = _round_price(args.price, tick_size, "down" if is_buy else "up")
        if limit_px <= 0:
            raise ValueError("--price rounds to zero")

        sizing = None
        if not _sizes_by_margin(args):
            qty, sizing = _qty_for_size(args, limit_px, qty_step, min_qty, priced_at="limit")

        price_data = await client.markets.get_perp_market_summary(symbol=args.symbol)
        mark_price = require_price(price_data.mark_price, f"{price_data.symbol} mark price")
        band = limit_price_band_refusal(limit_px, mark_price, is_buy, getattr(args, "force", False))
        snapshot = None
        if _sizes_by_margin(args):
            snapshot = await load_snapshot(
                client, account_id, symbols=[args.symbol], summaries={args.symbol: price_data}
            )
            qty, sizing = await _size_by_margin(
                client,
                args,
                snapshot,
                symbol=args.symbol,
                is_buy=is_buy,
                limit_px=limit_px,
                basis=limit_px,
                step=qty_step,
                min_qty=min_qty,
            )
        plan = {
            "action": "order_limit",
            "symbol": args.symbol,
            "side": args.side,
            "qty": _fmt(qty),
            "limit_px_usd": _fmt(limit_px),
            "notional_usd": float((qty * limit_px).quantize(Decimal("0.01"))),
            "mark_price_usd": float(mark_price),
            "limit_vs_mark_pct": _vs_mark_pct(limit_px, mark_price),
            **({"sizing": sizing} if sizing else {}),
            **await _order_plan(
                client,
                args,
                account_id=account_id,
                symbol=args.symbol,
                is_buy=is_buy,
                qty=qty,
                price=limit_px,
                mark=mark_price,
                summary=price_data,
                qty_step=qty_step,
                resting=True,
                refusals=[band] if band else [],
                snapshot=snapshot,
            ),
        }
        _note_underlying_hours(plan, params)
        if _stop_before_sending(args, plan):
            return

        response = await client.create_limit_order(
            LimitOrderParameters(
                symbol=args.symbol,
                is_buy=is_buy,
                limit_px=_fmt(limit_px),
                qty=_fmt(qty),
                time_in_force=TimeInForce.GTC,
                post_only=getattr(args, "post_only", False),
                client_order_id=_ensure_client_order_id(args),
            )
        )

        result = {**plan, **_order_result(response, args.client_order_id)}
        retain_result(result)
        await add_settled_fees(client, result, wait_s=getattr(args, "fees_wait_s", 0))
        _check_create_result(result)
        output_result(result)


def _resolve_order(orders, account_id, args):
    matches = [
        order
        for order in orders
        if order.account_id == account_id
        and (not getattr(args, "symbol", None) or order.symbol == args.symbol)
        and (order.order_id == args.order_id if args.order_id else order.client_order_id == str(args.client_order_id))
    ]
    if not matches:
        raise ValueError("Order is not open on the configured account/symbol; query orders/history before retrying")
    if len(matches) != 1:
        raise ValueError("Ambiguous order identifier; specify --symbol and verify the account's orders before signing")
    return matches[0]


async def _find_open_order(client, account_id: int, args):
    """The open order a cancel or modify names.

    Inside the runner its warm open-order view is tried first; the account's open-order read
    decides whenever that view is unavailable or does not hold exactly one match.
    """
    warm = client_hook(client, "warm_open_orders")
    orders = warm() if warm is not None else None
    if orders is not None:
        try:
            return _resolve_order(orders, account_id, args)
        except ValueError:
            pass
    return _resolve_order(await client.get_open_orders(), account_id, args)


async def cmd_order_cancel(args: argparse.Namespace) -> None:
    """Cancel an open order by order_id."""
    load_env()

    async with trading_client() as client:
        await client.start()

        account_id = await require_owned_account(client)
        order = await _find_open_order(client, account_id, args)
        await _get_market_params(client, order.symbol)  # kit remains perp-only
        response = await client.cancel_order(symbol=order.symbol, account_id=account_id, order_id=order.order_id)

        result = {
            "action": "order_cancel",
            "symbol": order.symbol,
            "account_id": account_id,
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "status": response.status,
        }
        if response.status != OrderStatus.CANCELLED:
            result["error"] = "Cancellation was not confirmed; query order state"
        output_result(result)


async def cmd_order_modify(args: argparse.Namespace) -> None:
    load_env()
    args.order_plan = None  # never report a plan from an earlier call on these args
    async with trading_client() as client:
        await client.start()
        account_id = await require_owned_account(client)
        order = await _find_open_order(client, account_id, args)
        if order.order_type != OrderType.LIMIT or order.time_in_force != TimeInForce.GTC:
            raise ValueError("Modify supports resting GTC limits only")
        market = await _get_market_params(client, order.symbol)
        qty = _round_qty(args.qty, market["qty_step_size"], market["min_order_qty"])
        if order.cum_qty is None or qty <= Decimal(order.cum_qty):
            raise ValueError("Total --qty must exceed the known already-filled quantity")
        is_buy = order.side.value == "B"
        price = _round_price(args.price, market["tick_size"], "down" if is_buy else "up")
        post_only = args.post_only if args.post_only is not None else order.post_only
        if post_only is None:
            raise ValueError("Resting maker flag unavailable; pass --post_only or --no-post_only explicitly")
        price_data = await client.markets.get_perp_market_summary(symbol=order.symbol)
        mark_price = require_price(price_data.mark_price, f"{price_data.symbol} mark price")
        band = limit_price_band_refusal(price, mark_price, is_buy, getattr(args, "force", False))
        plan = {
            "action": "order_modify",
            "account_id": account_id,
            "symbol": order.symbol,
            "order_id": order.order_id,
            "qty": _fmt(qty),
            "limit_px_usd": _fmt(price),
            "mark_price_usd": float(mark_price),
            "limit_vs_mark_pct": _vs_mark_pct(price, mark_price),
            **await _order_plan(
                client,
                args,
                account_id=account_id,
                symbol=order.symbol,
                is_buy=is_buy,
                qty=qty - Decimal(order.cum_qty),  # only the unfilled remainder can still add exposure
                price=price,
                mark=mark_price,
                summary=price_data,
                qty_step=market["qty_step_size"],
                resting=True,
                refusals=[band] if band else [],
            ),
        }
        _note_underlying_hours(plan, market)
        if plan["refused_by"]:
            output_result({**plan, "error": " ".join(r["message"] for r in plan["refused_by"])})
            return
        del plan["refused_by"]
        args.order_plan = plan  # reported with the error if the send fails; see _error_result
        response = await client.modify_order(
            ModifyOrderParameters(
                symbol=order.symbol,
                is_buy=is_buy,
                limit_px=_fmt(price),
                qty=_fmt(qty),
                post_only=post_only,
                expires_after=None,
                time_in_force=TimeInForce.GTC,
                order_id=int(order.order_id),
                client_order_id=int(order.client_order_id) if order.client_order_id else None,
                reduce_only=False,
            )
        )
        result = {**plan, "exec_qty": response.exec_qty, **_order_result(response)}
        _check_create_result(result)
        output_result(result)


def _order_preview(order) -> dict:
    return {
        "order_id": order.order_id,
        "symbol": order.symbol,
        "side": "long" if order.side.value == "B" else "short",
        "order_type": order.order_type.value,
        "qty": order.qty,
        "limit_px": order.limit_px,
        "trigger_px": order.trigger_px,
    }


async def _cancel_orders(client, account_id: int, targets) -> tuple[list, list]:
    """Cancel each order individually so every cancellation is confirmed (spot orders are never targeted)."""
    cancelled, errors = [], []
    for order in targets:
        try:
            response = await client.cancel_order(symbol=order.symbol, account_id=account_id, order_id=order.order_id)
            if response.status != OrderStatus.CANCELLED:
                raise ValueError(f"Cancellation not confirmed: {response.status}")
            cancelled.append({"order_id": order.order_id, "symbol": order.symbol, "status": response.status})
        except Exception as exc:
            errors.append({"order_id": order.order_id, "symbol": order.symbol, **exception_result(exc)})
    return cancelled, errors


async def cmd_order_cancel_all(args: argparse.Namespace) -> None:
    """Cancel perp orders within an explicitly selected scope."""
    _require_cleanup_scope(args)
    load_env()

    async with trading_client() as client:
        await client.start()
        account_id = await require_owned_account(client)
        market_defs = await client.reference.get_perp_market_definitions()
        perp_symbols = {m.symbol for m in market_defs}
        if args.symbol and args.symbol not in perp_symbols:
            raise ValueError(f"Unknown perp symbol '{args.symbol}'")
        orders = await client.get_open_orders()
        targets = [o for o in orders if o.account_id == account_id and o.symbol in perp_symbols]
        if args.symbol:
            targets = [o for o in targets if o.symbol == args.symbol]

        if args.preview:
            would_cancel = [_order_preview(o) for o in targets]
            output_json(
                {
                    "action": "order_cancel_all",
                    "preview": True,
                    "symbol_filter": args.symbol,
                    "would_cancel_count": len(would_cancel),
                    "would_cancel": would_cancel,
                }
            )
            return

        cancelled, errors = await _cancel_orders(client, account_id, targets)

        result: dict = {
            "action": "order_cancel_all",
            "symbol_filter": args.symbol,
            "cancelled_count": len(cancelled),
            "cancelled": cancelled,
        }
        if errors:
            result["error"] = "Some operations failed; inspect errors and account state before retrying"
            result["error_count"] = len(errors)
            result["errors"] = errors
        output_result(result)


async def _await_positions_shown(client, account_id: int, symbols: list[str], deadline: float) -> tuple[list, list]:
    """Wait, until `deadline` (loop time), for a position on each symbol: (positions shown, symbols still empty).

    A fill the venue has not indexed reads as no position, by REST and by the runner's feed alike, so
    absence is never taken from one read. Only reads are repeated; nothing is sent.
    """
    loop = asyncio.get_running_loop()
    shown, missing = [], []
    for symbol in symbols:
        remaining = max(deadline - loop.time(), 0.0)
        try:
            position, visible = await _await_position(
                client,
                account_id,
                symbol,
                lambda p: p is not None,
                reads=int(remaining * ENTRY_POSITION_READS_PER_S) + 1,
                timeout=remaining,
            )
        except Exception:
            position, visible = None, False
        if visible:
            shown.append(position)
        else:
            missing.append(symbol)
    return shown, missing


def _not_shown_error(symbol: str, wait_s: float, protected: bool) -> str:
    if protected:
        evidence, kept = "TP/SL are armed on it", " and its TP/SL were kept"
        cause = "a fill the venue has not indexed reads as no position, or the TP/SL are left over from a closed one"
    else:
        evidence, kept = "the runner's live feed showed a position on it", ""
        cause = "a fill the venue has not indexed reads as no position"
    return (
        f"{symbol}: {evidence}, but no position showed within {wait_s:g} s, so nothing was closed there{kept}. "
        f"Either {cause}: run `query positions --symbol {symbol}` and `query orders --symbol {symbol}` before acting."
    )


async def cmd_positions_close_all(args: argparse.Namespace) -> None:
    """Flatten positions within an explicitly selected scope via reduce-only IOC orders.

    TP/SL stay armed while the closes are sent: they only ever close, and the venue retires them
    once a position is flat. So a close that fills part, or a position read that trails a fill,
    never leaves a position without its stop. `--cancel_orders` cancels the other open orders in
    scope before the closes and, once the read-back shows a market flat, any TP/SL still listed
    there. A market with TP/SL but no position shown is waited for, up to the deployment's
    indexing ceiling, before anything is concluded about it.
    """
    _require_cleanup_scope(args)
    load_env()

    slippage = args.slippage
    check_slippage_guardrail(slippage, args.force)

    async with trading_client() as client:
        await client.start()
        account_id = await require_owned_account(client)

        # Fetch market params once for all symbols we'll touch.
        market_defs = await client.reference.get_perp_market_definitions()
        market_map = {md.symbol: md for md in market_defs}
        if args.symbol and args.symbol not in market_map:
            raise ValueError(f"Unknown perp symbol '{args.symbol}'")

        def in_scope(symbol: str) -> bool:
            return not args.symbol or symbol == args.symbol

        cancel_orders = getattr(args, "cancel_orders", False)
        _, wait_s = entry_position_bounds(client)
        deadline = asyncio.get_running_loop().time() + wait_s
        if not args.preview:
            # Inside the runner: let its feed show the session's own fills first (at once when none are pending).
            wait_own_fills = client_hook(client, "wait_own_fills")
            if wait_own_fills is not None:
                await wait_own_fills(wait_s)

        # Read positions, then open orders, before anything is sent: a failed position read changes nothing.
        positions = await client.get_positions()
        scoped_orders: list | None = None
        order_read_error: dict | None = None
        try:
            scoped_orders = [
                o
                for o in await client.get_open_orders()
                if o.account_id == account_id and o.symbol in market_map and in_scope(o.symbol)
            ]
        except Exception as exc:
            order_read_error = {"order_id": None, "symbol": args.symbol, **exception_result(exc)}
        protection = [o for o in scoped_orders or [] if o.order_type.value in PROTECTIVE_ORDER_TYPES]
        cancel_targets = (
            [o for o in scoped_orders or [] if o.order_type.value not in PROTECTIVE_ORDER_TYPES]
            if cancel_orders
            else []
        )

        # Optionally clear the other resting orders in scope first, so none can reopen the position.
        cancelled_first: list = []
        cancel_errors: list = []
        if cancel_orders and order_read_error:
            # Flattening must not depend on the order read; report that nothing was cancelled.
            cancel_errors.append(order_read_error)
        if cancel_targets and not args.preview:
            cancelled_first, cancel_errors = await _cancel_orders(client, account_id, cancel_targets)
            # Read again, so a fill that raced a cancel is closed too.
            try:
                positions = await client.get_positions()
            except Exception as exc:
                # Cancels may already have gone through: report them with the read failure.
                output_result(
                    {
                        "action": "positions_close_all",
                        "symbol_filter": args.symbol,
                        "error": "Position read failed after cancelling orders; nothing was closed and TP/SL were "
                        "kept. Read positions and orders before retrying",
                        "cancelled_count": len(cancelled_first),
                        "cancelled": cancelled_first,
                        "kept_protection": [_order_preview(o) for o in protection],
                        "errors": [
                            *({"context": "Cancel before close", **e} for e in cancel_errors),
                            {"context": "Position read", **exception_result(exc)},
                        ],
                    }
                )
                return
        targets = [p for p in positions if p.account_id == account_id and Decimal(p.qty) > 0 and in_scope(p.symbol)]

        # A market that should hold a position but reads empty: its TP/SL are armed, or (inside the
        # runner) the verified feed shows one REST does not yet.
        protected = {o.symbol for o in protection}
        expected = set(protected)
        warm = client_hook(client, "warm_position")
        if warm is not None:
            for symbol in filter(in_scope, market_map):
                published = warm(account_id, symbol)
                if published is not None and published.position is not None:
                    expected.add(symbol)
        missing = sorted(expected - {p.symbol for p in targets})
        if missing and not args.preview:
            shown, missing = await _await_positions_shown(client, account_id, missing, deadline)
            targets.extend(shown)

        close_orders = []
        would_close = []
        errors = []
        # One depth read per close the book did not fill completely (or per planned close in a preview), started
        # right after the send so it never holds back the next close; each read is bounded and never raises.
        book_reads: list[tuple[dict, asyncio.Task]] = []

        def read_book(entry: dict, *, is_buy: bool, limit_px: Decimal, tick: Decimal, step: Decimal) -> None:
            read = ioc_book(_v2_url(client), entry["symbol"], is_buy=is_buy, limit_px=limit_px, tick=tick, step=step)
            book_reads.append((entry, asyncio.create_task(read)))

        for pos in targets:
            off_step = False
            try:
                symbol = pos.symbol
                market = market_map.get(symbol)
                if market is None:
                    raise ValueError(f"Symbol '{symbol}' not found in market definitions.")

                tick_size = Decimal(market.tick_size)

                # Opposite side closes the position.
                pos_is_long = pos.side.value == "B"
                close_is_buy = not pos_is_long

                price_data = await client.markets.get_perp_market_summary(symbol=symbol)
                mark_price = require_price(price_data.mark_price, f"{price_data.symbol} mark price")

                # Close the venue's reported position qty verbatim — no step rounding. Below the market
                # minimum is fine (the API exempts reduce-only orders from it); an off-step quantity,
                # which ADL rescaling can leave, is rejected and gets a hint.
                pos_qty = Decimal(pos.qty)
                qty_step = Decimal(market.qty_step_size)
                off_step = pos_qty % qty_step != 0

                if close_is_buy:
                    limit_px = _round_price(mark_price * (1 + slippage), tick_size, "up")
                else:
                    limit_px = _round_price(mark_price * (1 - slippage), tick_size, "down")
                limit_px = max(limit_px, tick_size)

                entry: dict = {
                    "symbol": symbol,
                    "position_side": "long" if pos_is_long else "short",
                    "qty": pos.qty,
                    "mark_price_usd": float(mark_price),
                    "limit_px_usd": _fmt(limit_px),
                }
                if off_step:
                    entry["off_step_qty"] = True

                if args.preview:
                    entry["close_side"] = "buy" if close_is_buy else "sell"
                    entry["est_notional_usd"] = float((pos_qty * mark_price).quantize(Decimal("0.01")))
                    # TP/SL are whole-position orders: the venue cancels them once the position is flat.
                    entry["drops_protection"] = (
                        None
                        if scoped_orders is None
                        else [
                            {
                                "order_id": o.order_id,
                                "order_type": o.order_type.value,
                                "trigger_px": o.trigger_px,
                                "limit_px": o.limit_px,
                            }
                            for o in protection
                            if o.symbol == symbol
                        ]
                    )
                    read_book(entry, is_buy=close_is_buy, limit_px=limit_px, tick=tick_size, step=qty_step)
                    would_close.append(entry)
                    continue

                response = await client.create_limit_order(
                    LimitOrderParameters(
                        symbol=symbol,
                        is_buy=close_is_buy,
                        limit_px=_fmt(limit_px),
                        qty=pos.qty,
                        time_in_force=TimeInForce.IOC,
                        reduce_only=True,
                    )
                )
                entry.update(_order_result(response))
                if unfilled(entry):
                    read_book(entry, is_buy=close_is_buy, limit_px=limit_px, tick=tick_size, step=qty_step)
                close_orders.append(entry)
            except Exception as exc:
                error_entry: dict = {
                    "symbol": pos.symbol,
                    "qty": pos.qty,
                    **exception_result(exc),
                }
                if off_step:
                    error_entry["off_step_qty"] = True
                    error_entry["hint"] = OFF_STEP_CLOSE_HINT
                errors.append(error_entry)

        for entry, read in book_reads:
            entry["book"] = await read
            if args.preview:
                warning = shortfall_warning(entry["book"], Decimal(entry["qty"]), entry["close_side"] == "buy")
                if warning:
                    entry["warning"] = warning
            else:
                message = unfilled_message(entry, entry["symbol"])
                if message:
                    entry["error"] = message

        result: dict = {
            "action": "positions_close_all",
            "symbol_filter": args.symbol,
            "slippage": float(slippage),
        }
        if cancel_orders:
            if args.preview:
                result["would_cancel_count"] = len(cancel_targets)
                result["would_cancel"] = [_order_preview(o) for o in cancel_targets]
                result["would_keep_protection"] = (
                    None if scoped_orders is None else [_order_preview(o) for o in protection]
                )
            else:
                result["cancelled_count"] = len(cancelled_first)
                result["cancelled"] = cancelled_first
            errors.extend({"context": "Cancel before close", **e} for e in cancel_errors)
        if args.preview and order_read_error and would_close:
            # Unknown TP/SL behind a planned close; with nothing to close, nothing is dropped.
            errors.append({"context": "TP/SL read", **order_read_error})
        if args.preview:
            result["preview"] = True
            result["would_close_count"] = len(would_close)
            result["would_close"] = would_close
            dropped = [e["drops_protection"] for e in would_close]
            drop_count = None if None in dropped else sum(map(len, dropped))
            if missing:
                # The run waits for these positions and closes any that show, which also ends their TP/SL.
                drop_count = None
                result["positions_not_shown"] = [
                    {"symbol": s, "protection": [_order_preview(o) for o in protection if o.symbol == s]}
                    for s in missing
                ]
                result["warning"] = (
                    f"No position shows yet on {', '.join(missing)}, although "
                    + ("TP/SL are armed there" if set(missing) <= protected else "it should hold one")
                    + ": a fill the venue has not indexed reads as no position. The run waits up to "
                    f"{wait_s:g} s for it and closes it if it shows; otherwise it reports an error and keeps the TP/SL."
                )
            result["would_drop_protection_count"] = drop_count
        else:
            result["close_orders"] = close_orders
            errors.extend(
                {"context": "Position not shown", "symbol": s, "error": _not_shown_error(s, wait_s, s in protected)}
                for s in missing
            )
            if order_read_error and not cancel_orders:
                result["warning"] = (
                    "Open orders were unreadable, so a position the venue has not indexed yet behind armed TP/SL "
                    "could not be detected; run `query positions` to confirm the scope is flat"
                )
            # A fill response is not settlement proof. Read the account back and
            # never report a position closed when it remains visible or unreadable.
            closed: list = []
            try:
                filled_symbols = {e["symbol"] for e in close_orders if e["status"] == OrderStatus.FILLED}
                for attempt in range(5):
                    remaining = [
                        p
                        for p in await client.get_positions()
                        if p.account_id == account_id and in_scope(p.symbol) and Decimal(p.qty) != 0
                    ]
                    # Read-model publication can trail the fill response. Allow
                    # two seconds for convergence, without repeating any write.
                    if not filled_symbols.intersection(p.symbol for p in remaining) or attempt == 4:
                        break
                    await asyncio.sleep(0.5)
                result["remaining_positions"] = [
                    {"symbol": p.symbol, "side": p.side.value, "qty": p.qty} for p in remaining
                ]
                remaining_symbols = {p.symbol for p in remaining}
                closed = [
                    e
                    for e in close_orders
                    if e["symbol"] not in remaining_symbols and e["status"] == OrderStatus.FILLED
                ]
                result["closed"] = closed
                result["closed_count"] = len(closed)
                if remaining or len(closed) != len(targets):
                    result["error"] = (
                        "Close incomplete or not yet confirmed; query positions before taking further action"
                    )
            except Exception as exc:
                result["closed"] = []
                result["closed_count"] = 0
                result["remaining_positions"] = None
                errors.append({"context": "Position readback failed", **exception_result(exc)})
            if cancel_orders and scoped_orders is not None:
                errors.extend(await _clear_protection_once_flat(client, account_id, protection, closed, result))
            # Fee reads must not delay submission of other risk-reducing closes.
            retain_result(result)
            for entry in close_orders:
                await add_settled_fees(client, entry)
        if errors:
            result["error"] = "Some operations failed; inspect errors and account state before retrying"
            result["error_count"] = len(errors)
            result["errors"] = errors
        output_result(result)


async def _clear_protection_once_flat(client, account_id: int, protection: list, closed: list, result: dict) -> list:
    """After the read-back, cancel the TP/SL still listed on markets it showed flat; report every TP/SL in scope.

    `kept_protection`: on a market not confirmed flat, still protecting what is there. `cancelled_protection`:
    cancelled by the kit. `retired_protection`: already removed by the venue when the position went flat.
    Returns the errors.
    """
    flat = {e["symbol"] for e in closed}
    result["kept_protection"] = [_order_preview(o) for o in protection if o.symbol not in flat]
    result["cancelled_protection"], result["retired_protection"] = [], []
    left = [o for o in protection if o.symbol in flat]
    if not left:
        return []
    try:
        listed = {o.order_id for o in await client.get_open_orders() if o.account_id == account_id}
    except Exception as exc:
        return [{"context": "TP/SL cleanup", "order_ids": [o.order_id for o in left], **exception_result(exc)}]
    result["retired_protection"] = [o.order_id for o in left if o.order_id not in listed]
    cancelled, failed = await _cancel_orders(client, account_id, [o for o in left if o.order_id in listed])
    result["cancelled_protection"] = cancelled
    errors = []
    for failure in failed:
        if failure.get("error_code") == "ORDER_NOT_FOUND_ERROR":
            result["retired_protection"].append(failure["order_id"])  # retired between the read and the cancel
        else:
            errors.append({"context": "TP/SL cleanup", **failure})
    return errors


async def _cmd_order_trigger(args: argparse.Namespace, trigger_type: OrderType) -> None:
    """Shared implementation for `order tp` and `order sl`.

    Reya trigger orders close the WHOLE position when fired (no qty parameter).
    The `--side` argument is the side of the position being protected; is_buy
    for the trigger order itself is derived (sell to close a long, buy to close
    a short). Three guardrails apply, all overridable with --force: the limit must
    be inside the venue's TP/SL band around the trigger (_trigger_band, checked
    before anything is read or signed), the trigger must match an open position
    (existence + side), and it must not sit on the side of the mark price where it
    would fire immediately.
    """
    load_env()

    # is_buy for the trigger order = opposite of the position being protected.
    if args.side in ("long", "buy"):
        position_side = "long"
        is_buy = False
    elif args.side in ("short", "sell"):
        position_side = "short"
        is_buy = True
    else:
        raise ValueError(f"Invalid --side '{args.side}' (expected long/short/buy/sell)")

    async with trading_client() as client:
        await client.start()

        # Round trigger price to the market's tick size.
        params = await _get_market_params(client, args.symbol)
        tick_size = params["tick_size"]
        trigger_px = _round_price(args.trigger_px, tick_size, "nearest")
        if trigger_px <= 0:
            raise ValueError("--trigger_px rounds to zero")
        limit_px = _round_price(args.limit_px, tick_size, "down" if is_buy else "up")
        if limit_px <= 0:
            raise ValueError("--limit_px rounds to zero")

        # Guardrail: a limit outside the venue's TP/SL band is rejected by the venue, so refuse it before
        # waiting for the position or signing anything. --force sends it anyway, in case the band has been
        # widened since the kit's constant was verified against the engine's configuration.
        kind = _trigger_kind(trigger_type)
        band_refusal = _trigger_band.refusal(kind, trigger_px, limit_px, tick_size)
        band_warning = None
        if band_refusal is not None:
            if not args.force:
                output_result(
                    {
                        "action": "order_tp" if trigger_type == OrderType.TAKE_PROFIT else "order_sl",
                        "symbol": args.symbol,
                        "position_side": position_side,
                        "trigger_px_usd": _fmt(trigger_px),
                        "limit_px_usd": _fmt(limit_px),
                        "refused_by": [band_refusal],
                        "error": band_refusal["message"],
                    }
                )
                return
            band_warning = (
                f"The limit is {band_refusal['distance_bps']} bps from the trigger, outside the "
                f"{_trigger_band.TRIGGER_BAND_BPS} bps band the venue enforced when the kit was verified; sent because "
                "of --force, and the venue decides."
            )

        # Guardrail: the trigger must protect a real position (existence + side). Position reads
        # trail the entry's fill, and a flip shows the old side first, so keep reading (inside the
        # runner: watching its verified feed) until the expected side appears, within the deployment's
        # indexing ceiling. A fill not indexed yet reads exactly like no position, so a refusal waits
        # the whole ceiling too.
        account_id = await require_owned_account(client)
        reads, wait_s = entry_position_bounds(client)
        try:
            position, _ = await _await_position(
                client,
                account_id,
                args.symbol,
                lambda p: p is not None and _position_side(p) == position_side,
                reads=reads,
                timeout=wait_s,
            )
        except TimeoutError:
            raise ValueError(
                "Position read timed out; no protective order submitted. Reconcile the position before retrying"
            ) from None
        actual_side = None if position is None else _position_side(position)
        if _shared_client.get() is not None and actual_side != position_side:
            raise ValueError(
                f"Position is not visible with the expected side within {wait_s:g} seconds; no protective order "
                "submitted. Query status and reconcile before retrying protection; do not repeat the entry"
            )
        position_warning = _check_position_match(
            args.symbol, position_side, actual_side, account_id, args.force, wait_s
        )

        # Triggers use the mark price, not the oracle or mid price.
        price_data = await client.markets.get_perp_market_summary(symbol=args.symbol)
        mark_price = require_price(price_data.mark_price, f"{price_data.symbol} mark price")
        plausibility_warning = _check_trigger_plausibility(
            trigger_type, position_side, trigger_px, mark_price, args.force
        )
        entry_px = Decimal(position.avg_entry_price) if actual_side == position_side and position else None
        loss_warning = None
        if trigger_type == OrderType.TAKE_PROFIT and entry_px is not None:
            better = "above" if position_side == "long" else "below"
            loss_warning = _take_profit_loss_warning(
                position_side,
                limit_px,
                entry_px,
                f"To take only a profit, set --limit_px at or {better} the entry; it may then not fill.",
            )
        warnings = [w for w in (position_warning, plausibility_warning, loss_warning, band_warning) if w]

        try:
            response = await client.create_trigger_order(
                TriggerOrderParameters(
                    symbol=args.symbol,
                    is_buy=is_buy,
                    trigger_px=_fmt(trigger_px),
                    trigger_type=trigger_type,
                    limit_px=_fmt(limit_px),
                    time_in_force=TimeInForce(args.time_in_force),
                    client_order_id=_ensure_client_order_id(args),
                )
            )
        except Exception as exc:
            rejected = _error_result(args, exc)
            if rejected.get("error_code") != _trigger_band.BAND_ERROR_CODE:
                raise
            band = _trigger_band.rejection(args.symbol, rejected.get("message"), trigger_px, limit_px, tick_size)
            output_result(
                {
                    "action": "order_tp" if trigger_type == OrderType.TAKE_PROFIT else "order_sl",
                    "symbol": args.symbol,
                    "position_side": position_side,
                    "trigger_px_usd": _fmt(trigger_px),
                    "limit_px_usd": _fmt(limit_px),
                    **rejected,
                    "trigger_band": band,
                    "error": band["explanation"],
                }
            )
            return

        result = {
            "action": "order_tp" if trigger_type == OrderType.TAKE_PROFIT else "order_sl",
            "symbol": args.symbol,
            "position_side": position_side,
            "close_side": "buy" if is_buy else "sell",
            "quantity_mode": "whole_position",
            "trigger_px_usd": _fmt(trigger_px),
            "mark_price_usd": float(mark_price),
            "limit_px_usd": _fmt(limit_px),
            "time_in_force": args.time_in_force,
            **({"entry_px_usd": _fmt(entry_px)} if entry_px is not None else {}),
            **_order_result(response, args.client_order_id),
        }
        if warnings:
            result["warning"] = " ".join(warnings)
        retain_result(result)
        await add_settled_fees(client, result, wait_s=getattr(args, "fees_wait_s", 0))
        _check_create_result(result)
        output_result(result)


async def cmd_order_tp(args: argparse.Namespace) -> None:
    """Place a take-profit trigger order. Closes the whole position when fired."""
    await _cmd_order_trigger(args, OrderType.TAKE_PROFIT)


async def cmd_order_sl(args: argparse.Namespace) -> None:
    """Place a stop-loss trigger order. Closes the whole position when fired."""
    await _cmd_order_trigger(args, OrderType.STOP_LOSS)


def _require_cleanup_scope(args: argparse.Namespace) -> None:
    if bool(args.symbol) == bool(getattr(args, "all", False)):
        raise ValueError("Choose exactly one of --symbol or --all for cleanup")


def _client_order_id(value: str) -> int:
    if len(value) > 20 or not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 2**64 - 1:
        raise argparse.ArgumentTypeError("Client order ID must be an integer from 1 to 18446744073709551615")
    return int(value)


def _usage_arg(text: str) -> Decimal:
    value = positive_decimal_arg(text)
    if value >= 1:
        raise argparse.ArgumentTypeError("--usage is a fraction below 1 (0.5 = half way to liquidation)")
    return value


def _add_margin_sizing(group) -> None:
    group.add_argument(
        "--max_size",
        action="store_true",
        help=(
            "Size to the largest qty the matching engine would admit at this price, minus headroom "
            "(REYA_MARGIN_HEADROOM); within REYA_MAX_ORDER_NOTIONAL and REYA_MAX_LEVERAGE if set, unless --allow_large"
        ),
    )
    group.add_argument(
        "--usage",
        type=_usage_arg,
        help="Size so margin usage (liquidation margin / equity) is at most this fraction after a full fill",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="trade",
        description="Reya Agent Kit — write commands (live execution)",
    )
    subparsers = parser.add_subparsers(dest="group", required=True)

    # order group
    order_parser = subparsers.add_parser("order", help="Order operations")
    order_subparsers = order_parser.add_subparsers(dest="action", required=True)

    # order market
    market_parser = order_subparsers.add_parser("market", help="Place a market order (IOC with slippage)")
    market_parser.add_argument("symbol", type=symbol_arg, help="Trading symbol (e.g., ETHRUSDPERP or ETH)")
    market_parser.add_argument(
        "--side", type=str, required=True, choices=["long", "short", "buy", "sell"], help="Order side"
    )
    qty_group_market = market_parser.add_mutually_exclusive_group(required=True)
    qty_group_market.add_argument("--qty", type=positive_decimal_arg, help="Order quantity in base asset units")
    qty_group_market.add_argument(
        "--notional", type=positive_decimal_arg, help="Order size in USD (qty computed from current price)"
    )
    _add_margin_sizing(qty_group_market)
    market_parser.add_argument(
        "--slippage",
        type=slippage_arg,
        default=DEFAULT_SLIPPAGE,
        help=f"Max slippage as a decimal (default: {DEFAULT_SLIPPAGE} = 1%%)",
    )
    market_parser.add_argument(
        "--reduce_only",
        action="store_true",
        default=False,
        help="Reduce-only order (only closes existing position)",
    )
    market_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Bypass the 5%% slippage safety cap",
    )
    market_parser.add_argument(
        "--preview",
        action="store_true",
        default=False,
        help="Show qty, price bound, notional, estimated leverage and any refusal without sending anything",
    )
    market_parser.add_argument(
        "--sl",
        type=trigger_level_arg,
        default=None,
        help=(
            "Arm a stop-loss once the entry fills: a percent from the average fill price (0.35%%) or a "
            "price. Closes the whole position when it fires"
        ),
    )
    market_parser.add_argument(
        "--tp",
        type=trigger_level_arg,
        default=None,
        help="Arm a take-profit once the entry fills: a percent from the average fill price (0.5%%) or a price",
    )
    market_parser.add_argument(
        "--trigger_slippage",
        type=trigger_slippage_arg,
        default=DEFAULT_TRIGGER_SLIPPAGE,
        help=(
            f"Child limit distance from each trigger as a decimal (default: {DEFAULT_TRIGGER_SLIPPAGE} = 1%%, "
            f"at most {MAX_TRIGGER_SLIPPAGE}); the fired child is IOC"
        ),
    )

    # order limit
    limit_parser = order_subparsers.add_parser("limit", help="Place a GTC limit order")
    limit_parser.add_argument("symbol", type=symbol_arg, help="Trading symbol (e.g., ETHRUSDPERP or ETH)")
    limit_parser.add_argument(
        "--side", type=str, required=True, choices=["long", "short", "buy", "sell"], help="Order side"
    )
    limit_parser.add_argument("--price", type=positive_decimal_arg, required=True, help="Limit price in USD")
    qty_group_limit = limit_parser.add_mutually_exclusive_group(required=True)
    qty_group_limit.add_argument("--qty", type=positive_decimal_arg, help="Order quantity in base asset units")
    qty_group_limit.add_argument(
        "--notional", type=positive_decimal_arg, help="Order size in USD (qty computed from limit price)"
    )
    _add_margin_sizing(qty_group_limit)

    limit_parser.add_argument("--post_only", action="store_true", help="Rest as maker or cancel; never take liquidity")
    limit_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Allow a limit price more than 5%% through the mark (never more than 50%%)",
    )
    limit_parser.add_argument(
        "--preview",
        action="store_true",
        default=False,
        help="Show qty, notional, distance from mark, estimated leverage and any refusal without sending anything",
    )
    # Accepted only to refuse with guidance: protection attaches to market entries.
    limit_parser.add_argument("--sl", type=trigger_level_arg, default=None, help=argparse.SUPPRESS)
    limit_parser.add_argument("--tp", type=trigger_level_arg, default=None, help=argparse.SUPPRESS)

    # order cancel
    cancel_parser = order_subparsers.add_parser("cancel", help="Cancel an open order")
    cancel_parser.add_argument(
        "--symbol", type=symbol_arg, help="Market scope; required when an identifier matches multiple orders"
    )
    cancel_ids = cancel_parser.add_mutually_exclusive_group(required=True)
    cancel_ids.add_argument("--order_id", type=str, help="Venue order ID to cancel")
    cancel_ids.add_argument(
        "--client_order_id", type=_client_order_id, help="Client ID of an open order on this account"
    )

    modify_parser = order_subparsers.add_parser(
        "modify", help="Modify a resting GTC limit using its full intended size/price"
    )
    modify_ids = modify_parser.add_mutually_exclusive_group(required=True)
    modify_parser.add_argument(
        "--symbol", type=symbol_arg, help="Market scope; required when an identifier matches multiple orders"
    )
    modify_ids.add_argument("--order_id")
    modify_ids.add_argument("--client_order_id", type=_client_order_id)
    modify_parser.add_argument("--price", required=True, type=positive_decimal_arg)
    modify_parser.add_argument(
        "--qty",
        required=True,
        type=positive_decimal_arg,
        help="Total order size, including any already-filled quantity",
    )
    modify_parser.add_argument("--post_only", action=argparse.BooleanOptionalAction, default=None)
    modify_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Allow a limit price more than 5%% through the mark (never more than 50%%)",
    )

    # order cancel_all
    cancel_all_parser = order_subparsers.add_parser(
        "cancel_all", help="Cancel every open order on the configured account"
    )
    cancel_all_scope = cancel_all_parser.add_mutually_exclusive_group(required=True)
    cancel_all_scope.add_argument("--all", action="store_true", help="Explicitly target the whole configured account")
    cancel_all_scope.add_argument(
        "--symbol",
        type=symbol_arg,
        default=None,
        help="Only cancel orders for this symbol (e.g., ETHRUSDPERP)",
    )
    cancel_all_parser.add_argument(
        "--preview",
        action="store_true",
        default=False,
        help="List the orders that would be cancelled without sending anything",
    )

    # order tp — take-profit (closes whole position on fire)
    tp_parser = order_subparsers.add_parser(
        "tp", help="Place a take-profit trigger order (closes the whole position when fired)"
    )
    tp_parser.add_argument("symbol", type=symbol_arg, help="Trading symbol (e.g., ETHRUSDPERP or ETH)")
    tp_parser.add_argument(
        "--side",
        type=str,
        required=True,
        choices=["long", "short", "buy", "sell"],
        help="Side of the position being protected (long/short)",
    )
    tp_parser.add_argument(
        "--trigger_px",
        type=positive_decimal_arg,
        required=True,
        help="Price at which the take-profit fires (rounded to tick size)",
    )
    tp_parser.add_argument(
        "--limit_px",
        type=positive_decimal_arg,
        required=True,
        help="Worst acceptable execution price when the trigger fires",
    )
    tp_parser.add_argument(
        "--time_in_force",
        choices=["IOC", "GTC"],
        required=True,
        help="IOC takes or cancels; GTC may rest after triggering",
    )
    tp_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Bypass TP/SL guardrails (trigger band, position match and immediate-fire checks)",
    )

    # order sl — stop-loss (closes whole position on fire)
    sl_parser = order_subparsers.add_parser(
        "sl", help="Place a stop-loss trigger order (closes the whole position when fired)"
    )
    sl_parser.add_argument("symbol", type=symbol_arg, help="Trading symbol (e.g., ETHRUSDPERP or ETH)")
    sl_parser.add_argument(
        "--side",
        type=str,
        required=True,
        choices=["long", "short", "buy", "sell"],
        help="Side of the position being protected (long/short)",
    )
    sl_parser.add_argument(
        "--trigger_px",
        type=positive_decimal_arg,
        required=True,
        help="Price at which the stop-loss fires (rounded to tick size)",
    )
    sl_parser.add_argument(
        "--limit_px",
        type=positive_decimal_arg,
        required=True,
        help="Worst acceptable execution price when the trigger fires",
    )
    sl_parser.add_argument(
        "--time_in_force",
        choices=["IOC", "GTC"],
        required=True,
        help="IOC takes or cancels; GTC may rest after triggering",
    )
    sl_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Bypass TP/SL guardrails (trigger band, position match and immediate-fire checks)",
    )

    # positions group
    positions_parser = subparsers.add_parser("positions", help="Position operations")
    positions_subparsers = positions_parser.add_subparsers(dest="action", required=True)

    # positions close_all
    close_all_parser = positions_subparsers.add_parser(
        "close_all", help="Flatten open positions via reduce-only IOC orders"
    )
    close_all_parser.add_argument(
        "--cancel_orders",
        action="store_true",
        default=False,
        help="Cancel open perp orders in the same scope first, so resting orders cannot reopen a position",
    )
    close_all_scope = close_all_parser.add_mutually_exclusive_group(required=True)
    close_all_scope.add_argument("--all", action="store_true", help="Explicitly target the whole configured account")
    close_all_scope.add_argument(
        "--symbol",
        type=symbol_arg,
        default=None,
        help="Only close the position for this symbol (e.g., ETHRUSDPERP)",
    )
    close_all_parser.add_argument(
        "--slippage",
        type=slippage_arg,
        default=DEFAULT_SLIPPAGE,
        help=f"Max slippage as a decimal (default: {DEFAULT_SLIPPAGE} = 1%%)",
    )
    close_all_parser.add_argument(
        "--preview",
        action="store_true",
        default=False,
        help="List the positions that would be closed without sending anything",
    )
    close_all_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Bypass the 5%% slippage safety cap",
    )

    for order_command in (market_parser, limit_parser, modify_parser):
        order_command.add_argument(
            "--skip_margin_check",
            action="store_true",
            default=False,
            help=(
                "Send an order the kit predicts the matching engine would reject for margin; only when the kit "
                "is stricter than the engine (for example a collateral the engine credits that this kit version does not) "
                "and the user confirms"
            ),
        )
        order_command.add_argument(
            "--allow_large",
            action="store_true",
            default=False,
            help=(
                "Allow an order above a leverage limit (REYA_MAX_LEVERAGE) or per-order notional cap "
                "(REYA_MAX_ORDER_NOTIONAL) the user set in .env; only after the user confirms that order"
            ),
        )

    for order_command in (market_parser, limit_parser, tp_parser, sl_parser):
        order_command.add_argument(
            "--fees_wait_s",
            type=int,
            choices=range(0, 6),
            default=0,
            help="Poll settled-fee reads for up to 1..5 seconds; 0 uses one bounded read. Never retries the order",
        )
        order_command.add_argument("--client_order_id", type=_client_order_id, help="Nonzero uint64 client order ID")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.group == "order":
            if args.action == "market":
                asyncio.run(cmd_order_market(args))
            elif args.action == "limit":
                asyncio.run(cmd_order_limit(args))
            elif args.action == "modify":
                asyncio.run(cmd_order_modify(args))
            elif args.action == "cancel":
                asyncio.run(cmd_order_cancel(args))
            elif args.action == "cancel_all":
                asyncio.run(cmd_order_cancel_all(args))
            elif args.action == "tp":
                asyncio.run(cmd_order_tp(args))
            elif args.action == "sl":
                asyncio.run(cmd_order_sl(args))
            else:
                output_error(f"Unknown action: order {args.action}")
        elif args.group == "positions":
            if args.action == "close_all":
                asyncio.run(cmd_positions_close_all(args))
            else:
                output_error(f"Unknown action: positions {args.action}")
        else:
            output_error(f"Unknown group: {args.group}")
    except Exception as e:
        output_result(_error_result(args, e))


if __name__ == "__main__":
    main()
