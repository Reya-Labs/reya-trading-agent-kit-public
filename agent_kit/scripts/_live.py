"""Warm venue state for one runner session, kept from one public V2 WebSocket.

The runner reads marks and its dedicated account's wallet state from here instead of
REST before each order. Every input has a bound: outside it the getter returns None
and the caller reads REST, exactly as without this module, except while an own fill
is pending (below). The socket carries public
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
  lastTradeSequenceNumber >= S proves it. The venue indexes a fill seconds after it
  settles (12-13 s on devnet1), and REST trails the feed, so while a fill cannot be
  applied the wallet inputs refuse (OwnFillPending) instead of sending the caller to
  REST: until S is known, any position change since the send makes the state
  ambiguous, and so does a resubscribe, whose snapshot may already include the fill
  (the wallet stays unverified until such fills resolve; see _compare). A fill the
  feed has not shown within the environment's indexing_ceiling (busted, or a feed
  that missed it) is dropped, the wallet resubscribes, and REST decides.
- While the wallet is unverified, REST can prove an own fill instead (_prove_from_rest):
  the order's REST executions give S, and a REST position with lastTradeSequenceNumber
  >= S includes it. REST then decides before the ceiling. The feed leads REST by about
  0.5-0.8 s, so a fill the feed showed within REST_TRAIL_S needs the same REST proof
  before REST decides. A fill that leaves no position has no REST row to prove it: it
  waits for the feed, REST_TRAIL_S or the ceiling. Reads only, bounded by those.
- Each own fill's indexing lag goes to on_fill_lag when it resolves: seconds from the
  acknowledgement, and this computer's clock minus the trade's block timestamp.
- Position evidence for TP/SL (published_position) is the position exactly as the feed
  published it, without own fills: like REST, it is built from settled trades, so a
  busted fill never appears in it.
- Trigger evidence for the TP/SL read-back (published_open_orders) is the open orders
  exactly as the feed published them, without the registry: an acknowledged order the
  feed has not shown is no evidence that the venue lists it.
- The registry keeps each acknowledged resting order until the feed shows it. An order
  that ends while the socket is down never appears: the resubscribe snapshot holds open
  orders only. So once REST, read after a snapshot, matches the feed, the orders
  acknowledged before that snapshot are dropped; later ones stay until the feed shows them.
"""

from typing import Any, Callable, NamedTuple

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
# How long the venue may take to index a fill; an own fill the feed has not shown by then is dropped and the
# wallet resubscribed. Mainnet: block to stream about 1 s (p99.9 2.1 s, worst minute 4.2 s, six days to 19 Oct
# 2026). Elsewhere, e.g. devnet1 with 12-15 s episodes on 19 Oct.
INDEXING_CEILING_S = {"https://api.reya.xyz/v2": 15.0}
DEFAULT_INDEXING_CEILING_S = 30.0
BALANCE_SETTLE_S = 10.0  # how long an own fill's fee and realized-loss debit is kept once the feed shows the fill
# The feed leads REST by about 0.5-0.8 s: for this long after the feed shows an own fill, REST decides only once it
# proves the fill. REST proof reads back off from REST_PROOF_RETRY_S to REST_PROOF_MAX_S, each read bounded.
REST_TRAIL_S = 5.0
REST_PROOF_RETRY_S, REST_PROOF_MAX_S, REST_READ_S = 0.5, 4.0, 2.0
REST_EXECUTIONS_MARGIN_S = 60.0  # executions read from this long before the acknowledgement (clock offset)
CONNECT_TIMEOUT_S = 5.0
RECONNECT_BASE_S, RECONNECT_MAX_S = 0.5, 8.0
MAX_BUFFERED = 128
WALLET_FEEDS = ("positions", "accountBalances", "orderChanges", "perpExecutions")
OPEN_STATUSES = ("OPEN", "UNKNOWN")  # UNKNOWN may still rest: it stays cancellable
ORACLE_CHANNEL = "/v2/assetOraclePrices"


