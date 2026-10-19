"""Credential-free local paper trading. This command can only GET public V2 data."""

import asyncio
import time
from pathlib import Path

from agent_kit.scripts._common import JsonArgumentParser, output_result, symbol_arg
from agent_kit.scripts._paper import (
    MAX_EVENTS,
    PaperStore,
    arm_trigger,
    cancel_triggers,
    clock_offset,
    clock_report,
    due_triggers,
    initialize,
    observe,
    replay,
    simulate,
    upgrade,
    valuation,
)
from agent_kit.scripts._v2 import V2Client, identifier, rows

APIS = ("https://api.reya.xyz/v2", "https://api-devnet.reya-cronos.network/v2")
TRIGGER_KINDS = {"sl": "stop_loss", "tp": "take_profit"}


def _now() -> int:
    return int(time.time() * 1000)


async def run(args):
    now = _now()
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
        state = upgrade(state, now)
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
        action = args.order_kind if args.command == "order" else None
        if action == "cancel_all":
            state, result = cancel_triggers(state, _symbol(args, required=False), args.all)
            store.save(state)
            return result
        request, symbol = None, None
        if action in TRIGGER_KINDS:
            symbol = _symbol(args)
            if args.trigger_px is None or args.limit_px is None or args.time_in_force is None or not args.side:
                raise ValueError(f"paper order {action} needs --side, --trigger_px, --limit_px and --time_in_force")
        elif args.command == "order":
            request = _paper_request(args)
            symbol = request["symbol"]
            cached = replay(state, request)
            if cached:
                return cached  # A retry does not depend on network availability or current prices.
        async with V2Client(state["api_url"]) as client:
            summaries = rows(await client.get("/perpMarkets/summary"))
            clock = clock_offset(getattr(client, "server_date", None))
            definitions = None
            if symbol or state["positions"] or state["triggers"]:
                definitions = rows(await client.get("/perpMarketDefinitions"))
                if symbol and not any(r.get("symbol") == symbol for r in definitions):
                    raise ValueError("Unknown perpetual market")
            # TP/SL fire on the mark at this observation; their children need the books.
            books = {}
            for due in due_triggers(state, summaries, _now(), clock):
                books[due] = await client.get(f"/market/{due}/depth", limit=1000)
            if request and symbol not in books:
                books[symbol] = await client.get(f"/market/{symbol}/depth", limit=1000)
            now = _now()
            state, events = observe(state, summaries, definitions, books, now, clock)
            context = {"events": events, **clock_report(clock)}
            if request:
                liquidation = next((e for e in events if e["event"] == "liquidation"), None)
                if liquidation is not None:
                    store.save(state)
                    return {
                        "mode": "paper",
                        "request_id": request["request_id"],
                        "symbol": symbol,
                        "status": "not_simulated",
                        "error": (
                            f"The paper account was liquidated at this observation: equity {liquidation['equity']} "
                            f"was below its liquidation margin {liquidation['liquidation_margin']}. Every position "
                            "was closed and the balance is 0; the order was not simulated and its request ID is "
                            "unused. See `events` and `paper history`."
                        ),
                        **context,
                    }
                state, result = simulate(state, request, definitions, books[symbol], summaries, now, clock)
                store.save(state)
                return {**result, **context}
            if action in TRIGGER_KINDS:
                state, result = arm_trigger(
                    state,
                    symbol,
                    TRIGGER_KINDS[action],
                    args.side,
                    args.trigger_px,
                    args.limit_px,
                    definitions,
                    summaries,
                    now,
                    time_in_force=args.time_in_force,
                    force=args.force,
                    client_order_id=args.client_order_id,
                    clock=clock,
                )
                store.save(state)
                return {**result, **context}
            result = valuation(state, summaries, now, definitions if state["positions"] else None, clock)
            if args.command == "refresh":
                if len(state["snapshots"]) >= MAX_EVENTS:
                    raise ValueError("Snapshot bound reached; export and reset before further recording")
                state["snapshots"].append(result)
            store.save(state)
            return {**result, **context}


