"""Tests for the agent-facing CLI contract and the order guardrail validators.

Contract: every failure — including argument-parsing failures — prints
{"error": "..."} to stdout and exits 1, with nothing on stderr. Stock argparse
would print usage text to stderr and exit 2; JsonArgumentParser closes that
gap, and subparsers inherit it automatically through argparse's parser_class
default. The only non-JSON output is --help (exit 0).
"""

import argparse
import json
from decimal import Decimal

import pytest
from sdk.open_api.models.order_type import OrderType

from agent_kit.scripts import query, trade
from agent_kit.scripts._common import MAX_SLIPPAGE, check_slippage_guardrail, slippage_arg
from agent_kit.scripts.trade import _check_trigger_plausibility


def _expect_json_error(parser: argparse.ArgumentParser, argv: list, capsys: pytest.CaptureFixture) -> str:
    """parse_args(argv) must exit 1 with {"error": ...} on stdout and a clean stderr."""
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(argv)
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert set(payload) == {"error"}
    return payload["error"]


class TestJsonArgparseContract:
    def test_trade_invalid_choice_is_json(self, capsys: pytest.CaptureFixture) -> None:
        error = _expect_json_error(
            trade.build_parser(),
            ["order", "market", "ETHRUSDPERP", "--side", "LONG", "--notional", "100"],
            capsys,
        )
        assert "invalid choice: 'LONG'" in error

    def test_trade_missing_required_argument_is_json(self, capsys: pytest.CaptureFixture) -> None:
        error = _expect_json_error(trade.build_parser(), ["order", "market", "ETHRUSDPERP", "--side", "long"], capsys)
        assert "required" in error

    def test_trade_unknown_subcommand_is_json(self, capsys: pytest.CaptureFixture) -> None:
        error = _expect_json_error(trade.build_parser(), ["order", "bogus"], capsys)
        assert "invalid choice" in error

    def test_trade_bad_decimal_value_is_json(self, capsys: pytest.CaptureFixture) -> None:
        error = _expect_json_error(
            trade.build_parser(),
            ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "abc"],
            capsys,
        )
        assert "invalid decimal value" in error

    def test_trade_out_of_range_slippage_is_json(self, capsys: pytest.CaptureFixture) -> None:
        error = _expect_json_error(
            trade.build_parser(),
            ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "1", "--slippage", "0.99"],
            capsys,
        )
        assert "slippage" in error

    def test_query_unknown_command_is_json(self, capsys: pytest.CaptureFixture) -> None:
        error = _expect_json_error(query.build_parser(), ["bogus"], capsys)
        assert "invalid choice" in error

    def test_query_missing_market_action_is_json(self, capsys: pytest.CaptureFixture) -> None:
        _expect_json_error(query.build_parser(), ["market"], capsys)

    def test_error_names_the_failing_subcommand(self, capsys: pytest.CaptureFixture) -> None:
        error = _expect_json_error(
            trade.build_parser(),
            ["order", "market", "ETHRUSDPERP", "--side", "LONG", "--notional", "100"],
            capsys,
        )
        assert error.startswith("trade order market:")

    def test_help_remains_human_readable_exit_zero(self, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit) as exc_info:
            trade.build_parser().parse_args(["--help"])
        assert exc_info.value.code == 0
        assert "usage:" in capsys.readouterr().out


