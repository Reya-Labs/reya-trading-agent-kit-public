#!/usr/bin/env python3
"""
Example agent workflow: place a GTC limit buy for $100 of ETH at $50 below the
current mark price.

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
    python -m examples.agent_kit.eth_limit_order
"""

from typing import Any

import json
import logging
import subprocess
import sys

NOTIONAL_USD = 100
PRICE_OFFSET_USD = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("eth_limit_order")


def run_cli(*args: str) -> Any:
    """Invoke an agent kit CLI command and return its parsed JSON output.

    Example: run_cli("agent_kit.scripts.query", "prices", "--symbol", "ETHRUSDPERP")

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
    # Step 1: Read — fetch the current ETH mark price (the venue's execution reference).
    price = run_cli("agent_kit.scripts.query", "prices", "--symbol", "ETHRUSDPERP")
    eth_price = float(price["mark_price_usd"])
    logger.info("ETH mark price: $%s", f"{eth_price:,.2f}")

    # Step 2: Decide — target limit $50 below the mark.
    # trade.py rounds price to tick size automatically.
    limit_px = eth_price - PRICE_OFFSET_USD
    logger.info("Target limit price: $%s ($%s below mark)", f"{limit_px:,.2f}", PRICE_OFFSET_USD)

    # Step 3: Write — place the GTC limit buy.
    order = run_cli(
        "agent_kit.scripts.trade",
        "order",
        "limit",
        "ETHRUSDPERP",
        "--side",
        "long",
        "--price",
        str(limit_px),
        "--notional",
        str(NOTIONAL_USD),
    )
    logger.info("Order result:\n%s", json.dumps(order, indent=2))

    # Step 4: Verify — read open orders to confirm the limit landed.
    orders = run_cli("agent_kit.scripts.query", "orders", "--symbol", "ETHRUSDPERP")
    logger.info("Open ETHRUSDPERP orders:\n%s", json.dumps(orders, indent=2))


if __name__ == "__main__":
    main()
