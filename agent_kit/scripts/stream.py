"""Observe one public V2 feed as NDJSON; no signing, orders or persistent secrets."""

import asyncio
import json
import time

import aiohttp

from agent_kit.scripts._common import JsonArgumentParser, output_result, symbol_arg
from agent_kit.scripts._env import get_api_url, get_trading_config
from agent_kit.scripts._stream import EVENT_FEEDS, FeedState
from agent_kit.scripts._v2 import WALLET_FEEDS, WS_URLS, V2Client, identifier, parse_json


def emit(data):
    print(json.dumps(data, separators=(",", ":")), flush=True)


async def _reader(ws, queue):
    async for message in ws:
        if message.type == aiohttp.WSMsgType.TEXT:
            data = parse_json(message.data)
            if not isinstance(data, dict):
                raise ValueError("Invalid WebSocket message")
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong", "id": data.get("id")})
                continue
            # Never silently drop frames if REST recovery or a consumer is slow.
            queue.put_nowait(data)
        elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
            break
    raise ConnectionError("WebSocket disconnected")


async def _next(queue, reader, timeout):
    if reader.done():
        reader.result()
    get = asyncio.create_task(queue.get())
    try:
        done, _ = await asyncio.wait([get, reader], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if reader in done:
            reader.result()
        if get in done:
            return get.result()
        raise TimeoutError("No WebSocket message within the timeout")
    finally:
        get.cancel()
        await asyncio.gather(get, return_exceptions=True)


async def observe(api_url, channel, *, seconds=60, max_age=30, max_pages=10, reconnects=3, emit_fn=emit):
    if api_url not in WS_URLS:
        raise ValueError("No verified WebSocket deployment mapping; custom/Cronos requires API-owner confirmation")
    state = FeedState(api_url, channel)
    deadline = time.monotonic() + seconds
    kind = state.kind
    async with V2Client(api_url) as rest:
        # The budget is per outage: consecutive failed attempts, reset once the feed serves again.
        attempt = 0
        while time.monotonic() < deadline:
            state.reset(keep_order_continuity=True)
            emit_fn(state.view(max_age=max_age))
            try:
                async with rest.session.ws_connect(WS_URLS[api_url], heartbeat=10, max_msg_size=256 * 1024) as ws:
                    queue: asyncio.Queue = asyncio.Queue(maxsize=128)
                    reader = asyncio.create_task(_reader(ws, queue))
                    try:
                        await ws.send_json({"type": "subscribe", "channel": channel})
                        buffered = []
                        while True:
                            message = await _next(queue, reader, min(15, max(0.1, deadline - time.monotonic())))
                            if message.get("type") == "error":
                                raise ValueError("Server rejected the V2 subscription; no fallback")
                            if message.get("channel") != channel:
                                continue
                            if message.get("type") == "subscribed":
                                state.snapshot(message.get("contents"))
                                break
                            if message.get("type") == "channel_data":
                                buffered.append(message)
                                if len(buffered) > 128:
                                    raise ValueError("Snapshot overlap buffer exceeded")
                        if buffered and kind not in (*EVENT_FEEDS, "orderChanges"):
                            raise ValueError("Unordered snapshot overlap lacks a public cursor; resubscribe")
                        if kind in (*EVENT_FEEDS, "orderChanges"):
                            path = channel.removeprefix("/v2").replace("/orderChanges", "/orderHistory")
                            # Reader keeps buffering while REST pages are in flight.
                            result = await asyncio.wait_for(
                                rest.history(path, end_time=int(time.time() * 1000), max_pages=max_pages),
                                timeout=max(0.01, deadline - time.monotonic()),
                            )
                            state.backfill(result)
                            if reader.done():
                                reader.result()
                        for message in buffered:
                            state.update(message)
                        emit_fn(state.view(max_age=max_age))
                        attempt = 0
                        while time.monotonic() < deadline:
                            try:
                                message = await _next(queue, reader, min(5, deadline - time.monotonic()))
                            except TimeoutError:
                                emit_fn(state.view(max_age=max_age))
                                continue
                            if message.get("type") == "error":
                                raise ValueError("Server reported a subscription failure")
                            if message.get("type") == "channel_data":
                                state.update(message)
                                emit_fn(state.view(max_age=max_age))
                    finally:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
            except (
                ValueError,
                KeyError,
                TypeError,
                ConnectionError,
                TimeoutError,
                aiohttp.ClientError,
                asyncio.QueueFull,
            ) as exc:
                state.phase = "incomplete"
                emit_fn(
                    {
                        **state.view(max_age=max_age),
                        "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                        "reconnect_attempt": attempt,
                    }
                )
                if attempt == reconnects:
                    raise ValueError("Feed recovery budget exhausted; state is incomplete") from None
                await asyncio.sleep(min(2**attempt, 8, max(0, deadline - time.monotonic())))
                attempt += 1
            else:
                break


def build_parser():
    parser = JsonArgumentParser(prog="stream", description=__doc__)
    parser.add_argument("scope", choices=["market", "wallet"])
    parser.add_argument("--feed", required=True)
    parser.add_argument("--symbol", type=symbol_arg)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--max-age", type=int, default=30)
    parser.add_argument("--max-pages", type=int, default=10)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        if not 1 <= args.seconds <= 3600 or not 1 <= args.max_age <= 300 or not 1 <= args.max_pages <= 100:
            raise ValueError("seconds: 1..3600; max-age: 1..300; max-pages: 1..100")
        api_url = get_api_url()
        if args.scope == "wallet":
            if args.feed not in WALLET_FEEDS or args.symbol:
                raise ValueError("Choose a documented wallet feed; wallet feeds do not use --symbol")
            wallet = identifier(get_trading_config(include_private_key=False).owner_wallet_address, wallet=True)
            channel = f"/v2/wallet/{wallet}/{args.feed}"
        else:
            symbol = identifier(args.symbol or "")
            if args.feed not in ("depth", "perpExecutions", "executionBusts", "summary"):
                raise ValueError("Market feed must be depth, perpExecutions, executionBusts or summary")
            channel = f"/v2/{'perpMarket' if args.feed == 'summary' else 'market'}/{symbol}/{args.feed}"
        asyncio.run(observe(api_url, channel, seconds=args.seconds, max_age=args.max_age, max_pages=args.max_pages))
    except Exception as exc:
        output_result({"error": str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: stream failed"})


if __name__ == "__main__":
    main()