class TestSlippageValidation:
    @pytest.mark.parametrize("text", ["0.01", "0.05", "0.5", "1e-3"])
    def test_accepts_in_range(self, text: str) -> None:
        assert slippage_arg(text) == Decimal(text)

    @pytest.mark.parametrize("text", ["0", "-0.01", "0.51", "1", "abc", "nan"])
    def test_rejects_out_of_range_or_malformed(self, text: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            slippage_arg(text)

    def test_guardrail_allows_up_to_soft_cap(self) -> None:
        check_slippage_guardrail(MAX_SLIPPAGE, force=False)

    def test_guardrail_blocks_above_soft_cap_without_force(self) -> None:
        with pytest.raises(ValueError, match="safety cap"):
            check_slippage_guardrail(Decimal("0.06"), force=False)

    def test_guardrail_force_overrides_soft_cap(self) -> None:
        check_slippage_guardrail(Decimal("0.06"), force=True)


class TestTriggerPlausibility:
    MARK = Decimal("2000")

    @pytest.mark.parametrize(
        "trigger_type, position_side, trigger_px, plausible",
        [
            (OrderType.TAKE_PROFIT, "long", Decimal("2300"), True),
            (OrderType.TAKE_PROFIT, "long", Decimal("1900"), False),
            (OrderType.TAKE_PROFIT, "short", Decimal("1900"), True),
            (OrderType.TAKE_PROFIT, "short", Decimal("2300"), False),
            (OrderType.STOP_LOSS, "long", Decimal("1900"), True),
            (OrderType.STOP_LOSS, "long", Decimal("2300"), False),
            (OrderType.STOP_LOSS, "short", Decimal("2300"), True),
            (OrderType.STOP_LOSS, "short", Decimal("1900"), False),
        ],
    )
    def test_all_eight_cells(
        self, trigger_type: OrderType, position_side: str, trigger_px: Decimal, plausible: bool
    ) -> None:
        if plausible:
            assert _check_trigger_plausibility(trigger_type, position_side, trigger_px, self.MARK, False) is None
        else:
            with pytest.raises(ValueError, match="fire immediately"):
                _check_trigger_plausibility(trigger_type, position_side, trigger_px, self.MARK, False)

    def test_equality_counts_as_implausible(self) -> None:
        with pytest.raises(ValueError, match="fire immediately"):
            _check_trigger_plausibility(OrderType.TAKE_PROFIT, "long", self.MARK, self.MARK, False)

    def test_force_returns_warning_instead_of_raising(self) -> None:
        warning = _check_trigger_plausibility(OrderType.TAKE_PROFIT, "long", Decimal("1900"), self.MARK, True)
        assert warning is not None
        assert "fire immediately" in warning


@pytest.mark.parametrize(
    "entry,argv,handler",
    [
        (trade, ["trade", "order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "0.1"], "cmd_order_market"),
        (query, ["query", "prices"], "cmd_prices"),
    ],
)
async def test_cli_api_rejections_are_structured_without_headers(entry, argv, handler, monkeypatch, capsys):
    import asyncio
    from unittest.mock import AsyncMock

    from sdk.open_api.exceptions import BadRequestException

    error = BadRequestException(
        status=400,
        reason="private reason",
        body=json.dumps(
            {
                "error": "POST_ONLY_WOULD_CROSS_ERROR",
                "message": "Post-only order would cross the book",
                "debug": "private payload",
            }
        ),
    )
    error.headers = {"Set-Cookie": "private cookie", "Authorization": "private token"}
    monkeypatch.setattr(entry, handler, AsyncMock(side_effect=error))
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit) as exc:
        # CLI asyncio.run owns its loop; do not clear pytest's session loop.
        await asyncio.to_thread(entry.main)
    assert exc.value.code == 1
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output == {
        "error_code": "POST_ONLY_WOULD_CROSS_ERROR",
        "http_status": 400,
        "message": "Post-only order would cross the book",
        "error": "Post-only order would cross the book",
    }
    assert "private" not in captured.out and captured.err == ""


@pytest.mark.parametrize(
    "body", [None, "<html>private</html>", '["private"]', '{"error":"bad code","message":"private"}', "x" * 70000]
)
def test_unrecognized_api_errors_do_not_dump_body(body):
    from sdk.open_api.exceptions import ApiException

    from agent_kit.scripts._common import exception_result

    output = exception_result(ApiException(status=503, body=body, reason="private"))
    assert output["error_code"] == "API_ERROR" and output["http_status"] == 503
    assert "private" not in json.dumps(output)
