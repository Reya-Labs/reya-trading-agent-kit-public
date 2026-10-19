"""Shared CLI plumbing for the agent kit scripts.

Centralizes the JSON output contract: every command prints JSON to stdout and
exits 0 on success or 1 on any failure — including argument-parsing failures,
which stock argparse would report as usage text on stderr with exit code 2.
"""

from typing import NoReturn

import argparse
import json
import os
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation

from sdk.open_api.exceptions import ApiException

from agent_kit.scripts._http import RegionUnavailableError, region_blocked

DEFAULT_SLIPPAGE = Decimal("0.01")  # 1% default slippage for market orders
MAX_SLIPPAGE = Decimal("0.05")  # soft cap: above this, --force is required
MAX_SLIPPAGE_FORCED = Decimal("0.5")  # absolute ceiling, even with --force

# A limit price may sit at most this far through the mark (above it for a buy, below it
# for a sell) without --force. It matches the market-order slippage cap, so a marketable
# limit can never go further than `order market` allows without --force, and the venue's
# 5% on-chain fill collar busts fills further from its mark anyway: a wider price buys no
# extra fill, only the risk of sweeping the book.
LIMIT_PRICE_BAND = MAX_SLIPPAGE
MAX_ORDER_NOTIONAL_VAR = "REYA_MAX_ORDER_NOTIONAL"  # per-order cap in USDC; default: the account value
MAX_LEVERAGE_VAR = "REYA_MAX_LEVERAGE"
DEFAULT_MAX_LEVERAGE = Decimal("5")


_output_buffer: ContextVar[list | None] = ContextVar("output_buffer", default=None)


@contextmanager
def capture_output():
    buffer: list = []
    token = _output_buffer.set(buffer)
    try:
        yield buffer
    finally:
        _output_buffer.reset(token)


def output_json(data: dict | list) -> None:
    """Print JSON to stdout."""
    buffer = _output_buffer.get()
    if buffer is not None:
        buffer.append(data)
    else:
        print(json.dumps(data, indent=2))


def retain_result(data: dict) -> None:
    """Keep an acknowledged write available if runner post-write reads time out."""
    buffer = _output_buffer.get()
    if buffer is not None:
        buffer.append(data)


def output_error(message: str) -> NoReturn:
    """Print error JSON to stdout and exit with code 1."""
    output_json({"error": message})
    sys.exit(1)


def exception_result(exc: Exception, *, fallback: str | None = None) -> dict:
    """Project SDK errors without rendering headers, reason, or arbitrary body fields."""
    if isinstance(exc, RegionUnavailableError) or (
        isinstance(exc, ApiException) and region_blocked(exc.status, exc.body)
    ):
        message = str(RegionUnavailableError())
        return {"error": message, "error_code": "REGION_UNAVAILABLE", "message": message, "http_status": 403}
    if not isinstance(exc, ApiException):
        return {"error": fallback or f"{type(exc).__name__}: {exc}"}
    code, message = "API_ERROR", "Reya API request failed; inspect account state before retrying a write"
    body = exc.body
    data = None
    if isinstance(body, str) and len(body) <= 65536:
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            data = None
    if exc.status == 429:
        # The API reports its own limits as 400 RATE_LIMITED_ERROR; a 429 is the per-IP edge ban.
        code = "EDGE_RATE_LIMITED"
        message = "Reya's edge rate-limited this IP (HTTP 429); the request did not reach the API. Pause all requests for at least 60 seconds"
    elif isinstance(data, dict) and isinstance(data.get("error"), str):
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", data["error"]):
            code = data["error"]
            if isinstance(data.get("message"), str) and data["message"].strip():
                message = " ".join(data["message"].split())[:500]
    if code == "UNAUTHORIZED_SIGNATURE_ERROR" and os.environ.get("REYA_SIGNER", "agent") == "agent":
        message += ". The agent key's approval may have been revoked; run agent status for the re-approve link"
        result_hint = True
    else:
        result_hint = False
    result: dict = {"error": message, "error_code": code, "message": message}
    if result_hint:
        result["hint"] = "agent status"
    if type(exc.status) is int:
        result["http_status"] = exc.status
    retry_after = data.get("retryAfterMs") if isinstance(data, dict) else None
    if type(retry_after) is int and 0 <= retry_after <= 3_600_000:
        result["retry_after_ms"] = retry_after
    return result


def output_result(data: dict) -> None:
    """Retain structured results on failure, including partially successful batches."""
    output_json(data)
    if data.get("error"):
        sys.exit(1)


def require_price(value: str | None, label: str) -> Decimal:
    """Missing prices are not zero; refuse arithmetic that could size a bad order."""
    if value is None:
        raise ValueError(f"{label} is unavailable; no price-dependent action was taken")
    price = Decimal(value)
    if not price.is_finite() or price <= 0:
        raise ValueError(f"{label} must be a positive finite price")
    return price


_PERP_SUFFIX = "RUSDPERP"
_BARE_TICKER = re.compile(r"[A-Z0-9]{1,20}")


