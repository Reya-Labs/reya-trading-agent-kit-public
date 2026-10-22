#!/usr/bin/env python3
"""
Example agent workflow: long $100 of ETH if BTC > $70k, else short.

This script demonstrates how an AI agent would actually use the Reya Agent Kit —
by shelling out to the `query` and `trade` CLI scripts and parsing their JSON
output. No SDK imports, no shared helpers. The pattern is read → decide → write
→ verify.

Requirements:
    - A kit set up with `agent init`: `.env` at the repo root then holds the owner
      (PERP_WALLET_ADDRESS_1), the agent key and the Agent account (PERP_ACCOUNT_ID_1).
      No private key of yours is needed; REYA_SIGNER=own-key with PERP_PRIVATE_KEY_FILE
      is the deliberate alternative.

Usage:
    python -m examples.agent_kit.btc_eth_strategy
"""

from typing import Any

import json
import logging
import subprocess
import sys

BTC_THRESHOLD_USD = 70_000
NOTIONAL_USD = 100

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("btc_eth_strategy")


def run_cli(*args: str) -> Any:
    """Invoke an agent kit CLI command and return its parsed JSON output.

    Example: run_cli("agent_kit.scripts.query", "prices", "--symbol", "BTCRUSDPERP")

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


def main() -> None:
    # Step 1: Read — fetch the BTC mark price.
    btc = run_cli("agent_kit.scripts.query", "prices", "--symbol", "BTCRUSDPERP")
    btc_price = float(btc["mark_price_usd"])
    logger.info("BTC mark price: $%s", f"{btc_price:,.2f}")

    # Step 2: Decide — direction based on the BTC threshold.
    if btc_price > BTC_THRESHOLD_USD:
        side = "long"
        logger.info("BTC > $%s → LONG ETH", f"{BTC_THRESHOLD_USD:,}")
    else:
        side = "short"
        logger.info("BTC <= $%s → SHORT ETH", f"{BTC_THRESHOLD_USD:,}")

    # Step 3: Write — market order $100 notional on ETHRUSDPERP.
    # trade.py rounds qty/price to market spec automatically.
    order = run_cli(
        "agent_kit.scripts.trade",
        "order",
        "market",
        "ETHRUSDPERP",
        "--side",
        side,
        "--notional",
        str(NOTIONAL_USD),
    )
    logger.info("Order result:\n%s", json.dumps(order, indent=2))

    # Step 4: Verify — read positions to confirm the fill landed.
    positions = run_cli("agent_kit.scripts.query", "positions", "--symbol", "ETHRUSDPERP")
    logger.info("Positions after order:\n%s", json.dumps(positions, indent=2))


if __name__ == "__main__":
    main()
