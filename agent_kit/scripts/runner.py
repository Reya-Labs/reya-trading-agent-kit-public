"""A bounded, single-writer JSON-lines trading session for a dedicated account.

The agent supplies normal trade CLI arguments; the runner keeps the signer,
nonce lease, conservative submission budget and venue cancellation heartbeat.
"""

import asyncio
import json
import os
import re
import signal
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from pathlib import Path

from sdk.open_api.models.order_status import OrderStatus

from agent_kit.scripts import trade
from agent_kit.scripts._account_data import AccountDataClient
from agent_kit.scripts._common import (
    JsonArgumentParser,
    capture_output,
    exception_result,
    output_json,
    output_result,
    require_owned_account,
    require_price,
)
from agent_kit.scripts._env import get_trading_config
from agent_kit.scripts._orders import order_result
from agent_kit.scripts._settlement import read_settlement
from agent_kit.scripts._writer import CoordinatedTradingClient

PROTECTIVE_ORDER_TYPES = ("TAKE_PROFIT", "STOP_LOSS")
# Guardrail overrides the runner never accepts: its policy is the operator's limit.
OVERRIDE_FLAGS = ("--force", "--allow_large")


@dataclass(frozen=True)
class Policy:
    symbols: frozenset[str]
    max_order_notional: Decimal
    max_session_notional: Decimal
    max_slippage: Decimal
    max_commands: int
    idle_timeout_s: int

    @classmethod
    def load(cls, path: Path):
        data = json.loads(path.read_text())
        fields = {
            "symbols",
            "max_order_notional",
            "max_session_notional",
            "max_slippage",
            "max_commands",
            "idle_timeout_s",
        }
        if not isinstance(data, dict) or set(data) != fields:
            raise ValueError("Policy must contain exactly: " + ", ".join(sorted(fields)))
        symbols = data["symbols"]
        if (
            not isinstance(symbols, list)
            or not symbols
            or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]+RUSDPERP", s) for s in symbols)
        ):
            raise ValueError("Policy requires a nonempty list of perp symbols")
        values = [Decimal(str(data[k])) for k in ("max_order_notional", "max_session_notional", "max_slippage")]
        if any(not v.is_finite() or v <= 0 for v in values) or values[0] > values[1] or values[2] > Decimal("0.05"):
            raise ValueError("Positive budgets, order <= session and slippage <= 0.05 are required")
        if type(data["max_commands"]) is not int or not 1 <= data["max_commands"] <= 1000:
            raise ValueError("max_commands must be 1..1000")
        if type(data["idle_timeout_s"]) is not int or not 10 <= data["idle_timeout_s"] <= 3600:
            raise ValueError("idle_timeout_s must be 10..3600")
        return cls(frozenset(symbols), values[0], values[1], values[2], data["max_commands"], data["idle_timeout_s"])


async def flat_account(client) -> dict:
    account_id = await require_owned_account(client)
    orders = [o for o in await client.get_open_orders() if o.account_id == account_id]
    positions = [p for p in await client.get_positions() if p.account_id == account_id and Decimal(p.qty) != 0]
    return {
        "account_id": account_id,
        "open_order_ids": [o.order_id for o in orders],
        "orders": [order_result(o) for o in orders],
        "positions": [{"symbol": p.symbol, "side": p.side.value, "qty": p.qty} for p in positions],
    }


def position_protection(state: dict) -> list | None:
    """For each remaining position, the stop-loss and take-profit still armed for it (None when unknown)."""
    if state.get("positions") is None or state.get("orders") is None:
        return None
    result = []
    for position in state["positions"]:
        side = "long" if position["side"] == "B" else "short"
        entry = {"symbol": position["symbol"], "position_side": side, "qty": position["qty"]}
        for order_type, key in (("STOP_LOSS", "stop_loss"), ("TAKE_PROFIT", "take_profit")):
            armed = next(
                (
                    o
                    for o in state["orders"]
                    if o["symbol"] == position["symbol"]
                    and o["order_type"] == order_type
                    and o.get("position_side") == side
                ),
                None,
            )
            entry[key] = armed and {k: armed[k] for k in ("order_id", "trigger_px", "limit_px")}
        result.append(entry)
    return result


