"""A bounded, single-writer JSON-lines trading session for a dedicated account.

The agent supplies normal trade CLI arguments; the runner keeps the signer,
nonce lease, conservative submission budget and venue cancellation heartbeat.
"""

import asyncio
import contextvars
import json
import os
import re
import signal
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from pathlib import Path

import aiohttp
from sdk.open_api.exceptions import ApiException
from sdk.open_api.models.order import Order
from sdk.open_api.models.order_status import OrderStatus
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.side import Side
from sdk.open_api.models.time_in_force import TimeInForce

from agent_kit.scripts import trade
from agent_kit.scripts._account_data import AccountDataClient
from agent_kit.scripts._common import (
    JsonArgumentParser,
    capture_output,
    exception_result,
    output_json,
    output_result,
    require_owned_account,
    require_price,
    with_update_notice,
)
from agent_kit.scripts._env import get_trading_config
from agent_kit.scripts._executions import read_settled_fees
from agent_kit.scripts._live import LiveState, OwnFillPending, indexing_ceiling
from agent_kit.scripts._margin import FEE_TIERS_TIMEOUT_S, max_taker_fee
from agent_kit.scripts._orders import order_result
from agent_kit.scripts._settlement import SETTLEMENT_READ_S, read_settlement
from agent_kit.scripts._writer import CoordinatedTradingClient

PROTECTIVE_ORDER_TYPES = ("TAKE_PROFIT", "STOP_LOSS")
# Guardrail overrides the runner never accepts: its policy is the operator's limit.
OVERRIDE_FLAGS = ("--force", "--allow_large", "--skip_margin_check")
COUNTDOWN_MS = 30000
HEARTBEAT_INTERVAL_S = 10  # three renewals per 30 s countdown
# A failed renewal is retried, after HEARTBEAT_RETRY_S (or the venue's retryAfterMs when longer), while one more
# attempt bounded by WRITE_TIMEOUT_S can still be confirmed HEARTBEAT_MARGIN_S before the last confirmed deadline.
# Otherwise the session halts. A failed renewal leaves the previous countdown running at the venue.
HEARTBEAT_RETRY_S = 1.0
HEARTBEAT_MARGIN_S = 2.0
# Renewal failures worth another attempt: nothing reached the engine, the countdown budget was empty, or the
# outcome is unknown. Resending only restarts the countdown, and the deadline is kept from the last confirmed send.
RETRYABLE_RENEWAL_ERRORS = ("SERVICE_UNAVAILABLE_ERROR", "RATE_LIMITED_ERROR", "ORDER_OUTCOME_UNKNOWN_ERROR")
REFERENCE_TTL_S, REFERENCE_REFRESH_S = 60.0, 30.0
WARM_START_S = 5.0
FEE_DRAIN_S = 5.0
COMMAND_TIMEOUT_S = 15
WRITE_TIMEOUT_S = 5
# A deferred fee read waits for the venue to index the fill (seconds on devnet): it watches the
# warm execution feed, which costs no request, or re-reads REST with backoff when there is none.
FEE_SETTLE_WAIT_S = 30.0
FEE_FEED_POLL_S = 0.2
FEE_RETRY_S, FEE_RETRY_MAX_S = 1.0, 4.0
FEE_RESULT_KEYS = ("fees_status", "fees_rusd", "executions", "settled_qty")
# At stop, settlement evidence exists only once the venue has indexed the session's fills (about 1 s on
# mainnet, 12-15 s in devnet1 episodes). Within the deployment's indexing ceiling the runner first waits for
# its warm feed to show its own fills, which costs no request, then reads settlement with backoff for what
# remains, at least STOP_SETTLE_MIN_S.
STOP_SETTLE_MIN_S = 2.0
# Venue verdicts on one write that change nothing: the session keeps running (never retried).
# None: any write; otherwise only these commands.
DEFINITIVE_REJECTIONS = {
    "RATE_LIMITED_ERROR": None,
    # The API's contract: the request never reached the matching engine.
    "SERVICE_UNAVAILABLE_ERROR": None,
    "POST_ONLY_WOULD_CROSS_ERROR": None,
    "ORDER_NOT_FOUND_ERROR": ("cancel", "modify"),
    "MODIFY_QTY_BELOW_FILLED_ERROR": ("modify",),
    # The engine counts an opening fill only once it has settled, so a close sent right after it is refused.
    "REDUCE_ONLY_CONDITION_NOT_MET_ERROR": ("market",),
}


@dataclass(frozen=True)
class Policy:
    symbols: frozenset[str]
    max_order_notional: Decimal
    max_session_notional: Decimal
    max_slippage: Decimal
    max_commands: int
    idle_timeout_s: int

    @classmethod
    def load(cls, path: Path):
        data = json.loads(path.read_text())
        fields = {
            "symbols",
            "max_order_notional",
            "max_session_notional",
            "max_slippage",
            "max_commands",
            "idle_timeout_s",
        }
        if not isinstance(data, dict) or set(data) != fields:
            raise ValueError("Policy must contain exactly: " + ", ".join(sorted(fields)))
        symbols = data["symbols"]
        if (
            not isinstance(symbols, list)
            or not symbols
            or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]+RUSDPERP", s) for s in symbols)
        ):
            raise ValueError("Policy requires a nonempty list of perp symbols")
        values = [Decimal(str(data[k])) for k in ("max_order_notional", "max_session_notional", "max_slippage")]
        if any(not v.is_finite() or v <= 0 for v in values) or values[0] > values[1] or values[2] > Decimal("0.05"):
            raise ValueError("Positive budgets, order <= session and slippage <= 0.05 are required")
        if type(data["max_commands"]) is not int or not 1 <= data["max_commands"] <= 1000:
            raise ValueError("max_commands must be 1..1000")
        if type(data["idle_timeout_s"]) is not int or not 10 <= data["idle_timeout_s"] <= 3600:
            raise ValueError("idle_timeout_s must be 10..3600")
        return cls(frozenset(symbols), values[0], values[1], values[2], data["max_commands"], data["idle_timeout_s"])


