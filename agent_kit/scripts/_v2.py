"""Bounded unsigned V2 reads. Routes are explicit; there is no legacy fallback."""

import asyncio
import json
import re
import time
from decimal import Decimal, InvalidOperation

import aiohttp

from agent_kit.scripts._env import get_api_url
from agent_kit.scripts._http import USER_AGENT, RegionUnavailableError, region_blocked

MAX_BYTES = 4 * 1024 * 1024
WALLET_FEEDS = (
    "accounts",
    "positions",
    "accountBalances",
    "orderChanges",
    "perpExecutions",
    "executionBusts",
    "transfers",
)
HISTORY_FEEDS = ("orderHistory", "perpExecutions", "executionBusts", "transfers")
WS_URLS = {
    "https://api.reya.xyz/v2": "wss://ws.reya.xyz",
    "https://api-devnet.reya-cronos.network/v2": "wss://websocket-devnet.reya-cronos.network",
}


def identifier(value: str, *, wallet: bool = False) -> str:
    pattern = r"0x[0-9a-fA-F]{40}" if wallet else r"[A-Za-z0-9]{1,80}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Invalid wallet address" if wallet else "Invalid market symbol")
    return value.lower() if wallet else value


def integer(value, label="sequenceNumber") -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError(f"Invalid exact API integer: {label}")
    return value


def decimal(value, label: str) -> Decimal:
    if not isinstance(value, str) or len(value) > 160:
        raise ValueError(f"{label} must be an exact decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"Invalid decimal: {label}") from None
    if not result.is_finite() or abs(result.adjusted()) > 100:
        raise ValueError(f"Invalid decimal: {label}")
    return result


def rows(value) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Expected an API array of objects")
    if len(value) > 10000:
        raise ValueError("API array exceeds the supported bound")
    return value


def parse_json(raw):
    # Non-integer JSON numbers remain exact strings. API monetary values are strings.
    return json.loads(
        raw, parse_float=str, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON"))
    )


class V2Client:
    def __init__(self, api_url: str | None = None):
        self.api_url = api_url or get_api_url()
        if not self.api_url.endswith("/v2"):
            raise ValueError("V2 API base URL required; non-V2 routes need a recorded exception")

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20, connect=5), headers={"User-Agent": USER_AGENT}
        )
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def get(self, path: str, **params):
        if not re.fullmatch(r"/[A-Za-z0-9/]+", path) or "//" in path:
            raise ValueError("Invalid V2 read path")
        async with self.session.get(
            self.api_url + path, params={k: str(v) for k, v in params.items() if v is not None}, allow_redirects=False
        ) as response:
            try:
                raw = await response.content.readexactly(MAX_BYTES + 1)
            except asyncio.IncompleteReadError as exc:
                raw = exc.partial
            if len(raw) > MAX_BYTES:
                raise ValueError("V2 response exceeds the size bound")
            if region_blocked(response.status, raw):
                raise RegionUnavailableError()
            if response.status != 200:
                raise ValueError(f"V2 read {path} returned HTTP {response.status}; no fallback was attempted")
            return parse_json(raw)

    async def history(self, path: str, *, start_time=None, end_time=None, max_pages=10) -> dict:
        """Walk published cursors or inclusive time pages without skipping timestamp ties."""
        if not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100")
        if start_time is not None and end_time is not None and start_time > end_time:
            raise ValueError("start_time must not exceed end_time")
        cursor = None
        boundary = end_time
        seen_cursors = set()
        collected: dict[int, dict] = {}
        unidentified: list[dict] = []
        reason = "page_budget"
        pages = 0
        transfer = path.endswith("/transfers")
        for _ in range(max_pages):
            page = await self.get(
                path, startTime=start_time, endTime=boundary, **({"cursor": cursor, "limit": 100} if transfer else {})
            )
            pages += 1
            if not isinstance(page, dict) or not isinstance(page.get("meta"), dict):
                raise ValueError("Invalid history page")
            data = rows(page.get("data"))
            if len(data) > 100:
                raise ValueError("History page exceeds the published limit")
            previous_count = len(collected)
            for row in data:
                seq = integer(row.get("sequenceNumber"))
                if seq == 0 and path.endswith("/perpExecutions"):
                    unidentified.append(row)
                    continue
                if seq in collected and collected[seq] != row:
                    raise ValueError("History returned conflicting records for one sequence")
                collected[seq] = row
            if transfer:
                cursor = page["meta"].get("nextCursor")
                if cursor is None:
                    reason = "available_history_exhausted"
                    break
                if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in seen_cursors:
                    raise ValueError("Invalid or repeated history cursor")
                seen_cursors.add(cursor)
            else:
                if len(data) < 100:
                    reason = "available_history_exhausted"
                    break
                next_boundary = integer(page["meta"].get("endTime"), "endTime")
                if boundary is not None and next_boundary > boundary:
                    raise ValueError("History boundary moved forward")
                if next_boundary == boundary or len(collected) == previous_count:
                    reason = "timestamp_boundary_saturated"
                    break
                boundary = next_boundary
        return {
            "data": [collected[key] for key in sorted(collected, reverse=True)] + unidentified,
            "coverage": {
                "state": "bounded" if reason == "available_history_exhausted" and not unidentified else "incomplete",
                "reason": reason,
                "identity_verified": not unidentified,
                "unidentified_observations": len(unidentified),
                "identity_note": "Zero-sequence legacy executions have no reliable overlap identity; observations retained without deduplication.",
                "pages": pages,
                "start_time": start_time,
                "end_time": end_time,
                "next_cursor": cursor if transfer else None,
                "next_end_time": boundary if not transfer else None,
                "retention_complete": False,
                "note": "Exhausting available history does not establish full retention or nonexecution.",
            },
        }


def envelope(api_url: str, data, **extra) -> dict:
    return {"api_url": api_url, "observed_at_ms": int(time.time() * 1000), "data": data, **extra}
