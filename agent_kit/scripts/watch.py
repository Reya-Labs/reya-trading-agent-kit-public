"""Wait on Reya's WebSocket for a price or funding level, then run at most two agreed kit commands once.

For rules Reya cannot run itself (a pair trade on one trigger, a funding trigger, a cross-market
exit), and for watching a price without acting. It is event-driven: it reads every push of the
market summary channel and never polls REST. It runs only while this computer is awake and the
process lives, so anything it touches must already have a stop-loss on Reya.

    watch trigger --symbol S [--mark lte|gte:PRICE] [--funding lt|lte|gt|gte:PCT_PER_HOUR]  (at least one)
                  [--funding-window MIN] [--then "<trade command>"]... [--unprotected-ok]
                  [--max-hours H] [--detach]
    watch list | status ID | stop ID

Output is one JSON object per line (`event`): `arming`, `armed`, `watching`, `stale`, `recovered`,
`skipped`, `reconnecting`, then one terminal event: `fired`, `expired`, `stopped`, `halted` or
`refused`. Exit 0 for `fired` with every action done, `expired` and `stopped`; 1 otherwise.
"""

from typing import Any, Callable

import argparse
import asyncio
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import aiohttp
from sdk.open_api.models.order_type import OrderType

from agent_kit.scripts import _watch, trade
from agent_kit.scripts._common import JsonArgumentParser, capture_output, output_json, symbol_arg
from agent_kit.scripts._env import get_api_url, load_env
from agent_kit.scripts._http import USER_AGENT
from agent_kit.scripts._stream import FeedState
from agent_kit.scripts._v2 import WS_URLS
from agent_kit.scripts._writer import state_directory
from agent_kit.scripts.stream import _next, _reader

MAX_ACTIONS = 2
ALLOWED_ACTIONS = {("order", "market"), ("order", "limit"), ("positions", "close_all")}
REFUSED_FLAGS = ("--force", "--allow_large", "--skip_margin_check", "--preview", "--help", "-h", "--all")
MAX_HOURS = 24.0
IDLE_CHECK_S = 1.0
SUBSCRIBE_TIMEOUT_S = 10.0
RECONNECT_BASE_S, RECONNECT_MAX_S = 0.5, 8.0
DETACH_WAIT_S = 20.0  # how long `--detach` waits for the watcher's first verdict before returning
WATCH_ID = re.compile(r"^[a-z0-9-]{1,40}$")
TERMINAL = ("fired", "expired", "stopped", "halted", "refused")


class Refused(ValueError):
    def __init__(self, reason: str, message: str, **detail):
        super().__init__(message)
        self.reason, self.detail = reason, detail


def emit(event: dict) -> None:
    print(json.dumps({**event, "at_ms": int(time.time() * 1000)}, separators=(",", ":"), default=str), flush=True)


def watch_dir() -> Path:
    path = state_directory() / "watches"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


# ---------------------------------------------------------------------- actions


def parse_actions(texts: list[str], *, unprotected_ok: bool) -> list[tuple[list[str], argparse.Namespace]]:
    """Each `--then` as a kit trade command: allowed, without overrides, and protected unless agreed."""
    if len(texts) > MAX_ACTIONS:
        raise Refused("too_many_actions", f"At most {MAX_ACTIONS} --then commands; they run in order, not atomically")
    actions = []
    for number, text in enumerate(texts, 1):
        argv = shlex.split(text)
        if argv[:1] == ["trade"]:
            argv = argv[1:]
        if not argv or len(argv) > 40:
            raise Refused("invalid_action", f"--then {number} is empty or too long")
        options = [a.split("=", 1)[0] for a in argv if a.startswith("-")]
        bad = [flag for flag in REFUSED_FLAGS if flag in options]
        if bad:
            raise Refused("invalid_action", f"--then {number} may not use {', '.join(bad)}: no overrides in a watch")
        repeated = sorted({o for o in options if options.count(o) > 1})
        if repeated:
            raise Refused("invalid_action", f"--then {number} repeats {', '.join(repeated)}: give each option once")
        with capture_output() as output:
            try:
                args = trade.build_parser().parse_args(argv)
            except SystemExit:
                message = output[-1].get("error") if output else "invalid command"
                raise Refused("invalid_action", f"--then {number} is not a kit trade command: {message}") from None
        if (args.group, args.action) not in ALLOWED_ACTIONS:
            raise Refused(
                "invalid_action",
                f"--then {number} must be `order market`, `order limit` or `positions close_all --symbol X`",
            )
        if args.group == "positions" and not getattr(args, "symbol", None):
            raise Refused("invalid_action", f"--then {number}: `positions close_all` needs --symbol, never --all")
        reduce_only = bool(getattr(args, "reduce_only", False)) or args.group == "positions"
        if not reduce_only and not unprotected_ok:
            if args.action == "market" and getattr(args, "sl", None) is None:
                raise Refused(
                    "unprotected_entry",
                    f"--then {number} opens or adds to a position without --sl: add a stop-loss on Reya, "
                    "or pass --unprotected-ok only after the user agreed to an entry with no stop",
                )
            if args.action == "limit":
                raise Refused(
                    "unprotected_entry",
                    f"--then {number} rests a limit entry, which carries no stop: use `order market --sl`, or pass "
                    "--unprotected-ok only after the user agreed to a fill with no stop on Reya",
                )
        actions.append((argv, args))
    return actions


