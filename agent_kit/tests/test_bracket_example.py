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
    monkeypatch.setattr(example.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        example,
        "settlement_evidence",
        lambda *_: {"state": "settled_exposure", "net_settled_qty": {example.SYMBOL: ".01"}},
    )
    with pytest.raises(SystemExit):
        example.flatten_for_safety("bracket failure", {"status": "FILLED", "filled_qty": ".01"})
    # Bounded re-reads, never a single read, and only reads: no close or cancel is sent.
    assert run.call_count == example.POSITION_READS
    assert all(call.args[:2] == ("agent_kit.scripts.query", "positions") for call in run.call_args_list)


def test_lagging_position_read_still_reaches_the_flatten(monkeypatch):
    reads = iter([[], [], [{"symbol": example.SYMBOL}], {"close_orders": []}, []])
    run = Mock(side_effect=lambda *args: next(reads))
    monkeypatch.setattr(example, "run_cli", run)
    monkeypatch.setattr(example.time, "sleep", lambda _: None)
    evidence = iter(
        [
            {"state": "settled_exposure", "net_settled_qty": {example.SYMBOL: ".01"}},
            {"state": "settled_flat", "net_settled_qty": {}},
        ]
    )
    monkeypatch.setattr(example, "settlement_evidence", lambda *_: next(evidence))
    with pytest.raises(SystemExit):
        example.flatten_for_safety("no stop-loss", {"status": "CANCELLED", "filled_qty": ".01"})
    sent = [call.args[1:3] for call in run.call_args_list if call.args[0] == "agent_kit.scripts.trade"]
    assert sent == [("positions", "close_all"), ("order", "cancel_all")]


def _main_with(monkeypatch, entry_result, preview=None):
    preview = {"would_send": True, "refused_by": []} if preview is None else preview
    reads = Mock(return_value=[])
    sent = Mock(side_effect=[(0, preview), (1, entry_result)])
    flatten = Mock(side_effect=SystemExit(1))
    monkeypatch.setattr(example, "run_cli", reads)
    monkeypatch.setattr(example, "run_cli_result", sent)
    monkeypatch.setattr(example, "flatten_for_safety", flatten)
    return sent, flatten


def test_example_sends_one_protected_entry_and_does_not_flatten(monkeypatch):
    sent, flatten = _main_with(
        monkeypatch, {"protection": {"state": "protected", "stop_loss": {"armed": True}}, "filled_qty": ".01"}
    )
    example.main()
    preview_call, entry_call = sent.call_args_list
    assert "--preview" in preview_call.args and "--preview" not in entry_call.args
    assert entry_call.args[-4:] == ("--sl", example.SL_LEVEL, "--tp", example.TP_LEVEL)
    flatten.assert_not_called()


@pytest.mark.parametrize("state", ["unprotected", "unknown", "pending"])
def test_example_flattens_whenever_no_stop_loss_is_armed(monkeypatch, state):
    result = {"protection": {"state": state, "message": "Entry filled; UNPROTECTED", "stop_loss": None}}
    sent, flatten = _main_with(monkeypatch, result)
    with pytest.raises(SystemExit):
        example.main()
    assert sent.call_count == 2  # preview + one entry, never a second entry
    assert flatten.call_args.args == ("Entry filled; UNPROTECTED", result)


def test_example_stops_before_sending_on_a_refused_preview(monkeypatch):
    sent, flatten = _main_with(monkeypatch, None, preview={"would_send": False, "refused_by": [{"check": "x"}]})
    with pytest.raises(SystemExit):
        example.main()
    assert sent.call_count == 1
    flatten.assert_not_called()
