"""Acknowledgement/settlement separation and bounded evidence checks."""

import copy

import pytest

from agent_kit.scripts._settlement import reconcile_acknowledgements


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
