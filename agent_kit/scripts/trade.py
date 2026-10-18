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

from agent_kit.scripts._common import (
    DEFAULT_SLIPPAGE,
    MAX_LEVERAGE_VAR,
    MAX_ORDER_NOTIONAL_VAR,
    JsonArgumentParser,
    check_slippage_guardrail,
    exception_result,
    limit_price_band_refusal,
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
)
from agent_kit.scripts._env import get_trading_config, load_env
from agent_kit.scripts._executions import add_settled_fees
from agent_kit.scripts._writer import CoordinatedTradingClient as ReyaTradingClient

_shared_client: ContextVar[object | None] = ContextVar("shared_trading_client", default=None)


BELOW_MIN_CLOSE_HINT = (
    "The API rejects reduce-only orders below the market minimum and reduce-only orders larger than the "
    "position, so this residual cannot be closed with a reduce-only order. Increase the position to at least "
    "the minimum, then close it, or leave it."
)

# An order's fill is in its reply about a second before position reads show it, so a
# TP/SL placed straight after an entry reads positions until they catch up (reads only).
POSITION_POLL_S = 0.5
TRIGGER_POSITION_READS, TRIGGER_POSITION_WAIT_S = 7, 4
ENTRY_POSITION_READS, ENTRY_POSITION_WAIT_S = 11, 5
TRIGGER_READBACK_READS, TRIGGER_READBACK_WAIT_S = 5, 2
DEFAULT_TRIGGER_SLIPPAGE = Decimal("0.01")
MAX_TRIGGER_SLIPPAGE = Decimal("0.03")  # the venue's band around the trigger is 300 bp on mainnet


@asynccontextmanager
async def trading_client():
    shared = _shared_client.get()
    if shared is not None:
        yield shared
    else:
        async with ReyaTradingClient(config=get_trading_config(require_private_key=True)) as client:
            yield client


async def _get_market_params(client: ReyaTradingClient, symbol: str) -> dict:
    """Fetch qty_step_size, tick_size, min_order_qty for a symbol, as exact Decimals."""
    market_defs = await client.reference.get_perp_market_definitions()
    market = next((m for m in market_defs if m.symbol == symbol), None)
    if market is None:
        raise ValueError(f"Unknown symbol '{symbol}'. Run: query.py prices to see available symbols.")
    return {
        "qty_step_size": Decimal(market.qty_step_size),
        "tick_size": Decimal(market.tick_size),
        "min_order_qty": Decimal(market.min_order_qty),
    }


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


def _round_qty(raw_qty: Decimal, qty_step: Decimal, min_qty: Decimal) -> Decimal:
    """Round quantity down to the nearest step size. Raises if below minimum."""
    qty = _quantize(raw_qty, qty_step, "down")
    if qty < min_qty:
        raise ValueError(
            f"Calculated qty {_fmt(qty)} is below minimum order qty {_fmt(min_qty)}. " f"Increase --qty or --notional."
        )
    return qty


def _round_price(price: Decimal, tick_size: Decimal, direction: str = "nearest") -> Decimal:
    """Round price to tick size. direction: 'up', 'down', or 'nearest'."""
    return _quantize(price, tick_size, direction)


def _trigger_above(trigger_type: OrderType, position_side: str) -> bool:
    """A long's take-profit and a short's stop-loss sit above the price; the other two below."""
    return (trigger_type == OrderType.TAKE_PROFIT) == (position_side == "long")


