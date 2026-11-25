"""Credential-free local paper trading. This command can only GET public V2 data."""

from types import SimpleNamespace

import asyncio
import json
import math
import signal
import sqlite3
import sys
import time
from pathlib import Path

import aiohttp

from agent_kit.scripts._common import JsonArgumentParser, output_result, symbol_arg
from agent_kit.scripts._http import RegionUnavailableError
from agent_kit.scripts._paper import (
    CLOCK_WARNING_MS,
    MAX_EVENTS,
    PaperStore,
    arm_trigger,
    cancel_triggers,
    clock_offset,
    clock_report,
    due_triggers,
    initialize,
    observe,
    replay,
    simulate,
    upgrade,
    valuation,
)
from agent_kit.scripts._v2 import V2Client, V2StatusError, identifier, rows

APIS = ("https://api.reya.xyz/v2", "https://api-devnet.reya-cronos.network/v2")
TRIGGER_KINDS = {"sl": "stop_loss", "tp": "take_profit"}
WATCH_INTERVAL_S = 2.0
# V2 marks move every 500 ms; a tick is two round trips plus the ledger lock, which 0.5 s does not fit from afar.
MIN_WATCH_INTERVAL_S = 1.0
# Ticks in a row without an observation before one watch_warning.
WATCH_WARNING_TICKS = 3
# A clock warning clears a second inside its line, so jitter in the Date estimate does not repeat it.
CLOCK_CLEAR_MS = CLOCK_WARNING_MS - 1000
SKIP_REASONS = ("ledger_locked", "ledger_changed", "read_failed", "data_refused")
EDGE_BAN = (
    "Reya's edge rate-limited this IP (HTTP 429); watch stopped without retrying. Make no requests from this IP for "
    "at least 60 seconds: the ban also blocks live REST reads and cancels from it."
)
WATCH_NOTE = (
    "Paper saw the marks only at these observations: a move that reverses within max_gap_ms can still be missed. "
    "The venue fires TP/SL at the first mark through the trigger."
)


def _now() -> int:
    return int(time.time() * 1000)


def _clock() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _ledger(state):
    if state is None:
        raise ValueError("Run paper init first")
    if state["api_url"] not in APIS:
        raise ValueError("Paper ledger references an unsupported public V2 deployment")
    return state


