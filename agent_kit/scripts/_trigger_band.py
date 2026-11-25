"""The matching engine's TP/SL limit band, mirrored so the kit refuses what the venue would reject.

The engine admits a TP/SL only when |limit_px - trigger_px| <= trigger_px x band. It compares in
E9 integer prices with the bound floored (the matching engine's `TriggerBand::admits`), so the
kit computes it the same way and agrees with the engine exactly at the boundary.

The band is the engine's FILL_PRICE_MAX_DEVIATION_BPS less SLTP_BAND_MARGIN_BPS. Verified on
21 Oct 2026: 500 - 200 = 300 bps for every market on mainnet and devnet1, with no per-market
overrides, and the on-chain fillPriceMaxDeviation is 5% for every market. This constant
mirrors that engine configuration and goes stale if it changes; that is why `order sl` /
`order tp` accept --force for it outside the runner.

A market with no configured band answers the same code, TRIGGER_LIMIT_OUTSIDE_BAND_ERROR; only
the engine's message (passed through by the API) tells the two apart. Stand-alone: paper
imports this module too, so it imports nothing from the live trading path.
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

TRIGGER_BAND_BPS = 300
TRIGGER_BAND = Decimal(TRIGGER_BAND_BPS) / 10000  # 0.03: also the --trigger_slippage ceiling
BAND_ERROR_CODE = "TRIGGER_LIMIT_OUTSIDE_BAND_ERROR"
CHECK = "trigger_band"

_E9 = 9
_BPS_SCALE = 10000


def _e9(price: Decimal) -> int | None:
    """The engine's E9 integer for a price, or None when it has more than 9 decimals (the API refuses those)."""
    scaled = price.scaleb(_E9)
    return int(scaled) if scaled == scaled.to_integral_value() else None


def _on_tick(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (price / tick).to_integral_value(rounding=rounding) * tick


def _text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def admits(trigger_px: Decimal, limit_px: Decimal) -> bool | None:
    """The engine's verdict on this limit at the mirrored band; None when a price is not an E9 value."""
    trigger, limit = _e9(trigger_px), _e9(limit_px)
    if trigger is None or limit is None:
        return None
    bound = trigger * TRIGGER_BAND_BPS // _BPS_SCALE
    return abs(trigger - limit) <= bound


def admissible_limits(trigger_px: Decimal, tick: Decimal) -> tuple[Decimal, Decimal] | None:
    """The lowest and highest on-tick limit prices the band admits around this trigger."""
    trigger = _e9(trigger_px)
    if trigger is None or tick <= 0:
        return None
    bound = trigger * TRIGGER_BAND_BPS // _BPS_SCALE
    low = Decimal(trigger - bound).scaleb(-_E9)
    high = Decimal(trigger + bound).scaleb(-_E9)
    return _on_tick(low, tick, ROUND_CEILING), _on_tick(high, tick, ROUND_FLOOR)


def distance_bps(trigger_px: Decimal, limit_px: Decimal) -> Decimal:
    return (abs(limit_px - trigger_px) / trigger_px * _BPS_SCALE).quantize(Decimal("0.01"))


def _range_fields(trigger_px: Decimal, limit_px: Decimal, tick: Decimal) -> dict:
    fields: dict = {
        "band_bps": TRIGGER_BAND_BPS,
        "distance_bps": str(distance_bps(trigger_px, limit_px)),
    }
    bounds = admissible_limits(trigger_px, tick)
    if bounds is not None:
        low, high = bounds
        fields["admissible_limit_px"] = {"min": _text(low), "max": _text(high)}
        fields["closest_limit_px"] = _text(low if limit_px < trigger_px else high)
    return fields


def refusal(kind: str, trigger_px: Decimal, limit_px: Decimal, tick: Decimal) -> dict | None:
    """A `refused_by` entry when this TP/SL's limit is outside the band; None when the engine would admit it.

    `kind` is "take-profit" or "stop-loss". Prices are already on the market's tick.
    """
    if admits(trigger_px, limit_px) is not False:
        return None
    fields = _range_fields(trigger_px, limit_px, tick)
    closest = fields.get("closest_limit_px")
    message = (
        f"The {kind}'s --limit_px {_text(limit_px)} is {fields['distance_bps']} bps from its trigger "
        f"{_text(trigger_px)}, outside the venue's {TRIGGER_BAND_BPS} bps trigger band, so the venue would reject it "
        f"({BAND_ERROR_CODE}); nothing was sent."
        + (
            f" Use a --limit_px from {fields['admissible_limit_px']['min']} to "
            f"{fields['admissible_limit_px']['max']}; the closest is {closest}."
            if closest is not None
            else ""
        )
        + " Re-run with --force only if the venue's band has been widened."
    )
    return {"check": CHECK, "message": message, "override": "--force", **fields}


def classify_rejection(message: str | None) -> str:
    """Which of the two causes behind TRIGGER_LIMIT_OUTSIDE_BAND_ERROR the venue's message names.

    "no_band": the market has no trigger band, so it accepts no TP/SL at all. "outside_band": the
    limit is too far from the trigger. "unknown": the message names neither (the API's fallback
    text, used when the engine sends none, names both).
    """
    text = (message or "").lower()
    if "unconfigured" in text:
        return "no_band"
    if "outside" in text and "band" in text:
        return "outside_band"
    return "unknown"


def rejection(symbol: str, message: str | None, trigger_px: Decimal, limit_px: Decimal, tick: Decimal) -> dict:
    """Plain wording and the band facts for a venue's TRIGGER_LIMIT_OUTSIDE_BAND_ERROR."""
    reason = classify_rejection(message)
    if reason == "no_band":
        explanation = (
            f"{symbol} accepts no TP/SL: the venue has no trigger band configured for this market, so no "
            "--limit_px is admissible and a position here cannot carry a stop-loss. Do not open a protected entry "
            f"(--sl/--tp) on {symbol}; to exit a position there, close it with `positions close_all --symbol "
            f"{symbol}` (preview first)."
        )
        return {"reason": reason, "explanation": explanation}
    fields = _range_fields(trigger_px, limit_px, tick)
    bounds = fields.get("admissible_limit_px")
    in_band = admits(trigger_px, limit_px)
    if reason == "outside_band":
        explanation = (
            f"The limit {_text(limit_px)} is {fields['distance_bps']} bps from the trigger {_text(trigger_px)}, "
            "outside the venue's trigger band."
        )
        if in_band:
            explanation += (
                f" The kit expects a {TRIGGER_BAND_BPS} bps band, so the venue's band is now narrower: move the limit "
                "closer to the trigger."
            )
        elif bounds is not None:
            explanation += f" Use a --limit_px from {bounds['min']} to {bounds['max']}."
    else:
        explanation = (
            f"Either the limit {_text(limit_px)} is too far from the trigger {_text(trigger_px)} (the band is "
            f"{TRIGGER_BAND_BPS} bps"
            + (f": a --limit_px from {bounds['min']} to {bounds['max']}" if bounds is not None else "")
            + f"), or {symbol} accepts no TP/SL at all. The venue's message does not say which; if a limit "
            "inside that range is also rejected, the market accepts no TP/SL."
        )
    return {"reason": reason, "explanation": explanation, **fields}
