"""Client for Reya's hosted strategy service: documents, checks, signed start and stop, run status.

The service is the only evaluator. `validate` and `preview` ask it; the kit never decides locally whether a rule
would fire, and there is no replay. Strategies are `reya.workflow/3` documents. The account, the mode (Paper or
Live), the expiry and whether the first rule may fire at once are not in the document: they are the preview's
terms, which the service keeps as a preview record for ten minutes, and a Start signs that record's `previewId`.
So a Start is always of a preview the kit has just asked for.

Start and stop are EIP-712 messages (`StrategyStart`, `StrategyStop`) signed with the agent key from `agent init`,
never the owner's key, under the domain and types the service publishes at `/v1/info` (`v3.eip712`), which the kit
checks against the fields it knows. Reads of runs, and checks against an account, carry a read grant, also signed
with the agent key (see ReadGrants).

Signing is local (eth_account); nothing here creates an RPC provider or a transaction.
"""

from typing import Any, Callable

import asyncio
import base64
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
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

# A run's modes, as the service, the signed Start and `--mode` spell them. A strategy starts in Paper unless the
# user asks for Live.
MODES = ("paper", "live")
DEFAULT_MODE = "paper"
MODE_NAMES = {"paper": "Paper", "live": "Live"}
MODE_TEXTS = {
    "paper": "Paper: fills are simulated on the run's own book, nothing is sent",
    "live": "Live: it sends real orders on the Agent account",
}
# What a check is against: the document alone (`draft`), or a Paper or Live run of it on an account.
CHECK_MODES = ("draft", "paper", "live")

# The message fields the kit knows how to fill. `/v1/info` (`v3.eip712.types`) supplies their order and the
# domain; a service that publishes any other field set needs a newer kit.
MESSAGE_FIELDS = {
    "StrategyStart": {
        "verifyingChainId": "uint256",
        "environment": "string",
        "accountId": "uint64",
        "versionHash": "bytes32",
        "expiresAt": "uint64",
        "mode": "string",
        "fireNow": "bool",
        "previewId": "uint64",
        "handoverStops": "HandoverStop[]",
        "signer": "address",
        "nonce": "uint64",
        "deadline": "uint64",
    },
    "StrategyStop": {
        "verifyingChainId": "uint256",
        "environment": "string",
        "accountId": "uint64",
        "strategyId": "uint64",
        "runId": "uint64",
        "keepPosition": "bool",
        "signer": "address",
        "nonce": "uint64",
        "deadline": "uint64",
    },
}
# The structs the messages name. The kit moves no exchange stop into a strategy, so it always signs
# `handoverStops: []`; the type is still part of the Start's type hash.
STRUCT_FIELDS = {
    "HandoverStop": {
        "market": "string",
        "orderId": "uint64",
        "kind": "string",
        "isBuy": "bool",
        "triggerPrice": "uint256",
        "limitPrice": "uint256",
        "reduceOnly": "bool",
    },
}
UINT_BITS = {"uint64": 64, "uint128": 128, "uint256": 256}
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
# A start or stop is valid for this long; the service accepts at most MAX_DEADLINE_SECS.
REQUEST_LIFETIME_S = 300
# The service's bound on a deadline (its OpenAPI `x-eip712.maxDeadlineSecs`); `/v1/info` does not publish it.
MAX_DEADLINE_SECS = 600
# The service refuses a document over 12,288 bytes (workflow-core `MAX_DOCUMENT_BYTES`).
MAX_DOCUMENT_BYTES = 12_288
MAX_RESPONSE_BYTES = 1024 * 1024
# Run ids below this ran on the previous version of strategies; the service answers them 410.
FIRST_V3_RUN_ID = 3_000_000
# A preview record lives this long; a Start must sign one younger than that.
PREVIEW_RECORD_S = 600
# A run's end, from the preview: at least an hour ahead, at most 365 days (the service's bounds, kept a little
# inside them because the service judges them on its own clock).
DEFAULT_EXPIRES = "7d"
MIN_EXPIRY_S = 3600
MAX_EXPIRY_S = 365 * 86400
EXPIRY_SKEW_S = 300
MAX_LABEL_CHARS = 64
# Integers a document may hold: RFC 8785 writes numbers as doubles, so the service refuses any beyond these.
MAX_SAFE_INTEGER = 2**53 - 1

UINT_RE = re.compile(r"(0|[1-9][0-9]*)")
DECIMAL_RE = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]*[1-9])?")
SIGNED_DECIMAL_RE = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]*[1-9])?")
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
SYMBOL_RE = re.compile(r"[A-Z0-9]{1,32}")
# A base path in REYA_WORKFLOWS_URL: plain segments only, so `base + "/v1/..."` names exactly one
# path (no empty, dot or escaped segments for a server to normalise differently).
PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_~-][A-Za-z0-9._~-]*")
BYTES32_RE = re.compile(r"0x[0-9a-fA-F]{64}")
SIGNATURE_RE = re.compile(r"0x[0-9a-fA-F]{130}")

# Strategy documents: the service's `reya.workflow/3`, and its checks' `reya.check-result/3`.
SCHEMA_TAG = "reya.workflow/3"
CHECK_SCHEMA = "reya.check-result/3"
PREVIOUS_SCHEMAS = ("reya.workflow/1", "reya.workflow/2")
# The terms `draft` writes that the user does not give: the two settings (the only values this release runs) and
# the slippage cap on every immediate order. The in-app chat writes the same.
DEFAULT_MAX_SLIPPAGE_BPS = 25
MAX_SLIPPAGE_BPS = 10_000
ON_LIQUIDATION = "close_and_end"
ON_MARGIN_SKIP = "keep_running"
# workflow-core's caps a drafted basket can reach (`caps.rs`), and a loop's round limit.
MAX_ACTIONS = 16
MAX_RULES_PER_STATE = 6
MAX_ROUNDS = 100
# A run's `state`: running (`active`), holding positions after a final `hold` (`held`), or ended.
RUNNING_STATES = ("active", "held")
ENDED_STATES = ("stopped", "completed")

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
    "UNKNOWN_HOSTED_SIGNER": "The service's hosted signer changed: run strategy info, then the same start again.",
    "HOSTED_SIGNER_REJECTED": "A hosted signer cannot start or stop strategies; the kit signs with the agent key.",
    "NOT_WHITELISTED": (
        "The account's owner is not whitelisted for trading on this deployment, so no strategy can start on it, "
        "Paper included."
    ),
    "ACCOUNT_STRATEGY_LIMIT": (
        "An account runs one Live strategy at a time, and this one already runs one: stop it first (strategy "
        "status shows it), or start this one in Paper."
    ),
    "NONCE_TOO_LOW": "Nothing was started or stopped. Run the same command again for a fresh nonce.",
    "REQUEST_EXPIRED": "Nothing was started or stopped. Check this computer's clock, then run the command again.",
    "DEADLINE_TOO_FAR": "Nothing was started or stopped. Check this computer's clock, then run the command again.",
    "MODE_NOT_ALLOWED": "This environment does not allow that mode now; strategy info lists the modes it allows.",
    "DOCUMENT_MISMATCH": "The signed fields differ from the preview; run the same start again to preview afresh.",
    "WRONG_ENVIRONMENT": "The kit and the service disagree on the environment; check REYA_API_URL and REYA_WORKFLOWS_URL.",
    "RATE_LIMITED": "Wait `retry_after_s` seconds, then try again.",
    "ACCOUNT_STATE_UNAVAILABLE": "The service cannot read the account's state just now; try again shortly.",
    "BUSY": "The service is busy; try again shortly.",
    "NOT_READY": "The service is starting or restarting; try again shortly.",
    "RUN_ON_PREVIOUS_VERSION": (
        "That strategy ran on the previous version of strategies, which the service no longer shows or runs. "
        "strategy status lists this account's current ones."
    ),
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
# START_REFUSED's `details.reason`: why a signed Start was refused for its preview.
START_REFUSED_HINTS = {
    "card_expired": (
        "The preview it signed is gone: the service keeps one for ten minutes and forgets it on a restart. Run the "
        "same strategy start again: it previews afresh before it signs."
    ),
    "card_mismatch": (
        "The account changed since the preview (details.changed lists what): run strategy preview again, then the "
        "same start."
    ),
    "start_recheck_worse": "The account changed since the preview: run strategy preview again, then the same start.",
    "start_checks_refused": (
        "The service's checks refuse the start now (details.issues lists them): strategy preview shows why."
    ),
    "preview_used": "That preview already started a strategy: strategy status lists it.",
    "trades_any_market": (
        "A Live strategy must name the markets it trades, and this one may trade any: name them, or start it in "
        "Paper."
    ),
    "funding_crossing": (
        "This release only evaluates crossings of the mark price, so a crossing of a funding series would never "
        "fire: cross the mark, or read the funding series in a rule's `if` instead."
    ),
    "start_binding_mismatch": "The signed expiry or version hash differs from the preview: run the same start again.",
}
# INVALID_REQUEST's `details.reason` that need more than the service's message.
REQUEST_HINTS = {
    "resting_limits_unavailable": (
        "This release sends immediate orders only, so a strategy with resting limit orders (`limit_orders`) cannot "
        "start yet: draft it with immediate orders."
    ),
    "odds_source_off": "The odds source this strategy reads is switched off on this deployment; try again later.",
    "paper_balance_needed": (
        "A Paper run needs a balance to take its margin from, and the account has none: pass --paper-balance USD."
    ),
    "paper_keep_refused": "A Paper strategy stops only by closing: leave out --keep-position.",
    "expires_at_too_soon": "--expires is too soon: give at least an hour.",
    "expires_at_too_far": "--expires is too far: give at most 365 days.",
}
STOP_REFUSED_HINTS = {
    "run_ended": "The strategy has already ended: nothing was stopped. strategy status shows how it ended.",
    "already_keeping": (
        "A Stop, keep positions already ended it: its positions are yours now. Close them on the exchange "
        "(positions close_all, preview first)."
    ),
    "keep_blocked_by_close": "Its max-loss close is running; a keep cannot interrupt it. strategy status follows it.",
}

