"""Net fees, the execution type filter, MARKET_CLOSE records, and markets whose underlying closes."""

import argparse
from unittest.mock import AsyncMock

import pytest
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.market_definition import MarketDefinition
from sdk.open_api.models.side import Side

from agent_kit.scripts import data, query, trade
from agent_kit.scripts._common import market_categories, underlying_hours_warning
from agent_kit.scripts._executions import account_executions, net_fee
from agent_kit.scripts._v2 import V2Client
from agent_kit.tests.conftest import (
    DEFAULT_ACCOUNT_ID,
    FakeClient,
    make_balance,
    make_market_def,
    make_order,
    make_position,
    make_price,
    parse_stdout,
)
from agent_kit.tests.test_operator_feedback import execution, page
from agent_kit.tests.test_performance import fill, pages, report

# --------------------------------------------------------------------------
# Net fee
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fee,rebate,role,expected",
    [
        ("0.50", "0.10", "taker", "0.40"),  # the instant rebate comes back to the taker in the same settlement
        ("0.50", "0", "taker", "0.50"),
        ("0.50", None, "taker", None),  # no breakdown on older executions: no assumed zero rebate
        (None, "0.10", "taker", None),
        ("-0.0002", None, "maker", "-0.0002"),  # a maker fee is already net (negative is a rebate)
        (None, None, "maker", None),
        ("abc", "0.1", "taker", None),  # malformed amounts never hide the row
    ],
)
def test_net_fee_sign_and_missing_parts(fee, rebate, role, expected):
    assert net_fee(fee, rebate, role) == expected


def test_account_executions_report_net_fee_next_to_its_parts():
    taker = account_executions(page(execution()), 8009, 2)[0]
    assert (taker["fee_rusd"], taker["taker_rebate_credit_rusd"], taker["net_fee_rusd"]) == (
        "0.003600",
        "0.0001",
        "0.003500",
    )
    maker = account_executions(page(execution()), 8010, 2)[0]
    assert maker["taker_rebate_credit_rusd"] is None and maker["net_fee_rusd"] == "-0.0002"


def test_performance_reports_net_fee_and_keeps_the_parts():
    result = report(pages(perpExecutions=[fill(), fill(sequenceNumber=2, fillId="101")]))
    record = result["execution_records"][0]
    assert (record["fee"], record["rebate_credit"], record["net_fee"]) == ("2", ".1", "1.9")
    metrics = result["execution_metrics_rusd"]
    assert metrics["fee"]["observed_sum"] == "4" and metrics["net_fee"]["observed_sum"] == "3.8"
    assert result["net_trading_return"] is None


# --------------------------------------------------------------------------
# MARKET_CLOSE
# --------------------------------------------------------------------------


def test_market_close_records_are_labelled_apart_from_liquidations():
    result = report(
        pages(
            perpExecutions=[
                fill(type="MARKET_CLOSE", makerAccountId=None),
                fill(sequenceNumber=2, fillId="101", type="LIQUIDATION"),
            ]
        )
    )
    assert [r["type"] for r in result["market_close_execution_records"]] == ["MARKET_CLOSE"]
    assert result["adl_execution_records"] == []
    assert any("MARKET_CLOSE" in line and "delisted" in line for line in result["limitations"])


# --------------------------------------------------------------------------
# Execution type filter
# --------------------------------------------------------------------------


def _trades_args(*extra):
    return data.build_parser().parse_args(["market", "trades", "--symbol", "ETHRUSDPERP", *extra])


async def test_type_filter_is_sent_and_still_applied_locally(monkeypatch):
    rows = [
        {"sequenceNumber": 2, "type": "LIQUIDATION", "symbol": "ETHRUSDPERP"},
        {"sequenceNumber": 1, "type": "ORDER_MATCH", "symbol": "ETHRUSDPERP"},  # a server ignoring the filter
    ]
    get = AsyncMock(return_value={"data": rows, "meta": {"endTime": 0}})
    monkeypatch.setattr(V2Client, "get", get)
    result = await data.run(_trades_args("--type", "LIQUIDATION"))
    assert get.call_args.kwargs["type"] == "LIQUIDATION"
    assert [r["type"] for r in result["data"]] == ["LIQUIDATION"]
    assert result["coverage"]["type_filter"] == {"type": "LIQUIDATION", "sent_to_server": True, "removed_locally": 1}
    # Paging decided from the page as served: two rows, below the page size, so history is exhausted.
    assert result["coverage"]["reason"] == "available_history_exhausted"


