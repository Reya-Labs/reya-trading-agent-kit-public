"""Bounded sessions through the real SDK, with only network responses stubbed."""

from types import SimpleNamespace

import asyncio
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from eth_account import Account
from sdk.open_api.models.account import Account as TradingAccount
from sdk.open_api.models.cancel_all_after_response import CancelAllAfterResponse
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.mass_cancel_response import MassCancelResponse
from sdk.open_api.models.modify_order_response import ModifyOrderResponse
from sdk.open_api.models.time_in_force import TimeInForce
from sdk.reya_rest_api.config import TradingConfig
from sdk.reya_rest_api.models.orders import LimitOrderParameters

from agent_kit.scripts import runner, trade
from agent_kit.tests.conftest import make_balance, make_market_def, make_order, make_position, make_price


def policy():
    return runner.Policy(frozenset({"ETHRUSDPERP"}), Decimal("25"), Decimal("40"), Decimal("0.005"), 10, 10)


@pytest.fixture
async def client(monkeypatch):
    key = "0x" + "11" * 32
    config = TradingConfig(
        private_key=key,
        owner_wallet_address=Account.from_key(key).address,
        account_id=8009,
        api_url="https://api.reya.xyz/v2",
        chain_id=1729,
        dex_id_override=2,
    )
    async with runner.RunnerClient(config) as client:
        client.configure(policy())
        client.lease.state["runner"] = {"session_id": "test", "submitted_notional": "0", "reconciled": True}
        monkeypatch.setattr(
            client.reference, "get_perp_market_definitions", AsyncMock(return_value=[make_market_def()])
        )
        monkeypatch.setattr(client.reference, "get_spot_market_definitions", AsyncMock(return_value=[]))
        # Below the session cache, so warm-up and the order path read it as they would live.
        monkeypatch.setattr(client.reference._api, "get_fee_tier_parameters", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            client.markets,
            "get_perp_market_summary",
            AsyncMock(
                return_value=make_price(oracle_price="1800", mark_price="2000", updated_at=int(time.time() * 1000))
            ),
        )
        monkeypatch.setattr(
            client,
            "get_accounts",
            AsyncMock(return_value=[TradingAccount(account_id=8009, name="dedicated", type="SUBPERP")]),
        )
        monkeypatch.setattr(client, "get_positions", AsyncMock(return_value=[]))
        monkeypatch.setattr(client, "get_open_orders", AsyncMock(return_value=[]))
        monkeypatch.setattr(client, "get_account_balances", AsyncMock(return_value=[make_balance(real_balance="1000")]))
        monkeypatch.setattr(
            client.orders,
            "create_order",
            AsyncMock(return_value=CreateOrderResponse(order_id="123", status="OPEN", cum_qty="0")),
        )

        async def countdown(request):
            return CancelAllAfterResponse(
                account_id=8009,
                timeout_ms=request.timeout_ms,
                trigger_at=int(time.time() * 1000) + 30000 if request.timeout_ms else None,
            )

        monkeypatch.setattr(client.orders, "cancel_all_after", AsyncMock(side_effect=countdown))
        monkeypatch.setattr(client.orders, "cancel_all", AsyncMock(return_value=MassCancelResponse(cancelled_count=0)))
        # Explicit terminal, zero-fill history for the default resting order.
        terminal = make_order(order_id="123")
        terminal.status = "CANCELLED"
        terminal.cum_qty = "0"
        terminal.sequence_number = 1
        monkeypatch.setattr(
            client.wallet, "get_wallet_order_history", AsyncMock(return_value=SimpleNamespace(data=[terminal]))
        )
        for method in ("get_wallet_perp_executions", "get_wallet_execution_busts"):
            monkeypatch.setattr(client.wallet, method, AsyncMock(return_value=SimpleNamespace(data=[])))
        yield client


def stream(*commands, eof=True):
    reader = asyncio.StreamReader(limit=8192)
    for index, command in enumerate(commands):
        reader.feed_data((json.dumps({"id": str(index), "command": command}) + "\n").encode())
    if eof:
        reader.feed_eof()
    return reader


LIMIT = [
    "order",
    "limit",
    "ETHRUSDPERP",
    "--side",
    "long",
    "--qty",
    "0.01",
    "--price",
    "1900",
    "--post_only",
    "--client_order_id",
    "123",
]


