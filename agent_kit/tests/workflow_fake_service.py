"""An in-memory stand-in for the hosted strategy service, for the strategy command tests.

It serves the published API (`/v1/info`, validate, preview, start, stop, run status,
activity and account runs) on 127.0.0.1, under `base_path` the way the API host's gateway
serves the real one (`/workflows/v1/info`; anything outside the prefix is a plain 404), and
checks requests the way the service's contract describes: exact fields, environment and
chain, deadline, then the signature, which it recovers independently with eth_account rather
than with the kit's own code. Then who may sign, the per-signer nonce, the document against
the signed fields, the Agent account, the hosted signer, how many strategies the account
runs (`max_live`) and whether another live strategy already trades one of its markets.

Documents are `reya.workflow/2` only, and integers in its answers are decimal strings, as the
service writes them. The `basket` template is expanded by replaying the
examples in fixtures/workflow_v2_examples, copied unchanged from the service's core: the
parameters must match an example's,
except the envelope's own values (environment, account, allocation, expiry, bound and
resting cap), which the expansion copies into the envelope. With the example's own envelope
the answer is its canonical JSON byte for byte. Parameters with no example are refused
(`fake_no_example`), so a test drafts only what an example covers.

Tests change its state directly (approvals, marks, events) and can make the next
write fail or lose its answer.

Validate and preview make the start's Agent-account check on a document that passes, as the
service does from 0.10: another account is `ok: false` with `not_agent_account` and the start's
`reason`. `checks_agent_account = False` answers as a service from before that.

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
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

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
RUN_READ = re.compile(r"/v1/runs/([0-9]+)")
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
    "MARKET_IN_USE": 409,
    "ACCOUNT_STRATEGY_LIMIT": 409,
    "RATE_LIMITED": 429,
    "BUSY": 503,
    "NOT_READY": 503,
}
# The service's started event names the mode as the app does.
MODE_NAMES = {"shadow": "Watch only", "paper": "Paper", "production": "Live"}
UINT = re.compile(r"(0|[1-9][0-9]*)")
DECIMAL = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]*[1-9])?")
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
# The template's examples the fake replays, copied unchanged from the service's core. Each holds the request,
# the document, its canonical JSON and its version hash.
EXAMPLES = [
    json.loads(path.read_text())
    for path in sorted((Path(__file__).parent / "fixtures" / "workflow_v2_examples").glob("*.json"))
]
# The envelope's own values: copied from the parameters into the document, so they never pick the example.
ENVELOPE_PARAMS = ("environment", "account_id", "allocation_usd", "expires_at", "price_bound_bps", "max_resting_orders")
LIVE = ("active", "blocked", "held")


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
    max_allocation_usd: str = "500000"
    max_order_usd: str = "100000"
    # K: how many strategies may be live on one account.
    max_live: int = 3
    # Whether the service prices account margins: `/v1/info` then lists `available_balance` among its signal kinds,
    # and it takes the balance condition and sends the two balance warnings itself.
    prices_margins: bool = False
    # account -> owner; owner -> approved delegates; owner -> approved hosted signers
    accounts: dict = field(default_factory=dict)
    # The accounts tagged as Agent accounts: the trading read reports them AGENTPERP, and start refuses others.
    agent_accounts: set = field(default_factory=set)
    # Whether validate and preview refuse a document for any other account (see the module docstring).
    checks_agent_account: bool = True
    delegates: dict = field(default_factory=dict)
    hosted_enabled: dict = field(default_factory=dict)
    marks: dict = field(default_factory=lambda: {"BTCRUSDPERP": "91420", "ETHRUSDPERP": "2950", "SOLRUSDPERP": "140"})
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
    # Answer a stopped run's status read with a bare 503, as a gateway might: only the stop's own answer is left.
    refuse_stopped_reads: bool = False
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
        read = RUN_READ.fullmatch(path) if request.method == "GET" else None
        if self.refuse_stopped_reads and read and (self.runs.get(read.group(1)) or {}).get("state") == "stopped":
            return web.Response(status=503, text="unavailable")
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
        """The document with the format's defaults filled in, as RFC 8785 text."""
        doc = json.loads(json.dumps(doc))
        doc["envelope"].setdefault("price_bound_bps", 25)
        doc["envelope"].setdefault("max_resting_orders", 20)
        for state in doc["states"].values():
            state.setdefault("on", [])
            for transition in state["on"]:
                transition.setdefault("if", [])
        for action in doc["actions"].values():
            if action["kind"] == "limit_orders":
                action.setdefault("keep", False)
                for order in action["orders"]:
                    order.setdefault("post_only", True)
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    def admit(self, text: str) -> tuple[dict, str]:
        try:
            doc = json.loads(text)
        except ValueError:
            raise Refusal("INVALID_DOCUMENT", "not JSON") from None
        errors = []
        if doc.get("schema") != "reya.workflow/2":
            errors.append({"code": "schema_unknown", "path": "schema", "message": "schema must be reya.workflow/2"})
            raise _Invalid(errors)
        envelope = doc["envelope"]
        if envelope.get("environment") != self.environment:
            errors.append(
                {
                    "code": "wrong_environment",
                    "path": "envelope.environment",
                    "message": f"this service runs {self.environment}",
                }
            )
        for index, market in enumerate(envelope.get("markets") or []):
            if market not in self.marks:
                errors.append(
                    {
                        "code": "market_unknown",
                        "path": f"envelope.markets.{index}",
                        "message": f"unknown market {market}",
                    }
                )
        if errors:
            raise _Invalid(errors)
        return doc, self.canonical_override or self.canonical(doc)

    def expand(self, request: dict) -> tuple[dict, str]:
        """The basket template, by replaying the example whose parameters these are (see the module docstring)."""
        if request.get("template") != {"id": "basket", "version": 1}:
            raise _Invalid([{"code": "template_unknown", "path": "template", "message": "unknown template"}])
        params = request["params"]
        errors = [
            {
                "code": "market_unknown",
                "path": f"params.entry.orders.{i}.market",
                "message": f"unknown market {o['market']}",
            }
            for i, o in enumerate(params["entry"]["orders"])
            if o["market"] not in self.marks
        ]
        if errors:
            raise _Invalid(errors)
        shape = {k: v for k, v in params.items() if k not in ENVELOPE_PARAMS}
        for case in EXAMPLES:
            example = case["request"]["params"]
            if {k: v for k, v in example.items() if k not in ENVELOPE_PARAMS} == shape:
                doc = json.loads(case["canonical"])
                envelope = {k: params[k] for k in ENVELOPE_PARAMS if k in params}
                if all(doc["envelope"][k] == v for k, v in envelope.items()):
                    return doc, case["canonical"]  # the example itself, byte for byte
                doc["envelope"].update(envelope)
                return doc, self.canonical(doc)
        raise _Invalid([{"code": "fake_no_example", "path": "params", "message": "no example for these parameters"}])

    def not_agent_account(self, doc: dict) -> web.Response | None:
        account = doc["envelope"].get("account_id")
        if not self.checks_agent_account or account in self.agent_accounts:
            return None
        message = f"account {account} is not the Agent account: it was not created as the Agent account"
        error = {"code": "not_agent_account", "reason": "not_tagged", "message": message}
        return web.json_response({"ok": False, "errors": [error]})

    async def validate(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            if isinstance(body, dict) and set(body) == {"template", "params"}:
                doc, canonical = self.expand(body)
                canonical = self.canonical_override or canonical
            elif isinstance(body, dict) and set(body) == {"document"}:
                doc, canonical = self.admit(body["document"])
            else:
                raise Refusal("INVALID_REQUEST", "fields")
        except _Invalid as invalid:
            return web.json_response({"ok": False, "errors": invalid.errors})
        if refused := self.not_agent_account(doc):
            return refused
        return web.json_response(
            {"ok": True, "canonical": canonical, "version_hash": "0x" + keccak(text=canonical).hex(), "warnings": []}
        )

    def profit_exits(self, doc: dict) -> list[dict]:
        """One per transition on a crossing of the strategy's price move (`own_profit_pct`), with the entry value at
        the planned sizes (here: every immediate order's dollars) and the level in dollars."""
        entry: Decimal = sum(
            (
                Decimal(o["size"]["usd"])
                if "usd" in o["size"]
                else Decimal(o["size"]["weight"]) * Decimal(a["total_usd"])
            )
            for a in doc["actions"].values()
            if a["kind"] in ("orders", "limit_orders")
            for o in a["orders"]
        ) + Decimal(0)
        exits = []
        for state_id, state in doc["states"].items():
            for index, transition in enumerate(state["on"]):
                signal = doc["signals"][transition["when"]]
                source = doc["signals"].get(signal.get("input", ""), {})
                if signal["kind"] == "crosses" and source.get("kind") == "own_profit_pct":
                    level = Decimal(signal["level"])
                    exits.append(
                        {"state": state_id, "index": str(index), "signal": transition["when"], "market": source.get("market"),
                         "level_pct": signal["level"], "entry_usd": text(entry), "profit_usd": text(level / 100 * entry)}
                    )  # fmt: skip
        return exits

    def live_runs(self, account: str) -> list[dict]:
        return [r for r in self.runs.values() if r["account_id"] == account and r["state"] in LIVE]

    def signal_values(self, doc: dict) -> dict:
        values = {}
        for signal_id, signal in doc["signals"].items():
            kind = signal["kind"]
            if kind == "mark":
                values[signal_id] = {
                    "kind": kind,
                    "value": self.marks[signal["market"]],
                    "age_secs": "1",
                    "stale": False,
                }
            elif kind == "funding_avg":
                values[signal_id] = {"kind": kind, "value": "0.0003", "age_secs": "4", "stale": False}
            elif kind == "position":
                values[signal_id] = {"kind": kind, "value": "0", "age_secs": "1", "stale": False}
            elif kind in ("own_entry", "own_profit_pct", "available_balance"):
                values[signal_id] = {"kind": kind, "value": None, "age_secs": None, "stale": False}
        return values

    def met(self, doc: dict, signal_id: str) -> bool:
        signal = doc["signals"][signal_id]
        if signal["kind"] != "crosses" or doc["signals"][signal["input"]]["kind"] != "mark":
            return False
        mark = Decimal(self.marks[doc["signals"][signal["input"]]["market"]])
        level = Decimal(signal["level"])
        return mark >= level if signal["direction"] == "up" else mark <= level

    def sized(self, doc: dict, action_id: str) -> tuple[list, list, list, list]:
        """(orders, resting, closes, order_errors) one action would send at the current marks."""
        action, bound = doc["actions"][action_id], doc["envelope"].get("price_bound_bps", 25)
        orders: list[dict] = []
        resting: list[dict] = []
        closes: list[str] = []
        errors: list[dict] = []
        if action["kind"] == "close_own":
            closes += doc["envelope"]["markets"] if action["markets"] == "all" else action["markets"]
            return orders, resting, closes, errors
        for order in action["orders"]:
            size = order["size"]
            usd = Decimal(size["usd"]) if "usd" in size else Decimal(size["weight"]) * Decimal(action["total_usd"])
            mark = Decimal(self.marks[order["market"]])
            if action["kind"] == "orders":
                limit = mark * (1 + (bound if order["side"] == "buy" else -bound) / Decimal(10_000))
            elif "fixed" in order["price"]:
                limit = Decimal(order["price"]["fixed"])
            else:
                limit = (mark * (1 + Decimal(order["price"]["offset_bps"]) / 10_000)).quantize(Decimal("0.01"))
            qty = (usd / limit).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            if qty == 0:
                errors.append(
                    {"action": action_id, "order": order["id"], "market": order["market"], "code": "order_below_minimum",
                     "message": "the order is below the market's minimum"}
                )  # fmt: skip
                continue
            row = {
                "action": action_id,
                "order": order["id"],
                "market": order["market"],
                "side": order["side"],
                "qty": text(qty),
                "limit_px": text(limit),
                "reserved_usd": text(qty * limit),
                "stop_px": order.get("stop"),
                "take_profit_px": order.get("take_profit"),
            }
            if action["kind"] == "orders":
                orders.append(row | {"bound_bps": str(bound)})
            else:
                resting.append(row | {"post_only": order.get("post_only", True)})
        return orders, resting, closes, errors

    async def preview(self, request: web.Request) -> web.Response:
        body = await request.json()
        # As the service's PreviewRequest: the document (or the template), and nothing else.
        if not isinstance(body, dict) or set(body) != {"document"}:
            raise Refusal("INVALID_REQUEST", "fields")
        try:
            doc, canonical = self.admit(body["document"])
        except _Invalid as invalid:
            return web.json_response({"ok": False, "errors": invalid.errors})
        if refused := self.not_agent_account(doc):
            return refused
        now = int(time.time())
        markets = sorted(
            set(doc["envelope"]["markets"]) | {s["market"] for s in doc["signals"].values() if s.get("market")}
        )
        start = doc["start"]
        transitions = []
        for index, transition in enumerate(doc["states"][start]["on"]):
            orders: list[dict] = []
            resting: list[dict] = []
            closes: list[str] = []
            errors: list[dict] = []
            for action_id in transition["do"]:
                more = self.sized(doc, action_id)
                orders, resting, closes, errors = (
                    orders + more[0],
                    resting + more[1],
                    closes + more[2],
                    errors + more[3],
                )
            transitions.append(
                {
                    "index": str(index),
                    "already_met": self.met(doc, transition["when"]),
                    "orders": orders,
                    "resting": resting,
                    "closes": closes,
                    "order_errors": errors,
                    "reserved_usd": text(sum((Decimal(o["reserved_usd"]) for o in orders + resting), Decimal(0))),
                }
            )
        warnings: list[dict[str, Any]] = [
            {"code": "order_without_stop", "path": f"actions.{a}.orders.{i}", "message": f"order {o['id']} has no stop"}
            for a, action in doc["actions"].items()
            for i, o in enumerate(action.get("orders") or [])
            if not o.get("stop")
        ]
        for run in self.live_runs(doc["envelope"]["account_id"]):
            for market in doc["envelope"]["markets"]:
                if market in [m["market"] for m in run["markets"]]:
                    warnings.append(
                        {"code": "market_in_use", "message": f"strategy #{run['run_id']} already trades {market}",
                         "details": {"run_id": run["run_id"], "market": market}}
                    )  # fmt: skip
        schedule = [
            {"signal": sid, "next": [str(now + 3600 * k) for k in (1, 2, 3)]}
            for sid, s in doc["signals"].items()
            if s["kind"] in ("every", "at")
        ]
        return web.json_response(
            {
                "ok": True,
                "version_hash": "0x" + keccak(text=canonical).hex(),
                "as_of": str(now),
                "markets": [{"symbol": m, "mark": self.marks[m], "mark_at": str(now)} for m in markets],
                "signals": self.signal_values(doc),
                "states": [
                    {
                        "id": state_id,
                        "final": not state["on"],
                        "transitions": [
                            t
                            | {
                                "index": str(i),
                                "text": f"{t['when']}: {', '.join(t['do']) or 'nothing'}, then {t['go']}",
                            }
                            for i, t in enumerate(state["on"])
                        ],
                    }
                    for state_id, state in sorted(doc["states"].items())  # by id, as the live service lists them
                ],
                "start": start,
                "fire_now_needed": any(t["already_met"] for t in transitions),
                "start_transitions": transitions,
                "schedule": schedule,
                "profit_exits": self.profit_exits(doc),
                "warnings": warnings,
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
                "max_live_strategies_per_account": str(self.max_live),
                "max_orders_per_action": "14",
                "max_resting_orders": "40",
            },
            "schemas": ["reya.workflow/2"],
            "kinds": {
                "signals": ["mark", "funding_avg", "position", "own_entry", "own_profit_pct"]
                + (["available_balance"] if self.prices_margins else []),
                "actions": ["orders", "limit_orders", "close_own"],
            },
            "templates": [{"id": "basket", "version": 1}],
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
        if set(body) - {"document", "request", "signature", "label"}:
            raise Refusal("INVALID_REQUEST", "fields")
        label = body.get("label")
        if label is not None and (not isinstance(label, str) or not 0 < len(label) <= 60):
            raise Refusal("INVALID_REQUEST", "label")
        message, signer, digest = self.verify("WorkflowStart", body)
        replayed = self.replay(signer, message["nonce"], digest)
        if replayed:
            return web.json_response(replayed, status=200)
        try:
            doc, canonical = self.admit(body.get("document", ""))
        except _Invalid:
            raise Refusal("INVALID_DOCUMENT") from None
        envelope = doc["envelope"]
        if (
            "0x" + keccak(text=canonical).hex() != message["versionHash"].lower()
            or envelope["account_id"] != message["accountId"]
            or envelope["allocation_usd"] != message["allocationUsd"]
            or str(envelope["expires_at"]) != message["expiresAt"]
        ):
            raise Refusal("DOCUMENT_MISMATCH")
        if message["mode"] not in self.modes:
            raise Refusal("MODE_NOT_ALLOWED")
        if Decimal(message["allocationUsd"]) > Decimal(self.max_allocation_usd):
            raise Refusal("ABOVE_PILOT_LIMIT")
        if message["accountId"] not in self.agent_accounts:
            raise Refusal("NOT_AGENT_ACCOUNT", details={"reason": "not_tagged"})
        if message["hostedSigner"].lower() != self.hosted.lower():
            raise Refusal("UNKNOWN_HOSTED_SIGNER")
        owner = self.accounts[message["accountId"]]
        if self.hosted.lower() not in self.hosted_enabled.get(owner.lower(), set()):
            raise Refusal("HOSTED_SIGNER_NOT_ENABLED")
        live = self.live_runs(message["accountId"])
        for run in live:
            for market in envelope["markets"]:
                if market in [m["market"] for m in run["markets"]]:
                    raise Refusal("MARKET_IN_USE", f"strategy #{run['run_id']} already trades {market}",
                                  details={"run_id": run["run_id"], "market": market})  # fmt: skip
        if len(live) >= self.max_live:
            raise Refusal(
                "ACCOUNT_STRATEGY_LIMIT", details={"limit": str(self.max_live), "live": [r["run_id"] for r in live]}
            )
        run_id = str(len(self.runs) + 1)
        now = str(int(time.time()))
        self.runs[run_id] = {
            "run_id": run_id,
            "workflow_id": "1",
            "account_id": message["accountId"],
            "owner": owner,
            "label": label,
            "state": "active",
            "mode": message["mode"],
            "graph_state": doc["start"],
            "entered_at": now,
            "final": False,
            "stale": None,
            "allocation": {
                "total_usd": message["allocationUsd"],
                "reserved_usd": "0",
                "spent_usd": "0",
                "remaining_usd": message["allocationUsd"],
            },
            # The service places no stop at start: only after that market's first fill settles.
            "markets": [
                {"market": m, "own_qty": "0", "own_entry": None, "own_profit_pct": None, "own_profit_usd": None,
                 "position_qty": "0", "protection": None, "take_profit": None, "close": None}
                for m in envelope["markets"]
            ],  # fmt: skip
            "profit": {"pct": None, "usd": None, "entry_usd": None},
            "profit_exits": [
                {k: e[k] for k in ("state", "index", "signal", "market", "level_pct")}
                | {"current_usd": None, "target_usd": None}
                for e in self.profit_exits(doc)
            ],
            "signals": self.signal_values(doc),
            "resting_orders": [],
            "next_wake_at": None,
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
            self.event(1, "started", "user", "started", f"Started by {signer} in {MODE_NAMES[message['mode']]} mode."),
            self.event(2, "entered", "system", "entered", f"entered {doc['start']}"),
        ]
        response = {
            "run_id": run_id,
            "workflow_id": "1",
            "version_hash": message["versionHash"].lower(),
            "account_id": message["accountId"],
            "label": label,
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

    def event(self, seq: int, kind: str, actor: str, code: str, text: str, **extra) -> dict:
        entry: dict[str, Any] = {"seq": str(seq), "at": str(int(time.time())), "kind": kind, "actor": actor}
        entry["code"] = code
        entry |= {"text": text, "inputs": {}}
        return entry | extra

    def add_event(self, run_id: str, kind: str, code: str, text: str, **extra) -> None:
        trail = self.events[run_id]
        trail.append(self.event(len(trail) + 1, kind, "system", code, text, **extra))

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
        if state not in ("active", "all"):
            raise Refusal("INVALID_REQUEST", "state")
        before = int(request.query.get("before", str(2**63)))
        limit = int(request.query.get("limit", "50"))
        matching = [
            r
            for r in sorted(self.runs.values(), key=lambda r: -int(r["run_id"]))
            if r["account_id"] == account and (state == "all" or r["state"] == "active") and int(r["run_id"]) < before
        ]
        page = matching[:limit]
        runs = [
            {
                key: r[key]
                for key in ("run_id", "label", "state", "mode", "graph_state", "created_at", "completion", "stop")
            }
            | {
                "markets": [m["market"] for m in r["markets"]],
                "holds_markets": [
                    m["market"]
                    for m in r["markets"]
                    if r["state"] == "completed" and (m.get("protection") or {}).get("state") == "armed"
                ],
            }
            for r in page
        ]
        more = len(matching) > limit
        return web.json_response({"runs": runs, "next_before": page[-1]["run_id"] if more else None})

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
            return web.json_response(self.stop_answer(run) | {"already_stopped": True})
        keep = message["keepPosition"]
        run["state"] = "stopped"
        run["stop"] = {
            "by": "user",
            "reason": "user",
            "keep_position": keep,
            "signer": signer,
            "at": str(int(time.time())),
        }
        for order in run["resting_orders"]:
            if order["status"] in ("placing", "resting", "partial"):
                order["status"] = "cancelled"
        for market in run["markets"]:
            market["close"] = None if keep else close_of(run["mode"], market["market"])
            for kind in ("protection", "take_profit"):
                if market[kind]:
                    market[kind] = {**market[kind], "state": "cancel_pending"}
        self.add_event(run["run_id"], "stopped", "stopped", "stopped by a signed request")
        response = self.stop_answer(run)
        self.accept(signer, message["nonce"], digest, response)
        if self.next_write == "lose":
            self.next_write = None
            return web.Response(status=504, text="upstream timed out")
        return web.json_response(response)

    @staticmethod
    def stop_answer(run: dict) -> dict:
        """The stop's answer (contract §6.4a): each market with its stop and close, and the resting orders."""
        answer = {key: run[key] for key in ("run_id", "state", "stop", "markets", "resting_orders")}
        return json.loads(json.dumps(answer))


def text(value: Decimal) -> str:
    """A decimal's one canonical spelling, as the service writes amounts."""
    spelled = format(value.normalize(), "f")
    return spelled.rstrip("0").rstrip(".") if "." in spelled else spelled


def close_of(mode: str, market: str) -> dict:
    """A market's close as the stop that opens it answers: a Watch only strategy has nothing to close."""
    shadow = mode == "shadow"
    return {
        "market": market,
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