async def test_no_type_filter_sends_no_parameter(monkeypatch):
    get = AsyncMock(return_value={"data": [], "meta": {"endTime": 0}})
    monkeypatch.setattr(V2Client, "get", get)
    result = await data.run(_trades_args())
    assert "type" not in get.call_args.kwargs and "type_filter" not in result["coverage"]


def test_type_filter_accepts_only_documented_types(capsys):
    with pytest.raises(SystemExit):
        _trades_args("--type", "TRADE")


# --------------------------------------------------------------------------
# Categories and the hours warning
# --------------------------------------------------------------------------


def _definition(symbol="AAPLRUSDPERP", categories=("EQUITIES",)) -> MarketDefinition:
    row = make_market_def(symbol=symbol).to_dict()
    if categories is not None:
        row["categories"] = list(categories)
    return MarketDefinition.from_dict(row)


class _WithField:
    categories = ["INDICES"]
    additional_properties = {"categories": ["CRYPTO"]}


@pytest.mark.parametrize(
    "source,expected",
    [
        (_WithField(), ["INDICES"]),  # a newer SDK's own field wins
        (_definition(categories=("COMMODITIES",)), ["COMMODITIES"]),  # an older SDK keeps it unparsed
        ({"symbol": "SPYRUSDPERP", "categories": ["INDICES"]}, ["INDICES"]),  # a raw V2 row
        (_definition(categories=None), None),
        ({"categories": []}, None),
        ({"categories": "EQUITIES"}, None),  # malformed: never guessed
        (None, None),
    ],
)
def test_categories_are_read_from_each_source_and_never_inferred(source, expected):
    assert market_categories(source) == expected


@pytest.mark.parametrize(
    "categories,warns",
    [(["EQUITIES"], True), (["INDICES"], True), (["COMMODITIES"], True), (["CRYPTO"], False), (None, False)],
)
def test_hours_warning_is_for_markets_whose_underlying_closes(categories, warns):
    warning = underlying_hours_warning("XRUSDPERP", categories)
    assert bool(warning) is warns
    if warning:
        assert "stop-loss" in warning and "gap" in warning


def _arm_order(client: FakeClient, categories):
    client.reference.get_perp_market_definitions.return_value = [
        _definition(symbol="ETHRUSDPERP", categories=categories)
    ]
    client.markets.get_perp_market_summary.return_value = make_price(symbol="ETHRUSDPERP", oracle_price="2000")


@pytest.mark.parametrize(
    "categories,warns", [(("EQUITIES",), True), (("COMMODITIES",), True), (("CRYPTO",), False), (None, False)]
)
@pytest.mark.parametrize("command", ["market", "limit"])
async def test_order_preview_carries_the_hours_warning(patch_client, capsys, command, categories, warns):
    _arm_order(patch_client, categories)
    argv = ["order", command, "ETHRUSDPERP", "--side", "long", "--qty", "0.01", "--preview"]
    if command == "limit":
        argv += ["--price", "1990"]
    await getattr(trade, f"cmd_order_{command}")(trade.build_parser().parse_args(argv))
    output = parse_stdout(capsys)
    assert output["preview"] is True
    assert ("market_hours_warning" in output) is warns


async def test_order_result_carries_the_hours_warning_and_a_reduce_only_close_does_not(patch_client, capsys):
    _arm_order(patch_client, ("EQUITIES",))
    patch_client.create_limit_order.return_value = CreateOrderResponse(
        order_id="1", status="FILLED", cum_qty="0.01", avg_fill_px="2000"
    )
    await trade.cmd_order_market(
        trade.build_parser().parse_args(["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "0.01"])
    )
    assert "market_hours_warning" in parse_stdout(capsys)

    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B, qty="0.01")
    ]
    await trade.cmd_order_market(
        trade.build_parser().parse_args(
            ["order", "market", "ETHRUSDPERP", "--side", "short", "--qty", "0.01", "--reduce_only", "--preview"]
        )
    )
    assert "market_hours_warning" not in parse_stdout(capsys)


