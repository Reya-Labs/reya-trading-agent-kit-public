"""The runner's fast path: warm WebSocket state, deferred fee reads and definitive rejections.

Offline: the WebSocket is a fake socket that answers subscriptions with snapshots, and every
REST read is the existing runner fixture's stub.
"""

from types import SimpleNamespace

import asyncio
import json
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock

import aiohttp
import pytest
from sdk.open_api.exceptions import ApiException, BadRequestException, ServiceException
from sdk.open_api.models.cancel_order_response import CancelOrderResponse
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.side import Side

from agent_kit.scripts import _live, _margin, runner, trade
from agent_kit.scripts._live import LiveState
from agent_kit.tests.conftest import make_balance, make_market_summary, make_order, make_position, make_price
from agent_kit.tests.test_runner import LIMIT, client, outputs, policy, stream  # noqa: F401
from agent_kit.tests.test_settlement import fill

SYMBOL = "ETHRUSDPERP"
API = "https://api.reya.xyz/v2"
MARKET = ["order", "market", SYMBOL, "--side", "long", "--qty", "0.01", "--slippage", "0.005"]


def wide(client):  # noqa: F811
    """A session budget for two ~20 USDC orders (the shared policy allows 40 USDC in total)."""
    client.policy = runner.Policy(frozenset({SYMBOL}), Decimal("25"), Decimal("100"), Decimal("0.005"), 10, 10)
    return client.policy


def executions_page(*rows):
    from sdk.open_api.models.pagination_meta import PaginationMeta
    from sdk.open_api.models.perp_execution import PerpExecution
    from sdk.open_api.models.perp_execution_list import PerpExecutionList

    return PerpExecutionList(
        data=[PerpExecution.from_dict(row) for row in rows], meta=PaginationMeta(limit=100, count=len(rows))
    )


def now_ms() -> int:
    return int(time.time() * 1000)


class FakeSocket:
    def __init__(self, snapshots: dict):
        self.snapshots = snapshots
        self.sent: list = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send_json(self, data):
        self.sent.append(data)
        if data.get("type") == "subscribe" and data["channel"] in self.snapshots:
            self.push({"type": "subscribed", "channel": data["channel"], "contents": self.snapshots[data["channel"]]})

    def push(self, data):
        self.queue.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(data)))

    def update(self, channel, data, timestamp=None):
        self.push({"type": "channel_data", "channel": channel, "data": data, "timestamp": timestamp or now_ms()})

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.queue.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def close(self):
        self.closed = True
        self.queue.put_nowait(None)


class Venue:
    """Hands out a new socket per connection, answering with the current snapshots."""

    def __init__(self, wallet: str, *, balance: str = "1000"):
        self.wallet = wallet.lower()
        self.sockets: list[FakeSocket] = []
        self.refuse = False
        self.snapshots = {
            f"/v2/perpMarket/{SYMBOL}/summary": summary_row(),
            self.channel("positions"): [],
            self.channel("accountBalances"): [make_balance(real_balance=balance).to_dict()],
            self.channel("orderChanges"): {"data": [], "snapshotSequenceNumber": 10},
            self.channel("perpExecutions"): {"data": []},
            "/v2/assetOraclePrices": {"data": []},
        }

    def channel(self, kind: str) -> str:
        return f"/v2/wallet/{self.wallet}/{kind}"

    @property
    def socket(self) -> FakeSocket:
        return self.sockets[-1]

    @asynccontextmanager
    async def connect(self):
        if self.refuse:
            raise ConnectionError("refused")
        socket = FakeSocket(self.snapshots)
        self.sockets.append(socket)
        yield socket


def summary_row(mark="2000"):
    return make_market_summary(mark_price=mark, oracle_price=mark, prices_updated_at=now_ms()).to_dict()


async def settle(condition, *, within=2.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("condition never held")


@pytest.fixture(autouse=True)
def _quick_reconnect(monkeypatch):
    monkeypatch.setattr(_live, "RECONNECT_BASE_S", 0)


def inputs(live: LiveState) -> tuple[list, list]:
    result = live.wallet_inputs()
    assert result is not None
    return result


def mark(live: LiveState) -> str | None:
    summary = live.mark_summary(SYMBOL)
    assert summary is not None
    return summary.mark_price


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def rest_stub(*, positions=(), balances=None, orders=()):
    return SimpleNamespace(
        get_positions=AsyncMock(return_value=list(positions)),
        get_account_balances=AsyncMock(return_value=balances or [make_balance(real_balance="1000")]),
        get_open_orders=AsyncMock(return_value=list(orders)),
    )


def warm(client, venue, *, clock=None) -> LiveState:  # noqa: F811
    live = LiveState(
        API,
        client.config.owner_wallet_address,
        8009,
        {SYMBOL},
        rest=client,
        connect=venue.connect,
        clock=clock or time.monotonic,
    )
    client.live = live
    return live


def unshadow_marks(client, monkeypatch, mark="2000"):  # noqa: F811
    """The shared fixture stubs the summary on the runner's market proxy; use the proxy and stub REST under it."""
    del client.markets.get_perp_market_summary
    rest = AsyncMock(return_value=make_price(mark_price=mark, oracle_price=mark, updated_at=now_ms()))
    monkeypatch.setattr(client.markets._api, "get_perp_market_summary", rest)
    return rest


# ------------------------------------------------------------------ the live state itself


async def test_marks_are_served_only_while_their_channel_keeps_pushing():
    clock, venue = Clock(), Venue("0x" + "ab" * 20)
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest_stub(), connect=venue.connect, clock=clock)
    status = await live.start(timeout=1)
    assert status["wallet"] == "verified" and status["marks"] == [SYMBOL]
    assert mark(live) == "2000"
    clock.now += _live.MARK_MAX_AGE_S + 0.1
    assert live.mark_summary(SYMBOL) is None  # two missed pushes: the caller reads REST
    venue.socket.update(f"/v2/perpMarket/{SYMBOL}/summary", summary_row("2010"))
    await settle(lambda: live.mark_summary(SYMBOL) is not None)
    assert mark(live) == "2010"
    await live.close()


async def test_wallet_state_is_used_only_after_rest_agrees_and_a_mismatch_resubscribes(monkeypatch):
    monkeypatch.setattr(_live, "VERIFY_RETRY_S", 0)
    venue = Venue("0x" + "ab" * 20)
    rest = rest_stub(balances=[make_balance(real_balance="5")])  # REST disagrees with the snapshot
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest, connect=venue.connect)
    status = await live.start(timeout=0.2)
    assert status["wallet"] == "unavailable" and live.wallet_inputs() is None and live.open_orders() is None
    await settle(lambda: len(venue.sockets) >= 2)  # three mismatches: resubscribe for a fresh snapshot
    assert venue.sockets[0].closed
    rest.get_account_balances.return_value = [make_balance(real_balance="1000")]
    await settle(live.wallet_ready)
    balances, positions = inputs(live)
    assert [b.real_balance for b in balances] == ["1000"] and positions == []
    await live.close()


async def test_own_fill_counts_until_the_feed_includes_it_and_ambiguity_refuses():
    venue = Venue("0x" + "ab" * 20)
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest_stub(), connect=venue.connect)
    await live.start(timeout=1)
    fee = Decimal("0.0004")

    token = live.before_send(SYMBOL)
    live.on_ack(
        token,
        order_id="10",
        is_buy=True,
        filled_qty=Decimal("0.01"),
        price=Decimal("2000"),
        order_cum_qty=Decimal("0.01"),
        fee_rate=fee,
    )
    balances, positions = inputs(live)
    # Not shown by the feed yet: the acknowledged fill counts, and the fee bound is debited.
    assert [(p.side.value, p.qty, Decimal(p.avg_entry_price)) for p in positions] == [("B", "0.01", Decimal("2000"))]
    assert Decimal(balances[0].real_balance) == Decimal("1000") - Decimal("0.008")

    # The feed shows the order's execution (sequence 7) and then the position that includes it.
    venue.socket.update(venue.channel("perpExecutions"), [fill(order="10", seq=7, takerAccountId=8009)])
    await settle(lambda: live.own_fills and live.own_fills[0].sequence == 7)
    venue.socket.update(venue.channel("positions"), [make_position(qty="0.01", last_trade_sequence_number=7).to_dict()])
    await settle(lambda: not live.own_fills)
    _, positions = inputs(live)
    assert [p.qty for p in positions] == ["0.01"]  # counted once, from the feed

    # A second fill whose execution has not arrived while the position changed: it may or may not be included.
    token = live.before_send(SYMBOL)
    venue.socket.update(venue.channel("positions"), [make_position(qty="0.02", last_trade_sequence_number=8).to_dict()])
    await settle(lambda: live._position_updates[SYMBOL] > token.position_updates)
    live.on_ack(
        token,
        order_id="11",
        is_buy=True,
        filled_qty=Decimal("0.01"),
        price=Decimal("2000"),
        order_cum_qty=Decimal("0.01"),
        fee_rate=fee,
    )
    with pytest.raises(_live.OwnFillPending, match="Order 11 filled"):
        live.wallet_inputs()  # ambiguous, and REST trails the feed: refuse rather than read it
    venue.socket.update(venue.channel("perpExecutions"), [fill(order="11", seq=9, takerAccountId=8009)])
    await settle(lambda: live.own_fills and live.own_fills[0].sequence == 9)
    _, positions = inputs(live)  # sequence known and not yet applied: the fill counts again
    assert [p.qty for p in positions] == ["0.03"]
    await live.close()


