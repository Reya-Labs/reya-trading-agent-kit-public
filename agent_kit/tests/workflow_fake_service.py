"""An in-memory stand-in for the hosted strategy service (`reya.workflow/3`), for the strategy command tests.

It serves the published `/v1` API (info, validate, preview, start, stop, run status, activity and account runs) on
127.0.0.1, under `base_path` the way the API host's gateway serves the real one (`/workflows/v1/info`; anything
outside the prefix is a plain 404), and checks requests the way the service's contract describes, with its own
code rather than the kit's: a check's fields, then a Start's strict body (no repeated key, no unknown field), its
fields, environment and chain, deadline and signature, which it recovers with eth_account. Then who may sign, the
per-signer nonce (a Start already committed answers its run again), the switch for the mode, the Agent account,
the hosted signer, the preview record the Start signs (kept 600 s, one run each, its terms equal to the signed
ones) and one Live strategy per account. A Stop: the path's run is the signed one, a v3 id, of the signed account
and strategy, signed by the owner or a delegate, and not ended; a Paper run stops only by closing.

`/v1/info` is the devnet1 service's, its types the service's own signed-request vectors' (fixtures/
workflow_requests, copied unchanged). A check's version hash is keccak256 of the document's sorted, compact JSON.
Its issues are `issues` (as the service writes them), plus `analysis_incomplete` on a check without an account;
the verdict follows from them. A Paper or Live preview that refuses nothing records a preview and answers its id.

Read grants follow `read_grants`: checked when sent ("check", the default, as the service does while it does not
enforce them), also required ("require"), or refused with a bare 401 and no error envelope, as by a gateway that
does not take the header ("refuse"). A run read under a grant the fake verified carries the run's `detail`; any
other carries none (`read_grant_required`).

Tests change its state directly (approvals, runs, issues) and can make the next write fail (`next_refusal`) or
lose its answer (`next_write`: "lose" applies it and answers 504 with no envelope, "drop" answers 502 without
applying it).

`relay()` stands in for the trading API's owner reads the kit makes through AccountRelay (the legacy approval list
and the owner's accounts), answered from the same state.
"""

from typing import Any

import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

FIXTURES = Path(__file__).parent / "fixtures"
REQUESTS = FIXTURES / "workflow_requests"
START_VECTORS = json.loads((REQUESTS / "strategy_start.json").read_text())
STOP_VECTORS = json.loads((REQUESTS / "strategy_stop.json").read_text())
GRANT_VECTORS = json.loads((REQUESTS / "read_grant.json").read_text())
DOMAIN = START_VECTORS["domain"]
TYPES = {**START_VECTORS["types"], **STOP_VECTORS["types"]}
READ_GRANT_TYPE = GRANT_VECTORS["types"]["ReadGrant"]
DOMAIN_TYPE = [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}]

RUN_READ = re.compile(r"/v1/runs/([0-9]+)")
READ_PATH = re.compile(r"/v1/runs/([0-9]+)(?:/activity)?|/v1/accounts/([0-9]+)/runs")
CHECK_FIELDS = {"document", "account_id", "mode", "paper", "from_paper_run", "expires_at", "fire_now", "label"}
STATUS = {
    "INVALID_REQUEST": 400,
    "INVALID_DOCUMENT": 400,
    "DOCUMENT_MISMATCH": 400,
    "WRONG_ENVIRONMENT": 400,
    "MODE_NOT_ALLOWED": 400,
    "BAD_SIGNATURE": 401,
    "INVALID_READ_GRANT": 401,
    "READ_GRANT_REQUIRED": 401,
    "REQUEST_EXPIRED": 401,
    "DEADLINE_TOO_FAR": 401,
    "HOSTED_SIGNER_REJECTED": 403,
    "NOT_PERMITTED": 403,
    "NOT_AGENT_ACCOUNT": 403,
    "NOT_WHITELISTED": 403,
    "HOSTED_SIGNER_NOT_ENABLED": 403,
    "UNKNOWN_HOSTED_SIGNER": 403,
    "RUN_NOT_FOUND": 404,
    "NONCE_TOO_LOW": 409,
    "START_REFUSED": 409,
    "STOP_REFUSED": 409,
    "ACCOUNT_STRATEGY_LIMIT": 409,
    "RUN_ON_PREVIOUS_VERSION": 410,
    "RATE_LIMITED": 429,
    "ACCOUNT_STATE_UNAVAILABLE": 503,
    "BUSY": 503,
    "NOT_READY": 503,
}
UINT = re.compile(r"(0|[1-9][0-9]*)")
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
FIRST_V3_RUN = 3_000_000
PREVIEW_SECS = 600
# Whether the request carried a read grant the fake verified.
GRANT_OK = web.RequestKey("grant_ok", bool)