async def run_action(argv: list[str], *, preview: bool = False) -> dict:
    """One kit trade command in this process, through its own guards; its JSON result."""
    with capture_output() as output:
        try:
            args = trade.build_parser().parse_args([*argv, *(["--preview"] if preview else [])])
        except SystemExit:
            return output[-1] if output else {"error": "Invalid command"}
        function = getattr(trade, f"cmd_{args.group}_{args.action}")
        try:
            await function(args)
        except SystemExit:
            pass
        except Exception as exc:  # the trade CLI's own envelope, as `trade.main` reports it
            output_json(trade._error_result(args, exc))
    return output[-1] if output else {"error": "The command returned no outcome"}


async def backstop_check(actions, *, unprotected_ok: bool) -> list:
    """Every open position on a market the actions touch must already have a stop-loss on Reya.

    It is the protection that stays if this watch dies (computer asleep, process killed, feed lost).
    """
    symbols = sorted({args.symbol for _, args in actions})
    if unprotected_ok or not symbols:
        return []
    found = []
    async with trade.trading_client() as client:
        await client.start()
        account_id = await trade.require_owned_account(client)
        positions = [p for p in await client.get_positions() if p.account_id == account_id and Decimal(p.qty) != 0]
        for position in (p for p in positions if p.symbol in symbols):
            side = trade._position_side(position)
            armed = await trade._armed_for_side(client, account_id, position.symbol, side)
            entry = {"symbol": position.symbol, "side": side, "qty": position.qty}
            if armed is None:
                raise Refused("backstop_unknown", f"Could not read the orders on {position.symbol}", position=entry)
            if OrderType.STOP_LOSS not in armed:
                raise Refused(
                    "no_backstop",
                    f"The {side} {position.symbol} position has no stop-loss on Reya, so nothing protects it if this "
                    f"watch stops. Arm one (`order sl {position.symbol} ...`) first, or pass --unprotected-ok only "
                    "after the user agreed",
                    position=entry,
                )
            found.append({**entry, "stop_loss": "armed"})
    return found


async def preview_actions(actions) -> list:
    previews = []
    for number, (argv, _) in enumerate(actions, 1):
        result = await run_action(argv, preview=True)
        previews.append({"command": " ".join(argv), "preview": result})
        if result.get("error") or result.get("would_send") is False:
            raise Refused(
                "preview_refused",
                f"--then {number} would not be sent now: {result.get('error') or 'refused by its own checks'}",
                preview=result,
            )
    return previews


async def fire(actions) -> tuple[list, str | None]:
    """Run the actions once, in order; stop at the first failure. Never retried, never repeated."""
    results: list = []
    for number, (argv, _) in enumerate(actions, 1):
        result = await run_action(argv)
        results.append({"command": " ".join(argv), "result": result})
        if result.get("error"):
            skipped = len(actions) - number
            note = f"; {skipped} later command(s) not sent" if skipped else ""
            return results, f"--then {number} failed: {result['error']}{note}. Read the account before acting."
    return results, None


# ---------------------------------------------------------------------- the feed


def _connector(url: str) -> Callable:
    @asynccontextmanager
    async def connect():
        timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_connect=5)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            async with session.ws_connect(url, heartbeat=10, max_msg_size=256 * 1024) as ws:
                yield ws

    return connect