async def flat_account(client) -> dict:
    account_id = await require_owned_account(client)
    orders = [o for o in await client.get_open_orders() if o.account_id == account_id]
    positions = [p for p in await client.get_positions() if p.account_id == account_id and Decimal(p.qty) != 0]
    return {
        "account_id": account_id,
        "open_order_ids": [o.order_id for o in orders],
        "orders": [order_result(o) for o in orders],
        "positions": [{"symbol": p.symbol, "side": p.side.value, "qty": p.qty} for p in positions],
    }


def position_protection(state: dict) -> list | None:
    """For each remaining position, the stop-loss and take-profit still armed for it (None when unknown)."""
    if state.get("positions") is None or state.get("orders") is None:
        return None
    result = []
    for position in state["positions"]:
        side = "long" if position["side"] == "B" else "short"
        entry = {"symbol": position["symbol"], "position_side": side, "qty": position["qty"]}
        for order_type, key in (("STOP_LOSS", "stop_loss"), ("TAKE_PROFIT", "take_profit")):
            armed = next(
                (
                    o
                    for o in state["orders"]
                    if o["symbol"] == position["symbol"]
                    and o["order_type"] == order_type
                    and o.get("position_side") == side
                ),
                None,
            )
            entry[key] = armed and {k: armed[k] for k in ("order_id", "trigger_px", "limit_px")}
        result.append(entry)
    return result


def exposure_message(protection: list) -> str:
    parts = []
    for p in protection:
        kept = [name for name, key in (("stop-loss", "stop_loss"), ("take-profit", "take_profit")) if p[key]]
        status = " and ".join(kept) + " kept" if kept else "NO stop-loss or take-profit"
        parts.append(f"{p['symbol']} {p['position_side']} {p['qty']} ({status})")
    return (
        "Position left open: "
        + "; ".join(parts)
        + ". The runner never closes positions; close it explicitly or confirm its protection, then reconcile before another session"
    )


def unconfirmed_fills(acknowledgements: list, settlement: dict) -> list:
    """Acknowledged fills the settlement reads have not confirmed (an unindexed fill is in no read yet)."""
    if settlement["state"] != "unresolved":
        return []
    # Without per-order results (the reads failed), every acknowledged fill is unconfirmed.
    pending = settlement.get("unconfirmed_orders")
    keys = None if pending is None else {(o["symbol"], o["order_id"]) for o in pending}
    latest = {(a["symbol"], a["order_id"]): a for a in acknowledgements}  # an order's last acknowledgement
    result = []
    for key, ack in latest.items():
        try:
            filled = Decimal(str(ack.get("filled_qty") or 0)) > 0
        except ArithmeticError:
            filled = True  # unreadable: it may have filled
        if filled and (keys is None or key in keys):
            result.append({k: ack.get(k) for k in ("symbol", "order_id", "side", "filled_qty")})
    return result


def unconfirmed_message(fills: list) -> str:
    parts = [
        f"{f['symbol']} {'buy' if f['side'] == 'B' else 'sell'} {f['filled_qty']} (order {f['order_id']})"
        for f in fills
    ]
    return (
        "Acknowledged fills not yet confirmed by settlement: "
        + "; ".join(parts)
        + ". The venue may not have indexed them yet, so the final position reads can leave them out; check "
        "query positions and query executions --order_id before another session"
    )


class CachedReference:
    """Reference data changes by governance, not per order: serve reads up to REFERENCE_TTL_S old.

    The session refreshes it in the background every REFERENCE_REFRESH_S, so an order never waits.
    """

    CACHED = ("get_perp_market_definitions", "get_asset_definitions", "get_fee_tier_parameters")

    def __init__(self, api, clock=time.monotonic):
        self._api, self._clock, self._cache = api, clock, {}

    def __getattr__(self, name):
        return getattr(self._api, name)

    def cached(self, name: str):
        hit = self._cache.get(name)
        return hit[1] if hit is not None and self._clock() - hit[0] <= REFERENCE_TTL_S else None

    async def _read(self, name: str, *, fresh: bool = False):
        hit = None if fresh else self.cached(name)
        if hit is not None:
            return hit
        value = await getattr(self._api, name)()
        self._cache[name] = (self._clock(), value)
        return value

    async def refresh(self) -> None:
        for name in self.CACHED:
            if name in self._cache:
                try:
                    await self._read(name, fresh=True)
                except Exception:
                    pass  # served until REFERENCE_TTL_S, then read again by the next caller

    async def get_perp_market_definitions(self):
        return await self._read("get_perp_market_definitions")

    async def get_asset_definitions(self):
        return await self._read("get_asset_definitions")

    async def get_fee_tier_parameters(self):
        return await self._read("get_fee_tier_parameters")


class WarmMarkets:
    """A market summary from the session's WebSocket when it pushed within its bound, else REST."""

    def __init__(self, api, client):
        self._api, self._client = api, client

    def __getattr__(self, name):
        return getattr(self._api, name)

    async def get_perp_market_summary(self, symbol, **kwargs):
        live = getattr(self._client, "live", None)
        try:
            summary = live.mark_summary(symbol) if live is not None and not kwargs else None
        except Exception:
            summary = None  # never let warm state fail a read: REST decides
        return summary if summary is not None else await self._api.get_perp_market_summary(symbol, **kwargs)