def exposure_message(protection: list) -> str:
    parts = []
    for p in protection:
        kept = [name for name, key in (("stop-loss", "stop_loss"), ("take-profit", "take_profit")) if p[key]]
        status = " and ".join(kept) + " kept" if kept else "NO stop-loss or take-profit"
        parts.append(f"{p['symbol']} {p['position_side']} {p['qty']} ({status})")
    return (
        "Position left open: "
        + "; ".join(parts)
        + ". The runner never closes positions; close it explicitly or confirm its protection, then reconcile before another session"
    )


class RunnerClient(CoordinatedTradingClient):
    def configure(self, policy: Policy):
        self.policy = policy
        self.submitted = Decimal(0)
        self.operation_lock = asyncio.Lock()
        self.write_attempts = 0
        self.heartbeat_failed = False
        self.heartbeat_deadline: float | None = None
        self.ready = False

    async def start(self):
        if not self.ready:
            await super().start()
            self.ready = True

    def check_heartbeat(self):
        if self.heartbeat_deadline is not None and time.monotonic() >= self.heartbeat_deadline:
            self.heartbeat_failed = True
        if self.heartbeat_failed:
            raise ValueError("Cancellation heartbeat failed or expired; trading is halted for reconciliation")

    async def _write(self, call, *, cleanup=False, record_attempt=True):
        async with self.operation_lock:
            if not cleanup:
                self.check_heartbeat()
            if record_attempt:
                self.write_attempts += 1
            return await asyncio.wait_for(call(), timeout=5)

    async def _reserve(self, params):
        if params.symbol not in self.policy.symbols:
            raise ValueError("Symbol is outside the runner policy")
        qty = require_price(params.qty, "quantity")
        snapshot = await asyncio.wait_for(self.markets.get_perp_market_summary(symbol=params.symbol), timeout=3)
        if snapshot.prices_updated_at is None or not -5000 <= time.time() * 1000 - snapshot.prices_updated_at <= 30000:
            raise ValueError("Market price is unavailable or stale; no order submitted")
        price = max(require_price(params.limit_px, "limit price"), require_price(snapshot.mark_price, "mark price"))
        # Charge the full intended order, including modifies, BEFORE submission.
        # Never release budget on cancellation or lagging position/order reads.
        # If extreme precision requires rounding, charge upwards, never past a cap.
        with localcontext() as context:
            context.prec = 80
            context.rounding = ROUND_CEILING
            notional = qty * price
            submitted = self.submitted + notional
        if not params.reduce_only and notional > self.policy.max_order_notional:
            raise ValueError("Order exceeds max_order_notional")
        if not params.reduce_only:
            if submitted > self.policy.max_session_notional:
                raise ValueError(
                    "Session submission budget exhausted; reduce-only closes/cancellations remain available"
                )
            self.submitted = submitted
            self.lease.state["runner"]["submitted_notional"] = str(self.submitted)
            self.lease.save()

    async def create_limit_order(self, params):
        await self._reserve(params)
        response = await self._write(lambda: super(RunnerClient, self).create_limit_order(params))
        self.record_acknowledgement(params, response)
        return response

    async def create_trigger_order(self, params):
        if params.symbol not in self.policy.symbols:
            raise ValueError("Symbol is outside the runner policy")
        response = await self._write(lambda: super(RunnerClient, self).create_trigger_order(params))
        self.record_acknowledgement(params, response)
        return response

    async def modify_order(self, params):
        await self._reserve(params)
        response = await self._write(lambda: super(RunnerClient, self).modify_order(params))
        self.record_acknowledgement(params, response)
        return response

    def record_acknowledgement(self, params, response):
        # Persist before any optional fee/position reads. A crash after sending
        # but before this save still leaves the request's durable intent pending.
        ack = {
            "symbol": params.symbol,
            "order_id": response.order_id,
            "side": "B" if params.is_buy else "A",
            "status": response.status.value,
            "filled_qty": response.cum_qty,
            "first_fill_id": response.first_fill_id,
        }
        self.lease.state["runner"].setdefault("acknowledged_orders", []).append(ack)
        self.lease.save()

    async def cancel_order(self, **kwargs):
        return await self._write(lambda: super(RunnerClient, self).cancel_order(**kwargs))

    async def refresh(self):
        started = time.monotonic()
        response = await self._write(
            lambda: super(RunnerClient, self).cancel_all_after(timeout_ms=30000), record_attempt=False
        )
        if response.account_id != self.config.account_id or response.timeout_ms != 30000 or response.trigger_at is None:
            raise ValueError("Venue did not confirm the cancellation countdown")
        # Measure conservatively from before submission, never from a late reply.
        # Check the OLD deadline before accepting any renewal after a stall.
        self.check_heartbeat()
        self.heartbeat_deadline = started + 30
        self.check_heartbeat()

    async def disarm(self):
        response = await self._write(
            lambda: super(RunnerClient, self).cancel_all_after(timeout_ms=0), record_attempt=False
        )
        if response.account_id != self.config.account_id or response.timeout_ms != 0 or response.trigger_at is not None:
            raise ValueError("Venue did not confirm countdown disarm")

    async def cleanup(self) -> dict:
        """Cancel the session's working orders without stripping a position's TP/SL.

        A user cancelAll also clears armed TP/SL in the matching engine (only the
        venue countdown's own fire keeps them), so it is used only when the account
        is visibly flat with no TP/SL. Otherwise, including when a read fails, each
        other order is cancelled individually and TP/SL stay armed; the countdown is
        then left armed and sweeps anything these reads missed, still keeping TP/SL.
        """
        account_id = self.config.account_id

        async def read(call):
            try:
                return await asyncio.wait_for(call(), timeout=5)
            except Exception:
                return None  # Unknown: treated as possibly protecting a position.

        positions, orders = await asyncio.gather(read(self.get_positions), read(self.get_open_orders))
        open_positions = (
            None if positions is None else [p for p in positions if p.account_id == account_id and Decimal(p.qty) != 0]
        )
        own_orders = None if orders is None else [o for o in orders if o.account_id == account_id]
        protective = [o for o in own_orders or [] if o.order_type.value in PROTECTIVE_ORDER_TYPES]
        if open_positions == [] and own_orders is not None and not protective:
            # Nothing to protect: one account-wide cancel also covers orders the read missed.
            await self._write(lambda: super(RunnerClient, self).mass_cancel(), cleanup=True)
            return {"mode": "cancel_all"}
        report: dict = {
            "mode": "kept_protection",
            "kept_order_ids": [o.order_id for o in protective],
            "cancelled_order_ids": [],
            "errors": [],
        }
        if own_orders is None:
            report["errors"].append({"error": "Open orders unreadable; nothing was cancelled"})
            return report
        for order in own_orders:
            if order in protective:
                continue
            try:
                response = await self._write(
                    lambda o=order: super(RunnerClient, self).cancel_order(
                        symbol=o.symbol, account_id=account_id, order_id=o.order_id
                    ),
                    cleanup=True,
                )
                if response.status != OrderStatus.CANCELLED:
                    raise ValueError(f"Cancellation not confirmed: {response.status}")
                report["cancelled_order_ids"].append(order.order_id)
            except Exception as exc:
                report["errors"].append({"order_id": order.order_id, **exception_result(exc)})
        return report