def ack_fill(live: LiveState, order_id="10", *, is_buy=True, qty="0.01", fee_rate=Decimal("0.0004"), token=None):
    live.on_ack(
        token or live.before_send(SYMBOL),
        order_id=order_id,
        is_buy=is_buy,
        filled_qty=Decimal(qty),
        price=Decimal("2000"),
        order_cum_qty=Decimal(qty),
        fee_rate=fee_rate,
    )


@pytest.mark.parametrize("api, ceiling", [(API, 15), ("https://api-devnet.reya-cronos.network/v2", 30)])
async def test_an_own_fill_counts_through_slow_indexing_and_is_dropped_only_after_the_bound(api, ceiling):
    clock, venue = Clock(), Venue("0x" + "ab" * 20)
    live = LiveState(api, venue.wallet, 8009, {SYMBOL}, rest=rest_stub(), connect=venue.connect, clock=clock)
    assert live.own_fill_max_s == ceiling  # mainnet indexes in about 1 s; devnet1 had 12-15 s episodes
    await live.start(timeout=1)
    ack_fill(live)
    # Devnet1 indexed fills 12-13 s after the acknowledgement: the fill and its debit still count then.
    clock.now += 13
    venue.socket.update(f"/v2/perpMarket/{SYMBOL}/summary", summary_row())  # keep the mark fresh
    await settle(lambda: live.mark_summary(SYMBOL) is not None)
    balances, positions = inputs(live)
    assert [p.qty for p in positions] == ["0.01"]
    assert Decimal(balances[0].real_balance) == Decimal("1000") - Decimal("0.008")
    assert len(venue.sockets) == 1  # no resubscribe while the fill is within its bound

    # Never shown within the ceiling (busted, or the feed missed it): dropped, resubscribed, REST decides.
    clock.now = 1000.0 + ceiling + 0.1
    assert live.wallet_inputs() is None and not live.own_fills
    assert f"not shown by the feed within {ceiling} s" in (live.last_error or "")
    await settle(lambda: len(venue.sockets) >= 2)
    await live.close()


async def test_a_resubscribe_while_a_fill_is_pending_refuses_until_the_new_feed_shows_it(monkeypatch):
    monkeypatch.setattr(_live, "VERIFY_RETRY_S", 0.01)
    venue = Venue("0x" + "ab" * 20)
    rest = rest_stub()
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest, connect=venue.connect)
    await live.start(timeout=1)
    ack_fill(live)
    assert [p.qty for p in inputs(live)[1]] == ["0.01"]

    await venue.socket.close()  # the server resyncs: a new snapshot that may or may not include the fill
    await settle(lambda: len(venue.sockets) == 2 and live.connected)
    assert not live.wallet_ready()  # verification waits for the fill to resolve (see _compare)
    with pytest.raises(_live.OwnFillPending, match="does not show that fill yet"):
        live.wallet_inputs()

    rest.get_positions.return_value = [make_position(qty="0.01", last_trade_sequence_number=7)]
    venue.socket.update(venue.channel("perpExecutions"), [fill(order="10", seq=7, takerAccountId=8009)])
    venue.socket.update(venue.channel("positions"), [make_position(qty="0.01", last_trade_sequence_number=7).to_dict()])
    await settle(live.wallet_ready)
    _, positions = inputs(live)
    assert not live.own_fills and [p.qty for p in positions] == ["0.01"]  # counted once, from the feed
    await live.close()


async def test_a_fill_without_a_fee_bound_or_a_fresh_mark_refuses():
    clock, venue = Clock(), Venue("0x" + "ab" * 20)
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest_stub(), connect=venue.connect, clock=clock)
    await live.start(timeout=1)
    ack_fill(live, fee_rate=None)  # fee tiers unreadable: no bound to debit
    with pytest.raises(_live.OwnFillPending):
        live.wallet_inputs()
    live.own_fills.clear()
    ack_fill(live, order_id="11")
    assert inputs(live)
    clock.now += _live.MARK_MAX_AGE_S + 0.1  # the mark went stale: the fill cannot be valued
    with pytest.raises(_live.OwnFillPending):
        live.wallet_inputs()
    await live.close()


async def test_a_fill_debit_lasts_until_the_feed_shows_the_fill_then_settles():
    clock, venue = Clock(), Venue("0x" + "ab" * 20)
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest_stub(), connect=venue.connect, clock=clock)
    await live.start(timeout=1)
    ack_fill(live)
    clock.now += _live.BALANCE_SETTLE_S + 2  # past the old fixed debit window: the feed has not shown the fill
    venue.socket.update(f"/v2/perpMarket/{SYMBOL}/summary", summary_row())
    await settle(lambda: live.mark_summary(SYMBOL) is not None)
    assert Decimal(inputs(live)[0][0].real_balance) == Decimal("1000") - Decimal("0.008")

    venue.socket.update(venue.channel("perpExecutions"), [fill(order="10", seq=7, takerAccountId=8009)])
    venue.socket.update(venue.channel("positions"), [make_position(qty="0.01", last_trade_sequence_number=7).to_dict()])
    await settle(lambda: not live.own_fills)
    # The balance channel can trail the position: the debit stays BALANCE_SETTLE_S after the fill shows.
    clock.now += _live.BALANCE_SETTLE_S - 1
    venue.socket.update(f"/v2/perpMarket/{SYMBOL}/summary", summary_row())
    await settle(lambda: live.mark_summary(SYMBOL) is not None)
    assert Decimal(inputs(live)[0][0].real_balance) == Decimal("1000") - Decimal("0.008")
    clock.now += 2
    assert inputs(live)[0][0].real_balance == "1000"
    await live.close()


async def test_own_fills_shown_waits_for_the_feed_and_gives_up_at_its_bound():
    venue = Venue("0x" + "ab" * 20)
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest_stub(), connect=venue.connect)
    await live.start(timeout=1)
    assert await live.own_fills_shown(0)  # nothing pending
    ack_fill(live)
    assert not await live.own_fills_shown(0.05)

    async def index_later():
        await asyncio.sleep(0.05)
        venue.socket.update(
            venue.channel("positions"), [make_position(qty="0.01", last_trade_sequence_number=7).to_dict()]
        )
        await asyncio.sleep(0.05)
        # The execution arrives last: the fill resolves on it, without another position update.
        venue.socket.update(venue.channel("perpExecutions"), [fill(order="10", seq=7, takerAccountId=8009)])

    task = asyncio.create_task(index_later())
    started = time.monotonic()
    assert await live.own_fills_shown(5)
    assert time.monotonic() - started < 1
    await task
    await live.close()


# ------------------------------------------------------------------ the runner on warm state


async def test_runner_orders_read_nothing_before_the_send_and_guards_use_the_warm_state(
    client, monkeypatch, capsys  # noqa: F811
):
    venue = Venue(client.config.owner_wallet_address)
    warm(client, venue)
    rest_marks = unshadow_marks(client, monkeypatch)
    checks = {"balances": 0}

    async def first_order_then_shrink_balance(*args, **kwargs):
        # After the first order the venue pushes a small balance; REST still says 1000.
        venue.socket.update(venue.channel("accountBalances"), [make_balance(real_balance="10").to_dict()])
        await asyncio.sleep(0)
        return CreateOrderResponse(order_id="123", status="OPEN", cum_qty="0")

    client.orders.create_order.side_effect = first_order_then_shrink_balance
    original = client.get_account_balances.side_effect

    async def counted(*args, **kwargs):
        checks["balances"] += 1
        return [make_balance(real_balance="1000")] if original is None else await original()

    client.get_account_balances.side_effect = counted
    second = LIMIT[:-1] + ["124"]
    result = await runner.serve(stream(LIMIT, second), client, wide(client))
    events = outputs(capsys)
    assert events[0]["warm_state"]["wallet"] == "verified"
    assert events[1]["result"]["order_id"] == "123"
    refused = events[2]["result"]
    # The 10 USDC account value from the socket caps the 19 USDC order; REST (1000 USDC) would allow it.
    assert "max_order_notional" in [r["check"] for r in refused["refused_by"]]
    client.orders.create_order.assert_awaited_once()
    rest_marks.assert_not_awaited()
    assert checks["balances"] == 1  # the warm-state REST check only; no order read balances
    assert result["reason"] == "eof"


