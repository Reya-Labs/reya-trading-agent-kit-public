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

from agent_kit.scripts import _live, runner
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


async def test_own_fill_counts_until_the_feed_includes_it_and_ambiguity_reads_rest():
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
    assert live.wallet_inputs() is None  # ambiguous: the caller reads REST
    venue.socket.update(venue.channel("perpExecutions"), [fill(order="11", seq=9, takerAccountId=8009)])
    await settle(lambda: live.own_fills and live.own_fills[0].sequence == 9)
    _, positions = inputs(live)  # sequence known and not yet applied: the fill counts again
    assert [p.qty for p in positions] == ["0.03"]
    await live.close()


async def test_an_own_fill_the_feed_never_shows_makes_the_wallet_suspect(monkeypatch):
    clock, venue = Clock(), Venue("0x" + "ab" * 20)
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest_stub(), connect=venue.connect, clock=clock)
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
    clock.now += _live.OWN_FILL_RESOLVE_S + 1
    assert live.wallet_inputs() is None
    await settle(lambda: len(venue.sockets) >= 2)
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
