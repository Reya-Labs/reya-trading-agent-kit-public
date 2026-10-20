"""Client for Reya's hosted workflow service: documents, signed start and stop, run status.

The service is the only evaluator. `validate` and `preview` ask it; the kit never
decides locally whether a trigger would fire, and there is no replay. Start and
stop are EIP-712 messages signed with the agent key from `agent init`, never the
owner's key, under the domain and types the service publishes at `/v1/info`. The
kit refuses any account that `agent init` did not record as the Agent account.

Signing is local (eth_account); nothing here creates an RPC provider or a
transaction.
"""

from typing import Any

import asyncio
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
# A base path in REYA_WORKFLOWS_URL: plain segments only, so `base + "/v1/..."` names exactly one
# path (no empty, dot or escaped segments for a server to normalise differently).
PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_~-][A-Za-z0-9._~-]*")
BYTES32_RE = re.compile(r"0x[0-9a-fA-F]{64}")
SIGNATURE_RE = re.compile(r"0x[0-9a-fA-F]{130}")

# Workflow document v1 (the service's `workflow-v1` JSON Schema): the one template and its defaults.
SCHEMA_TAG = "reya.workflow/1"
TEMPLATE = {"id": "conditioned_purchase", "version": 1}
DEFAULT_BOUND_BPS = 25
MIN_BOUND_BPS, MAX_BOUND_BPS = 10, 100
COMPARISONS = ("lt", "lte", "gt", "gte")
COMPARISON_WORDS = {"lt": "below", "lte": "at or below", "gt": "above", "gte": "at or above"}

# Named reasons, keyed by the service's stable codes. The service's own fixed text is always
# shown next to the label; a code the kit does not know is shown as itself.
SKIP_REASONS = {
    "skip_condition_funding": "funding condition not met",
    "skip_funding_unavailable": "funding average unavailable",
    "skip_condition_position": "position condition not met",
    "skip_condition_balance": "balance condition not met",
    "skip_balance_unavailable": "available balance unavailable",
    "skip_firing_held": "firing held by an operator",
    "skip_below_minimum": "order below the market minimum",
    "skip_collar_too_tight": "market price band narrower than the bound",
    "skip_price_out_of_range": "price outside the venue's range",
    "crossed_while_stale": "crossed while data was stale, not fired",
}
STALE_REASONS = {
    "mark_stale": "mark price is stale",
    "mark_from_future": "mark price is stamped in the future",
    "venue_halted": "venue halted",
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
    "expired": "the workflow expired",
    "operator": "stopped by a Reya operator",
    "protection_fired": "the run's protective stop fired",
}
COMPLETION_REASONS = {
    "fired_once": "its once step fired",
    "allocation_used": "its allocation is used",
}
PHASES = {
    "starting": "starting: waiting for the first fresh mark",
    "armed": "armed: watching for a crossing",
    "waiting_for_reset": "waiting: the mark is already past the level and must move back past the re-arm band",
    "cooling": "cooling down after a crossing, until the cooldown and the re-arm band are both met",
    "done": "done: fires no more",
}

# Hints for the service's refusals. Codes the kit does not list still show the service's message.
ERROR_HINTS = {
    "NOT_AGENT_ACCOUNT": "The service does not see this account as the Agent account. Run agent status.",
    "NOT_PERMITTED": "The agent key is not an approved signer for this account's owner. Run agent status.",
    "HOSTED_SIGNER_NOT_ENABLED": (
        "Hosted runs need one more approval in the Reya app: approve the hosted signer (`hosted_signer`) "
        "the same way you approved the agent key, then start again."
    ),
    "UNKNOWN_HOSTED_SIGNER": "The hosted signer changed; run the start again so it signs the current one.",
    "ACCOUNT_HAS_ACTIVE_RUN": "One active workflow per account: see workflow status, or stop it first.",
    "NONCE_TOO_LOW": "Nothing was started or stopped. Run the same command again for a fresh nonce.",
    "REQUEST_EXPIRED": "Nothing was started or stopped. Check this computer's clock, then run the command again.",
    "MODE_NOT_ALLOWED": "This environment does not allow that mode; workflow info lists the modes it allows.",
    "ABOVE_PILOT_LIMIT": "Lower the allocation or order size to the pilot caps (workflow info).",
    "DOCUMENT_MISMATCH": "The signed fields differ from the document; draft or validate it again.",
    "WRONG_ENVIRONMENT": "The kit and the service disagree on the environment; check REYA_API_URL and REYA_WORKFLOWS_URL.",
    "RATE_LIMITED": "Wait `retry_after_s` seconds, then try again.",
}

