"""Funding rates as people read them: the exact value stays in the data, the text is rounded.

Reya funding is quoted in per cent per hour (positive: longs pay shorts). The text follows the Reya app's markets
table and trade page: four decimals, a minus sign only, `%/h`. A rate that is not zero but rounds to zero there keeps
one significant digit, so it never reads as no funding. The annualised figure is the hourly rate times 8,760, to the
hundredth. Every rounding is half away from zero, on the exact decimal, never a float.
"""

from typing import Any

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

HOURS_PER_YEAR = 8760
_PLACES = Decimal("0.0001")
_CENT = Decimal("0.01")


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        rate = Decimal(str(value))
    except InvalidOperation:
        return None
    return rate if rate.is_finite() else None


def _signed(amount: Decimal) -> str:
    """Fixed-point text with a minus sign only; a rounded zero has no sign."""
    if amount == 0:
        amount = abs(amount)
    return format(amount, "f")


def funding_text(value: Any) -> str | None:
    """Per cent per hour as text: `-0.0001%/h`, `0.0310%/h`, `-0.00003%/h`, `0.0000%/h`. None for a non-number."""
    rate = _decimal(value)
    if rate is None:
        return None
    if rate == 0:
        return "0.0000%/h"
    rounded = rate.quantize(_PLACES, rounding=ROUND_HALF_UP)
    if rounded == 0:
        # One significant digit: the exponent of the leading digit, then half away from zero there. Rounding up can
        # carry into the next place (0.0000096 → 0.000010), so the trailing zero goes.
        rounded = rate.quantize(Decimal(1).scaleb(rate.adjusted()), rounding=ROUND_HALF_UP).normalize()
    return f"{_signed(rounded)}%/h"


def funding_annual_text(value: Any) -> str | None:
    """The hourly rate times 8,760, to the hundredth: `-0.72% a year`. None for a non-number."""
    rate = _decimal(value)
    if rate is None:
        return None
    yearly = (rate * HOURS_PER_YEAR).quantize(_CENT, rounding=ROUND_HALF_UP)
    return f"{_signed(yearly)}% a year"


def funding_display(value: Any) -> str | None:
    """Both, as the kit's text fields give them: `-0.0001%/h (-0.72% a year)`. None for a non-number."""
    hourly, yearly = funding_text(value), funding_annual_text(value)
    if hourly is None or yearly is None:
        return None
    return f"{hourly} ({yearly})"