def _symbol(args, required=True):
    if args.order_symbol and args.symbol and args.order_symbol != args.symbol:
        raise ValueError("Conflicting symbols")
    symbol = args.order_symbol or args.symbol
    if not symbol:
        if required:
            raise ValueError("A symbol is required")
        return None
    return identifier(symbol)


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
    symbol = _symbol(args)
    if args.order_kind == "market" and args.kind == "ioc":
        raise ValueError("`order market` cannot be combined with --kind ioc")
    if not args.side:
        raise ValueError("--side is required")
    if args.qty is None and not args.max_size and args.usage is None:
        raise ValueError("Give one of --qty, --max_size or --usage")
    request = {
        "request_id": request_id,
        "symbol": symbol,
        "side": {"long": "buy", "short": "sell"}.get(args.side, args.side),
        "kind": args.kind or "market",
        "qty": args.qty,
        "price": args.price,
        "slippage": args.slippage,
        "reduce_only": args.reduce_only,
    }
    # Options appear only when used, so a request ID recorded before 0.10.0 still replays.
    if args.max_size:
        request["max_size"] = True
    if args.usage is not None:
        request["usage"] = args.usage
    if args.sl is not None or args.tp is not None:
        request.update(sl=args.sl, tp=args.tp, trigger_slippage=args.trigger_slippage)
    return request


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
    order = commands.add_parser(
        "order", help="Simulate an order or arm a TP/SL; accepts the live `order market|sl|tp|cancel_all` shapes"
    )
    order.add_argument(
        "order_kind",
        nargs="?",
        choices=("market", "limit", "sl", "tp", "cancel_all"),
        help="Live-compatible: order market ETH, order sl ETH, order cancel_all --symbol ETH",
    )
    order.add_argument("order_symbol", nargs="?", type=symbol_arg)
    order.add_argument("--request-id")
    order.add_argument("--client_order_id", help="Live-compatible alias, used as the request ID")
    order.add_argument("--symbol", type=symbol_arg)
    order.add_argument("--side", choices=("buy", "sell", "long", "short"))
    order.add_argument("--kind", choices=("market", "ioc"))
    size = order.add_mutually_exclusive_group()
    size.add_argument("--qty")
    size.add_argument("--max_size", action="store_true", help="Largest size the engine admits, less 0.2%% headroom")
    size.add_argument("--usage", help="Size so margin usage is at most this fraction after a full fill")
    order.add_argument("--price")
    order.add_argument("--slippage", default="0.01")
    order.add_argument("--reduce-only", "--reduce_only", dest="reduce_only", action="store_true")
    order.add_argument("--sl", help="Arm a stop-loss once the entry fills: a percent from the fill price or a price")
    order.add_argument("--tp", help="Arm a take-profit once the entry fills: a percent from the fill price or a price")
    order.add_argument("--trigger_slippage", default="0.01", help="Child limit distance from each trigger")
    order.add_argument("--trigger_px", help="order sl|tp: the mark at which it fires")
    order.add_argument("--limit_px", help="order sl|tp: the child's worst price")
    order.add_argument("--time_in_force", choices=("IOC", "GTC"), help="order sl|tp: paper fires IOC children only")
    order.add_argument("--force", action="store_true", help="order sl|tp: arm a trigger the mark has already reached")
    order.add_argument("--all", action="store_true", help="order cancel_all: every paper TP/SL")
    order.add_argument("--fees_wait_s", help="Accepted for live compatibility; paper fees are immediate")
    return parser


def main():
    try:
        output_result(asyncio.run(run(build_parser().parse_args())))
    except Exception as exc:
        output_result(
            {
                "error": (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else f"{type(exc).__name__}: paper operation failed; inspect the ledger and retry orders with the same request ID"
                ),
                **getattr(exc, "details", {}),
            }
        )


if __name__ == "__main__":
    main()
