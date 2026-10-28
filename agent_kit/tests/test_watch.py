"""`watch trigger`: wait on the WebSocket mark (or funding), then run at most two agreed kit commands once.

The trigger rules are pure (`_watch.Trigger`); the feed loop reads a fake socket; actions run through the
trade CLI in-process, patched here so nothing reaches a venue.
"""

from types import SimpleNamespace

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal

import aiohttp
import pytest
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.side import Side

from agent_kit.scripts import _watch, watch
from agent_kit.tests.conftest import FakeClient, make_order, make_position

SYMBOL = "ETHRUSDPERP"
CHANNEL = f"/v2/perpMarket/{SYMBOL}/summary"


def push(t: float, mark: str, *, pu: int | None = None, funding: str | None = None) -> _watch.Push:
    """A push delivered on time (its row computed as it arrives), with prices last updated at `pu`."""
    return _watch.Push(
        receipt=t,
        prices_updated_at=int(t * 1000) if pu is None else pu,
        mark=Decimal(mark),
        funding=None if funding is None else Decimal(funding),
        updated_at=int(t * 1000),
    )


def below(price: str) -> _watch.Trigger:
    return _watch.Trigger(_watch.Level("lte", Decimal(price)), None)


def feed(trigger: _watch.Trigger, pushes) -> list:
    return [(d.kind, d.reason) for d in (trigger.on_push(p) for p in pushes) if d is not None]


# --------------------------------------------------------------------------
# The crossing rule
# --------------------------------------------------------------------------


def test_a_fresh_crossing_fires_and_touching_counts():
    trigger = below("2492")
    assert feed(trigger, [push(0, "2500"), push(0.5, "2495")]) == [("watching", None)]
    decision = trigger.on_push(push(1.0, "2492"))
    assert decision is not None and decision.kind == "fire" and decision.detail["mark"] == "2492"


def test_gte_fires_upward_and_a_crossing_before_the_pace_is_known_is_held_not_lost():
    trigger = _watch.Trigger(_watch.Level("gte", Decimal("2600")), None)
    # 0.5 s after the start the pace cannot be judged yet: the crossing waits, compared with the first mark.
    assert feed(trigger, [push(0, "2500"), push(0.5, "2601")]) == [("watching", None)]
    assert feed(trigger, [push(1.0, "2602")]) == [("fire", None)]


def test_a_level_already_reached_at_the_start_is_refused_never_fired():
    assert feed(below("2492"), [push(0, "2480")]) == [("halt", "already_triggered")]


def test_a_crossing_hidden_in_a_gap_halts_instead_of_firing():
    # 5 s without a push: the move through the level was never seen fresh.
    trigger = below("2492")
    assert feed(trigger, [push(0, "2500"), push(5, "2480"), push(5.5, "2479")]) == [
        ("watching", None),
        ("stale", "push_gap"),
        ("halt", "crossed_while_stale"),
    ]


def test_after_a_gap_without_a_crossing_it_recovers_and_fires_later():
    trigger = below("2492")
    kinds = feed(trigger, [push(0, "2500"), push(5, "2499"), push(5.5, "2498"), push(6, "2490")])
    assert kinds == [("watching", None), ("stale", "push_gap"), ("recovered", None), ("fire", None)]


def test_prices_that_stop_updating_are_stale():
    trigger = below("2492")
    stuck = [push(0.5 * i, "2500", pu=1000) for i in range(1, 23)]  # pushes keep coming, prices frozen 11 s
    assert ("stale", "prices_not_updating") in feed(trigger, [push(0, "2500", pu=0), *stuck])


def test_silence_goes_stale_then_ends_the_watch():
    trigger = below("2492")
    trigger.on_push(push(0, "2500"))
    assert trigger.on_idle(2.0) is None
    stale = trigger.on_idle(3.5)
    assert stale is not None and stale.kind == "stale"
    halted = trigger.on_idle(61)
    assert halted is not None and (halted.kind, halted.reason) == ("halt", "feed_stale")


def test_a_server_side_jump_in_prices_updated_at_is_a_gap():
    # Pushes arrived on time here, but the server skipped 5 s of prices (for example after a resubscribe).
    trigger = below("2492")
    assert feed(trigger, [push(0, "2500", pu=0), push(0.5, "2480", pu=5500)])[-1] == ("stale", "push_gap")


# --------------------------------------------------------------------------
# Funding
# --------------------------------------------------------------------------


def test_funding_is_a_condition_at_the_crossing_and_needs_a_full_window():
    rule = _watch.FundingRule("gt", Decimal("0.001"), window_s=3.0)
    trigger = _watch.Trigger(_watch.Level("lte", Decimal("2492")), rule)
    marks = ["2500", "2500", "2500", "2490", "2500", "2500", "2500", "2491"]
    # 1.5 s: crosses before the 3 s window is full; 3.5 s: crosses again, window full, average 0.002 > 0.001.
    kinds = feed(trigger, [push(0.5 * i, m, funding="0.002") for i, m in enumerate(marks)])
    assert kinds == [("watching", None), ("skip", "funding_window_not_full"), ("fire", None)]