class RunnerClient(CoordinatedTradingClient):
    def configure(self, policy: Policy, live: LiveState | None = None):
        self.policy = policy
        self.submitted = Decimal(0)
        self.operation_lock = asyncio.Lock()
        self.write_attempts = 0
        self.heartbeat_failed = False
        self.heartbeat_deadline: float | None = None
        # Renewal retries for the operator: count, the last retried error, and the error that halted, if any.
        self.heartbeat_record: dict = {"retries": 0, "last_retry": None, "halted_by": None}
        self.ready = False
        self.live = live
        self.accounts: list | None = None
        self.request_id: str | None = None
        self.deferred_fees: list = []
        self.fee_tasks: set = set()

    @property
    def reference(self):
        cache = self.__dict__.get("_reference")
        if cache is None:
            cache = self.__dict__["_reference"] = CachedReference(super().reference)
        return cache

    @property
    def markets(self):
        warm = self.__dict__.get("_markets")
        if warm is None:
            warm = self.__dict__["_markets"] = WarmMarkets(super().markets, self)
        return warm

    async def start(self):
        if not self.ready:
            await super().start()
            self.ready = True

    async def warm_up(self) -> dict:
        """Load fee tiers, then the WebSocket state; neither is required to trade (REST is the fallback)."""
        try:
            await asyncio.wait_for(self.reference.get_fee_tier_parameters(), timeout=FEE_TIERS_TIMEOUT_S)
        except Exception:
            pass
        if self.live is None:
            return {"source": "rest"}
        self.live.on_fill_lag = self.record_fill_lag
        return await self.live.start(timeout=WARM_START_S)

    async def get_accounts(self):
        # Account owners never change on mainnet: the owned-account check is read once per session.
        if getattr(self, "accounts", None) is not None:
            return self.accounts
        accounts = await super().get_accounts()
        if any(a.account_id == self.config.account_id for a in accounts):
            self.accounts = accounts
        return accounts

    def warm_margin_inputs(self, account_id: int, symbols) -> dict | None:
        """Balances, positions, marks and collateral prices from the WebSocket, or None to read REST.

        Raises OwnFillPending (a ValueError: no order is sent) while an acknowledged fill cannot be
        counted, because REST would leave it out too.
        """
        live = getattr(self, "live", None)
        if live is None or account_id != self.config.account_id:
            return None
        try:
            wallet = live.wallet_inputs()
            if wallet is None:
                return None
            balances, positions = wallet
            summaries = {}
            for symbol in {p.symbol for p in positions} | set(symbols):
                summary = live.mark_summary(symbol)
                if summary is not None:
                    summaries[symbol] = summary
            prices = live.oracle_prices()
        except OwnFillPending:
            raise
        except Exception as exc:
            live.invalidate(f"Warm wallet state unusable ({type(exc).__name__}); reading REST")
            return None
        return {
            "balances": balances,
            "positions": positions,
            "summaries": summaries,
            "asset_prices": prices,
        }

    def warm_position(self, account_id: int, symbol: str):
        """The position as the verified WebSocket published it (LiveState.published_position), or None: read REST."""
        live = getattr(self, "live", None)
        if live is None or account_id != self.config.account_id:
            return None
        try:
            return live.published_position(symbol)
        except Exception:
            return None  # never let warm state fail a check: REST decides

    def warm_published_orders(self, account_id: int) -> list | None:
        """Open orders as the verified WebSocket published them (LiveState.published_open_orders), or None: read REST."""
        live = getattr(self, "live", None)
        if live is None or account_id != self.config.account_id:
            return None
        try:
            return live.published_open_orders()
        except Exception:
            return None  # never let warm state fail a check: REST decides

    async def wait_own_fills(self, timeout: float) -> None:
        """Wait up to `timeout` s for the warm feed to show the session's own fills (LiveState.own_fills_shown)."""
        live = getattr(self, "live", None)
        if live is not None:
            await live.own_fills_shown(timeout)

    async def warm_wallet_changed(self, timeout: float) -> None:
        """Wait for the next position or order update, or loss of verified wallet state (LiveState.wallet_changed)."""
        live = getattr(self, "live", None)
        if live is not None:
            await live.wallet_changed(timeout)

    def warm_open_orders(self) -> list | None:
        live = getattr(self, "live", None)
        try:
            return None if live is None else live.open_orders()
        except Exception:
            return None

    def record_fill_lag(self, record: dict) -> None:
        """Journal an own fill's indexing lag as `fill_lags` (LiveState.on_fill_lag), to size the indexing ceiling."""
        self.lease.state["runner"].setdefault("fill_lags", []).append(record)
        self.lease.save()

    def defer_settled_fees(self, result: dict) -> None:
        """Read a fill's fees after the command's result is out; see emit_fee_events."""
        keys = ("symbol", "order_id", "client_order_id", "filled_qty", "fill_count", "first_fill_id")
        self.deferred_fees.append((self.request_id, {k: result.get(k) for k in keys}))

    def start_fee_reads(self) -> None:
        pending, self.deferred_fees = self.deferred_fees, []
        for request_id, fill in pending:
            # A fresh context: nothing the command captured or shared leaks into the read.
            task = asyncio.get_running_loop().create_task(
                self._fee_event(request_id, fill), context=contextvars.Context()
            )
            self.fee_tasks.add(task)
            task.add_done_callback(self.fee_tasks.discard)

    async def _fee_event(self, request_id, fill: dict) -> None:
        """Read a fill's fees once the venue has indexed it, then emit them.

        A read straight after the result finds nothing: the execution is indexed after the
        fill settles. The event always goes out, with what is known at FEE_SETTLE_WAIT_S or
        when shutdown cuts the wait short (fees_status "pending").
        """
        fill.update(fees_status="pending", fees_rusd=None, executions=[])
        deadline = time.monotonic() + FEE_SETTLE_WAIT_S
        retry = FEE_RETRY_S
        found = None  # the last read that found executions: a later failed read must not erase them
        try:
            while True:
                # Read once the fill may be indexed, and at the deadline in case the feed missed it.
                if self._fill_indexed(fill) is not False or deadline - time.monotonic() <= 0:
                    await read_settled_fees(self, fill)  # one bounded read, as before
                    if fill["executions"]:
                        found = {k: fill.get(k) for k in FEE_RESULT_KEYS}
                    elif found is not None:
                        fill.update(found)
                    if fill["fees_status"] in ("complete", "not_filled") or _filled_since_ack(fill):
                        return
                    wait, retry = retry, min(retry * 2, FEE_RETRY_MAX_S)
                else:
                    wait = FEE_FEED_POLL_S  # the feed has not shown it: a read would find nothing
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                await asyncio.sleep(min(wait, remaining))
        finally:
            if found is not None and not fill.get("executions"):
                fill.update(found)  # cut short mid-read
            event = {"event": "fees", "request_id": request_id, **fill}
            self.lease.state["runner"].setdefault("fee_reads", []).append(event)
            self.lease.save()
            emit(event)

    def _fill_indexed(self, fill: dict) -> bool | None:
        """Whether the warm execution feed shows the whole fill; None without a verified feed to ask."""
        live = getattr(self, "live", None)
        try:
            if live is None or not live.wallet_ready():
                return None
            return live.executions_qty(fill["order_id"]) >= Decimal(fill["filled_qty"])
        except Exception:
            return None

    async def finish_fee_reads(self) -> None:
        self.start_fee_reads()
        if self.fee_tasks:
            await asyncio.wait(set(self.fee_tasks), timeout=FEE_DRAIN_S)
        for task in set(self.fee_tasks):
            task.cancel()
        await asyncio.gather(*self.fee_tasks, return_exceptions=True)

    async def refresh_reference(self) -> None:
        while True:
            await asyncio.sleep(REFERENCE_REFRESH_S)
            await self.reference.refresh()

    def _before_send(self, symbol: str):
        live = getattr(self, "live", None)
        return None if live is None else live.before_send(symbol)

    def _after_ack(self, token, params, response, *, order_type: OrderType, trigger_px=None, time_in_force=None):
        live = getattr(self, "live", None)
        if live is None or token is None:
            return
        try:
            self._record_ack(live, token, params, response, order_type, trigger_px, time_in_force)
        except Exception as exc:
            # The order is acknowledged and journaled; only the warm view is in doubt, so read REST.
            live.invalidate(f"Could not record an acknowledgement in the warm state ({type(exc).__name__})")

    def _record_ack(self, live, token, params, response, order_type, trigger_px, time_in_force) -> None:
        status = response.status.value
        executed = response.exec_qty if response.exec_qty is not None else response.cum_qty
        filled = Decimal(executed or "0")
        price = Decimal(response.avg_fill_px or params.limit_px)
        resting = None
        if status == "OPEN":
            now = int(time.time() * 1000)
            resting = Order(
                exchange_id=self.config.dex_id_override,
                symbol=params.symbol,
                account_id=self.config.account_id,
                order_id=str(response.order_id),
                client_order_id=response.client_order_id,
                qty=getattr(params, "qty", None),
                cum_qty=response.cum_qty,
                side=Side.B if params.is_buy else Side.A,
                limit_px=params.limit_px,
                order_type=order_type,
                trigger_px=trigger_px,
                time_in_force=time_in_force,
                post_only=getattr(params, "post_only", None),
                reduce_only=getattr(params, "reduce_only", None),
                status=OrderStatus.OPEN,
                created_at=now,
                last_update_at=now,
            )
        live.on_ack(
            token,
            order_id=str(response.order_id),
            is_buy=params.is_buy,
            filled_qty=filled,
            price=price,
            order_cum_qty=Decimal(response.cum_qty or executed or "0"),
            fee_rate=max_taker_fee(self.reference.cached("get_fee_tier_parameters") or []),
            resting=resting,
        )

    def check_heartbeat(self):
        if self.heartbeat_deadline is not None and time.monotonic() >= self.heartbeat_deadline:
            self.heartbeat_failed = True
        if self.heartbeat_failed:
            raise ValueError("Cancellation heartbeat failed or expired; trading is halted for reconciliation")

    async def _write(self, call, *, cleanup=False, record_attempt=True):
        async with self.operation_lock:
            if not cleanup:
                self.check_heartbeat()
            if record_attempt:
                self.write_attempts += 1
            return await asyncio.wait_for(call(), timeout=WRITE_TIMEOUT_S)

    async def _reserve(self, params):
        if params.symbol not in self.policy.symbols:
            raise ValueError("Symbol is outside the runner policy")
        qty = require_price(params.qty, "quantity")
        snapshot = await asyncio.wait_for(self.markets.get_perp_market_summary(symbol=params.symbol), timeout=3)
        if snapshot.prices_updated_at is None or not -5000 <= time.time() * 1000 - snapshot.prices_updated_at <= 30000:
            raise ValueError("Market price is unavailable or stale; no order submitted")
        price = max(require_price(params.limit_px, "limit price"), require_price(snapshot.mark_price, "mark price"))
        # Charge the full intended order, including modifies, BEFORE submission.
        # Never release budget on cancellation or lagging position/order reads.
        # If extreme precision requires rounding, charge upwards, never past a cap.
        with localcontext() as context:
            context.prec = 80
            context.rounding = ROUND_CEILING
            notional = qty * price
            submitted = self.submitted + notional
        if not params.reduce_only and notional > self.policy.max_order_notional:
            raise ValueError("Order exceeds max_order_notional")
        if not params.reduce_only:
            if submitted > self.policy.max_session_notional:
                raise ValueError(
                    "Session submission budget exhausted; reduce-only closes/cancellations remain available"
                )
            self.submitted = submitted
            self.lease.state["runner"]["submitted_notional"] = str(self.submitted)
            self.lease.save()

    async def create_limit_order(self, params):
        await self._reserve(params)
        token = self._before_send(params.symbol)
        response = await self._write(lambda: super(RunnerClient, self).create_limit_order(params))
        self.record_acknowledgement(params, response)
        self._after_ack(token, params, response, order_type=OrderType.LIMIT, time_in_force=params.time_in_force)
        return response

    async def create_trigger_order(self, params):
        if params.symbol not in self.policy.symbols:
            raise ValueError("Symbol is outside the runner policy")
        token = self._before_send(params.symbol)
        response = await self._write(lambda: super(RunnerClient, self).create_trigger_order(params))
        self.record_acknowledgement(params, response)
        self._after_ack(
            token,
            params,
            response,
            order_type=OrderType(params.trigger_type),
            trigger_px=params.trigger_px,
            time_in_force=params.time_in_force,
        )
        return response

    async def modify_order(self, params):
        await self._reserve(params)
        token = self._before_send(params.symbol)
        response = await self._write(lambda: super(RunnerClient, self).modify_order(params))
        self.record_acknowledgement(params, response)
        self._after_ack(token, params, response, order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC)
        return response

    def record_acknowledgement(self, params, response):
        # Persist before any optional fee/position reads. A crash after sending
        # but before this save still leaves the request's durable intent pending.
        ack = {
            "symbol": params.symbol,
            "order_id": response.order_id,
            "side": "B" if params.is_buy else "A",
            "status": response.status.value,
            "filled_qty": response.cum_qty,
            "first_fill_id": response.first_fill_id,
        }
        self.lease.state["runner"].setdefault("acknowledged_orders", []).append(ack)
        self.lease.save()

    async def cancel_order(self, **kwargs):
        response = await self._write(lambda: super(RunnerClient, self).cancel_order(**kwargs))
        live = getattr(self, "live", None)
        if live is not None and response.status == OrderStatus.CANCELLED:
            live.on_cancelled(str(kwargs.get("order_id")))
        return response

    async def refresh(self):
        started = time.monotonic()
        response = await self._write(
            lambda: super(RunnerClient, self).cancel_all_after(timeout_ms=COUNTDOWN_MS), record_attempt=False
        )
        if (
            response.account_id != self.config.account_id
            or response.timeout_ms != COUNTDOWN_MS
            or response.trigger_at is None
        ):
            raise ValueError("Venue did not confirm the cancellation countdown")
        # Measure conservatively from before submission, never from a late reply.
        # Check the OLD deadline before accepting any renewal after a stall.
        self.check_heartbeat()
        self.heartbeat_deadline = started + COUNTDOWN_MS / 1000
        self.check_heartbeat()

    def note_renewal_failure(self, error: dict, *, halted: bool = False) -> None:
        """Record a failed renewal where the operator sees it: the journal, `status` and the stopped event."""
        entry = {**error, "at_ms": int(time.time() * 1000)}
        if halted:
            self.heartbeat_record["halted_by"] = entry
        else:
            self.heartbeat_record["retries"] += 1
            self.heartbeat_record["last_retry"] = entry
        journal = self.lease.state.get("runner")
        if isinstance(journal, dict):
            journal["heartbeat"] = self.heartbeat_record
            self.lease.save()

    async def disarm(self):
        response = await self._write(
            lambda: super(RunnerClient, self).cancel_all_after(timeout_ms=0), record_attempt=False
        )
        if response.account_id != self.config.account_id or response.timeout_ms != 0 or response.trigger_at is not None:
            raise ValueError("Venue did not confirm countdown disarm")

    async def cleanup(self) -> dict:
        """Cancel the session's working orders without stripping a position's TP/SL.

        A user cancelAll also clears armed TP/SL in the matching engine (only the
        venue countdown's own fire keeps them), so it is used only when the account
        is visibly flat with no TP/SL. Otherwise, including when a read fails, each
        other order is cancelled individually and TP/SL stay armed; the countdown is
        then left armed and sweeps anything these reads missed, still keeping TP/SL.
        """
        account_id = self.config.account_id

        async def read(call):
            try:
                return await asyncio.wait_for(call(), timeout=5)
            except Exception:
                return None  # Unknown: treated as possibly protecting a position.

        positions, orders = await asyncio.gather(read(self.get_positions), read(self.get_open_orders))
        open_positions = (
            None if positions is None else [p for p in positions if p.account_id == account_id and Decimal(p.qty) != 0]
        )
        own_orders = None if orders is None else [o for o in orders if o.account_id == account_id]
        protective = [o for o in own_orders or [] if o.order_type.value in PROTECTIVE_ORDER_TYPES]
        if open_positions == [] and own_orders is not None and not protective:
            # Nothing to protect: one account-wide cancel also covers orders the read missed.
            await self._write(lambda: super(RunnerClient, self).mass_cancel(), cleanup=True)
            return {"mode": "cancel_all"}
        report: dict = {
            "mode": "kept_protection",
            "kept_order_ids": [o.order_id for o in protective],
            "cancelled_order_ids": [],
            "errors": [],
        }
        if own_orders is None:
            report["errors"].append({"error": "Open orders unreadable; nothing was cancelled"})
            return report
        for order in own_orders:
            if order in protective:
                continue
            try:
                response = await self._write(
                    lambda o=order: super(RunnerClient, self).cancel_order(
                        symbol=o.symbol, account_id=account_id, order_id=o.order_id
                    ),
                    cleanup=True,
                )
                if response.status != OrderStatus.CANCELLED:
                    raise ValueError(f"Cancellation not confirmed: {response.status}")
                report["cancelled_order_ids"].append(order.order_id)
            except Exception as exc:
                report["errors"].append({"order_id": order.order_id, **exception_result(exc)})
        return report


