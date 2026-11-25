"""Production-feedback regressions: no real credentials, I/O or trading."""

import argparse
from unittest.mock import AsyncMock

import pytest
from sdk.open_api.api.wallet_data_api import WalletDataApi
from sdk.open_api.models.account import Account
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.pagination_meta import PaginationMeta
from sdk.open_api.models.perp_execution import PerpExecution
from sdk.open_api.models.perp_execution_list import PerpExecutionList
from sdk.open_api.models.side import Side

from agent_kit.scripts import _env, query, trade
from agent_kit.scripts._executions import account_executions, add_settled_fees
from agent_kit.tests.conftest import make_order, make_price, parse_stdout

TEST_KEY = "0x" + "11" * 32


@pytest.fixture
def keyfile(tmp_path, monkeypatch):
    path = tmp_path / "signer.key"
    path.write_text(TEST_KEY + "\n")
    path.chmod(0o600)
    monkeypatch.setenv("PERP_PRIVATE_KEY_FILE", str(path))
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", "0x" + "22" * 20)
    return path


def test_keyfile_works_from_another_directory(keyfile, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert _env.get_trading_config().private_key == TEST_KEY


@pytest.mark.parametrize(
    "failure",
    [
        "permissions",
        "symlink",
        "empty",
        "invalid",
        "missing",
        "relative",
        "directory",
        "oversized",
        "zero",
        "range",
        "fifo",
    ],
)
def test_unsafe_keyfiles_fail_without_exposing_content(keyfile, monkeypatch, failure):
    if failure == "permissions":
        keyfile.chmod(0o644)
    elif failure == "symlink":
        link = keyfile.with_suffix(".link")
        link.symlink_to(keyfile)
        monkeypatch.setenv("PERP_PRIVATE_KEY_FILE", str(link))
        monkeypatch.setenv("REYA_SIGNER", "own-key")
    elif failure == "empty":
        keyfile.write_text("")
    elif failure == "invalid":
        keyfile.write_text(TEST_KEY + "-secret")
    elif failure == "missing":
        keyfile.unlink()
    elif failure == "relative":
        monkeypatch.setenv("PERP_PRIVATE_KEY_FILE", keyfile.name)
        monkeypatch.setenv("REYA_SIGNER", "own-key")
    elif failure == "directory":
        monkeypatch.setenv("PERP_PRIVATE_KEY_FILE", str(keyfile.parent))
        monkeypatch.setenv("REYA_SIGNER", "own-key")
    elif failure == "oversized":
        keyfile.write_text(TEST_KEY + " " * 256)
    elif failure == "zero":
        keyfile.write_text("0" * 64)
    elif failure == "range":
        keyfile.write_text("f" * 64)
    elif failure == "fifo":
        import os

        keyfile.unlink()
        os.mkfifo(keyfile, 0o600)
    with pytest.raises(ValueError) as exc:
        _env.get_trading_config()
    assert TEST_KEY not in str(exc.value)


def test_conflicting_key_sources_fail_closed(keyfile, monkeypatch):
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", TEST_KEY)
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    with pytest.raises(ValueError, match="only one"):
        _env.get_trading_config()


async def test_discovery_does_not_open_keys_or_require_account_id(keyfile, monkeypatch, capsys):
    keyfile.unlink()
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", "deliberately-invalid")
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    read = AsyncMock(return_value=[Account(account_id=8009, name="strategy", type="SUBPERP")])
    monkeypatch.setattr(WalletDataApi, "get_wallet_accounts", read)
    await query.cmd_accounts(query.build_parser().parse_args(["accounts"]))
    assert parse_stdout(capsys) == [{"account_id": 8009, "name": "strategy", "type": "SUBPERP"}]
    assert _env.get_trading_config(include_private_key=False).private_key is None


@pytest.mark.parametrize("command", [["order", "cancel_all"], ["positions", "close_all"]])
@pytest.mark.parametrize("extra", [[], ["--preview"], ["--all", "--symbol", "ETHRUSDPERP"]])
def test_cleanup_requires_one_scope(command, extra, capsys):
    with pytest.raises(SystemExit) as exc:
        trade.build_parser().parse_args(command + extra)
    assert exc.value.code != 0
    assert "error" in parse_stdout(capsys)


@pytest.mark.parametrize("command", [trade.cmd_order_cancel_all, trade.cmd_positions_close_all])
async def test_direct_cleanup_guard_runs_before_any_io(command, patch_client):
    with pytest.raises(ValueError, match="exactly one"):
        await command(argparse.Namespace(symbol=None, all=False))
    patch_client.start.assert_not_awaited()


@pytest.mark.parametrize("value", ["0", "-1", "18446744073709551616", "1.5", "NaN"])
def test_client_id_validation(value, capsys):
    with pytest.raises(SystemExit):
        trade.build_parser().parse_args(
            [
                "order",
                "limit",
                "ETHRUSDPERP",
                "--side",
                "long",
                "--qty",
                "0.1",
                "--price",
                "1900",
                "--client_order_id",
                value,
            ]
        )
    assert "error" in parse_stdout(capsys)


@pytest.mark.parametrize("order_type", ["TAKE_PROFIT", "STOP_LOSS"])
@pytest.mark.parametrize("side,position,close", [("B", "short", "buy"), ("A", "long", "sell")])
async def test_trigger_readback_explains_position_and_quantity(patch_client, capsys, order_type, side, position, close):
    patch_client.get_open_orders.return_value = [
        make_order().model_copy(update={"order_type": OrderType(order_type), "side": Side(side), "qty": None})
    ]
    await query.cmd_orders(query.build_parser().parse_args(["orders"]))
    row = parse_stdout(capsys)[0]
    assert row["position_side"] == position
    assert row["close_side"] == row["order_side"] == close
    assert row["quantity_mode"] == "whole_position" and row["qty"] is None


def execution(**updates):
    values = dict(
        exchange_id=2,
        symbol="ETHRUSDPERP",
        taker_account_id=8009,
        maker_account_id=8010,
        taker_order_id="123456",
        maker_order_id="654321",
        qty="0.1",
        side="B",
        price="120",
        taker_fee="0.003600",
        maker_fee="-0.0002",
        taker_rebate_credit="0.0001",
        taker_opening_fee="0.0003",
        type="ORDER_MATCH",
        timestamp=1791304500000,
        sequence_number=1,
        fill_id="77",
    )
    return PerpExecution(**(values | updates))


def page(*rows):
    return PerpExecutionList(data=list(rows), meta=PaginationMeta(limit=100, count=len(rows)))


def result(**updates):
    return (
        dict(
            order_id="123456", symbol="ETHRUSDPERP", status="FILLED", filled_qty="0.1", first_fill_id="77", fill_count=1
        )
        | updates
    )


@pytest.mark.parametrize("role", ["taker", "maker"])
async def test_fees_correlate_correct_role_and_deduplicate(patch_client, role):
    row = execution()
    patch_client.config.account_id = 8009 if role == "taker" else 8010
    patch_client.wallet.get_wallet_perp_executions.return_value = page(
        row,
        row,
        execution(taker_order_id="other", fill_id="78", sequence_number=2),
        execution(exchange_id=1, fill_id="79", sequence_number=3),
    )
    outcome = result(order_id="123456" if role == "taker" else "654321")
    # The unrelated row must not match the maker's order either.
    patch_client.wallet.get_wallet_perp_executions.return_value.data[2].maker_order_id = "other"
    await add_settled_fees(patch_client, outcome)
    assert outcome["fees_status"] == "complete"
    assert outcome["fees_rusd"] == ("0.003600" if role == "taker" else "-0.0002")
    assert len(outcome["executions"]) == 1
    assert outcome["executions"][0]["role"] == role


@pytest.mark.parametrize(
    "rows,changes,expected",
    [
        ([], {}, "pending"),
        ([execution(qty="0.05")], {}, "partial"),
        ([execution()], {"first_fill_id": "other"}, "partial"),
        ([execution()], {"fill_count": 2}, "partial"),
        ([execution(taker_account_id=1)], {}, "pending"),
        ([execution(symbol="BTCRUSDPERP")], {}, "pending"),
        ([execution(taker_order_id=None)], {}, "pending"),
        ([execution(maker_fee=None)], {"order_id": "654321", "account_id": 8010}, "partial"),
    ],
)
async def test_incomplete_fees_stay_null(patch_client, rows, changes, expected):
    changes = changes.copy()
    patch_client.config.account_id = changes.pop("account_id", 8009)
    patch_client.wallet.get_wallet_perp_executions.return_value = page(*rows)
    outcome = result(**changes)
    await add_settled_fees(patch_client, outcome)
    assert outcome["fees_status"] == expected and outcome["fees_rusd"] is None


async def test_multiple_fills_sum_exactly(patch_client):
    patch_client.wallet.get_wallet_perp_executions.return_value = page(
        execution(qty="0.04"), execution(qty="0.06", fill_id="78", sequence_number=2, taker_fee="0.0011")
    )
    outcome = result(fill_count=2)
    await add_settled_fees(patch_client, outcome)
    assert outcome["fees_status"] == "complete" and outcome["fees_rusd"] == "0.004700"
    assert outcome["executions"][0]["pnl_opening_fee_rusd"] == "0.0003"
    assert outcome["executions"][0]["taker_rebate_credit_rusd"] == "0.0001"


@pytest.mark.parametrize("error", [TimeoutError(), ValueError("bad read")])
async def test_fee_read_failure_preserves_trade_and_does_not_resubmit(patch_client, capsys, error):
    patch_client.markets.get_perp_market_summary.return_value = make_price()
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="123456", status="FILLED", cum_qty="0.1", first_fill_id="77", fill_count=1, client_order_id="123"
    )
    patch_client.wallet.get_wallet_perp_executions.side_effect = error
    await trade.cmd_order_market(
        trade.build_parser().parse_args(
            ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "0.1", "--client_order_id", "123"]
        )
    )
    outcome = parse_stdout(capsys)
    assert outcome["status"] == "FILLED" and "error" not in outcome
    assert (outcome["order_id"], outcome["client_order_id"], outcome["first_fill_id"], outcome["fill_count"]) == (
        "123456",
        "123",
        "77",
        1,
    )
    assert outcome["fees_status"] == "unavailable" and outcome["fees_rusd"] is None
    patch_client.create_limit_order.assert_awaited_once()
    patch_client.wallet.get_wallet_perp_executions.assert_awaited_once()


