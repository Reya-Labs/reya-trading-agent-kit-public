"""Host-local coordination across processes, restarts and real SDK signatures."""

import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
from eth_account import Account
from sdk.open_api.models.create_order_response import CreateOrderResponse
from sdk.open_api.models.time_in_force import TimeInForce
from sdk.reya_rest_api.config import TradingConfig
from sdk.reya_rest_api.models.orders import LimitOrderParameters

from agent_kit.scripts._writer import CoordinatedTradingClient, WriterLease, trading_resources
from agent_kit.tests.conftest import make_market_def


def test_cross_process_exclusion_and_release():
    code = "from agent_kit.scripts._writer import WriterLease\nwith WriterLease(['same']): print('acquired')"
    with WriterLease(["same"]):
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode != 0 and "Another kit writer" in result.stderr
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "acquired"


def test_nonce_survives_restart_and_clock_rollback(monkeypatch):
    monkeypatch.setattr("agent_kit.scripts._writer.time.time", lambda: 20)
    with WriterLease(["owner", "delegate"]) as lease:
        first = lease.next_nonce()
    monkeypatch.setattr("agent_kit.scripts._writer.time.time", lambda: 10)
    with WriterLease(["another-owner", "delegate"]) as lease:
        assert lease.next_nonce() == first + 1
    with WriterLease(["owner", "another-delegate"]) as lease:
        assert lease.next_nonce() == first + 1


@pytest.mark.parametrize("bad", ["directory-mode", "state-mode", "state-symlink", "lock-symlink", "corrupt"])
def test_unsafe_or_corrupt_state_fails_closed(bad, tmp_path):
    with WriterLease(["test"]) as lease:
        lease.next_nonce()
        path = lease.directory / (lease.keys[0] + ".json")
        lock = lease.directory / (lease.keys[0] + ".lock")
    if bad == "directory-mode":
        path.parent.chmod(0o755)
    elif bad == "state-mode":
        path.chmod(0o644)
    elif bad in ("state-symlink", "lock-symlink"):
        target = tmp_path / "outside"
        target.write_text("{}")
        victim = path if bad == "state-symlink" else lock
        victim.unlink()
        victim.symlink_to(target)
    else:
        path.write_text("bad json")
    with pytest.raises((ValueError, OSError)):
        with WriterLease(["test"]):
            pytest.fail("Unsafe state admitted a writer")


async def test_real_sdk_uses_durable_nonce_and_releases_lease(monkeypatch):
    key = "0x" + "11" * 32
    config = TradingConfig(
        private_key=key,
        owner_wallet_address=Account.from_key(key).address,
        account_id=8009,
        api_url="https://api.reya.xyz/v2",
        chain_id=1729,
        dex_id_override=2,
    )
    nonces = []
    for _ in range(2):
        async with CoordinatedTradingClient(config) as client:
            monkeypatch.setattr(
                client.reference, "get_perp_market_definitions", AsyncMock(return_value=[make_market_def()])
            )
            monkeypatch.setattr(client.reference, "get_spot_market_definitions", AsyncMock(return_value=[]))
            send = AsyncMock(return_value=CreateOrderResponse(order_id="1", status="OPEN", cum_qty="0"))
            monkeypatch.setattr(client.orders, "create_order", send)
            await client.start()
            await client.create_limit_order(
                LimitOrderParameters(
                    symbol="ETHRUSDPERP", is_buy=True, limit_px="1900", qty="0.01", time_in_force=TimeInForce.GTC
                )
            )
            payload = send.call_args.kwargs["create_order_request"]
            nonces.append(int(payload.nonce))
            assert len(payload.signature) == 132
            with pytest.raises(ValueError, match="Another kit writer"):
                with WriterLease(trading_resources(config, client.signer_wallet_address)):
                    pass
    assert nonces[1] > nonces[0]
    with WriterLease(trading_resources(config, Account.from_key(key).address)) as lease:
        assert lease.state["last_nonce"] == nonces[-1]