def _filled_since_ack(fill: dict) -> bool:
    """The order has filled more since its acknowledgement: its fees never match the acknowledged fill alone."""
    settled = fill.get("settled_qty")
    return settled is not None and Decimal(settled) > Decimal(fill["filled_qty"])


def renewal_retry_wait(exc: Exception) -> tuple[dict, float | None]:
    """A failed renewal as reported, and the wait before retrying it; None means halt now.

    Retried: SERVICE_UNAVAILABLE_ERROR (never reached the engine), RATE_LIMITED_ERROR (after its
    retryAfterMs), ORDER_OUTCOME_UNKNOWN_ERROR and other 5xx, timeouts and transport errors. Never
    retried: the edge's per-IP ban (EDGE_RATE_LIMITED lasts 60 s, longer than the countdown), a
    countdown the venue did not confirm, a missed deadline, and validation or permission errors.
    """
    if isinstance(exc, (TimeoutError, aiohttp.ClientError, ConnectionError)):
        return {"error_code": "TRANSPORT", "error": f"{type(exc).__name__}: {exc}"[:300]}, HEARTBEAT_RETRY_S
    if not isinstance(exc, ApiException):
        return {"error": str(exc)[:300]}, None
    result = exception_result(exc)
    error = {k: result[k] for k in ("error_code", "error", "http_status", "retry_after_ms") if k in result}
    code, status = result.get("error_code"), result.get("http_status")
    if status == 400 and code in RETRYABLE_RENEWAL_ERRORS:
        return error, max(HEARTBEAT_RETRY_S, result.get("retry_after_ms", 0) / 1000)
    if code != "EDGE_RATE_LIMITED" and isinstance(status, int) and status >= 500:
        return error, HEARTBEAT_RETRY_S
    return error, None


