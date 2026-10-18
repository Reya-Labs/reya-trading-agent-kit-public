"""Credential-free local paper trading. This command can only GET public V2 data."""

import asyncio
import time
from pathlib import Path

from agent_kit.scripts._common import JsonArgumentParser, output_json, output_result, symbol_arg
from agent_kit.scripts._paper import MAX_EVENTS, PaperStore, initialize, replay, simulate, valuation
from agent_kit.scripts._v2 import V2Client, identifier, rows

APIS = ("https://api.reya.xyz/v2", "https://api-devnet.reya-cronos.network/v2")


async def run(args):
    now = int(time.time() * 1000)
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
        if state is None:
            raise ValueError("Run paper init first")
        if state["api_url"] not in APIS:
            raise ValueError("Paper ledger references an unsupported public V2 deployment")
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
        request = None
        if args.command == "order":
            request = _paper_request(args)
            cached = replay(state, request)
            if cached:
                return cached  # A retry does not depend on network availability or current prices.
        async with V2Client(state["api_url"]) as client:
            summaries = rows(await client.get("/perpMarkets/summary"))
            if request:
                definitions = rows(await client.get("/perpMarketDefinitions"))
                definition = next((r for r in definitions if r["symbol"] == request["symbol"]), None)
                if definition is None:
                    raise ValueError("Unknown perpetual market")
                book = await client.get(f"/market/{request['symbol']}/depth", limit=1000)
                state, result = simulate(state, request, definition, book, summaries, int(time.time() * 1000))
                store.save(state)
                return result
            margin_definitions = rows(await client.get("/perpMarketDefinitions")) if state["positions"] else None
            result = valuation(state, summaries, int(time.time() * 1000), margin_definitions)
            if args.command == "refresh":
                if len(state["snapshots"]) >= MAX_EVENTS:
                    raise ValueError("Snapshot bound reached; export and reset before further recording")
                state["snapshots"].append(result)
                store.save(state)
            return result


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
    if args.order_symbol and args.symbol and args.order_symbol != args.symbol:
        raise ValueError("Conflicting symbols")
    symbol = args.order_symbol or args.symbol
    if not symbol:
        raise ValueError("A symbol is required")
    if args.order_kind == "market" and args.kind == "ioc":
        raise ValueError("`order market` cannot be combined with --kind ioc")
    return {
        "request_id": request_id,
        "symbol": identifier(symbol),
        "side": {"long": "buy", "short": "sell"}.get(args.side, args.side),
        "kind": args.kind or "market",
        "qty": args.qty,
        "price": args.price,
        "slippage": args.slippage,
        "reduce_only": args.reduce_only,
    }


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
    history = commands.add_parser("history")
    history.add_argument("--offset", type=int, default=0)
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--snapshots", action="store_true")
    order = commands.add_parser("order", help="Simulate an order; also accepts the live `order market <symbol>` shape")
    order.add_argument("order_kind", nargs="?", choices=("market", "limit"), help="Live-compatible: order market ETH")
    order.add_argument("order_symbol", nargs="?", type=symbol_arg)
    order.add_argument("--request-id")
    order.add_argument("--client_order_id", help="Live-compatible alias, used as the request ID")
    order.add_argument("--symbol", type=symbol_arg)
    order.add_argument("--side", choices=("buy", "sell", "long", "short"), required=True)
    order.add_argument("--kind", choices=("market", "ioc"))
    order.add_argument("--qty", required=True)
    order.add_argument("--price")
    order.add_argument("--slippage", default="0.01")
    order.add_argument("--reduce-only", "--reduce_only", dest="reduce_only", action="store_true")
    order.add_argument("--fees_wait_s", help="Accepted for live compatibility; paper fees are immediate")
    return parser


def main():
    try:
        output_json(asyncio.run(run(build_parser().parse_args())))
    except Exception as exc:
        output_result(
            {
                "error": (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else f"{type(exc).__name__}: paper operation failed; inspect the ledger and retry orders with the same request ID"
                )
            }
        )


if __name__ == "__main__":
    main()