def indexing_ceiling(api_url: str) -> float:
    """Seconds the venue may take to index a fill in this deployment (INDEXING_CEILING_S)."""
    return INDEXING_CEILING_S.get(api_url, DEFAULT_INDEXING_CEILING_S)


def _connector(url: str):
    @asynccontextmanager
    async def connect():
        timeout = aiohttp.ClientTimeout(total=None, connect=CONNECT_TIMEOUT_S, sock_connect=CONNECT_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            async with session.ws_connect(url, heartbeat=10, max_msg_size=1024 * 1024) as ws:
                yield ws

    return connect


class Registered(NamedTuple):
    """An acknowledged resting order the feed has not shown, and the orderChanges snapshots received before its ack."""

    order: Order
    snapshots: int


class Published(NamedTuple):
    """This account's open position in one market as the feed published it, and its last trade sequence."""

    position: Position | None  # None: flat
    sequence: int | None  # kept after a close; None: the feed has never shown one this connection


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
    left_at: float | None = None  # when the feed or REST showed it (or it was dropped)
    acked_wall: float | None = None  # this computer's clock at the acknowledgement
    trade_ms: int | None = None  # the block timestamp of the execution at `sequence`


class OwnFillPending(ValueError):
    """An acknowledged own fill the feed has not shown and the wallet inputs cannot apply.

    REST trails the feed, so reading it instead would leave the fill out of the guards.
    """

    def __init__(self, fill: OwnFill, age: float):
        side = "buy" if fill.signed_qty > 0 else "sell"
        super().__init__(
            f"Order {fill.order_id} filled ({side} {format(abs(fill.signed_qty), 'f')} {fill.symbol}) {age:.1f} s ago "
            "and the venue does not show that fill yet, so the account checks would leave it out; no order "
            "submitted. Retry once it shows (usually within seconds); a reduce-only market close is still sent"
        )
        self.fill = fill


@dataclass
class _Debit:
    fill: OwnFill
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
        wall_clock: Callable[[], float] = time.time,
    ):
        """`rest` provides get_positions(), get_account_balances() and get_open_orders() for the checks, and
        wallet.get_wallet_perp_executions() for REST proof of own fills."""
        self.api_url, self.wallet, self.account_id = api_url, wallet.lower(), account_id
        self.symbols = frozenset(symbols)
        self.rest, self.clock, self.wall_clock = rest, clock, wall_clock
        self.own_fill_max_s = indexing_ceiling(api_url)
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
        self.registry: dict[str, Registered] = {}  # acknowledged resting orders the feed has not shown yet
        self.order_snapshots = 0  # orderChanges snapshots received this session, one per connection
        self.on_fill_lag: Callable[[dict], None] | None = None  # receives each own fill's lag record
        self._trailing: list[OwnFill] = []  # shown by the feed within REST_TRAIL_S, not yet proven in REST
        self._proving: asyncio.Task | None = None
        self._position_updates: Counter = Counter()
        self._fills_by_order: dict[str, dict[int, Decimal]] = {}
        self._trade_ms: dict[int, int] = {}  # block timestamps of this account's executions, by sequence
        self._buffered: dict[str, list] = {}
        self._ws = None
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._ready = asyncio.Event()
        self._changed = asyncio.Event()  # replaced on every signal; see wallet_changed

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

    def _spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

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
        self._signal()

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
            self._signal()
        elif feed.kind == "perpExecutions":
            for row in rows:
                self._index_execution(row)
            self._resolve_fills()
        elif feed.kind == "orderChanges":
            if snapshot:
                self.order_snapshots += 1
            for row in rows:
                if isinstance(row, dict):
                    self.registry.pop(str(row.get("orderId")), None)
            self._signal()

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
                self._forget_unlisted()
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
                self._signal()
                await self._verify(generation)

    def _forget_unlisted(self) -> None:
        """Drop registered orders acknowledged before this connection's orderChanges snapshot.

        Called when REST matched the feed. _verify starts once the snapshot is in, so REST was
        read after it and after these acknowledgements. The match means REST lists exactly the
        open orders the feed shows, and the feed has shown none in the registry, so REST lists
        none of these either: they ended, for example while the socket was down. If REST is
        merely late, the feed shows the order when the venue publishes it. Orders acknowledged
        after the snapshot stay until the feed shows them.
        """
        for order_id in [i for i, entry in self.registry.items() if entry.snapshots < self.order_snapshots]:
            del self.registry[order_id]

    def invalidate(self, reason: str) -> None:
        """Stop serving wallet state (callers read REST) and resubscribe for a fresh snapshot."""
        self.last_error = reason
        self.verified = False
        self._ready.clear()
        self._signal()
        self._spawn(self._resubscribe())

    async def _resubscribe(self) -> None:
        self.verified = False
        self._ready.clear()
        self._signal()
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

    def _signal(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()
        self._kick_rest_proof()  # every loss of verified state signals

    async def wallet_changed(self, timeout: float) -> None:
        """Return at the next position or order update, own fill shown or loss of verified state, or at `timeout` s."""
        changed = self._changed  # taken before any await: a change after the caller's last look still wakes it
        try:
            async with asyncio.timeout(timeout):
                await changed.wait()
        except TimeoutError:
            pass

    def published_position(self, symbol: str) -> Published | None:
        """The position as the feed published it (own unincluded fills NOT applied), or None: read REST."""
        if not self.wallet_ready():
            return None
        feed = self._wallet_feed("positions")
        keys = [key for key in feed.versions if key[0] == self.account_id and key[2] == symbol]
        if not keys:
            return Published(None, None)
        latest = max(keys, key=lambda key: feed.versions[key])
        row = feed.records.get(latest)  # a closed position keeps its version but not its row
        position = None if row is None or Decimal(row["qty"]) == 0 else Position.from_dict(row)
        return Published(position, feed.versions[latest][0])

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

    def published_open_orders(self) -> list[Order] | None:
        """Open orders exactly as the feed published them (the registry NOT included), or None: read REST."""
        if not self.wallet_ready():
            return None
        try:
            return [
                Order.from_dict(r)
                for r in self._wallet_feed("orderChanges").records.values()
                if r.get("accountId") == self.account_id and r.get("status") in OPEN_STATUSES
            ]
        except Exception:
            return None

    def open_orders(self) -> list[Order] | None:
        """Open orders from the feed plus acknowledged resting orders it has not shown yet; None: read REST."""
        live = self.published_open_orders()
        if live is None:
            return None
        known = {str(r.get("orderId")) for r in self._wallet_feed("orderChanges").records.values()}
        return live + [entry.order for order_id, entry in self.registry.items() if order_id not in known]

    def wallet_inputs(self) -> tuple[list, list] | None:
        """(balances, positions) for this account with own unincluded fills applied, or None: read REST.

        Raises OwnFillPending while an own fill the feed has not shown cannot be applied:
        REST would leave it out as well.
        """
        now = self.clock()
        self._drop_expired_fills(now)
        if not self.wallet_ready():
            unproven = self._rest_unproven(now)
            if unproven:
                self._kick_rest_proof()
                raise OwnFillPending(unproven[0], now - unproven[0].acked_at)
            return None
        rows = {
            r["symbol"]: dict(r)
            for r in self._wallet_feed("positions").records.values()
            if r.get("accountId") == self.account_id
        }
        for fill in self.own_fills:
            summary = self.mark_summary(fill.symbol)
            # A position that changed since the send, before the fill's sequence is known, may or may not include it.
            ambiguous = fill.sequence is None and self._position_updates[fill.symbol] != fill.updates_at_send
            if not fill.debited or ambiguous or summary is None:
                raise OwnFillPending(fill, now - fill.acked_at)
            rows[fill.symbol] = _with_fill(rows.get(fill.symbol), fill, summary, self.account_id)
        positions = [Position.from_dict(r) for r in rows.values() if Decimal(r["qty"]) != 0]
        balances = {
            r["asset"]: dict(r)
            for r in self._wallet_feed("accountBalances").records.values()
            if r.get("accountId") == self.account_id
        }
        self.debits = [d for d in self.debits if d.fill.left_at is None or now - d.fill.left_at <= BALANCE_SETTLE_S]
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
            self.registry[order_id] = Registered(resting, self.order_snapshots)
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
            acked_wall=self.wall_clock(),
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
            self.debits.append(_Debit(fill, _fill_debit(row, fill, fee_rate)))
        self.own_fills.append(fill)
        self._resolve_fills()
        self._kick_rest_proof()

    def on_cancelled(self, order_id: str) -> None:
        self.registry.pop(str(order_id), None)

    def _index_execution(self, row) -> None:
        """Record this account's part in one execution, from the feed or a REST proof read."""
        if not isinstance(row, dict):
            return
        sequence = row.get("sequenceNumber")
        if type(sequence) is not int or sequence <= 0:
            return  # legacy zero sequences identify nothing
        for role in ("taker", "maker"):
            if row.get(f"{role}AccountId") == self.account_id and row.get(f"{role}OrderId") is not None:
                self._fills_by_order.setdefault(str(row[f"{role}OrderId"]), {})[sequence] = Decimal(row["qty"])
                if type(row.get("timestamp")) is int:
                    self._trade_ms[sequence] = row["timestamp"]

    def executions_qty(self, order_id) -> Decimal:
        """This account's quantity in the order's executions that the feed (or a REST proof read) has shown."""
        return sum(self._fills_by_order.get(str(order_id), {}).values(), Decimal(0))

    def _position_sequence(self, symbol: str) -> int | None:
        """The last trade sequence the feed applied to this account's position, kept after a close."""
        feed = self._wallet_feed("positions")
        sequences = [rank[0] for key, rank in feed.versions.items() if key[0] == self.account_id and key[2] == symbol]
        return max(sequences) if sequences else None

    def _sequence(self, fill: OwnFill) -> None:
        """Set the fill's sequence once the executions shown cover every fill of its order acknowledged so far."""
        if fill.sequence is None:
            seen = self._fills_by_order.get(fill.order_id, {})
            if seen and sum(seen.values(), Decimal(0)) >= fill.order_cum_qty:
                fill.sequence = max(seen)
                fill.trade_ms = self._trade_ms.get(fill.sequence)

    def _resolve_fills(self) -> None:
        resolved = False
        for fill in list(self.own_fills):
            self._sequence(fill)
            if fill.sequence is None:
                continue
            applied = self._position_sequence(fill.symbol)
            if applied is not None and applied >= fill.sequence:
                now = self.clock()
                self._leave(fill, "feed", now)
                self._rest_unproven(now)  # drops trailing fills REST has had time to show
                self._trailing.append(fill)  # the feed leads REST: REST may not show it yet
                resolved = True
        if resolved:
            self._signal()  # an execution can be the last evidence: wake waiters as a position update does

    def _leave(self, fill: OwnFill, resolved_by: str, now: float) -> None:
        """Take an own fill out of the overlay and report its lag to on_fill_lag."""
        self.own_fills.remove(fill)
        fill.left_at = now
        lag = None
        if resolved_by != "ceiling" and fill.trade_ms is not None:
            # This computer's clock when it saw the fill, minus the trade's block timestamp (whole seconds).
            lag = round(self.wall_clock() - fill.trade_ms / 1000, 3)
        record = {
            "order_id": fill.order_id,
            "symbol": fill.symbol,
            "sequence": fill.sequence,
            "resolved_by": resolved_by,  # feed, rest, or ceiling (dropped unshown)
            "ack_to_resolved_s": round(now - fill.acked_at, 3),
            "trade_timestamp_ms": fill.trade_ms,
            "venue_lag_s": lag,
            "ceiling_s": self.own_fill_max_s,
        }
        if self.on_fill_lag is not None:
            try:
                self.on_fill_lag(record)
            except Exception:
                pass  # evidence only: never let it disturb the state

    def _drop_expired_fills(self, now: float) -> None:
        """Drop own fills the feed has not shown within the indexing ceiling and resubscribe: REST decides then."""
        expired = [fill for fill in self.own_fills if now - fill.acked_at > self.own_fill_max_s]
        for fill in expired:
            self._leave(fill, "ceiling", now)
        if expired:
            self.invalidate(
                f"Own fill of order {expired[0].order_id} not shown by the feed within {self.own_fill_max_s:g} s; "
                "resubscribing"
            )

    def _rest_unproven(self, now: float) -> list[OwnFill]:
        """Own fills REST must prove before it decides: unshown ones, and ones the feed showed within REST_TRAIL_S."""
        self._trailing = [f for f in self._trailing if f.left_at is not None and now - f.left_at <= REST_TRAIL_S]
        return self.own_fills + self._trailing

    def _kick_rest_proof(self) -> None:
        """Prove own fills from REST in the background while the wallet is unverified, unless already doing so."""
        if self._closed or self.rest is None or self.wallet_ready() or not (self.own_fills or self._trailing):
            return
        if self._proving is not None and not self._proving.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop to read in: the feed or the ceiling decides
        self._proving = self._spawn(self._prove_from_rest())

    async def _prove_from_rest(self) -> None:
        """Read REST until it proves every own fill it must, so that REST decides before the ceiling.

        Reads only, and never sends anything. Ends once the wallet is verified again or nothing needs
        proof: an unshown fill leaves at the latest at the ceiling, a shown one REST_TRAIL_S after the
        feed showed it. Reads back off from REST_PROOF_RETRY_S to REST_PROOF_MAX_S.
        """
        delay = REST_PROOF_RETRY_S
        while not self._closed:
            now = self.clock()
            self._drop_expired_fills(now)
            if self.wallet_ready() or not self._rest_unproven(now):
                return
            try:
                await self._rest_proof()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # proves nothing: the next read, the feed or the ceiling decides
            if self.wallet_ready() or not self._rest_unproven(self.clock()):
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, REST_PROOF_MAX_S)

    async def _rest_proof(self) -> None:
        """One REST observation: the order's executions give its sequence S, and a position at or past S includes it.

        The executions are read only for fills whose S the feed has not given. Only fills unproven when the
        reads start are proven, and nothing changes if the feed is verified again meanwhile: it decides then.
        """
        candidates = self._rest_unproven(self.clock())
        unsequenced = [f for f in candidates if f.sequence is None]

        async def executions():
            if not unsequenced:
                return []
            acked = min(f.acked_wall if f.acked_wall is not None else self.wall_clock() for f in unsequenced)
            since_ms = max(0, int((acked - REST_EXECUTIONS_MARGIN_S) * 1000))
            page = await self.rest.wallet.get_wallet_perp_executions(address=self.wallet, start_time=since_ms)
            return page.data

        positions, rows = await asyncio.wait_for(
            asyncio.gather(self.rest.get_positions(), executions()), timeout=REST_READ_S
        )
        if self.wallet_ready():
            return
        for execution in rows:
            self._index_execution(execution.to_dict())
        applied: dict[str, int] = {}
        for position in positions:
            if position.account_id == self.account_id:
                previous = applied.get(position.symbol, -1)
                applied[position.symbol] = max(previous, position.last_trade_sequence_number)
        now, proven = self.clock(), False
        for fill in [f for f in self.own_fills if any(f is c for c in candidates)]:
            self._sequence(fill)
            if fill.sequence is not None and applied.get(fill.symbol, -1) >= fill.sequence:
                self._leave(fill, "rest", now)
                proven = True
        trailing = len(self._trailing)
        self._trailing = [f for f in self._trailing if f.sequence is None or applied.get(f.symbol, -1) < f.sequence]
        if proven or len(self._trailing) != trailing:
            self._signal()

    async def own_fills_shown(self, timeout: float) -> bool:
        """Wait up to `timeout` s for the feed to show every own fill; False at the timeout or if the socket drops."""
        deadline = time.monotonic() + timeout
        while self.own_fills:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.connected:
                return False
            await self.wallet_changed(remaining)
        return True


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
