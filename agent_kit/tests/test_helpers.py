"""Tests for the pure helper functions in trade.py: _decimal_arg, _fmt, _quantize, _round_qty, _round_price.

These handle the qty-step and tick-size rounding applied to every order before
submission, in exact decimal arithmetic. Several values below (0.29 at step
0.01, 1.15 at step 0.05, 3245.7 at tick 0.001) are regressions for the v1
float implementation, whose float-division floor silently lost one step.
"""

import argparse
from decimal import Decimal

import pytest

from agent_kit.scripts._common import _decimal_arg
from agent_kit.scripts.trade import _fmt, _quantize, _round_price, _round_qty

D = Decimal


class TestDecimalArg:
    def test_parses_exact_value(self) -> None:
        assert _decimal_arg("0.29") == D("0.29")

    def test_accepts_exponent_input(self) -> None:
        # Output is always fixed-point; exponent notation is fine on input.
        assert _decimal_arg("1e-3") == D("0.001")

    def test_accepts_underscore_separators_like_float_did(self) -> None:
        assert _decimal_arg("1_000") == D("1000")

    @pytest.mark.parametrize("text", ["abc", "", "nan", "inf", "-inf"])
    def test_rejects_malformed_and_non_finite(self, text: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="invalid decimal value"):
            _decimal_arg(text)


class TestFmt:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("0.000001", "0.000001"),  # the float path emitted '1e-06' here
            ("0.00002301", "0.00002301"),
            ("2020.000", "2020"),
            ("0.0500", "0.05"),
            ("2150.41", "2150.41"),
            ("101.50", "101.5"),
        ],
    )
    def test_fixed_point_never_scientific(self, value: str, expected: str) -> None:
        formatted = _fmt(D(value))
        assert formatted == expected
        assert "e" not in formatted.lower()


class TestQuantize:
    @pytest.mark.parametrize(
        "value, step, direction, expected",
        [
            # Float-hostile regressions: the old float floor lost a step on these.
            ("0.29", "0.01", "down", "0.29"),
            ("1.15", "0.05", "down", "1.15"),
            ("3245.7", "0.001", "down", "3245.7"),
            # Non-power-of-10 steps (no Reya market uses one today; the float
            # version rounded these off-grid).
            ("101.30", "0.25", "down", "101.25"),
            ("101.30", "0.25", "up", "101.50"),
            ("101.30", "0.25", "nearest", "101.25"),
            ("0.075", "0.025", "down", "0.075"),
        ],
    )
    def test_exact_for_arbitrary_steps(self, value: str, step: str, direction: str, expected: str) -> None:
        assert _quantize(D(value), D(step), direction) == D(expected)

    def test_nearest_ties_round_up(self) -> None:
        # Half-up, away from floor — deterministic, unlike float round()'s banker's rounding.
        assert _quantize(D("2000.005"), D("0.01"), "nearest") == D("2000.01")
        assert _quantize(D("100.125"), D("0.25"), "nearest") == D("100.25")

    def test_rejects_negative_value(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            _quantize(D("-2000.009"), D("0.01"), "down")


class TestRoundQty:
    def test_floors_to_step(self) -> None:
        # 0.0157 with step 0.001 → 0.015 (floor, not nearest).
        assert _round_qty(D("0.0157"), D("0.001"), D("0.001")) == D("0.015")

    def test_already_aligned_passes_through(self) -> None:
        assert _round_qty(D("0.05"), D("0.01"), D("0.01")) == D("0.05")

    def test_explicit_qty_on_step_is_never_shrunk(self) -> None:
        # Regression: the float floor turned an explicit --qty 0.29 into 0.28.
        assert _round_qty(D("0.29"), D("0.01"), D("0.01")) == D("0.29")

    def test_qty_exactly_at_minimum_passes(self) -> None:
        assert _round_qty(D("0.01"), D("0.001"), D("0.01")) == D("0.01")

    def test_btc_realistic_size(self) -> None:
        # $100 notional at $71k → 0.001408... → step 0.0001 → 0.0014
        raw = D("100") / D("71000")
        assert _round_qty(raw, D("0.0001"), D("0.001")) == D("0.0014")

    def test_eth_realistic_size(self) -> None:
        # $100 notional at $2100 → 0.04761... → step 0.001 → 0.047
        raw = D("100") / D("2100")
        assert _round_qty(raw, D("0.001"), D("0.01")) == D("0.047")

    def test_raises_when_below_min_qty(self) -> None:
        with pytest.raises(ValueError, match="below minimum order qty"):
            _round_qty(D("0.001"), D("0.01"), D("0.01"))

    def test_error_message_suggests_remedy(self) -> None:
        with pytest.raises(ValueError, match="Increase --qty or --notional"):
            _round_qty(D("0.001"), D("0.01"), D("0.01"))

    def test_error_message_uses_fixed_point_values(self) -> None:
        # A notional division produces 28-digit Decimals; the message must not leak them.
        raw = D("100") / D("2100")  # 0.04761904761904761904761904762
        with pytest.raises(ValueError, match=r"qty 0\.047 is below minimum order qty 0\.05\."):
            _round_qty(raw, D("0.001"), D("0.05"))


class TestRoundPrice:
    def test_round_up_ceils_to_tick(self) -> None:
        assert _round_price(D("2000.001"), D("0.01"), "up") == D("2000.01")

    def test_round_down_floors_to_tick(self) -> None:
        assert _round_price(D("2000.009"), D("0.01"), "down") == D("2000.00")

    def test_round_nearest(self) -> None:
        assert _round_price(D("2000.12345"), D("0.01"), "nearest") == D("2000.12")

    def test_aligned_price_unchanged(self) -> None:
        assert _round_price(D("2000.5"), D("0.001"), "nearest") == D("2000.5")

    def test_aligned_price_never_drops_a_tick(self) -> None:
        # Regression: the float floor turned 3245.7 into 3245.699 at tick 0.001.
        assert _round_price(D("3245.7"), D("0.001"), "down") == D("3245.7")

    def test_integer_tick(self) -> None:
        assert _round_price(D("2001.5"), D("1.0"), "down") == D("2001")

    @pytest.mark.parametrize(
        "price, tick, direction, expected",
        [
            # buy market-with-slippage uses "up" so the IOC fills
            ("2136.741", "0.001", "up", "2136.741"),
            ("2136.7411", "0.001", "up", "2136.742"),
            # sell market-with-slippage uses "down" so the IOC fills
            ("2136.7419", "0.001", "down", "2136.741"),
            # GTC limit buy uses "down" (less aggressive than user asked)
            ("2000.0049", "0.01", "down", "2000.0"),
            # GTC limit sell uses "up" (less aggressive than user asked)
            ("2000.0051", "0.01", "up", "2000.01"),
        ],
    )
    def test_direction_matrix(self, price: str, tick: str, direction: str, expected: str) -> None:
        assert _round_price(D(price), D(tick), direction) == D(expected)