async def heartbeat(client, failed: asyncio.Event):
    try:
        while True:
            await asyncio.sleep(5)
            await client.refresh()
    except asyncio.CancelledError:
        raise
    except Exception:
        client.heartbeat_failed = True
        failed.set()


def emit(value: dict):
    print(json.dumps(value, separators=(",", ":")), flush=True)


async def execute_request(client, request: dict, intent: dict | None = None) -> dict:
    command = request.get("command")
    if (
        not isinstance(command, list)
        or not 1 <= len(command) <= 40
        or any(not isinstance(x, str) or len(x) > 128 for x in command)
    ):
        raise ValueError("command must be a bounded array of trade CLI arguments")
    if any(x in ("--help", "-h", *OVERRIDE_FLAGS) for x in command):
        raise ValueError("Help and guardrail overrides are unavailable inside the runner")
    with capture_output() as output:
        try:
            args = trade.build_parser().parse_args(command)
        except SystemExit:
            return output[-1] if output else {"error": "Invalid command"}
        if getattr(args, "force", False) or getattr(args, "allow_large", False):
            raise ValueError("Runner guardrail overrides are unavailable")
        if hasattr(args, "slippage") and args.slippage > client.policy.max_slippage:
            raise ValueError("Slippage exceeds the runner policy")
        if getattr(args, "symbol", None) and args.symbol not in client.policy.symbols:
            raise ValueError("Symbol is outside the runner policy")
        if intent is not None:
            intent["command"] = command
            client.lease.save()
        function = getattr(trade, f"cmd_{args.group}_{args.action}")
        token = trade._shared_client.set(client)

        async def invoke():
            try:
                await function(args)
            except SystemExit:
                # Catch inside the task: SystemExit escaping a task stops the loop.
                # Keep structured partial/unknown outcomes, including fill IDs.
                pass

        # A protected entry also waits for its position and arms two triggers after the fill;
        # cutting it off between the fill and the stop would leave the position unprotected.
        timeout = 30 if trade.protection_requested(args) else 15
        try:
            await asyncio.wait_for(invoke(), timeout=timeout)
        except TimeoutError:
            if not output:
                raise
            return {
                **output[-1],
                "error": "Command timed out after an acknowledgement; reconcile account state before another write",
            }
        finally:
            trade._shared_client.reset(token)
        return output[-1] if output else {"error": "Command returned no outcome"}