def test_funding_detail_keeps_the_exact_figures_and_adds_text_to_quote():
    rule = _watch.FundingRule("gt", Decimal("0.001"), window_s=1.0)
    trigger = _watch.Trigger(_watch.Level("lte", Decimal("2492")), rule)
    for i, rate in enumerate(["0.0012", "0.0012", "0.0013", "0.0013"]):
        trigger.on_push(push(0.5 * i, "2500", funding=rate))
    decision = trigger.on_push(push(2.0, "2490", funding="-0.000082033333333333"))
    # The last second's average (this push's rate and the two before it) is below the level: a skip, with the detail.
    assert decision is not None and (decision.kind, decision.reason) == ("skip", "funding_condition_not_met")
    detail = decision.detail
    assert detail["funding_pct_per_hour"] == "-0.000082033333333333"
    assert detail["funding_text"] == "-0.0001%/h (-0.72% a year)"
    # The exact average runs to Decimal's 28 digits; the text rounds it.
    average = (Decimal("0.0013") * 2 + Decimal("-0.000082033333333333")) / 3
    assert detail["funding_average_pct_per_hour"] == str(average)
    assert detail["funding_average_text"] == "0.0008%/h (7.35% a year)"


def test_funding_condition_not_met_skips_the_crossing():
    rule = _watch.FundingRule("lt", Decimal("0"), window_s=1.0)
    trigger = _watch.Trigger(_watch.Level("lte", Decimal("2492")), rule)
    pushes = [push(0.5 * i, "2500", funding="0.001") for i in range(4)] + [push(2.0, "2490", funding="0.001")]
    assert feed(trigger, pushes)[-1] == ("skip", "funding_condition_not_met")


def test_funding_alone_fires_when_the_average_moves_to_meet_it():
    rule = _watch.FundingRule("gt", Decimal("0.001"), window_s=1.0)
    trigger = _watch.Trigger(None, rule)
    pushes = [push(0.5 * i, "2500", funding="0") for i in range(4)]  # full window, not met
    pushes += [push(2.0 + 0.5 * i, "2500", funding="0.01") for i in range(4)]
    kinds = feed(trigger, pushes)
    assert kinds[:2] == [("watching", None), ("watching", "funding_window_full")]
    assert kinds[-1] == ("fire", None)


def test_funding_already_met_when_the_window_fills_is_refused():
    rule = _watch.FundingRule("gt", Decimal("0.001"), window_s=1.0)
    pushes = [push(0.5 * i, "2500", funding="0.01") for i in range(4)]
    assert feed(_watch.Trigger(None, rule), pushes)[-1] == ("halt", "already_triggered")


@pytest.mark.parametrize("text", ["lt:2492", "lte:", "gte:abc", "lte:-1", "2492"])
def test_bad_levels_are_refused(text):
    with pytest.raises((ValueError, ArithmeticError)):
        _watch.parse_level(text)


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def test_protected_entries_and_symbol_closes_are_accepted():
    actions = watch.parse_actions(
        [
            "trade order market eth --side short --notional 30 --sl 2% --tp 3%",
            "positions close_all --symbol sol",
        ],
        unprotected_ok=False,
    )
    assert [(a.group, a.action) for _, a in actions] == [("order", "market"), ("positions", "close_all")]
    assert actions[0][0][0] == "order"  # a leading "trade" is dropped


@pytest.mark.parametrize(
    "text, reason",
    [
        ("order market eth --side long --notional 30", "unprotected_entry"),
        ("order limit eth --side long --price 2400 --qty 0.01", "unprotected_entry"),
        ("order market eth --side long --notional 30 --sl 2% --force", "invalid_action"),
        ("order market eth --side long --notional 30 --sl 2% --allow_large", "invalid_action"),
        ("positions close_all --all", "invalid_action"),
        ("order cancel_all --symbol eth", "invalid_action"),
        ("order sl eth --side long --trigger_px 1 --limit_px 1 --time_in_force IOC", "invalid_action"),
        ("order market eth --side long --notional 30 --sl 2% --preview", "invalid_action"),
        ("order market", "invalid_action"),
    ],
)
def test_actions_that_could_hurt_are_refused(text, reason):
    with pytest.raises(watch.Refused) as refused:
        watch.parse_actions([text], unprotected_ok=False)
    assert refused.value.reason == reason


def test_unprotected_entries_need_the_explicit_flag_and_closes_never_do():
    watch.parse_actions(["order market eth --side long --notional 30"], unprotected_ok=True)
    watch.parse_actions(["order limit eth --side short --price 2600 --qty 0.01"], unprotected_ok=True)
    watch.parse_actions(["order market eth --side short --qty 0.01 --reduce_only"], unprotected_ok=False)


