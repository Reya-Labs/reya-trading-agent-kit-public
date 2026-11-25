"""What a dollar-sized order really sends.

A quantity floors onto the market's step (`qty_step_size`, the service's lot), so "$30 of SOL" at
108.15 becomes 0.27 SOL, about $29.20. The `sizing` block states the requested and effective
notional, the shortfall and the next step up, so the size can be stated before anything is sent.
"""

from decimal import ROUND_CEILING, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def _cents(value: Decimal) -> Decimal:
    # Half-even, like the order plan's `notional_usd`, so the two figures always agree.
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _dollars(value: Decimal) -> str:
    return f"${_cents(value):,.2f}"


def base_asset(symbol: str) -> str:
    return symbol.removesuffix("RUSDPERP") or symbol


def notional_sizing(
    requested: Decimal, qty: Decimal, price: Decimal, step: Decimal | None, *, symbol: str, priced_at: str
) -> dict:
    """The `sizing` block for an order sized from a dollar amount.

    `qty` is what the order sends (already on the step), `price` what the amount was divided by and
    `priced_at` its name (`mark`, `limit`). `next_step_up` and `message` appear only when rounding
    took something off and the step is known.
    """
    effective = qty * price
    shortfall = requested - effective
    sizing: dict = {
        "sized_by": "notional",
        "requested_notional_usd": float(_cents(requested)),
        "effective_notional_usd": float(_cents(effective)),
        "shortfall_usd": float(_cents(shortfall)),
        "shortfall_pct": (
            float((shortfall / requested * 100).quantize(CENT, rounding=ROUND_HALF_UP)) if requested > 0 else 0.0
        ),
        "priced_at": priced_at,
        "price_usd": _plain(price),
        "qty_step": _plain(step) if step else None,
    }
    if shortfall > 0 and step:
        up = qty + step
        base = base_asset(symbol)
        sizing["next_step_up"] = {"qty": _plain(up), "notional_usd": float(_cents(up * price))}
        sizing["message"] = (
            f"{_dollars(requested)} at the {priced_at} {_plain(price)} rounds down to {_plain(qty)} {base} "
            f"(steps of {_plain(step)}) ≈ {_dollars(effective)}, {sizing['shortfall_pct']}% under. The next step "
            f"up is {_plain(up)} {base} ≈ {_dollars(up * price)}."
        )
    return sizing


def minimum_notional(min_qty: Decimal, price: Decimal) -> Decimal:
    """The smallest dollar amount, to the cent, whose quantity at `price` reaches `min_qty`."""
    return (min_qty * price).quantize(CENT, rounding=ROUND_CEILING)


def below_minimum_message(
    requested: Decimal, qty: Decimal, price: Decimal, min_qty: Decimal, *, symbol: str, priced_at: str
) -> str:
    """Why a dollar amount sends nothing: its quantity and the minimum, both with their dollar value."""
    base = base_asset(symbol)
    return (
        f"{_dollars(requested)} at the {priced_at} {_plain(price)} is {_plain(qty)} {base}, below minimum order "
        f"qty {_plain(min_qty)} {base} (≈ {_dollars(min_qty * price)} at this price). Use --notional "
        f"{_plain(minimum_notional(min_qty, price))} or more, or --qty {_plain(min_qty)}."
    )
