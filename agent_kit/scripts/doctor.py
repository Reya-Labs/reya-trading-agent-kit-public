"""Offline, secret-free installation and deployment diagnostics."""

import os
import platform
import stat
import sys
from importlib.metadata import version
from pathlib import Path

from dotenv import dotenv_values

from agent_kit.scripts import _env
from agent_kit.scripts._common import JsonArgumentParser, output_json
from agent_kit.scripts._v2 import WS_URLS


def _env_file_warnings() -> list[str]:
    """Flag a key in a `.env` other users can read. Checks presence and file mode only."""
    path = _env.KIT_ROOT / ".env"
    try:
        info = path.stat()
        values = dotenv_values(path)
        held = [name for name in ("PERP_PRIVATE_KEY_1", _env.AGENT_KEY_VAR) if values.get(name)]
    except (OSError, UnicodeError):
        return []
    getuid = getattr(os, "getuid", None)  # absent on Windows; the mode check still applies
    if held and ((getuid is not None and info.st_uid != getuid()) or stat.S_IMODE(info.st_mode) & 0o077):
        return [
            f".env holds {' and '.join(held)} but has mode {stat.S_IMODE(info.st_mode):04o} or another owner; "
            "run chmod 600 .env"
        ]
    return []


def _signer_report(errors: list[str]) -> dict:
    """Describe the selected signer without reading key material."""
    inline = bool(os.environ.get("PERP_PRIVATE_KEY_1"))
    keyfile = bool(os.environ.get("PERP_PRIVATE_KEY_FILE"))
    agent_key = bool(os.environ.get(_env.AGENT_KEY_VAR))
    try:
        mode = _env.signer_mode()
    except ValueError:
        errors.append("REYA_SIGNER must be agent (default) or own-key")
        mode = None
    if inline and keyfile:
        errors.append("Choose only one own-key credential source")
    if mode == "agent" and (inline or keyfile):
        errors.append(_env.OWN_KEY_NOT_SELECTED)
    address = None
    agent_accounts: list[int] = []
    if agent_key:
        try:
            address = _env.agent_address()
            agent_accounts = sorted(_env.agent_account_ids())
        except ValueError as exc:
            errors.append(str(exc))
    if mode == "own-key":
        source = "conflict" if inline and keyfile else "file" if keyfile else "environment" if inline else "none"
    else:
        source = "agent" if agent_key else "none"
    return {
        "signer_mode": mode,
        "credential_source": source,
        "agent_address": address,
        "agent_account_ids": agent_accounts,
    }


def diagnose():
    _env.load_env()
    errors = []
    api_url = None
    try:
        api_url = _env.get_api_url()
    except (ValueError, TypeError):
        errors.append("Invalid or inconsistent REYA_API_URL/CHAIN_ID/REYA_MODE; consult .env.example")
    owner = os.environ.get("PERP_WALLET_ADDRESS_1")
    config = None
    if owner:
        try:
            config = _env.get_trading_config(include_private_key=False)
        except (ValueError, TypeError):
            errors.append(
                "Invalid account configuration; verify owner, account ID, deployment, gateway and exchange ID"
            )
    signer = _signer_report(errors)
    links = {}
    codex_home = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
    for client, directory in (
        ("claude-code", Path.home() / ".claude/skills"),
        ("agentskills", Path.home() / ".agents/skills"),
        ("codex", codex_home / "skills"),
    ):
        link = directory / "reya-trading-agent-kit"
        links[client] = link.is_symlink() and link.resolve() == _env.KIT_ROOT
    return {
        "status": "error" if errors else "ok",
        "network_checked": False,
        "kit_version": version("reya-trading-agent-kit"),
        "python": platform.python_version(),
        "platform": platform.system(),
        "api_url": api_url,
        "mode": os.environ.get("REYA_MODE", "inferred_from_deployment"),
        "real_funds": api_url == "https://api.reya.xyz/v2",
        "websocket_mapping_available": api_url in WS_URLS,
        "owner_configured": bool(owner),
        "account_id": config.account_id if config else None,
        "ownership_verified": False,
        **signer,
        "credential_contents_checked": False,
        "skill_links": links,
        "paper_trading": "available_via_separate_paper_command",
        "non_v2_exception": (
            "account relay POST and Core nonce GET kept under the exception documented 12 Oct 2026; "
            "apiWalletStatus GET for agent approval under the exception documented 14 Oct 2026"
        ),
        "errors": errors,
        "warnings": _env_file_warnings(),
        "next_steps": [
            "Run health and data market summary --symbol ETHRUSDPERP for credential-free V2 reads.",
            "Run agent init --owner <wallet> for the guided setup: app-approved agent key, agent account, funding.",
            "The agent key trades only accounts recorded in PERP_AGENT_ACCOUNT_IDS; the on-chain permission is wallet-wide.",
        ],
    }


def main():
    JsonArgumentParser(prog="doctor", description=__doc__).parse_args()
    result = diagnose()
    output_json(result)
    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