def outputs(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


async def test_session_reuses_signer_and_enforces_nonrefundable_budget(client, capsys):
    result = await runner.serve(stream(LIMIT, LIMIT, LIMIT), client, policy())
    events = outputs(capsys)
    assert events[0]["event"] == "ready"
    assert client.orders.create_order.await_count == 2
    assert events[3]["result"]["error"].startswith("Session submission budget exhausted")
    assert client.submitted == Decimal("40")  # max(mark=2000,limit=1900) * .01 twice
    assert result["reconciled"] is True
    client.orders.cancel_all.assert_awaited_once()
    assert client.lease.state["runner"]["requests"][0]["result"]["order_id"] == "123"


@pytest.mark.parametrize("state", ["position", "order", "wrong-owner"])
async def test_dirty_or_unowned_account_cannot_start(client, state):
    if state == "position":
        client.get_positions.return_value = [make_position()]
    elif state == "order":
        client.get_open_orders.return_value = [make_order()]
    else:
        client.get_accounts.return_value = []
    with pytest.raises(ValueError):
        await runner.serve(stream(), client, policy())
    client.orders.cancel_all_after.assert_not_awaited()
    client.orders.create_order.assert_not_awaited()


async def test_stale_prices_block_submission(client, capsys):
    client.markets.get_perp_market_summary.return_value = make_price()
    await runner.serve(stream(LIMIT), client, policy())
    events = outputs(capsys)
    assert "stale" in events[1]["result"]["error"]
    client.orders.create_order.assert_not_awaited()


async def test_reducing_position_remains_possible_after_budget_exhaustion(client):
    await client.start()
    client.submitted = Decimal("40")
    client.orders.create_order.return_value = CreateOrderResponse(order_id="123", status="FILLED", cum_qty="1")
    await client.create_limit_order(
        LimitOrderParameters(
            symbol="ETHRUSDPERP",
            is_buy=False,
            qty="1",
            limit_px="1900",
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
        )
    )
    assert client.submitted == Decimal("40")
    payload = client.orders.create_order.call_args.kwargs["create_order_request"]
    assert payload.reduce_only is True and payload.qty == "1"


async def test_uncertain_write_halts_without_replay_and_blocks_restart(client, capsys):
    client.orders.create_order.side_effect = TimeoutError()
    result = await runner.serve(stream(LIMIT, LIMIT), client, policy())
    outputs(capsys)
    assert result["reason"] == "write_requires_reconciliation" and not result["reconciled"]
    client.orders.create_order.assert_awaited_once()
    prior = result["session_id"]
    with pytest.raises(ValueError, match="operator reconciliation"):
        await runner.serve(stream(), client, policy())
    # A manually acknowledged, flat session can start; requests are never replayed.
    client.configure(policy())
    await runner.serve(stream(), client, policy(), ack_previous=prior)
    assert client.orders.create_order.await_count == 1
    assert client.lease.state["runner"]["acknowledged_previous"] == prior


async def test_heartbeat_failure_stops_admission_but_attempts_cleanup(client, monkeypatch, capsys):
    async def fail_heartbeat(client, failed):
        client.heartbeat_failed = True
        failed.set()

    monkeypatch.setattr(runner, "heartbeat", fail_heartbeat)
    result = await runner.serve(stream(eof=False), client, policy())
    outputs(capsys)
    assert result["reason"] == "heartbeat_failed"
    client.orders.create_order.assert_not_awaited()
    client.orders.cancel_all.assert_awaited_once()


async def test_failed_arming_never_admits_trade(client, capsys):
    client.orders.cancel_all_after.side_effect = TimeoutError()
    result = await runner.serve(stream(LIMIT), client, policy())
    outputs(capsys)
    assert not result["reconciled"]
    client.orders.create_order.assert_not_awaited()
    client.orders.cancel_all.assert_awaited_once()


async def test_heartbeat_and_trade_writes_never_overlap(client, monkeypatch):
    await client.start()
    active = 0

    async def send(*args, **kwargs):
        nonlocal active
        active += 1
        assert active == 1
        await asyncio.sleep(0.01)
        active -= 1
        if "create_order_request" in kwargs:
            return CreateOrderResponse(order_id="1", status="OPEN", cum_qty="0")
        return CancelAllAfterResponse(account_id=8009, timeout_ms=30000, trigger_at=int(time.time() * 1000) + 30000)

    monkeypatch.setattr(client.orders, "create_order", send)
    monkeypatch.setattr(client.orders, "cancel_all_after", send)
    await asyncio.gather(
        client.refresh(),
        client.create_limit_order(
            LimitOrderParameters(
                symbol="ETHRUSDPERP", is_buy=True, qty="0.01", limit_px="1900", time_in_force=TimeInForce.GTC
            )
        ),
    )


async def test_shutdown_reports_exposure_and_never_claims_flat(client, capsys):
    client.get_positions.side_effect = [[], [make_position()], [make_position()]]
    result = await runner.serve(stream(["stop"]), client, policy())
    outputs(capsys)
    assert not result["reconciled"] and result["positions_auto_closed"] is False
    assert result["final_state"]["positions"][0]["qty"] == "0.05"
    assert result["protection"] == [
        {"symbol": "ETHRUSDPERP", "position_side": "long", "qty": "0.05", "stop_loss": None, "take_profit": None}
    ]
    assert "NO stop-loss or take-profit" in result["error"]
    client.orders.create_order.assert_not_awaited()


def protective(order_type, order_id, trigger_px):
    from sdk.open_api.models.side import Side

    return make_order(order_id=order_id).model_copy(
        update={"order_type": order_type, "side": Side.A, "qty": None, "trigger_px": trigger_px, "limit_px": "1"}
    )


def exit_with_position(client, monkeypatch, *, orders_at_exit):
    """Flat and empty at start; a long position and these open orders afterwards."""
    from sdk.open_api.models.cancel_order_response import CancelOrderResponse

    reads = {"positions": 0, "orders": 0}
    open_orders = orders_at_exit if isinstance(orders_at_exit, list) else []

    async def positions():
        reads["positions"] += 1
        return [] if reads["positions"] == 1 else [make_position()]

    async def orders():
        reads["orders"] += 1
        if reads["orders"] == 1:
            return []
        if isinstance(orders_at_exit, Exception):
            raise orders_at_exit
        return list(open_orders)

    monkeypatch.setattr(client, "get_positions", positions)
    monkeypatch.setattr(client, "get_open_orders", orders)

    async def cancel(request):
        open_orders[:] = [o for o in open_orders if o.order_id != request.order_id]
        return CancelOrderResponse(status="CANCELLED", order_id=request.order_id)

    monkeypatch.setattr(client.orders, "cancel_order", AsyncMock(side_effect=cancel))


@pytest.mark.parametrize("reason", ["stop", "eof", "idle_timeout", "command_limit", "heartbeat_failed"])
async def test_exit_with_position_keeps_tp_sl_and_cancels_only_other_orders(client, monkeypatch, capsys, reason):
    """A user cancelAll would clear the venue TP/SL; a position must keep its protection on every exit path."""
    from sdk.open_api.models.order_type import OrderType

    stop = protective(OrderType.STOP_LOSS, "501", "1900")
    take = protective(OrderType.TAKE_PROFIT, "502", "2200")
    resting = make_order(order_id="503")
    exit_with_position(client, monkeypatch, orders_at_exit=[stop, resting, take])
    session_policy = policy()
    reader = stream(["status"], eof=reason == "eof")
    if reason == "stop":
        reader = stream(["status"], ["stop"])
    elif reason == "idle_timeout":
        session_policy = runner.Policy(
            frozenset({"ETHRUSDPERP"}), Decimal("25"), Decimal("40"), Decimal("0.005"), 10, 0
        )
        reader = stream(eof=False)
    elif reason == "command_limit":
        session_policy = runner.Policy(
            frozenset({"ETHRUSDPERP"}), Decimal("25"), Decimal("40"), Decimal("0.005"), 1, 10
        )
        reader = stream(["status"], eof=False)
    elif reason == "heartbeat_failed":

        async def fail_heartbeat(client, failed):
            client.heartbeat_failed = True
            failed.set()

        monkeypatch.setattr(runner, "heartbeat", fail_heartbeat)
        reader = stream(eof=False)
    client.configure(session_policy)
    result = await runner.serve(reader, client, session_policy)
    events = outputs(capsys)
    assert result["reason"] == reason and events[-1] == result
    client.orders.cancel_all.assert_not_awaited()
    assert [c.args[0].order_id for c in client.orders.cancel_order.call_args_list] == ["503"]
    assert result["cleanup"] == {
        "mode": "kept_protection",
        "kept_order_ids": ["501", "502"],
        "cancelled_order_ids": ["503"],
        "errors": [],
    }
    assert result["protection"] == [
        {
            "symbol": "ETHRUSDPERP",
            "position_side": "long",
            "qty": "0.05",
            "stop_loss": {"order_id": "501", "trigger_px": "1900", "limit_px": "1"},
            "take_profit": {"order_id": "502", "trigger_px": "2200", "limit_px": "1"},
        }
    ]
    # Reconciliation and countdown semantics are unchanged: exposure is never "reconciled",
    # and the countdown (which keeps TP/SL when it fires) is left armed, never disarmed.
    assert not result["reconciled"] and result["countdown_status"] == "may_be_armed"
    assert all(c.args[0].timeout_ms != 0 for c in client.orders.cancel_all_after.call_args_list)
    assert "stop-loss and take-profit kept" in result["error"]
    assert client.lease.state["runner"]["cleanup"]["kept_order_ids"] == ["501", "502"]


async def test_tp_sl_without_visible_position_are_still_kept(client, monkeypatch, capsys):
    """A lagging position read must not turn into a cancelAll that strips armed protection."""
    from sdk.open_api.models.order_type import OrderType

    stop = protective(OrderType.STOP_LOSS, "501", "1900")
    exit_with_position(client, monkeypatch, orders_at_exit=[stop])
    client.get_positions = AsyncMock(return_value=[])
    result = await runner.serve(stream(["stop"]), client, policy())
    outputs(capsys)
    client.orders.cancel_all.assert_not_awaited()
    client.orders.cancel_order.assert_not_awaited()
    assert result["cleanup"]["kept_order_ids"] == ["501"] and not result["reconciled"]


async def test_unreadable_orders_at_exit_never_cancel_all(client, monkeypatch, capsys):
    exit_with_position(client, monkeypatch, orders_at_exit=TimeoutError())
    result = await runner.serve(stream(["stop"]), client, policy())
    outputs(capsys)
    client.orders.cancel_all.assert_not_awaited()
    client.orders.cancel_order.assert_not_awaited()
    assert result["cleanup"]["mode"] == "kept_protection" and result["cleanup"]["errors"]
    assert "countdown remains armed" in result["error"] and result["countdown_status"] == "may_be_armed"


async def test_unconfirmed_order_cancel_at_exit_is_reported(client, monkeypatch, capsys):
    exit_with_position(client, monkeypatch, orders_at_exit=[make_order(order_id="503")])
    client.orders.cancel_order.side_effect = TimeoutError()
    result = await runner.serve(stream(["stop"]), client, policy())
    outputs(capsys)
    client.orders.cancel_all.assert_not_awaited()
    assert result["cleanup"]["errors"][0]["order_id"] == "503"
    assert "not confirmed cancelled" in result["error"] and "Position left open: ETHRUSDPERP long" in result["error"]


@pytest.mark.parametrize(
    "command",
    [
        LIMIT + ["--force"],
        LIMIT + ["--allow_large"],
        ["positions", "close_all"],
        ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "0.01", "--slippage", "0.01"],
    ],
)
async def test_runner_rejects_overrides_unscoped_cleanup_and_slippage(client, capsys, command):
    await runner.serve(stream(command), client, policy())
    events = outputs(capsys)
    assert "error" in events[1]["result"]
    client.orders.create_order.assert_not_awaited()


