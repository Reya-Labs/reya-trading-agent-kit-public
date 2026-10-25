"""Client for Reya's hosted workflow service: documents, signed start and stop, run status.

The service is the only evaluator. `validate` and `preview` ask it; the kit never
decides locally whether a trigger would fire, and there is no replay. Start and
stop are EIP-712 messages signed with the agent key from `agent init`, never the
owner's key, under the domain and types the service publishes at `/v1/info`. The
kit refuses any account that `agent init` did not record as the Agent account.
Reads of runs carry a read grant, also signed with the agent key (see ReadGrants).

Signing is local (eth_account); nothing here creates an RPC provider or a
transaction.

`WorkflowStop` signs `keepPosition`: false closes what the run opened before its
protective stop is cancelled, true keeps the position.
"""

from typing import Any, Callable

import asyncio
import base64
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from eth_account import Account
from eth_account.messages import SignableMessage, encode_typed_data
from eth_keys import keys as eth_keys
from eth_utils import keccak, to_checksum_address

from agent_kit.scripts import _env
from agent_kit.scripts._http import USER_AGENT, RegionUnavailableError, region_blocked
from agent_kit.scripts._sizing import minimum_notional, notional_sizing
from agent_kit.scripts._writer import _open_private, state_directory

# The service for each documented API deployment: a path on the same API host, which the
# gateway strips before the service sees `/v1/...`. A custom deployment sets REYA_WORKFLOWS_URL
# (and REYA_WORKFLOWS_ENVIRONMENT when the API URL is not one of these).
SERVICE_URLS = {
    "https://api.reya.xyz/v2": "https://api.reya.xyz/workflows",
    "https://api-devnet.reya-cronos.network/v2": "https://api-devnet.reya-cronos.network/workflows",
}
ENVIRONMENTS = {
    "https://api.reya.xyz/v2": "mainnet",
    "https://api-devnet.reya-cronos.network/v2": "devnet1",
    "https://api-cronos.reya.xyz/v2": "testnet",
}
SERVICE_URL_VAR = "REYA_WORKFLOWS_URL"
ENVIRONMENT_VAR = "REYA_WORKFLOWS_ENVIRONMENT"
ENVIRONMENT_NAMES = ("mainnet", "testnet", "devnet1")
MODES = ("shadow", "paper", "production")
# The app's names for the modes, which users read. The service, the signed request and `--mode` keep the values.
MODE_NAMES = {"shadow": "Watch only", "paper": "Paper", "production": "Live"}
MODE_TEXTS = {
    "shadow": "Watch only: it records what it would do and sends no orders",
    "paper": "Paper: fills are simulated into a labelled ledger, not sent",
    "production": "Live: it sends real orders on the Agent account",
}

# The message fields the kit knows how to fill. `/v1/info` supplies the order and the domain;
# a service that publishes any other field set needs a newer kit.
MESSAGE_FIELDS = {
    "WorkflowStart": {
        "verifyingChainId": "uint256",
        "environment": "string",
        "accountId": "uint128",
        "versionHash": "bytes32",
        "allocationUsd": "string",
        "expiresAt": "uint64",
        "hostedSigner": "address",
        "mode": "string",
        "fireNow": "bool",
        "signer": "address",
        "nonce": "uint64",
        "deadline": "uint64",
    },
    "WorkflowStop": {
        "verifyingChainId": "uint256",
        "environment": "string",
        "accountId": "uint128",
        "runId": "uint64",
        "keepPosition": "bool",
        "signer": "address",
        "nonce": "uint64",
        "deadline": "uint64",
    },
}
UINT_BITS = {"uint64": 64, "uint128": 128, "uint256": 256}
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
# A start or stop is valid for this long; the service accepts at most `max_deadline_secs` (600).
REQUEST_LIFETIME_S = 300
# The service refuses bodies over 16 KiB; a document is most of a start's body.
MAX_DOCUMENT_BYTES = 12 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024

UINT_RE = re.compile(r"(0|[1-9][0-9]*)")
DECIMAL_RE = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]*[1-9])?")
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
SYMBOL_RE = re.compile(r"[A-Z0-9]{1,32}")
# A base path in REYA_WORKFLOWS_URL: plain segments only, so `base + "/v1/..."` names exactly one
# path (no empty, dot or escaped segments for a server to normalise differently).
PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_~-][A-Za-z0-9._~-]*")
BYTES32_RE = re.compile(r"0x[0-9a-fA-F]{64}")
SIGNATURE_RE = re.compile(r"0x[0-9a-fA-F]{130}")

# Strategy documents: the service's `reya.workflow/2`. The kit never writes one itself: `draft` fills the service's
# `basket` template and keeps the document the service expands it to.
SCHEMA_TAG = "reya.workflow/2"
TEMPLATE = {"id": "basket", "version": 1}
DEFAULT_BOUND_BPS = 25
MIN_BOUND_BPS, MAX_BOUND_BPS = 10, 100
DEFAULT_MAX_RESTING = 20
MAX_RESTING = 40
MAX_ORDERS_PER_ACTION = 14
MAX_LABEL_CHARS = 60
# Live strategies per account when `/v1/info` does not say (`limits.max_live_strategies_per_account`).
DEFAULT_LIVE_STRATEGIES = 3
# A live strategy holds its markets; a completed one still holds those where its stop is live (`holds_markets`).
LIVE_STATES = ("active", "blocked", "held")

# Named reasons, keyed by the service's stable codes. The service's own fixed text is always
# shown next to the label; a code the kit does not know is shown as itself.
SKIP_REASONS = {
    "skip_level": "a condition did not hold",
    "skip_no_value": "a value it reads has none yet (the strategy holds nothing there)",
    "skip_stale": "a value it needs was stale",
    "skip_superseded": "an earlier transition acted on the same wake-up",
    "skip_firing_held": "firing held by an operator",
    "skip_collar_too_tight": "market price band narrower than the bound",
    "skip_below_minimum": "an order below the market minimum",
    "skip_price_out_of_range": "price outside the venue's range",
    "skip_stop_crossed": "a stop it would arm is already crossed",
    "skip_take_profit_crossed": "a take-profit it would arm is already crossed",
    "skip_budget": "the remaining allocation cannot cover the step",
    "skip_margin": "the account's margin cannot take the step's orders",
    "schedule_missed": "scheduled times passed while it could not act; they are never caught up",
    "crossed_while_stale": "crossed while data was stale, not acted on",
}
STALE_REASONS = {
    "mark_missing": "no mark price yet",
    "mark_stale": "mark price is stale",
    "mark_from_future": "mark price is stamped in the future",
    "market_halted": "a market is halted",
    "account_stale": "account state is behind",
    "funding_stale": "funding samples are behind",
    "permissions_stale": "signer permissions are behind",
    "restarted": "service restarted, waiting for fresh data",
}
STOP_REASONS = {
    "user": "stopped by a signed request",
    "permission_revoked": "the starter's trading permission was revoked",
    "hosted_signer_revoked": "the hosted signer was revoked or withdrawn",
    "outcome_unknown": "an order's outcome stayed unknown",
    "operator": "stopped by a Reya operator",
}
COMPLETION_REASONS = {
    "final_state": "it reached its final state with no resting order left",
    "allocation_used": "its allocation cannot cover another step",
    "expired": "it reached its expiry and sends no new orders",
    "protection_fired": "a stop it armed fired",
}
# Completion alone does not confirm that a protective stop is on the venue.
COMPLETED_PROTECTION = {
    "armed": "stays on the position until it fires or the strategy is stopped",
    "arming": "is still arming; protection is not confirmed",
    "cancel_pending": "is being cancelled; it may still fire until cancellation is confirmed",
    "unknown": "has an unknown status; protection is not confirmed",
}
RESTING_STATUS = {
    "placing": "being placed",
    "resting": "resting on the book",
    "partial": "partly filled, the rest still resting",
    "filled": "filled",
    "cancelled": "cancelled",
    "cancelled_by_owner": "cancelled by the owner",
    "expired": "expired",
    "rejected": "rejected",
    "unknown": "outcome unknown: the strategy is blocked until it is known",
}

# Hints for the service's refusals. Codes the kit does not list still show the service's message.
ERROR_HINTS = {
    "NOT_AGENT_ACCOUNT": (
        "The service does not see this account as the Agent account. Run agent status: it names the command that "
        "switches the kit to your Agent account."
    ),
    "NOT_PERMITTED": "The agent key is not an approved signer for this account's owner. Run agent status.",
    "HOSTED_SIGNER_NOT_ENABLED": (
        "Hosted runs need one more approval in the Reya app: the owner approves the hosted signer (`hosted_signer`) "
        "once, on the setup page's Enable hosted runs step (`enable_url`), then starts again."
    ),
    "UNKNOWN_HOSTED_SIGNER": "The hosted signer changed; run the start again so it signs the current one.",
    "MARKET_IN_USE": (
        "Another strategy on this account already trades one of these markets: stop it first, or draft this one "
        "without that market. strategy status lists them."
    ),
    "ACCOUNT_STRATEGY_LIMIT": (
        "This account already runs as many strategies as it may: stop one first (strategy status lists them)."
    ),
    "NONCE_TOO_LOW": "Nothing was started or stopped. Run the same command again for a fresh nonce.",
    "REQUEST_EXPIRED": "Nothing was started or stopped. Check this computer's clock, then run the command again.",
    "MODE_NOT_ALLOWED": "This environment does not allow that mode; strategy info lists the modes it allows.",
    "ABOVE_PILOT_LIMIT": "Lower the allocation or order size to the pilot caps (strategy info).",
    "DOCUMENT_MISMATCH": "The signed fields differ from the document; draft or validate it again.",
    "WRONG_ENVIRONMENT": "The kit and the service disagree on the environment; check REYA_API_URL and REYA_WORKFLOWS_URL.",
    "RATE_LIMITED": "Wait `retry_after_s` seconds, then try again.",
    "INVALID_READ_GRANT": (
        "The service did not accept the agent key's read grant. The owner may have revoked the agent key: run "
        "agent status and, if it asks, re-approve the agent key in the Reya app. Also check this computer's clock "
        "(a grant is valid for an hour from it), then try again."
    ),
    "READ_GRANT_REQUIRED": (
        "Strategy runs are private: reading one needs a grant signed with the agent key, and none was sent. Run "
        "agent status to set up or re-approve the agent key (strategy commands never sign with REYA_SIGNER=own-key), "
        "check this computer's clock, then try again."
    ),
}

# NOT_AGENT_ACCOUNT's `details.reason` (and validate's and preview's `not_agent_account` error's `reason`): why the
# service does not take the account as the Agent account. The strategy commands replace this with the exact fix
# when they can read the owner's accounts (agent.agent_account_warning).
SWITCH_HINT = "Run agent status: it names the command that switches the kit to your Agent account."
AGENT_ACCOUNT_REASONS = {
    "not_tagged": f"The account was not created as the Agent account. {SWITCH_HINT}",
    "main_account": f"That is the owner's main account, not an Agent sub-account. {SWITCH_HINT}",
    "spot_account": f"That is a spot account, not an Agent sub-account. {SWITCH_HINT}",
}


class WorkflowError(ValueError):
    """An operator-facing refusal; carries no key material."""

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class ServiceError(Exception):
    """A refusal from the service, as its error envelope says."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        retryable: bool,
        details: dict,
        retry_after: int | None,
        request_id: str | None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details
        self.retry_after = retry_after
        self.request_id = request_id

    def result(self) -> dict:
        result: dict[str, Any] = {
            "error": self.message,
            "error_code": self.code,
            "retryable": self.retryable,
            "http_status": self.status,
        }
        if self.details:
            result["details"] = self.details
        if self.retry_after is not None:
            result["retry_after_s"] = self.retry_after
        if self.request_id:
            result["request_id"] = self.request_id
        if self.code in ERROR_HINTS:
            result["next"] = ERROR_HINTS[self.code]
        if self.code == "NOT_AGENT_ACCOUNT" and self.details.get("reason") in AGENT_ACCOUNT_REASONS:
            result["next"] = AGENT_ACCOUNT_REASONS[self.details["reason"]]
        if self.code == "MARKET_IN_USE":
            result["next"] = market_in_use_text(self.details) or result["next"]
        if self.code == "ACCOUNT_STRATEGY_LIMIT":
            result["next"] = strategy_limit_text(self.details) or result["next"]
        return result


def _run_ref(value: Any) -> str | None:
    """A run id from the service's details, only when it is one (details are data, never text to repeat)."""
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return text if UINT_RE.fullmatch(text) and int(text) < 2**64 else None


def market_in_use_text(details: dict) -> str | None:
    """MARKET_IN_USE (a start) or the preview's `market_in_use` warning: which strategy holds which market."""
    run, market = _run_ref(details.get("run_id")), details.get("market")
    if run is None or not isinstance(market, str) or not SYMBOL_RE.fullmatch(market):
        return None
    asset = base_asset(market)
    return (
        f"Strategy #{run} already trades {asset}, and one market belongs to one strategy on an account: stop it "
        f"first (strategy stop {run}), or draft this strategy without {asset}."
    )


def strategy_limit_text(details: dict) -> str | None:
    """ACCOUNT_STRATEGY_LIMIT: the account's live strategies, and that one has to stop first."""
    live = [run for run in (_run_ref(r) for r in details.get("live") or []) if run]
    count = _run_ref(details.get("limit"))  # a count, as a JSON number or a decimal string
    if not live or count is None:
        return None
    limit = int(count)
    return (
        f"This account already runs {limit} strateg{'y' if limit == 1 else 'ies'}, the most it may "
        f"({', '.join('#' + run for run in live)}): "
        "stop one first (strategy stop RUN_ID), then start this one again."
    )


class OutcomeUnknown(Exception):
    """A write that may have reached the service, with no answer saying what it did."""


# --------------------------------------------------------------------------
# Deployment
# --------------------------------------------------------------------------


