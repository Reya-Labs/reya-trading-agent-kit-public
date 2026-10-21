"""Snapshot/live and recovery invariants, with no network access."""

import asyncio

import pytest

from agent_kit.scripts._stream import FeedState
from agent_kit.scripts.stream import _next

WALLET = "0x" + "12" * 20


def state(kind, **kwargs):
    return FeedState("https://example.invalid/v2", f"/v2/wallet/{WALLET}/{kind}", **kwargs)


def update(view, data, timestamp=100):
    return {"type": "channel_data", "channel": view.channel, "timestamp": timestamp, "data": data}


def test_order_snapshot_cursor_excludes_overlap_and_joins_market_ids():
    view = state("orderChanges")
    order = {"accountId": 7, "symbol": "ETHRUSDPERP", "orderId": "1", "status": "OPEN"}
    view.snapshot({"data": [order], "snapshotSequenceNumber": 50}, now=0)
    view.update(update(view, [{**order, "sequenceNumber": 49, "status": "CANCELLED"}]), now=1)
    assert view.view(now=1)["data"][0]["status"] == "OPEN"
    view.update(
        update(
            view,
            [
                {**order, "sequenceNumber": 51, "status": "FILLED"},
                {**order, "symbol": "BTCRUSDPERP", "sequenceNumber": 52},
            ],
        ),
        now=2,
    )
    assert len(view.view(now=2)["data"]) == 2
    view.update(update(view, [{**order, "sequenceNumber": 50}]), now=3)
    assert view.view(now=3)["data"][0]["status"] == "FILLED"


def test_history_snapshot_live_overlap_deduplicates_transfer_account_sides():
    view = state("transfers")
    debit = {"sequenceNumber": 5, "accountId": 7, "amount": "-1"}
    credit = {"sequenceNumber": 99, "accountId": 8, "amount": "1"}
    view.snapshot({"data": [debit]}, now=0)
    view.backfill({"data": [debit, credit], "coverage": {"state": "bounded"}})
    view.update(update(view, [credit]), now=1)
    assert len(view.view(now=1)["data"]) == 2
    assert view.view(now=1)["history_coverage"]["state"] == "bounded"


def test_old_position_does_not_overwrite_new_and_zero_removes_position():
    view = state("positions")
    row = {
        "accountId": 7,
        "exchangeId": 2,
        "symbol": "ETHRUSDPERP",
        "qty": "2",
        "side": "B",
        "avgEntryPrice": "100",
        "lastTradeSequenceNumber": 20,
    }
    view.snapshot([row], now=0)
    view.update(update(view, [{**row, "lastTradeSequenceNumber": 19, "qty": "5"}]), now=1)
    assert view.view(now=1)["data"][0]["qty"] == "2"
    view.update(update(view, [{**row, "qty": "1.5"}], timestamp=101), now=2)  # ADL update, same trade sequence
    assert view.view(now=2)["data"][0]["qty"] == "1.5"
    view.update(update(view, [{**row, "qty": "0", "lastTradeSequenceNumber": 21}], timestamp=102), now=3)
    assert view.view(now=3)["data"] == []


def test_balance_updates_merge_instead_of_replacing_other_assets():
    view = state("accountBalances")
    row = {"accountId": 7, "asset": "RUSD", "realBalance": "100"}
    other = {**row, "asset": "ETH", "realBalance": "2"}
    view.snapshot({"data": [row, other]}, now=0)
    view.update(update(view, [{**row, "realBalance": "101"}]), now=1)
    view.update(update(view, [{**row, "realBalance": "99"}], timestamp=99), now=2)
    assert {r["asset"]: r["realBalance"] for r in view.view(now=2)["data"]} == {"RUSD": "101", "ETH": "2"}