async def run(args):
    if args.command == "watch":
        return await watch(args)
    now = _now()
    with PaperStore(args.directory) as store:
        state = store.state
        if args.command in ("init", "reset"):
            if args.command == "init" and state is not None:
                raise ValueError("Paper ledger already exists; inspect it or explicitly reset with its ledger ID")
            if args.command == "reset" and (state is None or args.confirm_ledger_id != state["ledger_id"]):
                raise ValueError("Reset requires the current ledger ID; export history before replacing it")
            state = initialize(args.collateral, args.fee_bps, args.api_url, now)
            store.save(state)
            return {**valuation(state, [], now), "api_url": state["api_url"]}
        state = upgrade(_ledger(state), now)
        if args.command == "history":
            if not 1 <= args.limit <= MAX_EVENTS or args.offset < 0:
                raise ValueError("Invalid paper history offset/limit")
            source = state["snapshots"] if args.snapshots else state["history"]
            data = list(reversed(source))[args.offset : args.offset + args.limit]
            return {
                "mode": "paper",
                "ledger_id": state["ledger_id"],
                "data": data,
                "coverage": {
                    "source": "local_only_since_initialization",
                    "created_at_ms": state["created_at_ms"],
                    "total_records": len(source),
                    "offset": args.offset,
                    "next_offset": args.offset + len(data) if args.offset + len(data) < len(source) else None,
                },
            }
        action = args.order_kind if args.command == "order" else None
        if action == "cancel_all":
            state, result = cancel_triggers(state, _symbol(args, required=False), args.all)
            store.save(state)
            return result
        request, symbol = None, None
        if action in TRIGGER_KINDS:
            symbol = _symbol(args)
            if args.trigger_px is None or args.limit_px is None or args.time_in_force is None or not args.side:
                raise ValueError(f"paper order {action} needs --side, --trigger_px, --limit_px and --time_in_force")
        elif args.command == "order":
            request = _paper_request(args)
            symbol = request["symbol"]
            cached = replay(state, request)
            if cached:
                return cached  # A retry does not depend on network availability or current prices.
        async with V2Client(state["api_url"]) as client:
            summaries = rows(await client.get("/perpMarkets/summary"))
            clock = clock_offset(getattr(client, "server_date", None))
            definitions = None
            if symbol or state["positions"] or state["triggers"]:
                definitions = rows(await client.get("/perpMarketDefinitions"))
                if symbol and not any(r.get("symbol") == symbol for r in definitions):
                    raise ValueError("Unknown perpetual market")
            # TP/SL fire on the mark at this observation; their children need the books.
            books = {}
            for due in due_triggers(state, summaries, _now(), clock):
                books[due] = await client.get(f"/market/{due}/depth", limit=1000)
            if request and symbol not in books:
                books[symbol] = await client.get(f"/market/{symbol}/depth", limit=1000)
            now = _now()
            state, events = observe(state, summaries, definitions, books, now, clock)
            context = {"events": events, **clock_report(clock)}
            if request:
                liquidation = next((e for e in events if e["event"] == "liquidation"), None)
                if liquidation is not None:
                    store.save(state)
                    return {
                        "mode": "paper",
                        "request_id": request["request_id"],
                        "symbol": symbol,
                        "status": "not_simulated",
                        "error": (
                            f"The paper account was liquidated at this observation: equity {liquidation['equity']} "
                            f"was below its liquidation margin {liquidation['liquidation_margin']}. Every position "
                            "was closed and the balance is 0; the order was not simulated and its request ID is "
                            "unused. See `events` and `paper history`."
                        ),
                        **context,
                    }
                state, result = simulate(state, request, definitions, books[symbol], summaries, now, clock)
                store.save(state)
                return {**result, **context}
            if action in TRIGGER_KINDS:
                state, result = arm_trigger(
                    state,
                    symbol,
                    TRIGGER_KINDS[action],
                    args.side,
                    args.trigger_px,
                    args.limit_px,
                    definitions,
                    summaries,
                    now,
                    time_in_force=args.time_in_force,
                    force=args.force,
                    client_order_id=args.client_order_id,
                    clock=clock,
                )
                store.save(state)
                return {**result, **context}
            result = valuation(state, summaries, now, definitions if state["positions"] else None, clock)
            if args.command == "refresh":
                if len(state["snapshots"]) >= MAX_EVENTS:
                    raise ValueError("Snapshot bound reached; export and reset before further recording")
                state["snapshots"].append(result)
            store.save(state)
            return {**result, **context}


# --------------------------------------------------------------------------
# watch: the observation, repeated
# --------------------------------------------------------------------------


class _Refused(Exception):
    """Market data the observation refuses (stale, skewed, missing); the ledger is untouched."""


def _emit(line: dict) -> None:
    print(json.dumps(line, separators=(",", ":")), flush=True)


def _watching(state) -> bool:
    """Something can still happen: a position to value, or a trigger the next observation fires or drops."""
    return bool(state["positions"] or state["triggers"])


def _quiet(state) -> dict:
    """The ledger less its observation time. A tick that changes only that is not saved."""
    return {key: value for key, value in state.items() if key != "last_observation_ms"}


def _on_sigterm():
    """SIGTERM stops watch as Ctrl-C does (asyncio.run cancels the task on Ctrl-C), so a background watch
    that is stopped still prints its summary. Returns the undo."""
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    assert task is not None
    try:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    except (NotImplementedError, RuntimeError):  # no loop signal handlers on Windows or off the main thread
        return lambda: None
    return lambda: loop.remove_signal_handler(signal.SIGTERM)


async def _read(client, state, tick):
    """The public reads for one observation of `state`: marks, definitions, and the books of due triggers."""
    summaries, definitions = await asyncio.gather(
        client.get("/perpMarkets/summary"), client.get("/perpMarketDefinitions")
    )
    summaries, definitions = rows(summaries), rows(definitions)
    tick.clock = clock_offset(getattr(client, "server_date", None))
    try:
        due = due_triggers(state, summaries, _now(), tick.clock)
    except ValueError as exc:
        raise _Refused(str(exc)) from None
    books = {symbol: await client.get(f"/market/{symbol}/depth", limit=1000) for symbol in due}
    return summaries, definitions, books


