"""Own fills on slow indexing: each fill's lag in the journal, and REST proof while the feed cannot show it.

Offline: the WebSocket is test_runner_warm's fake venue, and REST is a stub or the runner fixture's mocks.
"""

from types import SimpleNamespace

import asyncio
from unittest.mock import AsyncMock

import pytest
from sdk.open_api.models.create_order_response import CreateOrderResponse

from agent_kit.scripts import _live, runner
from agent_kit.scripts._live import LiveState
from agent_kit.tests.conftest import make_position
from agent_kit.tests.test_runner import client, outputs, stream  # noqa: F401
from agent_kit.tests.test_runner_warm import (  # noqa: F401
    API,
    MARKET,
    SYMBOL,
    Clock,
    Venue,
    _quick_reconnect,
    ack_fill,
    executions_page,
    fee_tiers,
    filled,
    now_ms,
    settle,
    unshadow_marks,
    warm,
    wide,
)
from agent_kit.tests.test_settlement import fill

BLOCK_MS = 1_792_404_101_000  # a block timestamp: whole seconds
SELL = ["order", "limit", SYMBOL, "--side", "short", "--qty", "0.01", "--price", "2100", "--post_only"]


def settled(order="10", seq=7, timestamp=BLOCK_MS, **kw):
    """An execution row as the feed and REST carry it."""
    return fill(
        order=order, seq=seq, qty="0.01", price="2000", takerFee="0.008", type="ORDER_MATCH", timestamp=timestamp, **kw
    )


def proving_rest(*, positions=()):
    """REST reads for the checks and for REST proof: positions, balances, open orders and executions."""
    from agent_kit.tests.conftest import make_balance

    return SimpleNamespace(
        get_positions=AsyncMock(return_value=list(positions)),
        get_account_balances=AsyncMock(return_value=[make_balance(real_balance="1000")]),
        get_open_orders=AsyncMock(return_value=[]),
        wallet=SimpleNamespace(get_wallet_perp_executions=AsyncMock(return_value=executions_page())),
    )


async def hold_feed_down(venue: Venue, live: LiveState) -> None:
    venue.refuse = True
    await venue.socket.close()
    await settle(lambda: not live.connected)


@pytest.fixture(autouse=True)
def _quick_proof(monkeypatch):
    monkeypatch.setattr(_live, "REST_PROOF_RETRY_S", 0.01)
    monkeypatch.setattr(_live, "REST_PROOF_MAX_S", 0.04)


# ------------------------------------------------------------------ lag evidence


async def test_each_own_fill_reports_its_lag_when_the_feed_shows_it_or_the_ceiling_drops_it():
    clock, wall, venue = Clock(), Clock(), Venue("0x" + "ab" * 20)
    wall.now = BLOCK_MS / 1000 - 0.6  # acknowledged 0.6 s before the block's timestamp second
    lags: list = []
    live = LiveState(
        API, venue.wallet, 8009, {SYMBOL}, rest=proving_rest(), connect=venue.connect, clock=clock, wall_clock=wall
    )
    live.on_fill_lag = lags.append
    await live.start(timeout=1)
    ack_fill(live)
    clock.now += 1.4
    wall.now += 1.4
    venue.socket.update(venue.channel("perpExecutions"), [settled()])
    venue.socket.update(venue.channel("positions"), [make_position(qty="0.01", last_trade_sequence_number=7).to_dict()])
    await settle(lambda: lags)
    assert lags == [
        {
            "order_id": "10",
            "symbol": SYMBOL,
            "sequence": 7,
            "resolved_by": "feed",
            "ack_to_resolved_s": 1.4,
            "trade_timestamp_ms": BLOCK_MS,
            "venue_lag_s": 0.8,  # this computer's clock when the feed showed it, minus the block timestamp
            "ceiling_s": 15.0,
        }
    ]

    # An order whose execution shows but whose position never does: dropped at the ceiling, with what was seen.
    ack_fill(live, order_id="11")
    venue.socket.update(venue.channel("perpExecutions"), [settled(order="11", seq=8, fill_id="78")])
    await settle(lambda: live.own_fills[0].sequence == 8)
    clock.now += 15.5
    assert live.wallet_inputs() is None
    assert lags[1] == {
        "order_id": "11",
        "symbol": SYMBOL,
        "sequence": 8,
        "resolved_by": "ceiling",
        "ack_to_resolved_s": 15.5,
        "trade_timestamp_ms": BLOCK_MS,
        "venue_lag_s": None,
        "ceiling_s": 15.0,
    }
    await live.close()