def book_view():
    view = FeedState("https://example.invalid/v2", "/v2/market/ETHRUSDPERP/depth")
    view.snapshot(
        {
            "symbol": "ETHRUSDPERP",
            "type": "SNAPSHOT",
            "updatedAt": 1,
            "bids": [{"px": "100", "qty": "2"}],
            "asks": [{"px": "101", "qty": "3"}],
        },
        now=0,
    )
    return view


def test_depth_absolute_updates_zero_deletion_and_quiet_staleness():
    view = book_view()
    row = {
        "symbol": "ETHRUSDPERP",
        "type": "UPDATE",
        "updatedAt": 2,
        "bids": [{"px": "100.0", "qty": "0"}, {"px": "99", "qty": "4"}],
        "asks": [],
    }
    view.update(update(view, row), now=1)
    view.update(update(view, row), now=2)  # repeated absolute diff is idempotent
    assert view.view(now=2)["data"]["bids"] == [{"px": "99", "qty": "4"}]
    assert view.view(now=40)["state"] == "stale"
    assert view.view(now=40)["safe_for_order_admission"] is False


def test_out_of_order_depth_requires_resnapshot():
    view = book_view()
    row = {"symbol": "ETHRUSDPERP", "type": "UPDATE", "updatedAt": 2, "bids": [{"px": "100", "qty": "1"}], "asks": []}
    view.update(update(view, row, 100))
    with pytest.raises(ValueError, match="Out-of-order"):
        view.update(update(view, row, 99))


def test_wallet_switch_and_reset_cannot_reuse_previous_state():
    view = state("transfers")
    view.snapshot({"data": [{"sequenceNumber": 1, "amount": "1"}]}, now=0)
    with pytest.raises(ValueError, match="different"):
        view.update({**update(view, []), "channel": view.channel.replace(WALLET, "0x" + "34" * 20)})
    view.reset()
    assert view.view()["data"] == []
    assert view.view()["state"] == "recovering"
    with pytest.raises(ValueError, match="snapshot"):
        view.update(update(view, []))


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "1e99999", "not-a-number", 0.1])
def test_malformed_decimal_never_makes_ready_state(amount):
    view = state("accountBalances")
    with pytest.raises(ValueError):
        view.snapshot({"data": [{"accountId": 7, "asset": "RUSD", "realBalance": amount}]})
    assert view.phase == "recovering"


def test_buffer_bound_and_conflicting_immutable_rows_fail():
    view = state("transfers", capacity=1)
    view.snapshot({"data": [{"sequenceNumber": 1, "amount": "1"}]})
    with pytest.raises(ValueError, match="Conflicting"):
        view.update(update(view, [{"sequenceNumber": 1, "amount": "2"}]))
    with pytest.raises(ValueError, match="capacity"):
        view.update(update(view, [{"sequenceNumber": 2, "amount": "2"}]))


def test_incomplete_backfill_is_visible():
    view = state("transfers")
    view.snapshot({"data": []}, now=0)
    view.backfill({"data": [], "coverage": {"state": "incomplete", "reason": "page_budget"}})
    assert view.view(now=1)["state"] == "incomplete"


def test_quiet_incomplete_feed_stays_incomplete_and_flags_staleness_separately():
    # Devnet1 phase-B transfers feed reported "stale" while its coverage was incomplete.
    view = state("transfers")
    view.snapshot({"data": []}, now=0)
    view.backfill({"data": [], "coverage": {"state": "incomplete", "reason": "page_budget"}})
    quiet = view.view(now=40)
    assert quiet["state"] == "incomplete" and quiet["stale"] is True
    complete = state("transfers")
    complete.snapshot({"data": []}, now=0)
    assert complete.view(now=40)["state"] == "stale" and complete.view(now=1)["stale"] is False


async def test_reader_failure_wakes_waiting_consumer():
    async def fail():
        raise ConnectionError("disconnected")

    reader = asyncio.create_task(fail())
    with pytest.raises(ConnectionError):
        await _next(asyncio.Queue(), reader, 1)