async def test_unfilled_order_skips_fee_lookup(patch_client):
    outcome = result(filled_qty="0")
    await add_settled_fees(patch_client, outcome)
    assert outcome["fees_status"] == "not_filled" and outcome["fees_rusd"] is None
    patch_client.wallet.get_wallet_perp_executions.assert_not_awaited()


async def test_execution_query_scopes_account_order_and_preserves_page_bounds(patch_client, capsys):
    patch_client.wallet.get_wallet_perp_executions.return_value = page(
        execution(),
        execution(taker_account_id=1, sequence_number=2, fill_id="78"),
        execution(taker_order_id="other", sequence_number=3, fill_id="79"),
    )
    args = query.build_parser().parse_args(
        ["executions", "--order_id", "123456", "--symbol", "ETHRUSDPERP", "--start_time", "10", "--end_time", "20"]
    )
    await query.cmd_executions(args)
    output = parse_stdout(capsys)
    assert len(output["executions"]) == 1
    assert output["source_page"]["count"] == 3
    patch_client.wallet.get_wallet_perp_executions.assert_awaited_once_with(
        address=patch_client.config.owner_wallet_address, start_time=10, end_time=20
    )


def test_execution_projection_keeps_both_account_roles():
    rows = account_executions(page(execution(maker_account_id=8009)), 8009, 2)
    assert [r["role"] for r in rows] == ["taker", "maker"]


