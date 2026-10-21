"""Regression scenarios from the production-grounded October review."""

from types import SimpleNamespace

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from agent_kit.scripts import trade
from agent_kit.scripts._paper import simulate
from agent_kit.scripts._performance import performance
from agent_kit.scripts._stream import FeedState
from agent_kit.scripts._v2 import V2Client
from agent_kit.tests.conftest import make_order
from agent_kit.tests.test_paper import NOW, inputs, request, state
from agent_kit.tests.test_performance import fill, pages, transfer


@pytest.mark.parametrize("action", ["cancel", "modify"])
@pytest.mark.parametrize("by_client", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_identifier_collision_requires_scope_before_write(patch_client, action, by_client, reverse, capsys):
    eth, btc = [make_order(order_id="123", symbol=s) for s in ("ETHRUSDPERP", "BTCRUSDPERP")]
    for row in (eth, btc):
        row.client_order_id = "42"
        row.cum_qty = "0"
        row.post_only = False
    patch_client.get_open_orders.return_value = [btc, eth] if reverse else [eth, btc]
    response = SimpleNamespace(
        status="CANCELLED" if action == "cancel" else "OPEN",
        order_id="123",
        client_order_id="42",
        first_fill_id=None,
        fill_count=0,
        cum_qty="0",
        avg_fill_px=None,
        cancel_reason=None,
        cancel_reason_message=None,
        exec_qty="0",
    )
    patch_client.cancel_order.return_value = response
    patch_client.modify_order = AsyncMock(return_value=response)
    command = ["order", action, "--client_order_id" if by_client else "--order_id", "42" if by_client else "123"]
    if action == "modify":
        command += ["--qty", ".05", "--price", "2000"]
    invoke = getattr(trade, "cmd_order_" + action)
    with pytest.raises(ValueError, match="Ambiguous"):
        await invoke(trade.build_parser().parse_args(command))
    patch_client.cancel_order.assert_not_called()
    patch_client.modify_order.assert_not_called()
    await invoke(trade.build_parser().parse_args(command + ["--symbol", "ETHRUSDPERP"]))
    call = getattr(patch_client, action + "_order").call_args
    assert (call.kwargs["symbol"] if action == "cancel" else call.args[0].symbol) == "ETHRUSDPERP"
    assert '"account_id": 8009' in capsys.readouterr().out


async def test_history_preserves_legacy_observations_without_invented_identity(monkeypatch):
    client = V2Client("https://example.invalid/v2")
    old = [fill(sequenceNumber=0, fillId=None), fill(sequenceNumber=0, fillId=None, qty="2")]
    recent = fill(sequenceNumber=5)
    monkeypatch.setattr(client, "get", AsyncMock(return_value={"data": [recent, recent, *old, old[0]], "meta": {}}))
    result = await client.history("/wallet/0x123/perpExecutions")
    assert result["data"] == [recent, *old, old[0]]
    assert result["coverage"]["state"] == "incomplete"
    assert result["coverage"]["unidentified_observations"] == 3
    report = performance(pages(perpExecutions=result["data"]), [7], start_time=0, end_time=1000)
    assert len(report["execution_records"]) == 4
    assert all(r["identity"] is None for r in report["execution_records"][1:])
    assert report["execution_metrics_rusd"]["fee"]["observed_sum"] is None
    assert report["execution_metrics_rusd"]["fee"]["state"] == "unavailable_identity_uncertain"


async def test_recent_and_legacy_page_overlap_retains_evidence(monkeypatch):
    client = V2Client("https://example.invalid/v2")
    newer = [fill(sequenceNumber=i + 1, fillId=str(i + 1)) for i in range(99)]
    old = fill(sequenceNumber=0, fillId=None)
    monkeypatch.setattr(
        client,
        "get",
        AsyncMock(
            side_effect=[
                {"data": [*newer, old], "meta": {"endTime": 500}},
                {"data": [newer[-1], old, {**old, "qty": "2"}], "meta": {}},
            ]
        ),
    )
    result = await client.history("/wallet/0x123/perpExecutions", end_time=1000)
    assert len(result["data"]) == 102
    assert result["coverage"]["unidentified_observations"] == 3


@pytest.mark.parametrize("side,opposite", [("B", "A"), ("A", "B")])
@pytest.mark.parametrize("accounts", [(7,), (8,), (7, 8)])
def test_execution_direction_belongs_to_reported_account(side, opposite, accounts):
    result = performance(pages(perpExecutions=[fill(side=side)]), accounts, start_time=0, end_time=1000)
    for row in result["execution_records"]:
        assert row["side"] == (side if row["role"] == "taker" else opposite)
        assert row["taker_side"] == side


def test_object_summary_updates_and_reconnect():
    channel = "/v2/market/ETHRUSDPERP/summary"
    view = FeedState("https://example.invalid/v2", channel)
    row = {"symbol": "ETHRUSDPERP", "updatedAt": 1, "markPrice": "2000"}
    for _ in range(2):
        view.snapshot(row, now=0)
        for n in (2, 3):
            view.update(
                {"channel": channel, "timestamp": n, "data": {**row, "updatedAt": n, "markPrice": str(2000 + n)}}, now=n
            )
        assert view.view(now=3)["state"] == "ready"
        assert view.view(now=3)["data"][0]["markPrice"] == "2003"
    balances = FeedState("https://example.invalid/v2", "/v2/wallet/0x123/accountBalances")
    balances.snapshot([], now=0)
    with pytest.raises(ValueError, match="array"):
        balances.update({"channel": balances.channel, "timestamp": 1, "data": {"accountId": 7}})


def test_legacy_stream_observations_survive_snapshot_backfill_updates():
    view = FeedState("https://example.invalid/v2", "/v2/wallet/0x123/perpExecutions", capacity=5)
    row = fill(sequenceNumber=0, fillId=None)
    view.snapshot({"data": [row, {**row, "qty": "2"}]}, now=0)
    view.backfill({"data": [row], "coverage": {"state": "bounded"}})
    view.update({"channel": view.channel, "timestamp": 1, "data": [row]}, now=1)
    assert len(view.view(now=1)["data"]) == 4
    assert view.view(now=1)["state"] == "incomplete"
    stable = {**row, "sequenceNumber": 7}
    view.update({"channel": view.channel, "timestamp": 2, "data": [stable]}, now=2)
    with pytest.raises(ValueError, match="Conflicting"):
        view.update({"channel": view.channel, "timestamp": 3, "data": [{**stable, "qty": "9"}]})
    with pytest.raises(ValueError, match="capacity"):
        view.update({"channel": view.channel, "timestamp": 4, "data": [row]})


def test_repeated_same_hash_amount_transfers_remain_ambiguous():
    entries = [transfer(987, 7, "-10", counterpartyAccountId=8), transfer(44, 8, "10", counterpartyAccountId=7)]
    result = performance(pages(transfers=entries), (7, 8), start_time=0, end_time=1000)
    assert result["internal_transfer_legs"][0]["state"] == "balanced_observations"
    assert result["internal_transfer_legs"][0]["net_wallet_cashflow"] is None
    assert result["internal_transfer_legs"][0]["leg_identity_verified"] is False
    repeated = [*entries, {**entries[0], "sequenceNumber": 201}, {**entries[1], "sequenceNumber": 809}]
    result = performance(pages(transfers=repeated), (7, 8), start_time=0, end_time=1000)
    assert len(result["internal_transfer_legs"]) == 1
    assert result["internal_transfer_legs"][0]["state"] == "incomplete_or_ambiguous"
    assert result["internal_transfer_legs"][0]["net_wallet_cashflow"] is None
    assert len(result["transfer_records"]) == 4
    assert all("leg_sequence" not in r for r in result["internal_transfer_legs"])


def test_paper_refuses_subminimum_residual_close_like_the_api():
    # Devnet1 (11 Oct 2026) rejected a reduce-only 0.001 ETH close with
    # PRICE_QTY_BOUNDS_ERROR and a reduce-only 0.01 close of that 0.001 position
    # with REDUCE_ONLY_CONDITION_NOT_MET_ERROR. Paper must not end flat where live cannot.
    definitions, book, summaries = inputs()
    definitions[0].update(qtyStepSize="0.0001", minOrderQty="0.001")
    book["asks"] = [{"px": "101", "qty": "0.0001"}]
    s, entry = simulate(state(), request(qty="0.001"), definitions, book, summaries, NOW)
    assert Decimal(entry["filled_qty"]) == Decimal("0.0001")
    close = request(request_id="close", side="sell", qty="0.0001", price="99", reduce_only=True)
    for bad in (close, {**close, "qty": "0.001"}):
        with pytest.raises(ValueError):
            simulate(s, bad, definitions, book, summaries, NOW)
    assert Decimal(s["positions"]["ETHRUSDPERP"]["qty"]) == Decimal("0.0001")


def test_legacy_order_reconciliation_retains_observations_and_suppresses_quantity():
    from agent_kit.scripts._reconcile import order_reconciliation

    old = fill(sequenceNumber=0, fillId=None, takerOrderId="123", qty="1")
    order = {"accountId": 7, "symbol": "ETHRUSDPERP", "orderId": "123", "cumQty": "3", "sequenceNumber": 5}
    result = order_reconciliation(7, [order], [old, {**old, "qty": "2"}, old], [])
    assert result[0]["settled_qty"] is None
    assert result[0]["outcome"] == "unknown_legacy_identity"
    assert len(result[0]["unidentified_execution_observations"]) == 3


def test_legacy_sdk_execution_projection_preserves_equal_rows_without_fee_total():
    from agent_kit.scripts._executions import account_executions
    from agent_kit.tests.test_operator_feedback import execution, page

    old = execution(sequence_number=0, fill_id=None, exchange_id=0)
    result = account_executions(page(old, old, execution(sequence_number=0, fill_id=None, qty="0.2")), 8009, 2)
    assert len(result) == 3
    assert all(row["identity_verified"] is False for row in result)
    assert result[0]["exchange_scope_verified"] is False
    assert result[0]["exchange_id"] == 0


def test_sdk_execution_projection_rejects_nonzero_identity_conflicts():
    from agent_kit.scripts._executions import account_executions
    from agent_kit.tests.test_operator_feedback import execution, page

    for conflict in (execution(qty="0.2"), execution(sequence_number=2, qty="0.2")):
        with pytest.raises(ValueError, match="Conflicting"):
            account_executions(page(execution(), conflict), 8009, 2)