# NOT_AGENT_ACCOUNT's `details.reason`: why the service does not take the account as the Agent account.
AGENT_ACCOUNT_REASONS = {
    "not_tagged": "The account was not created as the Agent account. Run agent status.",
    "main_account": "That is the owner's main account, not an Agent sub-account. Run agent status.",
    "spot_account": "That is a spot account, not an Agent sub-account. Run agent status.",
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
        return result


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
        raise WorkflowError(f"No workflow service is known for {api}; set {SERVICE_URL_VAR}")
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
            "No Agent account is recorded. Workflows only run on the Agent account that agent init records; "
            "run agent init first."
        )
    if requested is not None:
        if requested not in recorded:
            raise WorkflowError(
                f"Account {requested} is not your recorded Agent account ({', '.join(map(str, sorted(recorded)))}). "
                "Workflows only run on the Agent account that agent init recorded."
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
            "Workflow start and stop are signed with the agent key from agent init, never with your own key. "
            "Unset REYA_SIGNER=own-key for workflow commands."
        )
    raw = os.environ.get(_env.AGENT_KEY_VAR)
    if not raw:
        raise WorkflowError("No agent key: run agent init (the key it generates is the one workflows sign with)")
    key = _env._validate_key(raw)
    address = _env.agent_address()
    if address is None:
        raise WorkflowError("No agent key: run agent init")
    owner = os.environ.get("PERP_WALLET_ADDRESS_1", "")
    if owner and owner.lower() == address.lower():
        raise WorkflowError("The agent key is the owner wallet's key; workflows never sign with the owner key")
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

    @property
    def active_signer(self) -> str | None:
        active = [entry["address"] for entry in self.hosted_signers if entry.get("role") == "active"]
        return to_checksum_address(active[0]) if len(active) == 1 else None

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
            },
        }