async def test_reader_queue_timeout_does_not_leak_getter():
    reader = asyncio.create_task(asyncio.sleep(10))
    queue: asyncio.Queue = asyncio.Queue()
    try:
        with pytest.raises(TimeoutError):
            await _next(queue, reader, 0.001)
        queue.put_nowait("next")
        await asyncio.sleep(0)
        assert queue.get_nowait() == "next"
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def test_observer_reconnects_and_resnapshots_after_server_error(monkeypatch):
    from types import SimpleNamespace

    import json

    import aiohttp

    from agent_kit.scripts import stream

    channel = f"/v2/wallet/{WALLET}/transfers"
    outputs = []
    connections: list = []
    history_calls = []

    class Finished(Exception):
        pass

    class Socket:
        def __init__(self, fail):
            self.fail = fail
            self.sent = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_json(self, data):
            self.sent.append(data)

        async def __aiter__(self):
            message = (
                {"type": "error", "channel": channel}
                if self.fail
                else {
                    "type": "subscribed",
                    "channel": channel,
                    "contents": {"data": [{"sequenceNumber": 7, "amount": "1"}]},
                }
            )
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(message))
            await asyncio.Event().wait()

    class Rest:
        def __init__(self, api_url):
            self.session = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def ws_connect(self, *args, **kwargs):
            socket = Socket(fail=not connections)
            connections.append(socket)
            return socket

        async def history(self, path, **kwargs):
            history_calls.append(path)
            return {"data": [{"sequenceNumber": 7, "amount": "1"}], "coverage": {"state": "bounded"}}

    def emit(result):
        outputs.append(result)
        if result["state"] == "ready":
            raise Finished()

    monkeypatch.setattr(stream, "V2Client", Rest)
    with pytest.raises(Finished):
        await stream.observe("https://api.reya.xyz/v2", channel, seconds=5, emit_fn=emit)
    assert len(connections) == 2
    assert all(socket.sent == [{"type": "subscribe", "channel": channel}] for socket in connections)
    assert [row["state"] for row in outputs] == ["recovering", "incomplete", "recovering", "ready"]
    assert len(outputs[-1]["data"]) == 1
    assert history_calls == [channel.removeprefix("/v2")]


def test_order_resubscription_keeps_and_recovers_terminal_orders_missed_while_disconnected():
    # Devnet1: after a reconnect orderChanges showed 0 rows and "ready", missing a close fill.
    view = state("orderChanges")
    first = {"accountId": 7, "symbol": "ETHRUSDPERP", "orderId": "1", "status": "OPEN"}
    view.snapshot({"data": [first], "snapshotSequenceNumber": 10}, now=0)
    view.update(update(view, [{**first, "sequenceNumber": 11, "status": "FILLED"}]), now=1)
    missed = {"accountId": 7, "symbol": "ETHRUSDPERP", "orderId": "2", "status": "FILLED", "sequenceNumber": 15}
    older = {"accountId": 7, "symbol": "ETHRUSDPERP", "orderId": "0", "status": "FILLED", "sequenceNumber": 5}
    resting = {"accountId": 7, "symbol": "ETHRUSDPERP", "orderId": "3", "status": "OPEN"}
    view.snapshot({"data": [resting], "snapshotSequenceNumber": 20}, now=2)
    view.backfill({"data": [missed, older, {**resting, "sequenceNumber": 18}], "coverage": {"state": "bounded"}})
    result = view.view(now=2)
    assert {r["orderId"]: r["status"] for r in result["data"]} == {"1": "FILLED", "2": "FILLED", "3": "OPEN"}
    assert result["state"] == "ready" and "order_scope" in result
    fresh = state("orderChanges")
    fresh.snapshot({"data": [resting], "snapshotSequenceNumber": 20}, now=0)
    fresh.backfill({"data": [missed, older], "coverage": {"state": "bounded"}})
    assert [r["orderId"] for r in fresh.view(now=0)["data"]] == ["3"]


