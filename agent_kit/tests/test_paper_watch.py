"""paper watch: the observation repeated, with no lock across reads and no event applied twice."""

from types import SimpleNamespace

import asyncio
import json
import os
import signal
import sys
from decimal import Decimal
from email.utils import formatdate
from unittest.mock import AsyncMock

import aiohttp
import pytest

from agent_kit.scripts import paper
from agent_kit.scripts._paper import PaperStore, cancel_triggers, initialize, observe
from agent_kit.scripts._v2 import V2Client, V2StatusError
from agent_kit.tests.test_paper import NOW, SYMBOL, book, bracketed, leveraged_definition, leveraged_long
from agent_kit.tests.test_paper import state as fresh_ledger
from agent_kit.tests.test_paper import summary

SUMMARY, DEFINITIONS, DEPTH = "/perpMarkets/summary", "/perpMarketDefinitions", f"/market/{SYMBOL}/depth"
QUIET_TICK = [SUMMARY, DEFINITIONS]


@pytest.fixture
def clock(monkeypatch):
    """Wall and monotonic time that move only when watch sleeps."""
    now = SimpleNamespace(s=NOW / 1000)
    monkeypatch.setattr(paper.time, "time", lambda: now.s)
    monkeypatch.setattr(paper, "_clock", lambda: now.s)

    async def sleep(seconds):
        now.s += seconds

    monkeypatch.setattr(paper, "_sleep", sleep)
    return now


@pytest.fixture
def saves(monkeypatch):
    recorded = []
    original = PaperStore.save

    def save(self, state):
        recorded.append(state)
        original(self, state)

    monkeypatch.setattr(PaperStore, "save", save)
    return recorded


class Market:
    """Public V2 reads at the fake clock's time. The n-th summary read returns marks[n-1] (the last repeats)
    and first runs during[n], if any: another paper command, a failure. stale holds reads served 31 s old;
    skews the server Date's lag behind this computer, in seconds."""

    def __init__(self, clock, marks, bids=(("97.6", "20"),), asks=(("97.7", "20"),)):
        self.clock, self.marks, self.bids, self.asks = clock, list(marks), bids, asks
        self.reads, self.paths = 0, []
        self.during: dict = {}
        self.stale: set = set()
        self.skews: dict = {}

    def book(self):
        return book(bids=self.bids, asks=self.asks, at=int(self.clock.s * 1000))

    async def get(self, path, **params):
        self.paths.append(path)
        at = int(self.clock.s * 1000)
        if path == SUMMARY:
            self.reads += 1
            if self.reads in self.during:
                self.during.pop(self.reads)()
            if self.reads in self.skews:
                V2Client.server_date = (
                    self.clock.s,
                    self.clock.s,
                    formatdate(self.clock.s - self.skews[self.reads], usegmt=True),
                )
            mark = self.marks[min(self.reads, len(self.marks)) - 1]
            return [summary(mark=mark, at=at - 31000 if self.reads in self.stale else at)]
        if path == DEFINITIONS:
            return [leveraged_definition()]
        assert path == DEPTH and params == {"limit": 1000}
        return self.book()


def ledger(tmp_path, state):
    directory = tmp_path / "paper"
    with PaperStore(directory) as store:
        store.save(state)
    return directory


def connect(monkeypatch, market):
    monkeypatch.setattr(V2Client, "get", AsyncMock(side_effect=market.get))


async def watch(directory, *options):
    return await paper.run(paper.build_parser().parse_args(["--directory", str(directory), "watch", *options]))


def lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def history(directory):
    with PaperStore(directory) as store:
        return store.state, [e.get("event") for e in store.state["history"]]