async def _tick(client, directory, known, last_safe):
    """One observation, planned from `known` (the ledger as watch last saw it) and applied only to that same
    ledger, so no event is applied twice. Reads hold no lock; the lock is held only to apply. When another
    paper command changed the ledger in between, the reads are discarded and planned again once."""
    tick = SimpleNamespace(known=known, last_safe=last_safe, clock=None, skip=None, stop=None, message=None)
    for _ in range(2):
        try:
            summaries, definitions, books = await _read(client, known, tick)
        except RegionUnavailableError:
            raise
        except _Refused as exc:
            tick.skip, tick.message = "data_refused", str(exc)
            return tick
        except V2StatusError as exc:
            if exc.status == 429:
                tick.stop, tick.message = "edge_rate_limited", EDGE_BAN
                return tick
            tick.skip, tick.message = "read_failed", str(exc)
            return tick
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            tick.skip, tick.message = "read_failed", str(exc) or type(exc).__name__
            return tick
        now = _now()
        try:
            with PaperStore(directory) as store:
                disk = store.state
                if disk == known:
                    # No await from here to the commit: Ctrl-C and SIGTERM land only in reads and sleeps.
                    # A quiet tick is not saved, so the last safe observation is carried in memory.
                    before = known if last_safe is None else {**known, "last_observation_ms": last_safe}
                    try:
                        state, events = observe(before, summaries, definitions, books, now, tick.clock)
                    except ValueError as exc:
                        tick.skip, tick.message = "data_refused", str(exc)
                        return tick
                    saved = _quiet(state) != _quiet(known)
                    if saved:
                        store.save(state)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
            tick.skip, tick.message = "ledger_locked", "Another paper command held the ledger"
            return tick
        if disk == known:
            # Committed: only now may the events be reported.
            tick.update = SimpleNamespace(
                state=state, events=events, now=now, summaries=summaries, definitions=definitions
            )
            tick.known, tick.last_safe = (state, None) if saved else (known, now)
            tick.skip = tick.message = None
            return tick
        if disk is None or disk.get("ledger_id") != known["ledger_id"]:
            tick.stop = "ledger_replaced"
            tick.message = (
                "The paper ledger was reset or replaced while watching; watch follows one ledger and stopped. "
                "Start it again to watch the new one."
            )
            return tick
        known = tick.known = disk
        last_safe = tick.last_safe = None
        tick.skip, tick.message = "ledger_changed", "Another paper command changed the ledger during the reads"
        if not _watching(known):
            return tick
    return tick


async def watch(args):
    """`paper watch`: the observation of `paper state`, every --interval seconds, until --duration, Ctrl-C or
    SIGTERM, a flat ledger, or a liquidation. Prints each TP/SL fill or liquidation as one JSON line, then a
    summary line. Saves only when the ledger changed."""
    interval, duration = args.interval, args.duration
    if not math.isfinite(interval) or interval < MIN_WATCH_INTERVAL_S:
        raise ValueError(
            f"--interval must be at least {MIN_WATCH_INTERVAL_S:g} second: V2 marks move every 500 ms, and a tick "
            "is two reads and the ledger lock"
        )
    if duration is not None and not (math.isfinite(duration) and duration > 0):
        raise ValueError("--duration must be a positive number of seconds")
    now = _now()
    with PaperStore(args.directory) as store:
        known = upgrade(_ledger(store.state), now)
        if known is not store.state:
            store.save(known)
    start = _clock()
    w = SimpleNamespace(
        started_at_ms=now,
        interval=interval,
        duration=duration,
        ticks=0,
        observations=0,
        skipped=dict.fromkeys(SKIP_REASONS, 0),
        events={},
        streak=0,
        observed_at=start,
        observed_at_ms=None,
        max_gap=0.0,
        clock=None,
        clock_warned=False,
        account=None,
        peak=None,
        stopped_by=None,
        error=None,
    )
    if not _watching(known):
        w.stopped_by, w.account = "flat", valuation(known, [], now)
        return _watch_summary(w, known)
    _emit(
        {
            "event": "watch_started",
            "mode": "paper",
            "ledger_id": known["ledger_id"],
            "started_at_ms": now,
            "interval_s": interval,
            "duration_s": duration,
            "positions": sorted(known["positions"]),
            "triggers": {symbol: sorted(legs) for symbol, legs in sorted(known["triggers"].items())},
        }
    )
    deadline = None if duration is None else start + duration
    last_safe = None
    release = _on_sigterm()
    try:
        async with V2Client(known["api_url"]) as client:
            next_at = start
            while True:
                w.ticks += 1
                tick = await _tick(client, args.directory, known, last_safe)
                known, last_safe = tick.known, tick.last_safe
                _watch_clock(w, tick.clock)
                if tick.stop:
                    w.stopped_by, w.error = tick.stop, tick.message
                    break
                if tick.skip:
                    _watch_skip(w, tick)
                else:
                    _watch_observation(w, tick.update)
                    if "liquidation" in w.events:
                        w.stopped_by = "liquidation"
                        break
                if not _watching(known):
                    w.stopped_by = "flat"
                    break
                next_at = max(next_at + interval, _clock())
                if deadline is not None and next_at >= deadline:
                    await _sleep(max(deadline - _clock(), 0))
                    w.stopped_by = "duration"
                    break
                await _sleep(max(next_at - _clock(), 0))
    except asyncio.CancelledError:
        w.stopped_by = "interrupt"
    finally:
        release()
    return _watch_summary(w, known)