async def test_the_journal_keeps_each_fills_lag_and_no_event_gains_a_field(client, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(runner, "FEE_FEED_POLL_S", 0.005)
    venue = Venue(client.config.owner_wallet_address)
    warm(client, venue)
    unshadow_marks(client, monkeypatch)
    fee_tiers(client, monkeypatch)
    client.orders.create_order.return_value = filled("10", "77")
    entry = settled(timestamp=now_ms() - 1000)
    client.wallet.get_wallet_perp_executions.return_value = executions_page(entry)

    async def index():
        await settle(lambda: client.orders.create_order.await_count == 1, within=5)
        venue.socket.update(venue.channel("perpExecutions"), [entry])
        venue.socket.update(
            venue.channel("positions"), [make_position(qty="0.01", last_trade_sequence_number=7).to_dict()]
        )

    indexer = asyncio.create_task(index())
    result = await runner.serve(stream(MARKET, ["stop"]), client, wide(client))
    await indexer
    events = outputs(capsys)
    lags = client.lease.state["runner"]["fill_lags"]
    assert [(lag["order_id"], lag["sequence"], lag["resolved_by"]) for lag in lags] == [("10", 7, "feed")]
    assert 0 <= lags[0]["ack_to_resolved_s"] < 5 and 0.9 < lags[0]["venue_lag_s"] < 6
    assert lags[0]["ceiling_s"] == 15.0
    assert any(e.get("event") == "fees" for e in events)
    assert not [key for e in [*events, result] for key in e if "lag" in key]  # journal only


# ------------------------------------------------------------------ REST proof while the feed cannot show a fill


async def test_a_pending_fill_resolves_from_rest_while_the_feed_is_held_down():
    venue, rest, lags = Venue("0x" + "ab" * 20), proving_rest(), list[dict]()
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest, connect=venue.connect)
    live.on_fill_lag = lags.append
    await live.start(timeout=1)
    ack_fill(live)
    await hold_feed_down(venue, live)
    with pytest.raises(_live.OwnFillPending):
        live.wallet_inputs()  # REST has not indexed it either: refuse, as before
    await settle(lambda: rest.wallet.get_wallet_perp_executions.await_count >= 2)  # REST keeps being asked

    # REST indexes it: the order's executions give sequence 7, and the position is at 7.
    rest.wallet.get_wallet_perp_executions.return_value = executions_page(settled(timestamp=now_ms()))
    rest.get_positions.return_value = [make_position(qty="0.01", last_trade_sequence_number=7)]
    await settle(lambda: not live.own_fills)
    assert live.wallet_inputs() is None  # REST decides, long before the 15 s ceiling, and it includes the fill
    assert not live.connected  # the feed never came back
    assert [(lag["resolved_by"], lag["sequence"]) for lag in lags] == [("rest", 7)]
    kwargs = rest.wallet.get_wallet_perp_executions.await_args.kwargs
    assert kwargs["address"] == venue.wallet
    assert now_ms() - 70_000 < kwargs["start_time"] < now_ms() - 55_000  # from just before the acknowledgement

    proving = live._proving
    assert proving is not None
    await settle(proving.done)  # nothing left to prove: the reads stop
    reads = rest.get_positions.await_count
    await asyncio.sleep(0.05)
    assert rest.get_positions.await_count == reads
    await live.close()


@pytest.mark.parametrize("closes", [False, True])
async def test_a_fill_the_feed_just_showed_needs_rest_proof_before_rest_decides(closes):
    # The feed leads REST by about 0.5-0.8 s: a fill the feed has just shown may not be in REST yet.
    clock, venue, rest = Clock(), Venue("0x" + "ab" * 20), proving_rest()
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest, connect=venue.connect, clock=clock)
    await live.start(timeout=1)
    if closes:
        # Long 0.01 at 6, then the runner's own sell closes it: no position is left for REST to show.
        venue.socket.update(
            venue.channel("positions"), [make_position(qty="0.01", last_trade_sequence_number=6).to_dict()]
        )
        await settle(lambda: live._position_sequence(SYMBOL) == 6)
    ack_fill(live, is_buy=not closes)
    venue.socket.update(venue.channel("perpExecutions"), [settled(side="A" if closes else "B")])
    shown = make_position(qty="0" if closes else "0.01", last_trade_sequence_number=7)
    venue.socket.update(venue.channel("positions"), [shown.to_dict()])
    await settle(lambda: not live.own_fills)
    await hold_feed_down(venue, live)
    with pytest.raises(_live.OwnFillPending):
        live.wallet_inputs()

    if closes:
        await settle(lambda: rest.get_positions.await_count >= 2)  # no row can prove a close: REST_TRAIL_S decides
        with pytest.raises(_live.OwnFillPending):
            live.wallet_inputs()
        clock.now += _live.REST_TRAIL_S + 0.1
    else:
        rest.get_positions.return_value = [shown]
        await settle(lambda: not live._trailing)
    assert live.wallet_inputs() is None
    rest.wallet.get_wallet_perp_executions.assert_not_awaited()  # the feed gave the sequence: positions suffice
    await live.close()


