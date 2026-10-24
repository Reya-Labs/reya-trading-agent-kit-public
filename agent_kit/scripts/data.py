"""V2 public market research, bounded history and read-only account reconciliation."""

import asyncio
import time

from agent_kit.scripts._common import JsonArgumentParser, output_json, output_result, symbol_arg
from agent_kit.scripts._env import get_trading_config
from agent_kit.scripts._reconcile import account_projection, order_reconciliation
from agent_kit.scripts._v2 import HISTORY_FEEDS, V2Client, envelope, identifier, rows

# `type` on /market/{symbol}/perpExecutions, as reya-api-specs documents it (ExecutionTypeParam).
EXECUTION_TYPES = ("ORDER_MATCH", "LIQUIDATION", "ADL", "MARKET_CLOSE")


async def run(args):
    async with V2Client() as client:
        if args.command == "market":
            symbol = identifier(args.symbol) if args.symbol else None
            if args.kind == "search":
                definitions = rows(await client.get("/perpMarketDefinitions"))
                data = [row for row in definitions if not symbol or symbol.upper() in row["symbol"].upper()]
            elif args.kind == "book":
                if not 1 <= args.limit <= 1000:
                    raise ValueError("Depth limit must be between 1 and 1000")
                data = await client.get(f"/market/{symbol}/depth", limit=args.limit)
            elif args.kind == "candles":
                data = await client.get(
                    f"/candleHistory/{symbol}/{args.resolution}",
                    endTime=args.end_time if args.end_time is not None else int(time.time() * 1000),
                )
            elif args.kind == "trades":
                result = await client.history(
                    f"/market/{symbol}/perpExecutions",
                    start_time=args.start_time,
                    end_time=args.end_time,
                    max_pages=args.max_pages,
                    filters={"type": args.type} if args.type else None,
                )
                if args.type:
                    # Also filter here: a server that ignores the parameter must not return other types.
                    kept = [row for row in result["data"] if row.get("type") == args.type]
                    result["coverage"]["type_filter"] = {
                        "type": args.type,
                        "sent_to_server": True,
                        "removed_locally": len(result["data"]) - len(kept),
                    }
                    result["data"] = kept
                return envelope(client.api_url, **result)
            else:
                data = await client.get(f"/perpMarket/{symbol}/summary" if symbol else "/perpMarkets/summary")
            return envelope(client.api_url, data, state="observed", safe_for_order_admission=False)
        config = get_trading_config(include_private_key=False)
        wallet = identifier(config.owner_wallet_address, wallet=True)
        prefix = f"/wallet/{wallet}"
        accounts = rows(await client.get(prefix + "/accounts"))
        if args.command == "accounts":
            return envelope(client.api_url, accounts, wallet=wallet, selected_account=config.account_id)
        if config.account_id is None or not any(row.get("accountId") == config.account_id for row in accounts):
            raise ValueError("Select an account owned by PERP_WALLET_ADDRESS_1; run data accounts first")
        if args.command == "history":
            result = await client.history(
                prefix + "/" + args.kind, start_time=args.start_time, end_time=args.end_time, max_pages=args.max_pages
            )
            return envelope(client.api_url, **result, wallet=wallet, scope="whole_wallet")
        if args.command == "state":
            paths = [
                prefix + "/accountBalances",
                prefix + "/positions",
                "/perpMarkets/summary",
                "/assetDefinitions",
                "/assetOraclePrices",
            ]
            # No atomicity claim: each response is a separate observation.
            observations = [await client.get(path) for path in paths]
            result = account_projection(config.account_id, *observations)
            return envelope(client.api_url, result, wallet=wallet)
        end = args.end_time if args.end_time is not None else int(time.time() * 1000)
        pages = {
            kind: await client.history(
                prefix + "/" + kind, start_time=args.start_time, end_time=end, max_pages=args.max_pages
            )
            for kind in ("orderHistory", "perpExecutions", "executionBusts")
        }
        current_orders = rows(await client.get(prefix + "/openOrders"))
        data = order_reconciliation(
            config.account_id,
            current_orders + pages["orderHistory"]["data"],
            pages["perpExecutions"]["data"],
            pages["executionBusts"]["data"],
        )
        return envelope(
            client.api_url,
            data,
            wallet=wallet,
            account_id=config.account_id,
            coverage={kind: page["coverage"] for kind, page in pages.items()},
            safe_for_order_admission=False,
        )


def history_args(parser):
    parser.add_argument("--start-time", type=int)
    parser.add_argument("--end-time", type=int)
    parser.add_argument("--max-pages", type=int, default=10)


def build_parser():
    parser = JsonArgumentParser(prog="data", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    market = commands.add_parser("market")
    kinds = market.add_subparsers(dest="kind", required=True)
    for kind in ("search", "summary", "book", "candles", "trades"):
        sub = kinds.add_parser(kind)
        # search is a substring match, so it keeps the literal text.
        if kind == "search":
            sub.add_argument("--symbol")
        else:
            sub.add_argument("--symbol", required=kind in ("book", "candles", "trades"), type=symbol_arg)
        if kind == "book":
            sub.add_argument("--limit", type=int, default=100)
        if kind == "candles":
            sub.add_argument("--resolution", choices=["1m", "5m", "15m", "30m", "1h", "4h", "1d"], default="5m")
            sub.add_argument("--end-time", type=int)
        if kind == "trades":
            history_args(sub)
            sub.add_argument(
                "--type",
                choices=EXECUTION_TYPES,
                help="Only executions of this type (LIQUIDATION, ADL, MARKET_CLOSE or ORDER_MATCH)",
            )
    commands.add_parser("accounts")
    commands.add_parser("state")
    history = commands.add_parser("history")
    history.add_argument("kind", choices=HISTORY_FEEDS)
    history_args(history)
    history_args(commands.add_parser("reconcile-orders"))
    return parser


def main():
    args = build_parser().parse_args()
    try:
        output_json(asyncio.run(run(args)))
    except Exception as exc:
        output_result({"error": str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: V2 read failed"})


if __name__ == "__main__":
    main()
