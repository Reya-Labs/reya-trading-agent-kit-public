"""Funding as people read it: four decimals in % per hour, one significant digit when that rounds a rate to zero,
and the annualised rate to the hundredth. The same rule as the Reya app and the strategy service's text."""

from decimal import Decimal

import pytest

from agent_kit.scripts._funding import funding_annual_text, funding_display, funding_text


@pytest.mark.parametrize(
    ("rate", "text"),
    [
        ("-0.000082033333333333", "-0.0001%/h"),  # the strategy service's 18-decimal preview value
        ("0.031", "0.0310%/h"),
        ("0.0012", "0.0012%/h"),
        ("0.00005", "0.0001%/h"),  # half away from zero
        ("-0.00005", "-0.0001%/h"),
        ("-0.000032", "-0.00003%/h"),  # rounds to zero at four places: one significant digit
        ("0.0000049", "0.000005%/h"),
        ("0.00004999", "0.00005%/h"),
        ("0.0000096", "0.00001%/h"),  # the carry keeps one digit
        ("0", "0.0000%/h"),
        ("-0", "0.0000%/h"),
        ("0.2916666666666667", "0.2917%/h"),
    ],
)
def test_funding_text(rate: str, text: str) -> None:
    assert funding_text(rate) == text


@pytest.mark.parametrize(
    ("rate", "text"),
    [
        ("-0.000082033333333333", "-0.72% a year"),
        ("0.0012", "10.51% a year"),
        ("-0.0000001", "0.00% a year"),  # a rounded zero has no sign
        ("0", "0.00% a year"),
    ],
)
def test_funding_annual_text(rate: str, text: str) -> None:
    assert funding_annual_text(rate) == text


def test_funding_display_takes_strings_and_decimals_and_refuses_what_is_not_a_number() -> None:
    assert funding_display("-0.000082033333333333") == "-0.0001%/h (-0.72% a year)"
    assert funding_display(Decimal("0.0012")) == "0.0012%/h (10.51% a year)"
    assert funding_display(None) is None
    assert funding_display("n/a") is None
    assert funding_display("NaN") is None
    assert funding_display(True) is None