def test_at_most_two_actions():
    one = "positions close_all --symbol eth"
    with pytest.raises(watch.Refused, match="At most 2"):
        watch.parse_actions([one, one, one], unprotected_ok=False)


async def test_the_backstop_check_requires_a_stop_on_open_positions(patch_client: FakeClient):
    actions = watch.parse_actions(["positions close_all --symbol eth"], unprotected_ok=False)
    patch_client.get_positions.return_value = [make_position(qty="0.05")]  # a long
    with pytest.raises(watch.Refused) as refused:
        await watch.backstop_check(actions, unprotected_ok=False)
    assert refused.value.reason == "no_backstop"

    patch_client.get_open_orders.return_value = [make_order(order_type=OrderType.STOP_LOSS, side=Side.A)]
    assert await watch.backstop_check(actions, unprotected_ok=False) == [
        {"symbol": SYMBOL, "side": "long", "qty": "0.05", "stop_loss": "armed"}
    ]


# --------------------------------------------------------------------------
# The feed loop and a whole run
# --------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, snapshot: dict, answer: str = "subscribed"):
        self.snapshot = snapshot
        self.answer = answer  # "subscribed", "error" or "silent"
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list = []

    async def send_json(self, data):
        self.sent.append(data)
        if data.get("type") == "subscribe" and self.answer == "subscribed":
            self.push({"type": "subscribed", "channel": data["channel"], "contents": self.snapshot})
        elif data.get("type") == "subscribe" and self.answer == "error":
            self.push({"type": "error", "message": "Unknown channel"})

    def push(self, data):
        self.queue.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(data)))

    def update(self, row: dict):
        self.push({"type": "channel_data", "channel": CHANNEL, "data": row, "timestamp": row["updatedAt"]})

    def close(self):
        self.queue.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()


class Venue:
    def __init__(self, mark: str = "2500"):
        self.sockets: list[FakeSocket] = []
        self.clock = 1000.0
        self.mark = mark
        self.refuse = 0
        self.answer = "subscribed"

    def row(self, mark: str | None = None, funding: str = "0.001") -> dict:
        return {
            "symbol": SYMBOL,
            "updatedAt": int(self.clock * 1000),  # stamped as it is computed, delivered at once
            "markPrice": mark or self.mark,
            "oraclePrice": mark or self.mark,
            "fundingRate": funding,
            "pricesUpdatedAt": int(self.clock * 1000),
        }

    def now(self) -> float:
        return self.clock

    @asynccontextmanager
    async def connect(self):
        if self.refuse:
            self.refuse -= 1
            raise ConnectionError("refused")
        socket = FakeSocket(self.row(), self.answer)
        self.sockets.append(socket)
        yield socket

    async def tick(self, mark: str):
        await asyncio.sleep(0.01)
        self.clock += 0.5
        self.sockets[-1].update(self.row(mark))


async def until(condition, within=2.0):
    for _ in range(int(within / 0.005)):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never held")


async def test_the_feed_fires_on_the_pushed_crossing():
    venue, events, stop = Venue(), list[dict](), asyncio.Event()
    task = asyncio.create_task(
        watch.watch_feed(
            watch.get_api_url(),
            SYMBOL,
            below("2492"),
            deadline=venue.clock + 60,
            stop=stop,
            on_event=events.append,
            connect=venue.connect,
            clock=venue.now,
        )
    )
    await until(lambda: venue.sockets and events)
    assert events[0]["event"] == "watching" and venue.sockets[0].sent[0]["channel"] == CHANNEL
    await venue.tick("2495")
    await venue.tick("2491")
    decision = await asyncio.wait_for(task, 2)
    assert decision.kind == "fire" and decision.detail["mark"] == "2491"


async def test_the_feed_reconnects_and_stops_on_request(monkeypatch):
    venue, events, stop = Venue(), list[dict](), asyncio.Event()
    venue.refuse = 1
    monkeypatch.setattr(watch, "RECONNECT_BASE_S", 0.01)
    task = asyncio.create_task(
        watch.watch_feed(
            watch.get_api_url(),
            SYMBOL,
            below("2492"),
            deadline=venue.clock + 60,
            stop=stop,
            on_event=events.append,
            connect=venue.connect,
            clock=venue.now,
        )
    )
    await until(lambda: any(e["event"] == "watching" for e in events))
    assert events[0]["event"] == "reconnecting"
    stop.set()
    decision = await asyncio.wait_for(task, 3)
    assert (decision.kind, decision.reason) == ("halt", "stopped")