async def renew(client) -> None:
    """Renew the countdown, retrying a retryable failure while another attempt can still beat the deadline.

    The deadline is the last confirmed one: refresh() measures it from before each send and checks
    the old deadline before accepting a renewal, so a stall is never silently re-armed. The wait
    between attempts holds no lock, so trade writes go on until that deadline.
    """
    while True:
        try:
            await client.refresh()
            return
        except Exception as exc:
            error, wait = renewal_retry_wait(exc)
            deadline = client.heartbeat_deadline
            if (
                wait is None
                or deadline is None
                or time.monotonic() + wait + WRITE_TIMEOUT_S + HEARTBEAT_MARGIN_S > deadline
            ):
                client.note_renewal_failure(error, halted=True)
                raise
            client.note_renewal_failure(error)
            await asyncio.sleep(wait)


async def heartbeat(client, failed: asyncio.Event):
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            await renew(client)
    except asyncio.CancelledError:
        raise
    except Exception:
        client.heartbeat_failed = True
        failed.set()


def emit(value: dict):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def command_timeout_s(client, args) -> float:
    """The bound on one command inside the runner.

    A TP/SL also waits for its position (up to the deployment's ceiling) before it writes:
    `order sl` / `order tp` then send one trigger; a protected entry sends the stop-loss and
    take-profit and reads them back. Cutting it off between the fill and the stop would leave
    the position unprotected. `positions close_all` waits, within the same ceiling, for the
    session's own fills and for a position its TP/SL say is there.
    """
    trigger = getattr(args, "group", None) == "order" and getattr(args, "action", None) in ("sl", "tp")
    close_all = getattr(args, "group", None) == "positions" and getattr(args, "action", None) == "close_all"
    if not trigger and not close_all and not trade.protection_requested(args):
        return COMMAND_TIMEOUT_S
    _, wait_s = trade.entry_position_bounds(client)
    if close_all:
        return COMMAND_TIMEOUT_S + wait_s
    if trigger:
        return COMMAND_TIMEOUT_S + wait_s + WRITE_TIMEOUT_S
    return COMMAND_TIMEOUT_S + wait_s + 2 * WRITE_TIMEOUT_S + trade.TRIGGER_READBACK_WAIT_S