def _fires_at_once(trigger_type: OrderType, position_side: str, trigger_px: Decimal, mark_price: Decimal) -> bool:
    return trigger_px <= mark_price if _trigger_above(trigger_type, position_side) else trigger_px >= mark_price


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
) -> str | None:
    """Refuse TP/SL triggers that don't match an open position on the configured account.

    A trigger with no position behind it protects nothing — the agent believes
    the exposure is bracketed when it isn't — and a trigger whose --side
    disagrees with the actual position derives the wrong close direction and
    the wrong immediate-fire relation. Returns None when matched, raises
    ValueError when not, or returns a warning string when not matched but
    force=True.
    """
    if actual_side == position_side:
        return None
    if actual_side is None:
        message = (
            f"No open position on {symbol} for account {account_id} after reading for "
            f"{TRIGGER_POSITION_WAIT_S} seconds; this trigger would protect nothing, so none was sent. Check "
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


async def _await_position(client, account_id: int, symbol: str, accept, *, reads: int, timeout: float):
    """Read the position until `accept(position)` holds: at most `reads` reads within `timeout` seconds.

    Returns (last position read or None, accepted). Only reads are repeated; nothing is
    sent. A failed read is retried within the bound; raises TimeoutError, or the last read
    error, when no read completed.
    """
    seen: list = []
    errors: list = []

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


async def _account_exposure(client, account_id: int, symbol: str, mark: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(account value, gross notional of positions in other markets, signed position qty in `symbol`).

    Account value is collateral (USDC at 1, other assets at their oracle price, no haircut)
    plus estimated unrealized PnL at mark; funding and fees are not included. A missing
    price refuses the order rather than guessing.
    """
    balances = [
        b for b in await client.get_account_balances() if b.account_id == account_id and Decimal(b.real_balance) != 0
    ]
    positions = [p for p in await client.get_positions() if p.account_id == account_id and Decimal(p.qty) != 0]
    oracle = {}
    if any(b.asset != "RUSD" for b in balances):
        oracle = {a.asset: a.oracle_price for a in await client.markets.get_asset_oracle_prices()}
    marks = {}
    if any(p.symbol != symbol for p in positions):
        marks = {m.symbol: m.mark_price for m in await client.markets.get_perp_markets_summary()}
    value = Decimal(0)
    for balance in balances:
        price = (
            Decimal(1)
            if balance.asset == "RUSD"
            else require_price(oracle.get(balance.asset), f"{balance.asset} collateral oracle price")
        )
        value += Decimal(balance.real_balance) * price
    other_gross = Decimal(0)
    position_qty = Decimal(0)
    for position in positions:
        qty = abs(Decimal(position.qty)) * (1 if position.side.value == "B" else -1)
        price = (
            mark
            if position.symbol == symbol
            else require_price(marks.get(position.symbol), f"{position.symbol} mark price")
        )
        value += qty * (price - Decimal(position.avg_entry_price))
        if position.symbol == symbol:
            position_qty += qty
        else:
            other_gross += abs(qty) * price
    return value, other_gross, position_qty


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
    reduce_only: bool = False,
    refusals: list | None = None,
) -> dict:
    """Pre-trade figures and fat-finger refusals for one create. Reads only; sends nothing.

    `price` is the order's own notional basis (the mark for market orders, the limit
    price for limits). Estimated leverage is gross position notional at mark after a
    full fill divided by the account value; resting orders are not counted. An order
    that only reduces the position is never refused for size or leverage.
    """
    refusals = list(refusals or [])
    value, other_gross, position_qty = await _account_exposure(client, account_id, symbol, mark)
    cap_setting, max_leverage = order_size_limits()
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
    cap = cap_setting if cap_setting is not None else value
    source = (
        f"{MAX_ORDER_NOTIONAL_VAR} in .env"
        if cap_setting is not None
        else f"the account value; set {MAX_ORDER_NOTIONAL_VAR} in .env to change it"
    )
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
            if notional > cap and not allow_large:
                refusals.append(
                    {
                        "check": "max_order_notional",
                        "message": (
                            f"Order notional {_usd(notional):,.2f} USDC is above the per-order cap of "
                            f"{_usd(cap):,.2f} USDC ({source}). Confirm the size with the user, then re-run "
                            "with --allow_large."
                        ),
                        "override": "--allow_large",
                    }
                )
            if post_gross > max_leverage * value and post_gross > pre_gross and not allow_large:
                refusals.append(
                    {
                        "check": "max_leverage",
                        "message": (
                            f"Estimated leverage after this order is {_leverage(post_gross, value)}x, above the "
                            f"{_fmt(max_leverage)}x limit ({MAX_LEVERAGE_VAR}, default 5). Confirm the leverage "
                            "with the user, then re-run with --allow_large."
                        ),
                        "override": "--allow_large",
                    }
                )
    return {
        "account_value_usd": _usd(value),
        "current_leverage": _leverage(pre_gross, value),
        "est_leverage": _leverage(post_gross, value),
        "max_leverage": float(max_leverage),
        "max_order_notional_usd": _usd(max(cap, Decimal(0))),
        "max_order_notional_source": MAX_ORDER_NOTIONAL_VAR if cap_setting is not None else "account_value",
        "reduces_position": reducing,
        "refused_by": refusals,
    }


def _stop_before_sending(args: argparse.Namespace, plan: dict) -> bool:
    """Print the plan instead of sending for --preview or a refusal; True means nothing was sent."""
    if getattr(args, "preview", False):
        output_json({**plan, "preview": True, "would_send": not plan["refused_by"]})
        return True
    if plan["refused_by"]:
        output_result({**plan, "error": " ".join(r["message"] for r in plan["refused_by"])})
        return True
    del plan["refused_by"]
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
    positions, orders = await asyncio.gather(client.get_positions(), client.get_open_orders())
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
    return plan, refusals, before


async def _read_back_triggers(client, account_id: int, symbol: str, order_ids: set) -> set:
    """The IDs among `order_ids` that open-order reads show armed (bounded reads; nothing is sent)."""
    visible: set = set()

    async def poll() -> None:
        for attempt in range(TRIGGER_READBACK_READS):
            try:
                orders = await client.get_open_orders()
            except Exception:
                orders = []
            visible.update(
                o.order_id
                for o in orders
                if o.account_id == account_id and o.symbol == symbol and o.order_id in order_ids and not o.triggered
            )
            if visible >= order_ids:
                return
            if attempt < TRIGGER_READBACK_READS - 1:
                await asyncio.sleep(POSITION_POLL_S)

    try:
        await asyncio.wait_for(poll(), timeout=TRIGGER_READBACK_WAIT_S)
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
) -> None:
    """Arm the requested stop-loss, then take-profit, once position reads include the entry's fill.

    Evidence, not elapsed time: the position must show the entry's side with a later trade
    sequence number than before the entry. Bounded; on a miss nothing is placed and the
    position is reported UNPROTECTED. Never resends the entry or retries a trigger. Fills in
    `protection` in place, so a runner timeout still reports how far it got.
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

    before_seq = None if before is None else before.last_trade_sequence_number

    def includes_entry(position) -> bool:
        return (
            position is not None
            and _position_side(position) == position_side
            and (before_seq is None or position.last_trade_sequence_number > before_seq)
        )

    try:
        position, visible = await _await_position(
            client,
            account_id,
            symbol,
            includes_entry,
            reads=ENTRY_POSITION_READS,
            timeout=ENTRY_POSITION_WAIT_S,
        )
    except Exception:
        position, visible = None, False
    if not visible:
        protection.update(
            state="unprotected",
            message=(
                f"Entry filled {filled} {symbol}, but position reads did not show it within "
                f"{ENTRY_POSITION_WAIT_S} seconds, so no TP/SL was placed: the position is UNPROTECTED. Once "
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
            leg.pop("message", None)
            if leg.get("error_code") not in (None, "API_ERROR"):
                leg["error"] = f"{leg['error_code']}: {leg['error']}"
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
        protection.update(
            state="unprotected",
            message=(
                f"Entry filled {filled} {symbol}, but the {_trigger_kind(failed_type)} was not placed "
                f"({failed['error']}): the position is UNPROTECTED"
                + (f" (armed: {armed})" if armed else "")
                + f". Arm it with `{failed['command']}`, or close with `positions close_all --symbol {symbol}`. "
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
    --sl/--tp, the stop-loss and then the take-profit are armed once position
    reads include the fill; see _protect_entry.
    """
    load_env()

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

        # Compute qty: from --notional or --qty
        if args.notional is not None:
            raw_qty = args.notional / mark_price
        else:
            raw_qty = args.qty

        qty = _round_qty(raw_qty, qty_step, min_qty)

        # Compute aggressive limit price with slippage
        if is_buy:
            limit_px = _round_price(mark_price * (1 + slippage), tick_size, "up")
        else:
            limit_px = _round_price(mark_price * (1 - slippage), tick_size, "down")

        limit_px = max(limit_px, tick_size)

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
                    reduce_only=args.reduce_only,
                    refusals=refusals,
                )
            )
        except Exception as exc:
            if not args.reduce_only:
                raise
            # Size checks never apply to reduce-only orders; a failed account read must not block a close.
            plan.update(risk_unavailable=exception_result(exc)["error"], refused_by=[])
        if _stop_before_sending(args, plan):
            return

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
        _check_create_result(result, require_filled=True)
        if bracket and protection["state"] not in ("protected", "not_placed"):
            entry_error = result.get("error")
            result["error"] = protection["message"] + (f" Entry: {entry_error}" if entry_error else "")
        output_result(result)


async def cmd_order_limit(args: argparse.Namespace) -> None:
    """Place a GTC limit order."""
    load_env()
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

        # Compute qty: from --notional or --qty
        if args.notional is not None:
            raw_qty = args.notional / limit_px
        else:
            raw_qty = args.qty

        qty = _round_qty(raw_qty, qty_step, min_qty)

        price_data = await client.markets.get_perp_market_summary(symbol=args.symbol)
        mark_price = require_price(price_data.mark_price, f"{price_data.symbol} mark price")
        band = limit_price_band_refusal(limit_px, mark_price, is_buy, getattr(args, "force", False))
        plan = {
            "action": "order_limit",
            "symbol": args.symbol,
            "side": args.side,
            "qty": _fmt(qty),
            "limit_px_usd": _fmt(limit_px),
            "notional_usd": float((qty * limit_px).quantize(Decimal("0.01"))),
            "mark_price_usd": float(mark_price),
            "limit_vs_mark_pct": _vs_mark_pct(limit_px, mark_price),
            **await _order_plan(
                client,
                args,
                account_id=account_id,
                symbol=args.symbol,
                is_buy=is_buy,
                qty=qty,
                price=limit_px,
                mark=mark_price,
                refusals=[band] if band else [],
            ),
        }
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


async def cmd_order_cancel(args: argparse.Namespace) -> None:
    """Cancel an open order by order_id."""
    load_env()

    async with trading_client() as client:
        await client.start()

        account_id = await require_owned_account(client)
        orders = await client.get_open_orders()
        order = _resolve_order(orders, account_id, args)
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
    async with trading_client() as client:
        await client.start()
        account_id = await require_owned_account(client)
        orders = await client.get_open_orders()
        order = _resolve_order(orders, account_id, args)
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
        plan = await _order_plan(
            client,
            args,
            account_id=account_id,
            symbol=order.symbol,
            is_buy=is_buy,
            qty=qty - Decimal(order.cum_qty),  # only the unfilled remainder can still add exposure
            price=price,
            mark=mark_price,
            refusals=[band] if band else [],
        )
        if plan["refused_by"]:
            output_result(
                {
                    "action": "order_modify",
                    "account_id": account_id,
                    "symbol": order.symbol,
                    "order_id": order.order_id,
                    "qty": _fmt(qty),
                    "limit_px_usd": _fmt(price),
                    "mark_price_usd": float(mark_price),
                    "limit_vs_mark_pct": _vs_mark_pct(price, mark_price),
                    **plan,
                    "error": " ".join(r["message"] for r in plan["refused_by"]),
                }
            )
            return
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
        result = {
            "action": "order_modify",
            "account_id": account_id,
            "symbol": order.symbol,
            "qty": _fmt(qty),
            "limit_px_usd": _fmt(price),
            "exec_qty": response.exec_qty,
            **_order_result(response),
        }
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


async def cmd_positions_close_all(args: argparse.Namespace) -> None:
    """Flatten positions within an explicitly selected scope via reduce-only IOC orders."""
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

        # Optionally clear resting orders in the same scope first, so none can reopen the position.
        # A preview also reads them to list the TP/SL that stop protecting each closed position.
        cancel_orders = getattr(args, "cancel_orders", False)
        cancel_targets: list = []
        cancelled_first: list = []
        cancel_errors: list = []
        open_orders: list | None = None
        order_read_error: dict | None = None
        if cancel_orders or args.preview:
            try:
                open_orders = await client.get_open_orders()
            except Exception as exc:
                order_read_error = {"order_id": None, "symbol": args.symbol, **exception_result(exc)}
        if cancel_orders:
            if order_read_error:
                # Flattening must not depend on the order read; report that nothing was cancelled.
                cancel_errors.append(order_read_error)
            cancel_targets = [
                o
                for o in open_orders or []
                if o.account_id == account_id
                and o.symbol in market_map
                and (not args.symbol or o.symbol == args.symbol)
            ]
            if not args.preview:
                cancelled, failed = await _cancel_orders(client, account_id, cancel_targets)
                cancelled_first.extend(cancelled)
                cancel_errors.extend(failed)

        # Read positions after any cancellation, so a fill that raced the cancel is closed too.
        try:
            positions = await client.get_positions()
        except Exception as exc:
            if not cancel_orders:
                raise
            # Cancels may already have gone through: report them with the read failure.
            output_result(
                {
                    "action": "positions_close_all",
                    "symbol_filter": args.symbol,
                    "error": "Position read failed after cancelling orders; nothing was closed. Read positions and orders before retrying",
                    "cancelled_count": len(cancelled_first),
                    "cancelled": cancelled_first,
                    "errors": [
                        *({"context": "Cancel before close", **e} for e in cancel_errors),
                        {"context": "Position read", **exception_result(exc)},
                    ],
                }
            )
            return
        targets = [p for p in positions if p.account_id == account_id and Decimal(p.qty) > 0]
        if args.symbol:
            targets = [p for p in targets if p.symbol == args.symbol]

        close_orders = []
        would_close = []
        errors = []
        for pos in targets:
            below_min = False
            try:
                symbol = pos.symbol
                market = market_map.get(symbol)
                if market is None:
                    raise ValueError(f"Symbol '{symbol}' not found in market definitions.")

                tick_size = Decimal(market.tick_size)
                min_qty = Decimal(market.min_order_qty)

                # Opposite side closes the position.
                pos_is_long = pos.side.value == "B"
                close_is_buy = not pos_is_long

                price_data = await client.markets.get_perp_market_summary(symbol=symbol)
                mark_price = require_price(price_data.mark_price, f"{price_data.symbol} mark price")

                # Close the venue's reported position qty verbatim — no step rounding.
                # The venue produced this qty, so it can unwind it; flooring would
                # leave sub-step dust open after a "flatten".
                pos_qty = Decimal(pos.qty)
                below_min = pos_qty < min_qty

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
                if below_min:
                    entry["below_min_order_qty"] = True

                if args.preview:
                    entry["close_side"] = "buy" if close_is_buy else "sell"
                    entry["est_notional_usd"] = float((pos_qty * mark_price).quantize(Decimal("0.01")))
                    # TP/SL are whole-position orders: the venue cancels them once the position is flat.
                    entry["drops_protection"] = (
                        None
                        if open_orders is None
                        else [
                            {
                                "order_id": o.order_id,
                                "order_type": o.order_type.value,
                                "trigger_px": o.trigger_px,
                                "limit_px": o.limit_px,
                            }
                            for o in open_orders
                            if o.account_id == account_id
                            and o.symbol == symbol
                            and o.order_type.value in ("TAKE_PROFIT", "STOP_LOSS")
                        ]
                    )
                    would_close.append(entry)
                    continue

                def _close_params(qty_text: str) -> LimitOrderParameters:
                    return LimitOrderParameters(
                        symbol=symbol,
                        is_buy=close_is_buy,
                        limit_px=_fmt(limit_px),
                        qty=qty_text,
                        time_in_force=TimeInForce.IOC,
                        reduce_only=True,
                    )

                response = await client.create_limit_order(_close_params(pos.qty))
                entry.update(_order_result(response))
                close_orders.append(entry)
            except Exception as exc:
                error_entry: dict = {
                    "symbol": pos.symbol,
                    "qty": pos.qty,
                    **exception_result(exc),
                }
                if below_min:
                    error_entry["below_min_order_qty"] = True
                    error_entry["hint"] = BELOW_MIN_CLOSE_HINT
                errors.append(error_entry)

        result: dict = {
            "action": "positions_close_all",
            "symbol_filter": args.symbol,
            "slippage": float(slippage),
        }
        if cancel_orders:
            if args.preview:
                result["would_cancel_count"] = len(cancel_targets)
                result["would_cancel"] = [_order_preview(o) for o in cancel_targets]
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
            result["would_drop_protection_count"] = None if None in dropped else sum(map(len, dropped))
        else:
            result["close_orders"] = close_orders
            # A fill response is not settlement proof. Read the account back and
            # never report a position closed when it remains visible or unreadable.
            try:
                filled_symbols = {e["symbol"] for e in close_orders if e["status"] == OrderStatus.FILLED}
                for attempt in range(5):
                    remaining = [
                        p
                        for p in await client.get_positions()
                        if p.account_id == account_id
                        and (not args.symbol or p.symbol == args.symbol)
                        and Decimal(p.qty) != 0
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
            # Fee reads must not delay submission of other risk-reducing closes.
            retain_result(result)
            for entry in close_orders:
                await add_settled_fees(client, entry)
        if errors:
            result["error"] = "Some operations failed; inspect errors and account state before retrying"
            result["error_count"] = len(errors)
            result["errors"] = errors
        output_result(result)


async def _cmd_order_trigger(args: argparse.Namespace, trigger_type: OrderType) -> None:
    """Shared implementation for `order tp` and `order sl`.

    Reya trigger orders close the WHOLE position when fired (no qty parameter).
    The `--side` argument is the side of the position being protected; is_buy
    for the trigger order itself is derived (sell to close a long, buy to close
    a short). Two guardrails apply, both overridable with --force: the trigger
    must match an open position (existence + side), and it must not sit on the
    side of the mark price where it would fire immediately.
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

        # Guardrail: the trigger must protect a real position (existence + side). Position reads
        # trail the entry's fill, and a flip shows the old side first, so keep reading until the
        # expected side appears or the bound runs out.
        account_id = await require_owned_account(client)
        try:
            position, _ = await _await_position(
                client,
                account_id,
                args.symbol,
                lambda p: p is not None and _position_side(p) == position_side,
                reads=TRIGGER_POSITION_READS,
                timeout=TRIGGER_POSITION_WAIT_S,
            )
        except TimeoutError:
            raise ValueError(
                "Position read timed out; no protective order submitted. Reconcile the position before retrying"
            ) from None
        actual_side = None if position is None else _position_side(position)
        if _shared_client.get() is not None and actual_side != position_side:
            raise ValueError(
                "Position is not visible with the expected side after bounded reads; no protective order submitted. "
                "Query status and reconcile before retrying protection; do not repeat the entry"
            )
        position_warning = _check_position_match(args.symbol, position_side, actual_side, account_id, args.force)

        limit_px = _round_price(args.limit_px, tick_size, "down" if is_buy else "up")
        if limit_px <= 0:
            raise ValueError("--limit_px rounds to zero")

        # Triggers use the mark price, not the oracle or mid price.
        price_data = await client.markets.get_perp_market_summary(symbol=args.symbol)
        mark_price = require_price(price_data.mark_price, f"{price_data.symbol} mark price")
        plausibility_warning = _check_trigger_plausibility(
            trigger_type, position_side, trigger_px, mark_price, args.force
        )
        warnings = [w for w in (position_warning, plausibility_warning) if w]

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
        help="Bypass TP/SL guardrails (position match and immediate-fire checks)",
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
        help="Bypass TP/SL guardrails (position match and immediate-fire checks)",
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
            "--allow_large",
            action="store_true",
            default=False,
            help=(
                "Allow an order above the per-order notional cap (REYA_MAX_ORDER_NOTIONAL, default: the account "
                "value) or the estimated-leverage limit (REYA_MAX_LEVERAGE, default 5); only after the user confirms"
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
