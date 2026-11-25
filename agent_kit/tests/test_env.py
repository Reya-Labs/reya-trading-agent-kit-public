"""Tests for `agent_kit/scripts/_env.py` — cwd-independent .env loading.

The kit's credentials live in `.env` at the repo root. `load_env()` must find
that file no matter what the current working directory is (an agent shelling
out to the venv interpreter runs from its own cwd), and must never override
real environment variables.
"""

import os

from agent_kit.scripts import _env
from agent_kit.scripts._env import load_env
from agent_kit.scripts.query import _get_api_url


def test_load_env_is_cwd_independent(tmp_path, monkeypatch) -> None:
    """A .env at KIT_ROOT loads even when the cwd is somewhere else entirely."""
    kit_root = tmp_path / "kit"
    kit_root.mkdir()
    (kit_root / ".env").write_text("REYA_API_URL=https://cwd-independent.test/v2\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("REYA_API_URL", raising=False)
    monkeypatch.setattr(_env, "KIT_ROOT", kit_root)

    load_env()

    assert os.environ["REYA_API_URL"] == "https://cwd-independent.test/v2"


def test_load_env_does_not_override_shell_env(tmp_path, monkeypatch) -> None:
    """Real environment variables win over .env values (override=False semantics)."""
    kit_root = tmp_path / "kit"
    kit_root.mkdir()
    (kit_root / ".env").write_text("REYA_API_URL=https://from-file.test/v2\n")
    monkeypatch.setenv("REYA_API_URL", "https://from-shell.test/v2")
    monkeypatch.setattr(_env, "KIT_ROOT", kit_root)

    load_env()

    assert os.environ["REYA_API_URL"] == "https://from-shell.test/v2"


def test_load_env_missing_dotenv_is_noop(tmp_path, monkeypatch) -> None:
    """No .env at KIT_ROOT: load_env() must not raise and must not set anything."""
    monkeypatch.setattr(_env, "KIT_ROOT", tmp_path)
    monkeypatch.delenv("REYA_API_URL", raising=False)

    load_env()

    assert "REYA_API_URL" not in os.environ


def test_kit_root_points_at_repo_root() -> None:
    from pathlib import Path

    root = Path(_env.__file__).resolve().parents[2]
    assert (root / "pyproject.toml").is_file()
    assert (root / "SKILL.md").is_file()
    assert (root / ".env.example").is_file()


def test_get_api_url_reads_anchored_env(tmp_path, monkeypatch) -> None:
    """query._get_api_url resolves the URL from the anchored .env, not the cwd."""
    kit_root = tmp_path / "kit"
    kit_root.mkdir()
    (kit_root / ".env").write_text("REYA_API_URL=https://anchored.test/v2\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REYA_API_URL", raising=False)
    monkeypatch.setattr(_env, "KIT_ROOT", kit_root)

    assert _get_api_url() == "https://anchored.test/v2"


def test_devnet_config_is_loaded_at_call_time(tmp_path, monkeypatch):
    from sdk.reya_rest_api.config import DEVNET1_ORDERS_GATEWAY

    (tmp_path / ".env").write_text(
        "CHAIN_ID=89346162\nREYA_API_URL=https://api-devnet.reya-cronos.network/v2/\n"
        "REYA_DEX_ID=1\nPERP_ACCOUNT_ID_1=10\nPERP_WALLET_ADDRESS_1=0x" + "11" * 20 + "\n"
    )
    config = _env.get_trading_config()
    assert config.api_url == _env.get_api_url() == "https://api-devnet.reya-cronos.network/v2"
    assert config.chain_id == 89346162
    assert config.orders_gateway_address == DEVNET1_ORDERS_GATEWAY
    assert config.dex_id == 1
    assert config.account_id == 10
    assert config.private_key is None  # public wallet reads require no signer


def test_cronos_and_devnet_have_distinct_signing_gateways(monkeypatch):
    from sdk.reya_rest_api.config import CRONOS_ORDERS_GATEWAY

    monkeypatch.setenv("CHAIN_ID", "89346162")
    monkeypatch.setenv("REYA_API_URL", "https://api-cronos.reya.xyz/v2")
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", "0x" + "11" * 20)
    monkeypatch.setenv("REYA_DEX_ID", "1")
    assert _env.get_trading_config().orders_gateway_address == CRONOS_ORDERS_GATEWAY


def test_mixed_chain_and_endpoint_rejected(monkeypatch):
    import pytest

    monkeypatch.setenv("REYA_API_URL", "https://api-devnet.reya-cronos.network/v2")
    with pytest.raises(ValueError, match="different deployments"):
        _env.get_api_url()


def test_nonmainnet_requires_exchange_and_correct_gateway(monkeypatch):
    import pytest

    monkeypatch.setenv("CHAIN_ID", "89346162")
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", "0x" + "11" * 20)
    with pytest.raises(ValueError, match="REYA_DEX_ID"):
        _env.get_trading_config()
    monkeypatch.setenv("REYA_DEX_ID", "1")
    monkeypatch.setenv("REYA_ORDERS_GATEWAY", "0x" + "22" * 20)
    with pytest.raises(ValueError, match="does not match"):
        _env.get_trading_config()


def test_custom_deployment_requires_explicit_signing_domain(monkeypatch):
    import pytest

    monkeypatch.setenv("REYA_API_URL", "http://localhost:3000/v2")
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", "0x" + "11" * 20)
    with pytest.raises(ValueError, match="Custom deployments"):
        _env.get_trading_config()