async def test_stale_or_unverified_warm_state_falls_back_to_fresh_reads(client, monkeypatch):  # noqa: F811
    clock, venue = Clock(), Venue(client.config.owner_wallet_address)
    live = warm(client, venue, clock=clock)
    rest_marks = unshadow_marks(client, monkeypatch)
    await live.start(timeout=1)
    assert client.warm_margin_inputs(8009, [SYMBOL]) is not None
    assert (await client.markets.get_perp_market_summary(symbol=SYMBOL)).mark_price == "2000"
    rest_marks.assert_not_awaited()

    clock.now += _live.MARK_MAX_AGE_S + 0.5
    await client.markets.get_perp_market_summary(symbol=SYMBOL)
    rest_marks.assert_awaited_once()  # stale mark: a fresh read

    venue.refuse = True
    await venue.socket.close()
    await settle(lambda: not live.connected)
    assert client.warm_margin_inputs(8009, [SYMBOL]) is None  # disconnected: balances/positions over REST
    assert client.warm_open_orders() is None
    await live.close()


async def test_uncertain_write_with_warm_state_is_never_resent_and_intent_is_durable_first(
    client, monkeypatch, capsys  # noqa: F811
):
    warm(client, Venue(client.config.owner_wallet_address))
    unshadow_marks(client, monkeypatch)

    async def send(**kwargs):
        path = client.lease.directory / (client.lease.keys[0] + ".json")
        state = json.loads(path.read_text())
        assert state["runner"]["requests"][0]["state"] == "intent"
        assert state["runner"]["requests"][0]["command"] == LIMIT
        assert state["last_nonce"] == int(kwargs["create_order_request"].nonce)
        raise TimeoutError()

    monkeypatch.setattr(client.orders, "create_order", AsyncMock(side_effect=send))
    result = await runner.serve(stream(LIMIT, LIMIT), client, policy())
    outputs(capsys)
    assert result["reason"] == "write_requires_reconciliation" and not result["reconciled"]
    client.orders.create_order.assert_awaited_once()


async def test_fill_fees_follow_the_result_and_settlement_still_runs(client, monkeypatch, capsys):  # noqa: F811
    client.orders.create_order.return_value = CreateOrderResponse(
        order_id="10", status="FILLED", cum_qty="0.01", first_fill_id="77", fill_count=1
    )
    filled = fill(order="10", qty="0.01", price="2000", takerFee="0.008", type="ORDER_MATCH", timestamp=now_ms())
    client.wallet.get_wallet_perp_executions.return_value = executions_page(filled)
    result = await runner.serve(stream(MARKET, MARKET + ["--fees_wait_s", "1"]), client, wide(client))
    events = outputs(capsys)
    first = next(i for i, e in enumerate(events) if e.get("id") == "0")
    assert events[first]["result"]["fees_status"] == "deferred"
    fee_events = [e for e in events if e.get("event") == "fees"]
    assert [e["request_id"] for e in fee_events] == ["0"]
    assert events.index(fee_events[0]) > first  # never before the result it belongs to
    assert fee_events[0]["fees_status"] == "complete" and fee_events[0]["fees_rusd"] == "0.008"
    second = next(e for e in events if e.get("id") == "1")["result"]
    assert second["fees_status"] != "deferred"  # an explicit wait stays inline
    assert client.lease.state["runner"]["fee_reads"][0]["order_id"] == "10"
    assert result["settlement"]["state"]  # post-send reconciliation still reads the fills at stop


async def test_deferred_fees_wait_for_the_feed_to_show_the_fill_before_reading(
    client, monkeypatch, capsys  # noqa: F811
):
    # Devnet, 19 Oct: executions were indexed 12-13 s after the fill, so a read straight after found nothing.
    monkeypatch.setattr(runner, "FEE_FEED_POLL_S", 0.005)
    venue = Venue(client.config.owner_wallet_address)
    warm(client, venue)
    unshadow_marks(client, monkeypatch)
    client.orders.create_order.return_value = CreateOrderResponse(
        order_id="10", status="FILLED", cum_qty="0.01", first_fill_id="77", fill_count=1
    )
    settled = fill(order="10", qty="0.01", price="2000", takerFee="0.008", type="ORDER_MATCH", timestamp=now_ms())
    indexed = asyncio.Event()
    early_reads = []

    async def executions(**kwargs):
        if not indexed.is_set():
            early_reads.append(kwargs)
            return executions_page()
        return executions_page(settled)

    client.wallet.get_wallet_perp_executions.side_effect = executions

    async def index_later():
        await settle(lambda: client.orders.create_order.await_count == 1)
        await asyncio.sleep(0.05)
        indexed.set()
        venue.socket.update(venue.channel("perpExecutions"), [settled])

    indexer = asyncio.create_task(index_later())
    await runner.serve(stream(MARKET), client, wide(client))
    await indexer
    events = outputs(capsys)
    result_at = next(i for i, e in enumerate(events) if e.get("id") == "0")
    assert events[result_at]["result"]["fees_status"] == "deferred"
    (fees,) = [e for e in events if e.get("event") == "fees"]
    assert events.index(fees) > result_at
    assert fees["fees_status"] == "complete" and fees["fees_rusd"] == "0.008"
    assert early_reads == []  # nothing read before the feed showed the fill


async def test_deferred_fees_read_rest_at_the_deadline_when_the_feed_never_shows_the_fill(
    client, monkeypatch, capsys  # noqa: F811
):
    monkeypatch.setattr(runner, "FEE_FEED_POLL_S", 0.005)
    monkeypatch.setattr(runner, "FEE_SETTLE_WAIT_S", 0.1)
    warm(client, Venue(client.config.owner_wallet_address))  # verified, but never pushes the execution
    unshadow_marks(client, monkeypatch)
    client.orders.create_order.return_value = CreateOrderResponse(
        order_id="10", status="FILLED", cum_qty="0.01", first_fill_id="77", fill_count=1
    )
    settled = fill(order="10", qty="0.01", price="2000", takerFee="0.008", type="ORDER_MATCH", timestamp=now_ms())
    client.wallet.get_wallet_perp_executions.return_value = executions_page(settled)
    await runner.serve(stream(MARKET), client, wide(client))
    (fees,) = [e for e in outputs(capsys) if e.get("event") == "fees"]
    assert fees["fees_status"] == "complete" and fees["fees_rusd"] == "0.008"


async def test_deferred_fees_without_warm_state_reread_until_the_fill_is_indexed(
    client, monkeypatch, capsys  # noqa: F811
):
    monkeypatch.setattr(runner, "FEE_RETRY_S", 0.01)
    monkeypatch.setattr(runner, "FEE_RETRY_MAX_S", 0.02)
    client.orders.create_order.return_value = CreateOrderResponse(
        order_id="10", status="FILLED", cum_qty="0.01", first_fill_id="77", fill_count=1
    )
    settled = fill(order="10", qty="0.01", price="2000", takerFee="0.008", type="ORDER_MATCH", timestamp=now_ms())
    client.wallet.get_wallet_perp_executions.side_effect = [executions_page(), executions_page()] + [
        executions_page(settled)
    ] * 10
    await runner.serve(stream(MARKET), client, wide(client))
    (fees,) = [e for e in outputs(capsys) if e.get("event") == "fees"]
    assert fees["fees_status"] == "complete" and fees["fees_rusd"] == "0.008"
    assert client.lease.state["runner"]["fee_reads"][0]["fees_status"] == "complete"


def deferred_fill(filled_qty="0.01", fill_count=1):
    return {
        "symbol": SYMBOL,
        "order_id": "10",
        "client_order_id": None,
        "filled_qty": filled_qty,
        "fill_count": fill_count,
        "first_fill_id": "77",
    }


async def test_a_fill_after_the_acknowledgement_ends_the_fee_wait_as_partial(client, monkeypatch, capsys):  # noqa: F811
    # A resting order filled again before its acknowledged fill was indexed: it can never match that fill alone.
    monkeypatch.setattr(runner, "FEE_RETRY_S", 0.01)
    monkeypatch.setattr(runner, "FEE_SETTLE_WAIT_S", 0.2)
    client.lease.state["runner"] = {}
    client.wallet.get_wallet_perp_executions.return_value = executions_page(
        fill(
            order="10",
            qty="0.005",
            fill_id="77",
            seq=7,
            price="2000",
            takerFee="0.004",
            type="ORDER_MATCH",
            timestamp=now_ms(),
        ),
        fill(
            order="10",
            qty="0.005",
            fill_id="78",
            seq=8,
            price="2000",
            takerFee="0.004",
            type="ORDER_MATCH",
            timestamp=now_ms(),
        ),
    )
    await client._fee_event("r", deferred_fill(filled_qty="0.005"))
    (fees,) = outputs(capsys)
    assert fees["fees_status"] == "partial" and fees["settled_qty"] == "0.010" and len(fees["executions"]) == 2
    client.wallet.get_wallet_perp_executions.assert_awaited_once()  # no re-reads until the deadline