def definitive_outcome(result: dict, command: list) -> bool:
    """True when one write's failure changed nothing it did not report: a venue verdict or a terminal ack.

    Only single-write order commands qualify; anything ambiguous (timeouts, transport errors,
    5xx, UNKNOWN status, unprotected entries, other codes) still stops the session.
    """
    action = command[1] if len(command) > 1 and command[0] == "order" else None
    if action not in ("market", "limit", "modify", "cancel", "tp", "sl"):
        return False
    protection = result.get("protection")
    if protection is not None and protection.get("state") not in ("protected", "not_placed"):
        return False
    code, http_status = result.get("error_code"), result.get("http_status")
    if code is not None:
        if code == "EDGE_RATE_LIMITED" and http_status == 429:
            return True  # Reya's edge refused it before the API
        if http_status != 400 or code not in DEFINITIVE_REJECTIONS:
            return False
        actions = DEFINITIVE_REJECTIONS[code]
        return actions is None or action in actions
    # The acknowledgement says exactly what happened, e.g. an IOC that filled part and cancelled the rest.
    status = getattr(result.get("status"), "value", result.get("status"))
    return (
        action in ("market", "limit", "modify")
        and result.get("order_id") is not None
        and status == OrderStatus.CANCELLED.value
        and result.get("filled_qty") is not None
    )