async def test_duplicate_request_id_is_not_executed_twice(client, capsys):
    reader = asyncio.StreamReader()
    line = json.dumps({"id": "duplicate", "command": LIMIT}) + "\n"
    reader.feed_data((line + line).encode())
    reader.feed_eof()
    result = await runner.serve(reader, client, policy())
    outputs(capsys)
    client.orders.create_order.assert_awaited_once()
    assert not result["reconciled"]


async def test_modify_restates_immutable_fields_and_client_id(client, monkeypatch, capsys):
    order = make_order(order_id="123").model_copy(
        update={"client_order_id": "77", "post_only": True, "cum_qty": "0.001"}
    )
    client.get_open_orders.return_value = [order]
    modify = AsyncMock(
        return_value=ModifyOrderResponse(
            order_id="123", client_order_id="77", status="OPEN", cum_qty="0.001", exec_qty="0"
        )
    )
    monkeypatch.setattr(client.orders, "modify_order", modify)
    result = await runner.execute_request(
        client, {"command": ["order", "modify", "--client_order_id", "77", "--price", "1900", "--qty", "0.01"]}
    )
    assert result["order_id"] == "123" and result["client_order_id"] == "77"
    request = modify.call_args.args[0].to_dict()
    assert request["orderId"] == "123" and request["clientOrderId"] == "77"
    assert request["isBuy"] is True and request["timeInForce"] == "GTC" and request["postOnly"] is True
    assert request["qty"] == "0.01" and len(request["signature"]) == 132