# NOT_AGENT_ACCOUNT's `details.reason`: why the service does not take the account as the Agent account. The
# strategy commands replace this with the exact fix when they can read the owner's accounts
# (agent.agent_account_warning).
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

    @property
    def reason(self) -> str | None:
        reason = self.details.get("reason")
        return reason if isinstance(reason, str) else None

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
        hint = ERROR_HINTS.get(self.code)
        reason = self.reason
        if self.code == "NOT_AGENT_ACCOUNT" and reason in AGENT_ACCOUNT_REASONS:
            hint = AGENT_ACCOUNT_REASONS[reason]
        elif self.code == "START_REFUSED" and reason in START_REFUSED_HINTS:
            hint = START_REFUSED_HINTS[reason]
        elif self.code == "STOP_REFUSED" and reason in STOP_REFUSED_HINTS:
            hint = STOP_REFUSED_HINTS[reason]
        elif self.code in ("INVALID_REQUEST", "INVALID_DOCUMENT") and reason in REQUEST_HINTS:
            hint = REQUEST_HINTS[reason]
        elif self.code == "ACCOUNT_STRATEGY_LIMIT":
            hint = strategy_limit_text(self.details) or hint
        if hint:
            result["next"] = hint
        return result


def _run_ref(value: Any) -> str | None:
    """A run id from the service's details, only when it is one (details are data, never text to repeat)."""
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return text if UINT_RE.fullmatch(text) and int(text) < 2**64 else None


