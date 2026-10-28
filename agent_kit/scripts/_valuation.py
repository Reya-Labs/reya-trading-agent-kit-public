"""Live account figures as the API values them: `GET /v2/wallet/{address}/accountSummaries` (the account) and
`/positionRisk` (its open positions), shown by `query margin`, `query positions`, `data state`,
`history --include-current-state` and the strategy preview's available balance. The kit adds only arithmetic on
them (`margin_buffer_usd`, `health_usd`, `liquidatable`, `distance_to_liquidation_pct`); its own model
(`_margin.py`) stays for the pre-trade verdict, sizing, the account after a fill and paper accounts.

The API answers 503 rather than partial figures when a price or market input is missing or stale (a mark older
than 60 seconds included). The kit then shows the figures as unavailable with the reason, never an older or
locally computed number in their place.
"""

import asyncio
from decimal import Decimal

import aiohttp

from agent_kit.scripts._http import RegionUnavailableError
from agent_kit.scripts._margin import _distance_pct, _ratio, _usd, rounding_allowance
from agent_kit.scripts._v2 import V2Client, V2StatusError, decimal, identifier, integer, rows

UNAVAILABLE_CODE = "VALUATION_UNAVAILABLE"
# A read that failed on the wire or answered something the kit cannot use; anything else is a bug and propagates.
READ_ERRORS = (ValueError, OSError, aiohttp.ClientError)
RETRY = "Read again in a few seconds; the kit shows no figures of its own in their place."
MISMATCH = (
    "The positions and the API's valuation of them do not match: a fill landed between the two reads, or the "
    "valuation has not caught up with it yet. Liquidation prices are cross margin, so none of the account's figures "
    "are shown. Read again in a few seconds."
)

# Echoed by `query margin` so a reader knows where the figures come from and what they leave out (docs/margin.md).
NOTES = [
    "Every figure is the API's (accountSummaries and positionRisk), except margin_buffer_usd, health_usd, "
    "liquidatable and distance_to_liquidation_pct, which are the kit's arithmetic on the API's figures.",
    "equity_usd is the account's value before collateral haircuts (each collateral at its oracle price) + "
    "unrealized PnL at mark, as the Reya app shows it. margin_balance_usd is collateral after haircuts + "
    "unrealized PnL, and every margin figure (usage, buffers, health, liquidatable, liquidation prices) is measured "
    "against it. The collateral already includes realized funding and ADL cashflows, so no funding is added; "
    "funding_since_change_usd is a position's funding since its size last changed, already in it. equity_usd and "
    "funding_since_change_usd are null where the API does not send them yet.",
    "The initial-margin figures (initial_margin_req_usd, initial_margin_buffer_usd) are meant to match what order "
    "entry enforces. The liquidation figures (liquidation_margin_req_usd, margin_usage, liquidation prices) follow "
    "the chain, which liquidates below the liquidation margin (margin_usage 1).",
    "Liquidation price per position: cross margin, every other mark, collateral price and funding unchanged, rounded "
    "onto the tick toward the mark. has_correlated_collateral: the account holds collateral that moves with this "
    "market, which the estimate holds fixed.",
    "Settled positions only: matched fills that have not settled yet (seconds) are not included. The account "
    "figures and the positions are two reads a moment apart.",
]

# The decimal strings the kit reads from each row: (signed, never negative, never negative or null, signed or
# absent). The last are new in the API's 3.7.7 and absent where it is not deployed yet: the kit's field is then null,
# never another figure in its place, and the rest of the valuation is shown.
_SUMMARY = (
    ("collateralValue", "unrealizedPnl", "marginBalance", "freeCollateral"),
    ("initialMarginRequirement", "liquidationMarginRequirement"),
    ("liquidationMarginRatio",),
    ("equity",),
)
_POSITION = (
    ("avgEntryPrice", "unrealizedPnl"),
    ("qty", "markPrice", "notional", "liquidationMarginRequirement"),
    ("liquidationPrice",),
    ("fundingSinceChange",),
)