async def test_a_stop_loss_fires_between_commands_and_only_that_tick_saves(tmp_path, clock, saves, monkeypatch, capsys):
    directory = ledger(tmp_path, bracketed(sl="2%", tp="3%")[0])  # long 10 at 100, stop 98, take-profit 103
    saves.clear()
    market = Market(clock, ["100", "99", "97.5"])
    connect(monkeypatch, market)
    result = await watch(directory, "--duration", "60")
    started, fired = lines(capsys)
    assert started["event"] == "watch_started" and started["interval_s"] == 2
    assert started["positions"] == [SYMBOL] and started["triggers"] == {SYMBOL: ["stop_loss", "take_profit"]}
    assert fired["event"] == "stop_loss" and fired["timestamp"] == NOW + 4000
    assert [(f["qty"], f["price"]) for f in fired["fills"]] == [("10", "97.6")] and Decimal(
        fired["realized_pnl"]
    ) == -24
    assert result["stopped_by"] == "flat" and result["ticks"] == result["observations"] == 3
    assert result["events"] == {"stop_loss": 1} and result["max_gap_ms"] == 2000 and "error" not in result
    assert result["skipped"] == dict.fromkeys(paper.SKIP_REASONS, 0)
    assert result["account"]["positions"] == [] and Decimal(result["account"]["cash"]) == 976
    assert len(saves) == 1  # the two quiet ticks saved nothing
    # Public GETs only, and a book only for the tick whose mark reached the stop.
    assert market.paths == QUIET_TICK * 3 + [DEPTH]
    state, events = history(directory)
    assert events.count("stop_loss") == 1 and state["last_observation_ms"] == NOW + 4000


async def test_a_stop_that_does_not_fill_is_used_up_once_and_watch_keeps_going(
    tmp_path, clock, saves, monkeypatch, capsys
):
    directory = ledger(tmp_path, bracketed(sl="2%", tp="3%")[0])
    saves.clear()
    # Bids at 96, below the stop's child limit 97.02: nothing fills, the stop is used up, the position stays.
    market = Market(clock, ["96.05"], bids=(("96", "20"),), asks=(("96.1", "20"),))
    connect(monkeypatch, market)
    result = await watch(directory, "--duration", "5")
    [_, fired] = lines(capsys)
    assert fired["event"] == "stop_loss" and fired["status"] == "cancelled" and fired["remaining_position"] == "10"
    assert result["stopped_by"] == "duration" and result["observations"] == 3 and len(saves) == 1
    # The saved ledger is the one watch compares next: no tick re-reads for a change of its own.
    assert market.paths == QUIET_TICK + [DEPTH] + QUIET_TICK * 2
    state, _ = history(directory)
    assert set(state["triggers"][SYMBOL]) == {"take_profit"} and state["positions"][SYMBOL]["qty"] == "10"


async def test_reads_hold_no_lock_and_a_locked_tick_is_skipped(tmp_path, clock, saves, monkeypatch, capsys):
    directory = ledger(tmp_path, bracketed(sl="2%", tp="3%")[0])
    market = Market(clock, ["100", "97.5"])
    held = []

    def another_command():
        with PaperStore(directory):  # watch is reading: the ledger is free
            pass
        held.append(PaperStore(directory).__enter__())  # and is still held when watch applies

    market.during = {1: another_command, 2: lambda: held.pop().__exit__(None, None, None)}
    connect(monkeypatch, market)
    result = await watch(directory, "--duration", "600")
    assert [line["event"] for line in lines(capsys)] == ["watch_started", "stop_loss"]
    assert result["skipped"]["ledger_locked"] == 1 and result["observations"] == 1
    assert result["stopped_by"] == "flat" and result["events"] == {"stop_loss": 1}


async def test_an_event_another_command_applied_first_is_not_applied_again(tmp_path, clock, monkeypatch, capsys):
    directory = ledger(tmp_path, bracketed(sl="2%", tp="3%")[0])
    market = Market(clock, ["97.5"])

    def paper_state_fires_the_stop():
        at = int(clock.s * 1000)
        with PaperStore(directory) as store:
            state, events = observe(
                store.state, [summary(mark="97.5", at=at)], [leveraged_definition()], {SYMBOL: market.book()}, at
            )
            assert [e["event"] for e in events] == ["stop_loss"]
            store.save(state)

    market.during = {1: paper_state_fires_the_stop}
    connect(monkeypatch, market)
    result = await watch(directory, "--duration", "60")
    assert [line["event"] for line in lines(capsys)] == ["watch_started"]
    assert result["stopped_by"] == "flat" and result["skipped"]["ledger_changed"] == 1
    assert result["observations"] == 0 and result["events"] == {}
    _, events = history(directory)
    assert events.count("stop_loss") == 1