def service_url() -> str:
    """The service's base URL without a trailing slash: an origin, optionally with a base path
    (`https://api.reya.xyz/workflows`). Every request is this plus `/v1/...`."""
    api = _env.get_api_url()
    url = (os.environ.get(SERVICE_URL_VAR) or SERVICE_URLS.get(api) or "").strip().rstrip("/")
    if not url:
        raise WorkflowError(f"No strategy service is known for {api}; set {SERVICE_URL_VAR}")
    parsed = urlsplit(url)
    segments = parsed.path.split("/")[1:] if parsed.path else []
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "?" in url
        or "#" in url
        or (parsed.path and not parsed.path.startswith("/"))
        or not all(PATH_SEGMENT_RE.fullmatch(s) and s not in (".", "..") for s in segments)
    ):
        raise WorkflowError(
            f"{SERVICE_URL_VAR} must be an HTTP(S) URL, optionally with a base path such as "
            "https://api.reya.xyz/workflows, and without credentials, query, fragment or empty, dot "
            "or escaped path segments"
        )
    if segments and segments[-1] == "v1":
        raise WorkflowError(
            f"{SERVICE_URL_VAR} is the service's base URL; leave out /v1 (the kit adds it to every request)"
        )
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise WorkflowError(f"{SERVICE_URL_VAR} must use HTTPS except on localhost")
    return url


def kit_environment() -> tuple[str, int]:
    """(environment name, chain ID) of the deployment the kit is configured for."""
    api = _env.get_api_url()
    name = ENVIRONMENTS.get(api) or os.environ.get(ENVIRONMENT_VAR, "")
    if name not in ENVIRONMENT_NAMES:
        raise WorkflowError(f"Set {ENVIRONMENT_VAR} to mainnet, testnet or devnet1 for a custom REYA_API_URL")
    chain = _env.DEPLOYMENTS[api][0] if api in _env.DEPLOYMENTS else int(os.environ.get("CHAIN_ID", "0") or 0)
    if chain <= 0:
        raise WorkflowError("Custom deployments require CHAIN_ID")
    return name, chain


# --------------------------------------------------------------------------
# The Agent account and the agent key
# --------------------------------------------------------------------------


def agent_account(requested: int | None = None) -> int:
    """The recorded Agent account to use; any other account is refused."""
    recorded = _env.agent_account_ids()
    if not recorded:
        raise WorkflowError(
            "No Agent account is recorded. Strategies only run on the Agent account that agent init records; "
            "run agent init first."
        )
    if requested is not None:
        if requested not in recorded:
            raise WorkflowError(
                f"Account {requested} is not your recorded Agent account ({', '.join(map(str, sorted(recorded)))}). "
                "Strategies only run on the Agent account that agent init recorded."
            )
        return requested
    configured = os.environ.get("PERP_ACCOUNT_ID_1", "")
    if configured.isascii() and configured.isdecimal() and int(configured) in recorded:
        return int(configured)
    if len(recorded) == 1:
        return next(iter(recorded))
    raise WorkflowError(
        f"Several Agent accounts are recorded ({', '.join(map(str, sorted(recorded)))}); pass --account-id"
    )


def agent_signer() -> tuple[str, str]:
    """(agent key, its address) for signing a start or stop. The key never leaves this process."""
    _env.load_env()
    if _env.signer_mode() != "agent":
        raise WorkflowError(
            "Strategy start and stop are signed with the agent key from agent init, never with your own key. "
            "Unset REYA_SIGNER=own-key for strategy commands."
        )
    raw = os.environ.get(_env.AGENT_KEY_VAR)
    if not raw:
        raise WorkflowError("No agent key: run agent init (the key it generates is the one strategies sign with)")
    key = _env._validate_key(raw)
    address = _env.agent_address()
    if address is None:
        raise WorkflowError("No agent key: run agent init")
    owner = os.environ.get("PERP_WALLET_ADDRESS_1", "")
    if owner and owner.lower() == address.lower():
        raise WorkflowError("The agent key is the owner wallet's key; strategies never sign with the owner key")
    return key, to_checksum_address(address)


# --------------------------------------------------------------------------
# /v1/info
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceInfo:
    environment: str
    chain_id: int
    domain: dict[str, str]
    types: dict[str, list[dict[str, str]]]
    hosted_signers: list[dict]
    modes: list[str]
    max_allocation_usd: Decimal
    max_order_usd: Decimal
    max_deadline_secs: int
    # The v2 additions; None when a service does not publish them.
    max_live_strategies: int | None = None
    max_orders_per_action: int | None = None
    max_resting_orders: int | None = None
    schemas: tuple[str, ...] | None = None
    signal_kinds: tuple[str, ...] | None = None

    @property
    def reads_balance(self) -> bool:
        """Whether the service prices the account's margin: it then takes the balance condition and sends the
        balance warnings itself (`/v1/info` lists `available_balance` among its signal kinds)."""
        return self.signal_kinds is not None and "available_balance" in self.signal_kinds

    @property
    def live_strategies(self) -> int:
        """How many strategies may be live on one account (K)."""
        return self.max_live_strategies or DEFAULT_LIVE_STRATEGIES

    @property
    def active_signer(self) -> str | None:
        active = [entry["address"] for entry in self.hosted_signers if entry.get("role") == "active"]
        return to_checksum_address(active[0]) if len(active) == 1 else None

    def check_schema(self) -> None:
        """Refuse to sign for a service that says it does not run the documents this kit drafts."""
        if self.schemas is not None and SCHEMA_TAG not in self.schemas:
            raise WorkflowError(
                f"The strategy service runs {', '.join(self.schemas) or 'no'} strategy documents, and this kit drafts "
                f"{SCHEMA_TAG}; nothing was signed. Update the kit, or wait until the service runs {SCHEMA_TAG}."
            )

    def is_hosted(self, address: str) -> bool:
        return any(entry["address"].lower() == address.lower() for entry in self.hosted_signers)

    def view(self) -> dict:
        return {
            "environment": self.environment,
            "chain_id": self.chain_id,
            "eip712_domain": self.domain,
            "hosted_signer": self.active_signer,
            "hosted_signers": self.hosted_signers,
            "modes": self.modes,
            "limits": {
                "max_allocation_usd": decimal_text(self.max_allocation_usd),
                "max_order_usd": decimal_text(self.max_order_usd),
                "max_deadline_secs": self.max_deadline_secs,
                "max_live_strategies_per_account": self.live_strategies,
                "max_orders_per_action": self.max_orders_per_action or MAX_ORDERS_PER_ACTION,
                "max_resting_orders": self.max_resting_orders or MAX_RESTING,
            },
            "schemas": list(self.schemas) if self.schemas is not None else None,
            "reads_balance": self.reads_balance,
        }


def _incompatible(what: str) -> WorkflowError:
    return WorkflowError(f"The strategy service publishes {what} this kit cannot use; update the kit")


def parse_types(types: Any) -> dict[str, list[dict[str, str]]]:
    if not isinstance(types, dict):
        raise _incompatible("EIP-712 types")
    result = {}
    for primary, known in MESSAGE_FIELDS.items():
        fields = types.get(primary)
        if not isinstance(fields, list) or not all(
            isinstance(f, dict) and set(f) == {"name", "type"} and all(isinstance(v, str) for v in f.values())
            for f in fields
        ):
            raise _incompatible(f"a {primary} type")
        if len(fields) != len(known) or {f["name"]: f["type"] for f in fields} != known:
            raise _incompatible(f"a {primary} type")
        result[primary] = [{"name": f["name"], "type": f["type"]} for f in fields]
    return result


def parse_domain(domain: Any) -> dict[str, str]:
    # The workflow domain has no chainId and no verifyingContract: the message carries the chain and
    # environment instead. Anything else could make the agent key sign under another protocol's domain.
    if not isinstance(domain, dict) or set(domain) != {"name", "version"}:
        raise _incompatible("an EIP-712 domain")
    if not all(isinstance(domain[k], str) and domain[k] for k in ("name", "version")):
        raise _incompatible("an EIP-712 domain")
    return {"name": domain["name"], "version": domain["version"]}


def parse_info(data: Any, environment: str, chain_id: int, url: str) -> ServiceInfo:
    try:
        eip712 = data["eip712"]
        limits = data["limits"]
        signers = data["hosted_signers"]
        info = ServiceInfo(
            environment=data["environment"],
            chain_id=int(uint_text(data["chain_id"], 256)),
            domain=parse_domain(eip712["domain"]),
            types=parse_types(eip712["types"]),
            hosted_signers=[
                {"address": to_checksum_address(s["address"]), "role": s["role"], "can_sign": s["can_sign"] is True}
                for s in signers
                if isinstance(s, dict) and isinstance(s.get("address"), str) and ADDRESS_RE.fullmatch(s["address"])
            ],
            modes=[m for m in data["modes"] if m in MODES],
            max_allocation_usd=Decimal(decimal_value(limits["max_allocation_usd"])),
            max_order_usd=Decimal(decimal_value(limits["max_order_usd"])),
            max_deadline_secs=int(uint_text(limits["max_deadline_secs"], 64)),
            max_live_strategies=_count(limits.get("max_live_strategies_per_account")),
            max_orders_per_action=_count(limits.get("max_orders_per_action")),
            max_resting_orders=_count(limits.get("max_resting_orders")),
            schemas=(
                tuple(x for x in data["schemas"] if isinstance(x, str))
                if isinstance(data.get("schemas"), list)
                else None
            ),
            signal_kinds=_names(
                (data.get("kinds") or {}).get("signals") if isinstance(data.get("kinds"), dict) else None
            ),
        )
    except WorkflowError:
        raise
    except (KeyError, TypeError, ValueError):
        raise WorkflowError(f"{url}/v1/info did not return the strategy service's parameters") from None
    if info.environment != environment or info.chain_id != chain_id:
        raise WorkflowError(
            f"The strategy service at {url} serves {info.environment} (chain {info.chain_id}), but the kit is "
            f"configured for {environment} (chain {chain_id}). Check REYA_API_URL and {SERVICE_URL_VAR}.",
            code="WRONG_ENVIRONMENT",
        )
    return info


def _names(value: Any) -> tuple[str, ...] | None:
    return tuple(x for x in value if isinstance(x, str)) if isinstance(value, list) else None


def _count(value: Any) -> int | None:
    """A positive count from `/v1/info`, as a JSON number or a decimal string; None when absent or malformed."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    if isinstance(value, str) and UINT_RE.fullmatch(value) and 0 < int(value) < 2**32:
        return int(value)
    return None


# --------------------------------------------------------------------------
# Encoding checks, as the service applies them
# --------------------------------------------------------------------------


def uint_text(value: Any, bits: int) -> str:
    if not isinstance(value, str) or not UINT_RE.fullmatch(value) or int(value) >= 2**bits:
        raise WorkflowError(f"not a uint{bits} decimal string: {value!r}", code="INVALID_REQUEST")
    return value


def decimal_value(value: Any) -> str:
    if not isinstance(value, str) or not DECIMAL_RE.fullmatch(value):
        raise WorkflowError(f"not a canonical decimal: {value!r}", code="INVALID_REQUEST")
    return value


def decimal_text(value: Decimal) -> str:
    """The one canonical spelling of a non-negative decimal: no exponent, no trailing zero."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def check_message(primary: str, request: Any) -> None:
    """The request's field set and encodings (the service's INVALID_REQUEST checks)."""
    known = MESSAGE_FIELDS[primary]
    if not isinstance(request, dict) or set(request) != set(known):
        raise WorkflowError(f"{primary} must have exactly the fields {', '.join(known)}", code="INVALID_REQUEST")
    for name, kind in known.items():
        value = request[name]
        if kind in UINT_BITS:
            uint_text(value, UINT_BITS[kind])
        elif kind == "address":
            if not isinstance(value, str) or not ADDRESS_RE.fullmatch(value):
                raise WorkflowError(f"{name} is not an address", code="INVALID_REQUEST")
        elif kind == "bytes32":
            if not isinstance(value, str) or not BYTES32_RE.fullmatch(value):
                raise WorkflowError(f"{name} is not 32 bytes of hex", code="INVALID_REQUEST")
        elif kind == "bool":
            if type(value) is not bool:
                raise WorkflowError(f"{name} must be a JSON boolean", code="INVALID_REQUEST")
        elif not isinstance(value, str):
            raise WorkflowError(f"{name} must be a string", code="INVALID_REQUEST")
    if request["environment"] not in ENVIRONMENT_NAMES:
        raise WorkflowError("unknown environment", code="INVALID_REQUEST")
    if primary == "WorkflowStart":
        decimal_value(request["allocationUsd"])
        if request["mode"] not in MODES:
            raise WorkflowError(f"unknown mode {request['mode']!r}", code="INVALID_REQUEST")


def check_target(request: dict, info: ServiceInfo, now: int) -> None:
    """Environment, chain and deadline, in the service's order after the field checks."""
    if request["environment"] != info.environment or int(request["verifyingChainId"]) != info.chain_id:
        raise WorkflowError("the request names another environment or chain", code="WRONG_ENVIRONMENT")
    deadline = int(request["deadline"])
    if deadline <= now:
        raise WorkflowError(f"the deadline {deadline} has passed (now {now})", code="REQUEST_EXPIRED")
    if deadline > now + info.max_deadline_secs:
        raise WorkflowError(f"the deadline may be at most {info.max_deadline_secs} s ahead", code="DEADLINE_TOO_FAR")


# --------------------------------------------------------------------------
# EIP-712
# --------------------------------------------------------------------------


def _signable(primary: str, fields: list[dict[str, str]], domain: dict[str, str], request: dict) -> SignableMessage:
    """`request`'s JSON spelling (decimal strings, 0x hex) as the EIP-712 values eth_account encodes."""
    values: dict[str, Any] = {}
    for field in fields:
        name, kind = field["name"], field["type"]
        value = request[name]
        if kind in UINT_BITS:
            value = int(value)
        elif kind.endswith("[]") and kind[:-2] in UINT_BITS:
            value = [int(item) for item in value]
        elif kind in ("bytes32", "bytes16"):
            value = bytes.fromhex(value[2:])
        values[name] = value
    return encode_typed_data(
        full_message={
            "types": {
                "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}],
                primary: fields,
            },
            "primaryType": primary,
            "domain": domain,
            "message": values,
        }
    )


def typed_data(primary: str, request: dict, info: ServiceInfo) -> SignableMessage:
    return _signable(primary, info.types[primary], info.domain, request)


def struct_hash(primary: str, request: dict, info: ServiceInfo) -> str:
    return "0x" + typed_data(primary, request, info).body.hex()


def digest(primary: str, request: dict, info: ServiceInfo) -> str:
    message = typed_data(primary, request, info)
    return "0x" + keccak(b"\x19" + message.version + message.header + message.body).hex()


def sign(primary: str, request: dict, key: str, info: ServiceInfo) -> str:
    """The 65-byte r‖s‖v signature (v = 27 or 28). Errors never carry the key."""
    try:
        signed = Account.sign_message(typed_data(primary, request, info), key)
    except Exception:
        raise WorkflowError(f"Signing the {primary} request failed") from None
    return "0x" + bytes(signed.signature).hex()