async def test_rest_proof_reads_only_and_ends_at_the_ceiling():
    clock, venue, rest, lags = Clock(), Venue("0x" + "ab" * 20), proving_rest(), list[dict]()
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest, connect=venue.connect, clock=clock)
    live.on_fill_lag = lags.append
    await live.start(timeout=1)
    ack_fill(live)
    await hold_feed_down(venue, live)
    await settle(lambda: rest.get_positions.await_count >= 3)  # REST never shows it (busted, say)
    clock.now += live.own_fill_max_s + 0.1
    proving = live._proving
    assert proving is not None
    await settle(proving.done)
    assert [lag["resolved_by"] for lag in lags] == ["ceiling"] and not live.own_fills
    assert live.wallet_inputs() is None  # REST decides, as before this change
    reads = rest.get_positions.await_count + rest.wallet.get_wallet_perp_executions.await_count
    await asyncio.sleep(0.1)
    assert rest.get_positions.await_count + rest.wallet.get_wallet_perp_executions.await_count == reads
    await live.close()


async def test_a_failing_rest_read_proves_nothing_and_the_fill_still_refuses():
    venue, rest = Venue("0x" + "ab" * 20), proving_rest()
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest, connect=venue.connect)
    await live.start(timeout=1)
    ack_fill(live)
    rest.get_positions.side_effect = TimeoutError()
    rest.wallet.get_wallet_perp_executions.return_value = executions_page(settled(timestamp=now_ms()))
    await hold_feed_down(venue, live)
    await settle(lambda: rest.get_positions.await_count >= 3)
    with pytest.raises(_live.OwnFillPending):
        live.wallet_inputs()
    rest.get_positions.side_effect = None
    rest.get_positions.return_value = [make_position(qty="0.01", last_trade_sequence_number=6)]  # before the fill
    await settle(lambda: rest.get_positions.await_count >= 6)
    with pytest.raises(_live.OwnFillPending):
        live.wallet_inputs()  # a position before the fill's sequence does not include it
    await live.close()


async def test_a_rest_proof_that_returns_after_the_feed_is_verified_again_changes_nothing(monkeypatch):
    venue, rest, lags, ready = Venue("0x" + "ab" * 20), proving_rest(), list[dict](), [False]
    live = LiveState(API, venue.wallet, 8009, {SYMBOL}, rest=rest, connect=venue.connect)
    live.on_fill_lag = lags.append
    monkeypatch.setattr(live, "wallet_ready", lambda: ready[0])

    async def positions():
        ready[0] = True  # the feed comes back verified while REST answers
        return [make_position(qty="0.01", last_trade_sequence_number=7)]

    rest.get_positions.side_effect = positions
    rest.wallet.get_wallet_perp_executions.return_value = executions_page(settled(timestamp=now_ms()))
    ack_fill(live)  # no verified feed: REST proof starts
    proving = live._proving
    assert proving is not None
    await settle(proving.done)
    assert rest.get_positions.await_count == 1
    # The verified feed decides again: the fill keeps counting until the feed shows it.
    assert [f.order_id for f in live.own_fills] == ["10"] and live.own_fills[0].sequence is None and lags == []
    await live.close()


async def test_runner_order_refused_on_a_pending_fill_goes_through_once_rest_proves_it(
    client, monkeypatch  # noqa: F811
):
    venue = Venue(client.config.owner_wallet_address)
    live = warm(client, venue)
    unshadow_marks(client, monkeypatch)
    fee_tiers(client, monkeypatch)
    wide(client)
    assert (await client.warm_up())["wallet"] == "verified"
    client.lease.state["runner"] = {}  # the session journal serve() opens
    client.orders.create_order.return_value = filled("10", "77")
    assert (await runner.execute_request(client, {"command": MARKET}))["status"] == "FILLED"
    await hold_feed_down(venue, live)

    client.orders.create_order.return_value = CreateOrderResponse(order_id="11", status="OPEN", cum_qty="0")
    with pytest.raises(_live.OwnFillPending, match="no order submitted"):  # serve() reports it as the error
        await runner.execute_request(client, {"command": SELL})
    assert client.orders.create_order.await_count == 1

    # The feed stays down; REST indexes the fill.
    client.wallet.get_wallet_perp_executions.return_value = executions_page(settled(timestamp=now_ms()))
    client.get_positions.return_value = [make_position(qty="0.01", last_trade_sequence_number=7)]
    await settle(lambda: not live.own_fills)
    result = await runner.execute_request(client, {"command": SELL})
    assert result["status"] == "OPEN"
    assert result["reduces_position"] is True  # checked on REST, which includes the long
    assert client.orders.create_order.await_count == 2  # each order sent once: the proof only reads
    assert [lag["resolved_by"] for lag in client.lease.state["runner"]["fill_lags"]] == ["rest"]
    await live.close()
