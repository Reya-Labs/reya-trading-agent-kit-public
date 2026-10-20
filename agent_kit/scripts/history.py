"""Read-only V2 account-period history and performance with explicit coverage."""

import asyncio
import time

import aiohttp

from agent_kit.scripts._common import JsonArgumentParser, output_json, output_result, symbol_arg
from agent_kit.scripts._performance import performance
from agent_kit.scripts._reconcile import account_projection
from agent_kit.scripts._v2 import HISTORY_FEEDS, V2Client, envelope, identifier, integer, rows


async def run(args):
    wallet = identifier(args.wallet, wallet=True)
    start = integer(args.start_time, "start time")
    end = integer(args.end_time if args.end_time is not None else int(time.time() * 1000), "end time")
    if start > end or not 1 <= args.max_pages <= 100:
        raise ValueError("Invalid interval or page budget")
    symbol = identifier(args.symbol) if args.symbol else None
    async with V2Client() as client:
        prefix = f"/wallet/{wallet}"
        accounts = rows(await client.get(prefix + "/accounts"))
        owned = {integer(r.get("accountId"), "accountId") for r in accounts}
        if not args.whole_wallet and args.account_id not in owned:
            raise ValueError("Select an account owned by this wallet, or explicitly use --whole-wallet")
        scope = owned if args.whole_wallet else {args.account_id}
        pages = {}
        for kind in HISTORY_FEEDS:
            try:
                pages[kind] = await client.history(
                    prefix + "/" + kind, start_time=start, end_time=end, max_pages=args.max_pages
                )
            except (ValueError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                pages[kind] = {
                    "data": [],
                    "coverage": {
                        "state": "unavailable",
                        "reason": type(exc).__name__,
                        "detail": str(exc) if isinstance(exc, ValueError) else "V2 read failed; no fallback attempted",
                        "start_time": start,
                        "end_time": end,
                        "retention_complete": False,
                    },
                }
        result = performance(pages, scope, start_time=start, end_time=end, symbol=symbol)
        result["current_account_state"] = None
        if args.include_current_state:
            observations = [
                await client.get(path)
                for path in (
                    prefix + "/accountBalances",
                    prefix + "/positions",
                    "/perpMarkets/summary",
                    "/assetDefinitions",
                    "/assetOraclePrices",
                )
            ]
            result["current_account_state"] = [account_projection(account, *observations) for account in sorted(scope)]
            result["current_state_note"] = (
                "Observed now, not at interval boundaries; do not use as a historical return."
            )
        return envelope(client.api_url, result, wallet=wallet, scope="whole_wallet" if args.whole_wallet else "account")


def build_parser():
    parser = JsonArgumentParser(prog="history", description=__doc__)
    parser.add_argument("--wallet", required=True, help="Public owner wallet; no key required")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--account-id", type=int)
    scope.add_argument("--whole-wallet", action="store_true")
    parser.add_argument("--symbol", type=symbol_arg)
    parser.add_argument("--start-time", type=int, required=True, help="Inclusive Unix milliseconds")
    parser.add_argument("--end-time", type=int)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--include-current-state", action="store_true")
    return parser


def main():
    try:
        output_json(asyncio.run(run(build_parser().parse_args())))
    except Exception as exc:
        output_result(
            {"error": str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: history read failed"}
        )


if __name__ == "__main__":
    main()