def _watch_observation(w, update):
    for event in update.events:
        w.events[event["event"]] = w.events.get(event["event"], 0) + 1
        _emit(event)
    at = _clock()
    w.observations += 1
    w.streak = 0
    w.max_gap = max(w.max_gap, at - w.observed_at)
    w.observed_at, w.observed_at_ms = at, update.now
    state = update.state
    w.account = valuation(
        state, update.summaries, update.now, update.definitions if state["positions"] else None, w.clock
    )
    usage = (w.account["margin"] or {}).get("margin_usage")
    if usage is not None and (w.peak is None or usage > w.peak["margin_usage"]):
        w.peak = {"margin_usage": usage, "observed_at_ms": update.now}


def _watch_skip(w, tick):
    w.skipped[tick.skip] += 1
    w.streak += 1
    if w.streak == WATCH_WARNING_TICKS:
        _emit(
            {
                "event": "watch_warning",
                "mode": "paper",
                "reason": tick.skip,
                "message": tick.message,
                "ticks_without_observation": w.streak,
                "last_observation_ms": w.observed_at_ms,
            }
        )


def _watch_clock(w, clock):
    if clock is None:
        return
    w.clock = clock
    skew = abs(clock["offset_ms"])
    if not w.clock_warned and skew >= CLOCK_WARNING_MS:
        w.clock_warned = True
        _emit({"event": "clock_warning", "mode": "paper", **clock_report(clock)})
    elif w.clock_warned and skew < CLOCK_CLEAR_MS:
        w.clock_warned = False
        _emit({"event": "clock_ok", "mode": "paper", "clock": clock})


def _watch_summary(w, known) -> dict:
    gap = None if not w.ticks else round(max(w.max_gap, _clock() - w.observed_at) * 1000)
    summary = {
        "event": "watch_summary",
        "mode": "paper",
        "action": "watch",
        "ledger_id": known["ledger_id"],
        "stopped_by": w.stopped_by,
        "started_at_ms": w.started_at_ms,
        "ended_at_ms": _now(),
        "interval_s": w.interval,
        "duration_s": w.duration,
        "ticks": w.ticks,
        "observations": w.observations,
        "skipped": w.skipped,
        "max_gap_ms": gap,
        "events": w.events,
        "peak_margin_usage": w.peak,
        "account": w.account,
        **clock_report(w.clock),
        "note": WATCH_NOTE,
    }
    if not w.ticks:
        summary["message"] = "Nothing to watch: the paper ledger has no position or TP/SL."
    if w.error:
        summary["error"] = w.error
    return summary


def _symbol(args, required=True):
    if args.order_symbol and args.symbol and args.order_symbol != args.symbol:
        raise ValueError("Conflicting symbols")
    symbol = args.order_symbol or args.symbol
    if not symbol:
        if required:
            raise ValueError("A symbol is required")
        return None
    return identifier(symbol)


def _paper_request(args) -> dict:
    """Accept both the paper flags and the live `order market <symbol>` shape, so swapping
    `trade` for `paper` is enough. A request ID stays mandatory: a retry replays, never refills."""
    if args.request_id and args.client_order_id and args.request_id != args.client_order_id:
        raise ValueError("--request-id and --client_order_id differ; pass one replay key")
    request_id = args.request_id or args.client_order_id
    if not request_id:
        raise ValueError(
            "Paper orders need --request-id (or --client_order_id) so a retry replays instead of filling twice"
        )
    if args.order_kind == "limit":
        raise ValueError(
            "Paper cannot simulate resting GTC limit orders; use --kind ioc --price for immediate-or-cancel"
        )
    symbol = _symbol(args)
    if args.order_kind == "market" and args.kind == "ioc":
        raise ValueError("`order market` cannot be combined with --kind ioc")
    if not args.side:
        raise ValueError("--side is required")
    if args.qty is None and not args.max_size and args.usage is None:
        raise ValueError("Give one of --qty, --max_size or --usage")
    request = {
        "request_id": request_id,
        "symbol": symbol,
        "side": {"long": "buy", "short": "sell"}.get(args.side, args.side),
        "kind": args.kind or "market",
        "qty": args.qty,
        "price": args.price,
        "slippage": args.slippage,
        "reduce_only": args.reduce_only,
    }
    # Options appear only when used, so a request ID recorded before 0.10.0 still replays.
    if args.max_size:
        request["max_size"] = True
    if args.usage is not None:
        request["usage"] = args.usage
    if args.sl is not None or args.tp is not None:
        request.update(sl=args.sl, tp=args.tp, trigger_slippage=args.trigger_slippage)
    return request


