"""Anchored .env loading for the Reya Agent Kit.

`.env` lives at the kit root, next to `pyproject.toml` and `SKILL.md`. A bare
`load_dotenv()` searches from the current working directory, which silently
finds nothing (or worse, an unrelated project's `.env`) when the scripts are
invoked from elsewhere — e.g. an agent shelling out to the venv interpreter
from its own working directory. Anchoring to this file's location makes
credential loading cwd-independent.
"""

import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from eth_utils import is_address
from sdk.reya_rest_api.config import (
    CRONOS_ORDERS_GATEWAY,
    DEVNET1_ORDERS_GATEWAY,
    MAINNET_ORDERS_GATEWAY,
    TradingConfig,
    settlement_headroom_from_env,
)

KIT_ROOT = Path(__file__).resolve().parents[2]


def load_env() -> None:
    """Load the kit-root .env. Existing environment variables take precedence."""
    load_dotenv(dotenv_path=KIT_ROOT / ".env")


# Chain ID alone cannot distinguish devnet1 from Cronos. Resolve all commands
# from the same URL, and pass the signing config explicitly to the SDK (whose
# defaults can otherwise be evaluated before the kit's .env is loaded).
DEPLOYMENTS = {
    "https://api.reya.xyz/v2": (1729, MAINNET_ORDERS_GATEWAY),
    "https://api-devnet.reya-cronos.network/v2": (89346162, DEVNET1_ORDERS_GATEWAY),
    "https://api-cronos.reya.xyz/v2": (89346162, CRONOS_ORDERS_GATEWAY),
}


def get_api_url() -> str:
    load_env()
    chain = int(os.environ.get("CHAIN_ID", "1729"))
    default = "https://api.reya.xyz/v2" if chain == 1729 else "https://api-devnet.reya-cronos.network/v2"
    url = os.environ.get("REYA_API_URL", default).rstrip("/")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v2"
    ):
        raise ValueError("REYA_API_URL must be an HTTP(S) /v2 URL without credentials, query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("REYA_API_URL must use HTTPS except on localhost")
    if url in DEPLOYMENTS and chain != DEPLOYMENTS[url][0]:
        raise ValueError("CHAIN_ID and REYA_API_URL refer to different deployments")
    mode = os.environ.get("REYA_MODE")
    if mode == "paper":
        raise ValueError("Use the isolated agent_kit.scripts.paper command for paper trading; no live fallback")
    if mode not in (None, "read-only", "devnet", "live"):
        raise ValueError("REYA_MODE must be read-only, devnet or live; use the separate paper command for simulation")
    if mode == "live" and url != "https://api.reya.xyz/v2":
        raise ValueError("REYA_MODE=live requires mainnet configuration")
    if mode == "devnet" and url == "https://api.reya.xyz/v2":
        raise ValueError("REYA_MODE=devnet conflicts with mainnet")
    return url


def _validate_key(key: str) -> str:
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", key):
        raise ValueError("Private key must contain exactly 32 bytes of hexadecimal text")
    if not 0 < int(key, 16) < 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141:
        raise ValueError("Private key is outside the valid secp256k1 range")
    return key


def read_private_key_file(filename: str) -> str:
    if not Path(filename).is_absolute():
        raise ValueError("Private key file must be an absolute path")
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) not in (0o600, 0o400)
            ):
                raise ValueError("Private key file must be a regular file owned by you with mode 0600 or 0400")
            if info.st_size > 128:
                raise ValueError("Private key file must contain a single hexadecimal key")
            key = source.read(256).decode("ascii").strip()
    except (OSError, UnicodeError):
        raise ValueError("Cannot read private key file securely") from None
    return _validate_key(key)


# Signer selection. The default signer is the agent key the kit generated itself
# (`agent init`) and the owner approved in the Reya app; it never sees the
# owner's key. `REYA_SIGNER=own-key` is the deliberate alternative for a
# dedicated wallet whose key the user chooses to hand to the kit.
AGENT_KEY_VAR = "PERP_AGENT_PRIVATE_KEY"
AGENT_ADDRESS_VAR = "PERP_AGENT_ADDRESS"
AGENT_ACCOUNTS_VAR = "PERP_AGENT_ACCOUNT_IDS"
SIGNER_VAR = "REYA_SIGNER"
SIGNER_MODES = ("agent", "own-key")

OWN_KEY_NOT_SELECTED = (
    "PERP_PRIVATE_KEY_1/PERP_PRIVATE_KEY_FILE are set but REYA_SIGNER is not own-key. The kit signs with "
    "the agent key from `agent init` by default; set REYA_SIGNER=own-key to use your own key deliberately."
)


def signer_mode() -> str:
    mode = os.environ.get(SIGNER_VAR, "agent")
    if mode not in SIGNER_MODES:
        raise ValueError("REYA_SIGNER must be agent (default) or own-key")
    return mode


def _own_key() -> str | None:
    """Read one explicit credential source without including secrets in errors."""
    key = os.environ.get("PERP_PRIVATE_KEY_1")
    filename = os.environ.get("PERP_PRIVATE_KEY_FILE")
    if key and filename:
        raise ValueError("Set only one of PERP_PRIVATE_KEY_1 and PERP_PRIVATE_KEY_FILE")
    return read_private_key_file(filename) if filename else (_validate_key(key) if key else None)


def _private_key() -> tuple[str | None, str | None]:
    """Resolve (key, source) for the selected signer mode; source is 'agent', 'own-key' or None."""
    if signer_mode() == "own-key":
        key = _own_key()
        return key, "own-key" if key else None
    if os.environ.get("PERP_PRIVATE_KEY_1") or os.environ.get("PERP_PRIVATE_KEY_FILE"):
        raise ValueError(OWN_KEY_NOT_SELECTED)
    key = os.environ.get(AGENT_KEY_VAR)
    return (_validate_key(key), "agent") if key else (None, None)


