"""Acknowledgement/settlement separation and bounded evidence checks."""

from types import SimpleNamespace

import copy
import time
from unittest.mock import AsyncMock

import pytest

from agent_kit.scripts import _settlement
from agent_kit.scripts._settlement import SettlementIncomplete, read_settlement, reconcile_acknowledgements


def ack(order="10", side="B", status="FILLED", qty=".01", first="77"):
    return {
        "symbol": "ETHRUSDPERP",
        "order_id": order,
        "side": side,
        "status": status,
        "filled_qty": qty,
        "first_fill_id": first,
    }


def fill(order="10", side="B", fill_id="77", seq=7, **kw):
    return {
        "symbol": "ETHRUSDPERP",
        "exchangeId": 2,
        "takerAccountId": 8009,
        "makerAccountId": 9000,
        "takerOrderId": order,
        "makerOrderId": "99",
        "side": side,
        "qty": ".01",
        "fillId": fill_id,
        "sequenceNumber": seq,
        **kw,
    }


def reconcile(acks, settled=(), busts=(), history=()):
    return reconcile_acknowledgements(
        acks, {"perpExecutions": list(settled), "executionBusts": list(busts), "orderHistory": list(history)}, 8009, 2
    )


def test_acknowledged_entry_is_not_proven_flat_by_absent_or_settled_fill():
    with pytest.raises(ValueError, match="incomplete"):
        reconcile([ack()])
    result = reconcile([ack()], [fill()])
    assert result["state"] == "settled_exposure"
    assert result["net_settled_qty"] == {"ETHRUSDPERP": "0.01"}


def test_settled_entry_requires_settled_close_and_bust_is_not_a_close():
    acks = [ack(), ack(order="11", side="A", first="78")]
    close = fill(order="11", side="A", fill_id="78", seq=8)
    with pytest.raises(ValueError, match="incomplete"):
        reconcile(acks, [fill()])
    assert reconcile(acks, [fill()], [close])["state"] == "settled_exposure"
    result = reconcile(acks, [fill(), close, close])
    assert result["state"] == "settled_flat"
    assert result["net_settled_qty"] == {"ETHRUSDPERP": "0.00"}


def test_busted_entry_and_cancelled_zero_fill_can_reconcile_without_exposure():
    result = reconcile([ack()], busts=[fill()])
    assert result["state"] == "settled_flat"
    assert result["orders"][0]["busted_qty"] == "0.01"
    assert reconcile([ack(qty="0", first=None, status="CANCELLED")])["state"] == "settled_flat"


def test_open_ack_requires_terminal_history_including_later_fills():
    a = ack(status="OPEN", qty="0", first=None)
    with pytest.raises(ValueError, match="Terminal"):
        reconcile([a])
    history = {
        "symbol": "ETHRUSDPERP",
        "exchangeId": 2,
        "accountId": 8009,
        "orderId": "10",
        "sequenceNumber": 9,
        "cumQty": ".01",
        "status": "CANCELLED",
    }
    with pytest.raises(ValueError, match="incomplete"):
        reconcile([a], history=[history])
    assert reconcile([a], [fill()], history=[history])["state"] == "settled_exposure"
    assert reconcile([a], busts=[fill()], history=[history])["state"] == "settled_flat"
    with pytest.raises(ValueError, match="not terminal"):
        reconcile([a], history=[{**history, "status": "OPEN"}])


@pytest.mark.parametrize(
    "bad",
    [
        {"sequenceNumber": 0},
        {"fillId": None},
        {"side": "A"},
        {"qty": "0"},
        {"exchangeId": 1},
        {"symbol": "BTCRUSDPERP"},
        {"takerAccountId": 9001},
    ],
)
def test_malformed_legacy_or_wrong_scope_cannot_satisfy_acknowledgement(bad):
    with pytest.raises(ValueError):
        reconcile([ack()], [fill(**bad)])


