"""Warm venue state for one runner session, kept from one public V2 WebSocket.

The runner reads marks and its dedicated account's wallet state from here instead of
REST before each order. Every input has a bound: outside it the getter returns None
and the caller reads REST, exactly as without this module. The socket carries public
market and wallet data (ws.reya.xyz); it never carries orders.

- A mark is used only if its channel pushed within MARK_MAX_AGE_S. The server pushes
  every 500 ms; the caller still applies its own price staleness checks.
- Wallet state (balances, positions, open orders, fills) is used only while the socket
  is up, every wallet channel has its subscribe snapshot, no error or resync arrived
  since, and REST reads after the snapshot matched it. The server delivers every
  wallet update or closes with 1012 (reconnect and re-snapshot); an audit against REST
  every AUDIT_INTERVAL_S is the backstop.
- The runner's own acknowledged fills count in the position until the feed shows them
  included: the order's executions give sequence S, and the position's
  lastTradeSequenceNumber >= S proves it. Until S is known, any position change since
  the send makes the state ambiguous and the caller reads REST instead.
"""

from typing import Any, Callable

import asyncio
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, localcontext

import aiohttp
from sdk.open_api.models.account_balance import AccountBalance
from sdk.open_api.models.market_summary import MarketSummary
from sdk.open_api.models.order import Order
from sdk.open_api.models.position import Position

from agent_kit.scripts._http import USER_AGENT
from agent_kit.scripts._stream import FeedState
from agent_kit.scripts._v2 import WS_URLS, parse_json

MARK_MAX_AGE_S = 1.0  # two missed 500 ms pushes
ORACLE_MAX_AGE_S = 2.0
AUDIT_INTERVAL_S = 60.0
VERIFY_MISMATCHES, VERIFY_RETRY_S = 3, 1.0
OWN_FILL_RESOLVE_S = 5.0  # an own fill the feed has not shown by then makes the wallet suspect
BALANCE_SETTLE_S = 10.0  # how long an own fill's fee and realized-loss debit is kept
CONNECT_TIMEOUT_S = 5.0
RECONNECT_BASE_S, RECONNECT_MAX_S = 0.5, 8.0
MAX_BUFFERED = 128
WALLET_FEEDS = ("positions", "accountBalances", "orderChanges", "perpExecutions")
OPEN_STATUSES = ("OPEN", "UNKNOWN")  # UNKNOWN may still rest: it stays cancellable
ORACLE_CHANNEL = "/v2/assetOraclePrices"