def build_parser():
    parser = JsonArgumentParser(prog="paper", description=__doc__)
    parser.add_argument("--directory", default=str(Path.home() / ".local/state/reya-agent-kit/paper"))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "reset"):
        sub = commands.add_parser(command)
        sub.add_argument("--collateral", required=True)
        sub.add_argument("--fee-bps", required=True, help="Explicit simulated taker fee assumption; not your live tier")
        sub.add_argument("--api-url", choices=APIS, default=APIS[0])
        if command == "reset":
            sub.add_argument("--confirm-ledger-id", required=True)
    commands.add_parser("state")
    commands.add_parser("refresh")
    watcher = commands.add_parser(
        "watch", help="Observe the marks every few seconds, so TP/SL and liquidation happen between commands"
    )
    watcher.add_argument(
        "--interval", type=float, default=WATCH_INTERVAL_S, help="Seconds between observations, at least 1"
    )
    watcher.add_argument("--duration", type=float, help="Stop after this many seconds; without it, run until Ctrl-C")
    history = commands.add_parser("history")
    history.add_argument("--offset", type=int, default=0)
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--snapshots", action="store_true")
    order = commands.add_parser(
        "order", help="Simulate an order or arm a TP/SL; accepts the live `order market|sl|tp|cancel_all` shapes"
    )
    order.add_argument(
        "order_kind",
        nargs="?",
        choices=("market", "limit", "sl", "tp", "cancel_all"),
        help="Live-compatible: order market ETH, order sl ETH, order cancel_all --symbol ETH",
    )
    order.add_argument("order_symbol", nargs="?", type=symbol_arg)
    order.add_argument("--request-id")
    order.add_argument("--client_order_id", help="Live-compatible alias, used as the request ID")
    order.add_argument("--symbol", type=symbol_arg)
    order.add_argument("--side", choices=("buy", "sell", "long", "short"))
    order.add_argument("--kind", choices=("market", "ioc"))
    size = order.add_mutually_exclusive_group()
    size.add_argument("--qty")
    size.add_argument("--max_size", action="store_true", help="Largest size the engine admits, less 0.2%% headroom")
    size.add_argument("--usage", help="Size so margin usage is at most this fraction after a full fill")
    order.add_argument("--price")
    order.add_argument("--slippage", default="0.01")
    order.add_argument("--reduce-only", "--reduce_only", dest="reduce_only", action="store_true")
    order.add_argument("--sl", help="Arm a stop-loss once the entry fills: a percent from the fill price or a price")
    order.add_argument("--tp", help="Arm a take-profit once the entry fills: a percent from the fill price or a price")
    order.add_argument("--trigger_slippage", default="0.01", help="Child limit distance from each trigger")
    order.add_argument("--trigger_px", help="order sl|tp: the mark at which it fires")
    order.add_argument("--limit_px", help="order sl|tp: the child's worst price")
    order.add_argument("--time_in_force", choices=("IOC", "GTC"), help="order sl|tp: paper fires IOC children only")
    order.add_argument("--force", action="store_true", help="order sl|tp: arm a trigger the mark has already reached")
    order.add_argument("--all", action="store_true", help="order cancel_all: every paper TP/SL")
    order.add_argument("--fees_wait_s", help="Accepted for live compatibility; paper fees are immediate")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        result = {
            "error": (
                str(exc)
                if isinstance(exc, ValueError)
                else f"{type(exc).__name__}: paper operation failed; inspect the ledger and retry orders with the same request ID"
            ),
            **getattr(exc, "details", {}),
        }
    if args.command != "watch":
        output_result(result)
        return
    # watch prints JSON lines; its summary or error is one more line.
    _emit(result)
    if result.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