class Refusal(Exception):
    def __init__(self, code: str, message: str = "", details: dict | None = None):
        super().__init__(message or code)
        self.code = code
        self.details = details or {}


def canonical(doc: Any) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_of(doc: Any) -> str:
    return "0x" + keccak(text=canonical(doc)).hex()


def strict(raw: str) -> Any:
    """JSON with no repeated key, no fractional number, no NaN or Infinity and no integer beyond ±(2^53 − 1), as
    the service's strict parse."""

    def pairs(items: list) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise Refusal("INVALID_REQUEST", f"repeated key {key}")
            result[key] = value
        return result

    def no_float(text: str) -> Any:
        raise Refusal("INVALID_REQUEST", "a fractional number")

    def no_constant(text: str) -> Any:
        raise Refusal("INVALID_REQUEST", f"{text} is not JSON")

    def bounded(text: str) -> int:
        value = int(text)
        if abs(value) > 2**53 - 1:
            raise Refusal("INVALID_REQUEST", "an integer out of range")
        return value

    try:
        return json.loads(
            raw, object_pairs_hook=pairs, parse_float=no_float, parse_constant=no_constant, parse_int=bounded
        )
    except ValueError:
        raise Refusal("INVALID_REQUEST", "not JSON") from None


def typed(primary: str, message: dict) -> tuple[dict, Any]:
    """The message's EIP-712 values, and its signable form, from the service's own types."""

    def value(kind: str, raw: Any) -> Any:
        if kind.endswith("[]"):
            if not isinstance(raw, list):
                raise Refusal("INVALID_REQUEST", "not a list")
            return [value(kind[:-2], item) for item in raw]
        if kind in TYPES:
            return {f["name"]: value(f["type"], raw[f["name"]]) for f in TYPES[kind]}
        if kind.startswith("uint"):
            if not isinstance(raw, str) or not UINT.fullmatch(raw):
                raise Refusal("INVALID_REQUEST", "not a decimal string")
            return int(raw)
        if kind == "bytes32":
            if not isinstance(raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", raw):
                raise Refusal("INVALID_REQUEST", "not bytes32")
            return bytes.fromhex(raw[2:])
        if kind == "bool":
            if type(raw) is not bool:
                raise Refusal("INVALID_REQUEST", "not a boolean")
            return raw
        if not isinstance(raw, str):
            raise Refusal("INVALID_REQUEST", "not a string")
        return raw

    names = [f["name"] for f in TYPES[primary]]
    if not isinstance(message, dict) or set(message) != set(names):
        raise Refusal("INVALID_REQUEST", "fields")
    values = value(primary, message)
    used = {primary: TYPES[primary]}
    if primary == "StrategyStart":
        used["HandoverStop"] = TYPES["HandoverStop"]
    signable = encode_typed_data(
        full_message={
            "types": {"EIP712Domain": DOMAIN_TYPE, **used},
            "primaryType": primary,
            "domain": DOMAIN,
            "message": values,
        }
    )
    return values, signable


@dataclass
class FakeWorkflowService:
    environment: str = "devnet1"
    chain_id: int = 89346162
    hosted: str = "0x90F79bf6EB2c4f870365E785982E1f101E93b906"
    version: str = "0.20.10"
    live_enabled: bool = True
    paper_enabled: bool = True
    account_checks: bool = True
    # account -> owner; owner -> approved delegates; owner -> approved hosted signers
    accounts: dict = field(default_factory=dict)
    agent_accounts: set = field(default_factory=set)
    delegates: dict = field(default_factory=dict)
    hosted_enabled: dict = field(default_factory=dict)
    # The issues every check reports, as the service writes them (`{"code", "outcome", "message", ...}`).
    issues: list = field(default_factory=list)
    previews: dict = field(default_factory=dict)
    runs: dict = field(default_factory=dict)
    activity: dict = field(default_factory=dict)
    nonces: dict = field(default_factory=dict)
    accepted: dict = field(default_factory=dict)
    next_preview: int = 101
    next_run: int = FIRST_V3_RUN + 7
    requests: list = field(default_factory=list)
    info_override: dict | None = None
    hash_override: str | None = None
    next_write: str | None = None
    next_refusal: tuple | None = None
    base_path: str = "/workflows"
    raw_paths: list = field(default_factory=list)
    read_grants: str = "check"
    authorizations: list = field(default_factory=list)
    grant_signers: list = field(default_factory=list)
    trading_reads: list = field(default_factory=list)
    trading_failure: Exception | None = None
    server: TestServer | None = None

    # ------------------------------------------------------------------ setup

    def add_account(self, account_id: int, owner: str, *, agent: bool = True) -> None:
        self.accounts[str(account_id)] = owner
        if agent:
            self.agent_accounts.add(str(account_id))

    def approve(self, owner: str, signer: str) -> None:
        self.delegates.setdefault(owner.lower(), set()).add(signer.lower())

    def enable_hosted(self, owner: str) -> None:
        self.hosted_enabled.setdefault(owner.lower(), set()).add(self.hosted.lower())

    def relay(self) -> type:
        """A stand-in for the kit's AccountRelay class, reading this service's state."""
        return type("FakeAccountRelay", (FakeAccountRelay,), {"service": self})

    def trading_read(self, path: str, owner: str) -> Any:
        """What the trading API answers an owner read: approvals (agent keys and enabled hosted
        signers alike, as the legacy apiWalletStatus lists them) and the owner's accounts."""
        self.trading_reads.append(path)
        if self.trading_failure is not None:
            raise self.trading_failure
        if path.lower() == f"/api/trading/wallet/{owner}/apiWalletStatus".lower():
            approved = self.delegates.get(owner.lower(), set()) | self.hosted_enabled.get(owner.lower(), set())
            return {"enabledApiWallets": sorted(approved)}
        if path.lower() == f"/v2/wallet/{owner}/accounts".lower():
            return [
                {
                    "accountId": int(account),
                    "type": "AGENTPERP" if account in self.agent_accounts else "SUBPERP",
                    "name": f"Account {account}",
                }
                for account, holder in self.accounts.items()
                if holder.lower() == owner.lower()
            ]
        raise AssertionError(f"unexpected trading API read {path}")

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.port}{self.base_path}"

    async def start_server(self) -> None:
        app = web.Application()
        app.router.add_get("/v1/info", self.info)
        app.router.add_post("/v1/validate", self.validate)
        app.router.add_post("/v1/preview", self.preview)
        app.router.add_post("/v1/runs", self.start)
        app.router.add_get("/v1/runs/{run_id}", self.run_read)
        app.router.add_post("/v1/runs/{run_id}/stop", self.stop)
        app.router.add_get("/v1/runs/{run_id}/activity", self.activity_read)
        app.router.add_get("/v1/accounts/{account_id}/runs", self.account_runs)
        app.middlewares.append(self.record)
        if self.base_path:
            root = web.Application()
            root.add_subapp(self.base_path, app)
            app = root
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()

    async def close(self) -> None:
        if self.server is not None:
            await self.server.close()

    @web.middleware
    async def record(self, request: web.Request, handler):
        raw = await request.text() if request.can_read_body else ""
        self.raw_paths.append(request.path_qs)
        path = request.path.removeprefix(self.base_path)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw
        self.requests.append((request.method, path, body))
        authorization = request.headers.get("Authorization")
        self.authorizations.append((request.method, path, authorization))
        if authorization is not None and self.read_grants == "refuse":
            return web.Response(status=401, text="unauthorized")
        request[GRANT_OK] = False
        try:
            if authorization is not None:
                request[GRANT_OK] = self.check_grant(request.method, path, authorization, body)
            elif self.read_grants == "require" and (
                (request.method == "GET" and READ_PATH.fullmatch(path))
                or (
                    request.method == "POST"
                    and path in ("/v1/validate", "/v1/preview")
                    and isinstance(body, dict)
                    and "account_id" in body
                )
            ):
                raise Refusal("READ_GRANT_REQUIRED")
            if request.method == "POST" and path.startswith("/v1/runs") and self.next_refusal:
                code, details = self.next_refusal
                self.next_refusal = None
                raise Refusal(code, details=details)
            return await handler(request)
        except Refusal as refusal:
            headers = {"x-request-id": "req-test"}
            if STATUS[refusal.code] in (429, 503):
                headers["Retry-After"] = "7"
            envelope = {
                "error": {
                    "code": refusal.code,
                    "message": str(refusal),
                    "retryable": STATUS[refusal.code] in (429, 503),
                    "details": refusal.details,
                }
            }
            return web.json_response(envelope, status=STATUS[refusal.code], headers=headers)

    def writes(self, path_prefix: str = "/v1/runs") -> list:
        return [r for r in self.requests if r[0] == "POST" and r[1].startswith(path_prefix)]

    def reads(self, pattern: str) -> list:
        return [r for r in self.requests if r[0] == "GET" and re.fullmatch(pattern, r[1])]

    # ------------------------------------------------------------------ grants

    def check_grant(self, method: str, path: str, authorization: str, body: Any) -> bool:
        """The service's checks on a read grant, cheap ones first; a run it does not cover reads as not found."""
        match = READ_PATH.fullmatch(path) if method == "GET" else None
        check = method == "POST" and path in ("/v1/validate", "/v1/preview") and isinstance(body, dict)
        if match is None and not check:
            raise Refusal("INVALID_REQUEST", "a read grant goes only with a run read or an account check")
        try:
            assert authorization.startswith("Bearer ")
            token = authorization.removeprefix("Bearer ")
            data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            assert set(data) == {"grant", "signature"}
            grant, signature = data["grant"], bytes.fromhex(data["signature"][2:])
            assert list(grant) == [f["name"] for f in READ_GRANT_TYPE] and len(signature) == 65
            assert all(UINT.fullmatch(v) for v in (grant["verifyingChainId"], grant["expiresAt"], *grant["accountIds"]))
            values = {
                "verifyingChainId": int(grant["verifyingChainId"]),
                "environment": grant["environment"],
                "owner": grant["owner"],
                "accountIds": [int(a) for a in grant["accountIds"]],
                "expiresAt": int(grant["expiresAt"]),
                "salt": bytes.fromhex(grant["salt"][2:]),
            }
            assert len(values["salt"]) == 16 and int.from_bytes(signature[32:64], "big") <= N // 2
            signer = Account.recover_message(
                encode_typed_data(
                    full_message={
                        "types": {"EIP712Domain": DOMAIN_TYPE, "ReadGrant": READ_GRANT_TYPE},
                        "primaryType": "ReadGrant",
                        "domain": DOMAIN,
                        "message": values,
                    }
                ),
                signature=signature,
            )
        except (AssertionError, KeyError, TypeError, ValueError):
            raise Refusal("INVALID_READ_GRANT", details={"reason": "malformed"}) from None
        now = int(time.time())
        owner = grant["owner"].lower()
        if grant["environment"] != self.environment or values["verifyingChainId"] != self.chain_id:
            raise Refusal("INVALID_READ_GRANT", details={"reason": "wrong_environment"})
        if not now < values["expiresAt"] <= now + 8 * 3600:
            raise Refusal("INVALID_READ_GRANT", details={"reason": "expired"})
        if signer.lower() not in self.delegates.get(owner, set()):
            raise Refusal("INVALID_READ_GRANT", details={"reason": "not_delegate"})
        if match is not None:
            account = (self.runs.get(match.group(1)) or {}).get("account_id") if match.group(1) else match.group(2)
        else:
            account = body.get("account_id")
        if account is not None and (
            self.accounts.get(account, "").lower() != owner
            or (grant["accountIds"] and account not in grant["accountIds"])
        ):
            raise Refusal("RUN_NOT_FOUND" if match is not None else "NOT_PERMITTED")
        self.grant_signers.append(signer)
        return True

    # ------------------------------------------------------------------ info

    def info_body(self) -> dict:
        types = {"EIP712Domain": DOMAIN_TYPE, **TYPES}
        body = {
            "chain_id": str(self.chain_id),
            "eip712": {
                "domain": DOMAIN,
                "types": {"ReadGrant": READ_GRANT_TYPE, "StrategyStop": TYPES["StrategyStop"]},
            },
            "environment": self.environment,
            "hosted_signers": [{"address": self.hosted, "can_sign": True, "role": "active"}],
            "paper_start_enabled": self.paper_enabled,
            "schemas": ["reya.workflow/3"],
            "sources": [],
            "v3": {
                "accepted_kinds": {
                    "actions": ["order", "limit_orders", "close", "reduce"],
                    "stop_levels": ["price", "from_entry"],
                    "triggers": ["start", "cross", "every", "at", "elapsed", "filled", "stop_loss", "take_profit"],
                    "states": ["normal", "loop_head", "final"],
                    "refused_shapes": [{"feature": "stop_ladder", "rule": "a stop with more than one level"}],
                },
                "account_checks_enabled": self.account_checks,
                "eip712": {"domain": DOMAIN, "types": types},
                "paper_start_enabled": self.paper_enabled,
                "schema": "reya.workflow/3",
                "start_enabled": self.live_enabled,
                "trading_whitelist": "waived",
            },
            "version": self.version,
        }
        return self.info_override if self.info_override is not None else body

    async def info(self, request: web.Request) -> web.Response:
        return web.json_response(self.info_body())

    # ------------------------------------------------------------------ checks

    def check(self, request: web.Request, body: Any, preview: bool) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("document"), str):
            raise Refusal("INVALID_REQUEST", "a check takes a document")
        unknown = set(body) - CHECK_FIELDS
        if unknown:
            raise Refusal("INVALID_REQUEST", details={"reason": "field_unknown", "field": sorted(unknown)[0]})
        try:
            doc = strict(body["document"])
        except Refusal:
            raise Refusal("INVALID_DOCUMENT", "the document is not JSON") from None
        if not isinstance(doc, dict) or doc.get("schema") != "reya.workflow/3":
            raise Refusal("INVALID_DOCUMENT", "not a reya.workflow/3 document")
        if not isinstance(doc.get("actions", {}), dict) or not isinstance(doc.get("states", {}), dict):
            raise Refusal("INVALID_DOCUMENT", "actions and states are objects")
        mode = body.get("mode", "draft")
        account = body.get("account_id")
        if mode not in ("draft", "paper", "live") or (mode != "draft" and account is None):
            raise Refusal("INVALID_REQUEST", "a Paper or Live check names an account")
        if "paper" in body and mode != "paper":
            raise Refusal("INVALID_REQUEST", details={"reason": "paper_settings_not_paper"})
        now = int(time.time())
        if mode != "draft":
            expires = body.get("expires_at")
            if not isinstance(expires, str) or not UINT.fullmatch(expires) or int(expires) <= now:
                raise Refusal("INVALID_REQUEST", details={"reason": "expires_at_required"})
            if int(expires) < now + PREVIEW_SECS:
                raise Refusal("INVALID_REQUEST", details={"reason": "expires_at_too_soon"})
            if int(expires) > now + 365 * 86400:
                raise Refusal("INVALID_REQUEST", details={"reason": "expires_at_too_far"})
        if account is not None:
            if not self.account_checks:
                raise Refusal("ACCOUNT_STATE_UNAVAILABLE")
            if account not in self.agent_accounts:
                raise Refusal("NOT_AGENT_ACCOUNT", details={"reason": "not_tagged"})
        issues = [dict(i) for i in self.issues]
        if account is None:
            issues.append(
                {
                    "code": "analysis_incomplete",
                    "outcome": "warning",
                    "message": "not checked against an account: the money and analysis checks did not run",
                    "path": "",
                }
            )
        outcomes = {i["outcome"] for i in issues}
        verdict = "refusal" if "refusal" in outcomes else "warning" if "warning" in outcomes else "pass"
        answer: dict[str, Any] = {
            "version_hash": self.hash_override or hash_of(doc),
            "result": {
                "schema": "reya.check-result/3",
                "mode": mode,
                "verdict": verdict,
                "issue_counts": {k: sum(i["outcome"] == k for i in issues) for k in ("refusal", "warning", "info")},
                "issues": issues,
                "readouts": {
                    "margin_needed": {"total": {"kind": "at_most", "unit": "rusd", "value": "6.234776"}},
                    "additional_deposit_needed": {"kind": "at_most", "unit": "rusd", "value": "0"},
                    "worst_deployed": {"kind": "unknown", "reason": "account_snapshot_source_absent", "unit": "rusd"},
                },
            },
            "what_if": [{"event": "liquidation", "response": "close_and_end", "actions": []}],
        }
        if account is not None:
            answer["takes_over"] = {"positions": [], "resting_orders": [], "stops": []}
        paper: dict = body["paper"] if isinstance(body.get("paper"), dict) else {}
        if mode == "paper":
            balance = paper.get("balance")
            answer["paper"] = {
                "margin": (
                    {"source": "paper_balance", "amount": balance}
                    if balance is not None
                    else {"source": "account_balance", "amount": "200"}
                ),
                "slippage_bps": paper.get("slippage_bps", 1),
                "fee_tier": paper.get("fee_tier"),
                "start": paper.get("start", "empty"),
                "defaults": sorted({"slippage_bps", "fee_tier", "balance", "start"} - set(paper)),
            }
        if preview and mode != "draft" and verdict != "refusal":
            preview_id = str(self.next_preview)
            self.next_preview += 1
            self.previews[preview_id] = {
                "account_id": account,
                "mode": mode,
                "version_hash": answer["version_hash"],
                "expires_at": body["expires_at"],
                "fire_now": body.get("fire_now", False),
                "label": body.get("label"),
                "created": now,
                "strategy_id": preview_id,
                "document": canonical(doc),
                "paper": answer.get("paper"),
                "used": False,
            }
            answer |= {"preview_id": preview_id, "strategy_id": preview_id, "valid_until": str(now + PREVIEW_SECS)}
        return answer

    async def validate(self, request: web.Request) -> web.Response:
        return web.json_response(self.check(request, self.requests[-1][2], preview=False))

    async def preview(self, request: web.Request) -> web.Response:
        return web.json_response(self.check(request, self.requests[-1][2], preview=True))

    # ------------------------------------------------------------------ writes

    def verify(self, primary: str, message: Any, signature: Any) -> tuple[dict, str, bytes]:
        values, signable = typed(primary, message)
        if message["environment"] != self.environment or values["verifyingChainId"] != self.chain_id:
            raise Refusal("WRONG_ENVIRONMENT")
        if primary == "StrategyStart" and (message["mode"] not in ("paper", "live") or message["handoverStops"] != []):
            raise Refusal("INVALID_REQUEST", "mode or handoverStops")
        raw = bytes.fromhex(str(signature or "0x")[2:]) if re.fullmatch(r"0x[0-9a-fA-F]*", str(signature)) else b""
        if len(raw) != 65 or int.from_bytes(raw[32:64], "big") > N // 2:
            raise Refusal("BAD_SIGNATURE")
        signer = Account.recover_message(signable, signature=raw)
        if signer.lower() != message["signer"].lower():
            raise Refusal("BAD_SIGNATURE")
        digest = keccak(b"\x19" + signable.version + signable.header + signable.body)
        return values, signer, digest

    def judge_signer(self, signer: str, account: str, values: dict, digest: bytes) -> dict | None:
        """The signer's checks: a committed request answers its receipt again; then the deadline, the hosted
        signer, the owner or a delegate, the nonce."""
        prior = self.accepted.get(digest)
        if prior is not None:
            return prior
        now = int(time.time())
        if values["deadline"] <= now:
            raise Refusal("REQUEST_EXPIRED")
        if values["deadline"] > now + 600:
            raise Refusal("DEADLINE_TOO_FAR")
        if signer.lower() == self.hosted.lower():
            raise Refusal("HOSTED_SIGNER_REJECTED")
        owner = self.accounts.get(account)
        if owner is None:
            raise Refusal("NOT_PERMITTED")
        if signer.lower() != owner.lower() and signer.lower() not in self.delegates.get(owner.lower(), set()):
            raise Refusal("NOT_PERMITTED")
        if values["nonce"] <= self.nonces.get(signer.lower(), -1):
            raise Refusal("NONCE_TOO_LOW")
        return None

    def commit(self, signer: str, values: dict, digest: bytes, receipt: dict) -> None:
        self.nonces[signer.lower()] = values["nonce"]
        self.accepted[digest] = receipt

    def lost(self) -> web.Response | None:
        """After applying a write: the answer lost on the way back, when a test asks for it."""
        mode, self.next_write = self.next_write, None
        if mode == "lose":
            return web.Response(status=504, text="gateway timeout")
        return None

    async def start(self, request: web.Request) -> web.Response:
        if self.next_write == "drop":
            self.next_write = None
            return web.Response(status=502, text="<html>bad gateway</html>", content_type="text/html")
        body = strict(await request.text())
        if not isinstance(body, dict) or set(body) != {"document", "request", "signature"}:
            raise Refusal("INVALID_REQUEST", "a Start has exactly document, request and signature")
        message = body["request"]
        values, signer, digest = self.verify("StrategyStart", message, body["signature"])
        prior = self.judge_signer(signer, message["accountId"], values, digest)
        if prior is not None:
            return web.json_response(prior, status=200)
        mode = message["mode"]
        if not (self.live_enabled if mode == "live" else self.paper_enabled):
            raise Refusal("MODE_NOT_ALLOWED")
        account = message["accountId"]
        if account not in self.agent_accounts:
            raise Refusal("NOT_AGENT_ACCOUNT", details={"reason": "not_tagged"})
        owner = self.accounts[account].lower()
        if self.hosted.lower() not in self.hosted_enabled.get(owner, set()):
            raise Refusal("HOSTED_SIGNER_NOT_ENABLED")
        record = self.previews.get(message["previewId"])
        now = int(time.time())
        if record is None or now - record["created"] > PREVIEW_SECS:
            raise Refusal("START_REFUSED", "the preview is gone", details={"reason": "card_expired"})
        if record["used"]:
            raise Refusal("START_REFUSED", "the preview already started a run", details={"reason": "preview_used"})
        signed = (account, message["versionHash"], message["expiresAt"], mode, message["fireNow"])
        if signed != (
            record["account_id"],
            record["version_hash"],
            record["expires_at"],
            record["mode"],
            record["fire_now"],
        ):
            raise Refusal("DOCUMENT_MISMATCH")
        if (
            not isinstance(body["document"], str)
            or "0x" + keccak(text=body["document"]).hex() != message["versionHash"]
        ):
            raise Refusal("DOCUMENT_MISMATCH", "the document is not the previewed one")
        if mode == "live":
            live = [
                r
                for r in self.runs.values()
                if r["account_id"] == account and r["mode"] == "live" and r["state"] in ("active", "held")
            ]
            if live:
                raise Refusal(
                    "ACCOUNT_STRATEGY_LIMIT",
                    "the account runs one Live strategy",
                    details={"limit": 1, "live_strategy": live[0]["run_id"]},
                )
        # Everything that can fail comes before the run is allocated and the preview is used.
        doc = strict(body["document"])
        actions = doc.get("actions", {}) if isinstance(doc, dict) else None
        if not isinstance(actions, dict):
            raise Refusal("INVALID_DOCUMENT", "actions are an object")
        markets = sorted({a["market"] for a in actions.values() if isinstance(a, dict) and "market" in a})
        run_id = str(self.next_run)
        self.next_run += 1
        record["used"] = True
        self.runs[run_id] = {
            "format": "3",
            "run_id": run_id,
            "account_id": account,
            "strategy_id": record["strategy_id"],
            "label": record["label"],
            "mode": mode,
            "state": "active",
            "status": "running",
            "end_reason": None,
            "markets": markets,
            "holds_markets": markets,
            "since": str(now),
            "expires": record["expires_at"],
            "needs_you_count": "0",
            "document": body["document"],
            "version_hash": message["versionHash"],
            "preview_id": message["previewId"],
            "paper": record["paper"],
            "signer": signer,
        }
        self.activity[run_id] = [
            {"version": "1", "code": "start", "event": "start", "scope": "0", "reason": "", "at": str(now)}
        ]
        receipt = {
            "run_id": run_id,
            "strategy_id": record["strategy_id"],
            "mode": mode,
            "status_url": f"/v1/runs/{run_id}",
        }
        self.commit(signer, values, digest, receipt)
        return self.lost() or web.json_response(receipt, status=201)

    async def stop(self, request: web.Request) -> web.Response:
        if self.next_write == "drop":
            self.next_write = None
            return web.Response(status=502, text="<html>bad gateway</html>", content_type="text/html")
        path_run = request.match_info["run_id"]
        body = strict(await request.text())
        if not isinstance(body, dict) or set(body) != {"request", "signature"}:
            raise Refusal("INVALID_REQUEST", "a Stop has exactly request and signature")
        message = body["request"]
        values, signer, digest = self.verify("StrategyStop", message, body["signature"])
        if message["runId"] != path_run:
            raise Refusal("INVALID_REQUEST", "the path names another run")
        if int(path_run) < FIRST_V3_RUN:
            raise Refusal("RUN_ON_PREVIOUS_VERSION")
        prior = self.judge_signer(signer, message["accountId"], values, digest)
        if prior is not None:
            return web.json_response(prior, status=200)
        run = self.runs.get(path_run)
        if run is None or run["account_id"] != message["accountId"] or run["strategy_id"] != message["strategyId"]:
            raise Refusal("RUN_NOT_FOUND")
        if run["state"] not in ("active", "held"):
            raise Refusal("STOP_REFUSED", "the run has ended", details={"reason": "run_ended"})
        keep = message["keepPosition"]
        if keep and run["mode"] == "paper":
            raise Refusal(
                "INVALID_REQUEST", "a Paper run stops only by closing", details={"reason": "paper_keep_refused"}
            )
        run.update(state="stopped", status="ended", end_reason="stop_keep" if keep else "stop_close", holds_markets=[])
        run["ended_at"] = str(int(time.time()))
        self.activity[path_run].append(
            {
                "version": "2",
                "code": "ended",
                "event": "stop",
                "scope": "0",
                "reason": run["end_reason"],
                "at": run["ended_at"],
            }
        )
        receipt = {"run_id": path_run, "keep_position": keep, "status_url": f"/v1/runs/{path_run}"}
        self.commit(signer, values, digest, receipt)
        return self.lost() or web.json_response(receipt, status=200)

    # ------------------------------------------------------------------ reads

    def summary(self, run: dict) -> dict:
        keys = (
            "format", "run_id", "account_id", "strategy_id", "label", "mode", "state", "status", "end_reason",
            "markets", "holds_markets", "since", "expires", "needs_you_count",
        )  # fmt: skip
        return {key: run[key] for key in keys}

    def find(self, run_id: str) -> dict:
        if int(run_id) and int(run_id) < FIRST_V3_RUN:
            raise Refusal("RUN_ON_PREVIOUS_VERSION")
        run = self.runs.get(run_id)
        if run is None:
            raise Refusal("RUN_NOT_FOUND")
        return run

    async def run_read(self, request: web.Request) -> web.Response:
        run = self.find(request.match_info["run_id"])
        answer = self.summary(run)
        if request[GRANT_OK]:
            answer["detail"] = {
                "version": "3",
                "read_at": str(int(time.time())),
                "expires": run["expires"],
                "ended_at": run.get("ended_at"),
                "strategy": {
                    "version_hash": run["version_hash"],
                    "preview_id": run["preview_id"],
                    "document": run["document"],
                },
                "result": {
                    "basis": "since_start",
                    "known": True,
                    "total": "-0.004",
                    "settled": "-0.005",
                    "unrealised": "0.001",
                },
                # What the run still holds: each market it holds a position in, and after its close none.
                "positions": [
                    {"market": m, "qty": "0.01", "entry": "1680.4", "basis": "paper_book"} for m in run["holds_markets"]
                ]
                or [{"market": m, "qty": "0", "entry": None, "basis": "at_end"} for m in run["markets"]],
                "protection": None,
                "needs_you": [],
                "odds": [],
                "scopes": [],
                "paper": (
                    # The run's book starts from its margin amount; the fake fills nothing that would move it.
                    {"settings": run["paper"], "balance": run["paper"]["margin"]["amount"], "resting_orders": []}
                    if run["mode"] == "paper"
                    else None
                ),
            }
            answer["detail_reason"] = None
        else:
            answer["detail"] = None
            answer["detail_reason"] = "read_grant_required"
        return web.json_response(answer)

    async def activity_read(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        self.find(run_id)
        limit = int(request.query.get("limit", "20"))
        return web.json_response({"activity": self.activity[run_id][-limit:], "next_before": None, "attempts": []})

    async def account_runs(self, request: web.Request) -> web.Response:
        account = request.match_info["account_id"]
        state = request.query.get("state", "active")
        limit = int(request.query.get("limit", "50"))
        before = request.query.get("before")
        runs = sorted((r for r in self.runs.values() if r["account_id"] == account), key=lambda r: -int(r["run_id"]))
        live = next((r for r in runs if r["mode"] == "live" and r["state"] in ("active", "held")), None)
        if state == "active":
            runs = [live] if live else []
        if before is not None:
            runs = [r for r in runs if int(r["run_id"]) < int(before)]
        page = runs[:limit]
        more = len(runs) > limit
        return web.json_response(
            {
                "runs": [self.summary(r) for r in page],
                "next_before": page[-1]["run_id"] if more else None,
                "live_strategy": (
                    {"format": "3", "run_id": live["run_id"], "strategy_id": live["strategy_id"], "status": live["status"],
                     "since": live["since"], "label": live["label"], "markets": live["markets"], "stops_paused": False,
                     "stop_keep_blocked": False, "as_of": str(int(time.time()))}
                    if live
                    else None
                ),
            }
        )  # fmt: skip


class FakeAccountRelay:
    """The kit's AccountRelay for owner reads only, answered by `service` (see FakeWorkflowService.relay)."""

    service: FakeWorkflowService

    def __init__(self, config):
        self.owner = config.owner_wallet_address

    async def __aenter__(self) -> "FakeAccountRelay":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def request(self, method: str, path: str, **kwargs) -> Any:
        assert method == "GET", f"the kit only reads the trading API here, not {method} {path}"
        return self.service.trading_read(path, self.owner)

    async def accounts(self) -> dict[int, str]:
        rows = self.service.trading_read(f"/v2/wallet/{self.owner}/accounts", self.owner)
        return {row["accountId"]: row["type"] for row in rows}
