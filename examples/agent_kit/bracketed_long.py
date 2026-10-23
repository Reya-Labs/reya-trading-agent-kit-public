#!/usr/bin/env python3
"""
Example agent workflow: open a $100 long with its stop-loss and take-profit attached.

This is a "defensive bracketed entry" — the kind of small SOP a risk-conscious
agent runs end-to-end:

    1. Read market structure (min order qty, tick size, etc.) for sanity.
    2. Preview the entry and its protection; stop on any refusal.
    3. Send ONE `order market --sl --tp`. The command waits until position reads
       include the fill, arms the stop-loss, then the take-profit, reads both
       back and reports `protection`.
    4. If the entry filled but no stop-loss is armed, flatten the position with
       `positions close_all` rather than leave it unprotected, and say so loudly
       when even that cannot be confirmed.
    5. Verify with `query positions` and `query orders`.

All interaction with the kit goes through the CLI scripts via subprocess —
the same surface an AI agent reading SKILL.md would invoke.

Tune the constants below to point at whichever perp market you want.

Requirements:
    - A kit set up with `agent init`: `.env` at the repo root then holds the owner
      (PERP_WALLET_ADDRESS_1), the agent key and the Agent account (PERP_ACCOUNT_ID_1).
      No private key of yours is needed; REYA_SIGNER=own-key with PERP_PRIVATE_KEY_FILE
      is the deliberate alternative.
    - No open position or orders on SYMBOL when starting (the example assumes a clean slate).

Usage:
    python -m examples.agent_kit.bracketed_long
"""

from typing import Any

import asyncio
import json
import logging
import subprocess
import sys
import time
from decimal import Decimal

from agent_kit.scripts._account_data import AccountDataClient
from agent_kit.scripts._env import get_trading_config
from agent_kit.scripts._settlement import read_settlement

# Edit these to point at a different market or change risk parameters.
SYMBOL = "BTCRUSDPERP"
NOTIONAL_USD = 100
SL_LEVEL = "2%"  # stop-loss 2% below the entry's average fill price (or a price, e.g. "58000")
TP_LEVEL = "3%"  # take-profit 3% above it
POSITION_READS = 10  # bounded re-reads, 0.5 s apart, before calling a settled entry invisible

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bracketed_long")


def run_cli_result(*args: str) -> tuple[int, Any]:
    """Invoke an agent kit CLI command; return its exit code and parsed JSON (None if unparsable).

    A write that exits 1 still prints its full result (fills, protection), so read it.
    """
    cmd = [sys.executable, "-m", *args]
    logger.info("$ %s", " ".join(cmd[2:]))
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return result.returncode, json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.returncode, None


def run_cli(*args: str) -> Any:
    """Invoke an agent kit CLI command and return its parsed JSON output.

    Raises RuntimeError on non-zero exit or when the output isn't valid JSON.
    """
    cmd = [sys.executable, "-m", *args]
    logger.info("$ %s", " ".join(cmd[2:]))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {' '.join(args)}\n"
            f"stderr: {result.stderr.strip()}\n"
            f"stdout: {result.stdout.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse JSON from {' '.join(args)}: {exc}\nstdout: {result.stdout}")


def settlement_evidence(entry, closes=()):
    """Read positive fill/bust evidence; empty positions alone prove nothing."""
    acknowledgements = [
        {
            "symbol": result["symbol"],
            "order_id": result["order_id"],
            "status": result["status"],
            "filled_qty": result.get("filled_qty"),
            "first_fill_id": result.get("first_fill_id"),
            "side": side,
        }
        for result, side in [(entry, "B"), *((close, "A") for close in closes)]
    ]

    async def read():
        async with AccountDataClient(get_trading_config(include_private_key=False)) as client:
            return await asyncio.wait_for(read_settlement(client, acknowledgements), timeout=6)

    return asyncio.run(read())