async def execute_request(client, request: dict, intent: dict | None = None) -> dict:
    command = request.get("command")
    client.request_id = request.get("id")
    if (
        not isinstance(command, list)
        or not 1 <= len(command) <= 40
        or any(not isinstance(x, str) or len(x) > 128 for x in command)
    ):
        raise ValueError("command must be a bounded array of trade CLI arguments")
    if any(x in ("--help", "-h", *OVERRIDE_FLAGS) for x in command):
        raise ValueError("Help and guardrail overrides are unavailable inside the runner")
    with capture_output() as output:
        try:
            args = trade.build_parser().parse_args(command)
        except SystemExit:
            return output[-1] if output else {"error": "Invalid command"}
        if any(getattr(args, flag.lstrip("-"), False) for flag in OVERRIDE_FLAGS):
            raise ValueError("Runner guardrail overrides are unavailable")
        if hasattr(args, "slippage") and args.slippage > client.policy.max_slippage:
            raise ValueError("Slippage exceeds the runner policy")
        if getattr(args, "symbol", None) and args.symbol not in client.policy.symbols:
            raise ValueError("Symbol is outside the runner policy")
        if intent is not None:
            intent["command"] = command
            client.lease.save()
        function = getattr(trade, f"cmd_{args.group}_{args.action}")
        token = trade._shared_client.set(client)

        async def invoke():
            try:
                await function(args)
            except SystemExit:
                # Catch inside the task: SystemExit escaping a task stops the loop.
                # Keep structured partial/unknown outcomes, including fill IDs.
                pass

        try:
            await asyncio.wait_for(invoke(), timeout=command_timeout_s(client, args))
        except TimeoutError:
            if not output:
                raise
            return {
                **output[-1],
                "error": "Command timed out after an acknowledgement; reconcile account state before another write",
            }
        finally:
            trade._shared_client.reset(token)
        return output[-1] if output else {"error": "Command returned no outcome"}


