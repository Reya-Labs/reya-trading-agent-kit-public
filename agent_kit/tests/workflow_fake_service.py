"""An in-memory stand-in for the hosted workflow service, for the workflow command tests.

It serves the published API (`/v1/info`, validate, preview, start, stop, run status,
activity and account runs) on 127.0.0.1, under `base_path` the way the API host's gateway
serves the real one (`/workflows/v1/info`; anything outside the prefix is a plain 404), and
checks requests the way the service's
contract describes: exact fields, environment and chain, deadline, then the
signature, which it recovers independently with eth_account rather than with the
kit's own code. Then who may sign, the per-signer nonce, the document against the
signed fields, the Agent account, the hosted signer and one active run per account.

Tests change its state directly (approvals, marks, events) and can make the next
write fail or lose its answer.

Read grants on the three run reads follow `read_grants`: ignored, as by a service from before
grants (the default); checked when sent ("check") or also required ("require"), as the service
will, with its own decoding and eth_account's recovery, never the kit's; or refused with a bare
401 and no error envelope, as by a gateway that does not take the header ("refuse"). A grant the
service does not take is INVALID_READ_GRANT with a `reason`, none sent where one is required is
READ_GRANT_REQUIRED, and a run the grant does not cover reads as RUN_NOT_FOUND.

`relay()` stands in for the trading API's owner reads the kit makes through AccountRelay
(the legacy approval list and the owner's accounts), answered from the same state: an
approval the tests give here is one both the service and the approval read see, as the
chain shares it between them.
"""

from typing import Any

import base64
import json
import re
import time
from dataclasses import dataclass, field

from aiohttp import web
from aiohttp.test_utils import TestServer
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

DOMAIN = {"name": "Reya Workflows", "version": "1"}
TYPES = {
    "WorkflowStart": [
        {"name": "verifyingChainId", "type": "uint256"},
        {"name": "environment", "type": "string"},
        {"name": "accountId", "type": "uint128"},
        {"name": "versionHash", "type": "bytes32"},
        {"name": "allocationUsd", "type": "string"},
        {"name": "expiresAt", "type": "uint64"},
        {"name": "hostedSigner", "type": "address"},
        {"name": "mode", "type": "string"},
        {"name": "fireNow", "type": "bool"},
        {"name": "signer", "type": "address"},
        {"name": "nonce", "type": "uint64"},
        {"name": "deadline", "type": "uint64"},
    ],
    "WorkflowStop": [
        {"name": "verifyingChainId", "type": "uint256"},
        {"name": "environment", "type": "string"},
        {"name": "accountId", "type": "uint128"},
        {"name": "runId", "type": "uint64"},
        {"name": "keepPosition", "type": "bool"},
        {"name": "signer", "type": "address"},
        {"name": "nonce", "type": "uint64"},
        {"name": "deadline", "type": "uint64"},
    ],
}
# The build contract's ReadGrant (§6), written out here rather than taken from the kit.
READ_GRANT_TYPE = [
    {"name": "verifyingChainId", "type": "uint256"},
    {"name": "environment", "type": "string"},
    {"name": "owner", "type": "address"},
    {"name": "accountIds", "type": "uint128[]"},
    {"name": "expiresAt", "type": "uint64"},
    {"name": "salt", "type": "bytes16"},
]
READ_PATH = re.compile(r"/v1/runs/([0-9]+)(?:/activity)?|/v1/accounts/([0-9]+)/runs")
STATUS = {
    "INVALID_REQUEST": 400,
    "INVALID_DOCUMENT": 400,
    "DOCUMENT_MISMATCH": 400,
    "WRONG_ENVIRONMENT": 400,
    "MODE_NOT_ALLOWED": 400,
    "ABOVE_PILOT_LIMIT": 400,
    "BAD_SIGNATURE": 401,
    "INVALID_READ_GRANT": 401,
    "READ_GRANT_REQUIRED": 401,
    "REQUEST_EXPIRED": 401,
    "DEADLINE_TOO_FAR": 401,
    "HOSTED_SIGNER_REJECTED": 403,
    "NOT_PERMITTED": 403,
    "NOT_AGENT_ACCOUNT": 403,
    "HOSTED_SIGNER_NOT_ENABLED": 403,
    "UNKNOWN_HOSTED_SIGNER": 403,
    "RUN_NOT_FOUND": 404,
    "NONCE_TOO_LOW": 409,
    "ACCOUNT_HAS_ACTIVE_RUN": 409,
    "RATE_LIMITED": 429,
    "BUSY": 503,
    "NOT_READY": 503,
}
UINT = re.compile(r"(0|[1-9][0-9]*)")
DECIMAL = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]*[1-9])?")
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