async def serve(reader: asyncio.StreamReader, client, policy: Policy, *, ack_previous: str | None = None) -> dict:
    await asyncio.wait_for(client.start(), timeout=15)
    initial = await asyncio.wait_for(flat_account(client), timeout=15)
    if initial["positions"] or initial["open_order_ids"]:
        raise ValueError("Runner requires a dedicated account with no positions or open orders at startup")
    previous = client.lease.state.get("runner")
    if previous and not previous.get("reconciled") and ack_previous != previous["session_id"]:
        raise ValueError(
            f"Prior session {previous['session_id']} needs operator reconciliation; inspect its journal and account before --ack-previous-session"
        )
    # Flat reads alone cannot prove an ambiguous prior submission never reached the venue.
    session_id = str(uuid.uuid4())
    journal = {
        "session_id": session_id,
        "account_id": client.config.account_id,
        "reconciled": False,
        "submitted_notional": "0",
        "requests": [],
        "acknowledged_orders": [],
        "acknowledged_previous": ack_previous,
    }
    client.lease.state["runner"] = journal
    client.lease.save()
    failed = asyncio.Event()
    monitor = None
    failure_wait = None
    pending_read = None
    reason = "eof"
    try:
        await client.refresh()  # Do not admit a single command before the venue arms the timer.
        monitor = asyncio.create_task(heartbeat(client, failed))
        failure_wait = asyncio.create_task(failed.wait())
        emit(
            {
                "event": "ready",
                "journal_path": str(client.lease.directory / (client.lease.keys[0] + ".json")),
                "session_id": session_id,
                "account_id": client.config.account_id,
                "cancel_all_after_ms": 30000,
                "max_session_notional": str(policy.max_session_notional),
            }
        )
        seen: set[str] = set()
        request_count = 0
        while request_count < policy.max_commands:
            pending_read = asyncio.create_task(reader.readline())
            done, _ = await asyncio.wait(
                {pending_read, failure_wait},
                timeout=policy.idle_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            try:
                client.check_heartbeat()
            except ValueError:
                reason = "heartbeat_failed"
                break
            if failure_wait in done:
                reason = "heartbeat_failed"
                break
            if pending_read not in done:
                reason = "idle_timeout"
                break
            line = pending_read.result()
            if not line:
                break
            if len(line) > 8192:
                reason = "oversized_request"
                break
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"id", "command"}:
                raise ValueError("Requests must contain exactly id and command")
            request_id = request["id"]
            if (
                not isinstance(request_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id)
                or request_id in seen
            ):
                raise ValueError("Request ID must be unique in this session, using 1..80 letters/digits/_/-")
            seen.add(request_id)
            request_count += 1
            if request["command"] == ["stop"]:
                reason = "stop"
                break
            if request["command"] == ["status"]:
                emit(
                    {
                        "id": request_id,
                        "result": await asyncio.wait_for(flat_account(client), timeout=10),
                        "submitted_notional": str(client.submitted),
                    }
                )
                continue
            entry: dict = {"id": request_id, "state": "intent"}
            journal["requests"].append(entry)
            client.lease.save()  # Intent survives a crash; it is never automatically replayed.
            before = client.write_attempts
            try:
                result = await execute_request(client, request, entry)
            except ValueError as exc:
                result = {"error": str(exc)}
            except Exception as exc:
                result = exception_result(
                    exc, fallback=f"{type(exc).__name__}: command failed; inspect account state before another write"
                )
            entry.update(state="completed", result=result)
            client.lease.save()
            emit({"id": request_id, "result": result, "submitted_notional": str(client.submitted)})
            try:
                client.check_heartbeat()
            except ValueError:
                reason = "heartbeat_failed"
                break
            if result.get("error") and client.write_attempts > before:
                reason = "write_requires_reconciliation"
                break
        else:
            reason = "command_limit"
    except (Exception, asyncio.CancelledError):
        reason = "interrupted_or_failed"
    finally:
        for task in (monitor, failure_wait, pending_read):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (monitor, failure_wait, pending_read) if t), return_exceptions=True)
        # Leave the server countdown armed even if local cleanup/readback fails.
        cleanup_error = None
        cleanup: dict = {"mode": "unconfirmed"}
        try:
            cleanup = await client.cleanup()
        except Exception:
            cleanup_error = "Immediate cancellation unconfirmed; venue countdown remains armed"
        if cleanup.get("errors"):
            cleanup_error = (
                "Some orders were not confirmed cancelled; the venue countdown remains armed and cancels them, "
                "keeping TP/SL"
            )
        try:
            settlement = await asyncio.wait_for(read_settlement(client, journal["acknowledged_orders"]), timeout=6)
        except Exception:
            settlement = {
                "state": "unresolved",
                "reason": "Settlement read budget exhausted; inspect session fills/busts",
            }
        journal["settlement"] = settlement
        client.lease.save()
        try:
            final = await asyncio.wait_for(flat_account(client), timeout=10)
        except Exception:
            final = {"positions": None, "open_order_ids": None, "orders": None}
        journal["reconciled"] = (
            reason in ("eof", "stop", "command_limit", "idle_timeout")
            and cleanup_error is None
            and settlement["state"] == "settled_flat"
            and final["positions"] == []
            and final["open_order_ids"] == []
            and all(entry["state"] == "completed" for entry in journal["requests"])
        )
        if journal["reconciled"]:
            try:
                await client.disarm()
            except Exception:
                cleanup_error = "Countdown disarm unconfirmed; wait for expiry and reconcile before new orders"
                journal["reconciled"] = False
        protection = position_protection(final)
        journal.update(stop_reason=reason, final_state=final, cleanup_error=cleanup_error, cleanup=cleanup)
        client.lease.save()
    result = {
        "event": "stopped",
        "session_id": session_id,
        "reason": reason,
        "final_state": final,
        "reconciled": journal["reconciled"],
        "settlement": journal["settlement"],
        "positions_auto_closed": False,
        "cleanup": cleanup,
        "protection": protection,
        "countdown_status": "disarmed" if journal["reconciled"] else "may_be_armed",
    }
    if not journal["reconciled"]:
        problems = [cleanup_error, protection and exposure_message(protection)]
        result["error"] = " ".join(p for p in problems if p) or (
            "Inspect remaining exposure and journal before starting another session"
        )
    emit(result)
    return result


