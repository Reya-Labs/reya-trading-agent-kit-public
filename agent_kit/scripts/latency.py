"""Measure how long a runner order takes to leave the kit, without sending one.

Reads go to the live API for the configured account (PERP_WALLET_ADDRESS_1, PERP_ACCOUNT_ID_1).
Order entry is replaced by a stub that acknowledges locally, and orders are signed with a
throwaway key, so nothing reaches the venue and no trading key is loaded. Writer state goes to
a temporary directory, so a running runner is never disturbed.

Reports, in milliseconds (medians):
  round_trip   one market-data GET on a warm connection: a lower bound for any order call
  rest_path    an order through the runner with every input read over REST
  warm_path    the same order with marks and wallet state kept warm from the WebSocket
A real order adds one order round trip (about round_trip, or more) to both paths.

    python -m agent_kit.scripts.latency [--symbol ETH] [--samples 5]
"""

import asyncio
import os
import secrets
import statistics
import tempfile
import time
from dataclasses import replace
from decimal import Decimal

from sdk.open_api.models.cancel_all_after_response import CancelAllAfterResponse
from sdk.open_api.models.cancel_order_response import CancelOrderResponse
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.mass_cancel_response import MassCancelResponse

from agent_kit.scripts import runner
from agent_kit.scripts._common import JsonArgumentParser, exception_result, output_json, output_result, symbol_arg
from agent_kit.scripts._env import get_trading_config, load_env


class DryOrders:
    """Stands in for order entry: acknowledges locally and sends nothing."""

    def __init__(self):
        self.count = 0

    async def create_order(self, create_order_request=None, **_):
        self.count += 1
        return CreateOrderResponse(
            order_id=f"{900000000 + self.count}",
            status="OPEN",
            cum_qty="0",
            client_order_id=create_order_request.client_order_id if create_order_request else None,
        )

    async def cancel_order(self, cancel_order_request=None, **_):
        return CancelOrderResponse(status="CANCELLED", order_id=str(cancel_order_request.order_id))

    async def cancel_all(self, *_, **__):
        return MassCancelResponse(cancelled_count=0)

    async def cancel_all_after(self, cancel_all_after_request=None, **_):
        request = cancel_all_after_request
        trigger = int(time.time() * 1000) + request.timeout_ms if request.timeout_ms else None
        return CancelAllAfterResponse(account_id=request.account_id, timeout_ms=request.timeout_ms, trigger_at=trigger)

    async def modify_order(self, *_, **__):
        raise ValueError("The latency probe measures creates only")


class DryRunnerClient(runner.RunnerClient):
    @property
    def orders(self):
        dry = self.__dict__.get("_dry_orders")
        if dry is None:
            dry = self.__dict__["_dry_orders"] = DryOrders()
        return dry


def _count_requests(client) -> list:
    """Record the path of every HTTP request the client makes."""
    calls: list = []
    api = client._api_client
    original = api.call_api

    async def call_api(method, url, *args, **kwargs):
        calls.append(f"{method} {url.split('/v2', 1)[-1].split('?')[0]}")
        return await original(method, url, *args, **kwargs)

    api.call_api = call_api
    return calls


async def _orders(client, command: list, samples: int, calls: list) -> dict:
    durations, reads, outcome = [], None, None
    for index in range(samples):
        client.lease.state["runner"] = {"requests": [], "acknowledged_orders": [], "submitted_notional": "0"}
        client.submitted = Decimal(0)
        before = len(calls)
        started = time.perf_counter()
        result = await runner.execute_request(client, {"id": f"probe-{index}", "command": command}, {})
        durations.append((time.perf_counter() - started) * 1000)
        reads = calls[before:]
        outcome = "refused_before_sending" if result.get("refused_by") or result.get("error") else "would_send"
        if result.get("error"):
            outcome += f": {result['error'][:200]}"
        await asyncio.sleep(0.6)  # let at least one summary push arrive between samples
    return {
        "median_ms": round(statistics.median(durations), 1),
        "min_ms": round(min(durations), 1),
        "requests_per_order": len(reads or []),
        "requests": reads,
        "outcome": outcome,
    }


async def measure(symbol: str, samples: int) -> dict:
    load_env()
    config = replace(
        get_trading_config(include_private_key=False), private_key="0x" + secrets.token_hex(32)
    )  # throwaway: signs locally, never sent
    if config.account_id is None:
        raise ValueError("Set PERP_ACCOUNT_ID_1 (and PERP_WALLET_ADDRESS_1) to the account to measure")
    policy = runner.Policy(frozenset({symbol}), Decimal(10**9), Decimal(10**12), Decimal("0.05"), 1000, 3600)
    with tempfile.TemporaryDirectory() as state:
        os.chmod(state, 0o700)
        os.environ["REYA_STATE_DIR"] = state
        async with DryRunnerClient(config) as client:
            client.configure(policy)
            await client.start()
            calls = _count_requests(client)
            definitions = await client.reference.get_perp_market_definitions()
            market = next((m for m in definitions if m.symbol == symbol), None)
            if market is None:
                raise ValueError(f"Unknown symbol '{symbol}'")
            summary = await client.markets.get_perp_market_summary(symbol=symbol)
            # A passive post-only bid far below the mark: the same reads as any create.
            tick = Decimal(market.tick_size)
            price = (Decimal(summary.mark_price) * Decimal("0.8") // tick) * tick
            command = [
                "order",
                "limit",
                symbol,
                "--side",
                "long",
                "--price",
                format(price, "f"),
                "--qty",
                market.min_order_qty,
                "--post_only",
            ]
            round_trips = []
            for _ in range(samples):
                started = time.perf_counter()
                await client.markets._api.get_perp_market_summary(symbol=symbol)
                round_trips.append((time.perf_counter() - started) * 1000)
            await client.warm_up()
            rest = await _orders(client, command, samples, calls)
            client.configure(policy, live=runner.live_state(client, policy))
            await client.start()
            warm_state = await client.warm_up()
            try:
                warm = await _orders(client, command, samples, calls)
            finally:
                if client.live is not None:
                    await client.live.close()
    return {
        "api_url": config.api_url,
        "symbol": symbol,
        "samples": samples,
        "round_trip_ms": {"median": round(statistics.median(round_trips), 1), "min": round(min(round_trips), 1)},
        "rest_path": rest,
        "warm_path": {**warm, "warm_state": warm_state},
        "note": "Order entry was stubbed and nothing was sent. A real order adds one order round trip to each path.",
    }


def build_parser():
    parser = JsonArgumentParser(prog="latency", description=__doc__)
    parser.add_argument("--symbol", type=symbol_arg, default="ETHRUSDPERP")
    parser.add_argument("--samples", type=int, default=5, choices=range(1, 21), metavar="1..20")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        output_json(asyncio.run(measure(args.symbol, args.samples)))
    except Exception as exc:
        output_result(exception_result(exc))


if __name__ == "__main__":
    main()
