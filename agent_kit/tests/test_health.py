"""Unit tests for `agent_kit/scripts/health.py`.

health.py constructs its own ApiClient/ReferenceDataApi (it does not use
ReyaTradingClient), so these tests patch those symbols locally rather than
via the shared conftest fixtures.
"""

from types import SimpleNamespace
from typing import Any

import sys
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest
from sdk.open_api.api.reference_data_api import ReferenceDataApi

from agent_kit.scripts.health import check_health, main
from agent_kit.tests.conftest import make_market_def, parse_stdout


@pytest.fixture
def patch_health_api(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub health.py's ApiClient and ReferenceDataApi; returns the API mock."""
    fake_api_client = MagicMock()
    fake_api_client.rest_client = None
    monkeypatch.setattr("agent_kit.scripts.health.ApiClient", lambda *a, **kw: fake_api_client)
    api = create_autospec(ReferenceDataApi, instance=True)
    monkeypatch.setattr("agent_kit.scripts.health.ReferenceDataApi", lambda *a, **kw: api)
    return api


@pytest.mark.asyncio
async def test_health_ok_counts_perp_markets(
    patch_health_api: AsyncMock, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REYA_API_URL", "https://api.test/v2")
    patch_health_api.get_perp_market_definitions.return_value = [
        make_market_def(symbol="ETHRUSDPERP", market_id=1),
        make_market_def(symbol="BTCRUSDPERP", market_id=2),
    ]

    await check_health()
    output = parse_stdout(capsys)

    assert output["status"] == "ok"
    assert output["api_url"] == "https://api.test/v2"
    assert output["markets_available"] == 2
    assert isinstance(output["latency_ms"], int)
    assert output["latency_ms"] >= 0
    assert isinstance(output["sdk_version"], str)
    assert output["sdk_version"] != ""


@pytest.mark.asyncio
async def test_health_error_reports_and_exits_1(
    patch_health_api: AsyncMock, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REYA_API_URL", "https://api.test/v2")
    patch_health_api.get_perp_market_definitions.side_effect = ValueError("boom")

    with pytest.raises(SystemExit) as exc_info:
        await check_health()
    assert exc_info.value.code == 1

    output = parse_stdout(capsys)
    assert output["status"] == "error"
    assert "ValueError: boom" in output["error"]


def test_health_stray_args_honor_json_contract(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """health takes no arguments; stray ones must produce {"error": ...} + exit 1, not argparse usage text."""
    monkeypatch.setattr(sys, "argv", ["health", "bogus"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    output = parse_stdout(capsys)
    assert "error" in output
    assert "bogus" in output["error"]


def test_health_constructor_failure_honors_json_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Failures before check_health's own handler (e.g. client construction) still emit JSON, not a traceback.

    asyncio.run is stubbed in health's namespace rather than executed: a real
    asyncio.run() here would tear down the main-thread event loop and break
    pytest-asyncio's session-scoped loop for every async test after this one.
    """

    def _run(coro: Any) -> None:
        # Close the never-awaited coroutine (avoids a RuntimeWarning), then fail
        # the way a client-construction error would surface through asyncio.run.
        coro.close()
        raise RuntimeError("constructor boom")

    monkeypatch.setattr("agent_kit.scripts.health.asyncio", SimpleNamespace(run=_run))
    monkeypatch.setattr(sys, "argv", ["health"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    output = parse_stdout(capsys)
    assert output["status"] == "error"
    assert "RuntimeError: constructor boom" in output["error"]