class ValuationUnavailable(Exception):
    """The API did not value the account. `reason` is the sentence to show in place of the figures."""

    def __init__(self, reason: str, *, http_status: int | None = None, api_error_code: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status
        self.api_error_code = api_error_code

    def result(self, **extra) -> dict:
        """The error result of a command whose output is the figures themselves (`query margin`)."""
        result: dict = {"error": self.reason, "error_code": UNAVAILABLE_CODE, "message": self.reason, **extra}
        if self.http_status is not None:
            result["http_status"] = self.http_status
        if self.api_error_code:
            result["api_error_code"] = self.api_error_code
        return result


def unavailable(exc: BaseException) -> ValuationUnavailable:
    """Why a valuation read failed, as one sentence."""
    if not isinstance(exc, V2StatusError):
        if isinstance(exc, ValueError):
            return ValuationUnavailable(f"The API's valuation could not be read: {exc}")
        return ValuationUnavailable(f"The API's valuation read failed ({type(exc).__name__}). {RETRY}")
    if exc.status == 503:
        # The valuation's own 503 names its code; a gateway's or a maintenance page's does not.
        cause = (
            "a price or market input it needs is missing or stale"
            if exc.code == "SERVICE_UNAVAILABLE_ERROR"
            else "the service is unavailable"
        )
        reason = f"The API could not value this account right now (HTTP 503: {cause}). {RETRY}"
    elif exc.code == "NO_ACCOUNTS_FOUND_ERROR":
        reason = "The API found no accounts for this wallet (NO_ACCOUNTS_FOUND_ERROR), so it has nothing to value."
    else:
        reason = f"The API's valuation read failed (HTTP {exc.status}{' ' + exc.code if exc.code else ''}). {RETRY}"
    return ValuationUnavailable(reason, http_status=exc.status, api_error_code=exc.code)


async def read(v2: V2Client, wallet: str, account_id: int) -> tuple:
    """Both reads for one account through one V2 session, as the API sent them.

    Raises ValuationUnavailable rather than return one without the other. An edge ban (429) and a regional block
    (403) are not about the valuation: they propagate unchanged, so each command reports them as it always does."""
    path = f"/wallet/{identifier(wallet, wallet=True)}/"
    results = await asyncio.gather(
        v2.get(path + "accountSummaries", accountId=account_id),
        v2.get(path + "positionRisk", accountId=account_id),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    for failure in failures:
        banned = isinstance(failure, V2StatusError) and failure.status == 429
        if banned or isinstance(failure, RegionUnavailableError) or not isinstance(failure, READ_ERRORS):
            raise failure
    if failures:
        raise unavailable(failures[0]) from failures[0]
    return results[0], results[1]


async def read_account(config, account_id: int) -> tuple[dict, dict[str, dict]]:
    """One account's valuation (`query`, the strategy preview): see `for_account`."""
    async with V2Client(config.api_url) as v2:
        summaries, risk = await read(v2, config.owner_wallet_address, account_id)
    return for_account(summaries, risk, account_id)


def _checked(row: dict, fields: tuple, label: str) -> dict:
    signed, unsigned, nullable, optional = fields
    for key in signed + unsigned + nullable + optional:
        if key in nullable + optional and row.get(key) is None:
            continue
        if decimal(row.get(key), f"{label} {key}") < 0 and key not in signed + optional:
            raise ValueError(f"{label} {key} must not be negative")
    return row


def for_account(summaries, risk, account_id: int) -> tuple[dict, dict[str, dict]]:
    """The account's accountSummaries row and its positionRisk rows by symbol, in the API's order, once the fields
    the kit uses are checked. Raises ValuationUnavailable: no row for the account is no valuation, never zero."""
    try:
        found = [row for row in rows(summaries) if integer(row.get("accountId"), "accountId") == account_id]
        if not found:
            raise ValuationUnavailable(
                f"The API returned no valuation for account {account_id}; it values the wallet's perpetual margin "
                "accounts only."
            )
        if len(found) > 1:
            raise ValueError(f"accountSummaries returned account {account_id} more than once")
        summary = _checked(found[0], _SUMMARY, "accountSummaries")
        integer(summary.get("updatedAt"), "updatedAt")
        if summary.get("pricesUpdatedAt") is not None:
            integer(summary["pricesUpdatedAt"], "pricesUpdatedAt")
        values: dict[str, dict] = {}
        for row in rows(risk):
            if integer(row.get("accountId"), "accountId") != account_id:
                continue
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or not symbol or symbol in values:
                raise ValueError(f"positionRisk returned a row without a symbol, or {symbol} twice")
            if row.get("side") not in ("A", "B") or not isinstance(row.get("hasCorrelatedCollateral"), bool):
                raise ValueError(f"positionRisk returned an invalid side or hasCorrelatedCollateral for {symbol}")
            integer(row.get("lastTradeSequenceNumber"), "lastTradeSequenceNumber")
            integer(row.get("pricesUpdatedAt"), "pricesUpdatedAt")
            values[symbol] = _checked(row, _POSITION, symbol)
    except ValueError as exc:
        raise unavailable(exc) from exc
    return summary, values


def mismatch(values: dict[str, dict], records) -> str | None:
    """None when the valuation is of exactly the positions recorded, `records` as (symbol, side, qty, last trade
    sequence number); else MISMATCH. One fill between the reads moves every cross-margin figure of the account, so
    the caller leaves out all of them, not only the position it changed."""
    valued = {(s, row["side"], Decimal(row["qty"]), row["lastTradeSequenceNumber"]) for s, row in values.items()}
    recorded = {(symbol, side, Decimal(str(qty)), sequence) for symbol, side, qty, sequence in records}
    return None if recorded == valued else MISMATCH


def _text(value: str | None) -> str | None:
    return None if value is None else format(Decimal(value).normalize(), "f")


def distance_pct(row: dict) -> float | None:
    """How far the mark must move against the position to reach its liquidation price, in % rounded down.
    Negative when the account is already past the line."""
    mark = Decimal(row["markPrice"])
    if row["liquidationPrice"] is None or mark <= 0:
        return None
    price = Decimal(row["liquidationPrice"])
    return _distance_pct((mark - price if row["side"] == "B" else price - mark) / mark)


def account_fields(summary: dict, values: dict[str, dict]) -> dict:
    """`query margin`'s account figures, under the kit's field names and roundings (docs/margin.md). Margin is
    measured against the margin balance; `equity_usd` (before haircuts) is for display."""
    balance, lm, collateral = (
        Decimal(summary[key]) for key in ("marginBalance", "liquidationMarginRequirement", "collateralValue")
    )
    ratio, equity = summary["liquidationMarginRatio"], summary.get("equity")
    usage = None if ratio is None else Decimal(ratio)
    # The engine's health with no order (`_margin.project`), less its rounding allowance for each position.
    allowance = sum((rounding_allowance(Decimal(row["qty"]), 1) for row in values.values()), Decimal(0))
    return {
        "equity_usd": None if equity is None else _usd(Decimal(equity)),
        "margin_balance_usd": _usd(balance),
        "real_balance_usd": _usd(collateral),
        "unrealized_pnl_usd": _usd(Decimal(summary["unrealizedPnl"])),
        "unsettled_funding_estimate_usd": 0.0,
        "liquidation_margin_req_usd": float(lm),
        "initial_margin_req_usd": float(summary["initialMarginRequirement"]),
        "margin_usage": _ratio(usage),
        "margin_usage_pct": _ratio(None if usage is None else usage * 100, "0.01"),
        "liquidatable": balance < lm,
        "margin_buffer_usd": _usd(balance - lm),
        "initial_margin_buffer_usd": _usd(Decimal(summary["freeCollateral"])),
        "health_usd": _usd(min(balance - lm, collateral) - allowance),
        "effective_collateral_usd": round(float(collateral), 4),
        "updated_at": summary["updatedAt"],
        "prices_updated_at": summary["pricesUpdatedAt"],
    }


def position_fields(row: dict, definition) -> dict:
    """`query margin`'s figures for one position; `definition` (or None) gives the market's published rate."""
    funding = row.get("fundingSinceChange")
    return {
        "symbol": row["symbol"],
        "side": "long" if row["side"] == "B" else "short",
        "qty": row["qty"],
        "mark_price_usd": float(row["markPrice"]),
        "notional_usd": round(float(row["notional"]), 4),
        "avg_entry_price": row["avgEntryPrice"],
        "unrealized_pnl_usd": float(row["unrealizedPnl"]),
        "funding_since_change_usd": None if funding is None else float(funding),
        "unsettled_funding_estimate_usd": 0.0,
        "liq_margin_param": None if definition is None else float(definition.liquidation_margin_parameter),
        "liq_margin_req_usd": float(row["liquidationMarginRequirement"]),
        "liquidation_price_usd": _text(row["liquidationPrice"]),
        "distance_to_liquidation_pct": distance_pct(row),
        "has_correlated_collateral": row["hasCorrelatedCollateral"],
    }


def position_valuation(row: dict | None) -> dict:
    """`query positions`' valuation of one position; every field null without one."""
    return {
        "mark_price": row["markPrice"] if row else None,
        "mark_updated_at": row["pricesUpdatedAt"] if row else None,
        "estimated_unrealized_pnl_usd": row["unrealizedPnl"] if row else None,
        "funding_since_change_usd": row.get("fundingSinceChange") if row else None,
        "liquidation_price_usd": _text(row["liquidationPrice"]) if row else None,
        "distance_to_liquidation_pct": distance_pct(row) if row else None,
        "has_correlated_collateral": row["hasCorrelatedCollateral"] if row else None,
    }
