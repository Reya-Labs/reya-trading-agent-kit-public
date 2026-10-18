"""The example must not interpret an empty read after entry as proof of safety."""

from unittest.mock import Mock

import pytest

from examples.agent_kit import bracketed_long as example


def test_unknown_entry_never_attempts_one_shot_flatten_or_cancel(monkeypatch):
    run = Mock()
    monkeypatch.setattr(example, "run_cli", run)
    with pytest.raises(SystemExit):
        example.flatten_for_safety("entry timed out")
    run.assert_not_called()


def test_unsettled_entry_stops_before_brackets_or_cleanup(monkeypatch):
    run = Mock()
    monkeypatch.setattr(example, "run_cli", run)
    monkeypatch.setattr(example, "settlement_evidence", lambda *_: {"state": "unresolved"})
    with pytest.raises(SystemExit):
        example.flatten_for_safety("entry lag", {"status": "FILLED", "filled_qty": ".01"})
    run.assert_not_called()


def test_settled_entry_with_empty_projection_is_not_considered_clean(monkeypatch):
    run = Mock(return_value=[])
    monkeypatch.setattr(example, "run_cli", run)
    monkeypatch.setattr(
        example,
        "settlement_evidence",
        lambda *_: {"state": "settled_exposure", "net_settled_qty": {example.SYMBOL: ".01"}},
    )
    with pytest.raises(SystemExit):
        example.flatten_for_safety("bracket failure", {"status": "FILLED", "filled_qty": ".01"})
    assert run.call_count == 1
    assert run.call_args.args[1] == "positions" and run.call_args.args[0] == "agent_kit.scripts.query"