async def test_a_failed_reread_keeps_the_executions_an_earlier_read_found(client, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(runner, "FEE_RETRY_S", 0.01)
    monkeypatch.setattr(runner, "FEE_RETRY_MAX_S", 0.01)
    monkeypatch.setattr(runner, "FEE_SETTLE_WAIT_S", 0.1)
    client.lease.state["runner"] = {}
    first = executions_page(
        fill(
            order="10",
            qty="0.005",
            fill_id="77",
            seq=7,
            price="2000",
            takerFee="0.004",
            type="ORDER_MATCH",
            timestamp=now_ms(),
        )
    )
    reads = []

    async def executions(**kwargs):
        reads.append(kwargs)
        if len(reads) == 1:
            return first
        raise RuntimeError("read failed")

    client.wallet.get_wallet_perp_executions.side_effect = executions
    await client._fee_event("r", deferred_fill(fill_count=2))
    (fees,) = outputs(capsys)
    assert len(reads) > 1
    assert fees["fees_status"] == "partial" and [e["fill_id"] for e in fees["executions"]] == ["77"]
    assert client.lease.state["runner"]["fee_reads"][0]["executions"] == fees["executions"]


async def test_shutdown_still_emits_the_fees_event_of_an_unindexed_fill(client, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(runner, "FEE_DRAIN_S", 0.05)
    monkeypatch.setattr(runner, "FEE_RETRY_S", 0.01)
    monkeypatch.setattr(runner, "FEE_RETRY_MAX_S", 0.01)
    client.orders.create_order.return_value = CreateOrderResponse(
        order_id="10", status="FILLED", cum_qty="0.01", first_fill_id="77", fill_count=1
    )
    client.wallet.get_wallet_perp_executions.return_value = executions_page()  # never indexed
    result = await runner.serve(stream(MARKET), client, wide(client))
    (fees,) = [e for e in outputs(capsys) if e.get("event") == "fees"]
    assert fees["order_id"] == "10" and fees["fees_status"] == "pending" and fees["fees_rusd"] is None
    assert result["reason"] == "eof"
    client.orders.create_order.assert_awaited_once()


async def test_cancel_resolves_from_the_warm_view_and_registry_without_a_read(
    client, monkeypatch, capsys  # noqa: F811
):
    venue = Venue(client.config.owner_wallet_address)
    warm(client, venue)
    unshadow_marks(client, monkeypatch)
    monkeypatch.setattr(
        client.orders,
        "cancel_order",
        AsyncMock(return_value=CancelOrderResponse(status="CANCELLED", order_id="123")),
    )
    reads = {"orders": 0}
    original = client.get_open_orders

    async def counted():
        reads["orders"] += 1
        return await original()

    monkeypatch.setattr(client, "get_open_orders", counted)
    cancel = ["order", "cancel", "--order_id", "123"]
    unknown = ["order", "cancel", "--order_id", "999"]
    await runner.serve(stream(LIMIT, cancel, unknown), client, policy())
    events = outputs(capsys)
    assert events[2]["result"]["status"] == "CANCELLED"
    client.orders.cancel_order.assert_awaited_once()
    assert "not open" in events[3]["result"]["error"]  # unknown: REST decides, and nothing is sent
    # Startup check, the warm-state REST check, the unknown cancel and two shutdown reads; not the known cancel.
    assert reads["orders"] == 5


@pytest.mark.parametrize(
    "failure, command, continues",
    [
        (
            BadRequestException(status=400, body=json.dumps({"error": "RATE_LIMITED_ERROR", "retryAfterMs": 900})),
            LIMIT,
            True,
        ),
        (BadRequestException(status=400, body=json.dumps({"error": "POST_ONLY_WOULD_CROSS_ERROR"})), LIMIT, True),
        # A close sent before the fill it reduces has settled (devnet, 19 Oct): the engine refuses it.
        (
            BadRequestException(status=400, body=json.dumps({"error": "REDUCE_ONLY_CONDITION_NOT_MET_ERROR"})),
            MARKET[:3] + ["--side", "short", "--qty", "0.01", "--reduce_only", "--slippage", "0.005"],
            True,
        ),
        (
            BadRequestException(status=400, body=json.dumps({"error": "REDUCE_ONLY_CONDITION_NOT_MET_ERROR"})),
            LIMIT,
            False,
        ),
        (ApiException(status=429, reason="Too Many Requests"), LIMIT, True),
        (CreateOrderResponse(order_id="5", status="CANCELLED", cum_qty="0.005", avg_fill_px="2001"), MARKET, True),
        (BadRequestException(status=400, body=json.dumps({"error": "ORDER_NOT_FOUND_ERROR"})), LIMIT, False),
        (BadRequestException(status=400, body=json.dumps({"error": "CREATE_ORDER_OTHER_ERROR"})), LIMIT, False),
        (ServiceException(status=503, body="unavailable"), LIMIT, False),
        (CreateOrderResponse(order_id="5", status="UNKNOWN"), LIMIT, False),
    ],
)
async def test_definitive_rejections_keep_the_session_and_ambiguous_outcomes_halt(
    client, capsys, failure, command, continues  # noqa: F811
):
    client.orders.create_order.side_effect = [failure, CreateOrderResponse(order_id="6", status="OPEN", cum_qty="0")]
    result = await runner.serve(stream(command, LIMIT), client, wide(client))
    events = outputs(capsys)
    first = events[1]["result"]
    assert "error" in first
    if continues:
        assert first["session_continues"] is True and result["reason"] == "eof"
        assert client.orders.create_order.await_count == 2  # the next request; the rejected one is not resent
        if first.get("error_code") == "RATE_LIMITED_ERROR":
            assert first["retry_after_ms"] == 900
    else:
        assert "session_continues" not in first and result["reason"] == "write_requires_reconciliation"
        client.orders.create_order.assert_awaited_once()


async def test_cancel_of_an_order_that_already_finished_keeps_the_session(client, monkeypatch, capsys):  # noqa: F811
    client.get_open_orders.return_value = [make_order(order_id="123")]
    monkeypatch.setattr(
        client.orders,
        "cancel_order",
        AsyncMock(side_effect=BadRequestException(status=400, body=json.dumps({"error": "ORDER_NOT_FOUND_ERROR"}))),
    )
    started = asyncio.Event()

    async def flat_at_start():
        # Startup requires a flat account; the order appears afterwards.
        if not started.is_set():
            started.set()
            return []
        return [make_order(order_id="123")]

    client.get_open_orders.side_effect = flat_at_start
    result = await runner.serve(stream(["order", "cancel", "--order_id", "123"], LIMIT), client, policy())
    events = outputs(capsys)
    assert events[1]["result"]["error_code"] == "ORDER_NOT_FOUND_ERROR"
    assert events[1]["result"]["session_continues"] is True
    assert result["reason"] != "write_requires_reconciliation"
    client.orders.create_order.assert_awaited_once()


async def test_heartbeat_renews_the_thirty_second_countdown_every_ten_seconds(client, monkeypatch):  # noqa: F811
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(runner.asyncio, "sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await runner.heartbeat(client, asyncio.Event())
    assert sleeps == [10] and runner.COUNTDOWN_MS == 30000


async def test_reference_data_and_ownership_are_read_once_per_session(client, monkeypatch, capsys):  # noqa: F811
    from sdk.open_api.models.account import Account as TradingAccount

    from agent_kit.tests.conftest import make_market_def

    definitions = AsyncMock(return_value=[make_market_def()])
    del client.reference.get_perp_market_definitions
    monkeypatch.setattr(client.reference._api, "get_perp_market_definitions", definitions)
    accounts = AsyncMock(return_value=[TradingAccount(account_id=8009, name="dedicated", type="SUBPERP")])
    del client.get_accounts
    monkeypatch.setattr(client.wallet, "get_wallet_accounts", accounts)
    second = LIMIT[:-1] + ["124"]
    await runner.serve(stream(LIMIT, second), client, wide(client))
    outputs(capsys)
    assert client.orders.create_order.await_count == 2
    definitions.assert_awaited_once()
    accounts.assert_awaited_once()


async def test_a_stalled_fee_tier_read_holds_up_neither_the_session_nor_its_orders(
    client, monkeypatch, capsys  # noqa: F811
):
    async def stalled():
        await asyncio.Event().wait()  # a name lookup or a response that never comes

    monkeypatch.setattr(client.reference._api, "get_fee_tier_parameters", stalled)
    monkeypatch.setattr(runner, "FEE_TIERS_TIMEOUT_S", 0.01)
    monkeypatch.setattr(_margin, "FEE_TIERS_TIMEOUT_S", 0.01)
    # Bounded here, so a regression fails instead of hanging the suite.
    result = await asyncio.wait_for(runner.serve(stream(LIMIT), client, policy()), timeout=10)
    events = outputs(capsys)
    assert events[0]["event"] == "ready" and result["reason"] == "eof"
    assert "error" not in events[1]["result"]
    assert events[1]["result"]["margin"]["taker_fee_rate_assumed"] is None
    client.orders.create_order.assert_awaited_once()


async def test_a_warm_state_bookkeeping_failure_never_fails_an_acknowledged_order(
    client, monkeypatch, capsys  # noqa: F811
):
    live = warm(client, Venue(client.config.owner_wallet_address))
    unshadow_marks(client, monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError("bookkeeping bug")

    monkeypatch.setattr(live, "on_ack", broken)
    second = LIMIT[:-1] + ["124"]
    result = await runner.serve(stream(LIMIT, second), client, wide(client))
    events = outputs(capsys)
    assert events[1]["result"]["order_id"] == "123" and "error" not in events[1]["result"]
    assert client.orders.create_order.await_count == 2  # the session went on, reading REST
    assert result["reason"] == "eof"


# ------------------------------------------------------------------ TP/SL position evidence from the feed

PROTECTED = MARKET + ["--sl", "1%", "--tp", "1%"]
STOP = ["order", "sl", SYMBOL, "--side", "long", "--trigger_px", "1900", "--limit_px", "1899", "--time_in_force", "IOC"]


async def test_published_position_leaves_out_own_fills_and_wakes_waiters():
    venue = Venue("0x" + "ab" * 20)
    venue.snapshots[venue.channel("positions")] = [make_position(qty="0.01", last_trade_sequence_number=3).to_dict()]
    live = LiveState(
        API,
        venue.wallet,
        8009,
        {SYMBOL},
        rest=rest_stub(positions=[make_position(qty="0.01", last_trade_sequence_number=3)]),
        connect=venue.connect,
    )
    await live.start(timeout=1)
    live.on_ack(
        live.before_send(SYMBOL),
        order_id="10",
        is_buy=True,
        filled_qty=Decimal("0.01"),
        price=Decimal("2000"),
        order_cum_qty=Decimal("0.01"),
        fee_rate=Decimal("0.0004"),
    )
    published = live.published_position(SYMBOL)
    assert published is not None and published.sequence == 3
    assert published.position is not None and published.position.qty == "0.01"  # the own fill is not evidence
    _, positions = inputs(live)
    assert [p.qty for p in positions] == ["0.02"]  # while the guards still count it

    waiter = asyncio.ensure_future(live.wallet_changed(5))
    await asyncio.sleep(0)
    venue.socket.update(venue.channel("positions"), [make_position(qty="0", last_trade_sequence_number=4).to_dict()])
    await asyncio.wait_for(waiter, 1)  # woken by the update, not the timeout
    assert live.published_position(SYMBOL) == (None, 4)  # closed: flat, and the sequence is kept

    waiter = asyncio.ensure_future(live.wallet_changed(5))
    await asyncio.sleep(0)
    venue.refuse = True
    await venue.socket.close()
    await asyncio.wait_for(waiter, 1)  # woken by the disconnect
    assert live.published_position(SYMBOL) is None
    await live.close()


def trigger_row(order_id: str, sequence: int, *, triggered: bool = False) -> dict:
    """An orderChanges row for one of the bracket's triggers, as the venue publishes it."""
    order_type = OrderType.STOP_LOSS if order_id.startswith("sl") else OrderType.TAKE_PROFIT
    order = make_order(order_id=order_id, order_type=order_type, side=Side.A, trigger_px="1980")
    return {**json.loads(order.to_json()), "sequenceNumber": sequence, "triggered": triggered}


def protected_entry(
    client,  # noqa: F811
    monkeypatch,
    *,
    on_entry=None,
    rest_shows_entry_after=1,
    rest_before=True,
    venue=None,
    shown=("sl", "tp"),
    changes=None,
):
    """A filled entry, then the stop-loss and take-profit. Counts REST reads.

    plan_reads: position and open-order reads before the entry. reads_after_entry: position
    reads after it. order_reads: open-order reads after it. With `venue`, the feed publishes
    each trigger named in `shown` shortly after its acknowledgement: armed, or with `changes`.
    """
    held = make_position(qty="0.01", last_trade_sequence_number=3)
    entered = make_position(qty="0.02", last_trade_sequence_number=5)
    responses = iter(
        [
            CreateOrderResponse(order_id="entry", status="FILLED", cum_qty="0.01", avg_fill_px="2000"),
            CreateOrderResponse(order_id="sl", status="OPEN"),
            CreateOrderResponse(order_id="tp", status="OPEN"),
        ]
    )
    sent: list = []

    async def create(*args, **kwargs):
        sent.append(kwargs["create_order_request"])
        if len(sent) == 1 and on_entry is not None:
            await on_entry()
        response = next(responses)
        if venue is not None and response.order_id in shown:
            row = {**trigger_row(response.order_id, 10 + len(sent)), **(changes or {}).get(response.order_id, {})}
            asyncio.get_running_loop().call_later(0.05, venue.socket.update, venue.channel("orderChanges"), [row])
        return response

    plan_reads: list = []
    reads_after_entry: list = []
    order_reads: list = []

    async def positions():
        if not sent:
            plan_reads.append("positions")
            return [held] if rest_before else []
        reads_after_entry.append(True)
        return [entered] if len(reads_after_entry) > rest_shows_entry_after else [held]

    async def orders():
        (order_reads if sent else plan_reads).append("orders")
        armed = [("sl", OrderType.STOP_LOSS), ("tp", OrderType.TAKE_PROFIT)][: max(0, len(sent) - 1)]
        return [make_order(order_id=i, order_type=t, side=Side.A) for i, t in armed]

    monkeypatch.setattr(client.orders, "create_order", AsyncMock(side_effect=create))
    monkeypatch.setattr(client, "get_positions", positions)
    monkeypatch.setattr(client, "get_open_orders", orders)
    return SimpleNamespace(
        held=held,
        entered=entered,
        sent=sent,
        plan_reads=plan_reads,
        reads_after_entry=reads_after_entry,
        order_reads=order_reads,
    )


def held_venue(client) -> Venue:  # noqa: F811
    venue = Venue(client.config.owner_wallet_address)
    venue.snapshots[venue.channel("positions")] = [make_position(qty="0.01", last_trade_sequence_number=3).to_dict()]
    return venue


def trigger_types(sent) -> list:
    return [OrderType(request.order_type) for request in sent[1:]]


async def test_protection_arms_and_reads_back_on_the_feed_without_a_rest_read(client, monkeypatch):  # noqa: F811
    venue = held_venue(client)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)
    feed = venue.channel("positions")

    async def publish_later():
        # A funding-only update keeps the old sequence: not evidence. Then the settled entry.
        await asyncio.sleep(0.05)
        venue.socket.update(
            feed, [make_position(qty="0.01", last_trade_sequence_number=3, avg_entry_funding_value="1001").to_dict()]
        )
        await asyncio.sleep(0.05)
        venue.socket.update(feed, [entry.entered.to_dict()])

    async def on_entry():
        asyncio.ensure_future(publish_later())

    entry = protected_entry(client, monkeypatch, on_entry=on_entry, rest_shows_entry_after=99, venue=venue)
    await live.start(timeout=1)
    assert live.wallet_ready()
    entry.plan_reads.clear()  # the warm state's own REST check at start
    started = time.monotonic()
    outcome = await runner.execute_request(client, {"command": PROTECTED})

    protection = outcome["protection"]
    assert protection["state"] == "protected" and protection["verified"] is True
    assert protection["stop_loss"]["visible"] is True and protection["take_profit"]["visible"] is True
    assert protection["position"] == {"side": "long", "qty": "0.02", "last_trade_sequence_number": 5}
    assert entry.plan_reads == []  # the --sl/--tp checks used the warm view
    assert entry.reads_after_entry == []  # REST never showed the entry; the feed was the evidence
    assert entry.order_reads == []  # the feed showed both triggers armed: no open-order read
    assert trigger_types(entry.sent) == [OrderType.STOP_LOSS, OrderType.TAKE_PROFIT]  # stop-loss first
    assert time.monotonic() - started < 1.5  # armed on the update, not after polling
    await live.close()


@pytest.mark.parametrize("how", ["unverified", "disconnected", "invalidated"])
async def test_an_unverified_or_lost_feed_falls_back_to_rest_reads(client, monkeypatch, how):  # noqa: F811
    venue = held_venue(client)
    if how == "unverified":
        venue.snapshots[venue.channel("accountBalances")] = [make_balance(real_balance="5").to_dict()]
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)

    async def on_entry():
        if how == "disconnected":
            venue.refuse = True
            await venue.socket.close()  # the feed goes away while protection waits on it
        elif how == "invalidated":
            live.invalidate("test: wallet state suspect")

    entry = protected_entry(client, monkeypatch, on_entry=on_entry, venue=venue, shown=())
    status = await live.start(timeout=0.3)
    assert status["wallet"] == ("unavailable" if how == "unverified" else "verified")
    entry.plan_reads.clear()
    outcome = await runner.execute_request(client, {"command": PROTECTED})

    protection = outcome["protection"]
    assert protection["state"] == "protected" and protection["verified"] is True
    assert protection["position"]["last_trade_sequence_number"] == 5
    assert len(entry.reads_after_entry) == 2  # REST decided: the first read still lagged, the second showed it
    assert len(entry.order_reads) == 1  # and REST showed both triggers armed
    # The --sl/--tp checks read REST only while the wallet state is unverified; a later loss is after them.
    assert ("orders" in entry.plan_reads) is (how == "unverified")
    assert trigger_types(entry.sent) == [OrderType.STOP_LOSS, OrderType.TAKE_PROFIT]
    await live.close()


async def test_no_feed_evidence_within_the_bound_leaves_the_position_unprotected(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(trade, "indexing_ceiling", lambda api_url: 0.3)
    venue = held_venue(client)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)

    async def on_entry():
        # Only a funding-only update arrives; it keeps the sequence from before the entry.
        update = make_position(qty="0.01", last_trade_sequence_number=3, avg_entry_funding_value="1001")
        asyncio.get_running_loop().call_later(0.05, venue.socket.update, venue.channel("positions"), [update.to_dict()])

    entry = protected_entry(client, monkeypatch, on_entry=on_entry)
    await live.start(timeout=1)
    outcome = await runner.execute_request(client, {"command": PROTECTED})

    protection = outcome["protection"]
    assert protection["state"] == "unprotected" and "UNPROTECTED" in outcome["error"]
    assert "did not show it within 0.3 seconds" in protection["message"]
    assert "order sl ETHRUSDPERP --side long" in protection["message"]
    assert protection["stop_loss"]["armed"] is False and protection["take_profit"]["armed"] is False
    assert len(entry.sent) == 1  # the entry only: no trigger, and the entry is never resent
    assert entry.reads_after_entry == []  # the verified feed decided within the bound
    await live.close()


async def test_order_sl_in_the_runner_waits_on_the_feed_not_on_rest_reads(client, monkeypatch):  # noqa: F811
    venue = Venue(client.config.owner_wallet_address)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)
    reads: list = []
    original = client.get_positions

    async def counted():
        reads.append(True)
        return await original()

    await live.start(timeout=1)
    monkeypatch.setattr(client, "get_positions", counted)  # REST stays flat throughout
    shown = make_position(qty="0.01", last_trade_sequence_number=5)
    asyncio.get_running_loop().call_later(0.05, venue.socket.update, venue.channel("positions"), [shown.to_dict()])
    outcome = await runner.execute_request(client, {"command": STOP})

    assert outcome["position_side"] == "long" and outcome["order_id"] == "123"
    assert reads == []
    client.orders.create_order.assert_awaited_once()
    await live.close()


async def test_feed_evidence_must_be_newer_than_the_feeds_own_view_before_the_send(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(trade, "indexing_ceiling", lambda api_url: 0.3)
    venue = Venue(client.config.owner_wallet_address)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)
    entry = protected_entry(client, monkeypatch, rest_before=False)
    monkeypatch.setattr(client, "warm_open_orders", lambda: None)  # so the --sl/--tp checks read REST
    await live.start(timeout=1)
    # An earlier fill (sequence 3) is on the feed; REST still shows the account flat before the entry.
    venue.socket.update(venue.channel("positions"), [entry.held.to_dict()])
    await settle(lambda: live.published_position(SYMBOL) == (entry.held, 3))
    entry.plan_reads.clear()
    outcome = await runner.execute_request(client, {"command": PROTECTED})

    # The long at sequence 3 predates the entry, so it is no evidence of it: nothing is armed.
    assert "orders" in entry.plan_reads  # only the feed's view before the send knew of it
    assert outcome["protection"]["state"] == "unprotected"
    assert len(entry.sent) == 1 and entry.reads_after_entry == []
    await live.close()


DEVNET = "https://api-devnet.reya-cronos.network/v2"
SCALE = 0.02  # one second of a real ceiling is 20 ms here


def scaled_ceilings(monkeypatch):
    """The real per-deployment ceilings and read rate at SCALE (read caps unchanged), run in milliseconds."""
    monkeypatch.setattr(trade, "indexing_ceiling", lambda api_url: _live.indexing_ceiling(api_url) * SCALE)
    monkeypatch.setattr(trade, "ENTRY_POSITION_READS_PER_S", trade.ENTRY_POSITION_READS_PER_S / SCALE)


def from_the_wait(monkeypatch, *events) -> SimpleNamespace:
    """Run each (delay, callback) that long after the position wait starts; returns its loop times (started, ended).

    The scaled times count from where the ceiling counts, the wait's start, not from the send or the command:
    a loaded CI runner took 0.1 s from the send to the wait, so an update 20 s (0.4 s here) after the send
    came 15 s into the wait, exactly mainnet's ceiling. Bounds are on the wait itself for the same reason.
    """
    real = trade._await_position
    wait = SimpleNamespace(started=None, ended=None)

    async def timed(*args, **kwargs):
        if wait.started is not None:
            return await real(*args, **kwargs)
        loop = asyncio.get_running_loop()
        wait.started = loop.time()
        for delay, callback in events:
            loop.call_later(delay, callback)
        try:
            return await real(*args, **kwargs)
        finally:
            wait.ended = loop.time()

    monkeypatch.setattr(trade, "_await_position", timed)
    return wait


@pytest.mark.parametrize("api_url, state", [(DEVNET, "protected"), (API, "unprotected")])
async def test_a_fill_the_feed_shows_20_s_late_arms_within_devnets_ceiling_not_mainnets(
    client, monkeypatch, api_url, state  # noqa: F811
):
    # devnet1, 19 Oct 2026: fills reached the feed 4.6-15.4 s after their block (mainnet's worst: 4.15 s).
    scaled_ceilings(monkeypatch)
    monkeypatch.setattr(client.config, "api_url", api_url)
    venue = held_venue(client)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)
    entry = protected_entry(client, monkeypatch, rest_shows_entry_after=99, venue=venue)
    update = [entry.entered.to_dict()]
    from_the_wait(monkeypatch, (20 * SCALE, lambda: venue.socket.update(venue.channel("positions"), update)))
    await live.start(timeout=1)
    outcome = await runner.execute_request(client, {"command": PROTECTED})

    protection = outcome["protection"]
    _, ceiling = trade.entry_position_bounds(client)
    assert protection["state"] == state and protection["position_wait_limit_s"] == ceiling
    assert entry.reads_after_entry == []  # the verified feed decided, not REST
    if state == "protected":
        assert protection["verified"] is True
        assert trigger_types(entry.sent) == [OrderType.STOP_LOSS, OrderType.TAKE_PROFIT]  # stop-loss first
        assert protection["position"]["last_trade_sequence_number"] == 5  # the update's, not the one before
        assert 20 * SCALE - 0.01 <= protection["position_wait_s"] < ceiling  # ended at the evidence
    else:
        assert len(entry.sent) == 1  # nothing placed, and the entry is never resent
        assert protection["position_wait_s"] >= ceiling - 0.01
        assert f"did not show it within {ceiling:g} seconds" in protection["message"]
    await live.close()