@pytest.mark.parametrize(
    "change",
    [
        {"max_order_notional": "NaN"},
        {"max_slippage": "0.5"},
        {"max_commands": 0},
        {"unknown": "value"},
        {"symbols": []},
    ],
)
def test_bad_policy_rejected(tmp_path, change):
    data = dict(
        symbols=["ETHRUSDPERP"],
        max_order_notional="25",
        max_session_notional="40",
        max_slippage="0.005",
        max_commands=10,
        idle_timeout_s=10,
    )
    data.update(change)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        runner.Policy.load(path)


async def test_intent_and_nonce_are_durable_before_request_leaves_process(client, monkeypatch, capsys):
    async def send(**kwargs):
        path = client.lease.directory / (client.lease.keys[0] + ".json")
        state = json.loads(path.read_text())
        assert state["runner"]["requests"][0]["state"] == "intent"
        assert state["runner"]["requests"][0]["command"] == LIMIT
        assert state["last_nonce"] == int(kwargs["create_order_request"].nonce)
        raise TimeoutError()

    monkeypatch.setattr(client.orders, "create_order", AsyncMock(side_effect=send))
    result = await runner.serve(stream(LIMIT), client, policy())
    outputs(capsys)
    assert result["reason"] == "write_requires_reconciliation"
    client.orders.create_order.assert_awaited_once()


async def test_disarm_failure_leaves_reconciliation_required(client, monkeypatch, capsys):
    async def countdown(request):
        if request.timeout_ms == 0:
            raise TimeoutError()
        return CancelAllAfterResponse(account_id=8009, timeout_ms=30000, trigger_at=int(time.time() * 1000) + 30000)

    monkeypatch.setattr(client.orders, "cancel_all_after", AsyncMock(side_effect=countdown))
    result = await runner.serve(stream(["stop"]), client, policy())
    outputs(capsys)
    assert not result["reconciled"] and result["countdown_status"] == "may_be_armed"
    assert "disarm unconfirmed" in result["error"]