def _incompatible(what: str) -> WorkflowError:
    return WorkflowError(f"The workflow service publishes {what} this kit cannot use; update the kit")


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
        )
    except WorkflowError:
        raise
    except (KeyError, TypeError, ValueError):
        raise WorkflowError(f"{url}/v1/info did not return the workflow service's parameters") from None
    if info.environment != environment or info.chain_id != chain_id:
        raise WorkflowError(
            f"The workflow service at {url} serves {info.environment} (chain {info.chain_id}), but the kit is "
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


def typed_data(primary: str, request: dict, info: ServiceInfo) -> SignableMessage:
    values: dict[str, Any] = {}
    for field in info.types[primary]:
        name, kind = field["name"], field["type"]
        value = request[name]
        if kind in UINT_BITS:
            value = int(value)
        elif kind == "bytes32":
            value = bytes.fromhex(value[2:])
        values[name] = value
    return encode_typed_data(
        full_message={
            "types": {
                "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}],
                primary: info.types[primary],
            },
            "primaryType": primary,
            "domain": info.domain,
            "message": values,
        }
    )


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
            bytes.fromhex(digest(primary, request, info)[2:])
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


def stop_request(info: ServiceInfo, *, account_id: int, run_id: int, signer: str, nonce: int, deadline: int) -> dict:
    return ordered(
        "WorkflowStop",
        {
            "verifyingChainId": str(info.chain_id),
            "environment": info.environment,
            "accountId": str(account_id),
            "runId": str(run_id),
            "signer": to_checksum_address(signer),
            "nonce": str(nonce),
            "deadline": str(deadline),
        },
        info,
    )


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


def document_account(doc: dict) -> int:
    value = doc.get("account_id")
    if not isinstance(value, str) or not UINT_RE.fullmatch(value) or value == "0":
        raise WorkflowError("The document's account_id must be a decimal string")
    return int(value)


def canonical_json(doc: Any) -> str:
    """RFC 8785 for workflow documents: ASCII keys sorted, no whitespace, integers and strings only."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def version_hash(canonical: str) -> str:
    return "0x" + keccak(text=canonical).hex()


# Paths the canonical form may add with the schema's defaults when the author left them out.
DEFAULTS = {
    ("limits", "price_bound_bps"): DEFAULT_BOUND_BPS,
    ("steps", "*", "conditions"): [],
    ("steps", "*", "next"): None,
}


def _covers(mine: Any, theirs: Any, path: tuple) -> str | None:
    """Where `theirs` (the service's canonical form) differs from what the author wrote, if anywhere."""
    where = ".".join(path) or "document"
    if isinstance(mine, dict):
        if not isinstance(theirs, dict):
            return where
        for key, value in mine.items():
            if key not in theirs:
                if path == () and key == "protection" and value is None:
                    continue
                return ".".join(path + (key,))
            found = _covers(value, theirs[key], path + (key,))
            if found:
                return found
        for key in theirs.keys() - mine.keys():
            pattern = tuple("*" if part.isdecimal() else part for part in path + (key,))
            if pattern not in DEFAULTS or theirs[key] != DEFAULTS[pattern]:
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


def check_canonical(doc: dict, canonical: Any, claimed_hash: Any) -> dict:
    """Accept the service's canonical form only if it is this document with defaults filled in.

    The agent key signs the canonical form's hash, so a canonical form that changed anything the
    author wrote is refused, and so is a hash that is not keccak256 of the canonical text.
    """
    if not isinstance(canonical, str) or not isinstance(claimed_hash, str):
        raise WorkflowError("The service did not return a canonical form and version hash")
    parsed = parse_document(canonical)
    if canonical_json(parsed) != canonical:
        raise WorkflowError("The service's canonical form is not canonical JSON; nothing was signed")
    if version_hash(canonical) != claimed_hash.lower():
        raise WorkflowError("The service's version hash is not the hash of its canonical form; nothing was signed")
    differs = _covers(doc, parsed, ())
    if differs:
        raise WorkflowError(f"The service's canonical form changes {differs}; nothing was signed")
    return parsed


def first_step(doc: dict) -> dict:
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps or not isinstance(steps[0], dict):
        raise WorkflowError("The document has no step")
    return steps[0]


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


def condition(kind: str, spec: str) -> dict:
    """`lt:0.001` → a condition of `kind` (funding_avg, position_qty or available_balance)."""
    op, _, raw = spec.partition(":")
    if op not in COMPARISONS or not raw:
        raise WorkflowError(f"A condition is OP:VALUE with OP one of {', '.join(COMPARISONS)}; got {spec!r}")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise WorkflowError(f"Not a number: {raw!r}") from None
    if kind == "funding_avg":
        return {"kind": kind, "op": op, "pct_per_hour": canonical_amount(value, places=9, signed=True, positive=False)}
    if kind == "position_qty":
        return {"kind": kind, "op": op, "qty": canonical_amount(value, places=18, signed=True, positive=False)}
    return {"kind": kind, "op": op, "usd": canonical_amount(value, places=18, positive=False)}


def draft_document(
    *,
    environment: str,
    account_id: int,
    market: str,
    direction: str,
    level: Decimal,
    side: str,
    notional: Decimal,
    allocation: Decimal,
    expires_at: int,
    repeat: str,
    bound_bps: int,
    conditions: list[dict],
    stop_px: Decimal | None,
) -> dict:
    if not re.fullmatch(r"[A-Z0-9]{1,32}", market):
        raise WorkflowError(f"Not a market symbol: {market!r}")
    if not MIN_BOUND_BPS <= bound_bps <= MAX_BOUND_BPS:
        raise WorkflowError(f"The price bound is {MIN_BOUND_BPS} to {MAX_BOUND_BPS} bps")
    if notional > allocation:
        raise WorkflowError("The order size is larger than the allocation")
    if expires_at <= int(time.time()):
        raise WorkflowError("The expiry is in the past")
    doc: dict[str, Any] = {
        "schema": SCHEMA_TAG,
        "template": dict(TEMPLATE),
        "environment": environment,
        "account_id": str(account_id),
        "market": market,
        "steps": [
            {
                "id": "entry",
                "trigger": {
                    "kind": "price_cross",
                    "source": "mark",
                    "direction": direction,
                    "level": canonical_amount(level, places=9),
                },
                "conditions": conditions,
                "action": {"kind": side, "notional_usd": canonical_amount(notional, places=18)},
                "repeat": repeat,
                "next": None,
            }
        ],
        "limits": {
            "allocation_usd": canonical_amount(allocation, places=18),
            "expires_at": expires_at,
            "price_bound_bps": bound_bps,
        },
    }
    if stop_px is not None:
        doc["protection"] = {"kind": "whole_position_stop", "trigger_px": canonical_amount(stop_px, places=9)}
    return doc


def base_asset(market: str) -> str:
    return market.removesuffix("RUSDPERP") or market


def describe(doc: dict) -> str:
    """The document in one plain sentence, from its own fields (no evaluation)."""
    step = first_step(doc)
    trigger, action, limits = step.get("trigger", {}), step.get("action", {}), doc.get("limits", {})
    market = str(doc.get("market", "?"))
    parts = [
        f"{str(action.get('kind', '?')).capitalize()} {usd_text(action.get('notional_usd'))} of {base_asset(market)} "
        f"{'each time' if step.get('repeat') == 'each_crossing' else 'once, the first time'} the {market} mark "
        f"crosses {trigger.get('direction', '?')} through {group(trigger.get('level'))}"
    ]
    for item in step.get("conditions") or []:
        word = COMPARISON_WORDS.get(item.get("op"), item.get("op"))
        if item.get("kind") == "funding_avg":
            parts.append(f"if the 15-minute average funding is {word} {item.get('pct_per_hour')}%/h")
        elif item.get("kind") == "position_qty":
            parts.append(f"if the position is {word} {item.get('qty')} {base_asset(market)}")
        elif item.get("kind") == "available_balance":
            parts.append(f"if the available balance is {word} {usd_text(item.get('usd'))}")
    parts.append(f"up to {usd_text(limits.get('allocation_usd'))} in total")
    if isinstance(limits.get("expires_at"), int):
        parts.append(f"until {iso(limits['expires_at'])}")
    parts.append(f"each order within {limits.get('price_bound_bps', DEFAULT_BOUND_BPS)} bps of the mark")
    protection = doc.get("protection")
    if isinstance(protection, dict):
        parts.append(f"with a whole-position stop at {group(protection.get('trigger_px'))}")
    return ", ".join(parts) + "."


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


def firing_text(firing: dict, level: Any, market: str) -> str:
    """One firing as the user reads it: "triggered at X, executed at Y (+n bps), Q BASE for $N"."""
    at = f"triggered at {group(level)}"
    status = firing.get("order_status")
    qty, avg = firing.get("exec_qty"), firing.get("avg_fill_px")
    bound = firing.get("bound_bps")
    if status in ("filled", "partial") and qty and avg:
        notional = Decimal(str(qty)) * Decimal(str(avg))
        verb = "executed" if status == "filled" else "partly executed"
        return f"{at}, {verb} at {group(avg)} ({bps_from(level, avg)}), {qty} {base_asset(market)} for {usd_text(notional)}"
    if status == "shadow":
        return (
            f"{at} (mark {group(firing.get('mark'))}): shadow run, nothing sent; the order would have been "
            f"limited to {group(firing.get('limit_px'))} ({bound} bps bound)"
        )
    if status == "past_bound":
        return f"{at}, not filled: the market had moved past the {bound} bps bound before the order went out"
    if status == "zero_fill":
        return f"{at}, not filled: nothing traded within the {bound} bps bound"
    if status == "rejected":
        return f"{at}, rejected by the exchange: {firing.get('cancel_reason') or 'no reason given'}"
    if status == "unknown":
        return (
            f"{at}, outcome unknown: the run is blocked, keeping its reservation and protection, "
            "until the order's outcome is known"
        )
    return f"{at} (mark {group(firing.get('mark'))}), order {status}: waiting for its outcome"


def firing_view(event: dict, level: Any, market: str) -> dict:
    firing = event.get("firing") or {}
    view = {
        "at": iso(event.get("at")),
        "status": firing.get("order_status"),
        "text": firing_text(firing, level, market),
        "level": level,
        "mark": firing.get("mark"),
        "limit_px": firing.get("limit_px"),
        "avg_fill_px": firing.get("avg_fill_px"),
        "exec_qty": firing.get("exec_qty"),
        "bound_bps": firing.get("bound_bps"),
        "settlement": firing.get("settlement"),
        "client_order_id": firing.get("client_order_id"),
    }
    return view


def is_skip(event: dict) -> bool:
    code = event.get("code")
    return event.get("kind") == "skipped" or (isinstance(code, str) and code in SKIP_REASONS)


def skip_view(event: dict) -> dict:
    return {
        "at": iso(event.get("at")),
        "code": event.get("code"),
        "reason": reason(SKIP_REASONS, event.get("code")),
        "text": event.get("text"),
    }


def event_view(event: dict) -> dict:
    return {
        "seq": event.get("seq"),
        "at": iso(event.get("at")),
        "kind": event.get("kind"),
        "actor": event.get("actor"),
        "code": event.get("code"),
        "text": event.get("text"),
    }


def stop_view(stop: Any) -> dict | None:
    if not isinstance(stop, dict):
        return None
    return {
        "by": stop.get("by"),
        "reason": stop.get("reason"),
        "reason_text": reason(STOP_REASONS, stop.get("reason")),
        "signer": stop.get("signer"),
        "at": iso(stop.get("at")),
    }


def completion_view(completion: Any) -> dict | None:
    if not isinstance(completion, dict):
        return None
    return {
        "reason": completion.get("reason"),
        "reason_text": reason(COMPLETION_REASONS, completion.get("reason")),
        "at": iso(completion.get("at")),
    }


def allocation_view(allocation: Any) -> dict:
    if not isinstance(allocation, dict):
        return {}
    view = dict(allocation)
    view["text"] = (
        f"{usd_text(allocation.get('remaining_usd'))} of {usd_text(allocation.get('total_usd'))} remaining "
        f"({usd_text(allocation.get('spent_usd'))} spent, {usd_text(allocation.get('reserved_usd'))} reserved)"
    )
    return view


def side_of(mark: Any, level: Any) -> str:
    """Where the mark is relative to the level: "above", "below" or "at"."""
    try:
        difference = Decimal(str(mark)) - Decimal(str(level))
    except InvalidOperation:
        return "away from"
    return "above" if difference > 0 else "below" if difference < 0 else "at"


def armed_view(armed: Any, phase: Any, direction: Any) -> dict | None:
    if not isinstance(armed, dict):
        return None
    view = dict(armed)
    level, mark, bps = armed.get("level"), armed.get("last_mark"), armed.get("distance_bps")
    distance = distance_text(bps)
    if mark is None or distance is None:
        view["text"] = f"level {group(level)}; no fresh mark yet"
    else:
        side = side_of(mark, level)
        verb = "armed at" if phase == "armed" else "level"
        where = "at the level" if side == "at" else f"{distance} {side} it"
        view["text"] = f"{verb} {group(level)} (crossing {direction or '?'}): mark {group(mark)} is {where}"
    view["distance"] = distance
    return view


def status_view(run: dict, events: list[dict], *, truncated: bool = False, skips_shown: int = 20) -> dict:
    """A run's status with its armed level, firings, named skips and remaining allocation."""
    try:
        doc = json.loads(run["document"]["canonical"])
        trigger = first_step(doc).get("trigger", {})
    except (KeyError, TypeError, ValueError):
        doc, trigger = {}, {}
    level, market = trigger.get("level"), str(run.get("market") or doc.get("market") or "")
    firings = [firing_view(e, level, market) for e in events if e.get("kind") == "fired" and e.get("firing")]
    skips = [skip_view(e) for e in events if is_skip(e)]
    counts: dict[str, int] = {}
    for skip in skips:
        counts[str(skip["code"])] = counts.get(str(skip["code"]), 0) + 1
    stale = run.get("stale")
    view: dict[str, Any] = {
        "run_id": run.get("run_id"),
        "account_id": run.get("account_id"),
        "market": market,
        "state": run.get("state"),
        "mode": run.get("mode"),
        "phase": run.get("phase"),
        "phase_text": reason(PHASES, run.get("phase")),
        "armed": armed_view(run.get("armed"), run.get("phase"), trigger.get("direction")),
        "stale": (
            {"code": stale.get("code"), "reason": reason(STALE_REASONS, stale.get("code")), "text": stale.get("text")}
            if isinstance(stale, dict)
            else None
        ),
        "allocation": allocation_view(run.get("allocation")),
        "firings": firings,
        "skips": skips[-skips_shown:],
        "skip_counts": counts,
        "protection": run.get("protection"),
        "completion": completion_view(run.get("completion")),
        "stop": stop_view(run.get("stop")),
        "starter": run.get("starter"),
        "hosted_signer": run.get("hosted_signer"),
        "created_at": iso(run.get("created_at")),
        "expires_at": iso(run.get("expires_at")),
        "workflow": describe(doc) if doc else None,
        "version_hash": (run.get("document") or {}).get("version_hash"),
        "recent_events": [event_view(e) for e in events[-10:]],
    }
    if truncated:
        view["events_truncated"] = True
    view["summary"] = status_summary(view)
    return view


def status_summary(view: dict) -> str:
    state = view.get("state")
    parts = [f"Run {view.get('run_id')} ({view.get('mode')}) is {state}"]
    if state == "active":
        if view.get("stale"):
            parts.append(f"not firing: {view['stale']['reason']}")
        armed = view.get("armed")
        parts.append(armed["text"] if armed else view.get("phase_text") or "")
    elif view.get("completion"):
        parts.append(f"completed because {view['completion']['reason_text']}")
    elif view.get("stop"):
        parts.append(f"stopped: {view['stop']['reason_text']}")
    allocation = view.get("allocation") or {}
    if allocation.get("text"):
        parts.append(allocation["text"])
    parts.append(f"{len(view.get('firings') or [])} firing(s), {sum((view.get('skip_counts') or {}).values())} skip(s)")
    return "; ".join(p for p in parts if p) + "."


def order_preview_text(order: Any, error: Any) -> str | None:
    """One firing at the level, as the service sized it, or why it could not send one."""
    if isinstance(order, dict):
        covered = order.get("orders_covered")
        return (
            f"{order.get('side')} {order.get('qty')} at a limit of {group(order.get('limit_px'))} "
            f"({order.get('bound_bps')} bps max slippage), reserving {usd_text(order.get('reserved_usd'))}; "
            f"the allocation covers {covered} such order{'' if str(covered) == '1' else 's'}"
        )
    if isinstance(error, dict):
        return f"no order at the level: {error.get('message') or error.get('code')}"
    return None


def funding_preview_text(funding: Any) -> str | None:
    if not isinstance(funding, dict):
        return None
    latest = funding.get("latest") if isinstance(funding.get("latest"), dict) else None
    average = funding.get("average_15m_pct_per_hour")
    parts = [f"latest {latest.get('pct_per_hour')}%/h" if latest else "no sample yet"]
    parts.append(f"15-minute average {average}%/h" if average is not None else "no 15-minute average yet")
    return f"funding: {', '.join(parts)} ({funding.get('samples')} samples in the window)"


def preview_view(preview: dict, doc: dict) -> dict:
    market = preview.get("market") or {}
    trigger = preview.get("trigger") or {}
    lines = [line for line in preview.get("lines") or [] if isinstance(line, str)]
    mark, level = market.get("mark"), trigger.get("level")
    distance = distance_text(trigger.get("distance_bps"))
    side = trigger.get("side") or {"above": "below", "below": "above"}.get(side_of(mark, level), "away from")
    view: dict[str, Any] = {
        "ok": True,
        "version_hash": preview.get("version_hash"),
        "as_of": iso(preview.get("as_of")),
        "workflow": describe(doc),
        "market": {**market, "mark_at": iso(market.get("mark_at"))},
        "trigger": {**trigger, "distance": distance},
        "now": (lines[0] if lines else None)
        or f"{base_asset(str(market.get('symbol', '')))} mark is {group(mark)}; the level is {distance} {side} it",
        "order": preview.get("order"),
        "order_error": preview.get("order_error"),
        "order_text": order_preview_text(preview.get("order"), preview.get("order_error")),
        "allocation_usd": preview.get("allocation_usd"),
        "funding": preview.get("funding"),
        "funding_text": funding_preview_text(preview.get("funding")),
        "warnings": preview.get("warnings") or [],
        "lines": lines,
    }
    if trigger.get("already_triggered"):
        view["already_triggered"] = (
            "The mark is already past the level. Start with --fire-now to fire on the first fresh mark, or "
            "without it to wait until the mark moves back past the re-arm band and crosses again."
        )
    for key, value in preview.items():
        view.setdefault(key, value)
    return view


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


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
    """One request per call: no redirects, no retries, writes never repeated."""

    def __init__(self, base_url: str, *, timeout_s: float = 20.0):
        self.base_url = base_url
        self.timeout_s = timeout_s
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
        assert self.session is not None
        try:
            async with self.session.request(
                method, self.base_url + path, json=body, params=params, allow_redirects=False
            ) as response:
                try:
                    raw = await response.content.readexactly(MAX_RESPONSE_BYTES + 1)
                except asyncio.IncompleteReadError as exc:
                    raw = exc.partial
                status, headers = response.status, response.headers
        except aiohttp.ClientConnectorError:
            raise WorkflowError(f"Cannot reach the workflow service at {self.base_url}; nothing was sent") from None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if write:
                raise OutcomeUnknown(f"No answer from {self.base_url}{path}") from None
            raise WorkflowError(f"The workflow service at {self.base_url} did not answer") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            if write:
                raise OutcomeUnknown("The service's answer was too large to read")
            raise WorkflowError("The workflow service's answer was too large")
        if region_blocked(status, raw):
            raise RegionUnavailableError()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        if status in (200, 201) and data is not None:
            return data
        refusal = _envelope(status, data, headers)
        if refusal is not None:
            raise refusal
        if write:
            raise OutcomeUnknown(f"HTTP {status} without an answer the kit can read")
        raise WorkflowError(f"The workflow service answered HTTP {status} without an error the kit can read")

    async def object(self, method: str, path: str, **kwargs) -> dict:
        data = await self.request(method, path, **kwargs)
        if not isinstance(data, dict):
            if method == "POST" and path.startswith("/v1/runs"):
                raise OutcomeUnknown(f"{path} answered something other than a JSON object")
            raise WorkflowError(f"The workflow service answered {path} with something other than a JSON object")
        return data

    async def info(self) -> dict:
        return await self.object("GET", "/v1/info")

    async def validate(self, document: str) -> dict:
        return await self.object("POST", "/v1/validate", body={"document": document})

    async def preview(self, document: str) -> dict:
        # The document only: whether to fire on the first mark is the start's choice (`fireNow`).
        return await self.object("POST", "/v1/preview", body={"document": document})

    async def start(self, body: dict) -> dict:
        return await self.object("POST", "/v1/runs", body=body)

    async def stop(self, run_id: int, body: dict) -> dict:
        return await self.object("POST", f"/v1/runs/{run_id}/stop", body=body)

    async def run(self, run_id: int) -> dict:
        return await self.object("GET", f"/v1/runs/{run_id}")

    async def account_runs(self, account_id: int, *, state: str, limit: int = 50) -> dict:
        return await self.object("GET", f"/v1/accounts/{account_id}/runs", params={"state": state, "limit": str(limit)})

    async def activity(self, run_id: int, *, max_pages: int = 25) -> tuple[list[dict], bool]:
        """The whole event trail, oldest first; True when it stopped at `max_pages` pages."""
        events: list[dict] = []
        after: str | None = None
        for _ in range(max_pages):
            params = {"limit": "200"} | ({"after": after} if after else {})
            page = await self.request("GET", f"/v1/runs/{run_id}/activity", params=params)
            if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                raise WorkflowError("The workflow service returned an unreadable activity page")
            events.extend(e for e in page["events"] if isinstance(e, dict))
            after = page.get("next_after")
            if not isinstance(after, str) or not UINT_RE.fullmatch(after):
                return events, False
        return events, True