@pytest.mark.parametrize("categories,warns", [(("INDICES",), True), (("CRYPTO",), False), (None, False)])
async def test_positions_carry_the_hours_warning(patch_client, capsys, categories, warns):
    patch_client.reference.get_perp_market_definitions.return_value = [
        _definition(symbol="SPYRUSDPERP", categories=categories)
    ]
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="SPYRUSDPERP", side=Side.B, qty="1")
    ]
    await query.cmd_positions(argparse.Namespace(symbol=None))
    (position,) = parse_stdout(capsys)
    assert ("market_hours_warning" in position) is warns
    # One definitions read serves the warning and the margin figures.
    assert patch_client.reference.get_perp_market_definitions.await_count == 1


async def test_positions_take_the_hours_warning_from_the_snapshot_when_the_first_read_fails(patch_client, capsys):
    patch_client.reference.get_perp_market_definitions.side_effect = [
        RuntimeError("definitions unavailable"),
        [_definition(symbol="SPYRUSDPERP", categories=("INDICES",))],
    ]
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="SPYRUSDPERP", side=Side.B, qty="1")
    ]
    patch_client.get_account_balances.return_value = [make_balance(account_id=DEFAULT_ACCOUNT_ID, real_balance="1000")]
    patch_client.markets.get_perp_markets_summary.return_value = [make_price(symbol="SPYRUSDPERP", oracle_price="500")]
    await query.cmd_positions(argparse.Namespace(symbol=None))
    (position,) = parse_stdout(capsys)
    assert position["account_margin_usage"] is not None  # the snapshot read the definitions again
    assert "market_hours_warning" in position


async def test_order_modify_carries_the_hours_warning(patch_client, capsys):
    _arm_order(patch_client, ("EQUITIES",))
    patch_client.get_open_orders.return_value = [
        make_order(order_id="123", symbol="ETHRUSDPERP", qty="0.02", limit_px="1990").model_copy(
            update={"post_only": True, "cum_qty": "0"}
        )
    ]
    patch_client.modify_order = AsyncMock(return_value=CreateOrderResponse(order_id="123", status="OPEN", cum_qty="0"))
    await trade.cmd_order_modify(
        trade.build_parser().parse_args(["order", "modify", "--order_id", "123", "--qty", "0.03", "--price", "1990"])
    )
    assert "market_hours_warning" in parse_stdout(capsys)


async def test_margin_positions_carry_the_hours_warning(patch_client, capsys):
    patch_client.reference.get_perp_market_definitions.return_value = [
        _definition(symbol="SPYRUSDPERP", categories=("INDICES",)),
        _definition(symbol="ETHRUSDPERP", categories=("CRYPTO",)),
    ]
    patch_client.get_positions.return_value = [
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="SPYRUSDPERP", side=Side.B, qty="1"),
        make_position(account_id=DEFAULT_ACCOUNT_ID, symbol="ETHRUSDPERP", side=Side.B, qty="0.01"),
    ]
    patch_client.get_account_balances.return_value = [make_balance(account_id=DEFAULT_ACCOUNT_ID, real_balance="1000")]
    patch_client.markets.get_perp_markets_summary.return_value = [
        make_price(symbol="SPYRUSDPERP", oracle_price="500"),
        make_price(symbol="ETHRUSDPERP", oracle_price="2000"),
    ]
    await query.cmd_margin(argparse.Namespace())
    rows = {p["symbol"]: p for p in parse_stdout(capsys)["positions"]}
    assert "market_hours_warning" in rows["SPYRUSDPERP"] and "market_hours_warning" not in rows["ETHRUSDPERP"]


async def test_market_info_shows_categories(monkeypatch, capsys):
    from sdk.open_api.api.reference_data_api import ReferenceDataApi

    monkeypatch.setattr(
        ReferenceDataApi,
        "get_perp_market_definitions",
        AsyncMock(return_value=[_definition(), _definition(symbol="ETHRUSDPERP", categories=None)]),
    )
    await query.cmd_market_info(argparse.Namespace(symbol=None))
    rows = {r["symbol"]: r for r in parse_stdout(capsys)}
    assert rows["AAPLRUSDPERP"]["categories"] == ["EQUITIES"] and rows["ETHRUSDPERP"]["categories"] is None


def test_hours_warning_has_no_figures():
    warning = underlying_hours_warning("AAPLRUSDPERP", ["EQUITIES"]) or ""
    assert not any(ch.isdigit() for ch in warning) and warning.count(".") <= 1
