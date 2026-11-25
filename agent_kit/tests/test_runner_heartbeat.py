"""The runner's cancellation countdown: renewal retries, the deadline that bounds them, and what halts at once.

Issue #73: one failed renewal used to end the session although two more fit in the 30 s countdown.
"""

from types import SimpleNamespace

import asyncio
import json
import time

import aiohttp
import pytest
from sdk.open_api.exceptions import ApiException, BadRequestException, ServiceException
from sdk.open_api.models.cancel_all_after_response import CancelAllAfterResponse
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.time_in_force import TimeInForce
from sdk.reya_rest_api.models.orders import LimitOrderParameters

from agent_kit.scripts import runner
from agent_kit.tests.test_runner import LIMIT, client, outputs, policy, stream  # noqa: F401


def rejected(code, **body):
    return BadRequestException(status=400, body=json.dumps({"error": code, "message": code.lower(), **body}))


def confirmed(request):
    trigger_at = int(time.time() * 1000) + request.timeout_ms if request.timeout_ms else None  # none on disarm
    return CancelAllAfterResponse(account_id=8009, timeout_ms=request.timeout_ms, trigger_at=trigger_at)


def countdown_replies(client, *replies):  # noqa: F811
    """The venue's answers to successive cancelAllAfter calls, after the arming one; each request is kept."""
    requests = []
    pending = list(replies)

    async def reply(request):
        requests.append(request)
        if len(requests) == 1 or not pending:
            return confirmed(request)
        answer = pending.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer(request) if callable(answer) else answer

    client.orders.cancel_all_after.side_effect = reply
    return requests


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock the test moves; the runner's retry waits advance it instead of sleeping."""
    now = [100.0]
    waits = []
    monkeypatch.setattr(runner, "time", SimpleNamespace(time=time.time, monotonic=lambda: now[0]))

    async def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(runner.asyncio, "sleep", sleep)
    return SimpleNamespace(now=now, waits=waits)


async def arm(client):  # noqa: F811
    await client.start()
    await client.refresh()  # confirmed at 100: deadline 130


@pytest.mark.parametrize(
    "failure, code",
    [
        (rejected("SERVICE_UNAVAILABLE_ERROR"), "SERVICE_UNAVAILABLE_ERROR"),
        (rejected("ORDER_OUTCOME_UNKNOWN_ERROR"), "ORDER_OUTCOME_UNKNOWN_ERROR"),
        (ServiceException(status=503, body="upstream unavailable"), "API_ERROR"),
        (TimeoutError(), "TRANSPORT"),
        (aiohttp.ServerDisconnectedError(), "TRANSPORT"),
    ],
)
async def test_a_retryable_renewal_failure_is_retried_with_a_fresh_request(client, clock, failure, code):  # noqa: F811
    requests = countdown_replies(client, failure)
    await arm(client)
    clock.now[0] = 110.0
    await runner.renew(client)
    assert len(requests) == 3 and len({r.nonce for r in requests}) == 3  # each attempt is signed anew
    assert clock.waits == [runner.HEARTBEAT_RETRY_S]
    assert client.heartbeat_deadline == 111.0 + 30  # measured from before the confirmed retry
    record = client.heartbeat_record
    assert record["retries"] == 1 and record["last_retry"]["error_code"] == code and record["halted_by"] is None
    assert client.lease.state["runner"]["heartbeat"]["retries"] == 1  # in the journal


@pytest.mark.parametrize("retry_after_ms, wait", [(3500, 3.5), (300, runner.HEARTBEAT_RETRY_S), (None, 1.0)])
async def test_a_rate_limited_renewal_waits_for_the_venue_before_retrying(
    client, clock, retry_after_ms, wait  # noqa: F811
):
    body = {} if retry_after_ms is None else {"retryAfterMs": retry_after_ms}
    requests = countdown_replies(client, rejected("RATE_LIMITED_ERROR", **body))
    await arm(client)
    clock.now[0] = 110.0
    await runner.renew(client)
    assert clock.waits == [wait] and len(requests) == 3
    assert client.heartbeat_record["last_retry"]["error_code"] == "RATE_LIMITED_ERROR"


@pytest.mark.parametrize("failed_at, retried", [(122.0, True), (123.0, False)])
async def test_a_retry_is_sent_only_while_it_can_still_be_confirmed_before_the_deadline(
    client, clock, failed_at, retried  # noqa: F811
):
    # Deadline 130: a retry after 1 s, bounded by the 5 s write timeout and a 2 s margin, fits until 122.
    requests = countdown_replies(client, rejected("SERVICE_UNAVAILABLE_ERROR"))
    await arm(client)
    clock.now[0] = failed_at
    if retried:
        await runner.renew(client)
        assert len(requests) == 3 and client.heartbeat_record["halted_by"] is None
    else:
        with pytest.raises(BadRequestException):
            await runner.renew(client)
        assert len(requests) == 2 and clock.waits == []
        halted = client.heartbeat_record["halted_by"]
        assert halted["error_code"] == "SERVICE_UNAVAILABLE_ERROR" and halted["http_status"] == 400
        assert client.lease.state["runner"]["heartbeat"]["halted_by"] == halted


async def test_renewals_that_keep_failing_are_retried_until_the_deadline_leaves_no_room(client, clock):  # noqa: F811
    requests = countdown_replies(client, *[rejected("SERVICE_UNAVAILABLE_ERROR")] * 20)
    await arm(client)
    clock.now[0] = 115.0
    with pytest.raises(BadRequestException):
        await runner.renew(client)
    # Attempts at 115..123; the one at 123 can no longer be retried in time.
    assert len(requests) == 1 + 9 and client.heartbeat_record["retries"] == 8
    assert client.heartbeat_deadline == 130.0  # never moved by a failed attempt