async def test_abbreviated_guardrail_override_cannot_bypass_runner(client, capsys):
    await runner.serve(
        stream(["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "0.01", "--for"]), client, policy()
    )
    assert "error" in outputs(capsys)[1]["result"]
    client.orders.create_order.assert_not_awaited()


async def test_cancelled_acknowledgement_retains_result_and_the_session_continues(client, capsys):
    # A terminal acknowledgement says exactly what happened: nothing is left to reconcile.
    client.orders.create_order.return_value = CreateOrderResponse(
        order_id="123", status="CANCELLED", cum_qty="0", cancel_reason_message="Maker order would cross"
    )
    result = await runner.serve(stream(LIMIT, LIMIT), client, policy())
    events = outputs(capsys)
    assert events[1]["result"]["order_id"] == "123"
    assert "error" in events[1]["result"] and events[1]["result"]["session_continues"] is True
    assert result["reason"] == "eof"
    assert client.orders.create_order.await_count == 2  # the second request, never a resend of the first
    client.orders.cancel_all.assert_awaited_once()


async def test_budget_arithmetic_never_rounds_an_oversized_order_under_cap(client):
    await client.start()
    with pytest.raises(ValueError, match="max_order_notional"):
        await client.create_limit_order(
            LimitOrderParameters(
                symbol="ETHRUSDPERP",
                is_buy=True,
                qty="0.0100000000000000000000000000001",
                limit_px="2500",
                time_in_force=TimeInForce.GTC,
            )
        )
    client.orders.create_order.assert_not_awaited()


@pytest.mark.parametrize("command", [LIMIT, ["status"]])
async def test_slow_command_does_not_spend_next_input_idle_allowance(client, monkeypatch, capsys, command):
    from types import SimpleNamespace

    clock = [100.0]
    monkeypatch.setattr(runner, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    original_wait = asyncio.wait
    timeouts = []

    async def wait(tasks, *, timeout, return_when):
        timeouts.append(timeout)
        return await original_wait(tasks, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(asyncio, "wait", wait)
    if command == ["status"]:
        original_read = client.get_positions

        async def slow_read():
            if original_read.await_count == 1:
                clock[0] += 12
            return await original_read()

        monkeypatch.setattr(client, "get_positions", slow_read)
    else:
        original_execute = runner.execute_request

        async def slow_execute(*args):
            result = await original_execute(*args)
            clock[0] += 12
            return result

        monkeypatch.setattr(runner, "execute_request", slow_execute)
    outcome = await runner.serve(stream(command, ["stop"]), client, policy())
    outputs(capsys)
    assert timeouts == [10, 10]
    assert outcome["reason"] == "stop"


@pytest.mark.parametrize("where", ["input", "price_read", "refresh"])
async def test_expired_countdown_halts_even_if_heartbeat_task_has_not_run(client, monkeypatch, capsys, where):
    from types import SimpleNamespace

    clock = [100.0]
    monkeypatch.setattr(runner, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    reader = stream(LIMIT)
    if where == "input":
        original = reader.readline

        async def stalled_input():
            clock[0] += 31
            return await original()

        monkeypatch.setattr(reader, "readline", stalled_input)
    elif where == "price_read":

        async def stalled_price(**kwargs):
            clock[0] += 31
            return make_price(updated_at=int(time.time() * 1000))

        monkeypatch.setattr(client.markets, "get_perp_market_summary", stalled_price)
    else:
        await client.start()
        await client.refresh()
        clock[0] += 31
        with pytest.raises(ValueError, match="heartbeat"):
            await client.refresh()
        assert client.orders.cancel_all_after.await_count == 1
        return
    outcome = await runner.serve(reader, client, policy())
    outputs(capsys)
    assert outcome["reason"] == "heartbeat_failed" and not outcome["reconciled"]
    client.orders.create_order.assert_not_awaited()
    client.orders.cancel_all.assert_awaited_once()
    assert client.orders.cancel_all_after.await_count == 1  # never rearm or disarm after expiry


async def test_status_includes_protective_order_details(client):
    from sdk.open_api.models.order_type import OrderType
    from sdk.open_api.models.side import Side

    client.get_open_orders.return_value = [
        make_order().model_copy(
            update={
                "order_type": OrderType.STOP_LOSS,
                "side": Side.A,
                "qty": None,
                "client_order_id": "321",
                "trigger_px": "1900",
                "post_only": False,
            }
        )
    ]
    state = await runner.flat_account(client)
    order = state["orders"][0]
    assert order["order_id"] in state["open_order_ids"]
    assert order["client_order_id"] == "321" and order["trigger_px"] == "1900"
    assert order["position_side"] == "long" and order["close_side"] == "sell"
    assert order["quantity_mode"] == "whole_position" and "side" not in order


@pytest.mark.parametrize("final_position", ["matching", "missing", "opposite", "other_account"])
async def test_runner_waits_for_position_publication_before_protection(client, monkeypatch, final_position):
    from sdk.open_api.models.side import Side

    rows = (
        []
        if final_position == "missing"
        else [
            make_position().model_copy(
                update={
                    "side": Side.A if final_position == "opposite" else Side.B,
                    "account_id": 999 if final_position == "other_account" else 8009,
                }
            )
        ]
    )
    calls = []

    async def positions():
        calls.append(True)
        return [] if len(calls) == 1 else rows

    monkeypatch.setattr(client, "get_positions", positions)
    command = [
        "order",
        "tp",
        "ETHRUSDPERP",
        "--side",
        "long",
        "--trigger_px",
        "2100",
        "--limit_px",
        "2099",
        "--time_in_force",
        "IOC",
    ]
    if final_position == "matching":
        outcome = await runner.execute_request(client, {"command": command})
        assert outcome["position_side"] == "long" and outcome["order_id"] == "123"
        assert len(calls) == 2
        client.orders.create_order.assert_awaited_once()
    else:
        with pytest.raises(ValueError, match="within 15 seconds; no protective order submitted"):
            await runner.execute_request(client, {"command": command})
        # A flip shows the old side first, so a wrong-side read keeps reading to the bound too:
        # mainnet's REST read cap, one per 0.5 s across its 15 s ceiling plus the first.
        assert len(calls) == 31
        client.orders.create_order.assert_not_awaited()


async def test_runner_protected_entry_arms_stop_then_target_within_a_longer_bound(client, monkeypatch):
    from sdk.open_api.models.order_type import OrderType
    from sdk.open_api.models.side import Side

    responses = iter(
        [
            CreateOrderResponse(order_id="entry", status="FILLED", cum_qty="0.01", avg_fill_px="2000"),
            CreateOrderResponse(order_id="sl", status="OPEN"),
            CreateOrderResponse(order_id="tp", status="OPEN"),
        ]
    )
    create = AsyncMock(side_effect=lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(client.orders, "create_order", create)
    reads_after_entry = []

    async def positions():
        if not create.await_count:
            return []
        reads_after_entry.append(True)
        return [] if len(reads_after_entry) < 3 else [make_position(qty="0.01", last_trade_sequence_number=5)]

    async def orders():
        armed = [("sl", OrderType.STOP_LOSS), ("tp", OrderType.TAKE_PROFIT)][: max(0, create.await_count - 1)]
        return [make_order(order_id=i, order_type=t, side=Side.A) for i, t in armed]

    monkeypatch.setattr(client, "get_positions", positions)
    monkeypatch.setattr(client, "get_open_orders", orders)
    timeouts = []
    original_wait = asyncio.wait_for

    async def recording_wait(awaitable, timeout):
        timeouts.append(timeout)
        return await original_wait(awaitable, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", recording_wait)
    command = ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "0.01", "--slippage", "0.005"]
    outcome = await runner.execute_request(client, {"command": command + ["--sl", "1%", "--tp", "1%"]})

    assert outcome["protection"]["state"] == "protected" and outcome["protection"]["verified"] is True
    assert [o["order_id"] for o in client.lease.state["runner"]["acknowledged_orders"]] == ["entry", "sl", "tp"]
    # 15 s for the command, 15 s for mainnet's position ceiling, two 5 s trigger writes, 2 s read-back.
    assert len(reads_after_entry) == 3 and 42 in timeouts


@pytest.mark.parametrize(
    "api_url, sl, timeout",
    [
        ("https://api.reya.xyz/v2", ["--sl", "1%"], 42),  # 15 + 15 s ceiling + 2 x 5 s trigger writes + 2 s read-back
        ("https://api-devnet.reya-cronos.network/v2", ["--sl", "1%"], 57),  # the same with a 30 s ceiling
        ("https://api-devnet.reya-cronos.network/v2", [], 15),
    ],
)
def test_a_protected_entry_may_take_its_ceiling_two_trigger_writes_and_the_read_back(api_url, sl, timeout):
    command = ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "0.01", "--slippage", "0.005"]
    client = SimpleNamespace(config=SimpleNamespace(api_url=api_url))
    assert runner.command_timeout_s(client, trade.build_parser().parse_args(command + sl)) == timeout


TRIGGER_ARGS = ["ETHRUSDPERP", "--side", "long", "--trigger_px", "1900", "--limit_px", "1899", "--time_in_force", "IOC"]


@pytest.mark.parametrize("kind", ["sl", "tp"])
@pytest.mark.parametrize(
    "api_url, timeout",
    [
        ("https://api.reya.xyz/v2", 35),  # 15 + 15 s ceiling + one 5 s trigger write
        ("https://api-devnet.reya-cronos.network/v2", 50),  # the same with a 30 s ceiling
        ("https://api-cronos.reya.xyz/v2", 50),
    ],
)
def test_order_sl_and_tp_may_take_their_ceiling_and_one_trigger_write(api_url, kind, timeout):
    client = SimpleNamespace(config=SimpleNamespace(api_url=api_url))
    args = trade.build_parser().parse_args(["order", kind, *TRIGGER_ARGS, "--fees_wait_s", "5"])
    assert runner.command_timeout_s(client, args) == timeout
    other = trade.build_parser().parse_args(["order", "cancel", "--symbol", "ETHRUSDPERP", "--order_id", "1"])
    assert runner.command_timeout_s(client, other) == 15


async def test_protection_read_timeout_is_bounded_and_never_submits(client, monkeypatch):
    async def stuck():
        await asyncio.sleep(60)

    monkeypatch.setattr(trade, "indexing_ceiling", lambda api_url: 0.02)  # the bound on the position wait
    monkeypatch.setattr(client, "get_positions", stuck)
    with pytest.raises(ValueError, match="Position read timed out"):
        await runner.execute_request(client, {"command": ["order", "sl", *TRIGGER_ARGS]})
    client.orders.create_order.assert_not_awaited()


@pytest.mark.parametrize("initial", [True, False])
async def test_late_countdown_reply_cannot_reset_deadline_after_stall(client, monkeypatch, initial):
    from types import SimpleNamespace

    clock = [100.0]
    monkeypatch.setattr(runner, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    await client.start()
    if not initial:
        await client.refresh()
    original = client.orders.cancel_all_after

    async def stalled_reply(request):
        response = await original(request)
        clock[0] += 31
        return response

    monkeypatch.setattr(client.orders, "cancel_all_after", stalled_reply)
    with pytest.raises(ValueError, match="heartbeat"):
        await client.refresh()
    assert client.heartbeat_failed


async def test_stall_after_submission_preserves_order_reference_and_halts(client, monkeypatch, capsys):
    from types import SimpleNamespace

    clock = [100.0]
    monkeypatch.setattr(runner, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))

    async def stalled_reply(**kwargs):
        clock[0] += 31
        return CreateOrderResponse(order_id="123", status="OPEN", cum_qty="0")

    client.orders.create_order.side_effect = stalled_reply
    outcome = await runner.serve(stream(LIMIT, LIMIT), client, policy())
    events = outputs(capsys)
    assert events[1]["result"]["order_id"] == "123"
    assert outcome["reason"] == "heartbeat_failed" and not outcome["reconciled"]
    client.orders.create_order.assert_awaited_once()


async def test_sdk_error_in_runner_is_structured_and_still_requires_reconciliation(client, capsys):
    from sdk.open_api.exceptions import BadRequestException

    error = BadRequestException(
        status=400, body=json.dumps({"error": "CREATE_ORDER_OTHER_ERROR", "message": "Unknown error"})
    )
    error.headers = {"Authorization": "private-token"}
    client.orders.create_order.side_effect = error
    outcome = await runner.serve(stream(LIMIT, LIMIT), client, policy())
    events = outputs(capsys)
    assert events[1]["result"]["error_code"] == "CREATE_ORDER_OTHER_ERROR"
    assert "session_continues" not in events[1]["result"]
    assert "private-token" not in json.dumps(events)
    assert outcome["reason"] == "write_requires_reconciliation" and not outcome["reconciled"]
    client.orders.create_order.assert_awaited_once()


@pytest.mark.parametrize("source", ["null", "file"])
async def test_unsupported_stdin_rejected_before_signer_or_countdown(tmp_path, monkeypatch, source):
    from dataclasses import asdict

    values = asdict(policy())
    values["symbols"] = list(values["symbols"])
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(values, default=str))
    args = runner.build_parser().parse_args(["--policy", str(path), "--execute", "--ack-account-wide-cancel"])
    filename = "/dev/null"
    if source == "file":
        data = tmp_path / "commands.jsonl"
        data.write_text('{"id":"stop","command":["stop"]}\n')
        filename = str(data)

    def unexpected_config():
        pytest.fail("Must reject unsupported stdin before loading signer")

    monkeypatch.setattr(runner, "get_trading_config", unexpected_config)
    with open(filename) as stdin:
        monkeypatch.setattr("sys.stdin", stdin)
        with pytest.raises(ValueError, match="stdin must be a pipe or terminal"):
            await runner.run(args)


async def test_command_deadline_during_fee_poll_retains_fill_and_halts(client, monkeypatch, capsys):
    original_wait = asyncio.wait_for

    async def bounded_wait(awaitable, timeout):
        return await original_wait(awaitable, timeout=0.03 if timeout == 15 else timeout)

    async def stuck(**kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(asyncio, "wait_for", bounded_wait)
    monkeypatch.setattr(client.wallet, "get_wallet_perp_executions", stuck)
    client.orders.create_order.return_value = CreateOrderResponse(
        order_id="123", status="FILLED", cum_qty="0.01", first_fill_id="77", fill_count=1, client_order_id="123"
    )
    outcome = await runner.serve(stream(LIMIT + ["--fees_wait_s", "5"], LIMIT), client, policy())
    response = outputs(capsys)[1]["result"]
    assert response["order_id"] == "123" and response["first_fill_id"] == "77"
    assert response["status"] == "FILLED" and response["filled_qty"] == "0.01"
    assert response["fees_status"] == "pending" and response["fees_rusd"] is None
    assert "timed out after an acknowledgement" in response["error"]
    assert outcome["reason"] == "write_requires_reconciliation" and not outcome["reconciled"]
    client.orders.create_order.assert_awaited_once()


def settlement_page(*rows):
    return SimpleNamespace(data=[SimpleNamespace(to_dict=lambda row=row: row) for row in rows])


async def test_filled_ack_with_lagged_empty_reads_stays_unresolved_and_blocks_restart(client, capsys):
    client.orders.create_order.return_value = CreateOrderResponse(
        order_id="123", status="FILLED", cum_qty="0.01", first_fill_id="77", fill_count=1
    )
    command = ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", ".01", "--slippage", ".001"]
    result = await runner.serve(stream(command, ["stop"]), client, policy())
    outputs(capsys)
    journal = client.lease.state["runner"]
    assert journal["acknowledged_orders"][0]["filled_qty"] == "0.01"
    assert not result["reconciled"] and result["settlement"]["state"] == "unresolved"
    # The final reads look flat (session 1 on devnet1): the event names the fill settlement has not seen.
    assert result["final_state"]["positions"] == [] and result["protection"] == []
    unconfirmed = [{"symbol": "ETHRUSDPERP", "order_id": "123", "side": "B", "filled_qty": "0.01"}]
    assert result["unconfirmed_fills"] == unconfirmed and journal["unconfirmed_fills"] == unconfirmed
    assert "not yet confirmed by settlement: ETHRUSDPERP buy 0.01 (order 123)" in result["error"]
    assert [c.args[0].timeout_ms for c in client.orders.cancel_all_after.call_args_list] == [30000]
    with pytest.raises(ValueError, match="operator reconciliation"):
        await runner.serve(stream(), client, policy())
    client.orders.create_order.assert_awaited_once()


async def test_stopped_event_names_only_the_fills_settlement_has_not_seen(client, monkeypatch, capsys):
    from agent_kit.tests.test_settlement import fill

    monkeypatch.setattr(runner, "FEE_SETTLE_WAIT_S", 0)
    client.orders.create_order.side_effect = [
        CreateOrderResponse(order_id="10", status="FILLED", cum_qty="0.01", first_fill_id="77", fill_count=1),
        CreateOrderResponse(order_id="11", status="FILLED", cum_qty="0.01", first_fill_id="78", fill_count=1),
    ]
    client.wallet.get_wallet_perp_executions.return_value = settlement_page(fill())  # the close is not indexed
    entry = ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", ".01", "--slippage", ".001"]
    close = ["order", "market", "ETHRUSDPERP", "--side", "short", "--qty", ".01", "--slippage", ".001", "--reduce_only"]
    result = await runner.serve(stream(entry, close, ["stop"]), client, policy())
    outputs(capsys)
    assert result["settlement"]["state"] == "unresolved"
    assert result["unconfirmed_fills"] == [
        {"symbol": "ETHRUSDPERP", "order_id": "11", "side": "A", "filled_qty": "0.01"}
    ]
    assert "ETHRUSDPERP sell 0.01 (order 11)" in result["error"] and "order 10" not in result["error"]


def test_unconfirmed_fills_without_per_order_results_list_every_acknowledged_fill():
    acks = [
        {"symbol": "ETHRUSDPERP", "order_id": "10", "side": "B", "status": "OPEN", "filled_qty": "0"},
        {"symbol": "ETHRUSDPERP", "order_id": "10", "side": "B", "status": "OPEN", "filled_qty": "0.02"},  # modify
        {"symbol": "ETHRUSDPERP", "order_id": "11", "side": "A", "status": "OPEN", "filled_qty": "0"},
        {"symbol": "ETHRUSDPERP", "order_id": "12", "side": "A", "status": "FILLED", "filled_qty": "0.01"},
    ]
    failed = {"state": "unresolved", "reason": "Supported settlement reads failed; no submission repeated"}
    assert [f["order_id"] for f in runner.unconfirmed_fills(acks, failed)] == ["10", "12"]
    assert runner.unconfirmed_fills(acks, {"state": "settled_exposure"}) == []


async def test_filled_ack_later_bust_reconciles_flat_without_repeating_submission(client, capsys):
    from agent_kit.tests.test_settlement import fill

    client.orders.create_order.return_value = CreateOrderResponse(
        order_id="10", status="FILLED", cum_qty="0.01", first_fill_id="77", fill_count=1
    )
    client.wallet.get_wallet_execution_busts.side_effect = [settlement_page(), settlement_page(fill())]
    command = ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", ".01", "--slippage", ".001"]
    result = await runner.serve(stream(command, ["stop"]), client, policy())
    outputs(capsys)
    assert result["reconciled"] and result["settlement"]["orders"][0]["busted_qty"] == "0.01"
    client.orders.create_order.assert_awaited_once()
    assert client.wallet.get_wallet_execution_busts.await_count == 2


async def test_delayed_close_settlement_keeps_polling_until_known_net_exposure_is_zero(client, monkeypatch, capsys):
    from agent_kit.tests.test_settlement import fill

    monkeypatch.setattr(runner, "FEE_SETTLE_WAIT_S", 0)  # one fee read per fill, as the pages below assume

    client.orders.create_order.side_effect = [
        CreateOrderResponse(order_id="10", status="FILLED", cum_qty="0.01", first_fill_id="77", fill_count=1),
        CreateOrderResponse(order_id="11", status="FILLED", cum_qty="0.01", first_fill_id="78", fill_count=1),
    ]
    # First two are optional fee reads, then delayed close evidence at shutdown.
    client.wallet.get_wallet_perp_executions.side_effect = [
        settlement_page(),
        settlement_page(),
        settlement_page(fill()),
        settlement_page(fill(), fill(order="11", side="A", fill_id="78", seq=8)),
    ]
    entry = ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", ".01", "--slippage", ".001"]
    close = ["order", "market", "ETHRUSDPERP", "--side", "short", "--qty", ".01", "--slippage", ".001", "--reduce_only"]
    result = await runner.serve(stream(entry, close, ["stop"]), client, policy())
    outputs(capsys)
    assert result["reconciled"] and result["settlement"]["net_settled_qty"] == {"ETHRUSDPERP": "0.00"}
    assert client.orders.create_order.await_count == 2
    assert client.wallet.get_wallet_perp_executions.await_count == 4


async def test_settled_entry_still_blocks_false_flat_even_if_account_projection_lags(client, capsys):
    from agent_kit.tests.test_settlement import fill

    client.orders.create_order.return_value = CreateOrderResponse(
        order_id="10", status="FILLED", cum_qty="0.01", first_fill_id="77", fill_count=1
    )
    client.wallet.get_wallet_perp_executions.return_value = settlement_page(fill())
    command = ["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", ".01", "--slippage", ".001"]
    result = await runner.serve(stream(command, ["stop"]), client, policy())
    outputs(capsys)
    assert result["settlement"]["state"] == "settled_exposure"
    assert not result["reconciled"]


@pytest.mark.parametrize("action", ["cancel", "modify"])
@pytest.mark.parametrize("by_client", [False, True])
async def test_market_qualified_identifier_reaches_real_sdk_signed_payload(client, monkeypatch, action, by_client):
    from agent_kit.tests.conftest import make_market_def

    orders = [
        make_order(order_id="123", symbol=s).model_copy(
            update={"client_order_id": "77", "post_only": True, "cum_qty": "0"}
        )
        for s in ("BTCRUSDPERP", "ETHRUSDPERP")
    ]
    client.get_open_orders.return_value = orders
    monkeypatch.setattr(
        client.reference,
        "get_perp_market_definitions",
        AsyncMock(return_value=[make_market_def(), make_market_def(symbol="BTCRUSDPERP", market_id=2)]),
    )
    method = AsyncMock(
        return_value=ModifyOrderResponse(
            order_id="123",
            client_order_id="77",
            status="CANCELLED" if action == "cancel" else "OPEN",
            cum_qty="0",
            exec_qty="0",
        )
    )
    monkeypatch.setattr(client.orders, action + "_order", method)
    command = [
        "order",
        action,
        "--client_order_id" if by_client else "--order_id",
        "77" if by_client else "123",
        "--symbol",
        "ETHRUSDPERP",
    ]
    if action == "modify":
        command += ["--price", "1900", "--qty", "0.01"]
    result = await runner.execute_request(client, {"command": command})
    assert result["symbol"] == "ETHRUSDPERP" and result["account_id"] == 8009
    call = method.call_args
    payload = (call.args[0] if call.args else next(iter(call.kwargs.values()))).to_dict()
    assert payload["symbol"] == "ETHRUSDPERP" and payload["accountId"] == 8009
    assert len(payload["signature"]) == 132


async def test_kit_guards_apply_inside_the_runner_without_overrides(client, capsys):
    through = ["order", "limit", "ETHRUSDPERP", "--side", "long", "--qty", "0.01", "--price", "2200"]
    client.get_account_balances.return_value = [make_balance(real_balance="1")]
    result = await runner.serve(stream(through, LIMIT, ["stop"]), client, policy())
    events = outputs(capsys)
    assert events[1]["result"]["refused_by"][0]["check"] == "limit_price_band"
    # 19 USDC fits the runner policy but not a 1 USDC account: the kit cap still applies.
    assert events[2]["result"]["refused_by"][0]["check"] == "max_order_notional"
    assert result["reason"] == "stop" and result["reconciled"]  # refusals sent nothing, so nothing to reconcile
    client.orders.create_order.assert_not_awaited()