def symbol_arg(text: str) -> str:
    """Accept a bare perp ticker ("eth") for its full symbol ("ETHRUSDPERP").

    Reya perp symbols are TICKER + RUSDPERP, so the mapping is mechanical and never
    ambiguous for this perp-only kit. Full symbols pass through uppercased; anything
    else is left for the existing market-definition check to reject.
    """
    value = text.strip().upper()
    if value.endswith(_PERP_SUFFIX) or not _BARE_TICKER.fullmatch(value) or value.endswith("RUSD"):
        return value
    return value + _PERP_SUFFIX


def positive_decimal_arg(text: str) -> Decimal:
    value = _decimal_arg(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


class JsonArgumentParser(argparse.ArgumentParser):
    """ArgumentParser whose failures honor the kit's JSON contract.

    Stock argparse prints usage text to stderr and exits 2 on bad arguments;
    agents parse stdout and the documented contract promises {"error": "..."}
    with exit 1 for every failure. Subparsers created via add_subparsers() /
    add_parser() inherit this class automatically (argparse defaults
    parser_class to type(self)), so only the root parser needs to use it —
    do not pass an explicit parser_class= that would break that propagation.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        output_error(f"{self.prog}: {message}")


def require_configured_account_id(client) -> int:
    """Return the PERP_ACCOUNT_ID_1 from the client's config or raise."""
    account_id = client.config.account_id
    if account_id is None:
        raise ValueError("PERP_ACCOUNT_ID_1 environment variable is required for this command.")
    return account_id


async def require_owned_account(client) -> int:
    """Do not mistake an account/owner mismatch for an empty trading account."""
    account_id = require_configured_account_id(client)
    accounts = await client.get_accounts()
    if not any(account.account_id == account_id for account in accounts):
        raise ValueError("PERP_ACCOUNT_ID_1 does not belong to PERP_WALLET_ADDRESS_1 on this deployment")
    return account_id


def _decimal_arg(text: str) -> Decimal:
    """argparse type for exact decimal CLI values. Rejects non-numbers, NaN, and infinity."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"invalid decimal value: '{text}'") from None
    if not value.is_finite():
        raise argparse.ArgumentTypeError(f"invalid decimal value: '{text}'")
    return value


def slippage_arg(text: str) -> Decimal:
    """argparse type for --slippage: an exact decimal in (0, 0.5].

    Slippage is a decimal fraction (0.01 = 1%). The hard bound rejects the
    worst typo classes outright — '--slippage 1' meaning 1%, negatives, zero —
    before any network call.
    """
    value = _decimal_arg(text)
    if not (0 < value <= MAX_SLIPPAGE_FORCED):
        raise argparse.ArgumentTypeError(
            f"slippage must be in (0, {MAX_SLIPPAGE_FORCED}]; it is a decimal fraction (0.01 = 1%), got '{text}'"
        )
    return value


def check_slippage_guardrail(slippage: Decimal, force: bool) -> None:
    """Reject slippage above MAX_SLIPPAGE unless --force is passed.

    The escape hatch exists because close_all is the emergency-flatten
    primitive: on an illiquid market a guaranteed fill can legitimately need
    more than 5%, and refusing with no override would block the risk-off path
    exactly when it matters.
    """
    if slippage > MAX_SLIPPAGE and not force:
        raise ValueError(
            f"Slippage {slippage} exceeds the {MAX_SLIPPAGE} (5%) safety cap. "
            f"Re-run with --force if this is intentional."
        )


def _positive_env_decimal(name: str) -> Decimal | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        value = Decimal("NaN")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def order_size_limits() -> tuple[Decimal | None, Decimal]:
    """(per-order notional cap in USDC, or None for the account value; estimated-leverage limit)."""
    return _positive_env_decimal(MAX_ORDER_NOTIONAL_VAR), (
        _positive_env_decimal(MAX_LEVERAGE_VAR) or DEFAULT_MAX_LEVERAGE
    )


def limit_price_band_refusal(limit_px: Decimal, mark: Decimal, is_buy: bool, force: bool) -> dict | None:
    """Refuse a limit price more than LIMIT_PRICE_BAND through the mark unless --force.

    A buy above the mark (or a sell below it) trades at once against the book, so a
    fat-fingered price sweeps it. Beyond MAX_SLIPPAGE_FORCED even --force is refused,
    as for market-order slippage. Prices on the passive side are never refused.
    """
    through = (limit_px - mark) / mark if is_buy else (mark - limit_px) / mark
    if through <= LIMIT_PRICE_BAND:
        return None
    relation = "above" if is_buy else "below"
    side = "buy" if is_buy else "sell"
    message = (
        f"Limit price {format(limit_px.normalize(), 'f')} is {through * 100:.2f}% {relation} the mark "
        f"{format(mark.normalize(), 'f')}; a {side} this far through the mark trades immediately against the book"
    )
    if through > MAX_SLIPPAGE_FORCED:
        return {
            "check": "limit_price_band",
            "message": f"{message}, and more than {MAX_SLIPPAGE_FORCED * 100:.0f}% is refused even with --force.",
            "override": None,
        }
    if force:
        return None
    return {
        "check": "limit_price_band",
        "message": f"{message} (band: {LIMIT_PRICE_BAND * 100:.0f}%). Re-run with --force if this is intentional.",
        "override": "--force",
    }