@pytest.mark.parametrize("rest_shows", [True, False])
async def test_feed_loss_mid_wait_hands_only_the_remaining_bound_to_rest(client, monkeypatch, rest_shows):  # noqa: F811
    scaled_ceilings(monkeypatch)
    monkeypatch.setattr(client.config, "api_url", DEVNET)
    monkeypatch.setattr(trade, "POSITION_POLL_S", 0.01)
    venue = held_venue(client)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)

    async def drop():
        venue.refuse = True
        await venue.socket.close()

    wait = from_the_wait(monkeypatch, (10 * SCALE, lambda: asyncio.ensure_future(drop())))
    entry = protected_entry(client, monkeypatch, venue=venue, shown=())
    rest_reads: list = []

    async def positions():
        # REST shows the entry 20 s (scaled) into the wait: after the feed is gone, inside devnet's 30 s.
        if wait.started is not None:
            rest_reads.append(True)
            if rest_shows and asyncio.get_running_loop().time() - wait.started >= 20 * SCALE:
                return [entry.entered]
        return [entry.held]

    monkeypatch.setattr(client, "get_positions", positions)
    await live.start(timeout=1)
    outcome = await runner.execute_request(client, {"command": PROTECTED})

    protection = outcome["protection"]
    _, ceiling = trade.entry_position_bounds(client)
    assert len(rest_reads) > 1  # REST took over once the feed went away
    if rest_shows:
        assert protection["state"] == "protected" and protection["position"]["last_trade_sequence_number"] == 5
        assert 20 * SCALE - 0.01 <= protection["position_wait_s"] < ceiling
    else:
        assert protection["state"] == "unprotected" and len(entry.sent) == 1
        # One bound in all: REST got what the feed left, not a fresh ceiling (that would end a third later).
        assert ceiling - 0.01 <= protection["position_wait_s"] < ceiling + 4 * SCALE
    await live.close()