def _connector(url: str):
    @asynccontextmanager
    async def connect():
        timeout = aiohttp.ClientTimeout(total=None, connect=CONNECT_TIMEOUT_S, sock_connect=CONNECT_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            async with session.ws_connect(url, heartbeat=10, max_msg_size=1024 * 1024) as ws:
                yield ws

    return connect


@dataclass
class SendToken:
    symbol: str
    position_updates: int


@dataclass
class OwnFill:
    """Quantity one of the runner's own requests filled, until the position feed includes it."""

    order_id: str
    symbol: str
    signed_qty: Decimal
    price: Decimal
    order_cum_qty: Decimal  # every fill of the order so far, as acknowledged
    updates_at_send: int
    acked_at: float
    debited: bool
    sequence: int | None = None


@dataclass
class _Debit:
    at: float
    amount: Decimal  # negative: the fee bound plus any realized loss


class LiveState:
    def __init__(
        self,
        api_url: str,
        wallet: str,
        account_id: int,
        symbols,
        *,
        rest: Any = None,
        connect: Callable | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """`rest` provides get_positions(), get_account_balances() and get_open_orders() for the checks."""
        self.api_url, self.wallet, self.account_id = api_url, wallet.lower(), account_id
        self.symbols = frozenset(symbols)
        self.rest, self.clock = rest, clock
        if connect is None:
            if api_url not in WS_URLS:
                raise ValueError("No verified WebSocket deployment for this API URL")
            connect = _connector(WS_URLS[api_url])
        self.connect = connect
        self.feeds: dict[str, FeedState] = {}
        for symbol in sorted(self.symbols):
            channel = f"/v2/perpMarket/{symbol}/summary"
            self.feeds[channel] = FeedState(api_url, channel)
        for kind in WALLET_FEEDS:
            channel = f"/v2/wallet/{self.wallet}/{kind}"
            self.feeds[channel] = FeedState(api_url, channel)
        self.feeds[ORACLE_CHANNEL] = FeedState(api_url, ORACLE_CHANNEL)
        self.connected = self.verified = False
        self.generation = 0
        self.last_error: str | None = None
        self.own_fills: list[OwnFill] = []
        self.debits: list[_Debit] = []
        self.registry: dict[str, Order] = {}  # acknowledged resting orders the feed has not shown yet
        self._position_updates: Counter = Counter()
        self._fills_by_order: dict[str, dict[int, Decimal]] = {}
        self._buffered: dict[str, list] = {}
        self._ws = None
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._ready = asyncio.Event()

    # ---------------------------------------------------------------- lifecycle

    async def start(self, *, timeout: float = 5.0) -> dict:
        """Connect in the background and wait up to `timeout` for verified state. Never raises."""
        self._spawn(self._run())
        self._spawn(self._audit())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except TimeoutError:
            pass
        return self.status()

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._drop()

    def status(self) -> dict:
        return {
            "source": "websocket",
            "connected": self.connected,
            "wallet": "verified" if self.wallet_ready() else "unavailable",
            "marks": sorted(s for s in self.symbols if self.mark_summary(s) is not None),
            **({"last_error": self.last_error} if self.last_error else {}),
        }

    def _spawn(self, coroutine) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self) -> None:
        attempt = 0
        while not self._closed:
            try:
                async with self.connect() as ws:
                    self._ws = ws
                    attempt = 0
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc) if isinstance(exc, (ValueError, ConnectionError)) else type(exc).__name__
            self._drop()
            if self._closed:
                return
            await asyncio.sleep(min(RECONNECT_BASE_S * 2**attempt, RECONNECT_MAX_S))
            attempt += 1

    def _drop(self) -> None:
        self._ws = None
        self.connected = self.verified = False
        self._ready.clear()

    async def _session(self, ws) -> None:
        self.generation += 1
        generation = self.generation
        self._buffered = {}
        for feed in self.feeds.values():
            feed.reset()
        # A new snapshot may already include an own fill: count it as a position change.
        for fill in self.own_fills:
            self._position_updates[fill.symbol] += 1
        for channel in self.feeds:
            await ws.send_json({"type": "subscribe", "channel": channel})
        self.connected = True
        verifying = False
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                await self._handle(ws, parse_json(message.data))
            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                break
            if not verifying and self._wallet_snapshotted():
                verifying = True
                self._spawn(self._verify(generation))
        raise ConnectionError("WebSocket closed; reconnecting")

    async def _handle(self, ws, data) -> None:
        if not isinstance(data, dict):
            raise ValueError("Invalid WebSocket message")
        kind = data.get("type")
        if kind == "ping":
            await ws.send_json({"type": "pong", "id": data.get("id")})
            return
        if kind == "error":
            raise ValueError("The server reported a subscription error; resubscribing")
        channel = data.get("channel")
        feed = self.feeds.get(channel) if isinstance(channel, str) else None
        if feed is None or not isinstance(channel, str):
            return
        now = self.clock()
        if kind == "subscribed":
            feed.snapshot(data.get("contents"), now=now)
            self._after_rows(feed, list(feed.records.values()), snapshot=True)
            for message in self._buffered.pop(channel, []):
                self._apply(feed, message, now)
        elif kind == "channel_data":
            if feed.phase not in ("ready", "stale"):
                buffered = self._buffered.setdefault(channel, [])
                buffered.append(data)
                if len(buffered) > MAX_BUFFERED:
                    raise ValueError("Snapshot overlap buffer exceeded; resubscribing")
                return
            self._apply(feed, data, now)

    def _apply(self, feed: FeedState, message: dict, now: float) -> None:
        feed.update(message, now=now)
        data = message.get("data")
        self._after_rows(feed, data if isinstance(data, list) else [data], snapshot=False)

    def _after_rows(self, feed: FeedState, rows: list, *, snapshot: bool) -> None:
        if feed.kind == "positions":
            if not snapshot:
                for row in rows:
                    if isinstance(row, dict) and row.get("accountId") == self.account_id:
                        self._position_updates[row.get("symbol")] += 1
            self._resolve_fills()
        elif feed.kind == "perpExecutions":
            for row in rows:
                self._index_execution(row)
            self._resolve_fills()
        elif feed.kind == "orderChanges":
            for row in rows:
                if isinstance(row, dict):
                    self.registry.pop(str(row.get("orderId")), None)

    def _wallet_feed(self, kind: str) -> FeedState:
        return self.feeds[f"/v2/wallet/{self.wallet}/{kind}"]

    def _wallet_snapshotted(self) -> bool:
        return all(self._wallet_feed(kind).phase in ("ready", "stale") for kind in WALLET_FEEDS)

    # ---------------------------------------------------------------- REST checks

    async def _compare(self) -> bool | None:
        """True/False when REST and the feed agree/disagree; None when the check proves nothing."""
        if self.own_fills:
            return None  # an own fill in flight: compare once it has settled
        try:
            positions, balances, orders = await asyncio.gather(
                self.rest.get_positions(), self.rest.get_account_balances(), self.rest.get_open_orders()
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"REST check failed: {type(exc).__name__}"
            return None
        if self.own_fills:
            return None
        return self._matches(positions, balances, orders)

    async def _verify(self, generation: int) -> None:
        mismatches, delay = 0, VERIFY_RETRY_S
        while generation == self.generation and not self._closed:
            verdict = await self._compare()
            if generation != self.generation:
                return
            if verdict:
                self.verified = True
                self._ready.set()
                return
            if verdict is False:
                mismatches += 1
                if mismatches >= VERIFY_MISMATCHES:
                    self.last_error = "WebSocket wallet state did not match REST; resubscribing"
                    await self._resubscribe()
                    return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8)

    async def _audit(self) -> None:
        while not self._closed:
            await asyncio.sleep(AUDIT_INTERVAL_S)
            generation = self.generation
            if self.verified and await self._compare() is False and generation == self.generation:
                self.verified = False
                self._ready.clear()
                await self._verify(generation)

    def invalidate(self, reason: str) -> None:
        """Stop serving wallet state (callers read REST) and resubscribe for a fresh snapshot."""
        self.last_error = reason
        self.verified = False
        self._ready.clear()
        self._spawn(self._resubscribe())

    async def _resubscribe(self) -> None:
        self.verified = False
        self._ready.clear()
        if self._ws is not None:
            await self._ws.close()

    def _matches(self, positions, balances, orders) -> bool:
        live_positions = {
            (r["symbol"], r["side"], Decimal(r["qty"]), r["lastTradeSequenceNumber"])
            for r in self._wallet_feed("positions").records.values()
            if r.get("accountId") == self.account_id and Decimal(r["qty"]) != 0
        }
        rest_positions = {
            (p.symbol, p.side.value, Decimal(p.qty), p.last_trade_sequence_number)
            for p in positions
            if p.account_id == self.account_id and Decimal(p.qty) != 0
        }
        live_orders = {
            str(r.get("orderId"))
            for r in self._wallet_feed("orderChanges").records.values()
            if r.get("accountId") == self.account_id and r.get("status") in OPEN_STATUSES
        }
        rest_orders = {str(o.order_id) for o in orders if o.account_id == self.account_id}
        live_balances = {
            r["asset"]: Decimal(r["realBalance"])
            for r in self._wallet_feed("accountBalances").records.values()
            if r.get("accountId") == self.account_id
        }
        rest_balances = {b.asset: Decimal(b.real_balance) for b in balances if b.account_id == self.account_id}
        for asset in set(live_balances) | set(rest_balances):
            live, rest = live_balances.get(asset, Decimal(0)), rest_balances.get(asset, Decimal(0))
            # Funding accrues between the two reads; a fee or a fill moves a balance far more.
            if abs(live - rest) > max(Decimal("0.01"), abs(rest) * Decimal("0.0001")):
                return False
        return live_positions == rest_positions and live_orders == rest_orders

    # ---------------------------------------------------------------- reads

    def wallet_ready(self) -> bool:
        return self.connected and self.verified and self._wallet_snapshotted()

    def _fresh(self, feed: FeedState | None, max_age: float) -> bool:
        return (
            feed is not None
            and self.connected
            and feed.phase in ("ready", "stale")
            and feed.received_at is not None
            and self.clock() - feed.received_at <= max_age
        )

    def mark_summary(self, symbol: str) -> MarketSummary | None:
        feed = self.feeds.get(f"/v2/perpMarket/{symbol}/summary")
        if not self._fresh(feed, MARK_MAX_AGE_S):
            return None
        row = feed.records.get((symbol,)) if feed is not None else None
        return None if row is None else MarketSummary.from_dict(row)

    def oracle_prices(self) -> dict | None:
        feed = self.feeds[ORACLE_CHANNEL]
        if not self._fresh(feed, ORACLE_MAX_AGE_S):
            return None
        return {r["asset"]: r["oraclePrice"] for r in feed.records.values()}

    def open_orders(self) -> list[Order] | None:
        """Open orders from the feed plus acknowledged resting orders it has not shown yet; None: read REST."""
        if not self.wallet_ready():
            return None
        rows = self._wallet_feed("orderChanges").records.values()
        try:
            live = [
                Order.from_dict(r)
                for r in rows
                if r.get("accountId") == self.account_id and r.get("status") in OPEN_STATUSES
            ]
        except Exception:
            return None
        known = {str(r.get("orderId")) for r in rows}
        return live + [order for order_id, order in self.registry.items() if order_id not in known]

    def wallet_inputs(self) -> tuple[list, list] | None:
        """(balances, positions) for this account with own unincluded fills applied, or None: read REST."""
        if not self.wallet_ready():
            return None
        now = self.clock()
        rows = {
            r["symbol"]: dict(r)
            for r in self._wallet_feed("positions").records.values()
            if r.get("accountId") == self.account_id
        }
        for fill in list(self.own_fills):
            if now - fill.acked_at > OWN_FILL_RESOLVE_S:
                self.own_fills.remove(fill)
                self.invalidate(f"Own fill of order {fill.order_id} not shown by the feed; resubscribing")
                return None
            if not fill.debited:
                return None  # no fee bound to debit: read REST until this fill settles
            if fill.sequence is None and self._position_updates[fill.symbol] != fill.updates_at_send:
                return None  # the position changed since the send: it may or may not include this fill
            summary = self.mark_summary(fill.symbol)
            if summary is None:
                return None
            rows[fill.symbol] = _with_fill(rows.get(fill.symbol), fill, summary, self.account_id)
        positions = [Position.from_dict(r) for r in rows.values() if Decimal(r["qty"]) != 0]
        balances = {
            r["asset"]: dict(r)
            for r in self._wallet_feed("accountBalances").records.values()
            if r.get("accountId") == self.account_id
        }
        self.debits = [d for d in self.debits if now - d.at <= BALANCE_SETTLE_S]
        if self.debits:
            usdc = balances.setdefault(
                "RUSD", {"accountId": self.account_id, "asset": "RUSD", "realBalance": "0", "balanceDEPRECATED": "0"}
            )
            with localcontext() as context:
                context.prec = 60
                total = Decimal(usdc["realBalance"]) + sum((d.amount for d in self.debits), Decimal(0))
            usdc["realBalance"] = format(total, "f")
        return [AccountBalance.from_dict(r) for r in balances.values()], positions

    # ---------------------------------------------------------------- own writes

    def before_send(self, symbol: str) -> SendToken:
        return SendToken(symbol, self._position_updates[symbol])

    def on_ack(
        self,
        token: SendToken,
        *,
        order_id: str,
        is_buy: bool,
        filled_qty: Decimal,
        price: Decimal,
        order_cum_qty: Decimal,
        fee_rate: Decimal | None,
        resting: Order | None = None,
    ) -> None:
        """Record an acknowledged request: its fill until the feed includes it, a resting order until shown."""
        order_id = str(order_id)
        seen = {str(r.get("orderId")) for r in self._wallet_feed("orderChanges").records.values()}
        if resting is not None and order_id not in seen:
            self.registry[order_id] = resting
        if filled_qty <= 0:
            return
        now = self.clock()
        fill = OwnFill(
            order_id=order_id,
            symbol=token.symbol,
            signed_qty=filled_qty if is_buy else -filled_qty,
            price=price,
            order_cum_qty=order_cum_qty,
            updates_at_send=token.position_updates,
            acked_at=now,
            debited=fee_rate is not None,
        )
        if fee_rate is not None:
            row = next(
                (
                    r
                    for r in self._wallet_feed("positions").records.values()
                    if r.get("accountId") == self.account_id and r.get("symbol") == token.symbol
                ),
                None,
            )
            self.debits.append(_Debit(now, _fill_debit(row, fill, fee_rate)))
        self.own_fills.append(fill)
        self._resolve_fills()

    def on_cancelled(self, order_id: str) -> None:
        self.registry.pop(str(order_id), None)

    def _index_execution(self, row) -> None:
        if not isinstance(row, dict):
            return
        sequence = row.get("sequenceNumber")
        if type(sequence) is not int or sequence <= 0:
            return  # legacy zero sequences identify nothing
        for role in ("taker", "maker"):
            if row.get(f"{role}AccountId") == self.account_id and row.get(f"{role}OrderId") is not None:
                self._fills_by_order.setdefault(str(row[f"{role}OrderId"]), {})[sequence] = Decimal(row["qty"])

    def _position_sequence(self, symbol: str) -> int | None:
        """The last trade sequence the feed applied to this account's position, kept after a close."""
        feed = self._wallet_feed("positions")
        sequences = [rank[0] for key, rank in feed.versions.items() if key[0] == self.account_id and key[2] == symbol]
        return max(sequences) if sequences else None

    def _resolve_fills(self) -> None:
        for fill in list(self.own_fills):
            if fill.sequence is None:
                seen = self._fills_by_order.get(fill.order_id, {})
                if seen and sum(seen.values(), Decimal(0)) >= fill.order_cum_qty:
                    fill.sequence = max(seen)
            if fill.sequence is None:
                continue
            applied = self._position_sequence(fill.symbol)
            if applied is not None and applied >= fill.sequence:
                self.own_fills.remove(fill)