async def serve(reader: asyncio.StreamReader, client, policy: Policy, *, ack_previous: str | None = None) -> dict:
    await asyncio.wait_for(client.start(), timeout=15)
    initial = await asyncio.wait_for(flat_account(client), timeout=15)
    if initial["positions"] or initial["open_order_ids"]:
        raise ValueError("Runner requires a dedicated account with no positions or open orders at startup")
    previous = client.lease.state.get("runner")
    if previous and not previous.get("reconciled") and ack_previous != previous["session_id"]:
        raise ValueError(
            f"Prior session {previous['session_id']} needs operator reconciliation; inspect its journal and account before --ack-previous-session"
        )
    # Flat reads alone cannot prove an ambiguous prior submission never reached the venue.
    session_id = str(uuid.uuid4())
    journal = {
        "session_id": session_id,
        "account_id": client.config.account_id,
        "reconciled": False,
        "submitted_notional": "0",
        "requests": [],
        "acknowledged_orders": [],
        "acknowledged_previous": ack_previous,
        "heartbeat": client.heartbeat_record,
    }
    client.lease.state["runner"] = journal
    client.lease.save()
    failed = asyncio.Event()
    monitor = None
    failure_wait = None
    pending_read = None
    refresher = None
    reason = "eof"
    try:
        # Warm state is optional: without it every input is read over REST, as before.
        warm_state = await client.warm_up()
        refresher = asyncio.create_task(client.refresh_reference())
        await client.refresh()  # Do not admit a single command before the venue arms the timer.
        monitor = asyncio.create_task(heartbeat(client, failed))
        failure_wait = asyncio.create_task(failed.wait())
        # kit_update from the last daily check only: nothing here may wait on the network
        # once the heartbeat is armed.
        ready = {
            "event": "ready",
            "journal_path": str(client.lease.directory / (client.lease.keys[0] + ".json")),
            "session_id": session_id,
            "account_id": client.config.account_id,
            "cancel_all_after_ms": COUNTDOWN_MS,
            "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
            "max_session_notional": str(policy.max_session_notional),
            "warm_state": warm_state,
        }
        emit(with_update_notice(ready, refresh=False))
        seen: set[str] = set()
        request_count = 0
        while request_count < policy.max_commands:
            pending_read = asyncio.create_task(reader.readline())
            done, _ = await asyncio.wait(
                {pending_read, failure_wait},
                timeout=policy.idle_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            try:
                client.check_heartbeat()
            except ValueError:
                reason = "heartbeat_failed"
                break
            if failure_wait in done:
                reason = "heartbeat_failed"
                break
            if pending_read not in done:
                reason = "idle_timeout"
                break
            line = pending_read.result()
            if not line:
                break
            if len(line) > 8192:
                reason = "oversized_request"
                break
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"id", "command"}:
                raise ValueError("Requests must contain exactly id and command")
            request_id = request["id"]
            if (
                not isinstance(request_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id)
                or request_id in seen
            ):
                raise ValueError("Request ID must be unique in this session, using 1..80 letters/digits/_/-")
            seen.add(request_id)
            request_count += 1
            if request["command"] == ["stop"]:
                reason = "stop"
                break
            if request["command"] == ["status"]:
                emit(
                    {
                        "id": request_id,
                        "result": await asyncio.wait_for(flat_account(client), timeout=10),
                        "submitted_notional": str(client.submitted),
                        "heartbeat": dict(client.heartbeat_record),
                    }
                )
                continue
            entry: dict = {"id": request_id, "state": "intent"}
            journal["requests"].append(entry)
            client.lease.save()  # Intent survives a crash; it is never automatically replayed.
            before = client.write_attempts
            try:
                result = await execute_request(client, request, entry)
            except ValueError as exc:
                result = {"error": str(exc)}
            except Exception as exc:
                result = exception_result(
                    exc, fallback=f"{type(exc).__name__}: command failed; inspect account state before another write"
                )
            writes = client.write_attempts - before
            # A venue verdict on one write, or a terminal acknowledgement, leaves nothing to reconcile.
            halts = bool(result.get("error")) and writes > 0
            if halts and writes == 1 and definitive_outcome(result, request["command"]):
                halts = False
                result["session_continues"] = True
            entry.update(state="completed", result=result)
            client.lease.save()
            emit({"id": request_id, "result": result, "submitted_notional": str(client.submitted)})
            client.start_fee_reads()
            try:
                client.check_heartbeat()
            except ValueError:
                reason = "heartbeat_failed"
                break
            if halts:
                reason = "write_requires_reconciliation"
                break
        else:
            reason = "command_limit"
    except (Exception, asyncio.CancelledError):
        reason = "interrupted_or_failed"
    finally:
        tasks = [t for t in (monitor, failure_wait, pending_read, refresher) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Leave the server countdown armed even if local cleanup/readback fails.
        cleanup_error = None
        cleanup: dict = {"mode": "unconfirmed"}
        try:
            cleanup = await client.cleanup()
        except Exception:
            cleanup_error = "Immediate cancellation unconfirmed; venue countdown remains armed"
        if cleanup.get("errors"):
            cleanup_error = (
                "Some orders were not confirmed cancelled; the venue countdown remains armed and cancels them, "
                "keeping TP/SL"
            )
        settle_s = indexing_ceiling(client.config.api_url)
        settle_by = time.monotonic() + settle_s
        await client.wait_own_fills(settle_s)  # the fee reads below also need them indexed
        await client.finish_fee_reads()
        budget = max(settle_by - time.monotonic(), STOP_SETTLE_MIN_S)
        try:
            settlement = await asyncio.wait_for(
                read_settlement(client, journal["acknowledged_orders"], budget=budget),
                timeout=budget + SETTLEMENT_READ_S + 1,  # a backstop: the reads stop at the budget themselves
            )
        except Exception:
            settlement = {
                "state": "unresolved",
                "reason": "Settlement read budget exhausted; inspect session fills/busts",
            }
        journal["settlement"] = settlement
        unconfirmed = unconfirmed_fills(journal["acknowledged_orders"], settlement)
        journal["unconfirmed_fills"] = unconfirmed
        client.lease.save()
        try:
            final = await asyncio.wait_for(flat_account(client), timeout=10)
        except Exception:
            final = {"positions": None, "open_order_ids": None, "orders": None}
        journal["reconciled"] = (
            reason in ("eof", "stop", "command_limit", "idle_timeout")
            and cleanup_error is None
            and settlement["state"] == "settled_flat"
            and final["positions"] == []
            and final["open_order_ids"] == []
            and all(entry["state"] == "completed" for entry in journal["requests"])
        )
        if journal["reconciled"]:
            try:
                await client.disarm()
            except Exception:
                cleanup_error = "Countdown disarm unconfirmed; wait for expiry and reconcile before new orders"
                journal["reconciled"] = False
        protection = position_protection(final)
        journal.update(stop_reason=reason, final_state=final, cleanup_error=cleanup_error, cleanup=cleanup)
        client.lease.save()
        if client.live is not None:
            await client.live.close()
    result = {
        "event": "stopped",
        "session_id": session_id,
        "reason": reason,
        "final_state": final,
        "reconciled": journal["reconciled"],
        "settlement": journal["settlement"],
        "unconfirmed_fills": unconfirmed,
        "positions_auto_closed": False,
        "cleanup": cleanup,
        "protection": protection,
        "countdown_status": "disarmed" if journal["reconciled"] else "may_be_armed",
        "heartbeat": dict(client.heartbeat_record),
    }
    if not journal["reconciled"]:
        problems = [
            cleanup_error,
            protection and exposure_message(protection),
            unconfirmed and unconfirmed_message(unconfirmed),
        ]
        result["error"] = " ".join(p for p in problems if p) or (
            "Inspect remaining exposure and journal before starting another session"
        )
    emit(result)
    return result


def live_state(client, policy: Policy) -> LiveState | None:
    """The session's WebSocket state, or None where no public WebSocket deployment is mapped."""
    config = client.config
    try:
        return LiveState(config.api_url, config.owner_wallet_address, config.account_id, policy.symbols, rest=client)
    except ValueError:
        return None


async def run(args):
    policy = Policy.load(args.policy)
    if not args.execute:
        async with AccountDataClient(get_trading_config(include_private_key=False)) as client:
            state = await asyncio.wait_for(flat_account(client), timeout=15)
            output_json(
                {
                    "preview": True,
                    **state,
                    "symbols": sorted(policy.symbols),
                    "max_order_notional": str(policy.max_order_notional),
                    "max_session_notional": str(policy.max_session_notional),
                    "requires_dedicated_flat_account": True,
                    "account_wide_cancellation": True,
                }
            )
        return
    if not args.ack_account_wide_cancel:
        raise ValueError("Live runner requires --ack-account-wide-cancel on a dedicated account")
    # asyncio cannot register regular files or /dev/null with its pipe selector.
    # Reject before constructing a signer or arming the account timer.
    mode = os.fstat(sys.stdin.fileno()).st_mode
    if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or sys.stdin.isatty()):
        raise ValueError(
            "Runner stdin must be a pipe or terminal; pipe a command file with cat instead of redirecting it"
        )
    async with RunnerClient(get_trading_config(require_private_key=True)) as client:
        client.configure(policy, live=live_state(client, policy))
        reader = asyncio.StreamReader(limit=8192)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            result = await serve(reader, client, policy, ack_previous=args.ack_previous_session)
            if result.get("error"):
                raise SystemExit(1)
        finally:
            transport.close()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)


def build_parser():
    parser = JsonArgumentParser(prog="runner", description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="Start the live session; otherwise preview reads only")
    parser.add_argument("--ack-account-wide-cancel", action="store_true")
    parser.add_argument(
        "--ack-previous-session", help="Exact session ID, only after manually reconciling a failed/unfinished run"
    )
    return parser


def main():
    try:
        asyncio.run(run(build_parser().parse_args()))
    except Exception as exc:
        output_result(exception_result(exc))


if __name__ == "__main__":
    main()