@pytest.mark.parametrize("api_url, placed", [(DEVNET, True), (API, False)])
async def test_order_sl_on_a_position_the_feed_shows_20_s_late_within_devnets_ceiling_not_mainnets(
    client, monkeypatch, api_url, placed  # noqa: F811
):
    # The recovery step after an `unprotected` entry runs while the venue is slow to index the fill.
    scaled_ceilings(monkeypatch)
    # The runner's command bound at the same scale: it must cover the ceiling and the trigger write.
    monkeypatch.setattr(runner, "COMMAND_TIMEOUT_S", runner.COMMAND_TIMEOUT_S * SCALE)
    monkeypatch.setattr(runner, "WRITE_TIMEOUT_S", runner.WRITE_TIMEOUT_S * SCALE)
    monkeypatch.setattr(client.config, "api_url", api_url)
    venue = Venue(client.config.owner_wallet_address)  # flat until the fill is indexed
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)
    reads: list = []

    async def counted():
        reads.append(True)
        return []

    await live.start(timeout=1)
    monkeypatch.setattr(client, "get_positions", counted)  # REST stays flat throughout
    shown = make_position(qty="0.01", last_trade_sequence_number=5)
    _, ceiling = trade.entry_position_bounds(client)
    wait = from_the_wait(
        monkeypatch, (20 * SCALE, lambda: venue.socket.update(venue.channel("positions"), [shown.to_dict()]))
    )
    if placed:
        outcome = await runner.execute_request(client, {"command": STOP})
        assert outcome["position_side"] == "long" and outcome["order_id"] == "123"
        client.orders.create_order.assert_awaited_once()
        assert 20 * SCALE - 0.01 <= wait.ended - wait.started < ceiling  # ended at the position
    else:
        with pytest.raises(ValueError, match=f"within {ceiling:g} seconds; no protective order submitted"):
            await runner.execute_request(client, {"command": STOP})
        client.orders.create_order.assert_not_awaited()
        assert ceiling - 0.01 <= wait.ended - wait.started < 20 * SCALE
    assert reads == []  # the verified feed decided, not REST
    await live.close()