def agent_address() -> str | None:
    """The agent key's address, derived in-process; the key itself is never returned or logged."""
    key = os.environ.get(AGENT_KEY_VAR)
    if not key:
        return None
    from eth_account import Account

    derived = Account.from_key(_validate_key(key)).address
    recorded = os.environ.get(AGENT_ADDRESS_VAR)
    if recorded and recorded.lower() != derived.lower():
        raise ValueError("PERP_AGENT_ADDRESS does not match PERP_AGENT_PRIVATE_KEY; run agent status")
    return derived


def agent_account_ids() -> set[int]:
    """Accounts recorded as agentic by `agent init`; the agent key trades nothing else."""
    raw = os.environ.get(AGENT_ACCOUNTS_VAR, "")
    result = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isascii() or not part.isdecimal() or not 0 < int(part) <= 2**53 - 1:
            raise ValueError("PERP_AGENT_ACCOUNT_IDS must be a comma-separated list of account IDs")
        result.add(int(part))
    return result


MISSING_SIGNING_KEY = (
    "Writes need a signer. Default: the agent key created by `agent init` (PERP_AGENT_PRIVATE_KEY) and approved "
    "in the Reya app. Alternative: REYA_SIGNER=own-key with PERP_PRIVATE_KEY_FILE (owner-only 0600/0400 file) "
    "or PERP_PRIVATE_KEY_1, not both. Reads need no key."
)

AGENT_ACCOUNT_GUARD = (
    "The agent key only trades accounts recorded by `agent init` (PERP_AGENT_ACCOUNT_IDS); PERP_ACCOUNT_ID_1 "
    "is not one of them. Run agent init, or set REYA_SIGNER=own-key to trade this account with your own key."
)


def _private_env_path() -> Path:
    path = KIT_ROOT / ".env"
    if path.is_symlink():
        raise ValueError(".env must be a regular file, not a symlink")
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(".env must be a regular file owned by you")
    return path


def write_env_values(values: dict[str, str]) -> None:
    """Persist settings to the kit-root .env (created 0600) and the current process.

    Used by `agent init` for the owner, the generated agent key and the agentic
    account record. Other lines are preserved; the file is left owner-only.
    """
    from dotenv import set_key

    path = _private_env_path()
    for name, value in values.items():
        if not re.fullmatch(r"[A-Z0-9_]+", name) or any(ch in value for ch in "\r\n"):
            raise ValueError("Refusing to write an invalid .env entry")
        set_key(path, name, value, quote_mode="never")
        os.environ[name] = value
    os.chmod(path, 0o600)


def get_owner_address() -> str:
    """The configured owner wallet, validated without requiring any trading settings."""
    owner = os.environ.get("PERP_WALLET_ADDRESS_1")
    if not owner:
        raise ValueError("PERP_WALLET_ADDRESS_1 is required for account commands")
    if not is_address(owner) or int(owner, 16) == 0:
        raise ValueError("PERP_WALLET_ADDRESS_1 must be a nonzero EVM address")
    return owner


def get_trading_config(*, include_private_key: bool = True, require_private_key: bool = False) -> TradingConfig:
    """Resolve account and signing settings without the SDK searching the cwd.

    Signing paths pass ``require_private_key`` so a missing credential names the
    variables to set instead of surfacing the SDK's generic signer error.
    """
    url = get_api_url()
    if include_private_key and os.environ.get("REYA_MODE") == "read-only":
        raise ValueError("REYA_MODE=read-only disables trading credentials and writes")
    owner = get_owner_address()
    account = os.environ.get("PERP_ACCOUNT_ID_1")
    if account and (not account.isascii() or not account.isdecimal() or not 0 < int(account) <= 2**53 - 1):
        raise ValueError("PERP_ACCOUNT_ID_1 must be a positive exact API integer")
    gateway = os.environ.get("REYA_ORDERS_GATEWAY")
    if url in DEPLOYMENTS:
        expected_gateway = DEPLOYMENTS[url][1]
        if gateway and gateway.lower() != expected_gateway.lower():
            raise ValueError("REYA_ORDERS_GATEWAY does not match REYA_API_URL")
        gateway = expected_gateway
    elif not gateway or not os.environ.get("CHAIN_ID"):
        raise ValueError("Custom deployments require CHAIN_ID and REYA_ORDERS_GATEWAY")
    # Do not guess which exchange a test deployment has registered.
    dex = os.environ.get("REYA_DEX_ID")
    if url != "https://api.reya.xyz/v2" and not dex:
        raise ValueError("REYA_DEX_ID is required for non-mainnet account commands")
    private_key, source = _private_key() if include_private_key else (None, None)
    if require_private_key and private_key is None:
        raise ValueError(MISSING_SIGNING_KEY)
    # Policy guard for the delegated agent key: the on-chain permission is wallet-wide,
    # so the kit itself refuses any account that `agent init` did not record as agentic.
    if source == "agent" and account and int(account) not in agent_account_ids():
        raise ValueError(AGENT_ACCOUNT_GUARD)
    return TradingConfig(
        api_url=url,
        chain_id=int(os.environ.get("CHAIN_ID", "1729")),
        owner_wallet_address=owner,
        private_key=private_key,
        account_id=int(os.environ["PERP_ACCOUNT_ID_1"]) if os.environ.get("PERP_ACCOUNT_ID_1") else None,
        orders_gateway_address=gateway,
        dex_id_override=int(dex) if dex else 2,
        settlement_headroom_s=settlement_headroom_from_env(),
    )