async def test_reads_planned_on_a_ledger_another_command_changed_are_discarded(tmp_path, clock, monkeypatch, capsys):
    directory = ledger(tmp_path, bracketed(sl="2%", tp="3%")[0])
    market = Market(clock, ["97.5"])  # through the stop

    def cancel_all():
        with PaperStore(directory) as store:
            store.save(cancel_triggers(store.state, SYMBOL)[0])

    market.during = {1: cancel_all}
    connect(monkeypatch, market)
    result = await watch(directory, "--duration", "3")
    # The stop was cancelled after watch read a mark through it: watch plans again and fires nothing.
    assert [line["event"] for line in lines(capsys)] == ["watch_started"]
    assert market.paths == QUIET_TICK + [DEPTH] + QUIET_TICK * 2
    assert result["observations"] == 2 and result["skipped"]["ledger_changed"] == 0
    assert result["stopped_by"] == "duration" and result["events"] == {}
    state, events = history(directory)
    assert "stop_loss" not in events and state["positions"][SYMBOL]["qty"] == "10"


async def test_a_liquidation_between_commands_stops_watch(tmp_path, clock, saves, monkeypatch, capsys):
    directory = ledger(tmp_path, leveraged_long()[0])  # long 100 at 100 on 1,000: liquidation price 92.86
    saves.clear()
    connect(monkeypatch, Market(clock, ["95", "93", "92.8"]))
    result = await watch(directory, "--duration", "600")
    [_, liquidation] = lines(capsys)
    # The quiet ticks were not saved, yet the last safe observation is the tick before.
    assert liquidation["event"] == "liquidation" and liquidation["last_safe_observation_ms"] == NOW + 2000
    assert result["stopped_by"] == "liquidation" and "error" not in result and len(saves) == 1
    assert result["peak_margin_usage"]["observed_at_ms"] == NOW + 2000
    assert result["peak_margin_usage"]["margin_usage"] > 0.9
    state, _ = history(directory)
    assert state["cash"] == "0" and state["positions"] == {} and state["last_observation_ms"] == NOW + 2000


async def test_duration_interval_bounds_and_quiet_ticks(tmp_path, clock, saves, monkeypatch, capsys):
    directory = ledger(tmp_path, leveraged_long(qty="10")[0])
    saves.clear()
    market = Market(clock, ["100"])
    connect(monkeypatch, market)
    for options in (["--interval", "0.5"], ["--interval", "nan"], ["--duration", "0"], ["--duration", "inf"]):
        with pytest.raises(ValueError, match="--interval|--duration"):
            await watch(directory, *options)
    result = await watch(directory, "--interval", "2", "--duration", "10")
    assert result["stopped_by"] == "duration" and result["ticks"] == result["observations"] == 5
    assert result["max_gap_ms"] == 2000 and result["ended_at_ms"] == NOW + 10000 and result["duration_s"] == 10
    assert market.paths == QUIET_TICK * 5 and saves == []
    assert result["account"]["positions"][0]["qty"] == "10" and result["account"]["margin"]["margin_usage"] > 0
    assert [line["event"] for line in lines(capsys)] == ["watch_started"]


async def test_an_edge_ban_stops_watch_without_retrying(tmp_path, clock, monkeypatch):
    directory = ledger(tmp_path, leveraged_long(qty="10")[0])
    get = AsyncMock(side_effect=V2StatusError(SUMMARY, 429))
    monkeypatch.setattr(V2Client, "get", get)
    result = await watch(directory, "--duration", "60")
    assert result["stopped_by"] == "edge_rate_limited" and "at least 60 seconds" in result["error"]
    assert result["ticks"] == 1 and get.await_count == 2  # the tick's two reads, nothing after