@pytest.mark.parametrize("rest_shows", [True, False])
async def test_order_sl_feed_loss_mid_wait_hands_only_the_remaining_bound_to_rest(
    client, monkeypatch, rest_shows  # noqa: F811
):
    scaled_ceilings(monkeypatch)
    monkeypatch.setattr(client.config, "api_url", DEVNET)
    monkeypatch.setattr(trade, "POSITION_POLL_S", 0.01)
    venue = Venue(client.config.owner_wallet_address)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)
    loop = asyncio.get_running_loop()
    rest_reads: list = []

    async def drop():
        venue.refuse = True
        await venue.socket.close()

    wait = from_the_wait(monkeypatch, (10 * SCALE, lambda: asyncio.ensure_future(drop())))

    async def positions():
        # REST shows the position 20 s (scaled) into the wait: after the feed is gone, inside devnet's 30 s.
        if wait.started is not None:
            rest_reads.append(True)
            if rest_shows and loop.time() - wait.started >= 20 * SCALE:
                return [make_position(qty="0.01", last_trade_sequence_number=5)]
        return []

    await live.start(timeout=1)
    monkeypatch.setattr(client, "get_positions", positions)
    _, ceiling = trade.entry_position_bounds(client)
    if rest_shows:
        outcome = await runner.execute_request(client, {"command": STOP})
        assert outcome["order_id"] == "123"
        assert 20 * SCALE - 0.01 <= wait.ended - wait.started < ceiling
    else:
        with pytest.raises(ValueError, match="no protective order submitted"):
            await runner.execute_request(client, {"command": STOP})
        client.orders.create_order.assert_not_awaited()
        # One bound in all: REST got what the feed left, not a fresh ceiling (that would end a third later).
        assert ceiling - 0.01 <= wait.ended - wait.started < ceiling + 4 * SCALE
    assert len(rest_reads) > 1  # REST took over once the feed went away
    await live.close()


# ------------------------------------------------------------------ trigger read-back and checks on the warm view


ENTERED = make_position(qty="0.02", last_trade_sequence_number=5)  # protected_entry's position after the entry


def publish_evidence_on_entry(venue, *, then=None):
    """on_entry: the feed publishes the settled entry shortly after the send; `then` runs a little later."""

    async def on_entry():
        loop = asyncio.get_running_loop()
        loop.call_later(0.02, venue.socket.update, venue.channel("positions"), [ENTERED.to_dict()])
        if then is not None:
            loop.call_later(0.3, then)

    return on_entry


@pytest.mark.parametrize("how", ["disconnected", "invalidated"])
async def test_trigger_read_back_falls_back_to_rest_when_the_feed_goes_away(client, monkeypatch, how):  # noqa: F811
    venue = held_venue(client)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)

    def lose():
        # The triggers are acknowledged but not yet on the feed when it goes away.
        if how == "disconnected":
            venue.refuse = True
            asyncio.ensure_future(venue.socket.close())
        else:
            live.invalidate("test: wallet state suspect")

    entry = protected_entry(
        client,
        monkeypatch,
        on_entry=publish_evidence_on_entry(venue, then=lose),
        rest_shows_entry_after=99,
        venue=venue,
        shown=(),
    )
    await live.start(timeout=1)
    started = time.monotonic()
    outcome = await runner.execute_request(client, {"command": PROTECTED})

    protection = outcome["protection"]
    assert protection["state"] == "protected" and protection["verified"] is True
    assert protection["stop_loss"]["visible"] is True and protection["take_profit"]["visible"] is True
    assert entry.reads_after_entry == []  # the feed was the position evidence
    assert len(entry.order_reads) == 1  # REST decided the read-back once the feed went away
    assert time.monotonic() - started < 1.5  # woken by the loss, not at the end of the bound
    assert trigger_types(entry.sent) == [OrderType.STOP_LOSS, OrderType.TAKE_PROFIT]
    await live.close()


@pytest.mark.parametrize(
    "take_profit",
    [None, {"status": "CANCELLED", "cancelReason": "POSITION_CLOSED"}, {"triggered": True}],
    ids=["never_shown", "shown_cancelled", "shown_fired"],
)
async def test_only_a_trigger_the_feed_shows_armed_is_visible_never_an_acknowledged_one(
    client, monkeypatch, take_profit  # noqa: F811
):
    monkeypatch.setattr(trade, "TRIGGER_READBACK_WAIT_S", 0.3)
    venue = held_venue(client)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)
    entry = protected_entry(
        client,
        monkeypatch,
        on_entry=publish_evidence_on_entry(venue),
        rest_shows_entry_after=99,
        venue=venue,
        shown=("sl",) if take_profit is None else ("sl", "tp"),
        changes={"tp": take_profit},
    )
    await live.start(timeout=1)
    outcome = await runner.execute_request(client, {"command": PROTECTED})

    protection = outcome["protection"]
    assert protection["state"] == "protected"  # both acknowledged as armed
    assert protection["stop_loss"]["visible"] is True
    assert protection["take_profit"]["visible"] is False
    assert protection["verified"] is False
    assert "open-order reads do not show them yet" in protection["message"]
    if take_profit is None:
        # The warm view counts the acknowledged take-profit (the registry), so a cancel resolves it;
        # the read-back never does.
        assert {o.order_id for o in client.warm_open_orders()} == {"sl", "tp"}
    assert entry.order_reads == []  # the verified feed decided within the bound; REST would have shown it
    assert len(entry.sent) == 3  # entry, stop-loss, take-profit: nothing retried
    await live.close()


@pytest.mark.parametrize("seen", ["feed_trigger", "acknowledged_trigger", "own_fill"])
async def test_protection_checks_before_the_entry_use_the_warm_view_and_never_weaken(
    client, monkeypatch, seen  # noqa: F811
):
    venue = held_venue(client)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)
    entry = protected_entry(client, monkeypatch)  # REST: a long 0.01 and no open orders
    await live.start(timeout=1)
    entry.plan_reads.clear()
    if seen == "feed_trigger":
        venue.socket.update(venue.channel("orderChanges"), [trigger_row("sl0", 11)])
        await settle(lambda: client.warm_published_orders(8009))
    elif seen == "acknowledged_trigger":
        # Armed by an earlier command and acknowledged; neither the feed nor REST shows it yet.
        live.on_ack(
            live.before_send(SYMBOL),
            order_id="sl0",
            is_buy=False,
            filled_qty=Decimal(0),
            price=Decimal("1980"),
            order_cum_qty=Decimal(0),
            fee_rate=Decimal("0.0004"),
            resting=make_order(order_id="sl0", order_type=OrderType.STOP_LOSS, side=Side.A),
        )
    else:
        # An earlier sell of 0.02 filled and was acknowledged; neither the feed nor REST includes it yet.
        live.on_ack(
            live.before_send(SYMBOL),
            order_id="s1",
            is_buy=False,
            filled_qty=Decimal("0.02"),
            price=Decimal("2000"),
            order_cum_qty=Decimal("0.02"),
            fee_rate=Decimal("0.0004"),
        )
    outcome = await runner.execute_request(client, {"command": PROTECTED})

    checks = [r["check"] for r in outcome["refused_by"]]
    assert ("protection_opposite_position" if seen == "own_fill" else "protection_exists") in checks
    assert entry.sent == []  # refused before sending
    assert entry.plan_reads == []  # from the warm view: REST would show a long and no trigger
    await live.close()


# ------------------------------------------------------------------ acknowledged orders across a resubscribe


