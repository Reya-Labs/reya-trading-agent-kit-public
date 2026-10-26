"""Configuration diagnostics must never inspect or disclose credentials."""

import json
from pathlib import Path

import pytest

from agent_kit.scripts import _env, doctor


@pytest.mark.parametrize(
    "url",
    [
        "https://api.reya.xyz/api",
        "https://api.reya.xyz/v2?token=secret",
        "https://user:secret@api.reya.xyz/v2",
        "https://api.reya.xyz/v2#secret",
    ],
)
def test_non_v2_or_secret_bearing_base_is_rejected(monkeypatch, url):
    monkeypatch.setenv("REYA_API_URL", url)
    with pytest.raises(ValueError):
        _env.get_api_url()
    result = doctor.diagnose()
    assert result["status"] == "error"
    assert "secret" not in json.dumps(result)
    assert result["api_url"] is None


@pytest.mark.parametrize("mode", ["paper", "typo", "devnet"])
def test_mode_cannot_silently_change_to_mainnet(monkeypatch, mode):
    monkeypatch.setenv("REYA_MODE", mode)
    with pytest.raises(ValueError):
        _env.get_api_url()


def test_read_only_blocks_trading_credentials_but_permits_account_reads(monkeypatch):
    monkeypatch.setenv("REYA_MODE", "read-only")
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", "0x" + "11" * 20)
    monkeypatch.setenv("PERP_PRIVATE_KEY_FILE", "/must/not/open")
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    assert _env.get_trading_config(include_private_key=False).private_key is None
    with pytest.raises(ValueError, match="read-only"):
        _env.get_trading_config()


def test_diagnostics_report_source_without_reading_key(monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", "0x" + "11" * 20)
    monkeypatch.setenv("PERP_ACCOUNT_ID_1", "7")
    monkeypatch.setenv("PERP_PRIVATE_KEY_FILE", "/private/secret-key")
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    monkeypatch.setattr(_env, "read_private_key_file", lambda _: pytest.fail("Doctor read a key"))
    result = doctor.diagnose()
    assert result["account_id"] == 7
    assert result["credential_source"] == "file"
    assert result["credential_contents_checked"] is False
    assert "/private" not in json.dumps(result)


def test_credential_conflict_and_invalid_account_have_actionable_diagnostics(monkeypatch):
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", "secret")
    monkeypatch.setenv("PERP_PRIVATE_KEY_FILE", "/also-secret")
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", "0x" + "11" * 20)
    monkeypatch.setenv("PERP_ACCOUNT_ID_1", "-1")
    result = doctor.diagnose()
    assert len(result["errors"]) == 2
    assert "secret" not in json.dumps(result)


def test_skill_diagnostics_distinguish_this_checkout_from_other_install(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    directory = tmp_path / ".claude" / "skills"
    directory.mkdir(parents=True)
    (directory / "reya-trading-agent-kit").symlink_to(_env.KIT_ROOT, target_is_directory=True)
    assert doctor.diagnose()["skill_links"]["claude-code"] is True
    assert doctor.diagnose()["skill_links"]["agentskills"] is False


def test_skill_diagnostics_report_the_codex_link_under_codex_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    directory = tmp_path / "codex" / "skills"
    directory.mkdir(parents=True)
    (directory / "reya-trading-agent-kit").symlink_to(_env.KIT_ROOT, target_is_directory=True)
    links = doctor.diagnose()["skill_links"]
    assert links == {"claude-code": False, "agentskills": False, "codex": True}


def _devnet_owner(monkeypatch):
    monkeypatch.setenv("REYA_MODE", "devnet")
    monkeypatch.setenv("CHAIN_ID", "89346162")
    monkeypatch.setenv("REYA_API_URL", "https://api-devnet.reya-cronos.network/v2")
    monkeypatch.setenv("REYA_DEX_ID", "1")
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", "0x" + "11" * 20)
    monkeypatch.setenv("PERP_ACCOUNT_ID_1", "7")


def test_missing_signing_key_names_both_credential_variables(monkeypatch):
    _devnet_owner(monkeypatch)
    assert _env.get_trading_config().private_key is None
    with pytest.raises(ValueError, match="PERP_PRIVATE_KEY_FILE.*PERP_PRIVATE_KEY_1"):
        _env.get_trading_config(require_private_key=True)


async def test_trade_without_key_names_variables_before_constructing_client(monkeypatch):
    from agent_kit.scripts import trade

    _devnet_owner(monkeypatch)

    def unexpected_client(*args, **kwargs):
        pytest.fail("Must name the missing key before constructing a signing client")

    monkeypatch.setattr(trade, "ReyaTradingClient", unexpected_client)
    args = trade.build_parser().parse_args(["order", "market", "ETHRUSDPERP", "--side", "long", "--qty", "0.01"])
    with pytest.raises(ValueError, match="PERP_PRIVATE_KEY_FILE.*PERP_PRIVATE_KEY_1"):
        await trade.cmd_order_market(args)