def recover(primary: str, request: dict, signature: Any, info: ServiceInfo) -> str:
    """The signer of `signature`, by the service's rules: 65 bytes, v in {0, 1, 27, 28}, low s."""
    return recover_digest(digest(primary, request, info), signature)


def recover_digest(digest_hex: str, signature: Any) -> str:
    """The signer of `signature` over an EIP-712 digest, by the service's rules (see recover)."""
    if not isinstance(signature, str) or not SIGNATURE_RE.fullmatch(signature):
        raise WorkflowError("the signature must be 65 bytes of hex", code="BAD_SIGNATURE")
    raw = bytes.fromhex(signature[2:])
    r, s, v = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:64], "big"), raw[64]
    if v not in (0, 1, 27, 28):
        raise WorkflowError(f"v must be 27 or 28, got {v}", code="BAD_SIGNATURE")
    if not 0 < s <= SECP256K1_N // 2 or not 0 < r < SECP256K1_N:
        raise WorkflowError("the signature's s is not in the lower half of the curve order", code="BAD_SIGNATURE")
    try:
        public = eth_keys.Signature(vrs=(v % 27 if v >= 27 else v, r, s)).recover_public_key_from_msg_hash(
            bytes.fromhex(digest_hex[2:])
        )
    except Exception:
        raise WorkflowError("the signature does not recover a signer", code="BAD_SIGNATURE") from None
    return to_checksum_address(public.to_checksum_address())


def verify_body(primary: str, body: Any, info: ServiceInfo, now: int) -> str:
    """The checks the service makes without state, in its order: fields, target, deadline, signature.

    The kit runs them on every request it signed before sending it.
    """
    if not isinstance(body, dict) or "request" not in body or "signature" not in body:
        raise WorkflowError("the body needs request and signature", code="INVALID_REQUEST")
    request = body["request"]
    check_message(primary, request)
    check_target(request, info, now)
    signer = recover(primary, request, body["signature"], info)
    if signer.lower() != request["signer"].lower():
        raise WorkflowError("the signature is not by the request's signer", code="BAD_SIGNATURE")
    return signer


def ordered(primary: str, values: dict, info: ServiceInfo) -> dict:
    return {field["name"]: values[field["name"]] for field in info.types[primary]}


def start_request(
    info: ServiceInfo,
    *,
    account_id: int,
    version_hash: str,
    allocation_usd: str,
    expires_at: int,
    hosted_signer: str,
    mode: str,
    fire_now: bool,
    signer: str,
    nonce: int,
    deadline: int,
) -> dict:
    return ordered(
        "WorkflowStart",
        {
            "verifyingChainId": str(info.chain_id),
            "environment": info.environment,
            "accountId": str(account_id),
            "versionHash": version_hash.lower(),
            "allocationUsd": allocation_usd,
            "expiresAt": str(expires_at),
            "hostedSigner": to_checksum_address(hosted_signer),
            "mode": mode,
            "fireNow": fire_now,
            "signer": to_checksum_address(signer),
            "nonce": str(nonce),
            "deadline": str(deadline),
        },
        info,
    )


def stop_request(
    info: ServiceInfo,
    *,
    account_id: int,
    run_id: int,
    signer: str,
    nonce: int,
    deadline: int,
    keep_position: bool,
) -> dict:
    """The stop message: `keep_position` false closes what the run opened, true keeps it."""
    values = {
        "verifyingChainId": str(info.chain_id),
        "environment": info.environment,
        "accountId": str(account_id),
        "runId": str(run_id),
        "keepPosition": keep_position,
        "signer": to_checksum_address(signer),
        "nonce": str(nonce),
        "deadline": str(deadline),
    }
    return ordered("WorkflowStop", values, info)


def fresh_nonce() -> int:
    """Microseconds since the epoch: strictly increasing per signer across kit invocations.

    The service keeps its own nonce counter, apart from order entry's, so this never
    collides with the agent key's order nonces.
    """
    return time.time_ns() // 1000


def signed_body(primary: str, request: dict, key: str, address: str, info: ServiceInfo, **extra) -> dict:
    """Sign, then check the result the way the service will before it leaves this process."""
    body = {**extra, "request": request, "signature": sign(primary, request, key, info)}
    if verify_body(primary, body, info, int(time.time())) != address:
        raise WorkflowError(f"The signed {primary} request does not verify; nothing was sent")
    return body


# --------------------------------------------------------------------------
# Read grant: what lets the kit read its runs once the service keeps them private
# --------------------------------------------------------------------------

# The three run reads (`GET /v1/runs/{id}`, its activity and `/v1/accounts/{id}/runs`) carry
# `Authorization: Bearer <grant>`. The grant is an EIP-712 ReadGrant under the start and stop domain, signed
# with the agent key (an approved delegate of the owner, never the owner's key): until `expiresAt` its bearer
# may read the owner's runs on `accountIds` (empty would mean all of the owner's accounts). The service accepts
# grants before it requires them; once it requires them it refuses a read with READ_GRANT_REQUIRED (none sent)
# or INVALID_READ_GRANT (one it does not take), and a run the grant does not cover reads as RUN_NOT_FOUND.
READ_GRANT_DOMAIN = {"name": "Reya Workflows", "version": "1"}
READ_GRANT_TYPE = [
    {"name": "verifyingChainId", "type": "uint256"},
    {"name": "environment", "type": "string"},
    {"name": "owner", "type": "address"},
    {"name": "accountIds", "type": "uint128[]"},
    {"name": "expiresAt", "type": "uint64"},
    {"name": "salt", "type": "bytes16"},
]
READ_GRANT_LIFETIME_S = 3600
# A kept grant is replaced once this share of its life has passed, so a command never sends one about to expire.
READ_GRANT_REFRESH_AT = 0.8
READ_GRANT_FILE = "workflow-read-grant.json"
READ_GRANT_PATH_RE = re.compile(r"/v1/runs/[0-9]+(/activity)?|/v1/accounts/[0-9]+/runs")
BYTES16_RE = re.compile(r"0x[0-9a-fA-F]{32}")


def read_grant(*, chain_id: int, environment: str, owner: str, account_ids: Any, expires_at: int, salt: bytes) -> dict:
    """The ReadGrant message in its JSON spelling: integers as decimal strings, hex with 0x."""
    if len(salt) != 16:
        raise WorkflowError("A read grant's salt is 16 bytes")
    return {
        "verifyingChainId": str(chain_id),
        "environment": environment,
        "owner": to_checksum_address(owner),
        "accountIds": [str(account) for account in sorted(account_ids)],
        "expiresAt": str(expires_at),
        "salt": "0x" + salt.hex(),
    }


def check_read_grant(grant: Any) -> None:
    """The grant's field set and encodings, as for start and stop."""
    names = [field["name"] for field in READ_GRANT_TYPE]
    if not isinstance(grant, dict) or list(grant) != names:
        raise WorkflowError(f"ReadGrant must have exactly the fields {', '.join(names)}", code="INVALID_REQUEST")
    uint_text(grant["verifyingChainId"], 256)
    uint_text(grant["expiresAt"], 64)
    if not isinstance(grant["accountIds"], list):
        raise WorkflowError("accountIds must be a list", code="INVALID_REQUEST")
    for account in grant["accountIds"]:
        uint_text(account, 128)
    if grant["environment"] not in ENVIRONMENT_NAMES:
        raise WorkflowError("unknown environment", code="INVALID_REQUEST")
    if not isinstance(grant["owner"], str) or not ADDRESS_RE.fullmatch(grant["owner"]):
        raise WorkflowError("owner is not an address", code="INVALID_REQUEST")
    if not isinstance(grant["salt"], str) or not BYTES16_RE.fullmatch(grant["salt"]):
        raise WorkflowError("salt is not 16 bytes of hex", code="INVALID_REQUEST")


def read_grant_digest(grant: dict) -> str:
    message = _signable("ReadGrant", READ_GRANT_TYPE, READ_GRANT_DOMAIN, grant)
    return "0x" + keccak(b"\x19" + message.version + message.header + message.body).hex()


def sign_read_grant(grant: dict, key: str) -> str:
    """The 65-byte r‖s‖v signature (v = 27 or 28). Errors never carry the key."""
    check_read_grant(grant)
    try:
        signed = Account.sign_message(_signable("ReadGrant", READ_GRANT_TYPE, READ_GRANT_DOMAIN, grant), key)
    except Exception:
        raise WorkflowError("Signing the read grant failed") from None
    return "0x" + bytes(signed.signature).hex()