def _push(row: dict, receipt: float) -> _watch.Push:
    funding = row.get("fundingRate")
    updated = row.get("updatedAt")
    return _watch.Push(
        receipt=receipt,
        prices_updated_at=int(row["pricesUpdatedAt"]),
        mark=Decimal(str(row["markPrice"])),
        funding=None if funding is None else Decimal(str(funding)),
        updated_at=None if updated is None else int(updated),
    )


async def watch_feed(
    api_url: str,
    symbol: str,
    trigger: _watch.Trigger,
    *,
    deadline: float,
    stop: asyncio.Event,
    on_event: Callable[[dict], None],
    connect: Callable | None = None,
    clock: Callable[[], float] = time.time,
) -> _watch.Decision:
    """Read every push of the market summary channel until the trigger fires or halts, the deadline, or a stop."""
    if connect is None:
        if api_url not in WS_URLS:
            raise ValueError("No verified WebSocket deployment for this API URL")
        connect = _connector(WS_URLS[api_url])
    channel = f"/v2/perpMarket/{symbol}/summary"
    state = FeedState(api_url, channel)
    attempt = 0
    refusals = 0  # subscription refusals before any price: the second one ends the watch
    started = clock()

    def idle() -> _watch.Decision | None:
        """No usable push: the trigger's own staleness rules, and a feed that never served a first push."""
        if trigger.last is None and clock() - started > _watch.HALT_STALE_S:
            return _watch.Decision("halt", "feed_stale", {"stale_s": round(clock() - started, 1)})
        return handle(trigger.on_idle(clock()))

    def ended() -> _watch.Decision | None:
        if stop.is_set():
            return _watch.Decision("halt", "stopped")
        if clock() >= deadline:
            return _watch.Decision("halt", "expired")
        return None

    def handle(decision: _watch.Decision | None) -> _watch.Decision | None:
        if decision is None:
            return None
        if decision.kind in ("fire", "halt"):
            return decision
        names = {"watching": "watching", "stale": "stale", "recovered": "recovered", "skip": "skipped"}
        event: dict = {"event": names[decision.kind]}
        if decision.reason:
            event["reason"] = decision.reason
        on_event({**event, **decision.detail})
        return None

    def take(contents_row: dict | None) -> _watch.Decision | None:
        if contents_row is None:
            return None
        return handle(trigger.on_push(_push(contents_row, clock())))

    while True:
        if (over := ended()) is not None:
            return over
        try:
            async with connect() as ws:
                queue: asyncio.Queue = asyncio.Queue(maxsize=128)
                reader = asyncio.create_task(_reader(ws, queue))
                try:
                    await ws.send_json({"type": "subscribe", "channel": channel})
                    subscribed_by = time.monotonic() + SUBSCRIBE_TIMEOUT_S
                    while True:
                        message = await _next(queue, reader, max(0.1, subscribed_by - time.monotonic()))
                        if message.get("type") == "error":
                            text = json.dumps(message)[:300]
                            if "RATE_EXCEEDED" in text or trigger.last is not None or refusals == 0:
                                # Limited, a market it already streamed, or a first refusal: retry.
                                if trigger.last is None and "RATE_EXCEEDED" not in text:
                                    refusals += 1
                                raise ValueError(f"The server refused the subscription: {text}")
                            # Refused twice before any price: an unknown or delisted symbol.
                            return _watch.Decision("halt", "subscription_refused", {"server": text})
                        if message.get("channel") == channel and message.get("type") == "subscribed":
                            state.snapshot(message.get("contents"))
                            break
                    attempt = 0
                    if (over := ended()) is not None:
                        return over
                    decision = take(state.records.get((symbol,)))
                    if decision is not None:
                        return decision
                    while True:
                        if (over := ended()) is not None:
                            return over
                        try:
                            message = await _next(queue, reader, IDLE_CHECK_S)
                        except TimeoutError:
                            decision = idle()
                            if decision is not None:
                                return decision
                            continue
                        # A stop or the deadline that came while waiting wins over the push it waited for.
                        if (over := ended()) is not None:
                            return over
                        if message.get("type") == "error":
                            raise ValueError("The server reported a subscription failure")
                        if message.get("channel") != channel or message.get("type") != "channel_data":
                            continue
                        state.update(message)
                        decision = take(state.records.get((symbol,)))
                        if decision is not None:
                            return decision
                finally:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
        except (
            ValueError,
            KeyError,
            TypeError,
            ConnectionError,
            TimeoutError,
            aiohttp.ClientError,
            asyncio.QueueFull,
        ) as exc:
            decision = idle()
            if decision is not None:
                return decision
            wait = min(RECONNECT_BASE_S * 2**attempt, RECONNECT_MAX_S)
            on_event({"event": "reconnecting", "error": str(exc) or type(exc).__name__, "in_s": wait})
            attempt += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.0, min(wait, deadline - clock())))
            except TimeoutError:
                pass