def _signed(row: dict | None) -> Decimal:
    if row is None:
        return Decimal(0)
    qty = Decimal(row["qty"])
    return qty if row["side"] == "B" else -qty


def _with_fill(row: dict | None, fill: OwnFill, summary: MarketSummary, account_id: int) -> dict:
    """The position row as it will be once this fill is included (weighted entry for an increase)."""
    with localcontext() as context:
        context.prec = 60
        q0, dq = _signed(row), fill.signed_qty
        q1 = q0 + dq
        market_funding = Decimal(summary.long_funding_value if q1 > 0 else summary.short_funding_value)
        if row is None or q0 == 0 or (q1 != 0 and (q0 > 0) != (q1 > 0)):
            # New, or flipped: what remains is opened at the fill price.
            entry, funding = fill.price, market_funding
        elif (q0 > 0) == (dq > 0):
            weight = abs(q0) + abs(dq)
            entry = (abs(q0) * Decimal(row["avgEntryPrice"]) + abs(dq) * fill.price) / weight
            funding = (abs(q0) * Decimal(row["avgEntryFundingValue"]) + abs(dq) * market_funding) / weight
        else:
            entry, funding = Decimal(row["avgEntryPrice"]), Decimal(row["avgEntryFundingValue"])
        return {
            "exchangeId": (row or {}).get("exchangeId", 2),
            "symbol": fill.symbol,
            "accountId": account_id,
            "qty": format(abs(q1), "f"),
            "side": "A" if q1 < 0 else "B",
            "avgEntryPrice": format(entry, "f"),
            "avgEntryFundingValue": format(funding, "f"),
            "lastTradeSequenceNumber": (row or {}).get("lastTradeSequenceNumber", 0),
        }


def _fill_debit(row: dict | None, fill: OwnFill, fee_rate: Decimal) -> Decimal:
    """A conservative balance change for one fill: the fee bound plus any realized loss, never a gain."""
    with localcontext() as context:
        context.prec = 60
        fee = abs(fill.signed_qty) * fill.price * fee_rate
        q0, realized = _signed(row), Decimal(0)
        if row is not None and q0 != 0 and (q0 > 0) != (fill.signed_qty > 0):
            closed = min(abs(q0), abs(fill.signed_qty))
            realized = closed * (fill.price - Decimal(row["avgEntryPrice"])) * (1 if q0 > 0 else -1)
        return min(realized, Decimal(0)) - fee