def test_conflicting_settlement_and_bust_never_double_count():
    with pytest.raises(ValueError, match="Conflicting"):
        reconcile([ack()], [fill()], [fill()])
    with pytest.raises(ValueError, match="Conflicting"):
        reconcile([ack()], [fill(), fill(qty=".02")])


def test_maker_fill_side_is_inverted_and_history_immutability_is_checked():
    assert reconcile([ack(side="A")], [fill(makerAccountId=8009, takerAccountId=9000, makerOrderId="10")])[
        "net_settled_qty"
    ] == {"ETHRUSDPERP": "-0.01"}
    row = {
        "symbol": "ETHRUSDPERP",
        "exchangeId": 2,
        "accountId": 8009,
        "orderId": "10",
        "sequenceNumber": 9,
        "cumQty": "0",
        "status": "CANCELLED",
    }
    changed = copy.deepcopy(row)
    changed["cumQty"] = ".01"
    with pytest.raises(ValueError, match="conflicting"):
        reconcile([ack(status="OPEN", qty="0", first=None)], history=[row, changed])


def test_incomplete_evidence_names_every_unconfirmed_order():
    acks = [ack(), ack(order="11", side="A", first="78"), ack(order="12", first="79")]
    with pytest.raises(SettlementIncomplete, match="incomplete for ETHRUSDPERP/11") as raised:
        reconcile(acks, [fill()])
    assert [(o["order_id"], o["reason"]) for o in raised.value.orders] == [
        ("11", "Settlement/bust evidence incomplete for ETHRUSDPERP/11"),
        ("12", "Settlement/bust evidence incomplete for ETHRUSDPERP/12"),
    ]


def page(*rows):
    return SimpleNamespace(data=[SimpleNamespace(to_dict=lambda row=row: row) for row in rows])


def settlement_client(executions):
    config = SimpleNamespace(owner_wallet_address="0x" + "ab" * 20, account_id=8009, dex_id_override=2)
    wallet = SimpleNamespace(
        get_wallet_order_history=AsyncMock(return_value=page()),
        get_wallet_perp_executions=AsyncMock(side_effect=executions),
        get_wallet_execution_busts=AsyncMock(return_value=page()),
    )
    return SimpleNamespace(config=config, wallet=wallet)


@pytest.fixture
def quick_backoff(monkeypatch):
    monkeypatch.setattr(_settlement, "SETTLEMENT_BACKOFF_S", 0.01)
    monkeypatch.setattr(_settlement, "SETTLEMENT_BACKOFF_MAX_S", 0.04)


async def test_settlement_reads_back_off_across_the_budget_until_the_fill_is_indexed(quick_backoff):
    # Fills were indexed 12-13 s after settling on devnet1: three reads within 1.4 s all missed them.
    client = settlement_client([page()] * 4 + [page(fill())])
    result = await read_settlement(client, [ack()], budget=5)
    assert result["state"] == "settled_exposure"
    assert client.wallet.get_wallet_perp_executions.await_count == 5  # stops at the first definitive answer


async def test_settlement_reads_end_at_the_budget_naming_what_is_unconfirmed(quick_backoff):
    client = settlement_client(lambda **kwargs: page(fill()))
    acks = [ack(), ack(order="11", side="A", first="78")]
    started = time.monotonic()
    result = await read_settlement(client, acks, budget=0.3)
    elapsed = time.monotonic() - started
    assert result["state"] == "unresolved" and "incomplete for ETHRUSDPERP/11" in result["reason"]
    assert [o["order_id"] for o in result["unconfirmed_orders"]] == ["11"]
    assert client.wallet.get_wallet_perp_executions.await_count > 3  # spread over the budget, 0.01 s to 0.04 s
    assert 0.25 < elapsed < 0.5


async def test_failed_settlement_reads_name_no_orders(quick_backoff):
    client = settlement_client(ConnectionError("down"))
    result = await read_settlement(client, [ack()], budget=0.05)
    assert result == {"state": "unresolved", "reason": "Supported settlement reads failed; no submission repeated"}