async def test_batch_close_submits_all_orders_before_fee_reads(patch_client, capsys):
    from agent_kit.tests.conftest import make_position

    patch_client.get_positions.side_effect = [[make_position(), make_position(symbol="BTCRUSDPERP")], []]
    patch_client.markets.get_perp_market_summary.return_value = make_price()
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="123456", status="FILLED", cum_qty="0.05"
    )

    async def read_fees(**kwargs):
        assert patch_client.create_limit_order.await_count == 2
        return page()

    patch_client.wallet.get_wallet_perp_executions.side_effect = read_fees
    await trade.cmd_positions_close_all(trade.build_parser().parse_args(["positions", "close_all", "--all"]))
    output = parse_stdout(capsys)
    assert output["closed_count"] == 2
    assert all(order["fees_status"] == "pending" for order in output["close_orders"])


@pytest.mark.parametrize("field,value", [("taker_fee", "NaN"), ("taker_fee", "Infinity"), ("qty", "NaN")])
async def test_invalid_settlement_data_never_reports_complete_fees(patch_client, field, value):
    async def malformed_read(**kwargs):
        # The real SDK validates response models during the awaited API read.
        return page(execution(**{field: value}))

    patch_client.wallet.get_wallet_perp_executions.side_effect = malformed_read
    outcome = result()
    await add_settled_fees(patch_client, outcome)
    assert outcome["fees_status"] == "unavailable" and outcome["fees_rusd"] is None