def strategy_limit_text(details: dict) -> str | None:
    """ACCOUNT_STRATEGY_LIMIT: the account's Live strategy, and that it has to stop first."""
    live = details.get("live_strategy")
    run = _run_ref(live.get("run_id") if isinstance(live, dict) else live)
    if run is None:
        return None
    return (
        f"This account already runs Live strategy #{run}, and an account runs one Live strategy at a time: stop it "
        f"first (strategy stop {run}), or start this one in Paper."
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
    schemas: tuple[str, ...]
    # Whether a new Live (`v3.start_enabled`) and a new Paper (`v3.paper_start_enabled`) run may start.
    live_enabled: bool
    paper_enabled: bool
    # Whether a check may name an account (`v3.account_checks_enabled`); without it only a draft check runs.
    account_checks: bool = True
    accepted: dict = field(default_factory=dict)
    trading_whitelist: str | None = None
    version: str | None = None
    max_deadline_secs: int = MAX_DEADLINE_SECS

    @property
    def modes(self) -> list[str]:
        """The modes a new run may start in now."""
        return [mode for mode, on in (("paper", self.paper_enabled), ("live", self.live_enabled)) if on]

    @property
    def active_signer(self) -> str | None:
        active = [entry["address"] for entry in self.hosted_signers if entry.get("role") == "active"]
        return to_checksum_address(active[0]) if len(active) == 1 else None

    def check_schema(self) -> None:
        """Refuse to sign for a service that says it does not run the documents this kit drafts."""
        if SCHEMA_TAG not in self.schemas:
            raise WorkflowError(
                f"The strategy service runs {', '.join(self.schemas) or 'no'} strategy documents, and this kit drafts "
                f"{SCHEMA_TAG}; nothing was signed. Update the kit, or wait until the service runs {SCHEMA_TAG}."
            )

    def is_hosted(self, address: str) -> bool:
        return any(entry["address"].lower() == address.lower() for entry in self.hosted_signers)

    def accepts(self, group: str, kind: str) -> bool:
        """Whether the service runs `kind` among its accepted `group` (`triggers`, `actions`, ...). A service that
        lists no such group is taken at its word by its own checks, not the kit's."""
        names = self.accepted.get(group)
        return not isinstance(names, list) or kind in names

    def view(self) -> dict:
        refused = self.accepted.get("refused_shapes")
        return {
            "environment": self.environment,
            "chain_id": self.chain_id,
            "service_version": self.version,
            "eip712_domain": self.domain,
            "hosted_signer": self.active_signer,
            "hosted_signers": self.hosted_signers,
            "schemas": list(self.schemas),
            "modes": self.modes,
            "mode_names": {mode: MODE_NAMES[mode] for mode in self.modes},
            "account_checks": self.account_checks,
            "trading_whitelist": self.trading_whitelist,
            "max_deadline_secs": self.max_deadline_secs,
            "accepted": {
                key: self.accepted[key]
                for key in ("triggers", "actions", "stop_levels", "states", "lists", "expr_series")
                if isinstance(self.accepted.get(key), list)
            },
            "refused_shapes": (
                [
                    {"feature": r.get("feature"), "rule": r.get("rule")}
                    for r in refused
                    if isinstance(r, dict) and isinstance(r.get("rule"), str)
                ]
                if isinstance(refused, list)
                else []
            ),
        }


def _incompatible(what: str) -> WorkflowError:
    return WorkflowError(f"The strategy service publishes {what} this kit cannot use; update the kit")


def _fields_of(types: dict, name: str, known: dict[str, str]) -> list[dict[str, str]]:
    fields = types.get(name)
    if not isinstance(fields, list) or not all(
        isinstance(f, dict) and set(f) == {"name", "type"} and all(isinstance(v, str) for v in f.values())
        for f in fields
    ):
        raise _incompatible(f"a {name} type")
    if len(fields) != len(known) or {f["name"]: f["type"] for f in fields} != known:
        raise _incompatible(f"a {name} type")
    return [{"name": f["name"], "type": f["type"]} for f in fields]


def parse_types(types: Any) -> dict[str, list[dict[str, str]]]:
    """The messages' and their structs' fields in the order the service publishes them, each checked against the
    field set the kit knows. Any other type it publishes (`EIP712Domain`) is not read."""
    if not isinstance(types, dict):
        raise _incompatible("EIP-712 types")
    known = {**MESSAGE_FIELDS, **STRUCT_FIELDS}
    return {name: _fields_of(types, name, fields) for name, fields in known.items()}


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
        v3 = data["v3"]
        eip712 = v3["eip712"]
        domain = parse_domain(eip712["domain"])
        # The read grant's type, where the service publishes it, is the one the kit signs (READ_GRANT_TYPE).
        published = data.get("eip712") if isinstance(data.get("eip712"), dict) else {}
        grant = (published.get("types") or {}).get("ReadGrant") if isinstance(published.get("types"), dict) else None
        if grant is not None and grant != READ_GRANT_TYPE:
            raise _incompatible("a ReadGrant type")
        if isinstance(published.get("domain"), dict) and parse_domain(published["domain"]) != domain:
            raise _incompatible("two EIP-712 domains")
        schemas = data.get("schemas")
        accepted = v3.get("accepted_kinds")
        info = ServiceInfo(
            environment=data["environment"],
            chain_id=int(uint_text(data["chain_id"], 256)),
            domain=domain,
            types=parse_types(eip712["types"]),
            hosted_signers=[
                {"address": to_checksum_address(s["address"]), "role": s["role"], "can_sign": s["can_sign"] is True}
                for s in data["hosted_signers"]
                if isinstance(s, dict) and isinstance(s.get("address"), str) and ADDRESS_RE.fullmatch(s["address"])
            ],
            schemas=tuple(x for x in schemas if isinstance(x, str)) if isinstance(schemas, list) else (),
            live_enabled=v3.get("start_enabled") is True,
            paper_enabled=v3.get("paper_start_enabled") is True,
            account_checks=v3.get("account_checks_enabled") is not False,
            accepted=accepted if isinstance(accepted, dict) else {},
            trading_whitelist=v3.get("trading_whitelist") if isinstance(v3.get("trading_whitelist"), str) else None,
            version=data.get("version") if isinstance(data.get("version"), str) else None,
        )
    except WorkflowError:
        raise
    except (KeyError, TypeError, ValueError):
        raise WorkflowError(
            f"{url}/v1/info did not return the strategy service's parameters for {SCHEMA_TAG}; update the kit, or "
            "check REYA_WORKFLOWS_URL"
        ) from None
    if info.environment != environment or info.chain_id != chain_id:
        raise WorkflowError(
            f"The strategy service at {url} serves {info.environment} (chain {info.chain_id}), but the kit is "
            f"configured for {environment} (chain {chain_id}). Check REYA_API_URL and {SERVICE_URL_VAR}.",
            code="WRONG_ENVIRONMENT",
        )
    return info


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
        elif kind == "HandoverStop[]":
            # This release moves no exchange stop into a strategy (the service takes `[]` only), and neither does
            # the kit.
            if value != []:
                raise WorkflowError(f"{name} must be empty", code="INVALID_REQUEST")
        elif not isinstance(value, str):
            raise WorkflowError(f"{name} must be a string", code="INVALID_REQUEST")
    if request["environment"] not in ENVIRONMENT_NAMES:
        raise WorkflowError("unknown environment", code="INVALID_REQUEST")
    if primary == "StrategyStart" and request["mode"] not in MODES:
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


def _referenced(primary: str, types: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
    """`primary` and every struct type it names, directly or through another."""
    found: dict[str, list[dict[str, str]]] = {}
    pending = [primary]
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found[name] = types[name]
        pending += [f["type"].removesuffix("[]") for f in types[name] if f["type"].removesuffix("[]") in types]
    return found


def _typed_value(kind: str, value: Any, types: dict[str, list[dict[str, str]]]) -> Any:
    """A field's JSON spelling (decimal strings, 0x hex) as the EIP-712 value eth_account encodes."""
    if kind.endswith("[]"):
        return [_typed_value(kind[:-2], item, types) for item in value]
    if kind in types:
        return {f["name"]: _typed_value(f["type"], value[f["name"]], types) for f in types[kind]}
    if kind in UINT_BITS:
        return int(value)
    if kind in ("bytes32", "bytes16"):
        return bytes.fromhex(value[2:])
    return value


def _signable(
    primary: str, types: dict[str, list[dict[str, str]]], domain: dict[str, str], request: dict
) -> SignableMessage:
    used = _referenced(primary, types)
    return encode_typed_data(
        full_message={
            "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}]}
            | used,
            "primaryType": primary,
            "domain": domain,
            "message": _typed_value(primary, request, used),
        }
    )


def typed_data(primary: str, request: dict, info: ServiceInfo) -> SignableMessage:
    return _signable(primary, info.types, info.domain, request)


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


# The fields of each body, as the service's strict parse takes them.
BODY_FIELDS = {"StrategyStart": ("document", "request", "signature"), "StrategyStop": ("request", "signature")}
# What only a previous version's Start or Stop carries.
PREVIOUS_START_FIELDS = ("allocationUsd", "hostedSigner")


def _strict_body(text: str) -> Any:
    def pairs(items: list) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise WorkflowError(f"the body repeats the key {key!r}", code="INVALID_REQUEST")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_float=lambda _: _invalid_number())
    except json.JSONDecodeError:
        raise WorkflowError("the body is not JSON", code="INVALID_REQUEST") from None


def _invalid_number() -> Any:
    raise WorkflowError("the body has a JSON number with a fraction", code="INVALID_REQUEST")


def verify_start_body(text: str, info: ServiceInfo, now: int) -> str:
    """The checks the service makes on a Start without state, in its order: the strict parse (no repeated key, no
    unknown field, every integer a decimal string), a previous version's Start, then the message (verify_body)."""
    body = _strict_body(text)
    if not isinstance(body, dict):
        raise WorkflowError("the body must be a JSON object", code="INVALID_REQUEST")
    request = body.get("request")
    if "label" in body or (isinstance(request, dict) and any(k in request for k in PREVIOUS_START_FIELDS)):
        raise WorkflowError(
            "a Start of the previous version", code="INVALID_REQUEST", details={"reason": "previous_version"}
        )
    if set(body) != set(BODY_FIELDS["StrategyStart"]) or not isinstance(body.get("document"), str):
        raise WorkflowError("a Start has exactly document, request and signature", code="INVALID_REQUEST")
    return verify_body("StrategyStart", body, info, now)


def verify_stop_body(path_run_id: Any, body: Any, info: ServiceInfo, now: int) -> str:
    """The checks the service makes on a Stop without state, in its order: the path's run is the signed `runId` and
    a v3 id, the Stop names its strategy, then the message (verify_body)."""
    if not isinstance(body, dict) or set(body) != set(BODY_FIELDS["StrategyStop"]):
        raise WorkflowError("a Stop has exactly request and signature", code="INVALID_REQUEST")
    request = body["request"]
    check_message("StrategyStop", request)
    if str(path_run_id) != request["runId"]:
        raise WorkflowError("the path names another run than the signed one", code="INVALID_REQUEST")
    if int(request["runId"]) < FIRST_V3_RUN_ID:
        raise WorkflowError("a run of the previous version", code="RUN_ON_PREVIOUS_VERSION")
    if request["strategyId"] == "0":
        raise WorkflowError("a Stop names its strategy", code="INVALID_REQUEST", details={"reason": "previous_version"})
    return verify_body("StrategyStop", body, info, now)


def ordered(primary: str, values: dict, info: ServiceInfo) -> dict:
    return {field["name"]: values[field["name"]] for field in info.types[primary]}


def start_request(
    info: ServiceInfo,
    *,
    account_id: int,
    version_hash: str,
    expires_at: int,
    mode: str,
    fire_now: bool,
    preview_id: int,
    signer: str,
    nonce: int,
    deadline: int,
) -> dict:
    """The Start message for one preview record: its account, version hash, expiry, mode and `fireNow` are the
    preview's, and `previewId` names the record."""
    return ordered(
        "StrategyStart",
        {
            "verifyingChainId": str(info.chain_id),
            "environment": info.environment,
            "accountId": str(account_id),
            "versionHash": version_hash.lower(),
            "expiresAt": str(expires_at),
            "mode": mode,
            "fireNow": fire_now,
            "previewId": str(preview_id),
            "handoverStops": [],
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
    strategy_id: int,
    run_id: int,
    signer: str,
    nonce: int,
    deadline: int,
    keep_position: bool,
) -> dict:
    """The stop message: `keep_position` false closes the run's positions and then ends it, true ends it leaving
    them open with no stop from the service."""
    values = {
        "verifyingChainId": str(info.chain_id),
        "environment": info.environment,
        "accountId": str(account_id),
        "strategyId": str(strategy_id),
        "runId": str(run_id),
        "keepPosition": keep_position,
        "signer": to_checksum_address(signer),
        "nonce": str(nonce),
        "deadline": str(deadline),
    }
    return ordered("StrategyStop", values, info)


def fresh_nonce() -> int:
    """Microseconds since the epoch: strictly increasing per signer across kit invocations, Starts and Stops
    alike (the service keeps one nonce sequence per signer for both).

    The service keeps its own nonce counter, apart from order entry's, so this never
    collides with the agent key's order nonces.
    """
    return time.time_ns() // 1000


def signed_body(primary: str, request: dict, key: str, address: str, info: ServiceInfo, **extra) -> dict:
    """Sign, then check the body the way the service will before it leaves this process."""
    body = {**extra, "request": request, "signature": sign(primary, request, key, info)}
    now = int(time.time())
    try:
        if primary == "StrategyStart":
            signer = verify_start_body(json.dumps(body), info, now)
        else:
            signer = verify_stop_body(request["runId"], body, info, now)
    except WorkflowError as exc:
        raise WorkflowError(f"The signed {primary} request does not verify ({exc}); nothing was sent") from None
    if signer != address:
        raise WorkflowError(f"The signed {primary} request does not verify; nothing was sent")
    return body


# --------------------------------------------------------------------------
# Read grant: what lets the kit read its runs once the service keeps them private
# --------------------------------------------------------------------------

# The run reads (`GET /v1/runs/{id}`, its activity and `/v1/accounts/{id}/runs`) and the checks that name an
# account (`/v1/validate`, `/v1/preview`) carry `Authorization: Bearer <grant>`. The grant is an EIP-712 ReadGrant
# under the start and stop domain, signed with the agent key (an approved delegate of the owner, never the owner's
# key): until `expiresAt` its bearer may read the owner's runs on `accountIds` (empty would mean all of the owner's
# accounts). The service accepts grants before it requires them; once it requires them it refuses a read with
# READ_GRANT_REQUIRED (none sent) or INVALID_READ_GRANT (one it does not take), and a run the grant does not cover
# reads as RUN_NOT_FOUND. A run read under a grant the service verified carries the run's `detail`.
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
CHECK_PATHS = ("/v1/validate", "/v1/preview")
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
    message = _signable("ReadGrant", {"ReadGrant": READ_GRANT_TYPE}, READ_GRANT_DOMAIN, grant)
    return "0x" + keccak(b"\x19" + message.version + message.header + message.body).hex()


def sign_read_grant(grant: dict, key: str) -> str:
    """The 65-byte r‖s‖v signature (v = 27 or 28). Errors never carry the key."""
    check_read_grant(grant)
    try:
        signed = Account.sign_message(
            _signable("ReadGrant", {"ReadGrant": READ_GRANT_TYPE}, READ_GRANT_DOMAIN, grant), key
        )
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

    Minted when a read first needs it, valid READ_GRANT_LIFETIME_S, and kept in the state directory
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
        """Forget the grant (the service or a gateway did not take it): this command sends it no more, and the next
        command signs a new one."""
        self._token = None
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


def _no_constants(text: str) -> Any:
    raise WorkflowError(f"The document has {text}, which is not JSON")


def _safe_integers(value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
        raise WorkflowError(f"The document has an integer beyond ±(2^53 - 1) ({value}); the service refuses it")
    for item in value.values() if isinstance(value, dict) else value if isinstance(value, list) else ():
        _safe_integers(item)


def parse_document(text: str) -> dict:
    if len(text.encode()) > MAX_DOCUMENT_BYTES:
        raise WorkflowError(f"The document is over {MAX_DOCUMENT_BYTES} bytes")
    try:
        doc = json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_float=_no_floats, parse_constant=_no_constants
        )
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"The document is not JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from None
    if not isinstance(doc, dict):
        raise WorkflowError("The document must be a JSON object")
    _safe_integers(doc)
    return doc


def load_document(path: str) -> tuple[str, dict]:
    """The document at `path` in its canonical form (the text the kit sends and the version hash is over), and
    parsed. The file itself may be spelled any way JSON allows."""
    try:
        with open(path, "rb") as source:
            raw = source.read(MAX_DOCUMENT_BYTES + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise WorkflowError(f"Cannot read the document {path}") from None
    doc = parse_document(text)
    check_schema(doc)
    return canonical_json(doc), doc


def check_schema(doc: dict) -> None:
    """Only `reya.workflow/3`: a document of the previous versions is refused before anything is sent."""
    schema = doc.get("schema")
    if schema == SCHEMA_TAG:
        return
    if schema in PREVIOUS_SCHEMAS:
        raise WorkflowError(
            f"This strategy is a {schema} document, from the previous version of strategies, which the service no "
            f"longer runs. Draft it again with strategy draft ({SCHEMA_TAG}).",
            code="PREVIOUS_VERSION",
        )
    raise WorkflowError(f"Not a strategy document: its schema is {schema!r}, not {SCHEMA_TAG}")


def canonical_json(doc: Any) -> str:
    """RFC 8785 for strategy documents: keys sorted, no whitespace, only strings, integers, booleans, arrays and
    objects (the service hashes the same form)."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def version_hash(canonical: str) -> str:
    """keccak256 of the canonical text: the version hash a Start signs."""
    return "0x" + keccak(text=canonical).hex()


def check_version_hash(text: str, claimed: Any) -> str:
    """The service's version hash for `text`, only when it is the hash of exactly that text. The agent key signs
    it, so one the kit cannot reproduce is refused, never signed."""
    if not isinstance(claimed, str) or not BYTES32_RE.fullmatch(claimed):
        raise WorkflowError("The service did not return the document's version hash; nothing was signed")
    if version_hash(text) != claimed.lower():
        raise WorkflowError(
            "The service's version hash is not the hash of this document; nothing was signed. Draft it again with "
            "strategy draft."
        )
    return claimed.lower()


def _obj(value: Any) -> dict:
    """`value` when it is a JSON object, else an empty one: the service's answers are data, read defensively."""
    return value if isinstance(value, dict) else {}


def _blocks(doc: dict, name: str) -> dict[str, dict]:
    value = doc.get(name)
    return {k: v for k, v in value.items() if isinstance(v, dict)} if isinstance(value, dict) else {}


def document_markets(doc: dict) -> list[str]:
    """The markets the document names, in the order it first names them (actions, then rules)."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            market = node.get("market")
            if isinstance(market, str) and SYMBOL_RE.fullmatch(market) and market not in found:
                found.append(market)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc.get("actions"))
    walk(doc.get("rules"))
    walk(doc.get("lists"))
    return found


def drafts_directory() -> Path:
    path = state_directory() / "workflows"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


# --------------------------------------------------------------------------
# Drafting: a basket, the shape the in-app chat drafts from a few fields
# --------------------------------------------------------------------------

DURATION_RE = re.compile(r"([1-9][0-9]{0,5})([smhdw])")
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
TRIGGER_SHAPE = "start, price:SYMBOL:up|down:LEVEL or every:PERIOD (with --rounds)"


@dataclass(frozen=True)
class Basket:
    """What `draft` writes: immediate orders on one or more markets, entered at Start, when a mark crosses a
    level, or every period for a number of rounds; then a stop-loss and a take-profit from the entry on each
    market, a time exit, and a loss limit, each optional."""

    orders: tuple[dict, ...]
    entry: dict
    stop_pct: str | None = None
    take_profit_pct: str | None = None
    exit_after_secs: int | None = None
    max_loss: str | None = None
    max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS

    def markets(self) -> list[str]:
        return list(dict.fromkeys(order["market"] for order in self.orders))


def market_symbol(text: str, flag: str) -> str:
    """`eth` or `ETHRUSDPERP` → `ETHRUSDPERP`, as every other kit command reads a market."""
    from agent_kit.scripts._common import symbol_arg

    symbol = symbol_arg(text)
    if not SYMBOL_RE.fullmatch(symbol):
        raise WorkflowError(f"{flag}: not a market: {text!r}")
    return symbol


def base_asset(market: str) -> str:
    return market.removesuffix("RUSDPERP") or market


def _number(text: str, flag: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise WorkflowError(f"{flag}: not a number: {text!r}") from None
    if not value.is_finite():
        raise WorkflowError(f"{flag}: not a number: {text!r}")
    return value


# The most integer digits an amount may have: far above any real size or price, and bounded before it is written out
# in full (`1e999999` would be a million digits).
MAX_AMOUNT_DIGITS = 30


def positive_amount(text: str, flag: str, *, places: int = 18) -> str:
    """A positive decimal in its canonical spelling (`500`, `0.1`), at most `places` decimal places. The places
    are counted on the exact value as given (`1.000` has none), never on a rounded one."""
    value = _number(text.strip(), flag)
    if value <= 0:
        raise WorkflowError(f"{flag}: {text} must be above 0")
    if value.adjusted() >= MAX_AMOUNT_DIGITS:
        raise WorkflowError(f"{flag}: {text} is too large")
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)  # finite: _number refuses the rest
    zeros = len(digits) - len("".join(map(str, digits)).rstrip("0"))
    if -(exponent + zeros) > places:
        raise WorkflowError(f"{flag}: {text} has more than {places} decimal places")
    return decimal_text(value)


def duration_secs(text: str, flag: str) -> int:
    """`30s`, `90m`, `4h`, `3d` or `1w`."""
    match = DURATION_RE.fullmatch(text.strip().lower())
    if not match:
        raise WorkflowError(f"{flag}: a duration is a whole number of s, m, h, d or w (90m, 4h, 3d, 1w); got {text!r}")
    return int(match.group(1)) * DURATION_UNITS[match.group(2)]


def parse_trigger(spec: str) -> dict:
    """`start`, `price:SYMBOL:up|down:LEVEL` (the mark crosses LEVEL) or `every:PERIOD`."""
    kind, _, rest = spec.strip().partition(":")
    kind = kind.lower()
    if kind == "start" and not rest:
        return {"kind": "start"}
    if kind == "price":
        parts = [part.strip() for part in rest.split(":")]
        if len(parts) != 3 or not all(parts):
            raise WorkflowError(f"--trigger takes {TRIGGER_SHAPE}; got {spec!r}")
        market, direction, level = parts
        if direction.lower() not in ("up", "down"):
            raise WorkflowError(f"--trigger price: the direction is up or down, not {direction!r}")
        return {
            "kind": "price",
            "market": market_symbol(market, "--trigger"),
            "direction": direction.lower(),
            "level": positive_amount(level, "--trigger level", places=9),
        }
    if kind == "every" and rest:
        return {"kind": "every", "period_secs": duration_secs(rest, "--trigger every")}
    raise WorkflowError(f"--trigger takes {TRIGGER_SHAPE}; got {spec!r}")


# A size with a unit after the number: a base quantity when the unit is the order's market's base asset (`1SOL`).
UNIT_SIZE_RE = re.compile(r"(.*?[0-9.])\s*([A-Za-z][A-Za-z0-9]*)")


def parse_size(text: str, market: str) -> dict:
    """`500` (USD) or a base quantity: the number with the order's market's base asset after it (`1SOL`,
    `0.05btc`). A bare number is always USD."""
    text = text.strip()
    if text.endswith("%"):
        raise WorkflowError("--order: a size is USD (500) or a base quantity (1SOL), not a weight")
    try:
        Decimal(text)
    except InvalidOperation:
        unit = UNIT_SIZE_RE.fullmatch(text)
        if not unit:
            raise WorkflowError(f"--order: not a size: {text!r}") from None
        if unit.group(2).upper() in ("USD", "USDC", "RUSD"):
            raise WorkflowError(f"--order: USD is a plain number, without a unit: {unit.group(1)}, not {text!r}")
        base = base_asset(market)
        number = text[: -len(base)].strip() if text.upper().endswith(base) else ""
        if not number:
            raise WorkflowError(
                f"--order: {unit.group(2).upper()} in {text!r} is not {market}'s base asset ({base}): give a quantity "
                f"in {base} ({unit.group(1)}{base}) or USD as a plain number (500)"
            )
        return {"qty": positive_amount(number, "--order quantity")}
    return {"rusd": positive_amount(text, "--order")}


def parse_order(spec: str) -> dict:
    """`--order SYMBOL:buy|sell:USD|QTY<ASSET>`: an immediate order, sized in USD or as a base quantity."""
    parts = [part.strip() for part in spec.split(":")]
    if len(parts) != 3 or not all(parts):
        raise WorkflowError(f"--order takes SYMBOL:buy|sell:USD|QTY<ASSET>; got {spec!r}")
    market, side, size = parts
    symbol = market_symbol(market, "--order")
    if side.lower() not in ("buy", "sell"):
        raise WorkflowError(f"--order: the side is buy or sell, not {side!r}")
    return {"market": symbol, "side": side.lower(), "size": parse_size(size, symbol)}


def basket(
    *,
    orders: list[dict],
    trigger: dict,
    rounds: int | None = None,
    first_now: bool = False,
    stop_pct: str | None = None,
    take_profit_pct: str | None = None,
    exit_after_secs: int | None = None,
    max_loss: str | None = None,
    max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS,
) -> Basket:
    """The basket the draft flags describe, refused here with what to change where the document would break one of
    the format's rules the kit can see without the service."""
    if not orders:
        raise WorkflowError("Give at least one --order")
    seen: dict[str, int] = {}
    for index, order in enumerate(orders):
        if order["market"] in seen:
            raise WorkflowError(
                f"{base_asset(order['market'])} is given twice (orders {seen[order['market']] + 1} and {index + 1}): "
                "give each market once, with the one size meant"
            )
        seen[order["market"]] = index
    if len(orders) > MAX_ACTIONS:
        raise WorkflowError(f"At most {MAX_ACTIONS} orders")
    if not 1 <= max_slippage_bps <= MAX_SLIPPAGE_BPS:
        raise WorkflowError(f"--max-slippage-bps is 1 to {MAX_SLIPPAGE_BPS}")
    every = trigger["kind"] == "every"
    if every:
        if rounds is None:
            raise WorkflowError(
                "--trigger every needs --rounds: how many times in all it buys (1 to 100); a strategy never repeats "
                "without a limit"
            )
        if not 1 <= rounds <= MAX_ROUNDS + (1 if first_now else 0):
            raise WorkflowError(f"--rounds is 1 to {MAX_ROUNDS}")
        if exit_after_secs is not None:
            raise WorkflowError("--exit-after needs a --trigger of start or price, not every")
    elif rounds is not None or first_now:
        raise WorkflowError("--rounds and --first-now go with --trigger every")
    markets = len(seen)
    exits = markets * ((stop_pct is not None) + (take_profit_pct is not None)) + (exit_after_secs is not None)
    # The repeating entry's rule sits beside the exits only while a timed round remains: one round in all, at the
    # start (`--rounds 1 --first-now`), has none.
    timed = every and rounds is not None and rounds - (1 if first_now else 0) > 0
    if exits + timed > MAX_RULES_PER_STATE:
        raise WorkflowError(
            f"A stop-loss and a take-profit on each market, with the time exit or a repeating entry, make "
            f"{exits + timed} rules on one step, and a step takes at most {MAX_RULES_PER_STATE}: use fewer markets or "
            "fewer exits"
        )
    if every:
        entry = {"kind": "every", "period_secs": trigger["period_secs"], "rounds": rounds, "first_now": first_now}
    else:
        entry = dict(trigger)
    return Basket(
        orders=tuple(orders),
        entry=entry,
        stop_pct=stop_pct,
        take_profit_pct=take_profit_pct,
        exit_after_secs=exit_after_secs,
        max_loss=max_loss,
        max_slippage_bps=max_slippage_bps,
    )


def basket_document(spec: Basket) -> dict:
    """The `reya.workflow/3` document of a basket, its ids numbered in the format's walk order (`remint`).

    The same shape as the in-app chat's `basket`: the entry rule sends every order at once; the exits (a stop-loss
    and a take-profit from the entry on each market, then the time exit) go to a final `done`, which closes what
    the strategy holds. A repeating entry is a loop of `rounds` rounds that then holds what it bought, its stops
    still watched."""
    counter = iter(range(1, 10_000))
    actions: dict[str, dict] = {}
    rules: dict[str, dict] = {}
    states: dict[str, dict] = {}

    def new(prefix: str) -> str:
        return f"{prefix}{next(counter)}"

    order_ids = []
    for order in spec.orders:
        action = new("a_")
        actions[action] = {
            "kind": "order",
            "market": order["market"],
            "side": order["side"],
            "size": dict(order["size"]),
            "max_slippage_bps": spec.max_slippage_bps,
        }
        order_ids.append(action)
    elapsed = spec.exit_after_secs is not None
    has_exits = spec.stop_pct is not None or spec.take_profit_pct is not None or elapsed
    done = new("st_") if has_exits else None
    exit_rules: list[str] = []
    for kind, pct in (("stop_loss", spec.stop_pct), ("take_profit", spec.take_profit_pct)):
        if pct is None:
            continue
        for market in spec.markets():
            rule = new("r_")
            level = new("lv_")
            rules[rule] = {
                "when": {
                    "kind": kind,
                    "market": market,
                    "levels": [{"id": level, "at": {"kind": "from_entry", "pct": pct}}],
                },
                "go": done,
            }
            exit_rules.append(rule)
    if elapsed:
        rule = new("r_")
        rules[rule] = {"when": {"kind": "elapsed", "after_secs": spec.exit_after_secs}, "go": done}
        exit_rules.append(rule)
    if done is not None:
        states[done] = {"kind": "final", "outcome": "done"}

    if spec.entry["kind"] == "every":
        rounds = int(spec.entry["rounds"])
        first_now = bool(spec.entry["first_now"])
        timed = rounds - 1 if first_now else rounds
        held = new("st_")
        states[held] = {"kind": "final", "outcome": "hold"} | ({"on": list(exit_rules)} if exit_rules else {})
        start = None
        if timed > 0:
            loop = new("st_")
            buy = new("r_")
            rules[buy] = {
                "when": {"kind": "every", "period_secs": spec.entry["period_secs"]},
                "do": order_ids,
                "go": loop,
            }
            states[loop] = {"kind": "loop_head", "on": exit_rules + [buy], "rounds": timed, "then": held}
            start = loop
        if first_now:
            first = new("st_")
            first_buy = new("r_")
            rules[first_buy] = {"when": {"kind": "start"}, "do": order_ids, "go": start or held}
            states[first] = {"kind": "normal", "on": [first_buy]}
            start = first
    else:
        wait = new("st_")
        opened = new("st_")
        entry = new("r_")
        if spec.entry["kind"] == "start":
            when: dict = {"kind": "start"}
        else:
            when = {
                "kind": "cross",
                "value": {"kind": "series", "market": spec.entry["market"], "series": "mark"},
                "direction": spec.entry["direction"],
                "level": {"kind": "num", "value": spec.entry["level"]},
            }
        rules[entry] = {"when": when, "do": order_ids, "go": opened}
        states[wait] = {"kind": "normal", "on": [entry]}
        states[opened] = {"kind": "normal", "on": exit_rules} if exit_rules else {"kind": "final", "outcome": "hold"}
        start = wait
    doc: dict[str, Any] = {
        "schema": SCHEMA_TAG,
        "start": start,
        "states": states,
        "rules": rules,
        "actions": actions,
        "on_liquidation": ON_LIQUIDATION,
        "on_margin_skip": ON_MARGIN_SKIP,
    }
    if spec.max_loss is not None:
        doc["max_loss_rusd"] = spec.max_loss
    return remint(doc)


# The block kinds in workflow-core's table order (`IdKind`), by id prefix: a block the walk does not reach is
# numbered after the rest in this order.
ID_KINDS = ("st_", "r_", "a_", "o_", "l_", "m_", "lv_", "w_", "se_", "oo_")


def _prefix(block_id: str) -> str:
    return block_id[: block_id.index("_") + 1]


def _kind_rank(block_id: str) -> int:
    prefix = block_id[: block_id.find("_") + 1]
    return ID_KINDS.index(prefix) if prefix in ID_KINDS else len(ID_KINDS)


def _levels(rule: dict) -> list[dict]:
    when = _obj(rule.get("when"))
    levels = when.get("levels") if when.get("kind") in ("stop_loss", "take_profit") else None
    if not isinstance(levels, list):
        return []
    return [level for level in levels if isinstance(level, dict) and isinstance(level.get("id"), str)]


def _ids(value: Any) -> list[str]:
    """The ids in a list of references (`on`, `do`); anything else reads as none, so a malformed document the
    service will refuse still renders."""
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def walk_order(doc: dict) -> list[str]:
    """Every block id of a document without lists or series in the format's walk order (workflow-core
    `ids::walk_order`): breadth-first from `start`, a state, then each rule in its `on` order (the rule, its stop
    levels, then its actions in `do` order), queueing the rule's `go`, then a loop head's `then`; any block the walk
    does not reach last, by kind, then by id."""
    states, rules, actions = _blocks(doc, "states"), _blocks(doc, "rules"), _blocks(doc, "actions")
    order: list[str] = []
    seen: set[str] = set()

    def number(block_id: str) -> bool:
        if block_id in seen:
            return False
        seen.add(block_id)
        order.append(block_id)
        return True

    queue: deque[Any] = deque([doc.get("start")])
    while queue:
        state_id = queue.popleft()
        state = states.get(state_id) if isinstance(state_id, str) else None
        if state is None or not number(state_id):
            continue
        for rule_id in _ids(state.get("on")):
            rule = rules.get(rule_id)
            if rule is None or not number(rule_id):
                continue
            for level in _levels(rule):
                number(level["id"])
            for action_id in _ids(rule.get("do")):
                if action_id in actions:
                    number(action_id)
            queue.append(rule.get("go"))
        if state.get("kind") == "loop_head" and isinstance(state.get("then"), str):
            queue.append(state["then"])
    defined = list(states) + list(rules) + [level["id"] for rule in rules.values() for level in _levels(rule)]
    defined += list(actions)
    rest = sorted((i for i in defined if i not in seen), key=lambda i: (_kind_rank(i), i))
    return order + rest


def remint(doc: dict) -> dict:
    """The document with every block numbered from 1 in walk order, one counter for every kind, and every
    reference rewritten (§1.6 "Remint on import"). Only for the documents `draft` writes: no lists, series or
    resting orders."""
    if any(key in doc for key in ("lists", "series")) or any(
        a.get("kind") == "limit_orders" for a in _blocks(doc, "actions").values()
    ):
        raise WorkflowError("The kit renumbers only the baskets it drafts")
    names = {old: f"{_prefix(old)}{n}" for n, old in enumerate(walk_order(doc), start=1)}

    def state(value: dict) -> dict:
        value = dict(value)
        if "on" in value:
            value["on"] = [names[r] for r in value["on"]]
        if "then" in value:
            value["then"] = names[value["then"]]
        return value

    def rule(value: dict) -> dict:
        value = dict(value, go=names[value["go"]])
        if "do" in value:
            value["do"] = [names[a] for a in value["do"]]
        if _levels(value):
            when = dict(value["when"])
            when["levels"] = [dict(level, id=names[level["id"]]) for level in when["levels"]]
            value["when"] = when
        return value

    result = dict(doc, start=names[doc["start"]])
    result["states"] = {names[k]: state(v) for k, v in _blocks(doc, "states").items()}
    result["rules"] = {names[k]: rule(v) for k, v in _blocks(doc, "rules").items()}
    result["actions"] = {names[k]: v for k, v in _blocks(doc, "actions").items()}
    return json.loads(canonical_json(result))


def parse_expiry(text: str, now: int | None = None) -> int:
    """`7d`, `12h`, `90m`, `2w`, Unix seconds, or an ISO 8601 time with a zone; at least an hour and at most 365
    days ahead."""
    now = int(time.time()) if now is None else now
    text = text.strip()
    if DURATION_RE.fullmatch(text.lower()):
        at = now + duration_secs(text, "--expires")
    elif text.isascii() and text.isdecimal():
        at = int(text)
    else:
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise WorkflowError(f"--expires takes 7d, 12h, 90m, Unix seconds or an ISO time; got {text!r}") from None
        if moment.tzinfo is None:
            raise WorkflowError("An ISO expiry needs a time zone, e.g. 2026-12-01T00:00:00Z")
        at = int(moment.timestamp())
    if at < now + MIN_EXPIRY_S:
        raise WorkflowError("--expires is at least an hour ahead")
    if at > now + MAX_EXPIRY_S - EXPIRY_SKEW_S:
        raise WorkflowError(
            "--expires is at most 365 days ahead, less 5 minutes for the service's clock: 364d, or a time before then"
        )
    return at


# --------------------------------------------------------------------------
# The document in plain sentences
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


def duration_text(secs: Any) -> str:
    """`3 days`, `90 minutes`, `1 day`."""
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        return str(secs)
    for unit, name in ((7 * 86400, "week"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= unit and secs % unit == 0:
            count = secs // unit
            return f"1 {name}" if count == 1 else f"{count} {name}s"
    return f"{secs} seconds"


def size_text(size: Any, market: Any) -> str:
    if not isinstance(size, dict):
        return "an unknown size"
    if isinstance(size.get("rusd"), str):
        return f"{usd_text(size['rusd'])} of {base_asset(str(market))}" if market else usd_text(size["rusd"])
    if isinstance(size.get("qty"), str):
        return f"{group(size['qty'])} {base_asset(str(market))}" if market else f"{group(size['qty'])} (base)"
    if "row" in size:
        return "the row's size"
    return "a size from an expression"


SERIES_TEXTS = {
    "mark": "mark price",
    "funding_rate": "funding rate",
    "funding_rate_avg_15m": "15-minute average funding rate",
    "funding_paid": "funding paid",
}
OP_TEXTS = {
    "add": "+",
    "sub": "-",
    "mul": "×",
    "div": "÷",
    "gt": ">",
    "ge": "≥",
    "lt": "<",
    "le": "≤",
    "eq": "=",
    "ne": "≠",
    "and": "and",
    "or": "or",
}


def expr_text(expr: Any) -> str:
    """An expression in plain words, for the rules' lines. A shape the kit does not know reads as its kind."""
    if not isinstance(expr, dict):
        return "?"
    kind = expr.get("kind")
    if kind == "num":
        return group(expr.get("value"))
    if kind == "series":
        series = SERIES_TEXTS.get(str(expr.get("series")), str(expr.get("series")))
        market = expr.get("market")
        return f"{base_asset(market)} {series}" if isinstance(market, str) else f"the row's {series}"
    if kind == "input":
        name = str(expr.get("name"))
        market = expr.get("market")
        return f"{base_asset(market)} {name}" if isinstance(market, str) else name
    if kind == "outside":
        return f"outside series {expr.get('series')}"
    if kind == "row":
        return f"the row's {expr.get('row')}"
    if kind == "op":
        # A nested operation is parenthesised, so `(a or b) and c` never reads as `a or b and c`.
        raw: list = expr["args"] if isinstance(expr.get("args"), list) else []
        op = str(expr.get("op"))
        if op in ("neg", "not") and len(raw) == 1:
            return f"{'-' if op == 'neg' else 'not '}({expr_text(raw[0])})"
        args = [
            f"({expr_text(arg)})" if isinstance(arg, dict) and arg.get("kind") == "op" else expr_text(arg)
            for arg in raw
        ]
        if len(args) == 2:
            return f"{args[0]} {OP_TEXTS.get(op, op)} {args[1]}"
        return f"{op}({', '.join(args)})"
    if kind == "fn":
        return f"{expr.get('fn')}({', '.join(expr_text(arg) for arg in expr.get('args') or [])})"
    return str(kind)


def level_text(level: Any) -> str:
    at = level.get("at") if isinstance(level, dict) else None
    if not isinstance(at, dict):
        return "?"
    if at.get("kind") == "price":
        return f"at {group(at.get('price'))}"
    if at.get("kind") == "from_entry":
        if "pct" in at:
            return f"{at['pct']}% from the entry"
        return f"{group(at.get('distance'))} from the entry"
    return str(at.get("kind"))


def trigger_text(when: Any) -> str:
    if not isinstance(when, dict):
        return "?"
    kind = when.get("kind")
    market = when.get("market")
    on = f" on {base_asset(market)}" if isinstance(market, str) else ""
    if kind == "start":
        return "When the strategy starts"
    if kind == "cross":
        direction = "above" if when.get("direction") == "up" else "below"
        band = f" (fires again only after moving back {when['band']})" if isinstance(when.get("band"), str) else ""
        return f"When {expr_text(when.get('value'))} crosses {direction} {expr_text(when.get('level'))}{band}"
    if kind == "every":
        return f"Every {duration_text(when.get('period_secs'))}"
    if kind == "at":
        day = f" on {when['day']}" if isinstance(when.get("day"), str) else ""
        return f"Every {'week' if when.get('cadence') == 'weekly' else 'day'}{day} at {when.get('time')} UTC"
    if kind == "elapsed":
        return f"Once this step has lasted {duration_text(when.get('after_secs'))}"
    if kind in ("stop_loss", "take_profit"):
        name = "stop-loss" if kind == "stop_loss" else "take-profit"
        levels = "; ".join(level_text(level) for level in when.get("levels") or [])
        return f"When the {name}{on} is hit ({levels})"
    if kind == "filled":
        return f"When resting order {when.get('order')} fills"
    return f"On {kind}"


def action_text(action: Any) -> str:
    if not isinstance(action, dict):
        return "?"
    kind = action.get("kind")
    market = action.get("market")
    if kind == "order":
        side = action.get("side") if isinstance(action.get("side"), str) else "buy or sell (the row's side)"
        slippage = action.get("max_slippage_bps")
        return f"{side} {size_text(action.get('size'), market)} at once (max slippage {slippage} bps)"
    if kind == "limit_orders":
        return f"place {len(action.get('orders') or [])} resting limit orders"
    if kind == "close":
        return f"close the strategy's {base_asset(market) if isinstance(market, str) else 'row'} position"
    if kind == "reduce":
        return f"reduce the strategy's {base_asset(market) if isinstance(market, str) else 'row'} position"
    return str(kind)


def state_text(state_id: str, state: dict) -> str:
    kind = state.get("kind")
    if kind == "loop_head":
        rounds = state.get("rounds")
        then = state.get("then")
        if rounds is not None:
            return f"{state_id} (a loop of at most {rounds} rounds, then {then})"
        return f"{state_id} (a loop)"
    if kind == "final":
        if state.get("outcome") == "hold":
            return f"{state_id} (the end: it keeps the positions and opens nothing new)"
        return f"{state_id} (the end: it closes what the strategy holds and is done)"
    return state_id


def rules(doc: dict) -> list[str]:
    """The document's rules in plain sentences, step by step in walk order from the start, then its settings."""
    states, rule_blocks, actions = _blocks(doc, "states"), _blocks(doc, "rules"), _blocks(doc, "actions")
    order = [i for i in walk_order(doc) if i in states] if "lists" not in doc else list(states)
    lines = [f"Starts in {state_text(str(doc.get('start')), states.get(str(doc.get('start'))) or {})}."]
    for state_id in order:
        state = states[state_id]
        on = [r for r in _ids(state.get("on")) if r in rule_blocks]
        for rule_id in on:
            rule = rule_blocks[rule_id]
            do = [action_text(actions.get(a)) for a in _ids(rule.get("do"))]
            condition = f", if {expr_text(rule['if'])}" if "if" in rule else ""
            go = rule.get("go")
            target = states.get(go) if isinstance(go, str) else None
            then = state_text(str(go), target) if target is not None else str(go)
            if go == state_id and state.get("kind") == "loop_head":
                then = f"another round of {state_id}"
            what = f"{', then '.join(do)}, then go to {then}" if do else f"go to {then}"
            lines.append(f"In {state_id}: {trigger_text(rule.get('when'))}{condition}: {what}.")
        if state.get("kind") == "final" and state_id != doc.get("start"):
            lines.append(f"{state_text(state_id, state)}.")
    settings = []
    if isinstance(doc.get("max_loss_rusd"), str):
        settings.append(f"max loss {usd_text(doc['max_loss_rusd'])}: past it, everything is closed and it ends")
    if doc.get("on_liquidation") == "close_and_end":
        settings.append("if liquidated, it closes and ends")
    if doc.get("on_margin_skip") == "keep_running":
        settings.append("an order skipped for margin is skipped and it keeps running")
    if settings:
        lines.append("Settings: " + "; ".join(settings) + ".")
    return lines


def document_view(doc: dict) -> dict:
    """The document's markets and rules. A document the kit cannot render (the service refuses it, and says why)
    shows no rules rather than failing the command."""
    try:
        lines = rules(doc)
    except (TypeError, KeyError, AttributeError, ValueError):
        lines = []
    return {"schema": SCHEMA_TAG, "markets": document_markets(doc), "rules": lines}


# --------------------------------------------------------------------------
# A check's answer (validate, preview): reya.check-result/3
# --------------------------------------------------------------------------

ISSUE_FIELDS = ("code", "outcome", "message", "path", "block_id", "market", "value", "units", "live_would_refuse")
# The readouts a check returns, in the order the user reads them.
READOUTS = ("margin_needed", "additional_deposit_needed", "worst_deployed", "liquidation", "max_loss")


def _text(value: Any, limit: int = 500) -> str:
    return " ".join(str(value).split())[:limit]


def issue_view(issue: Any) -> dict:
    if not isinstance(issue, dict):
        return {"message": _text(issue)}
    view = {key: issue[key] for key in ISSUE_FIELDS if key in issue and issue[key] is not None}
    if "message" in view:
        view["message"] = _text(view["message"])
    return view


FIGURE_BOUNDS = {"at_most": "at most ", "at_least": "at least "}


def figure_text(figure: Any) -> str | None:
    """A readout figure (`{kind, value, unit}`): its value with its unit, or why it is unknown."""
    if not isinstance(figure, dict):
        return None
    kind = figure.get("kind")
    value = figure.get("value")
    if kind == "unknown":
        return f"unknown ({figure.get('reason')})" if figure.get("reason") else "unknown"
    if isinstance(value, str) and SIGNED_DECIMAL_RE.fullmatch(value):
        text = usd_text(value) if figure.get("unit") == "rusd" else group(value)
        return f"{FIGURE_BOUNDS.get(str(kind), '')}{text}"
    return None


def readouts_view(readouts: Any) -> dict:
    """The check's main figures in plain text; the service's readouts stay in `readouts` as they are."""
    if not isinstance(readouts, dict):
        return {}
    view: dict[str, Any] = {}
    margin = readouts.get("margin_needed")
    if isinstance(margin, dict):
        text = figure_text(margin.get("total"))
        if text:
            view["margin_needed"] = text
    for key in ("additional_deposit_needed", "worst_deployed"):
        text = figure_text(readouts.get(key))
        if text:
            view[key] = text
    return view


def check_view(answer: dict, text: str) -> dict:
    """A validate or preview answer, for the user: the verdict, the issues by outcome, the main figures, what the
    strategy does if each thing happens, and (a preview's) the preview record a Start signs."""
    result = _obj(answer.get("result"))
    if result.get("schema") not in (None, CHECK_SCHEMA):
        raise WorkflowError(f"The service answered with a {result.get('schema')} check this kit cannot read")
    issues = [issue_view(i) for i in result.get("issues") or []]
    view: dict[str, Any] = {
        "verdict": result.get("verdict"),
        "check_mode": result.get("mode"),
        "version_hash": check_version_hash(text, answer.get("version_hash")),
        "refusals": [i for i in issues if i.get("outcome") == "refusal"],
        "warnings": [i for i in issues if i.get("outcome") == "warning"],
        "notes": [i for i in issues if i.get("outcome") not in ("refusal", "warning")],
        "figures": readouts_view(result.get("readouts")),
        "readouts": result.get("readouts") if isinstance(result.get("readouts"), dict) else {},
        "what_if": [row for row in answer.get("what_if") or [] if isinstance(row, dict)],
    }
    for key in ("takes_over", "paper", "paper_to_live"):
        if isinstance(answer.get(key), dict):
            view[key] = answer[key]
    for key in ("preview_id", "strategy_id", "valid_until"):
        value = answer.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and UINT_RE.fullmatch(str(value)):
            view[key] = str(value)
    if "valid_until" in view:
        view["valid_until_iso"] = iso(view["valid_until"])
    return view


def refused(view: dict) -> bool:
    return view.get("verdict") == "refusal" or bool(view.get("refusals"))


# --------------------------------------------------------------------------
# Runs, as the service reads them
# --------------------------------------------------------------------------

STATE_TEXTS = {
    "active": "running",
    "held": "holding its positions, watching its protective rules",
    "stopped": "stopped",
    "completed": "completed",
}
STATUS_TEXTS = {
    "running": "running",
    "holding": "holding",
    "closing": "closing its positions",
    "ended": "ended",
}


def strategy_name(run_id: Any, label: Any) -> str:
    return f"#{run_id} {label}" if isinstance(label, str) and label else f"#{run_id}"


def summary_row(run: dict) -> dict:
    """One run as a listing shows it (V3RunSummary)."""
    mode = run.get("mode")
    row: dict[str, Any] = {
        "run_id": run.get("run_id"),
        "strategy_id": run.get("strategy_id"),
        "label": run.get("label"),
        "mode": mode,
        "mode_name": MODE_NAMES.get(str(mode), mode),
        "state": run.get("state"),
        "status": run.get("status"),
        "end_reason": run.get("end_reason"),
        "markets": run.get("markets") if isinstance(run.get("markets"), list) else [],
        "holds_markets": run.get("holds_markets") if isinstance(run.get("holds_markets"), list) else [],
        "since": iso(run.get("since")),
        "expires": iso(run.get("expires")),
        "needs_you_count": run.get("needs_you_count"),
    }
    state = STATE_TEXTS.get(str(run.get("state")), str(run.get("state")))
    status = run.get("status")
    if status == "closing":
        state = STATUS_TEXTS["closing"]
    end = f" ({run['end_reason']})" if isinstance(run.get("end_reason"), str) else ""
    markets = ", ".join(base_asset(str(m)) for m in row["markets"]) or "no market"
    row["text"] = f"{strategy_name(run.get('run_id'), run.get('label'))}: {row['mode_name']}, {state}{end}, {markets}"
    return row


def is_running(run: dict) -> bool:
    """A run that has not ended: running, or holding after a final `hold`."""
    return run.get("state") in RUNNING_STATES


def run_view(run: dict) -> dict:
    """A run read (V3Run): its summary, and, under a read grant the service verified, its result, positions,
    protection and Needs-you items."""
    view = summary_row(run)
    detail = run.get("detail")
    if not isinstance(detail, dict):
        view["detail"] = None
        view["detail_reason"] = run.get("detail_reason")
        return view
    result = _obj(detail.get("result"))
    view["result"] = {
        key: result.get(key)
        for key in ("basis", "known", "unknown_reason", "total", "settled", "unrealised", "parts", "by_market")
        if key in result
    }
    view["positions"] = [p for p in detail.get("positions") or [] if isinstance(p, dict)]
    protection = detail.get("protection")
    view["protection"] = protection if isinstance(protection, dict) else None
    view["needs_you"] = [n for n in detail.get("needs_you") or [] if isinstance(n, dict)]
    paper = detail.get("paper")
    if isinstance(paper, dict):
        view["paper"] = {
            key: paper.get(key) for key in ("settings", "balance", "balance_reason", "resting_orders") if key in paper
        }
    strategy = _obj(detail.get("strategy"))
    document = strategy.get("document")
    if isinstance(document, str):
        try:
            view["rules"] = rules(parse_document(document))
        except WorkflowError:
            pass
    view["version_hash"] = strategy.get("version_hash")
    view["ended_at"] = iso(detail.get("ended_at"))
    return view


def activity_view(entry: dict) -> dict:
    view = {key: entry.get(key) for key in ("code", "event", "rule", "reason") if entry.get(key) is not None}
    view["at"] = iso(entry.get("at"))
    if isinstance(view.get("reason"), str):
        view["reason"] = _text(view["reason"])
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
    """One request per call: no redirects, no retries, writes never repeated. The one exception is a read (a run
    read, or a check that names an account) that carried a read grant and was refused without the service's error
    envelope (see request)."""

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

    def _wants_grant(self, method: str, path: str, body: dict | None) -> bool:
        if method == "GET":
            return bool(READ_GRANT_PATH_RE.fullmatch(path))
        return method == "POST" and path in CHECK_PATHS and isinstance(body, dict) and "account_id" in body

    async def request(self, method: str, path: str, *, body: dict | None = None, params: dict | None = None) -> Any:
        write = method == "POST" and path not in CHECK_PATHS
        grant = None
        if self.read_grants is not None and self._wants_grant(method, path, body):
            grant = self.read_grants.header()
        status, headers, raw = await self._exchange(method, path, body, params, write, grant)
        if grant is not None and 400 <= status < 500 and status != 429:
            assert self.read_grants is not None
            refusal = _envelope(status, _json_or_none(raw), headers)
            if refusal is None:
                # Not the service's answer: a gateway that does not take the header. Until the service requires
                # grants, ask once more without it (and sign a new one next time), so a grant never costs the
                # answer. The service's own refusals (INVALID_READ_GRANT, READ_GRANT_REQUIRED, RUN_NOT_FOUND, ...)
                # stand: asking again without the grant would only hide why. Only reads get here: a check writes
                # nothing.
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

    async def validate(self, request: dict) -> dict:
        """A check of `request` (CheckRequest's fields: `document`, and `account_id`, `mode` ...)."""
        return await self.object("POST", "/v1/validate", body=request)

    async def preview(self, request: dict) -> dict:
        """The same check as validate, with what it would do now; a Paper or Live preview that does not refuse
        answers a `preview_id` a Start signs."""
        return await self.object("POST", "/v1/preview", body=request)

    async def start(self, body: dict) -> dict:
        return await self.object("POST", "/v1/runs", body=body)

    async def stop(self, run_id: int, body: dict) -> dict:
        return await self.object("POST", f"/v1/runs/{run_id}/stop", body=body)

    async def run(self, run_id: int) -> dict:
        return await self.object("GET", f"/v1/runs/{run_id}")

    async def account_runs(self, account_id: int, *, state: str, limit: int = 50, before: str | None = None) -> dict:
        params = {"state": state, "limit": str(limit)} | ({"before": before} if before else {})
        return await self.object("GET", f"/v1/accounts/{account_id}/runs", params=params)

    async def activity(self, run_id: int, *, limit: int = 20) -> dict:
        """The run's newest `limit` logged commits, oldest first (one page)."""
        page = await self.object("GET", f"/v1/runs/{run_id}/activity", params={"limit": str(limit)})
        if not isinstance(page.get("activity"), list):
            raise WorkflowError("The strategy service returned an unreadable activity page")
        return page