# ---------------------------------------------------------------------- trigger


def _rules(args) -> tuple[_watch.Level | None, _watch.FundingRule | None]:
    if not 0 < args.max_hours <= MAX_HOURS:
        raise Refused("invalid_arguments", f"--max-hours must be above 0 and at most {MAX_HOURS:g}")
    if not 1 <= args.funding_window <= 60:
        raise Refused("invalid_arguments", "--funding-window is 1..60 minutes")
    try:
        level = _watch.parse_level(args.mark) if args.mark else None
        funding = _watch.parse_funding(args.funding, args.funding_window) if args.funding else None
    except ArithmeticError:
        raise Refused("invalid_arguments", "--mark and --funding need numbers") from None
    except ValueError as exc:
        raise Refused("invalid_arguments", str(exc)) from None
    if level is None and funding is None:
        raise Refused("invalid_arguments", "Give --mark, --funding or both")
    return level, funding


async def run_trigger(args, *, connect: Callable | None = None, clock: Callable[[], float] = time.time) -> int:
    started = clock()
    deadline = started + args.max_hours * 3600
    base = {**({"id": args.id} if args.id else {}), "symbol": args.symbol}

    def on_event(event: dict) -> None:
        emit({**base, **event})

    def end(event: str, **fields) -> int:
        emit({**base, "event": event, **fields})
        ok = event in ("expired", "stopped") or (event == "fired" and not fields.get("error"))
        return 0 if ok else 1

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):  # no loop signal handlers on Windows or off the main thread
            pass
    try:
        return await _run_trigger(args, stop, base, deadline, end, on_event, connect, clock)
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


async def _run_trigger(args, stop, base, deadline, end, on_event, connect, clock) -> int:
    try:
        api_url = get_api_url()
        if connect is None and api_url not in WS_URLS:
            raise Refused("no_websocket", "This deployment has no verified WebSocket; nothing can be watched here")
        level, funding = _rules(args)
        actions = parse_actions(args.then or [], unprotected_ok=args.unprotected_ok)
        on_event(
            {
                "event": "arming",
                "rule": " and ".join(r.text for r in (level, funding) if r is not None),
                "then": [" ".join(argv) for argv, _ in actions],
                "alert_only": not actions,
                "expires_at_ms": int(deadline * 1000),
            }
        )
        backstops = await backstop_check(actions, unprotected_ok=args.unprotected_ok)
        previews = await preview_actions(actions)
        if actions:
            on_event({"event": "armed", "backstops": backstops, "previews": previews})
    except Refused as exc:
        return end("refused", reason=exc.reason, error=str(exc), **exc.detail)
    except Exception as exc:
        return end("refused", reason="arming_failed", error=str(exc) or type(exc).__name__)

    try:
        decision = await watch_feed(
            api_url,
            args.symbol,
            _watch.Trigger(level, funding),
            deadline=deadline,
            stop=stop,
            on_event=on_event,
            connect=connect,
            clock=clock,
        )
    except Exception as exc:
        return end("halted", reason="feed_error", error=str(exc) or type(exc).__name__)
    if decision.kind == "halt":
        if decision.reason in ("expired", "stopped"):
            return end(decision.reason, **decision.detail)
        messages = {
            "already_triggered": "The level was already reached when the watch started; nothing was sent",
            "crossed_while_stale": "The level was crossed while the feed was stale; nothing was sent",
            "feed_stale": "No fresh price for too long; nothing was sent",
            "subscription_refused": "The server refused the market's price feed (check the symbol); nothing was sent",
        }
        return end("halted", reason=decision.reason, error=messages.get(decision.reason or ""), **decision.detail)
    # Fire: a stop arriving now waits until every action has its outcome.
    results, error = await fire(actions)
    fields: dict[str, Any] = {"trigger": decision.detail, "actions": results}
    if len(actions) > 1:
        fields["legs_atomic"] = False
    if error:
        fields["error"] = error
    return end("fired", **fields)