async def test_execution_time_range_rejected_before_reads(patch_client):
    args = query.build_parser().parse_args(["executions", "--start_time", "20", "--end_time", "10"])
    with pytest.raises(ValueError, match="no later"):
        await query.cmd_executions(args)
    patch_client.wallet.get_wallet_perp_executions.assert_not_awaited()


@pytest.mark.parametrize("post_only", [True, False, None])
async def test_order_readback_reports_maker_flag_without_guessing(patch_client, capsys, post_only):
    patch_client.get_open_orders.return_value = [make_order().model_copy(update={"post_only": post_only})]
    await query.cmd_orders(query.build_parser().parse_args(["orders"]))
    row = parse_stdout(capsys)[0]
    assert row["post_only"] is post_only and "side" not in row
    assert row["order_side"] == "buy"


def test_pnl_opening_fee_is_not_relabelled_as_the_fill_fee():
    rows = account_executions(
        page(
            execution(taker_opening_fee=None),
            execution(fill_id="78", sequence_number=2, taker_fee="0.003607", taker_opening_fee="0.003609"),
        ),
        8009,
        2,
    )
    assert rows[0]["pnl_opening_fee_rusd"] is None
    assert rows[1]["pnl_opening_fee_rusd"] == "0.003609"
    assert rows[1]["fee_rusd"] == "0.003607"
    assert "opening_fee_rusd" not in rows[1]


async def test_descending_source_page_is_labelled_before_account_filtering(patch_client, capsys):
    response = page(execution(taker_account_id=1))
    response.meta.start_time = 20
    response.meta.end_time = 10
    patch_client.wallet.get_wallet_perp_executions.return_value = response
    await query.cmd_executions(query.build_parser().parse_args(["executions"]))
    output = parse_stdout(capsys)
    assert output["executions"] == []
    assert output["source_page"] == {
        "limit": 100,
        "count": 1,
        "startTime": 20,
        "endTime": 10,
        "order": "newest_first",
        "newest_timestamp_ms": 20,
        "oldest_timestamp_ms": 10,
        "scope": "wallet_before_local_filters",
    }


async def test_opt_in_fee_polling_finds_delayed_fill_without_repeating_order(patch_client, capsys):
    patch_client.markets.get_perp_market_summary.return_value = make_price()
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="123456", status="FILLED", cum_qty="0.1", first_fill_id="77", fill_count=1
    )
    patch_client.wallet.get_wallet_perp_executions.side_effect = [page(), page(execution())]
    await trade.cmd_order_market(
        trade.build_parser().parse_args(
            [
                "order",
                "market",
                "ETHRUSDPERP",
                "--side",
                "long",
                "--qty",
                "0.1",
                "--fees_wait_s",
                "2",
            ]
        )
    )
    output = parse_stdout(capsys)
    assert output["fees_status"] == "complete" and output["fees_rusd"] == "0.003600"
    assert patch_client.wallet.get_wallet_perp_executions.await_count == 2
    patch_client.create_limit_order.assert_awaited_once()


async def test_fee_polling_is_bounded_and_preserves_unknown_fees(patch_client):
    import asyncio
    import time

    async def stuck(**kwargs):
        await asyncio.sleep(60)

    patch_client.wallet.get_wallet_perp_executions.side_effect = stuck
    output = result()
    started = time.monotonic()
    await add_settled_fees(patch_client, output, wait_s=0.02)
    assert time.monotonic() - started < 1
    assert output["fees_status"] == "unavailable" and output["fees_rusd"] is None
    assert output["order_id"] == "123456" and output["status"] == "FILLED"


@pytest.mark.parametrize("value", ["-1", "6", "nan", "0.5"])
def test_fee_wait_invalid_values_rejected_before_submission(value, capsys):
    with pytest.raises(SystemExit):
        trade.build_parser().parse_args(
            [
                "order",
                "market",
                "ETHRUSDPERP",
                "--side",
                "long",
                "--qty",
                "0.1",
                "--fees_wait_s",
                value,
            ]
        )
    assert "error" in parse_stdout(capsys)