def require_visible_settled_entry(entry):
    """Positive settlement evidence, then a position read that shows it (bounded re-reads)."""
    filled = Decimal((entry or {}).get("filled_qty") or "0")
    # A partly filled IOC ends CANCELLED with its fill; settlement evidence covers both.
    if entry is None or entry.get("status") not in ("FILLED", "CANCELLED") or filled <= 0:
        raise RuntimeError("Entry outcome is unknown; inspect the original order/fills/busts, never resubmit")
    evidence = settlement_evidence(entry)
    if evidence["state"] != "settled_exposure" or Decimal(evidence["net_settled_qty"].get(SYMBOL, "0")) != filled:
        raise RuntimeError("Entry settlement remains unresolved; empty account reads are not proof of nonexecution")
    # Position reads trail fills; one empty read proves nothing.
    for attempt in range(POSITION_READS):
        if run_cli("agent_kit.scripts.query", "positions", "--symbol", SYMBOL):
            return
        if attempt < POSITION_READS - 1:
            time.sleep(0.5)
    raise RuntimeError("Settled entry is still not visible in account reads; operator reconciliation required")


def flatten_for_safety(reason: str, entry=None) -> None:
    """Attempt one close only for a positively settled/visible entry; never retry."""
    logger.error("Entry or protection failed: %s", reason)
    logger.error("Position is UNPROTECTED — flattening %s for safety.", SYMBOL)
    try:
        require_visible_settled_entry(entry)
        flatten = run_cli(
            "agent_kit.scripts.trade",
            "positions",
            "close_all",
            "--symbol",
            SYMBOL,
        )
        logger.error("Flatten result:\n%s", json.dumps(flatten, indent=2))
        evidence = settlement_evidence(entry, flatten.get("close_orders", []))
        if evidence["state"] != "settled_flat":
            raise RuntimeError("Close is not settled flat; retain its acknowledgement and reconcile manually")
        run_cli("agent_kit.scripts.trade", "order", "cancel_all", "--symbol", SYMBOL)
    except Exception:
        # Last-ditch: if even close_all fails, the operator MUST intervene manually.
        logger.exception(
            "Any %s position may be UNPROTECTED and was not closed — manual intervention required; "
            "no submission will be retried. %s",
            SYMBOL,
            reason,
        )
    sys.exit(1)


def main() -> None:
    positions = run_cli("agent_kit.scripts.query", "positions", "--symbol", SYMBOL)
    orders = run_cli("agent_kit.scripts.query", "orders", "--symbol", SYMBOL)
    if positions or orders:
        raise RuntimeError("This example requires no existing position or orders on " + SYMBOL)

    # Step 1: Read market structure. Useful for an agent to sanity-check before trading.
    info = run_cli("agent_kit.scripts.query", "market", "info", "--symbol", SYMBOL)
    logger.info("Market info:\n%s", json.dumps(info, indent=2))

    # Step 2: Preview the entry and its protection. Nothing is sent.
    entry = ["agent_kit.scripts.trade", "order", "market", SYMBOL, "--side", "long", "--notional", str(NOTIONAL_USD)]
    entry += ["--sl", SL_LEVEL, "--tp", TP_LEVEL]
    _, preview = run_cli_result(*entry, "--preview")
    logger.info("Preview:\n%s", json.dumps(preview, indent=2))
    if not preview or preview.get("refused_by") or not preview.get("would_send"):
        logger.error("Entry refused or preview unavailable; nothing was sent.")
        sys.exit(1)

    # Step 3: Send once. Even a failed IOC response can carry a partial fill, and the
    # command reports what it armed for whatever filled.
    _, result = run_cli_result(*entry)
    logger.info("Entry and protection:\n%s", json.dumps(result, indent=2))
    protection = (result or {}).get("protection") or {}
    state = protection.get("state")
    if state == "not_placed":
        logger.info("The entry did not fill; nothing to protect.")
        sys.exit(1)
    if not (protection.get("stop_loss") or {}).get("armed"):
        # Step 4: filled (or unknown) with no stop-loss armed: never leave it like that.
        flatten_for_safety(protection.get("message") or (result or {}).get("error") or "no result", result)
        return
    if state != "protected":
        logger.warning("Stop-loss armed, but: %s", protection.get("message"))

    # Step 5: Verify.
    positions = run_cli("agent_kit.scripts.query", "positions", "--symbol", SYMBOL)
    orders = run_cli("agent_kit.scripts.query", "orders", "--symbol", SYMBOL)
    logger.info("Position state:\n%s", json.dumps(positions, indent=2))
    logger.info("Open trigger orders:\n%s", json.dumps(orders, indent=2))
    if state != "protected":
        sys.exit(1)


if __name__ == "__main__":
    main()