def _trigger_args(**overrides) -> argparse.Namespace:
    values = dict(
        symbol=SYMBOL,
        mark="lte:2492",
        funding=None,
        funding_window=15,
        then=None,
        unprotected_ok=False,
        max_hours=1.0,
        detach=False,
        id="eth-test",
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _lines(capsys) -> list:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


async def _run(venue: Venue, args, capsys) -> tuple[int, list]:
    task = asyncio.create_task(watch.run_trigger(args, connect=venue.connect, clock=venue.now))
    await until(lambda: venue.sockets)
    await asyncio.sleep(0.05)
    await venue.tick("2495")
    await venue.tick("2490")
    code = await asyncio.wait_for(task, 3)
    return code, _lines(capsys)


async def test_an_alert_only_watch_reports_the_crossing_and_exits_0(capsys):
    code, lines = await _run(Venue(), _trigger_args(), capsys)
    assert code == 0
    assert [line["event"] for line in lines] == ["arming", "watching", "fired"]
    assert lines[0]["alert_only"] is True and lines[-1]["actions"] == []
    assert lines[-1]["trigger"]["mark"] == "2490"


async def test_a_pair_fires_in_order_and_stops_at_the_first_failure(monkeypatch, capsys):
    calls: list = []

    async def fake_run_action(argv, *, preview=False):
        calls.append((" ".join(argv), preview))
        if not preview and argv[2] == "eth":
            return {"error": "IOC did not fill completely", "filled_qty": "0"}
        return {"would_send": True} if preview else {"status": "FILLED"}

    async def no_backstop(actions, *, unprotected_ok):
        return []

    monkeypatch.setattr(watch, "run_action", fake_run_action)
    monkeypatch.setattr(watch, "backstop_check", no_backstop)
    args = _trigger_args(
        then=[
            "order market eth --side short --notional 30 --sl 2%",
            "order market sol --side long --notional 30 --sl 2%",
        ]
    )
    code, lines = await _run(Venue(), args, capsys)

    assert code == 1
    assert [c for c in calls if not c[1]] == [("order market eth --side short --notional 30 --sl 2%", False)]
    fired = lines[-1]
    assert fired["event"] == "fired" and fired["legs_atomic"] is False
    assert "1 later command(s) not sent" in fired["error"]
    assert [line["event"] for line in lines[:3]] == ["arming", "armed", "watching"]


async def test_an_entry_without_a_stop_is_refused_before_any_read(patch_client: FakeClient, capsys):
    venue = Venue()
    code = await watch.run_trigger(
        _trigger_args(then=["order market eth --side long --notional 30"]), connect=venue.connect, clock=venue.now
    )
    lines = _lines(capsys)
    assert code == 1 and lines[-1]["event"] == "refused" and lines[-1]["reason"] == "unprotected_entry"
    patch_client.get_positions.assert_not_awaited()
    assert venue.sockets == []


async def test_a_preview_the_venue_would_refuse_stops_the_arming(monkeypatch, capsys):
    async def refused_preview(argv, *, preview=False):
        return {"would_send": False, "refused_by": [{"check": "engine_margin"}]}

    async def no_backstop(actions, *, unprotected_ok):
        return []

    monkeypatch.setattr(watch, "run_action", refused_preview)
    monkeypatch.setattr(watch, "backstop_check", no_backstop)
    venue = Venue()
    code = await watch.run_trigger(
        _trigger_args(then=["order market eth --side long --notional 30 --sl 2%"]),
        connect=venue.connect,
        clock=venue.now,
    )
    assert code == 1 and _lines(capsys)[-1]["reason"] == "preview_refused"
    assert venue.sockets == []


async def test_a_level_already_reached_halts_without_acting(capsys):
    venue = Venue(mark="2480")
    code = await asyncio.wait_for(watch.run_trigger(_trigger_args(), connect=venue.connect, clock=venue.now), timeout=3)
    last = _lines(capsys)[-1]
    assert code == 1 and last["event"] == "halted" and last["reason"] == "already_triggered"


async def test_it_expires_at_its_deadline(capsys):
    venue = Venue()
    task = asyncio.create_task(
        watch.run_trigger(_trigger_args(max_hours=0.0001), connect=venue.connect, clock=venue.now)
    )
    await until(lambda: venue.sockets)
    await venue.tick("2499")
    venue.clock += 1  # past the 0.36 s deadline
    code = await asyncio.wait_for(task, 3)
    assert code == 0 and _lines(capsys)[-1]["event"] == "expired"


@pytest.mark.parametrize("hours", ["0", "25", "-1"])
def test_max_hours_is_bounded(hours):
    with pytest.raises(watch.Refused):
        watch._rules(_trigger_args(max_hours=float(hours)))


# --------------------------------------------------------------------------
# Background watches
# --------------------------------------------------------------------------


def test_detach_records_the_watch_and_list_status_stop_find_it(monkeypatch, tmp_path):
    monkeypatch.setenv("REYA_STATE_DIR", str(tmp_path / "state"))
    started: list = []

    class Process:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        started.append(command)
        sink = kwargs["stdout"]
        sink.write(json.dumps({"event": "watching", "mark": "2500"}) + "\n")
        sink.flush()
        return Process()

    monkeypatch.setattr(watch.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(watch, "_alive", lambda pid, watch_id: pid == 4242)
    monkeypatch.setattr(watch.sys, "argv", ["watch", "trigger", "--symbol", "eth", "--mark", "lte:2492", "--detach"])
    result = watch.detach(_trigger_args(detach=True, id=None))

    assert result["event"] == "detached" and result["state"] == "running" and result["pid"] == 4242
    assert "--detach" not in started[0] and started[0][started[0].index("--id") + 1] == result["id"]
    watch_id = result["id"]
    assert watch_id.startswith("eth-")
    assert [w["id"] for w in watch.list_watches()["watches"]] == [watch_id]
    assert watch.status(watch_id)["last_event"]["event"] == "watching"

    killed: list = []
    monkeypatch.setattr(watch.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(watch, "_alive", lambda pid, watch_id: False if killed else True)
    stopped = watch.stop_watch(watch_id)
    assert killed == [(4242, watch.signal.SIGTERM)] and stopped["stopped"] is True


def test_status_refuses_an_unknown_or_malformed_id(monkeypatch, tmp_path):
    monkeypatch.setenv("REYA_STATE_DIR", str(tmp_path / "state"))
    with pytest.raises(ValueError):
        watch.status("../../etc")
    with pytest.raises(ValueError):
        watch.status("eth-00000000")


# --------------------------------------------------------------------------
# Review round: failure paths
# --------------------------------------------------------------------------


def _feed(venue: Venue, events: list, stop: asyncio.Event, trigger=None):
    return watch.watch_feed(
        watch.get_api_url(),
        SYMBOL,
        trigger or below("2492"),
        deadline=venue.clock + 3600,
        stop=stop,
        on_event=events.append,
        connect=venue.connect,
        clock=venue.now,
    )


async def test_a_market_the_server_refuses_twice_ends_the_watch(monkeypatch):
    monkeypatch.setattr(watch, "RECONNECT_BASE_S", 0.0)
    venue = Venue()
    venue.answer = "error"
    decision = await asyncio.wait_for(_feed(venue, [], asyncio.Event()), 2)
    assert (decision.kind, decision.reason) == ("halt", "subscription_refused")
    assert len(venue.sockets) == 2  # one retry: a single refusal can be a passing server error


async def test_a_feed_that_never_serves_a_first_push_ends_the_watch(monkeypatch):
    monkeypatch.setattr(watch, "SUBSCRIBE_TIMEOUT_S", 0.02)
    monkeypatch.setattr(watch, "RECONNECT_BASE_S", 0.0)
    venue = Venue()
    venue.answer = "silent"
    task = asyncio.create_task(_feed(venue, [], asyncio.Event()))
    await until(lambda: len(venue.sockets) >= 2)
    venue.clock += 61
    decision = await asyncio.wait_for(task, 2)
    assert (decision.kind, decision.reason) == ("halt", "feed_stale")


async def test_a_stop_that_arrives_while_waiting_wins_over_the_crossing_push():
    venue, events, stop = Venue(), list[dict](), asyncio.Event()
    task = asyncio.create_task(_feed(venue, events, stop))
    await until(lambda: events)
    stop.set()
    venue.clock += 0.5
    venue.sockets[-1].update(venue.row("2480"))  # a crossing, queued behind the stop
    decision = await asyncio.wait_for(task, 2)
    assert (decision.kind, decision.reason) == ("halt", "stopped")


def test_a_push_older_than_one_already_seen_is_ignored():
    trigger = below("2492")
    trigger.on_push(push(10, "2500", pu=10_000))
    trigger.on_push(push(10.5, "2499", pu=10_500))
    assert trigger.on_push(push(11, "2400", pu=8_000)) is None  # an older snapshot, below the level
    assert trigger.last is not None and trigger.last.prices_updated_at == 10_500


def test_funding_met_only_after_a_gap_halts_as_crossed_while_stale():
    rule = _watch.FundingRule("gt", Decimal("0.001"), window_s=1.0)
    trigger = _watch.Trigger(None, rule)
    pushes = [push(0.5 * i, "2500", funding="0") for i in range(4)]  # full window, not met
    pushes += [push(10 + 0.5 * i, "2500", funding="0.01") for i in range(5)]  # after a gap, met
    assert feed(trigger, pushes)[-1] == ("halt", "crossed_while_stale")


def test_options_given_twice_are_refused():
    with pytest.raises(watch.Refused, match="repeats --symbol"):
        watch.parse_actions(["positions close_all --symbol eth --symbol btc"], unprotected_ok=False)


async def test_actions_run_through_the_real_trade_code(patch_client: FakeClient):
    preview = await watch.run_action(
        ["order", "market", "eth", "--side", "long", "--qty", "0.05", "--sl", "2%"], preview=True
    )
    assert preview["preview"] is True and preview["would_send"] is True and preview["protection_plan"]["stop_loss"]
    failed = await watch.run_action(
        ["order", "market", "nosuch", "--side", "long", "--qty", "1", "--sl", "2%"], preview=True
    )
    assert "error" in failed
    patch_client.create_limit_order.assert_not_awaited()


def test_a_dead_watch_is_died_not_running(monkeypatch, tmp_path):
    monkeypatch.setenv("REYA_STATE_DIR", str(tmp_path / "state"))
    output = watch.watch_dir() / "eth-deadbeef.jsonl"
    output.write_text(json.dumps({"event": "watching"}) + "\n")
    monkeypatch.setattr(watch, "_alive", lambda pid, watch_id: False)
    record = {"id": "eth-deadbeef", "pid": 1, "symbol": SYMBOL, "output": str(output)}
    assert watch._summary(record)["state"] == "died"


def test_detach_exits_1_when_the_watch_was_refused(monkeypatch, capsys):
    monkeypatch.setattr(watch.sys, "argv", ["watch", "trigger", "--symbol", "eth", "--mark", "lte:2492", "--detach"])
    monkeypatch.setattr(watch, "detach", lambda args: {"event": "detached", "state": "refused"})
    with pytest.raises(SystemExit) as exited:
        watch.main()
    assert exited.value.code == 1


def test_an_odd_symbol_still_gives_a_safe_watch_id(monkeypatch, tmp_path):
    monkeypatch.setenv("REYA_STATE_DIR", str(tmp_path / "state"))

    class Process:
        pid = 7

        def poll(self):
            return 0

    monkeypatch.setattr(watch.subprocess, "Popen", lambda command, **kwargs: Process())
    monkeypatch.setattr(watch, "_alive", lambda pid, watch_id: False)
    result = watch.detach(_trigger_args(symbol="../ETH/USD", detach=True, id=None))
    assert watch.WATCH_ID.match(result["id"]) and result["id"].startswith("ethusd-")
    assert {p.name for p in watch.watch_dir().iterdir()} == {f"{result['id']}.json", f"{result['id']}.jsonl"}


def test_a_second_watch_for_the_same_rule_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("REYA_STATE_DIR", str(tmp_path / "state"))

    class Process:
        pid = 9

        def poll(self):
            return None

    monkeypatch.setattr(watch.subprocess, "Popen", lambda command, **kwargs: Process())
    monkeypatch.setattr(watch, "_alive", lambda pid, watch_id: True)
    monkeypatch.setattr(watch, "DETACH_WAIT_S", 0.0)
    watch.detach(_trigger_args(detach=True, id=None))
    with pytest.raises(watch.Refused) as refused:
        watch.detach(_trigger_args(detach=True, id=None))
    assert refused.value.reason == "duplicate"


async def test_a_refusal_after_prices_flowed_is_transient_and_reconnects(monkeypatch):
    monkeypatch.setattr(watch, "RECONNECT_BASE_S", 0.0)
    venue, events, stop = Venue(), list[dict](), asyncio.Event()
    task = asyncio.create_task(_feed(venue, events, stop))
    await until(lambda: events)
    venue.answer = "error"
    venue.sockets[-1].push({"type": "error", "message": "Error while fetching snapshot"})
    await until(lambda: any(e["event"] == "reconnecting" for e in events))
    venue.answer = "subscribed"
    await until(lambda: len(venue.sockets) >= 3)
    stop.set()
    decision = await asyncio.wait_for(task, 2)
    assert decision.reason == "stopped"


def test_a_process_this_computer_cannot_inspect_is_unknown_and_blocks_a_new_watch(monkeypatch, tmp_path):
    monkeypatch.setenv("REYA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(watch, "_cmdline", lambda pid: None)  # no /proc and no ps
    output = watch.watch_dir() / "eth-0000abcd.jsonl"
    output.write_text(json.dumps({"event": "watching"}) + "\n")
    record = {
        "id": "eth-0000abcd",
        "pid": watch.os.getpid(),  # a live process
        "symbol": SYMBOL,
        "rule": "mark <= 2492",
        "then": [],
        "expires_at_ms": 10**15,
        "output": str(output),
    }
    (watch.watch_dir() / "eth-0000abcd.json").write_text(json.dumps(record))
    assert watch._summary(record)["state"] == "unknown"
    with pytest.raises(watch.Refused) as refused:
        watch.detach(_trigger_args(mark="gte:3000", detach=True, id=None))
    assert refused.value.reason == "liveness_unknown"


def test_a_duplicate_is_found_whatever_the_spelling_of_its_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("REYA_STATE_DIR", str(tmp_path / "state"))

    class Process:
        pid = 11

        def poll(self):
            return None

    monkeypatch.setattr(watch.subprocess, "Popen", lambda command, **kwargs: Process())
    monkeypatch.setattr(watch, "_alive", lambda pid, watch_id: True)
    monkeypatch.setattr(watch, "DETACH_WAIT_S", 0.0)
    watch.detach(_trigger_args(then=["trade positions close_all --symbol ETH"], detach=True, id=None))
    with pytest.raises(watch.Refused) as refused:
        watch.detach(_trigger_args(then=["positions close_all --symbol eth"], detach=True, id=None))
    assert refused.value.reason == "duplicate"


def test_detach_exits_1_when_the_watch_fired_with_an_error(monkeypatch):
    monkeypatch.setattr(watch.sys, "argv", ["watch", "trigger", "--symbol", "eth", "--mark", "lte:2492", "--detach"])
    monkeypatch.setattr(
        watch, "detach", lambda args: {"state": "fired", "last_event": {"event": "fired", "error": "--then 1 failed"}}
    )
    with pytest.raises(SystemExit) as exited:
        watch.main()
    assert exited.value.code == 1


def test_frames_buffered_while_paused_are_stale_and_a_crossing_in_them_never_fires():
    # Live pushes every 0.5 s arrive ~0.2 s after their prices. Then the process is paused for 6 s: on waking it
    # reads the frames the socket buffered, quickly, 6 s late, and one of them crosses the level.
    trigger = below("2492")
    live = [push(0.5 * i, "2500", pu=int(0.5 * i * 1000) - 200) for i in range(4)]
    # Every buffered frame is 6 s late by its row stamp, whether or not its price changed.
    buffered = [
        _watch.Push(
            receipt=8.0 + 0.01 * i,
            prices_updated_at=2000 + 1000 * (i // 2),
            mark=Decimal(m),
            funding=None,
            updated_at=2000 + 500 * i,
        )
        for i, m in enumerate(["2499", "2499", "2490", "2490", "2488"])
    ]
    on_time = [push(8.5, "2487", pu=8300), push(9.0, "2486", pu=8800)]
    kinds = feed(trigger, live + buffered + on_time)
    assert ("fire", None) not in kinds
    assert ("recovered", None) not in kinds  # buffered frames never count as live again
    assert ("stale", "late_delivery") in kinds
    assert kinds[-1] == ("halt", "crossed_while_stale")  # the level was crossed while the feed was not live


def test_a_late_frame_after_live_ones_is_stale_even_with_an_unchanged_price():
    trigger = below("2492")
    feed(trigger, [push(0.5 * i, "2500") for i in range(4)])
    late = _watch.Push(receipt=2.0, prices_updated_at=1500, mark=Decimal("2500"), funding=None, updated_at=-2000)
    decision = trigger.on_push(late)
    assert decision is not None and (decision.kind, decision.reason) == ("stale", "late_delivery")


def test_the_kit_env_decides_where_watches_are_recorded(monkeypatch, tmp_path, capsys):
    # conftest points the kit root at tmp_path; a REYA_STATE_DIR set only in its .env must be used.
    state = tmp_path / "from-env"
    (tmp_path / ".env").write_text(f"REYA_STATE_DIR={state}\n")
    monkeypatch.delenv("REYA_STATE_DIR", raising=False)
    monkeypatch.setattr(watch.sys, "argv", ["watch", "list"])
    watch.main()
    assert json.loads(capsys.readouterr().out) == {"watches": []}
    assert (state / "watches").is_dir()


def stamped(t: float, mark: str, stamp_ms: int, pu: int | None = None) -> _watch.Push:
    """A push received at `t` whose row the server stamped at `stamp_ms`."""
    return _watch.Push(
        receipt=t,
        prices_updated_at=stamp_ms if pu is None else pu,
        mark=Decimal(mark),
        funding=None,
        updated_at=stamp_ms,
    )


def test_a_buffered_first_push_and_the_burst_behind_it_never_fire():
    # The snapshot and the frames after it were all held while the process was paused: they arrive together.
    trigger = below("2492")
    burst = [
        stamped(100.0 + 0.01 * i, m, 1000 * i // 2)
        for i, m in enumerate(["2500", "2499", "2495", "2490", "2488", "2487"])
    ]
    live = [stamped(100.5 + 0.5 * i, "2486", 3000 + 500 * i) for i in range(1, 6)]
    kinds = feed(trigger, burst + live)
    assert ("fire", None) not in kinds
    assert ("stale", "late_delivery") in kinds and kinds[-1] == ("halt", "crossed_while_stale")


def test_network_jitter_never_reads_as_stale_and_the_crossing_fires():
    trigger = below("2492")
    jitter = [0.0, 0.3, -0.2, 0.25, -0.3, 0.1, 0.2, -0.1]
    pushes = [stamped(10 + 0.5 * i + jitter[i], "2500", 10_000 + 500 * i) for i in range(len(jitter))]
    pushes.append(stamped(14.0, "2491", 14_000))
    kinds = feed(trigger, pushes)
    assert kinds == [("watching", None), ("fire", None)]


def test_a_clock_step_heals_instead_of_ending_the_watch():
    trigger = below("2492")
    before = [stamped(0.5 * i, "2500", 500 * i) for i in range(6)]
    after = [stamped(3.0 + 5 + 0.5 * i, "2500", 3000 + 500 * i) for i in range(70)]  # receipts jump 5 s ahead
    kinds = feed(trigger, before + after)
    assert ("stale", "late_delivery") in kinds or ("stale", "push_gap") in kinds
    assert ("recovered", None) in kinds and not any(k == "halt" for k, _ in kinds)


@pytest.mark.parametrize("late_ms, stale", [(3000, False), (3001, True)])
def test_three_seconds_late_is_the_limit(late_ms, stale):
    trigger = below("2492")
    feed(trigger, [stamped(0.5 * i, "2500", 500 * i) for i in range(5)])
    # On time by this computer's clock and by prices, but its row was stamped `late_ms` before the usual delivery.
    decision = trigger.on_push(stamped(2.5, "2500", 2500 - late_ms, pu=2500))
    assert (decision is not None and decision.kind == "stale") is stale
    assert trigger.late_ms == late_ms


def test_without_row_stamps_only_the_gap_and_price_rules_apply():
    trigger = below("2492")
    pushes = [
        _watch.Push(receipt=0.5 * i, prices_updated_at=500 * i, mark=Decimal("2500"), funding=None) for i in range(4)
    ]
    pushes.append(_watch.Push(receipt=2.0, prices_updated_at=2000, mark=Decimal("2490"), funding=None))
    assert feed(trigger, pushes)[-1] == ("fire", None)
    assert trigger.late_ms is None


async def test_a_burst_of_held_frames_on_the_socket_never_fires():
    venue, events, stop = Venue(), list[dict](), asyncio.Event()
    task = asyncio.create_task(_feed(venue, events, stop))
    await until(lambda: events)
    for i, mark in enumerate(["2499", "2495", "2490", "2488"]):  # stamped 0.5 s apart, delivered together
        venue.sockets[-1].update({**venue.row(mark), "updatedAt": int(venue.clock * 1000) + 500 * (i + 1)})
    venue.clock += 0.05
    await asyncio.sleep(0.1)
    stop.set()
    decision = await asyncio.wait_for(task, 2)
    assert decision.reason == "stopped"
    assert not any(e["event"] == "fired" for e in events)


LAG_S = 0.25  # one-way delivery


def live_from(t0: float, n: int, mark: str = "2500") -> list:
    return [stamped(t0 + 0.5 * i + LAG_S, mark, int((t0 + 0.5 * i) * 1000)) for i in range(n)]


@pytest.mark.parametrize("pause_s", [6, 20, 35, 55])
def test_frames_held_through_a_pause_of_any_length_never_fire(pause_s):
    # Held frames are judged against deliveries from before the pause: the baseline is counted, not timed.
    trigger = below("2492")
    marks = ["2500", "2499", "2495", "2490"] + ["2488"] * 200
    held = [
        stamped(10 + pause_s + LAG_S + 0.002 * k, marks[k], int((10 + 0.5 * (k + 1)) * 1000))
        for k in range(int(pause_s / 0.5))
    ]
    resumed = live_from(10 + pause_s + 0.5, 4, "2488")
    kinds = feed(trigger, live_from(0, 21) + held + resumed)
    assert ("fire", None) not in kinds
    assert kinds[-1] == ("halt", "crossed_while_stale")


def test_a_short_network_stall_is_not_stale_and_the_next_crossing_fires():
    trigger = below("2492")
    stalled = [stamped(10.5 + 1.5 + LAG_S + 0.002 * k, "2500", int((10.5 + 0.5 * k) * 1000)) for k in range(4)]
    after = [stamped(12.5 + LAG_S, "2490", 12500)]
    assert feed(trigger, live_from(0, 21) + stalled + after) == [("watching", None), ("fire", None)]


def test_a_slow_push_cadence_still_fires():
    trigger = below("2492")
    pushes = [stamped(2.5 * i + LAG_S, "2500" if i < 6 else "2490", 2500 * i) for i in range(8)]
    assert feed(trigger, pushes)[-1] == ("fire", None)


def test_a_crossing_after_more_than_60_held_frames_never_fires():
    # A 45 s pause holds 90 frames, more than the 60-push delivery baseline. Held frames must never displace the
    # live deliveries from before the pause, or the last ones would look on time and a late crossing would fire.
    trigger = below("2492")
    marks = ["2500"] * 70 + ["2490"] * 20  # the crossing comes after the 60th held frame
    held = [stamped(10 + 45 + LAG_S + 0.002 * k, marks[k], int((10 + 0.5 * (k + 1)) * 1000)) for k in range(90)]
    resumed = live_from(10 + 45 + 0.5, 4, "2490")
    kinds = feed(trigger, live_from(0, 21) + held + resumed)
    assert ("fire", None) not in kinds
    assert kinds[-1] == ("halt", "crossed_while_stale")