async def test_failed_reads_and_refused_data_skip_ticks_with_one_warning_per_streak(
    tmp_path, clock, saves, monkeypatch, capsys
):
    directory = ledger(tmp_path, leveraged_long(qty="10")[0])
    saves.clear()
    market = Market(clock, ["100"])

    def fail(exc):
        def raises():
            raise exc

        return raises

    market.during = {
        1: fail(aiohttp.ClientConnectionError("connection reset")),
        2: fail(asyncio.TimeoutError()),
        3: fail(V2StatusError(SUMMARY, 502)),
    }
    market.stale = {5}
    connect(monkeypatch, market)
    result = await watch(directory, "--duration", "11")
    [warning] = [line for line in lines(capsys) if line["event"] == "watch_warning"]
    assert warning["reason"] == "read_failed" and "HTTP 502" in warning["message"]
    assert warning["ticks_without_observation"] == 3 and warning["last_observation_ms"] is None
    assert result["skipped"] == {"ledger_locked": 0, "ledger_changed": 0, "read_failed": 3, "data_refused": 1}
    assert result["ticks"] == 6 and result["observations"] == 2 and result["max_gap_ms"] == 6000
    assert saves == []


async def test_a_clock_warning_prints_once_and_when_it_clears(tmp_path, clock, monkeypatch, capsys):
    directory = ledger(tmp_path, leveraged_long(qty="10")[0])
    monkeypatch.setattr(V2Client, "server_date", None, raising=False)
    market = Market(clock, ["100"])
    market.skews = {1: 3, 2: 3, 3: 0, 4: 0}  # Reya's clock 3 s behind this computer, then in step
    connect(monkeypatch, market)
    result = await watch(directory, "--duration", "7")
    warning, cleared = [line for line in lines(capsys) if line["event"] != "watch_started"]
    assert warning["event"] == "clock_warning" and warning["clock"]["offset_ms"] == 2500
    assert "2.5 s ahead of Reya's server clock" in warning["clock_warning"]
    assert cleared["event"] == "clock_ok" and cleared["clock"]["offset_ms"] == -500
    assert result["clock"]["offset_ms"] == -500 and "clock_warning" not in result and result["observations"] == 4


async def test_a_flat_ledger_has_nothing_to_watch(tmp_path, clock, monkeypatch, capsys):
    directory = ledger(tmp_path, fresh_ledger())
    monkeypatch.setattr(V2Client, "get", AsyncMock(side_effect=AssertionError("no reads")))
    result = await watch(directory)
    assert result["stopped_by"] == "flat" and result["ticks"] == 0 and result["max_gap_ms"] is None
    assert "Nothing to watch" in result["message"] and Decimal(result["account"]["equity"]) == 1000
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(sys.platform == "win32", reason="no loop signal handlers on Windows")
async def test_sigterm_stops_watch_with_its_summary(tmp_path, clock, saves, monkeypatch, capsys):
    directory = ledger(tmp_path, bracketed(sl="2%", tp="3%")[0])
    saves.clear()
    connect(monkeypatch, Market(clock, ["100"]))

    async def terminated(seconds):
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(1)  # the handler cancels watch here

    monkeypatch.setattr(paper, "_sleep", terminated)
    before = signal.getsignal(signal.SIGTERM)
    result = await watch(directory, "--duration", "600")
    assert result["stopped_by"] == "interrupt" and result["observations"] == 1 and "error" not in result
    assert signal.getsignal(signal.SIGTERM) == before and saves == []


async def test_an_interrupt_during_a_read_writes_nothing(tmp_path, clock, saves, monkeypatch):
    directory = ledger(tmp_path, bracketed(sl="2%", tp="3%")[0])
    saves.clear()
    market = Market(clock, ["100", "97.5"])

    def interrupted():
        raise asyncio.CancelledError  # Ctrl-C: asyncio.run cancels the task

    market.during = {2: interrupted}
    connect(monkeypatch, market)
    result = await watch(directory, "--duration", "600")
    assert result["stopped_by"] == "interrupt" and result["observations"] == 1 and saves == []
    _, events = history(directory)
    assert "stop_loss" not in events


async def test_a_reset_ledger_stops_watch(tmp_path, clock, monkeypatch):
    directory = ledger(tmp_path, leveraged_long(qty="10")[0])
    market = Market(clock, ["100"])

    def reset():
        with PaperStore(directory) as store:
            store.save(initialize("500", "0", paper.APIS[0], int(clock.s * 1000)))

    market.during = {2: reset}
    connect(monkeypatch, market)
    result = await watch(directory, "--duration", "600")
    assert result["stopped_by"] == "ledger_replaced" and "follows one ledger" in result["error"]
    assert result["observations"] == 1