@pytest.mark.parametrize(
    "failure, code",
    [
        (ApiException(status=429, reason="Too Many Requests"), "EDGE_RATE_LIMITED"),
        (rejected("UNAUTHORIZED_SIGNATURE_ERROR"), "UNAUTHORIZED_SIGNATURE_ERROR"),
        (rejected("INPUT_VALIDATION_ERROR"), "INPUT_VALIDATION_ERROR"),
        (lambda request: CancelAllAfterResponse(account_id=1, timeout_ms=30000, trigger_at=1), None),
        (lambda request: CancelAllAfterResponse(account_id=8009, timeout_ms=30000, trigger_at=None), None),
    ],
)
async def test_what_cannot_be_retried_halts_at_once(client, clock, failure, code):  # noqa: F811
    requests = countdown_replies(client, failure)
    await arm(client)
    clock.now[0] = 110.0
    with pytest.raises((ApiException, ValueError)):
        await runner.renew(client)
    assert len(requests) == 2 and clock.waits == []
    halted = client.heartbeat_record["halted_by"]
    assert client.heartbeat_record["retries"] == 0 and halted.get("error_code") == code
    if code is None:
        assert "did not confirm" in halted["error"]


async def test_a_retry_answered_after_the_old_deadline_is_never_accepted(client, clock):  # noqa: F811
    def late(request):
        clock.now[0] = 131.0  # the reply arrives after the last confirmed deadline
        return confirmed(request)

    requests = countdown_replies(client, rejected("SERVICE_UNAVAILABLE_ERROR"), late)
    await arm(client)
    clock.now[0] = 120.0
    with pytest.raises(ValueError, match="heartbeat"):
        await runner.renew(client)
    assert len(requests) == 3 and client.heartbeat_deadline == 130.0
    assert "heartbeat" in client.heartbeat_record["halted_by"]["error"]


async def test_trade_writes_go_on_while_a_renewal_waits_to_retry(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner, "HEARTBEAT_RETRY_S", 0.6)
    calls = []

    async def countdown(request):
        calls.append("countdown")
        if calls.count("countdown") == 2:
            raise rejected("SERVICE_UNAVAILABLE_ERROR")
        return confirmed(request)

    async def create(*args, **kwargs):
        calls.append("create")
        return CreateOrderResponse(order_id="1", status="OPEN", cum_qty="0")

    monkeypatch.setattr(client.orders, "cancel_all_after", countdown)
    monkeypatch.setattr(client.orders, "create_order", create)
    await client.start()
    await client.refresh()
    renewal = asyncio.create_task(runner.renew(client))
    while client.heartbeat_record["retries"] == 0:
        await asyncio.sleep(0.01)
    await asyncio.wait_for(
        client.create_limit_order(
            LimitOrderParameters(
                symbol="ETHRUSDPERP", is_buy=True, qty="0.01", limit_px="1900", time_in_force=TimeInForce.GTC
            )
        ),
        timeout=0.3,  # well inside the 0.6 s wait: the wait holds no lock
    )
    assert not renewal.done()
    await renewal
    assert calls == ["countdown", "countdown", "create", "countdown"]


async def test_a_session_survives_a_failed_renewal(client, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(runner, "HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(runner, "HEARTBEAT_RETRY_S", 0.01)
    requests = countdown_replies(client, rejected("SERVICE_UNAVAILABLE_ERROR"))

    reader = stream(eof=False)

    async def later():
        while len(requests) < 4:  # arm, the failed renewal, its retry, the next renewal
            await asyncio.sleep(0.01)
        reader.feed_data(b'{"id":"s","command":["status"]}\n')
        reader.feed_eof()

    feeder = asyncio.create_task(later())
    result = await runner.serve(reader, client, policy())
    await feeder
    events = outputs(capsys)
    assert result["reason"] == "eof"
    status = next(e for e in events if e.get("id") == "s")
    assert status["heartbeat"]["retries"] == 1
    assert status["heartbeat"]["last_retry"]["error_code"] == "SERVICE_UNAVAILABLE_ERROR"
    assert events[-1]["heartbeat"]["halted_by"] is None


async def test_an_edge_ban_on_the_renewal_ends_the_session_and_says_why(client, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(runner, "HEARTBEAT_INTERVAL_S", 0.01)
    requests = countdown_replies(client, ApiException(status=429, reason="Too Many Requests"))
    result = await runner.serve(stream(eof=False), client, policy())
    events = outputs(capsys)
    assert result["reason"] == "heartbeat_failed" and not result["reconciled"]
    assert len(requests) == 2
    stopped = events[-1]
    assert stopped["event"] == "stopped" and stopped["heartbeat"]["halted_by"]["error_code"] == "EDGE_RATE_LIMITED"
    assert client.lease.state["runner"]["heartbeat"]["halted_by"]["error_code"] == "EDGE_RATE_LIMITED"
    client.orders.create_order.assert_not_awaited()


def test_service_unavailable_on_one_write_keeps_the_session():
    # The API's contract (HTTP 400): the request never reached the matching engine.
    result = {"error": "x", "error_code": "SERVICE_UNAVAILABLE_ERROR", "http_status": 400}
    for action in ("market", "limit", "modify", "cancel", "tp", "sl"):
        assert runner.definitive_outcome(result, ["order", action, "ETHRUSDPERP"]) is True
    # An entry left unprotected or with unknown protection still halts.
    for state in ("unprotected", "unknown", "partial", "pending"):
        assert runner.definitive_outcome({**result, "protection": {"state": state}}, ["order", "market"]) is False
    assert runner.definitive_outcome({**result, "http_status": 503}, ["order", "limit"]) is False