def read_grant_token(grant: dict, signature: str) -> str:
    """The bearer token: base64url without padding of the compact JSON `{grant, signature}`."""
    raw = json.dumps({"grant": grant, "signature": signature}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def parse_read_grant_token(token: Any) -> tuple[dict, str, str]:
    """(grant, signature, signer) from a token, checked as the service checks it before its state checks."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", token):
        raise WorkflowError("A read grant is base64url text", code="INVALID_REQUEST")
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except ValueError:
        raise WorkflowError("A read grant is base64url JSON", code="INVALID_REQUEST") from None
    if not isinstance(data, dict) or set(data) != {"grant", "signature"}:
        raise WorkflowError("A read grant has exactly grant and signature", code="INVALID_REQUEST")
    check_read_grant(data["grant"])
    signer = recover_digest(read_grant_digest(data["grant"]), data["signature"])
    return data["grant"], data["signature"], signer


class ReadGrants:
    """The agent key's read grant for the recorded Agent accounts, for one command.

    Minted when a run read first needs it, valid READ_GRANT_LIFETIME_S, and kept in the state directory
    (owner-only) so the next command reuses it until READ_GRANT_REFRESH_AT of its life has passed. Best
    effort: when none can be made (no agent key or owner, REYA_SIGNER=own-key, no Agent account recorded,
    an unusable state directory) the reads go without one. The grant never appears in a result or an error.
    """

    def __init__(self, now: Callable[[], float] = time.time):
        self.now = now
        self._token: str | None = None
        self._tried = False

    def header(self) -> str | None:
        """`Bearer <grant>` for the Authorization header, or None."""
        if not self._tried:
            self._tried = True
            try:
                self._token = self._current()
            except (WorkflowError, ValueError, OSError, KeyError, TypeError):
                self._token = None
        return f"Bearer {self._token}" if self._token else None

    def discard(self) -> None:
        """Forget the kept grant (the service did not take it), so the next command signs a new one."""
        try:
            (state_directory() / READ_GRANT_FILE).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def _current(self) -> str | None:
        key, address = agent_signer()
        owner = os.environ.get("PERP_WALLET_ADDRESS_1", "")
        accounts = _env.agent_account_ids()
        if not ADDRESS_RE.fullmatch(owner) or not accounts:
            return None
        environment, chain = kit_environment()
        now = int(self.now())
        kept = self._kept(now, address, chain, environment, owner, accounts)
        if kept:
            return kept
        grant = read_grant(
            chain_id=chain,
            environment=environment,
            owner=owner,
            account_ids=accounts,
            expires_at=now + READ_GRANT_LIFETIME_S,
            salt=os.urandom(16),
        )
        token = read_grant_token(grant, sign_read_grant(grant, key))
        if parse_read_grant_token(token)[2] != address:
            raise WorkflowError("The signed read grant does not verify")
        self._keep(token)
        return token

    def _kept(self, now: int, address: str, chain: int, environment: str, owner: str, accounts: set) -> str | None:
        """The kept grant, if it is this agent key's for this deployment, owner and accounts, with life left."""
        path = state_directory() / READ_GRANT_FILE
        if not path.exists():
            return None
        try:
            with os.fdopen(_open_private(path, os.O_RDONLY), "r") as stream:
                token = json.load(stream).get("token")
            grant, _, signer = parse_read_grant_token(token)
        except (WorkflowError, ValueError, OSError, AttributeError):
            return None
        left = int(grant["expiresAt"]) - now
        if (
            signer != address
            or grant["verifyingChainId"] != str(chain)
            or grant["environment"] != environment
            or grant["owner"].lower() != owner.lower()
            or grant["accountIds"] != [str(account) for account in sorted(accounts)]
            or not READ_GRANT_LIFETIME_S * (1 - READ_GRANT_REFRESH_AT) < left <= READ_GRANT_LIFETIME_S
        ):
            return None
        return token

    def _keep(self, token: str) -> None:
        try:
            fd = _open_private(state_directory() / READ_GRANT_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
            with os.fdopen(fd, "w") as stream:
                json.dump({"token": token}, stream)
        except (OSError, ValueError):
            pass  # keeping it saves a signature next time; it is never required


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowError(f"The document repeats the key {key!r}")
        result[key] = value
    return result


def _no_floats(text: str) -> Any:
    raise WorkflowError(f"The document has a JSON number with a fraction ({text}); decimals are strings")


def parse_document(text: str) -> dict:
    if len(text.encode()) > MAX_DOCUMENT_BYTES:
        raise WorkflowError(f"The document is over {MAX_DOCUMENT_BYTES} bytes")
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_float=_no_floats)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"The document is not JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from None
    if not isinstance(doc, dict):
        raise WorkflowError("The document must be a JSON object")
    return doc


def load_document(path: str) -> tuple[str, dict]:
    try:
        with open(path, "rb") as source:
            raw = source.read(MAX_DOCUMENT_BYTES + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise WorkflowError(f"Cannot read the document {path}") from None
    return text, parse_document(text)


def check_schema(doc: dict) -> None:
    """Only `reya.workflow/2`: an older document is refused before anything is sent."""
    schema = doc.get("schema")
    if schema == SCHEMA_TAG:
        return
    if schema == "reya.workflow/1":
        raise WorkflowError(
            "This is an older single-market strategy document (reya.workflow/1), which the service no longer runs. "
            "Draft it again with strategy draft."
        )
    raise WorkflowError(f"Not a strategy document: its schema is {schema!r}, not {SCHEMA_TAG}")


def envelope(doc: dict) -> dict:
    value = doc.get("envelope")
    if not isinstance(value, dict):
        raise WorkflowError("The document has no envelope")
    return value


def document_account(doc: dict) -> int:
    value = envelope(doc).get("account_id")
    if not isinstance(value, str) or not UINT_RE.fullmatch(value) or value == "0":
        raise WorkflowError("The document's envelope.account_id must be a decimal string")
    return int(value)


def document_markets(doc: dict) -> list[str]:
    markets = envelope(doc).get("markets")
    return [m for m in markets if isinstance(m, str)] if isinstance(markets, list) else []


def canonical_json(doc: Any) -> str:
    """RFC 8785 for strategy documents: ASCII keys sorted, no whitespace, integers and strings only."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def version_hash(canonical: str) -> str:
    return "0x" + keccak(text=canonical).hex()


# Paths the canonical form may add with the format's defaults when the author left them out. Ids of signals,
# actions and states, and list positions, read as "*".
DEFAULTS = {
    ("envelope", "price_bound_bps"): DEFAULT_BOUND_BPS,
    ("envelope", "max_resting_orders"): DEFAULT_MAX_RESTING,
    ("states", "*", "on"): [],
    ("states", "*", "on", "*", "if"): [],
    ("actions", "*", "keep"): False,
    ("actions", "*", "orders", "*", "post_only"): True,
}
ID_MAPS = ("signals", "actions", "states")


def _pattern(path: tuple) -> tuple:
    return tuple(
        "*" if part.isdecimal() or (index == 1 and path[0] in ID_MAPS) else part for index, part in enumerate(path)
    )


def _covers(mine: Any, theirs: Any, path: tuple) -> str | None:
    """Where `theirs` (the service's canonical form) differs from what the author wrote, if anywhere."""
    where = ".".join(path) or "document"
    if isinstance(mine, dict):
        if not isinstance(theirs, dict):
            return where
        for key, value in mine.items():
            if key not in theirs:
                if value is None:
                    continue  # an absent optional, spelled null
                return ".".join(path + (key,))
            found = _covers(value, theirs[key], path + (key,))
            if found:
                return found
        for key in theirs.keys() - mine.keys():
            pattern = _pattern(path + (key,))
            if theirs[key] is not None and (pattern not in DEFAULTS or theirs[key] != DEFAULTS[pattern]):
                return ".".join(path + (key,))
        return None
    if isinstance(mine, list):
        if not isinstance(theirs, list) or len(mine) != len(theirs):
            return where
        for index, (a, b) in enumerate(zip(mine, theirs)):
            found = _covers(a, b, path + (str(index),))
            if found:
                return found
        return None
    return None if type(mine) is type(theirs) and mine == theirs else where


def check_canonical(doc: dict | None, canonical: Any, claimed_hash: Any) -> dict:
    """Accept the service's canonical form only if it is this document with defaults filled in.

    The agent key signs the canonical form's hash, so a canonical form that changed anything the
    author wrote is refused, and so is a hash that is not keccak256 of the canonical text. With no
    document (a draft, which the service expanded from template parameters), the form and its hash
    are checked here and the expansion by check_expansion.
    """
    if not isinstance(canonical, str) or not isinstance(claimed_hash, str):
        raise WorkflowError("The service did not return a canonical form and version hash")
    parsed = parse_document(canonical)
    if canonical_json(parsed) != canonical:
        raise WorkflowError("The service's canonical form is not canonical JSON; nothing was signed")
    if version_hash(canonical) != claimed_hash.lower():
        raise WorkflowError("The service's version hash is not the hash of its canonical form; nothing was signed")
    check_schema(parsed)
    differs = _covers(doc, parsed, ()) if doc is not None else None
    if differs:
        raise WorkflowError(f"The service's canonical form changes {differs}; nothing was signed")
    return parsed


def canonical_amount(value: Decimal, *, places: int, signed: bool = False, positive: bool = True) -> str:
    if not value.is_finite():
        raise WorkflowError("amounts must be finite")
    if positive and value <= 0:
        raise WorkflowError(f"{value} must be positive")
    if not signed and value < 0:
        raise WorkflowError(f"{value} must not be negative")
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > places:
        raise WorkflowError(f"{value} has more than {places} decimal places")
    text = decimal_text(abs(value))
    return ("-" + text) if value < 0 else text


# --------------------------------------------------------------------------
# Drafting: the `basket` template's parameters, from the draft command's flags
# --------------------------------------------------------------------------

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = dict(zip(DAYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")))
TIME_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
DURATION_RE = re.compile(r"([1-9][0-9]{0,5})([mhdw])")
DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
OFFSET_RE = re.compile(r"([+-]?[0-9]{1,4})bps")
MAX_LIFETIME_S = 90 * 86400
MIN_PERIOD_S, MAX_PERIOD_S = 300, MAX_LIFETIME_S
MIN_ELAPSED_S, MAX_ELAPSED_S = 60, MAX_LIFETIME_S
ON_STOP = {"keep-others": "keep_others", "close-all": "close_all"}
MAX_OFFSET_BPS = 5000
REPEATS = {"once": "once", "each": "each", "each_crossing": "each"}


def market_symbol(text: str, flag: str) -> str:
    """`eth` or `ETHRUSDPERP` → `ETHRUSDPERP`, as every other kit command reads a market."""
    from agent_kit.scripts._common import symbol_arg

    symbol = symbol_arg(text)
    if not SYMBOL_RE.fullmatch(symbol):
        raise WorkflowError(f"{flag}: not a market: {text!r}")
    return symbol


def _number(text: str, flag: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise WorkflowError(f"{flag}: not a number: {text!r}") from None
    if not value.is_finite():
        raise WorkflowError(f"{flag}: not a number: {text!r}")
    return value


def _amount(text: str, flag: str, *, places: int, signed: bool = False) -> str:
    try:
        return canonical_amount(_number(text, flag), places=places, signed=signed, positive=not signed)
    except WorkflowError as exc:
        raise WorkflowError(f"{flag}: {exc}") from None


def _fields(spec: str, flag: str, shape: str, low: int, high: int) -> list[str]:
    parts = spec.split(":")
    if not low <= len(parts) <= high or not all(part.strip() for part in parts):
        raise WorkflowError(f"{flag} takes {shape}; got {spec!r}")
    return [part.strip() for part in parts]


def _choice(value: str, choices: tuple, flag: str, what: str) -> str:
    word = value.lower()
    if word not in choices:
        raise WorkflowError(f"{flag}: the {what} is {' or '.join(choices)}, not {value!r}")
    return word


def duration_secs(text: str, flag: str, low: int, high: int) -> int:
    """`90m`, `4h`, `3d` or `1w`, within [low, high] seconds."""
    match = DURATION_RE.fullmatch(text.strip().lower())
    if not match:
        raise WorkflowError(f"{flag}: a duration is a whole number of m, h, d or w (90m, 4h, 3d, 1w); got {text!r}")
    secs = int(match.group(1)) * DURATION_UNITS[match.group(2)]
    if not low <= secs <= high:
        raise WorkflowError(f"{flag}: {text} is outside {duration_text(low)} to {duration_text(high)}")
    return secs


def utc_midnight(now: int) -> int:
    return now - now % 86400


def parse_anchor(text: str, now: int) -> int:
    """`HH:MM` (UTC, today), Unix seconds, or an ISO time with a zone."""
    text = text.strip()
    if TIME_RE.fullmatch(text):
        hours, minutes = text.split(":")
        return utc_midnight(now) + int(hours) * 3600 + int(minutes) * 60
    if text.isascii() and text.isdecimal():
        return int(text)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise WorkflowError(
            f"--trigger every: the anchor is HH:MM (UTC), Unix seconds or an ISO time; got {text!r}"
        ) from None
    if moment.tzinfo is None:
        raise WorkflowError("--trigger every: an ISO anchor needs a time zone, e.g. 2026-10-26T09:00:00Z")
    return int(moment.timestamp())


TRIGGER_SHAPE = "price:SYMBOL:up|down:LEVEL, every:PERIOD[@ANCHOR], at:daily@HH:MM, at:mon..sun@HH:MM or start"


def parse_trigger(spec: str, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    kind, _, rest = spec.strip().partition(":")
    kind = kind.lower()
    if kind == "start" and not rest:
        return {"kind": "start"}
    if kind == "price":
        market, direction, level = _fields(rest, "--trigger price", "price:SYMBOL:up|down:LEVEL", 3, 3)
        return {
            "kind": "price",
            "market": market_symbol(market, "--trigger"),
            "direction": _choice(direction, ("up", "down"), "--trigger", "direction"),
            "level": _amount(level, "--trigger level", places=9),
        }
    if kind == "every" and rest:
        period, _, anchor = rest.partition("@")
        secs = duration_secs(period, "--trigger every", MIN_PERIOD_S, MAX_PERIOD_S)
        return {
            "kind": "every",
            "period_secs": secs,
            "anchor": parse_anchor(anchor, now) if anchor else utc_midnight(now),
        }
    if kind == "at" and "@" in rest:
        when, _, at = rest.partition("@")
        if not TIME_RE.fullmatch(at.strip()):
            raise WorkflowError(f"--trigger at: the time is HH:MM in UTC, e.g. 09:30; got {at!r}")
        when = when.strip().lower()
        if when == "daily":
            return {"kind": "at", "cadence": "daily", "time": at.strip()}
        if when in DAYS:
            return {"kind": "at", "cadence": "weekly", "day": when, "time": at.strip()}
        raise WorkflowError(f"--trigger at: daily or a day of the week (mon … sun), not {when!r}")
    raise WorkflowError(f"--trigger takes {TRIGGER_SHAPE}; got {spec!r}")


def parse_size(text: str, flag: str) -> dict:
    """`500` (USD) or `25%` (a weight of --total)."""
    if text.endswith("%"):
        percent = _number(text[:-1], flag)
        if not 0 < percent <= 100:
            raise WorkflowError(f"{flag}: a weight is above 0% and at most 100%; got {text!r}")
        return {"weight": _amount(decimal_text(percent / 100), flag, places=18)}
    return {"usd": _amount(text, flag, places=18)}


def _with_protection(spec: str, flag: str, shape: str, required: int) -> tuple[list[str], dict]:
    """The order's own fields, then its optional `:STOP[:TAKE_PROFIT]`: a take-profit without a stop leaves the stop
    empty (`eth:buy:1000::3200`)."""
    parts = [part.strip() for part in spec.split(":")]
    head, tail = parts[:required], parts[required:]
    if len(head) < required or not all(head) or len(tail) > 2 or (tail and not tail[-1]):
        raise WorkflowError(f"{flag} takes {shape}; got {spec!r}")
    protection = {}
    if tail and tail[0]:
        protection["stop"] = _amount(tail[0], f"{flag} stop", places=9)
    if len(tail) == 2:
        protection["take_profit"] = _amount(tail[1], f"{flag} take-profit", places=9)
    return head, protection


def parse_order(spec: str) -> dict:
    """`--order SYMBOL:buy|sell:USD|WEIGHT%[:STOP[:TAKE_PROFIT]]`: an immediate (IOC) order."""
    shape = "SYMBOL:buy|sell:USD|WEIGHT%[:STOP[:TAKE_PROFIT]]"
    (market, side, size), protection = _with_protection(spec, "--order", shape, 3)
    return {
        "type": "immediate",
        "market": market_symbol(market, "--order"),
        "side": _choice(side, ("buy", "sell"), "--order", "side"),
        "size": parse_size(size, "--order"),
    } | protection


def parse_resting(spec: str, post_only: bool) -> dict:
    """`--resting SYMBOL:buy|sell:USD|WEIGHT%:PRICE|±OFFSETbps[:STOP[:TAKE_PROFIT]]`: a resting limit order."""
    shape = "SYMBOL:buy|sell:USD|WEIGHT%:PRICE|±OFFSETbps[:STOP[:TAKE_PROFIT]]"
    (market, side, size, price), protection = _with_protection(spec, "--resting", shape, 4)
    offset = OFFSET_RE.fullmatch(price.lower())
    if offset:
        bps = int(offset.group(1))
        if not -MAX_OFFSET_BPS <= bps <= MAX_OFFSET_BPS or bps == 0:
            raise WorkflowError(f"--resting: an offset is -5000bps to 5000bps from the mark, not 0; got {price!r}")
        limit: dict = {"offset_bps": bps}
    else:
        limit = {"fixed": _amount(price, "--resting price", places=9)}
    return (
        {
            "type": "resting",
            "market": market_symbol(market, "--resting"),
            "side": _choice(side, ("buy", "sell"), "--resting", "side"),
            "size": parse_size(size, "--resting"),
            "price": limit,
        }
        | ({} if post_only else {"post_only": False})
        | protection
    )


def parse_condition(spec: str) -> dict:
    """`funding:SYMBOL:above|below:PCT_PER_HOUR`, `position:SYMBOL:above|below:QTY`, or `balance:above|below:USD`
    (the account's available balance; only where the service reads it, see ServiceInfo.reads_balance)."""
    shape = "funding:SYMBOL:above|below:PCT_PER_HOUR, position:SYMBOL:above|below:QTY or balance:above|below:USD"
    if spec.strip().lower().startswith("balance:"):
        _, op, level = _fields(spec, "--if", shape, 3, 3)
        return {
            "kind": "available_balance",
            "op": _choice(op, ("above", "below"), "--if", "comparison"),
            "level": _amount(level, "--if level", places=18, signed=True),
        }
    kind, market, op, level = _fields(spec, "--if", shape, 4, 4)
    kind = kind.lower()
    if kind not in ("funding", "position"):
        raise WorkflowError(f"--if takes {shape}; got {spec!r}")
    return {
        "kind": "funding_avg" if kind == "funding" else "position",
        "market": market_symbol(market, "--if"),
        "op": _choice(op, ("above", "below"), "--if", "comparison"),
        "level": _amount(level, "--if level", places=9 if kind == "funding" else 18, signed=True),
    }


def parse_exits(specs: list[str]) -> dict | None:
    """`--exit target:SYMBOL:up|down:LEVEL`, `profit:PCT` and `after:DURATION`, at most one of each."""
    if not specs:
        return None
    exits: dict[str, Any] = {}
    for spec in specs:
        kind, _, rest = spec.strip().partition(":")
        kind = kind.lower()
        key = {"target": "target", "profit": "profit_pct", "after": "after_secs"}.get(kind)
        if key is None or not rest:
            raise WorkflowError(f"--exit takes target:SYMBOL:up|down:LEVEL, profit:PCT or after:DURATION; got {spec!r}")
        if key in exits:
            raise WorkflowError(f"--exit {kind} is given twice; give each kind of exit at most once")
        if kind == "target":
            market, direction, level = _fields(rest, "--exit target", "target:SYMBOL:up|down:LEVEL", 3, 3)
            exits[key] = {
                "market": market_symbol(market, "--exit"),
                "direction": _choice(direction, ("up", "down"), "--exit", "direction"),
                "level": _amount(level, "--exit level", places=9),
            }
        elif kind == "profit":
            exits[key] = _amount(rest.removesuffix("%"), "--exit profit", places=9)
        else:
            exits[key] = duration_secs(rest, "--exit after", MIN_ELAPSED_S, MAX_ELAPSED_S)
    return exits


def basket_params(
    *,
    environment: str,
    account_id: int,
    allocation: Decimal,
    expires_at: int,
    bound_bps: int,
    max_resting: int,
    trigger: dict,
    conditions: list[dict],
    orders: list[dict],
    total: Decimal | None,
    exits: dict | None,
    repeat: str,
    on_stop: str | None = None,
) -> dict:
    """The `basket` template's parameters, each optional one left out when absent (the contract's preferred
    spelling). The service expands them into the document and checks them; the kit checks only what it can say
    more plainly."""
    if not orders:
        raise WorkflowError("A strategy needs at least one --order or --resting order")
    weighted = [o for o in orders if "weight" in o["size"]]
    if weighted and total is None:
        raise WorkflowError("Weights (25%) are shares of --total: pass --total USD, or give each order in USD")
    if weighted and (len(weighted) != len(orders) or len({o["type"] for o in orders}) > 1):
        raise WorkflowError(
            "A weighted entry is all --order or all --resting orders, every one a weight of --total: give the others "
            "in USD and leave out --total, or weight only one kind"
        )
    if total is not None and not weighted:
        raise WorkflowError("--total goes with weights (an order size like 25%); with USD sizes leave it out")
    if repeat == "each" and exits:
        raise WorkflowError("--repeat each takes no --exit: a strategy with an exit enters once and exits once")
    if repeat == "each" and trigger["kind"] == "start":
        raise WorkflowError("--repeat each needs a price or schedule trigger: start happens once")
    if not MIN_BOUND_BPS <= bound_bps <= MAX_BOUND_BPS:
        raise WorkflowError(f"The price bound is {MIN_BOUND_BPS} to {MAX_BOUND_BPS} bps")
    entry: dict[str, Any] = {"orders": orders}
    if total is not None:
        entry = {"total_usd": canonical_amount(total, places=18)} | entry
    params: dict[str, Any] = {
        "environment": environment,
        "account_id": str(account_id),
        "allocation_usd": canonical_amount(allocation, places=18),
        "expires_at": expires_at,
        "price_bound_bps": bound_bps,
        "max_resting_orders": max_resting,
        "trigger": trigger,
    }
    if conditions:
        params["if"] = conditions
    params["entry"] = entry
    if exits:
        params["exit"] = exits
    params["repeat"] = repeat
    if on_stop is not None:
        params["on_stop"] = on_stop
    return params


def default_on_stop(orders: list[dict]) -> str:
    """What a fired stop does when the draft does not say: `close_all` for an entry that both buys and sells (legs
    that hedge each other), `keep_others` for one whose orders all point one way (separate bets)."""
    sides = {order["side"] for order in orders}
    return "close_all" if sides == {"buy", "sell"} else "keep_others"


def target_take_profit(params: dict, order: dict) -> str | None:
    """The take-profit the template gives `order` from the exit target: the target level, when the target watches
    the order's market, no other entry order trades it, and the target is on the order's winning side (up for a
    buy, down for a sell)."""
    target = (params.get("exit") or {}).get("target")
    if not target or target["market"] != order["market"]:
        return None
    on_market = sum(1 for other in params["entry"]["orders"] if other["market"] == order["market"])
    winning = (order["side"], target["direction"]) in (("buy", "up"), ("sell", "down"))
    return target["level"] if on_market == 1 and winning else None


def template_request(params: dict) -> dict:
    return {"template": dict(TEMPLATE), "params": params}


def check_expansion(params: dict, doc: dict) -> None:
    """The service's expansion of `params` keeps what the user asked for: the envelope, and every order's
    market, side, size, price, stop and take-profit under the ids the template gives them (`o1`… immediate, `r1`…
    resting, in the order given), with the take-profit the exit target gives and what a fired stop does (`on_stop`)
    as the template derives them. The draft is refused, and nothing is written, if it does not."""

    def refuse(where: str) -> WorkflowError:
        return WorkflowError(f"The service's document changes {where} from what was asked; nothing was written")

    if doc.get("template") != TEMPLATE:
        raise refuse("the template")
    env = envelope(doc)
    expect = {key: params[key] for key in ("environment", "account_id", "allocation_usd", "expires_at")}
    expect["price_bound_bps"] = params.get("price_bound_bps", DEFAULT_BOUND_BPS)
    expect["max_resting_orders"] = params.get("max_resting_orders", DEFAULT_MAX_RESTING)
    expect["markets"] = sorted({order["market"] for order in params["entry"]["orders"]})
    for key, value in expect.items():
        if env.get(key) != value:
            raise refuse(f"envelope.{key}")
    placed: dict[str, tuple[dict, dict]] = {}
    for action in (doc.get("actions") or {}).values():
        if isinstance(action, dict) and action.get("kind") in ("orders", "limit_orders"):
            for order in action.get("orders") or []:
                if isinstance(order, dict):
                    placed[str(order.get("id"))] = (action, order)
    asked = params["entry"]["orders"]
    immediate = [o for o in asked if o["type"] == "immediate"]
    resting = [o for o in asked if o["type"] == "resting"]
    ids = [f"o{i}" for i in range(1, len(immediate) + 1)] + [f"r{i}" for i in range(1, len(resting) + 1)]
    if sorted(placed) != sorted(ids):
        raise refuse("the orders")
    total = params["entry"].get("total_usd")
    on_stop = params.get("on_stop") or default_on_stop(asked)
    for order_id, order in zip(ids, immediate + resting):
        action, found = placed[order_id]
        kind = "orders" if order["type"] == "immediate" else "limit_orders"
        fields = ("market", "side", "size", "stop") + (("price",) if kind == "limit_orders" else ())
        if action.get("kind") != kind or any(found.get(f) != order.get(f) for f in fields):
            raise refuse(f"order {order_id}")
        if found.get("take_profit") != (order.get("take_profit") or target_take_profit(params, order)):
            raise refuse(f"order {order_id}'s take-profit")
        if action.get("on_stop") != on_stop:
            raise refuse(f"what a fired stop does (order {order_id}'s on_stop)")
        if kind == "limit_orders" and found.get("post_only", True) != order.get("post_only", True):
            raise refuse(f"order {order_id}")
        if "weight" in order["size"] and action.get("total_usd") != total:
            raise refuse(f"order {order_id}'s total")
    trigger = params["trigger"]
    if trigger["kind"] == "price":
        signals = doc.get("signals") or {}
        crossings = [
            s
            for s in signals.values()
            if isinstance(s, dict)
            and s.get("kind") == "crosses"
            and s.get("direction") == trigger["direction"]
            and s.get("level") == trigger["level"]
            and (signals.get(s.get("input")) or {}).get("market") == trigger["market"]
        ]
        if not crossings:
            raise refuse("the trigger")
    if params.get("exit") and not any(
        isinstance(a, dict) and a.get("kind") == "close_own" for a in (doc.get("actions") or {}).values()
    ):
        raise refuse("the exit")


def parse_expiry(text: str, now: int | None = None) -> int:
    """`7d`, `12h`, `90m`, Unix seconds, or an ISO 8601 time with a zone."""
    now = int(time.time()) if now is None else now
    match = re.fullmatch(r"([1-9][0-9]{0,4})([mhd])", text.strip())
    if match:
        return now + int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
    if text.isascii() and text.isdecimal():
        return int(text)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise WorkflowError(f"--expires takes 7d, 12h, 90m, Unix seconds or an ISO time; got {text!r}") from None
    if moment.tzinfo is None:
        raise WorkflowError("An ISO expiry needs a time zone, e.g. 2026-10-26T00:00:00Z")
    return int(moment.timestamp())


def drafts_directory() -> Path:
    from agent_kit.scripts._writer import state_directory

    path = state_directory() / "workflows"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


# --------------------------------------------------------------------------
# The document in plain sentences
# --------------------------------------------------------------------------


def base_asset(market: str) -> str:
    return market.removesuffix("RUSDPERP") or market


def assets(markets: list[str]) -> str:
    names = [base_asset(str(m)) for m in markets]
    return ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else (names[0] if names else "no market")


def duration_text(secs: Any, *, every: bool = False) -> str:
    """`3 days`, `90 minutes`; with `every`, one unit reads `day` ("every day", not "every 1 day")."""
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        return str(secs)
    for unit, name in ((7 * 86400, "week"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= unit and secs % unit == 0:
            count = secs // unit
            if count == 1:
                return name if every else f"1 {name}"
            return f"{count} {name}s"
    return f"{secs} seconds"


def _signals(doc: dict) -> dict:
    value = doc.get("signals")
    return value if isinstance(value, dict) else {}


def _actions(doc: dict) -> dict:
    value = doc.get("actions")
    return value if isinstance(value, dict) else {}


def value_text(doc: dict, signal_id: Any) -> str:
    """A value signal as the user reads it: "the ETH mark"."""
    signal = _signals(doc).get(signal_id)
    if not isinstance(signal, dict):
        return str(signal_id)
    kind, market = signal.get("kind"), base_asset(str(signal.get("market") or ""))
    texts = {
        "mark": f"the {market} mark",
        "funding_avg": f"the 15-minute average {market} funding",
        "position": f"the {market} position",
        "available_balance": "the account's available balance",
        "own_entry": f"this strategy's {market} entry price",
        # A price move of the strategy's own positions, unleveraged, before fees and funding: never "profit" alone.
        "own_profit_pct": (
            f"this strategy's price move on {market}"
            if signal.get("market")
            else "this strategy's price move over all its markets"
        ),
    }
    return texts.get(str(kind), f"{signal_id} ({kind})")


def level_text(doc: dict, signal_id: Any, level: Any) -> str:
    """A level in its input's unit: 3,000 for a price, 0.001%/h for funding, 0.5 ETH for a position."""
    signal = _signals(doc).get(signal_id) or {}
    kind = signal.get("kind") if isinstance(signal, dict) else None
    if kind == "funding_avg":
        return f"{level}%/h"
    if kind == "position":
        return f"{level} {base_asset(str(signal.get('market') or ''))}"
    if kind == "available_balance":
        return usd_text(level)
    if kind == "own_profit_pct":
        return f"{'' if str(level).startswith('-') else '+'}{level}%"
    return group(level)


def event_text(doc: dict, signal_id: Any, state: Any = None) -> str:
    """When an event fires, as the start of a sentence: "When the ETH mark crosses up through 3,000"."""
    signal = _signals(doc).get(signal_id)
    if not isinstance(signal, dict):
        return f"When {signal_id} fires"
    kind = signal.get("kind")
    if kind == "crosses":
        source = signal.get("input")
        if (_signals(doc).get(source) or {}).get("kind") == "own_profit_pct":
            verb = "rises" if signal.get("direction") == "up" else "falls"
            return (
                f"When {value_text(doc, source)} {verb} to {level_text(doc, source, signal.get('level'))} "
                "(unleveraged, before fees and funding)"
            )
        return (
            f"When {value_text(doc, source)} crosses {signal.get('direction')} through "
            f"{level_text(doc, source, signal.get('level'))}"
        )
    if kind == "every":
        return f"Every {duration_text(signal.get('period_secs'), every=True)}, counted from {iso(signal.get('anchor'))}"
    if kind == "at":
        day = "day" if signal.get("cadence") == "daily" else DAY_NAMES.get(str(signal.get("day")), signal.get("day"))
        return f"Every {day} at {signal.get('time')} UTC"
    if kind == "start":
        return "Right after the start"
    if kind == "elapsed":
        return f"{duration_text(signal.get('after_secs'))} after it enters {state or 'this state'}".capitalize()
    if kind == "filled":
        return f"When resting order {signal.get('order')} is completely filled"
    return f"When {signal_id} ({kind}) fires"


def condition_text(doc: dict, signal_id: Any) -> str:
    signal = _signals(doc).get(signal_id)
    if not isinstance(signal, dict) or signal.get("kind") not in ("above", "below"):
        return str(signal_id)
    source = signal.get("input")
    return f"{value_text(doc, source)} is {signal.get('kind')} {level_text(doc, source, signal.get('level'))}"


def size_usd(size: Any, total: Any) -> Decimal | None:
    """An order's dollars: its USD size, or its weight of the action's total."""
    try:
        if isinstance(size, dict) and "usd" in size:
            return Decimal(decimal_value(size["usd"]))
        if isinstance(size, dict) and "weight" in size:
            return Decimal(decimal_value(size["weight"])) * Decimal(decimal_value(total))
    except (WorkflowError, InvalidOperation):
        return None
    return None


def size_text(size: Any, total: Any, market: str) -> str:
    """`$500.00 of BTC`, or for a weight `$60.00 of BTC (60% of $100.00)`."""
    usd = size_usd(size, total)
    amount = f"{usd_text(usd) if usd is not None else size} of {market}"
    if isinstance(size, dict) and "weight" in size and usd is not None:
        return f"{amount} ({decimal_text(Decimal(str(size['weight'])) * 100)}% of {usd_text(total)})"
    return amount


def price_text(price: Any, side: Any) -> str:
    if isinstance(price, dict) and "fixed" in price:
        return f"at {group(price['fixed'])}"
    if isinstance(price, dict) and "offset_bps" in price:
        try:
            bps = int(price["offset_bps"])
        except (TypeError, ValueError):
            return str(price)
        return f"{abs(bps)} bps {'below' if bps < 0 else 'above'} the mark when placed"
    return str(price)


def order_line(order: dict, action: dict, bound_bps: Any) -> str:
    """One order of the document: "sell $1,000.00 of ETH at the mark within 25 bps, stop at 3,300"."""
    size = size_text(order.get("size"), action.get("total_usd"), base_asset(str(order.get("market", "?"))))
    if action.get("kind") == "limit_orders":
        text = f"{order.get('side')} {size} resting {price_text(order.get('price'), order.get('side'))}"
        text += ", post-only" if order.get("post_only", True) else ""
    else:
        text = f"{order.get('side')} {size} at the mark within {bound_bps} bps"
    if order.get("stop"):
        text += f", stop at {group(order['stop'])}"
    if order.get("take_profit"):
        text += f", take profit at {group(order['take_profit'])}"
    return text


ON_STOP_TEXTS = {
    "close_all": "If a stop-loss or take-profit fires, it closes its other legs and ends.",
    "keep_others": (
        "If a stop-loss or take-profit fires, its other legs keep running, and it ends once nothing is left."
    ),
}


def on_stop_text(doc: dict) -> str | None:
    """What a fired stop-loss or take-profit does, when any order carries one: the actions' `on_stop`."""
    actions = [a for a in _actions(doc).values() if isinstance(a, dict) and a.get("kind") in ("orders", "limit_orders")]
    protected = any(
        o.get("stop") or o.get("take_profit") for a in actions for o in a.get("orders") or [] if isinstance(o, dict)
    )
    choices = {a.get("on_stop") for a in actions}
    if not protected or len(choices) != 1:
        return None
    choice = str(next(iter(choices)))
    if len(document_markets(doc)) == 1 and choice in ON_STOP_TEXTS:
        # One market: there are no other legs to keep or close.
        return "If its stop-loss or take-profit fires, it ends once nothing is left to do."
    return ON_STOP_TEXTS.get(choice)


def action_text(doc: dict, action_id: Any) -> str:
    action = _actions(doc).get(action_id)
    if not isinstance(action, dict):
        return str(action_id)
    bound = envelope(doc).get("price_bound_bps", DEFAULT_BOUND_BPS) if isinstance(doc.get("envelope"), dict) else 25
    kind, orders = action.get("kind"), [o for o in action.get("orders") or [] if isinstance(o, dict)]
    lines = "; ".join(order_line(o, action, bound) for o in orders)
    if kind == "orders":
        return f"send {len(orders)} immediate order{'' if len(orders) == 1 else 's'} ({lines})"
    if kind == "limit_orders":
        kept = ", kept when it leaves this state" if action.get("keep") else ""
        return f"place {len(orders)} resting order{'' if len(orders) == 1 else 's'} ({lines}){kept}"
    if kind == "close_own":
        markets = action.get("markets")
        where = "all its markets" if markets == "all" else assets(markets if isinstance(markets, list) else [])
        return f"close this strategy's own share in {where}"
    return f"{action_id} ({kind})"


def _states(doc: dict) -> dict:
    value = doc.get("states")
    return value if isinstance(value, dict) else {}


def is_final(doc: dict, state: Any) -> bool:
    entry = _states(doc).get(state)
    return isinstance(entry, dict) and not entry.get("on")


def transition_text(doc: dict, state: Any, transition: dict) -> str:
    """One transition as one sentence."""
    text = event_text(doc, transition.get("when"), state)
    conditions = [condition_text(doc, c) for c in transition.get("if") or []]
    if conditions:
        text += ", if " + " and ".join(conditions)
    steps = [action_text(doc, a) for a in transition.get("do") or []]
    text += ": " + ("; then ".join(steps) if steps else "nothing is sent")
    go = transition.get("go")
    if go == state:
        text += ", and it keeps watching for the next time"
    elif is_final(doc, go):
        text += f", then it moves to {go}, where it ends"
    else:
        text += f", then it moves to {go}"
    return text + "."


def state_order(doc: dict) -> list:
    """The states the strategy can reach, in the order it reaches them: the start, then each transition's target
    in turn. A state nothing leads to is left out: it never happens."""
    states = _states(doc)
    order: list = []
    queue = [doc.get("start")]
    while queue:
        state = queue.pop(0)
        if state in order or state not in states:
            continue
        order.append(state)
        entry = states[state]
        if isinstance(entry, dict):
            queue += [t.get("go") for t in entry.get("on") or [] if isinstance(t, dict)]
    return order


def rules(doc: dict) -> list[str]:
    """The states as sentences, in the order the strategy reaches them: one per transition, one per final state."""
    states = _states(doc)
    start = doc.get("start")
    order = state_order(doc)
    sentences = []
    for state in order:
        entry = states.get(state) or {}
        transitions = [t for t in entry.get("on") or [] if isinstance(t, dict)] if isinstance(entry, dict) else []
        label = f"{state} (the start)" if state == start else state
        for transition in transitions:
            sentences.append(f"In {label}: {transition_text(doc, state, transition)}")
        if not transitions:
            sentences.append(f"{state} is final: the strategy ends there once none of its resting orders is left.")
    fired = on_stop_text(doc)
    return sentences + ([fired] if fired else [])


def order_rows(doc: dict) -> list[dict]:
    """A row per order the document can send, in document order."""
    rows = []
    bound = envelope(doc).get("price_bound_bps", DEFAULT_BOUND_BPS)
    for action_id, action in _actions(doc).items():
        if not isinstance(action, dict) or action.get("kind") not in ("orders", "limit_orders"):
            continue
        for order in action.get("orders") or []:
            if not isinstance(order, dict):
                continue
            usd = size_usd(order.get("size"), action.get("total_usd"))
            row = {
                "action": action_id,
                "order": order.get("id"),
                "type": "immediate" if action["kind"] == "orders" else "resting",
                "market": order.get("market"),
                "side": order.get("side"),
                "size": order.get("size"),
                "usd": decimal_text(usd) if usd is not None else None,
                "stop": order.get("stop"),
                "take_profit": order.get("take_profit"),
            }
            if action["kind"] == "limit_orders":
                row |= {
                    "price": order.get("price"),
                    "post_only": order.get("post_only", True),
                    "keep": action.get("keep", False),
                }
            row["text"] = order_line(order, action, bound)
            rows.append(row)
    return rows


def describe(doc: dict) -> str:
    """The document in plain sentences, from its own fields (no evaluation)."""
    env = envelope(doc)
    head = (
        f"May trade {assets(document_markets(doc))} with up to {usd_text(env.get('allocation_usd'))} in total"
        + (f" until {iso(env['expires_at'])}" if isinstance(env.get("expires_at"), int) else "")
        + f", each immediate order within {env.get('price_bound_bps', DEFAULT_BOUND_BPS)} bps of the mark."
    )
    return " ".join([head, *rules(doc)])


def document_view(doc: dict) -> dict:
    """What a draft would do, read from its own fields: the markets, the rules as sentences and a row per order."""
    env = envelope(doc)
    return {
        "workflow": describe(doc),
        "markets": document_markets(doc),
        "rules": rules(doc),
        "orders": order_rows(doc),
        "allocation_usd": env.get("allocation_usd"),
        "expires_at": iso(env.get("expires_at")),
        "price_bound_bps": env.get("price_bound_bps", DEFAULT_BOUND_BPS),
        "max_resting_orders": env.get("max_resting_orders", DEFAULT_MAX_RESTING),
    }


def order_caps(doc: dict) -> list[tuple[str, Decimal]]:
    """(order id, dollars) for every order the document can send, for the per-order cap."""
    return [(str(row["order"]), Decimal(row["usd"])) for row in order_rows(doc) if row["usd"] is not None]


# --------------------------------------------------------------------------
# Plain-text formatting
# --------------------------------------------------------------------------


def iso(seconds: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def group(value: Any) -> str:
    """A decimal string with thousands separators, exact: `90,012.5`."""
    text = str(value)
    if not re.fullmatch(r"-?[0-9]+(\.[0-9]+)?", text):
        return text
    sign, rest = ("-", text[1:]) if text.startswith("-") else ("", text)
    whole, _, frac = rest.partition(".")
    grouped = f"{int(whole):,}"
    return sign + grouped + (f".{frac}" if frac else "")


def usd_text(value: Any) -> str:
    """USD to the cent, rounded half up: `$1,234.57`."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    if not amount.is_finite():
        return str(value)
    cents = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents):,.2f}"


def bps_from(level: Any, price: Any) -> str:
    """How far `price` is from `level`, in basis points to one decimal, rounded half away from zero."""
    try:
        lvl, px = Decimal(str(level)), Decimal(str(price))
    except InvalidOperation:
        return "n/a"
    if lvl <= 0:
        return "n/a"
    tenths = (abs(px - lvl) * 100_000 / lvl).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    sign = "" if tenths == 0 else ("+" if px >= lvl else "-")
    return f"{sign}{tenths // 10}.{tenths % 10} bps"


def distance_text(bps: Any) -> str | None:
    if not isinstance(bps, str) or not UINT_RE.fullmatch(bps):
        return None
    percent = (Decimal(bps) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{percent}% ({bps} bps)"


def reason(table: dict[str, str], code: Any) -> str:
    return table.get(code, code) if isinstance(code, str) else "unknown"


def mode_flag(mode: str) -> str:
    """A `--mode` value with the app's name for it when the two differ: `shadow (Watch only)`."""
    name = MODE_NAMES.get(mode)
    return f"{mode} ({name})" if name and name.lower() != mode else mode


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def firing_text(firing: dict) -> str:
    """One order a transition sent, as the user reads it: "buy BTC: executed 0.0002 at 90,012 (+1.3 bps from the
    mark 90,000) for $18.00"."""
    market = base_asset(str(firing.get("market") or "?"))
    side, qty = firing.get("side") or "?", firing.get("qty")
    what = f"{side} {qty} {market}" if qty else f"{side} {market}"
    status = firing.get("order_status")
    exec_qty, avg, mark = firing.get("exec_qty"), firing.get("avg_fill_px"), firing.get("mark")
    bound = firing.get("bound_bps")
    filled_qty, average = _decimal(exec_qty), _decimal(avg)
    if status in ("filled", "partial") and filled_qty is not None and average is not None:
        verb = "executed" if status == "filled" else "partly executed"
        return (
            f"{side} {market}: {verb} {exec_qty} at {group(avg)} ({bps_from(mark, avg)} from the mark "
            f"{group(mark)}) for {usd_text(filled_qty * average)}"
        )
    if status == "shadow":
        return (
            f"{what} at the mark {group(mark)}: Watch only, recorded and not sent; the order would have been limited "
            f"to {group(firing.get('limit_px'))} ({bound} bps bound)"
        )
    if status == "past_bound":
        return f"{what}: not filled, the market had moved past the {bound} bps bound before the order went out"
    if status == "zero_fill":
        return f"{what}: not filled, nothing traded within the {bound} bps bound"
    if status == "rejected":
        return f"{what}: rejected by the exchange: {firing.get('cancel_reason') or 'no reason given'}"
    if status == "unknown":
        return (
            f"{what}: outcome unknown; the strategy is blocked, keeping its reservations and stops, until the "
            "order's outcome is known"
        )
    return f"{what} (mark {group(mark)}), order {status}: waiting for its outcome"


FIRING_FIELDS = (
    "transition", "action", "order", "market", "side", "qty", "order_status", "mark", "limit_px", "avg_fill_px",
    "exec_qty", "bound_bps", "settlement", "client_order_id",
)  # fmt: skip


def firing_view(firing: dict) -> dict:
    view = {key: firing.get(key) for key in FIRING_FIELDS}
    view["status"] = view.pop("order_status")
    view["text"] = firing_text(firing)
    return view


def resting_view(order: Any) -> dict:
    """A resting order with a sentence for where it is."""
    if not isinstance(order, dict):
        return {}
    view = {**order}
    for key in ("expires_at", "placed_at"):
        view[key] = iso(order.get(key))
    status = order.get("status")
    text = (
        f"{order.get('side')} {order.get('qty')} {base_asset(str(order.get('market') or '?'))} at "
        f"{group(order.get('limit_px'))} (order {order.get('order')}{', post-only' if order.get('post_only') else ''}): "
        f"{reason(RESTING_STATUS, status)}"
    )
    filled = _decimal(order.get("filled_qty"))
    if filled and status != "filled":
        text += f", {order.get('filled_qty')} filled"
    if order.get("reason_message") or order.get("reason_code"):
        text += f" ({order.get('reason_message') or order.get('reason_code')})"
    view["text"] = text
    return view


def transition_firing_view(event: dict) -> dict:
    """A `fired` event: one transition, the service's sentence for it, and a row per order it sent or placed."""
    return {
        "at": iso(event.get("at")),
        "transition": event.get("transition"),
        "text": event.get("text"),
        "orders": [firing_view(f) for f in event.get("firings") or [] if isinstance(f, dict)],
        "resting_orders": [resting_view(r) for r in event.get("resting_orders") or [] if isinstance(r, dict)],
    }


def is_skip(event: dict) -> bool:
    code = event.get("code")
    return event.get("kind") == "skipped" or (isinstance(code, str) and code in SKIP_REASONS)


def skip_view(event: dict) -> dict:
    return {
        "at": iso(event.get("at")),
        "code": event.get("code"),
        "reason": reason(SKIP_REASONS, event.get("code")),
        "transition": event.get("transition"),
        "market": event.get("market"),
        "text": event.get("text"),
    }


def event_view(event: dict) -> dict:
    view = {
        "seq": event.get("seq"),
        "at": iso(event.get("at")),
        "kind": event.get("kind"),
        "actor": event.get("actor"),
        "code": event.get("code"),
        "text": event.get("text"),
    }
    for key in ("transition", "market", "order", "protection_kind"):
        if event.get(key) is not None:
            view[key] = event[key]
    return view


def stop_view(stop: Any) -> dict | None:
    if not isinstance(stop, dict):
        return None
    return {
        "by": stop.get("by"),
        "reason": stop.get("reason"),
        "reason_text": reason(STOP_REASONS, stop.get("reason")),
        "signer": stop.get("signer"),
        "at": iso(stop.get("at")),
        "keep_position": stop.get("keep_position") is not False,
    }


def close_view(close: Any) -> dict | None:
    """A stop's close of the strategy's own share in one market, with a sentence for where it is."""
    if not isinstance(close, dict):
        return None
    view = {**close, "next_attempt_at": iso(close.get("next_attempt_at"))}
    state = close.get("state")
    attempts, most = close.get("attempts"), close.get("max_attempts")
    open_qty = str(close.get("open_qty") or "").lstrip("-")
    texts = {
        "pending": "waiting for the strategy's last orders and fills before closing its own share",
        "closing": f"closing its own share: attempt {attempts} of {most} made, {close.get('closed_qty')} closed so far",
        "closed": f"closed its own share ({close.get('closed_qty')} in all)",
        "nothing_to_close": "nothing of its own share was still open",
        "stopped_still_open": (
            f"{open_qty or 'part'} of its own share is still open after {attempts} close attempts: close it by hand"
        ),
        "unknown": "a close order's outcome is unknown, so the service stopped trying: check the position",
    }
    text = texts.get(str(state), str(state))
    if close.get("simulated"):
        text += " (simulated: nothing was sent)"
    view["text"] = f"{base_asset(str(close['market']))}: {text}" if close.get("market") else text
    return view


def completion_view(completion: Any) -> dict | None:
    if not isinstance(completion, dict):
        return None
    view = {
        "reason": completion.get("reason"),
        "reason_text": reason(COMPLETION_REASONS, completion.get("reason")),
        "at": iso(completion.get("at")),
    }
    if completion.get("market"):
        view["market"] = completion["market"]
    return view


def allocation_view(allocation: Any) -> dict:
    if not isinstance(allocation, dict):
        return {}
    view = dict(allocation)
    view["text"] = (
        f"{usd_text(allocation.get('remaining_usd'))} of {usd_text(allocation.get('total_usd'))} remaining "
        f"({usd_text(allocation.get('spent_usd'))} spent, {usd_text(allocation.get('reserved_usd'))} reserved)"
    )
    return view


def protection_text(protection: Any, what: str = "stop") -> str | None:
    """ "its stop at 3,300 is armed", or with `what` "take-profit" for the market's take-profit."""
    if not isinstance(protection, dict):
        return None
    return f"its {what} at {group(protection.get('trigger_px'))} is {protection.get('state')}"


def signed_pct(value: Any) -> str:
    """`+0.5%`, `-1.2%`: a price move always shows its sign."""
    text = str(value)
    return f"{'' if text.startswith('-') else '+'}{text}%"


def signed_usd(value: Any) -> str:
    """`+$12.30`, `-$4.00`."""
    text = usd_text(value)
    return text if text.startswith("-") else f"+{text}"


def price_move_text(pct: Any, usd: Any) -> str:
    """ "a +0.5% price move (+$12.30)": unleveraged, before fees and funding, never called profit alone."""
    move = f"a {signed_pct(pct)} price move"
    return f"{move} ({signed_usd(usd)})" if usd is not None else move


def market_view(row: Any) -> dict:
    """One market of a strategy: its own share, entry and price move, the account's position, its stop-loss and
    take-profit, and its close."""
    if not isinstance(row, dict):
        return {}
    view = {**row, "close": close_view(row.get("close"))}
    asset = base_asset(str(row.get("market") or "?"))
    parts = [f"{asset}: this strategy holds {row.get('own_qty') or '0'} {asset}"]
    if row.get("own_entry") is not None:
        parts[0] += f" from an average entry of {group(row['own_entry'])}"
    if row.get("own_profit_pct") is not None:
        parts.append(
            f"{price_move_text(row['own_profit_pct'], row.get('own_profit_usd'))} at the mark, unleveraged, before fees "
            "and funding"
        )
    if row.get("position_qty") is not None:
        parts.append(f"the account's position is {row['position_qty']} {asset}")
    for key, what in (("protection", "stop"), ("take_profit", "take-profit")):
        text = protection_text(row.get(key), what)
        if text:
            parts.append(text)
    if view["close"]:
        parts.append(view["close"]["text"].removeprefix(f"{asset}: "))
    view["text"] = "; ".join(parts)
    return view


def signals_view(signals: Any, doc: dict) -> dict:
    """Each value signal's current value and age, with a sentence."""
    if not isinstance(signals, dict):
        return {}
    view = {}
    for signal_id, signal in signals.items():
        if not isinstance(signal, dict):
            continue
        value, age = signal.get("value"), signal.get("age_secs")
        name = value_text(doc, signal_id) if doc else str(signal_id)
        if value is None:
            text = f"{name} has no value yet"
        else:
            text = f"{name} is {level_text(doc, signal_id, value) if doc else value}"
            if age is not None:
                text += f" ({age} s old)"
        if signal.get("stale"):
            text += ": stale, so nothing that reads it fires"
        view[signal_id] = {**signal, "text": text}
    return view


STATE_TEXTS = {
    "blocked": "blocked: an order's outcome is unknown, and it sends nothing new until it is known",
    "held": "held by a Reya operator: it sends nothing until it is resumed",
}


def _document_of(run: dict) -> dict:
    try:
        doc = json.loads(run["document"]["canonical"])
    except (KeyError, TypeError, ValueError):
        return {}
    return doc if isinstance(doc, dict) and isinstance(doc.get("envelope"), dict) else {}


def profit_view(profit: Any) -> dict | None:
    """The strategy's price move over all its markets: the service's `pct`, `usd` and `entry_usd` (what its own
    shares cost), with a sentence. Unleveraged, before fees and funding: never called profit alone."""
    if not isinstance(profit, dict):
        return None
    view = dict(profit)
    if profit.get("pct") is None:
        view["text"] = "no price move to show: it holds nothing, or a mark or the account is stale"
    else:
        view["text"] = (
            f"{price_move_text(profit['pct'], profit.get('usd'))} over all its markets"
            + (f", on {usd_text(profit['entry_usd'])} of positions" if profit.get("entry_usd") is not None else "")
            + ", unleveraged, before fees and funding"
        )
    return view


def profit_exit_view(row: Any, preview: bool = False) -> dict:
    """An exit on the strategy's price move, with dollars beside the level: in a preview at the planned entry
    ("+10% price move, about $149"), in a status at the entry value now, with the move so far."""
    if not isinstance(row, dict):
        return {}
    level = row.get("level_pct")
    where = f" on {base_asset(str(row['market']))}" if row.get("market") else ""
    text = f"exit at a {signed_pct(level)} price move{where}"
    dollars = row.get("profit_usd") if preview else row.get("target_usd")
    if dollars is not None:
        text += f", about {signed_usd(dollars)}"
        if preview and row.get("entry_usd") is not None:
            text += f" on {usd_text(row['entry_usd'])} of entry"
    if not preview and row.get("current_usd") is not None:
        text += f"; the move now is {signed_usd(row['current_usd'])}"
    return {**row, "text": text + " (unleveraged, before fees and funding)"}


def status_view(run: dict, events: list[dict], *, truncated: bool = False, skips_shown: int = 20) -> dict:
    """A strategy's status: its state, a row per market, its resting orders, firings per transition and skips."""
    doc = _document_of(run)
    firings = [transition_firing_view(e) for e in events if e.get("kind") == "fired"]
    skips = [skip_view(e) for e in events if is_skip(e)]
    counts: dict[str, int] = {}
    for skip in skips:
        counts[str(skip["code"])] = counts.get(str(skip["code"]), 0) + 1
    stale = run.get("stale")
    view: dict[str, Any] = {
        "run_id": run.get("run_id"),
        "account_id": run.get("account_id"),
        "label": run.get("label"),
        "state": run.get("state"),
        "mode": run.get("mode"),
        "mode_text": reason(MODE_TEXTS, run.get("mode")),
        "graph_state": run.get("graph_state"),
        "entered_at": iso(run.get("entered_at")),
        "final": run.get("final") is True,
        "stale": (
            {"code": stale.get("code"), "reason": reason(STALE_REASONS, stale.get("code")), "text": stale.get("text")}
            if isinstance(stale, dict)
            else None
        ),
        "allocation": allocation_view(run.get("allocation")),
        "markets": [market_view(m) for m in run.get("markets") or [] if isinstance(m, dict)],
        "profit": profit_view(run.get("profit")),
        "profit_exits": [profit_exit_view(r) for r in run.get("profit_exits") or [] if isinstance(r, dict)],
        "signals": signals_view(run.get("signals"), doc),
        "resting_orders": [resting_view(r) for r in run.get("resting_orders") or [] if isinstance(r, dict)],
        "next_wake_at": iso(run.get("next_wake_at")),
        "firings": firings,
        "skips": skips[-skips_shown:],
        "skip_counts": counts,
        "completion": completion_view(run.get("completion")),
        "stop": stop_view(run.get("stop")),
        "starter": run.get("starter"),
        "hosted_signer": run.get("hosted_signer"),
        "created_at": iso(run.get("created_at")),
        "expires_at": iso(run.get("expires_at")),
        "workflow": describe(doc) if doc else None,
        "rules": rules(doc) if doc else [],
        "version_hash": (run.get("document") or {}).get("version_hash"),
        "recent_events": [event_view(e) for e in events[-10:]],
    }
    if truncated:
        view["events_truncated"] = True
    view["summary"] = status_summary(view)
    return view


def strategy_name(run_id: Any, label: Any) -> str:
    """ "Strategy #7", or "Strategy #7 "ETH breakout"" with its label, as the app names it. The label is the user's
    own text: shown as text, never followed."""
    return f"Strategy #{run_id}" + (f' "{label}"' if isinstance(label, str) and label else "")


def status_summary(view: dict) -> str:
    """Summarize the strategy, and report its stops according to their confirmed state."""
    state = view.get("state")
    parts = [
        f"{strategy_name(view.get('run_id'), view.get('label'))} ({reason(MODE_NAMES, view.get('mode'))}) is {state}"
    ]
    markets = view.get("markets") or []
    if markets:
        parts.append(f"it trades {assets([m.get('market') for m in markets])}")
    if state in LIVE_STATES:
        if state in STATE_TEXTS:
            parts.append(STATE_TEXTS[state])
        if view.get("graph_state"):
            parts.append(
                f"in {view['graph_state']}" + (f" since {view['entered_at']}" if view.get("entered_at") else "")
            )
        if view.get("stale"):
            parts.append(f"not acting: {view['stale']['reason']}")
        if view.get("next_wake_at"):
            parts.append(f"next scheduled at {view['next_wake_at']}")
        profit = view.get("profit") or {}
        if profit.get("pct") is not None:
            parts.append(f"{price_move_text(profit['pct'], profit.get('usd'))} so far, before fees and funding")
        resting = [r for r in view.get("resting_orders") or [] if r.get("status") in ("placing", "resting", "partial")]
        if resting:
            parts.append(f"{len(resting)} resting order{'' if len(resting) == 1 else 's'} on the book")
    elif view.get("completion"):
        completion = view["completion"]
        where = f" on {base_asset(completion['market'])}" if completion.get("market") else ""
        parts.append(f"completed because {completion['reason_text']}{where}")
        for market in markets:
            for key, what in (("protection", "stop"), ("take_profit", "take-profit")):
                protection = market.get(key)
                kept = COMPLETED_PROTECTION.get(protection.get("state", "")) if isinstance(protection, dict) else None
                if kept:
                    parts.append(
                        f"its {what} on {base_asset(str(market.get('market')))} at "
                        f"{group(protection.get('trigger_px'))} {kept}"
                    )
    elif view.get("stop"):
        parts.append(f"stopped: {view['stop']['reason_text']}")
        parts += [m["close"]["text"] for m in markets if m.get("close")]
    allocation = view.get("allocation") or {}
    if allocation.get("text"):
        parts.append(allocation["text"])
    fired = len(view.get("firings") or [])
    skipped = sum((view.get("skip_counts") or {}).values())
    parts.append(f"{fired} step{'' if fired == 1 else 's'} fired, {skipped} skip{'' if skipped == 1 else 's'}")
    return "; ".join(p for p in parts if p) + "."


def summary_row(run: dict) -> dict:
    """A strategy in the account's list: label, markets and state, in one line."""
    markets = [m for m in run.get("markets") or [] if isinstance(m, str)]
    held = [m for m in run.get("holds_markets") or [] if isinstance(m, str)]
    state = run.get("state")
    text = f"{strategy_name(run.get('run_id'), run.get('label'))} ({reason(MODE_NAMES, run.get('mode'))})"
    text += f" trades {assets(markets)}: {state}" if markets else f": {state}"
    if state in LIVE_STATES and run.get("graph_state"):
        text += f", in {run['graph_state']}"
    if held and state not in LIVE_STATES:
        text += f"; still holds {assets(held)} while its stop there is live"
    return {
        **run,
        "mode_text": reason(MODE_TEXTS, run.get("mode")),
        "created_at": iso(run.get("created_at")),
        "completion": completion_view(run.get("completion")),
        "stop": stop_view(run.get("stop")),
        "text": text,
    }


def is_live(run: dict) -> bool:
    """Active, blocked or held; or completed while a stop it armed still holds a market."""
    return run.get("state") in LIVE_STATES or bool(run.get("holds_markets"))


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------


def _find_order(doc: dict, action_id: Any, order_id: Any) -> tuple[dict, dict] | None:
    action = _actions(doc).get(action_id)
    if not isinstance(action, dict):
        return None
    for order in action.get("orders") or []:
        if isinstance(order, dict) and order.get("id") == order_id:
            return action, order
    return None


def order_sizing(doc: dict, row: dict, steps: dict[str, Decimal] | None) -> dict | None:
    """Requested vs effective dollars for one order: the service floors the quantity to the lot at its limit.

    `steps` (each market's lot, from its public definition) adds the next step up and the dollars that reach
    it; without them the block still compares the two amounts.
    """
    found = _find_order(doc, row.get("action"), row.get("order"))
    if found is None:
        return None
    requested = size_usd(found[1].get("size"), found[0].get("total_usd"))
    qty, limit = _decimal(row.get("qty")), _decimal(row.get("limit_px"))
    if requested is None or requested <= 0 or qty is None or limit is None:
        return None
    market = str(row.get("market", ""))
    step = (steps or {}).get(market)
    sizing = notional_sizing(requested, qty, limit, step, symbol=market, priced_at="limit")
    if step is not None and "next_step_up" in sizing:
        # The service divides the order's dollars by its limit, so this is the least that reaches the next step.
        draft = decimal_text(minimum_notional(qty + step, limit))
        sizing["next_step_up"]["draft_usd"] = draft
        sizing["message"] += f" Draft it with {draft} USD to send that."
    return sizing


def preview_order_view(row: dict, doc: dict, steps: dict[str, Decimal] | None, resting: bool) -> dict:
    asset = base_asset(str(row.get("market") or "?"))
    if resting:
        text = (
            f"{row.get('side')} {row.get('qty')} {asset} resting at {group(row.get('limit_px'))}"
            f"{' (post-only)' if row.get('post_only') else ''}, reserving {usd_text(row.get('reserved_usd'))}"
        )
    else:
        text = (
            f"{row.get('side')} {row.get('qty')} {asset} at a limit of {group(row.get('limit_px'))} "
            f"({row.get('bound_bps')} bps max slippage), reserving {usd_text(row.get('reserved_usd'))}"
        )
    if row.get("stop_px"):
        text += f", with a stop at {group(row['stop_px'])}"
    if row.get("take_profit_px"):
        text += f", take profit at {group(row['take_profit_px'])}"
    view = {**row, "text": text, "sizing": order_sizing(doc, row, steps)}
    message = (view["sizing"] or {}).get("message")
    if message:
        view["text"] += f". {message}"
    return view


def start_transition_view(transition: dict, doc: dict, steps: dict[str, Decimal] | None) -> dict:
    """One transition of the start state as the service sized it now: its orders, resting orders and closes."""
    orders = [preview_order_view(o, doc, steps, False) for o in transition.get("orders") or [] if isinstance(o, dict)]
    resting = [preview_order_view(o, doc, steps, True) for o in transition.get("resting") or [] if isinstance(o, dict)]
    closes = [m for m in transition.get("closes") or [] if isinstance(m, str)]
    errors = [
        {
            **e,
            "text": f"order {e.get('order')} ({base_asset(str(e.get('market') or '?'))}): {e.get('message') or e.get('code')}",
        }
        for e in transition.get("order_errors") or []
        if isinstance(e, dict)
    ]
    parts = []
    if orders:
        parts.append(f"sends {len(orders)} immediate order{'' if len(orders) == 1 else 's'}")
    if resting:
        parts.append(f"places {len(resting)} resting order{'' if len(resting) == 1 else 's'}")
    if closes:
        parts.append(f"closes this strategy's own share in {assets(closes)}")
    text = (" and ".join(parts) or "sends nothing") + f", reserving {usd_text(transition.get('reserved_usd'))}"
    if errors:
        text += f"; {len(errors)} order{'' if len(errors) == 1 else 's'} could not be sized now: " + "; ".join(
            e["text"] for e in errors
        )
    if transition.get("already_met"):
        text = "its trigger is already met; it " + text
    return {**transition, "orders": orders, "resting": resting, "closes": closes, "order_errors": errors, "text": text}


def _start_transitions(doc: dict) -> list[dict]:
    entry = _states(doc).get(doc.get("start"))
    return [t for t in entry.get("on") or [] if isinstance(t, dict)] if isinstance(entry, dict) else []


def now_line(doc: dict, signals: dict, schedule: list[dict]) -> str | None:
    """Where the start state's triggers stand now: the value against each crossing's level, the next scheduled
    time, or that it fires right after the start. From the service's values, never computed from prices."""
    lines = []
    for transition in _start_transitions(doc):
        when = transition.get("when")
        signal = _signals(doc).get(when) or {}
        kind = signal.get("kind")
        if kind == "crosses":
            source, level = signal.get("input"), signal.get("level")
            value = (signals.get(source) or {}).get("value")
            name = value_text(doc, source)
            if value is None:
                lines.append(f"{name} has no value yet; the level is {level_text(doc, source, level)}")
                continue
            current, target = _decimal(value), _decimal(level)
            if current is None or target is None or current == 0:
                lines.append(f"{name} is {level_text(doc, source, value)}")
                continue
            bps = int((abs(target - current) * 10_000 / abs(current)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            met = current >= target if signal.get("direction") == "up" else current <= target
            where = "above" if target > current else "below" if target < current else "at"
            lines.append(
                f"{name} is {level_text(doc, source, value)}"
                + (
                    f", already past the level of {level_text(doc, source, level)}"
                    if met
                    else f"; the level {level_text(doc, source, level)} is {distance_text(str(bps))} {where} it"
                )
            )
        elif kind in ("every", "at"):
            nexts = next((s["next"] for s in schedule if s.get("signal") == when and s.get("next")), None)
            lines.append(f"{event_text(doc, when)}: next at {nexts[0]}" if nexts else event_text(doc, when))
        elif kind == "start":
            lines.append("it acts right after the start")
    return "; ".join(lines) or None


def balance_warnings(view: dict, balance: Decimal | None, budgets: tuple[Decimal, int] | None) -> list[dict]:
    """The two warnings the service cannot give, since it has no balance source: the kit's own, shown like the
    service's (`source: "kit"`).

    `size_above_balance`: the start state's largest step reserves more than the account's available balance (at
    1×). `budgets_above_balance`: that step plus the remaining budgets of the account's other live strategies
    (`budgets`: their total and how many) is more than it; only with at least one other, since otherwise it
    says the same. Neither when the balance could not be read, nor when the service already gave it.
    """
    if balance is None:
        return []
    steps = [_decimal(t.get("reserved_usd")) for t in view.get("start_transitions") or [] if isinstance(t, dict)]
    largest = max((step for step in steps if step is not None), default=Decimal(0))
    given = {w.get("code") for w in view.get("warnings") or [] if isinstance(w, dict)}
    warnings = []
    if largest > balance and "size_above_balance" not in given:
        warnings.append(
            {
                "code": "size_above_balance",
                "source": "kit",
                "message": (
                    f"Its largest step at the start reserves {usd_text(largest)}, more than the account's available "
                    f"balance of {usd_text(balance)} (equity less initial margin): at 1x, orders may be refused for "
                    "margin."
                ),
                "details": {
                    "reserved_usd": decimal_text(largest),
                    "available_balance_usd": format(balance.normalize(), "f"),
                },
            }
        )
    if budgets is not None and budgets[1] > 0 and "budgets_above_balance" not in given:
        others, count = budgets
        if largest + others > balance:
            warnings.append(
                {
                    "code": "budgets_above_balance",
                    "source": "kit",
                    "message": (
                        f"Its largest step ({usd_text(largest)}) and the remaining budgets of the account's "
                        f"{count} other live strateg{'y' if count == 1 else 'ies'} ({usd_text(others)}) add up to "
                        f"{usd_text(largest + others)}, more than the account's available balance of "
                        f"{usd_text(balance)}: they share that margin, so one strategy's orders may be refused for "
                        "another's position."
                    ),
                    "details": {
                        "reserved_usd": decimal_text(largest),
                        "other_budgets_usd": decimal_text(others),
                        "available_balance_usd": format(balance.normalize(), "f"),
                    },
                }
            )
    return warnings


def warning_view(warning: Any) -> dict:
    if not isinstance(warning, dict):
        return {"message": str(warning)}
    view = dict(warning)
    if warning.get("code") == "market_in_use" and isinstance(warning.get("details"), dict):
        hint = market_in_use_text(warning["details"])
        if hint:
            view["next"] = hint
    return view


def preview_view(preview: dict, doc: dict, steps: dict[str, Decimal] | None = None) -> dict:
    """The service's preview, rendered: markets and signals now, the start state's transitions as it would size
    them, the states as its sentences, the next scheduled times and its warnings."""
    signals = signals_view(preview.get("signals"), doc)
    schedule = [
        {"signal": s.get("signal"), "next": [iso(t) for t in s.get("next") or []]}
        for s in preview.get("schedule") or []
        if isinstance(s, dict)
    ]
    # The service lists its states by id; read them in the order the strategy reaches them, any other last.
    order = state_order(doc)
    states = sorted(
        (s for s in preview.get("states") or [] if isinstance(s, dict)),
        key=lambda s: order.index(s.get("id")) if s.get("id") in order else len(order),
    )
    service_rules = [
        f"In {state.get('id')}: {t.get('text')}"
        for state in states
        for t in state.get("transitions") or []
        if isinstance(t, dict) and isinstance(t.get("text"), str)
    ]
    view: dict[str, Any] = {
        "ok": True,
        "version_hash": preview.get("version_hash"),
        "as_of": iso(preview.get("as_of")),
        "workflow": describe(doc),
        "rules": service_rules or rules(doc),
        "orders": order_rows(doc),
        "markets": [
            {**m, "mark_at": iso(m.get("mark_at"))} for m in preview.get("markets") or [] if isinstance(m, dict)
        ],
        "signals": signals,
        "start_transitions": [
            start_transition_view(t, doc, steps) for t in preview.get("start_transitions") or [] if isinstance(t, dict)
        ],
        "schedule": schedule,
        "fire_now_needed": preview.get("fire_now_needed") is True,
        "now": now_line(doc, signals, schedule),
        "profit_exits": [
            profit_exit_view(r, preview=True) for r in preview.get("profit_exits") or [] if isinstance(r, dict)
        ],
        "warnings": [warning_view(w) for w in preview.get("warnings") or []],
    }
    if view["fire_now_needed"]:
        view["already_triggered"] = (
            "The start trigger is already met. Start with --fire-now to act on the first fresh value, or with "
            "--wait-for-rearm to wait until the value moves back past the re-arm band and crosses again; without "
            "either, start signs nothing and asks."
        )
    for key, value in preview.items():
        view.setdefault(key, value)
    return view


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _json_or_none(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _envelope(status: int, data: Any, headers) -> ServiceError | None:
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        return None
    message = " ".join(str(error.get("message") or error["code"]).split())[:500]
    retry_after = headers.get("Retry-After")
    details = error.get("details")
    return ServiceError(
        status=status,
        code=error["code"][:100],
        message=message,
        retryable=error.get("retryable") is True,
        details=details if isinstance(details, dict) else {},
        retry_after=int(retry_after) if isinstance(retry_after, str) and retry_after.isdecimal() else None,
        request_id=headers.get("x-request-id"),
    )


class WorkflowClient:
    """One request per call: no redirects, no retries, writes never repeated. The one exception is a run read
    that carried a read grant and was refused without the service's error envelope (see request)."""

    def __init__(self, base_url: str, *, timeout_s: float = 20.0, read_grants: ReadGrants | None = None):
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.read_grants = read_grants
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "WorkflowClient":
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout_s, connect=5), headers={"User-Agent": USER_AGENT}
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            await self.session.close()

    async def request(self, method: str, path: str, *, body: dict | None = None, params: dict | None = None) -> Any:
        write = method == "POST" and path != "/v1/validate" and path != "/v1/preview"
        grant = None
        if self.read_grants is not None and method == "GET" and READ_GRANT_PATH_RE.fullmatch(path):
            grant = self.read_grants.header()
        status, headers, raw = await self._exchange(method, path, body, params, write, grant)
        if grant is not None and 400 <= status < 500 and status != 429:
            assert self.read_grants is not None
            refusal = _envelope(status, _json_or_none(raw), headers)
            if refusal is None:
                # Not the service's answer: a gateway that does not take the header. Until the service requires
                # grants, ask once more without it (and sign a new one next time), so a grant never costs the
                # answer. The service's own refusals (INVALID_READ_GRANT, READ_GRANT_REQUIRED, RUN_NOT_FOUND, ...)
                # stand: asking again without the grant would only hide why.
                self.read_grants.discard()
                status, headers, raw = await self._exchange(method, path, body, params, write, None)
            elif refusal.code == "INVALID_READ_GRANT":
                self.read_grants.discard()  # never send that one again; the next command signs a new one
        return self._answer(status, headers, raw, write)

    async def _exchange(
        self, method: str, path: str, body: dict | None, params: dict | None, write: bool, grant: str | None
    ) -> tuple[int, Any, bytes]:
        assert self.session is not None
        try:
            async with self.session.request(
                method,
                self.base_url + path,
                json=body,
                params=params,
                allow_redirects=False,
                headers={"Authorization": grant} if grant else None,
            ) as response:
                try:
                    raw = await response.content.readexactly(MAX_RESPONSE_BYTES + 1)
                except asyncio.IncompleteReadError as exc:
                    raw = exc.partial
                status, headers = response.status, response.headers
        except aiohttp.ClientConnectorError:
            raise WorkflowError(f"Cannot reach the strategy service at {self.base_url}; nothing was sent") from None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if write:
                raise OutcomeUnknown(f"No answer from {self.base_url}{path}") from None
            raise WorkflowError(f"The strategy service at {self.base_url} did not answer") from None
        return status, headers, raw

    def _answer(self, status: int, headers: Any, raw: bytes, write: bool) -> Any:
        if len(raw) > MAX_RESPONSE_BYTES:
            if write:
                raise OutcomeUnknown("The service's answer was too large to read")
            raise WorkflowError("The strategy service's answer was too large")
        if region_blocked(status, raw):
            raise RegionUnavailableError()
        data = _json_or_none(raw)
        if status in (200, 201) and data is not None:
            return data
        refusal = _envelope(status, data, headers)
        if refusal is not None:
            raise refusal
        if write:
            raise OutcomeUnknown(f"HTTP {status} without an answer the kit can read")
        raise WorkflowError(f"The strategy service answered HTTP {status} without an error the kit can read")

    async def object(self, method: str, path: str, **kwargs) -> dict:
        data = await self.request(method, path, **kwargs)
        if not isinstance(data, dict):
            if method == "POST" and path.startswith("/v1/runs"):
                raise OutcomeUnknown(f"{path} answered something other than a JSON object")
            raise WorkflowError(f"The strategy service answered {path} with something other than a JSON object")
        return data

    async def info(self) -> dict:
        return await self.object("GET", "/v1/info")

    async def validate(self, document: str) -> dict:
        return await self.object("POST", "/v1/validate", body={"document": document})

    async def expand(self, params: dict) -> dict:
        """The `basket` template filled with `params`: the service expands it, checks it like a document, and
        answers as validate does (`canonical`, `version_hash`, `warnings`; or `errors` with `params.` paths)."""
        return await self.object("POST", "/v1/validate", body=template_request(params))

    async def preview(self, document: str) -> dict:
        # The document only: whether to fire on the first mark is the start's choice (`fireNow`).
        return await self.object("POST", "/v1/preview", body={"document": document})

    async def start(self, body: dict) -> dict:
        return await self.object("POST", "/v1/runs", body=body)

    async def stop(self, run_id: int, body: dict) -> dict:
        return await self.object("POST", f"/v1/runs/{run_id}/stop", body=body)

    async def run(self, run_id: int) -> dict:
        return await self.object("GET", f"/v1/runs/{run_id}")

    async def account_runs(self, account_id: int, *, state: str, limit: int = 50, before: str | None = None) -> dict:
        params = {"state": state, "limit": str(limit)} | ({"before": before} if before else {})
        return await self.object("GET", f"/v1/accounts/{account_id}/runs", params=params)

    async def activity(self, run_id: int, *, max_pages: int = 25) -> tuple[list[dict], bool]:
        """The whole event trail, oldest first; True when it stopped at `max_pages` pages."""
        events: list[dict] = []
        after: str | None = None
        for _ in range(max_pages):
            params = {"limit": "200"} | ({"after": after} if after else {})
            page = await self.request("GET", f"/v1/runs/{run_id}/activity", params=params)
            if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                raise WorkflowError("The strategy service returned an unreadable activity page")
            events.extend(e for e in page["events"] if isinstance(e, dict))
            after = page.get("next_after")
            if not isinstance(after, str) or not UINT_RE.fullmatch(after):
                return events, False
        return events, True