def ack_stop_loss(live: LiveState, order_id: str) -> None:
    """A stop-loss an earlier command armed: acknowledged as resting, not shown by the feed."""
    live.on_ack(
        live.before_send(SYMBOL),
        order_id=order_id,
        is_buy=False,
        filled_qty=Decimal(0),
        price=Decimal("1980"),
        order_cum_qty=Decimal(0),
        fee_rate=Decimal("0.0004"),
        resting=make_order(order_id=order_id, order_type=OrderType.STOP_LOSS, side=Side.A),
    )


@pytest.mark.parametrize("later", [False, True], ids=["ended_while_disconnected", "acknowledged_after_the_snapshot"])
async def test_an_acknowledged_trigger_stops_counting_once_a_later_snapshot_and_rest_leave_it_out(
    client, monkeypatch, later  # noqa: F811
):
    venue = held_venue(client)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)
    entry = protected_entry(client, monkeypatch, on_entry=publish_evidence_on_entry(venue), venue=venue)
    await live.start(timeout=1)
    ack_stop_loss(live, "sl0")
    assert [o.order_id for o in client.warm_open_orders()] == ["sl0"]

    # The socket drops before the feed shows sl0, which is cancelled while disconnected: the new
    # snapshot (open orders only) and REST leave it out. One wallet snapshot is held back, so the
    # new connection has its orderChanges snapshot but is not verified yet.
    executions = venue.channel("perpExecutions")
    held_back = venue.snapshots.pop(executions)
    await venue.socket.close()
    await settle(lambda: len(venue.sockets) == 2 and live.order_snapshots == 2)
    if later:
        ack_stop_loss(live, "sl1")  # acknowledged after that snapshot: neither it nor REST shows it yet
        ended = {**trigger_row("sl9", 12), "status": "CANCELLED", "cancelReason": "POSITION_CLOSED"}
        venue.socket.update(venue.channel("orderChanges"), [ended])  # an unrelated update is not a snapshot
        await settle(lambda: any(r["orderId"] == "sl9" for r in live._wallet_feed("orderChanges").records.values()))
    venue.snapshots[executions] = held_back
    venue.socket.push({"type": "subscribed", "channel": executions, "contents": held_back})
    await settle(live.wallet_ready)  # REST, read after the snapshot, matched it

    assert [o.order_id for o in client.warm_open_orders()] == (["sl1"] if later else [])
    entry.plan_reads.clear()
    outcome = await runner.execute_request(client, {"command": PROTECTED})

    assert entry.plan_reads == []  # the checks used the warm view; REST shows no trigger either way
    if later:
        assert "protection_exists" in [r["check"] for r in outcome["refused_by"]]
        assert entry.sent == []
    else:
        assert "refused_by" not in outcome
        assert outcome["protection"]["state"] == "protected"
        assert trigger_types(entry.sent) == [OrderType.STOP_LOSS, OrderType.TAKE_PROFIT]
    await live.close()


# ------------------------------------------------------------------ slow fill indexing (devnet1, 19 October)

CLOSE = ["order", "market", SYMBOL, "--side", "short", "--qty", "0.01", "--slippage", "0.005", "--reduce_only"]


def filled(order_id: str, first_fill_id: str) -> CreateOrderResponse:
    return CreateOrderResponse(
        order_id=order_id, status="FILLED", cum_qty="0.01", first_fill_id=first_fill_id, fill_count=1
    )


def fee_tiers(client, monkeypatch):  # noqa: F811
    """Fee tiers for the fee bound an own fill is debited with; without them every unshown fill refuses."""
    tiers = AsyncMock(return_value=[SimpleNamespace(taker_fee="0.0004")])
    monkeypatch.setattr(client.reference._api, "get_fee_tier_parameters", tiers)


async def test_an_order_that_cannot_count_a_pending_fill_is_refused_unsent_and_a_close_still_goes(
    client, monkeypatch, capsys  # noqa: F811
):
    monkeypatch.setattr(runner, "FEE_SETTLE_WAIT_S", 0.05)
    venue = Venue(client.config.owner_wallet_address)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)
    fee_tiers(client, monkeypatch)
    responses = iter([filled("10", "77"), filled("11", "78")])

    async def send(**kwargs):
        if client.orders.create_order.await_count == 1:
            # The position moves during the entry's send, before its execution shows: it may or may not include
            # the fill. REST trails the feed, so it would leave the fill out as well.
            venue.socket.update(
                venue.channel("positions"), [make_position(qty="0.01", last_trade_sequence_number=7).to_dict()]
            )
            await settle(lambda: live._position_updates[SYMBOL] == 1)
        return next(responses)

    client.orders.create_order.side_effect = send
    result = await runner.serve(stream(MARKET, MARKET, CLOSE), client, wide(client))
    events = {e["id"]: e["result"] for e in outputs(capsys) if "id" in e}
    assert events["0"]["status"] == "FILLED"
    refused = events["1"]
    assert "does not show that fill yet" in refused["error"] and "no order submitted" in refused["error"]
    assert "session_continues" not in refused  # nothing was written: nothing to reconcile
    assert events["2"]["status"] == "FILLED"  # a reduce-only close is not blocked; the engine decides
    assert client.orders.create_order.await_count == 2
    assert result["reason"] == "eof"
    assert [f.debited for f in live.own_fills] == [True, True]  # refused for the ambiguity, not a missing fee bound


async def test_a_stop_right_after_fills_waits_for_the_feed_then_reconciles(client, monkeypatch, capsys):  # noqa: F811
    # Session 1 on devnet1: a stop 1 s after a fill read settlement before it was indexed and stayed unreconciled.
    monkeypatch.setattr(runner, "indexing_ceiling", lambda api_url: 5)
    monkeypatch.setattr(runner, "FEE_FEED_POLL_S", 0.005)
    venue = Venue(client.config.owner_wallet_address)
    warm(client, venue)
    unshadow_marks(client, monkeypatch)
    fee_tiers(client, monkeypatch)
    client.orders.create_order.side_effect = [filled("10", "77"), filled("11", "78")]
    fee = {"qty": "0.01", "price": "2000", "takerFee": "0.008", "type": "ORDER_MATCH", "timestamp": now_ms()}
    entry, close = fill(order="10", seq=7, **fee), fill(order="11", side="A", fill_id="78", seq=8, **fee)
    indexed = asyncio.Event()
    early_reads = []

    async def executions(**kwargs):
        if not indexed.is_set():
            early_reads.append(kwargs)
            return executions_page()
        return executions_page(entry, close)

    client.wallet.get_wallet_perp_executions.side_effect = executions

    async def index_after_stop():
        await settle(lambda: client.orders.cancel_all.await_count == 1, within=5)  # stop's cleanup ran
        await asyncio.sleep(0.3)
        indexed.set()
        venue.socket.update(venue.channel("perpExecutions"), [entry, close])
        venue.socket.update(
            venue.channel("positions"), [make_position(qty="0", last_trade_sequence_number=8).to_dict()]
        )

    indexer = asyncio.create_task(index_after_stop())
    started = time.monotonic()
    result = await runner.serve(stream(MARKET, CLOSE, ["stop"]), client, wide(client))
    await indexer
    events = outputs(capsys)
    assert result["settlement"]["state"] == "settled_flat", result["settlement"]
    assert result["reconciled"]
    assert result["unconfirmed_fills"] == [] and result["countdown_status"] == "disarmed"
    assert early_reads == []  # neither the fee nor the settlement reads ran before the feed showed the fills
    assert {e["fees_status"] for e in events if e.get("event") == "fees"} == {"complete"}
    assert time.monotonic() - started < 3  # woken by the feed, not by the 5 s budget


async def test_an_order_thirteen_seconds_after_a_fill_is_still_checked_with_it(client, monkeypatch):  # noqa: F811
    # Finding 3 of the rehearsal: with a 5 s bound, an order 5-13 s after a fill was checked on REST and feed
    # state that both lacked it. Here the feed shows the fill only after 13 s.
    clock, venue = Clock(), Venue(client.config.owner_wallet_address)
    live = warm(client, venue, clock=clock)
    unshadow_marks(client, monkeypatch)
    fee_tiers(client, monkeypatch)
    wide(client)
    assert (await client.warm_up())["wallet"] == "verified"
    client.orders.create_order.return_value = filled("10", "77")
    assert (await runner.execute_request(client, {"command": MARKET}))["status"] == "FILLED"
    clock.now += 13
    venue.socket.update(f"/v2/perpMarket/{SYMBOL}/summary", summary_row())
    await settle(lambda: live.mark_summary(SYMBOL) is not None)
    client.get_positions.reset_mock()
    client.get_account_balances.reset_mock()

    sell = ["order", "limit", SYMBOL, "--side", "short", "--qty", "0.01", "--price", "2100", "--post_only"]
    client.orders.create_order.return_value = CreateOrderResponse(order_id="11", status="OPEN", cum_qty="0")
    result = await runner.execute_request(client, {"command": sell})
    assert result["status"] == "OPEN"
    # The unshown long counts: the sell reduces it, and the account holds 0.02x leverage, not zero.
    assert result["reduces_position"] is True and result["current_leverage"] == 0.02
    client.get_positions.assert_not_awaited()
    client.get_account_balances.assert_not_awaited()
    await live.close()