async def run(args):
    policy = Policy.load(args.policy)
    if not args.execute:
        async with AccountDataClient(get_trading_config(include_private_key=False)) as client:
            state = await asyncio.wait_for(flat_account(client), timeout=15)
            output_json(
                {
                    "preview": True,
                    **state,
                    "symbols": sorted(policy.symbols),
                    "max_order_notional": str(policy.max_order_notional),
                    "max_session_notional": str(policy.max_session_notional),
                    "requires_dedicated_flat_account": True,
                    "account_wide_cancellation": True,
                }
            )
        return
    if not args.ack_account_wide_cancel:
        raise ValueError("Live runner requires --ack-account-wide-cancel on a dedicated account")
    # asyncio cannot register regular files or /dev/null with its pipe selector.
    # Reject before constructing a signer or arming the account timer.
    mode = os.fstat(sys.stdin.fileno()).st_mode
    if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or sys.stdin.isatty()):
        raise ValueError(
            "Runner stdin must be a pipe or terminal; pipe a command file with cat instead of redirecting it"
        )
    async with RunnerClient(get_trading_config(require_private_key=True)) as client:
        client.configure(policy)
        reader = asyncio.StreamReader(limit=8192)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            result = await serve(reader, client, policy, ack_previous=args.ack_previous_session)
            if result.get("error"):
                raise SystemExit(1)
        finally:
            transport.close()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)


def build_parser():
    parser = JsonArgumentParser(prog="runner", description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="Start the live session; otherwise preview reads only")
    parser.add_argument("--ack-account-wide-cancel", action="store_true")
    parser.add_argument(
        "--ack-previous-session", help="Exact session ID, only after manually reconciling a failed/unfinished run"
    )
    return parser


def main():
    try:
        asyncio.run(run(build_parser().parse_args()))
    except Exception as exc:
        output_result(exception_result(exc))


if __name__ == "__main__":
    main()