# ---------------------------------------------------------------------- detach, list, status, stop


def _record_path(watch_id: str) -> Path:
    if not WATCH_ID.match(watch_id):
        raise ValueError("Unknown watch ID")
    return watch_dir() / f"{watch_id}.json"


def _read_events(output: Path) -> list[dict]:
    try:
        lines = output.read_text().splitlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _cmdline(pid: int) -> str | None:
    """The process's command line from /proc (Linux, WSL) or `ps` (macOS); None when neither can tell."""
    proc = Path(f"/proc/{pid}/cmdline")
    try:
        if proc.exists():
            return proc.read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _alive(pid: int, watch_id: str) -> bool | None:
    """True when `pid` runs and is this watch's own process (never signal a reused PID); None when unknown."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # another user's process reused the PID
    except OSError:
        return None
    command = _cmdline(pid)
    if not command:
        return None
    return "agent_kit.scripts.watch" in command and f"--id {watch_id}" in command


def _summary(record: dict) -> dict:
    events = _read_events(Path(record["output"]))
    last = events[-1] if events else None
    alive = None if last is not None and last.get("event") in TERMINAL else _alive(record["pid"], record["id"])
    if last is not None and last.get("event") in TERMINAL:
        state = last["event"]
    elif alive:
        state = "running"
    elif alive is None:
        state = "unknown"  # this computer cannot say whether the process runs: treat it as running
    else:
        state = "died"  # killed, or the computer restarted: it ended without saying why
    return {
        "id": record["id"],
        "symbol": record["symbol"],
        "state": state,
        "rule": record.get("rule"),
        "then": record.get("then"),
        "expires_at_ms": record.get("expires_at_ms"),
        "last_event": last,
        "output": record["output"],
    }


def _records() -> list[dict]:
    records = []
    for path in sorted(watch_dir().glob("*.json")):
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return records


def _child_argv(args, watch_id: str) -> list[str]:
    """The same watch as `args`, in the foreground, under `watch_id`."""
    argv = ["trigger", "--symbol", args.symbol, "--funding-window", str(args.funding_window)]
    argv += ["--max-hours", repr(args.max_hours), "--id", watch_id]
    if args.mark:
        argv += ["--mark", args.mark]
    if args.funding:
        argv += ["--funding", args.funding]
    for command in args.then or []:
        argv += ["--then", command]
    if args.unprotected_ok:
        argv.append("--unprotected-ok")
    return argv


def _normalized(commands: list[str], *, unprotected_ok: bool) -> list[str]:
    """`--then` commands as parsed (no leading `trade`, upper-cased symbols), so equal commands compare equal."""
    return [" ".join(argv).upper() for argv, _ in parse_actions(commands, unprotected_ok=unprotected_ok)]


def detach(args) -> dict:
    """Start the watch in its own session, record it, and wait briefly for its first verdict."""
    level, funding = _rules(args)
    commands = _normalized(args.then or [], unprotected_ok=args.unprotected_ok)
    rule = " and ".join(r.text for r in (level, funding) if r is not None)
    now_ms = int(time.time() * 1000)
    for record in _records():
        try:
            current = _summary(record)
            same = (record["symbol"], record.get("rule")) == (args.symbol, rule) and _normalized(
                record.get("then") or [], unprotected_ok=True
            ) == commands
        except (KeyError, TypeError, ValueError):
            continue  # a malformed record never blocks a new watch
        if current["state"] == "unknown" and (record.get("expires_at_ms") or 0) > now_ms:
            raise Refused(
                "liveness_unknown",
                f"Cannot tell whether watch {record['id']} (pid {record['pid']}) still runs on this computer; check "
                "it, or wait until it expires, before starting another",
                watch=current,
            )
        if current["state"] == "running" and same:
            raise Refused("duplicate", f"Watch {record['id']} already waits for the same rule", watch=current)
    ticker = re.sub(r"[^a-z0-9]", "", args.symbol.lower().removesuffix("rusdperp"))[:16] or "watch"
    watch_id = f"{ticker}-{secrets.token_hex(4)}"
    path = _record_path(watch_id)
    output = path.with_suffix(".jsonl")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as sink:
        process = subprocess.Popen(
            [sys.executable, "-m", "agent_kit.scripts.watch", *_child_argv(args, watch_id)],
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
    record = {
        "id": watch_id,
        "pid": process.pid,
        "symbol": args.symbol,
        "rule": rule,
        "then": args.then or [],
        "started_at_ms": int(time.time() * 1000),
        "expires_at_ms": int((time.time() + args.max_hours * 3600) * 1000),
        "output": str(output),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle)
    waited_until = time.monotonic() + DETACH_WAIT_S
    while time.monotonic() < waited_until:
        events = _read_events(output)
        if any(e.get("event") in ("watching", *TERMINAL) for e in events) or process.poll() is not None:
            break
        time.sleep(0.2)
    return {"event": "detached", **_summary(record), "pid": process.pid}


def list_watches() -> dict:
    watches = []
    for record in _records():
        try:
            watches.append(_summary(record))
        except (KeyError, TypeError):
            continue
    return {"watches": watches}


def status(watch_id: str) -> dict:
    path = _record_path(watch_id)
    if not path.exists():
        raise ValueError(f"No watch {watch_id}; run `watch list`")
    record = json.loads(path.read_text())
    events = _read_events(Path(record["output"]))
    return {**_summary(record), "events": len(events), "recent": events[-5:]}


def stop_watch(watch_id: str) -> dict:
    current = status(watch_id)
    if current["state"] != "running":
        return {**current, "stopped": False, "message": "The watch is not running"}
    record = json.loads(_record_path(watch_id).read_text())
    try:
        os.kill(record["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    until = time.monotonic() + 10
    while time.monotonic() < until and _alive(record["pid"], watch_id):
        time.sleep(0.2)
    alive = _alive(record["pid"], watch_id)
    result = {**status(watch_id), "stopped": not alive}
    if alive:
        result["message"] = (
            "Still running: it waits for the commands it already started to finish (up to about a minute). "
            "Run `watch status` again."
        )
    return result


# ---------------------------------------------------------------------- CLI


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="watch", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    trigger = commands.add_parser("trigger", help="wait for a mark or funding level, then act once (or only report)")
    trigger.add_argument("--symbol", type=symbol_arg, required=True)
    trigger.add_argument("--mark", help="lte:PRICE or gte:PRICE: fires when the mark crosses to it")
    trigger.add_argument("--funding", help="lt|lte|gt|gte:PCT_PER_HOUR on the window's average funding")
    trigger.add_argument("--funding-window", type=int, default=15, help="minutes averaged (1..60, default 15)")
    trigger.add_argument("--then", action="append", help="a kit trade command to run once it fires (at most 2)")
    trigger.add_argument("--unprotected-ok", action="store_true", help="only after the user agreed to no stop")
    trigger.add_argument("--max-hours", type=float, default=4.0, help="ends the watch (above 0, at most 24)")
    trigger.add_argument("--detach", action="store_true", help="run in the background; see `watch list`")
    trigger.add_argument("--id", default=None, help=argparse.SUPPRESS)
    commands.add_parser("list", help="watches started with --detach")
    for name in ("status", "stop"):
        sub = commands.add_parser(name)
        sub.add_argument("watch_id")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    # The kit's .env first, as every other command: REYA_STATE_DIR there decides where watches are recorded.
    load_env()
    try:
        if args.command == "trigger":
            if args.detach:
                result = detach(args)
                print(json.dumps(result, indent=2))
                last = result.get("last_event") or {}
                if result["state"] in ("refused", "halted", "died") or (
                    result["state"] == "fired" and last.get("error")
                ):
                    sys.exit(1)
                return
            if args.id is not None and not WATCH_ID.match(args.id):
                raise ValueError("Invalid watch ID")
            sys.exit(asyncio.run(run_trigger(args)))
        result = {
            "list": list_watches,
            "status": lambda: status(args.watch_id),
            "stop": lambda: stop_watch(args.watch_id),
        }[args.command]()
        print(json.dumps(result, indent=2))
    except Refused as exc:
        print(json.dumps({"event": "refused", "reason": exc.reason, "error": str(exc), **exc.detail}, indent=2))
        sys.exit(1)
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc) or type(exc).__name__}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
