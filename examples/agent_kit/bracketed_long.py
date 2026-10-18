#!/usr/bin/env python3
"""
Example agent workflow: open a $100 long with TP/SL brackets attached.

This is a "defensive bracketed entry" — the kind of small SOP a risk-conscious
agent runs end-to-end:

    1. Read market structure (min order qty, tick size, etc.) for sanity.
    2. Read the current mark price as a TP/SL anchor.
    3. Open a market long for a fixed notional.
    4. Immediately attach a take-profit and a stop-loss.
    5. If either bracket fails to attach AFTER the position opened,
       flatten the position with `positions close_all` rather than
       leave it unprotected.
    6. Verify with `query positions` and `query orders`.

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
from decimal import Decimal

from agent_kit.scripts._account_data import AccountDataClient
from agent_kit.scripts._env import get_trading_config
from agent_kit.scripts._settlement import read_settlement

# Edit these to point at a different market or change risk parameters.
SYMBOL = "BTCRUSDPERP"
NOTIONAL_USD = 100
TP_PCT = 0.03  # take-profit at +3% from the mark after entry
SL_PCT = 0.02  # stop-loss at -2% from the mark after entry
TRIGGER_SLIPPAGE = 0.001  # 0.1% child limit offset; must fit the venue band

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bracketed_long")


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
    if entry is None or entry.get("status") != "FILLED":
        raise RuntimeError("Entry outcome is unknown; inspect the original order/fills/busts, never resubmit")
    evidence = settlement_evidence(entry)
    if evidence["state"] != "settled_exposure" or Decimal(evidence["net_settled_qty"].get(SYMBOL, "0")) != Decimal(
        entry["filled_qty"]
    ):
        raise RuntimeError("Entry settlement remains unresolved; empty account reads are not proof of nonexecution")
    positions = run_cli("agent_kit.scripts.query", "positions", "--symbol", SYMBOL)
    if not positions:
        raise RuntimeError("Settled entry is not visible in account reads yet; operator reconciliation required")


def flatten_for_safety(reason: str, entry=None) -> None:
    """Attempt one close only for a positively settled/visible entry; never retry."""
    logger.error("Entry or bracket failed: %s", reason)
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
        logger.exception("Outcome remains unresolved — manual intervention required; no submission will be retried")
    sys.exit(1)


def main() -> None:
    positions = run_cli("agent_kit.scripts.query", "positions", "--symbol", SYMBOL)
    orders = run_cli("agent_kit.scripts.query", "orders", "--symbol", SYMBOL)
    if positions or orders:
        raise RuntimeError("This example requires no existing position or orders on " + SYMBOL)

    # Step 1: Read market structure. Useful for an agent to sanity-check before trading.
    info = run_cli("agent_kit.scripts.query", "market", "info", "--symbol", SYMBOL)
    logger.info("Market info:\n%s", json.dumps(info, indent=2))

    # Step 2/3: Open once. Even a failed IOC response can have a partial fill.
    open_order = None
    try:
        open_order = run_cli(
            "agent_kit.scripts.trade",
            "order",
            "market",
            SYMBOL,
            "--side",
            "long",
            "--notional",
            str(NOTIONAL_USD),
        )
        logger.info("Entry execution response:\n%s", json.dumps(open_order, indent=2))
        require_visible_settled_entry(open_order)
        price = run_cli("agent_kit.scripts.query", "prices", "--symbol", SYMBOL)
        mark_price = float(price["mark_price_usd"])
    except Exception as exc:
        flatten_for_safety(f"Entry outcome requires reconciliation: {exc}", open_order)
        return

    # Step 4: Choose trigger and child limit separately. These are not guaranteed fills.
    tp_px = mark_price * (1 + TP_PCT)
    sl_px = mark_price * (1 - SL_PCT)
    logger.info(
        "Brackets — TP $%.2f (+%.1f%%)  SL $%.2f (-%.1f%%)",
        tp_px,
        TP_PCT * 100,
        sl_px,
        SL_PCT * 100,
    )

    # Step 5: Attach TP and SL. If either fails, flatten for safety.
    try:
        tp_order = run_cli(
            "agent_kit.scripts.trade",
            "order",
            "tp",
            SYMBOL,
            "--side",
            "long",
            "--trigger_px",
            str(tp_px),
            "--limit_px",
            str(tp_px * (1 - TRIGGER_SLIPPAGE)),
            "--time_in_force",
            "IOC",
        )
        logger.info("TP attached:\n%s", json.dumps(tp_order, indent=2))
    except Exception as exc:
        flatten_for_safety(f"TP placement failed: {exc}", open_order)

    try:
        sl_order = run_cli(
            "agent_kit.scripts.trade",
            "order",
            "sl",
            SYMBOL,
            "--side",
            "long",
            "--trigger_px",
            str(sl_px),
            "--limit_px",
            str(sl_px * (1 - TRIGGER_SLIPPAGE)),
            "--time_in_force",
            "IOC",
        )
        logger.info("SL attached:\n%s", json.dumps(sl_order, indent=2))
    except Exception as exc:
        flatten_for_safety(f"SL placement failed (TP was already attached): {exc}", open_order)

    # Step 6: Verify.
    positions = run_cli("agent_kit.scripts.query", "positions", "--symbol", SYMBOL)
    orders = run_cli("agent_kit.scripts.query", "orders", "--symbol", SYMBOL)
    logger.info("Position state:\n%s", json.dumps(positions, indent=2))
    logger.info("Open trigger orders:\n%s", json.dumps(orders, indent=2))


if __name__ == "__main__":
    main()