def _scripted_observer(monkeypatch, channel, plan, history_rows):
    """Run observe() over scripted sockets: each plan entry is (snapshot contents, drop after snapshot)."""
    from types import SimpleNamespace

    import json

    import aiohttp

    from agent_kit.scripts import stream

    connections: list = []

    class Socket:
        def __init__(self, contents, drop):
            self.contents, self.drop = contents, drop

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_json(self, data):
            pass

        async def __aiter__(self):
            message = {"type": "subscribed", "channel": channel, "contents": self.contents}
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(message))
            if self.drop:
                await asyncio.sleep(0.01)
                return  # the kit's reader treats the end of iteration as a dropped socket
            await asyncio.Event().wait()

    class Rest:
        def __init__(self, api_url):
            self.session = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def ws_connect(self, *args, **kwargs):
            contents, drop = plan[min(len(connections), len(plan) - 1)]
            connections.append(1)
            return Socket(contents, drop)

        async def history(self, path, **kwargs):
            return {"data": history_rows(len(connections)), "coverage": {"state": "bounded"}}

    monkeypatch.setattr(stream, "V2Client", Rest)
    monkeypatch.setattr(stream.asyncio, "sleep", _fast_sleep)
    return stream, connections


_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *args, **kwargs):
    await _real_sleep(min(delay, 0.01))


async def test_observe_recovers_orders_filled_while_disconnected(monkeypatch):
    # Devnet1 12 Oct: after a live socket drop, orders filled while disconnected never reappeared.
    channel = f"/v2/wallet/{WALLET}/orderChanges"
    missed = {"accountId": 7, "symbol": "ETHRUSDPERP", "orderId": "2", "status": "FILLED", "sequenceNumber": 15}
    plan = [({"data": [], "snapshotSequenceNumber": 10}, True), ({"data": [], "snapshotSequenceNumber": 20}, False)]
    stream, connections = _scripted_observer(monkeypatch, channel, plan, lambda n: [missed] if n > 1 else [])
    outputs = []

    class Finished(Exception):
        pass

    def emit(view):
        outputs.append(view)
        if view["state"] == "ready" and len(connections) == 2:
            raise Finished()

    with pytest.raises(Finished):
        await stream.observe("https://api.reya.xyz/v2", channel, seconds=10, emit_fn=emit)
    assert [o["state"] for o in outputs] == ["recovering", "ready", "incomplete", "recovering", "ready"]
    assert [r["orderId"] for r in outputs[-1]["data"]] == ["2"]


async def test_observe_reconnect_budget_is_per_outage(monkeypatch):
    channel = f"/v2/wallet/{WALLET}/transfers"
    healthy_then_drop = ({"data": [{"sequenceNumber": 7, "amount": "1"}]}, True)
    stream, connections = _scripted_observer(monkeypatch, channel, [healthy_then_drop], lambda n: [])
    outputs = []

    class Finished(Exception):
        pass

    def emit(view):
        outputs.append(view)
        if len(connections) == 6 and view["state"] == "ready":
            raise Finished()

    # Five separate drops, each recovered, never exhaust a three-attempt budget.
    with pytest.raises(Finished):
        await stream.observe("https://api.reya.xyz/v2", channel, seconds=10, reconnects=3, emit_fn=emit)
    assert max(o.get("reconnect_attempt", 0) for o in outputs) == 0


def test_explicit_reset_forgets_order_continuity():
    view = state("orderChanges")
    view.snapshot({"data": [], "snapshotSequenceNumber": 10}, now=0)
    view.backfill({"data": [], "coverage": {"state": "bounded"}})
    view.reset()
    view.snapshot({"data": [], "snapshotSequenceNumber": 20}, now=1)
    missed = {"accountId": 7, "symbol": "ETHRUSDPERP", "orderId": "2", "status": "FILLED", "sequenceNumber": 15}
    view.backfill({"data": [missed], "coverage": {"state": "bounded"}})
    assert view.view(now=1)["data"] == []