class Refusal(Exception):
    def __init__(self, code: str, message: str = "", details: dict | None = None):
        super().__init__(message or code)
        self.code = code
        self.details = details or {}


@dataclass
class FakeWorkflowService:
    environment: str = "devnet1"
    chain_id: int = 89346162
    hosted: str = "0x90F79bf6EB2c4f870365E785982E1f101E93b906"
    modes: list = field(default_factory=lambda: ["shadow"])
    max_allocation_usd: str = "500"
    max_order_usd: str = "100"
    # account -> owner; owner -> approved delegates; owner -> approved hosted signers
    accounts: dict = field(default_factory=dict)
    # The accounts tagged as Agent accounts: the trading read reports them AGENTPERP, and start refuses others.
    agent_accounts: set = field(default_factory=set)
    delegates: dict = field(default_factory=dict)
    hosted_enabled: dict = field(default_factory=dict)
    marks: dict = field(default_factory=lambda: {"BTCRUSDPERP": "91420"})
    runs: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    nonces: dict = field(default_factory=dict)
    accepted: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)
    info_override: dict | None = None
    canonical_override: str | None = None
    # "lose": apply the next write, then answer 504 with no envelope; "drop": answer 502 without applying it.
    next_write: str | None = None
    next_refusal: str | None = None
    # Where the API is mounted, as the gateway mounts it on the API host; "" serves it at the root.
    base_path: str = "/workflows"
    # Every request's path and query exactly as it arrived, prefix included.
    raw_paths: list = field(default_factory=list)
    # "ignore", "check", "require" or "refuse" (see the module docstring).
    read_grants: str = "ignore"
    # (method, path, Authorization header or None) for every request.
    authorizations: list = field(default_factory=list)
    # The signer recovered from each grant "check" accepted.
    grant_signers: list = field(default_factory=list)
    # The trading API's owner reads (see relay()): each path read, and an error to raise instead.
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
        """The base URL a client is configured with: the origin plus `base_path`."""
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.port}{self.base_path}"

    async def start_server(self) -> None:
        app = web.Application()
        app.router.add_get("/v1/info", self.info)
        app.router.add_post("/v1/validate", self.validate)
        app.router.add_post("/v1/preview", self.preview)
        app.router.add_post("/v1/runs", self.start)
        app.router.add_get("/v1/runs/{run_id}", self.run_status)
        app.router.add_post("/v1/runs/{run_id}/stop", self.stop)
        app.router.add_get("/v1/runs/{run_id}/activity", self.activity)
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
        body = await request.json() if request.can_read_body else None
        self.raw_paths.append(request.path_qs)
        # The service's own view of the path, as after the gateway strips the prefix.
        path = request.path.removeprefix(self.base_path)
        self.requests.append((request.method, path, body))
        authorization = request.headers.get("Authorization")
        self.authorizations.append((request.method, path, authorization))
        if authorization is not None and self.read_grants == "refuse":
            return web.Response(status=401, text="unauthorized")
        try:
            if authorization is not None and self.read_grants in ("check", "require"):
                self.check_grant(request.method, path, authorization)
            elif self.read_grants == "require" and request.method == "GET" and READ_PATH.fullmatch(path):
                raise Refusal("READ_GRANT_REQUIRED")
            if request.method == "POST" and path.startswith("/v1/runs") and self.next_refusal:
                code, self.next_refusal = self.next_refusal, None
                raise Refusal(code)
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

    # ------------------------------------------------------------ documents

    @staticmethod
    def canonical(doc: dict) -> str:
        doc = json.loads(json.dumps(doc))
        doc["limits"].setdefault("price_bound_bps", 25)
        for step in doc["steps"]:
            step.setdefault("conditions", [])
            step.setdefault("next", None)
        if doc.get("protection", "absent") is None:
            del doc["protection"]
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    def admit(self, text: str) -> tuple[dict, str]:
        try:
            doc = json.loads(text)
        except ValueError:
            raise Refusal("INVALID_DOCUMENT", "not JSON") from None
        errors = []
        if doc.get("schema") != "reya.workflow/1":
            errors.append({"code": "schema", "message": "schema must be reya.workflow/1"})
        if doc.get("environment") != self.environment:
            errors.append({"code": "environment", "message": f"this service runs {self.environment}"})
        if doc.get("market") not in self.marks:
            errors.append({"code": "unknown_market", "message": f"unknown market {doc.get('market')}"})
        if errors:
            raise _Invalid(errors)
        return doc, self.canonical_override or self.canonical(doc)

    async def validate(self, request: web.Request) -> web.Response:
        try:
            _, canonical = self.admit((await request.json())["document"])
        except _Invalid as invalid:
            return web.json_response({"ok": False, "errors": invalid.errors})
        return web.json_response(
            {"ok": True, "canonical": canonical, "version_hash": "0x" + keccak(text=canonical).hex(), "warnings": []}
        )

    async def preview(self, request: web.Request) -> web.Response:
        body = await request.json()
        # As the service's PreviewRequest: the document and nothing else.
        if not isinstance(body, dict) or set(body) != {"document"}:
            raise Refusal("INVALID_REQUEST", "fields")
        try:
            doc, canonical = self.admit(body["document"])
        except _Invalid as invalid:
            return web.json_response({"ok": False, "errors": invalid.errors})
        trigger = doc["steps"][0]["trigger"]
        mark, level = self.marks[doc["market"]], trigger["level"]
        distance = round(abs(float(mark) - float(level)) / float(mark) * 10_000)
        side = "below" if float(level) < float(mark) else "above" if float(level) > float(mark) else "at"
        triggered = float(mark) <= float(level) if trigger["direction"] == "down" else float(mark) >= float(level)
        now = str(int(time.time()))
        first = (
            f"the mark is {mark}, already at or below the level of {level}: fire now, or wait for the mark to "
            f"move 0.5% back above the level and cross it again"
            if triggered
            else f"the mark is {mark}; this would arm {distance / 100:.2f}% {side}, at {level}"
        )
        # The shape of the hosted service's preview response.
        return web.json_response(
            {
                "ok": True,
                "version_hash": "0x" + keccak(text=canonical).hex(),
                "as_of": now,
                "market": {"symbol": doc["market"], "mark": mark, "mark_at": now},
                "trigger": {
                    "level": level,
                    "direction": trigger["direction"],
                    "distance_bps": str(distance),
                    "side": side,
                    "already_triggered": triggered,
                },
                "order": {
                    "side": "buy",
                    "qty": "0.0002",
                    "limit_px": "90225",
                    "reserved_usd": "18.045",
                    "bound_bps": "25",
                    "orders_covered": "11",
                },
                "order_error": None,
                "allocation_usd": doc["limits"]["allocation_usd"],
                "funding": {
                    "samples": "180",
                    "latest": {"pct_per_hour": "0.0004", "at": now},
                    "average_15m_pct_per_hour": "0.0003",
                },
                "warnings": [],
                "lines": [
                    first,
                    "one order at the level: buy 0.0002 at a limit of 90,225 (25 bps max slippage), reserving "
                    "$18.05 of the $200 allocation, which covers 11 orders",
                ],
            }
        )

    # --------------------------------------------------------------- signing

    def info_body(self) -> dict:
        body = {
            "environment": self.environment,
            "chain_id": str(self.chain_id),
            "eip712": {"domain": DOMAIN, "types": TYPES},
            "hosted_signers": [{"address": self.hosted, "role": "active", "can_sign": True}],
            "modes": self.modes,
            "limits": {
                "max_allocation_usd": self.max_allocation_usd,
                "max_order_usd": self.max_order_usd,
                "max_deadline_secs": "600",
            },
        }
        return {**body, **(self.info_override or {})}

    async def info(self, request: web.Request) -> web.Response:
        return web.json_response(self.info_body())

    def verify(self, primary: str, body: Any) -> tuple[dict, str, bytes]:
        if not isinstance(body, dict) or not isinstance(body.get("request"), dict):
            raise Refusal("INVALID_REQUEST")
        message = body["request"]
        names = [f["name"] for f in TYPES[primary]]
        if set(message) != set(names):
            raise Refusal("INVALID_REQUEST", "fields")
        values: dict[str, Any] = {}
        for f in TYPES[primary]:
            value = message[f["name"]]
            if f["type"].startswith("uint"):
                if not isinstance(value, str) or not UINT.fullmatch(value):
                    raise Refusal("INVALID_REQUEST", f["name"])
                value = int(value)
            elif f["type"] == "bytes32":
                value = bytes.fromhex(value[2:])
            elif f["type"] == "bool" and type(value) is not bool:
                raise Refusal("INVALID_REQUEST", f["name"])
            values[f["name"]] = value
        if message["environment"] != self.environment or values["verifyingChainId"] != self.chain_id:
            raise Refusal("WRONG_ENVIRONMENT")
        now = int(time.time())
        if values["deadline"] <= now:
            raise Refusal("REQUEST_EXPIRED")
        if values["deadline"] > now + 600:
            raise Refusal("DEADLINE_TOO_FAR")
        signable = encode_typed_data(
            full_message={
                "types": {
                    "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}],
                    primary: TYPES[primary],
                },
                "primaryType": primary,
                "domain": DOMAIN,
                "message": values,
            }
        )
        signature = bytes.fromhex(str(body.get("signature", "0x"))[2:])
        if len(signature) != 65 or int.from_bytes(signature[32:64], "big") > N // 2:
            raise Refusal("BAD_SIGNATURE")
        signer = Account.recover_message(signable, signature=signature)
        if signer.lower() != message["signer"].lower():
            raise Refusal("BAD_SIGNATURE")
        digest = keccak(b"\x19" + signable.version + signable.header + signable.body)
        if signer.lower() == self.hosted.lower():
            raise Refusal("HOSTED_SIGNER_REJECTED")
        owner = self.accounts.get(message["accountId"])
        if owner is None or (
            signer.lower() != owner.lower() and signer.lower() not in self.delegates.get(owner.lower(), set())
        ):
            raise Refusal("NOT_PERMITTED")
        prior = self.accepted.get((signer.lower(), values["nonce"]))
        if prior is not None and prior[0] == digest:
            return message, signer, digest
        if values["nonce"] <= self.nonces.get(signer.lower(), -1):
            raise Refusal("NONCE_TOO_LOW")
        return message, signer, digest

    def check_grant(self, method: str, path: str, authorization: str) -> None:
        """The service's checks on a read grant, cheap ones first; a run it does not cover reads as not found."""
        match = READ_PATH.fullmatch(path)
        if method != "GET" or match is None:
            raise Refusal("INVALID_REQUEST", "a read grant goes only with a run read")
        try:
            assert authorization.startswith("Bearer ")
            token = authorization.removeprefix("Bearer ")
            body = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            assert set(body) == {"grant", "signature"}
            grant, signature = body["grant"], bytes.fromhex(body["signature"][2:])
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
                        "types": {
                            "EIP712Domain": [
                                {"name": "name", "type": "string"},
                                {"name": "version", "type": "string"},
                            ],
                            "ReadGrant": READ_GRANT_TYPE,
                        },
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
        # Delegates only: never the owner's own key.
        if signer.lower() not in self.delegates.get(owner, set()):
            raise Refusal("INVALID_READ_GRANT", details={"reason": "not_delegate"})
        account = (self.runs.get(match.group(1)) or {}).get("account_id") if match.group(1) else match.group(2)
        if (
            account is None
            or self.accounts.get(account, "").lower() != owner
            or (grant["accountIds"] and account not in grant["accountIds"])
        ):
            raise Refusal("RUN_NOT_FOUND")
        self.grant_signers.append(signer)

    def accept(self, signer: str, nonce: str, digest: bytes, response: dict) -> None:
        self.nonces[signer.lower()] = int(nonce)
        self.accepted[(signer.lower(), int(nonce))] = (digest, response)

    def replay(self, signer: str, nonce: str, digest: bytes) -> dict | None:
        prior = self.accepted.get((signer.lower(), int(nonce)))
        return {**prior[1], "replayed": True} if prior and prior[0] == digest else None

    def lose_or_drop(self) -> web.Response | None:
        mode, self.next_write = self.next_write, None
        if mode == "drop":
            return web.Response(status=502, text="<html>bad gateway</html>", content_type="text/html")
        return None

    # ------------------------------------------------------------------ runs

    async def start(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.next_write == "drop":
            return self.lose_or_drop()  # type: ignore[return-value]
        message, signer, digest = self.verify("WorkflowStart", body)
        replayed = self.replay(signer, message["nonce"], digest)
        if replayed:
            return web.json_response(replayed, status=200)
        try:
            doc, canonical = self.admit(body.get("document", ""))
        except _Invalid:
            raise Refusal("INVALID_DOCUMENT") from None
        if (
            "0x" + keccak(text=canonical).hex() != message["versionHash"].lower()
            or doc["account_id"] != message["accountId"]
            or doc["limits"]["allocation_usd"] != message["allocationUsd"]
            or str(doc["limits"]["expires_at"]) != message["expiresAt"]
        ):
            raise Refusal("DOCUMENT_MISMATCH")
        if message["mode"] not in self.modes:
            raise Refusal("MODE_NOT_ALLOWED")
        if float(message["allocationUsd"]) > float(self.max_allocation_usd):
            raise Refusal("ABOVE_PILOT_LIMIT")
        if message["accountId"] not in self.agent_accounts:
            raise Refusal("NOT_AGENT_ACCOUNT", details={"reason": "not_tagged"})
        if message["hostedSigner"].lower() != self.hosted.lower():
            raise Refusal("UNKNOWN_HOSTED_SIGNER")
        owner = self.accounts[message["accountId"]]
        if self.hosted.lower() not in self.hosted_enabled.get(owner.lower(), set()):
            raise Refusal("HOSTED_SIGNER_NOT_ENABLED")
        if any(r["account_id"] == message["accountId"] and r["state"] == "active" for r in self.runs.values()):
            raise Refusal("ACCOUNT_HAS_ACTIVE_RUN")
        run_id = str(len(self.runs) + 1)
        now = str(int(time.time()))
        level = doc["steps"][0]["trigger"]["level"]
        self.runs[run_id] = {
            "run_id": run_id,
            "workflow_id": "1",
            "account_id": message["accountId"],
            "owner": owner,
            "market": doc["market"],
            "state": "active",
            "mode": message["mode"],
            "phase": "armed",
            "armed": {"level": level, "last_mark": self.marks[doc["market"]], "distance_bps": "155"},
            "stale": None,
            "allocation": {
                "total_usd": message["allocationUsd"],
                "reserved_usd": "0",
                "spent_usd": "0",
                "remaining_usd": message["allocationUsd"],
            },
            # The service places no stop at start: only after the first fill settles.
            "protection": None,
            "starter": signer,
            "hosted_signer": self.hosted,
            "created_at": now,
            "expires_at": message["expiresAt"],
            "completion": None,
            "stop": None,
            "document": {"canonical": canonical, "version_hash": message["versionHash"].lower()},
            "last_seq": "2",
        }
        self.events[run_id] = [
            self.event(1, "started", "user", "started", f"Started by {signer} in {message['mode']} mode."),
            self.event(2, "armed", "system", "armed", f"armed: watching for the mark to cross down through {level}"),
        ]
        response = {
            "run_id": run_id,
            "workflow_id": "1",
            "version_hash": message["versionHash"].lower(),
            "account_id": message["accountId"],
            "state": "active",
            "mode": message["mode"],
            "starter": signer,
            "hosted_signer": self.hosted,
            "allocation_usd": message["allocationUsd"],
            "expires_at": message["expiresAt"],
            "status_url": f"/v1/runs/{run_id}",
        }
        self.accept(signer, message["nonce"], digest, response)
        if self.next_write == "lose":
            self.next_write = None
            return web.Response(status=504, text="upstream timed out")
        return web.json_response(response, status=201)

    def event(self, seq: int, kind: str, actor: str, code: str, text: str, firing: dict | None = None) -> dict:
        entry: dict[str, Any] = {"seq": str(seq), "at": str(int(time.time())), "kind": kind, "actor": actor}
        entry["code"] = code
        entry |= {"text": text, "inputs": {}}
        if firing is not None:
            entry["firing"] = firing
        return entry

    def add_event(self, run_id: str, kind: str, code: str, text: str, firing: dict | None = None) -> None:
        trail = self.events[run_id]
        trail.append(self.event(len(trail) + 1, kind, "system", code, text, firing))

    def get(self, request: web.Request) -> dict:
        run = self.runs.get(request.match_info["run_id"])
        if run is None:
            raise Refusal("RUN_NOT_FOUND")
        return run

    async def run_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.get(request))

    async def activity(self, request: web.Request) -> web.Response:
        run = self.get(request)
        after = int(request.query.get("after", "0"))
        limit = int(request.query.get("limit", "50"))
        trail = [e for e in self.events[run["run_id"]] if int(e["seq"]) > after]
        page = trail[:limit]
        more = len(trail) > limit
        return web.json_response(
            {"run_id": run["run_id"], "events": page, "next_after": page[-1]["seq"] if more else None}
        )

    async def account_runs(self, request: web.Request) -> web.Response:
        account = request.match_info["account_id"]
        state = request.query.get("state", "active")
        runs = [
            {key: r[key] for key in ("run_id", "state", "mode", "market", "phase", "created_at", "completion", "stop")}
            for r in sorted(self.runs.values(), key=lambda r: -int(r["run_id"]))
            if r["account_id"] == account and (state == "all" or r["state"] == "active")
        ]
        return web.json_response({"runs": runs, "next_before": None})

    async def stop(self, request: web.Request) -> web.Response:
        run = self.get(request)
        body = await request.json()
        if self.next_write == "drop":
            return self.lose_or_drop()  # type: ignore[return-value]
        message, signer, digest = self.verify("WorkflowStop", body)
        if message["runId"] != run["run_id"] or message["accountId"] != run["account_id"]:
            raise Refusal("RUN_NOT_FOUND")
        replayed = self.replay(signer, message["nonce"], digest)
        if replayed:
            return web.json_response(replayed)
        if run["state"] == "stopped":
            return web.json_response(
                {"run_id": run["run_id"], "state": "stopped", "stop": run["stop"], "protection": run["protection"]}
                | {"close": run.get("close"), "already_stopped": True}
            )
        keep = message["keepPosition"]
        run["state"], run["phase"] = "stopped", "done"
        run["stop"] = {
            "by": "user",
            "reason": "user",
            "keep_position": keep,
            "signer": signer,
            "at": str(int(time.time())),
        }
        run["close"] = None if keep else close_of(run["mode"])
        if run["protection"]:
            run["protection"] = {**run["protection"], "state": "cancel_pending"}
        self.add_event(run["run_id"], "stopped", "stopped", "stopped by a signed request")
        response = {
            "run_id": run["run_id"],
            "state": "stopped",
            "stop": run["stop"],
            "close": run["close"],
            "protection": run["protection"],
        }
        self.accept(signer, message["nonce"], digest, response)
        if self.next_write == "lose":
            self.next_write = None
            return web.Response(status=504, text="upstream timed out")
        return web.json_response(response)


def close_of(mode: str) -> dict:
    """A close as the stop that opens it answers: a shadow run's has nothing to close."""
    shadow = mode == "shadow"
    return {
        "state": "nothing_to_close" if shadow else "pending",
        "attempts": "0",
        "max_attempts": "3",
        "close_retry_secs": "20",
        "simulated": mode != "production",
        "closed_qty": "0",
        "open_qty": None,
        "next_attempt_at": None,
    }


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


class _Invalid(Exception):
    def __init__(self, errors: list):
        super().__init__("invalid")
        self.errors = errors
