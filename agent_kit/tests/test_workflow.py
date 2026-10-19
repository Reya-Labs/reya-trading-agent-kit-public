"""Workflow commands: request signing against the service's vectors, the Agent-account guard,
named reasons in the output, and the command flow against a local fake of the service.

The vectors in fixtures/workflow_requests are the service's own signed-request vectors,
copied unchanged; fixtures/workflow_version_hash.json is its document-hash vector file.
"""

from types import SimpleNamespace

import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from agent_kit.scripts import _workflow as wf
from agent_kit.scripts import agent, workflow
from agent_kit.tests.conftest import parse_stdout
from agent_kit.tests.workflow_fake_service import FakeWorkflowService

FIXTURES = Path(__file__).parent / "fixtures"
START = json.loads((FIXTURES / "workflow_requests" / "start.json").read_text())
STOP = json.loads((FIXTURES / "workflow_requests" / "stop.json").read_text())
HASHES = json.loads((FIXTURES / "workflow_version_hash.json").read_text())
KEYS = {name: entry["private_key"] for name, entry in START["keys"].items()}
ADDRESSES = {name: entry["address"] for name, entry in START["keys"].items()}
CHAINS = {"devnet1": 89346162, "testnet": 89346162, "mainnet": 1729}
ACCOUNT = 123456
DEVNET_API = "https://api-devnet.reya-cronos.network/v2"
# Captured at import, before the suite's offline guard replaces it for each test.
REAL_CONNECT = socket.socket.connect


def vector_info(vectors: dict, environment: str) -> wf.ServiceInfo:
    return wf.ServiceInfo(
        environment=environment,
        chain_id=CHAINS[environment],
        domain=wf.parse_domain(vectors["domain"]),
        types=wf.parse_types({**START["types"], **STOP["types"]}),
        hosted_signers=[{"address": ADDRESSES["hosted"], "role": "active", "can_sign": True}],
        modes=["shadow", "paper", "production"],
        max_allocation_usd=wf.Decimal("500"),
        max_order_usd=wf.Decimal("100"),
        max_deadline_secs=600,
    )


def ids(cases: list) -> list:
    return [case["name"] for case in cases]


def name_of(case: dict) -> str:
    return case["name"]


def normalized(signature: str) -> str:
    raw = bytes.fromhex(signature[2:])
    return "0x" + (raw[:64] + bytes([raw[64] + 27 if raw[64] < 27 else raw[64]])).hex()


# --------------------------------------------------------------------------
# Signing: byte for byte against the service's vectors
# --------------------------------------------------------------------------


def test_domain_and_type_hashes_match_the_vectors() -> None:
    for vectors, primary in ((START, "WorkflowStart"), (STOP, "WorkflowStop")):
        info = vector_info(vectors, "devnet1")
        assert ",".join(f"{f['type']} {f['name']}" for f in info.types[primary]) in vectors["encode_type"]
        case = vectors["cases"][0]
        assert "0x" + wf.typed_data(primary, case["body"]["request"], info).header.hex() == vectors["domain_separator"]
        assert "0x" + wf.keccak(text=vectors["encode_type"]).hex() == vectors["type_hash"]


@pytest.mark.parametrize("case", [c for c in START["cases"] if "ok" in c["expect"]], ids=name_of)
def test_start_requests_are_built_and_signed_exactly_as_the_vectors(case: dict) -> None:
    info = vector_info(START, case["context"]["environment"])
    sent = case["body"]["request"]
    request = wf.start_request(
        info,
        account_id=int(sent["accountId"]),
        version_hash=sent["versionHash"],
        allocation_usd=sent["allocationUsd"],
        expires_at=int(sent["expiresAt"]),
        hosted_signer=sent["hostedSigner"].lower(),
        mode=sent["mode"],
        fire_now=sent["fireNow"],
        signer=sent["signer"].lower(),
        nonce=int(sent["nonce"]),
        deadline=int(sent["deadline"]),
    )
    assert json.dumps(request) == json.dumps(sent)  # same fields, order, spelling and types
    assert wf.struct_hash("WorkflowStart", request, info) == case["struct_hash"]
    assert wf.digest("WorkflowStart", request, info) == case["digest"]
    signature = wf.sign("WorkflowStart", request, KEYS[case["signed_by"]], info)
    assert signature == normalized(case["body"]["signature"])
    body = {"document": case["body"]["document"], "request": request, "signature": signature}
    assert json.dumps(body) == json.dumps({**case["body"], "signature": signature})


@pytest.mark.parametrize("case", [c for c in STOP["cases"] if "ok" in c["expect"]], ids=name_of)
def test_stop_requests_are_built_and_signed_exactly_as_the_vectors(case: dict) -> None:
    info = vector_info(STOP, case["context"]["environment"])
    sent = case["body"]["request"]
    request = wf.stop_request(
        info,
        account_id=int(sent["accountId"]),
        run_id=int(sent["runId"]),
        signer=sent["signer"],
        nonce=int(sent["nonce"]),
        deadline=int(sent["deadline"]),
    )
    assert json.dumps(request) == json.dumps(sent)
    assert wf.digest("WorkflowStop", request, info) == case["digest"]
    assert wf.sign("WorkflowStop", request, KEYS[case["signed_by"]], info) == case["body"]["signature"]


@pytest.mark.parametrize(
    "primary,case",
    [("WorkflowStart", c) for c in START["cases"]] + [("WorkflowStop", c) for c in STOP["cases"]],
    ids=ids(START["cases"]) + ids(STOP["cases"]),
)
def test_every_vector_reaches_the_services_verdict(primary: str, case: dict) -> None:
    vectors = START if primary == "WorkflowStart" else STOP
    info = vector_info(vectors, case["context"]["environment"])
    now = int(case["context"]["now"])
    if "digest" in case:
        assert wf.digest(primary, case["body"]["request"], info) == case["digest"]
    if case["expect"].get("ok"):
        assert wf.verify_body(primary, case["body"], info, now) == case["expect"]["signer"]
    else:
        with pytest.raises(wf.WorkflowError) as refused:
            wf.verify_body(primary, case["body"], info, now)
        assert refused.value.code == case["expect"]["error"]


@pytest.mark.parametrize("case", HASHES["cases"], ids=ids(HASHES["cases"]))
def test_document_hash_vectors(case: dict) -> None:
    assert wf.canonical_json(json.loads(case["canonical"])) == case["canonical"]
    assert wf.version_hash(case["canonical"]) == case["version_hash"]
    # The author's document (any key order, defaults omitted or spelled out) is what the canonical form says.
    wf.check_canonical(wf.parse_document(case["json"]), case["canonical"], case["version_hash"])


def test_a_canonical_form_that_changes_the_document_is_never_signed() -> None:
    case = HASHES["cases"][0]
    doc = wf.parse_document(case["json"])
    tampered = case["canonical"].replace('"level":"90000"', '"level":"95000"')
    with pytest.raises(wf.WorkflowError, match="changes steps.0.trigger.level"):
        wf.check_canonical(doc, tampered, wf.version_hash(tampered))
    with pytest.raises(wf.WorkflowError, match="not the hash"):
        wf.check_canonical(doc, case["canonical"], "0x" + "00" * 32)
    wider = case["canonical"].replace('"price_bound_bps":25', '"price_bound_bps":100')
    no_bound = json.loads(case["json"])
    with pytest.raises(wf.WorkflowError, match="limits.price_bound_bps"):
        wf.check_canonical(no_bound, wider, wf.version_hash(wider))


def test_info_with_another_domain_or_extra_fields_is_refused() -> None:
    good = FakeWorkflowService().info_body()
    for change in (
        {"eip712": {**good["eip712"], "domain": {**good["eip712"]["domain"], "verifyingContract": "0x" + "11" * 20}}},
        {"eip712": {**good["eip712"], "types": {**good["eip712"]["types"], "WorkflowStop": []}}},
    ):
        with pytest.raises(wf.WorkflowError, match="update the kit"):
            wf.parse_info({**good, **change}, "devnet1", 89346162, "http://x")
    with pytest.raises(wf.WorkflowError, match="serves mainnet"):
        wf.parse_info({**good, "environment": "mainnet", "chain_id": "1729"}, "devnet1", 89346162, "http://x")


# --------------------------------------------------------------------------
# Named reasons and the "triggered at X, executed at Y" lines
# --------------------------------------------------------------------------


def run_status(**changes) -> dict:
    doc = json.loads(HASHES["cases"][0]["canonical"])
    status = {
        "run_id": "7",
        "account_id": str(ACCOUNT),
        "market": "BTCRUSDPERP",
        "state": "active",
        "mode": "shadow",
        "phase": "armed",
        "armed": {"level": "90000", "last_mark": "91420", "distance_bps": "155"},
        "stale": None,
        "allocation": {"total_usd": "200", "reserved_usd": "0", "spent_usd": "25", "remaining_usd": "175"},
        "protection": None,
        "starter": ADDRESSES["agent"],
        "hosted_signer": ADDRESSES["hosted"],
        "created_at": "1792368000",
        "expires_at": "1792886400",
        "completion": None,
        "stop": None,
        "document": {"canonical": json.dumps(doc, separators=(",", ":")), "version_hash": "0x" + "ab" * 32},
        "last_seq": "1",
    }
    return status | changes


def event(seq: int, kind: str, code: str, text: str, firing: dict | None = None) -> dict:
    entry = {"seq": str(seq), "at": "1792368060", "kind": kind, "actor": "system", "code": code, "text": text}
    return entry | {"inputs": {}} | ({"firing": firing} if firing else {})


@pytest.mark.parametrize("code", sorted(wf.SKIP_REASONS))
def test_each_named_skip_reason_is_shown_with_the_services_text(code: str) -> None:
    text = f"triggered, skipped: {code} detail"
    view = wf.status_view(run_status(), [event(1, "skipped", code, text)])
    assert view["skips"] == [
        {"at": "2026-10-19T00:01:00Z", "code": code, "reason": wf.SKIP_REASONS[code], "text": text}
    ]
    assert view["skip_counts"] == {code: 1}
    assert "1 skip(s)" in view["summary"]


def test_an_unknown_skip_code_is_shown_as_itself() -> None:
    view = wf.status_view(run_status(), [event(1, "skipped", "skip_something_new", "triggered, skipped: new")])
    assert view["skips"][0]["reason"] == "skip_something_new"
    assert view["skips"][0]["text"] == "triggered, skipped: new"


@pytest.mark.parametrize("code", sorted(wf.STALE_REASONS))
def test_each_stale_reason_is_named(code: str) -> None:
    view = wf.status_view(run_status(stale={"code": code, "text": "not firing until the data recovers"}), [])
    assert view["stale"] == {
        "code": code,
        "reason": wf.STALE_REASONS[code],
        "text": "not firing until the data recovers",
    }
    assert f"not firing: {wf.STALE_REASONS[code]}" in view["summary"]


@pytest.mark.parametrize("code", sorted(wf.STOP_REASONS))
def test_each_stop_reason_is_named(code: str) -> None:
    stop = {"by": "user" if code == "user" else "system", "reason": code, "at": "1792368120"}
    view = wf.status_view(run_status(state="stopped", phase="done", stop=stop), [])
    assert view["stop"]["reason_text"] == wf.STOP_REASONS[code]
    assert view["summary"].startswith(f"Run 7 (shadow) is stopped; stopped: {wf.STOP_REASONS[code]}")


@pytest.mark.parametrize("code", sorted(wf.COMPLETION_REASONS))
def test_each_completion_reason_is_named(code: str) -> None:
    view = wf.status_view(run_status(state="completed", completion={"reason": code, "at": "1792368120"}), [])
    assert f"completed because {wf.COMPLETION_REASONS[code]}" in view["summary"]


def firing(status: str, **changes) -> dict:
    base = {
        "client_order_id": "6286",
        "order_status": status,
        "mark": "89990",
        "limit_px": "90215",
        "bound_bps": "25",
        "exec_qty": None,
        "avg_fill_px": None,
        "cancel_reason": None,
        "settlement": "none",
    }
    return base | changes


@pytest.mark.parametrize(
    "status,changes,text",
    [
        (
            "filled",
            {"exec_qty": "0.0028", "avg_fill_px": "90012", "settlement": "settled"},
            "triggered at 90,000, executed at 90,012 (+1.3 bps), 0.0028 BTC for $252.03",
        ),
        (
            "partial",
            {"exec_qty": "0.0001", "avg_fill_px": "89991", "settlement": "provisional"},
            "triggered at 90,000, partly executed at 89,991 (-1.0 bps), 0.0001 BTC for $9.00",
        ),
        (
            "shadow",
            {},
            "triggered at 90,000 (mark 89,990): shadow run, nothing sent; the order would have been limited to "
            "90,215 (25 bps bound)",
        ),
        ("past_bound", {}, "triggered at 90,000, not filled: the market had moved past the 25 bps bound"),
        ("zero_fill", {}, "triggered at 90,000, not filled: nothing traded within the 25 bps bound"),
        ("rejected", {"cancel_reason": "RISK"}, "triggered at 90,000, rejected by the exchange: RISK"),
        ("unknown", {}, "triggered at 90,000, outcome unknown: the run is blocked"),
        ("sent", {}, "triggered at 90,000 (mark 89,990), order sent: waiting for its outcome"),
    ],
)
def test_firings_read_triggered_at_then_executed_at(status: str, changes: dict, text: str) -> None:
    view = wf.status_view(run_status(), [event(1, "fired", "fired", "triggered at 90,000", firing(status, **changes))])
    assert view["firings"][0]["text"].startswith(text)
    assert view["firings"][0]["level"] == "90000"


def test_status_shows_the_armed_level_distance_and_remaining_allocation() -> None:
    view = wf.status_view(run_status(), [])
    assert view["armed"]["text"] == "armed at 90,000 (crossing down): mark 91,420 is 1.55% (155 bps) above it"
    assert view["allocation"]["text"] == "$175.00 of $200.00 remaining ($25.00 spent, $0.00 reserved)"
    assert view["summary"] == (
        "Run 7 (shadow) is active; armed at 90,000 (crossing down): mark 91,420 is 1.55% (155 bps) above it; "
        "$175.00 of $200.00 remaining ($25.00 spent, $0.00 reserved); 0 firing(s), 0 skip(s)."
    )


def test_bps_and_usd_round_like_the_services_text() -> None:
    assert wf.bps_from("90000", "90012") == "+1.3 bps"
    assert wf.bps_from("90000", "89991") == "-1.0 bps"
    assert wf.bps_from("90000", "90000") == "0.0 bps"
    assert wf.usd_text("25.005") == "$25.01"
    assert wf.usd_text("1234567.891") == "$1,234,567.89"
    assert wf.group("90012.5") == "90,012.5"


# --------------------------------------------------------------------------
# The Agent-account guard and the agent key
# --------------------------------------------------------------------------


@pytest.fixture
def kit_env(monkeypatch) -> None:
    monkeypatch.setenv("CHAIN_ID", "89346162")
    monkeypatch.setenv("REYA_API_URL", DEVNET_API)
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", ADDRESSES["owner"])
    monkeypatch.setenv("PERP_AGENT_PRIVATE_KEY", KEYS["agent"])
    monkeypatch.setenv("PERP_AGENT_ADDRESS", ADDRESSES["agent"])
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", str(ACCOUNT))
    monkeypatch.setenv("PERP_ACCOUNT_ID_1", str(ACCOUNT))


def test_only_a_recorded_agent_account_is_accepted(kit_env, monkeypatch) -> None:
    assert wf.agent_account() == ACCOUNT
    assert wf.agent_account(ACCOUNT) == ACCOUNT
    with pytest.raises(wf.WorkflowError, match="not your recorded Agent account"):
        wf.agent_account(999)
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", "")
    with pytest.raises(wf.WorkflowError, match="No Agent account is recorded"):
        wf.agent_account(ACCOUNT)
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", f"{ACCOUNT},654321")
    monkeypatch.delenv("PERP_ACCOUNT_ID_1")
    with pytest.raises(wf.WorkflowError, match="pass --account-id"):
        wf.agent_account()


def test_only_the_agent_key_signs(kit_env, monkeypatch) -> None:
    key, address = wf.agent_signer()
    assert address == ADDRESSES["agent"] and key == KEYS["agent"]
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", KEYS["owner"])
    with pytest.raises(wf.WorkflowError, match="never with your own key"):
        wf.agent_signer()
    monkeypatch.delenv("REYA_SIGNER")
    monkeypatch.delenv("PERP_AGENT_PRIVATE_KEY")
    with pytest.raises(wf.WorkflowError, match="No agent key"):
        wf.agent_signer()
    # An "agent key" that is really the owner's is refused too.
    monkeypatch.setenv("PERP_AGENT_PRIVATE_KEY", KEYS["owner"])
    monkeypatch.setenv("PERP_AGENT_ADDRESS", ADDRESSES["owner"])
    with pytest.raises(wf.WorkflowError, match="never sign with the owner key"):
        wf.agent_signer()


def own_loop_run(coro):
    """asyncio.run without touching the suite's session event loop (see test_health)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_cli_refusals_are_json_and_never_carry_the_key(kit_env, monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(workflow, "asyncio", SimpleNamespace(run=own_loop_run))
    doc = tmp_path / "other.json"
    doc.write_text(json.dumps(json.loads(HASHES["cases"][0]["json"]) | {"environment": "devnet1", "account_id": "999"}))
    for argv in (
        ["start", "--file", str(doc)],
        ["stop", "5", "--account-id", "999"],
        ["status", "--account-id", "999"],
    ):
        monkeypatch.setattr(sys, "argv", ["workflow", *argv])
        with pytest.raises(SystemExit) as exited:
            workflow.main()
        assert exited.value.code == 1
        out = capsys.readouterr()
        assert "Agent account" in json.loads(out.out)["error"]
        assert KEYS["agent"][2:] not in out.out + out.err


# --------------------------------------------------------------------------
# Commands against the fake service
# --------------------------------------------------------------------------


@pytest.fixture
def loopback(monkeypatch) -> None:
    """Let the client reach the fake service on 127.0.0.1, and nothing else."""

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] == "127.0.0.1":
            return REAL_CONNECT(sock, address)
        raise AssertionError(f"workflow tests reach only the local fake service, not {address!r}")

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
async def service(kit_env, loopback, monkeypatch):
    fake = FakeWorkflowService(hosted=ADDRESSES["hosted"])
    fake.add_account(ACCOUNT, ADDRESSES["owner"])
    fake.approve(ADDRESSES["owner"], ADDRESSES["agent"])
    fake.enable_hosted(ADDRESSES["owner"])
    await fake.start_server()
    monkeypatch.setenv("REYA_WORKFLOWS_URL", fake.url)
    yield fake
    await fake.close()


async def call(*argv: str) -> dict:
    result = await workflow.run(workflow.build_parser().parse_args(list(argv)))
    assert KEYS["agent"][2:] not in json.dumps(result)
    return result


async def drafted(tmp_path: Path, *extra: str) -> str:
    path = str(tmp_path / "draft.json")
    result = await call(
        "draft",
        "--symbol",
        "btc",
        "--direction",
        "down",
        "--level",
        "90000",
        "--notional",
        "25",
        "--allocation",
        "200",
        "--expires",
        "7d",
        "--funding",
        "lt:0.001",
        "--stop-px",
        "85000",
        "--out",
        path,
        *extra,
    )
    assert result["file"] == path, result
    return path


async def test_draft_preview_start_status_stop(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    doc = json.loads(Path(path).read_text())
    assert doc["account_id"] == str(ACCOUNT) and doc["environment"] == "devnet1"
    assert doc["steps"][0]["conditions"] == [{"kind": "funding_avg", "op": "lt", "pct_per_hour": "0.001"}]

    validated = await call("validate", "--file", path)
    assert validated["ok"] is True and validated["version_hash"] == wf.version_hash(validated["canonical"])

    preview = await call("preview", "--file", path)
    assert preview["now"] == "the mark is 91420; this would arm 1.55% below, at 90000"
    assert preview["trigger"]["distance"] == "1.55% (155 bps)" and preview["trigger"]["side"] == "below"
    assert preview["order_text"] == (
        "buy 0.0002 at a limit of 90,225 (25 bps max slippage), reserving $18.05; "
        "the allocation covers 11 such orders"
    )
    assert (
        preview["funding_text"] == "funding: latest 0.0004%/h, 15-minute average 0.0003%/h (180 samples in the window)"
    )
    assert preview["allocation_usd"] == "200" and len(preview["lines"]) == 2
    assert "already_triggered" not in preview and "--fire-now" not in preview["next"]
    assert [body for method, p, body in service.requests if p == "/v1/preview"] == [
        {"document": Path(path).read_text()}
    ], "preview sends the document and nothing else"
    assert not service.writes(), "validate and preview never start anything"

    started = await call("start", "--file", path)
    assert started["started"] is True and started["state"] == "active" and started["mode"] == "shadow"
    assert started["signer"] == ADDRESSES["agent"] and started["hosted_signer"] == ADDRESSES["hosted"]
    run_id = started["run_id"]
    run = service.runs[run_id]
    assert run["starter"] == ADDRESSES["agent"]  # recovered by the fake from the signature
    assert run["document"]["canonical"] == validated["canonical"]

    service.add_event(run_id, "skipped", "skip_condition_funding", "triggered, skipped: funding")
    service.add_event(
        run_id,
        "fired",
        "fired",
        "triggered at 90,000",
        firing("filled", exec_qty="0.0002", avg_fill_px="90012", settlement="provisional"),
    )
    status = await call("status", run_id)
    assert status["armed"]["text"] == "armed at 90,000 (crossing down): mark 91,420 is 1.55% (155 bps) above it"
    assert status["firings"][0]["text"] == "triggered at 90,000, executed at 90,012 (+1.3 bps), 0.0002 BTC for $18.00"
    assert status["skips"][0]["reason"] == "funding condition not met"
    assert status["allocation"]["text"] == "$200.00 of $200.00 remaining ($0.00 spent, $0.00 reserved)"

    listing = await call("status")
    assert [r["run_id"] for r in listing["runs"]] == [run_id]
    assert listing["active_run"]["run_id"] == run_id

    stopped = await call("stop", run_id)
    assert stopped["state"] == "stopped" and stopped["stop"]["reason"] == "user"
    assert stopped["stop"]["signer"] == ADDRESSES["agent"]
    assert stopped["protection"]["state"] == "cancel_pending"
    assert "protective order's cancellation" in stopped["next"]
    again = await call("stop", run_id)
    assert again["already_stopped"] is True
    assert len(service.writes()) == 2, "a stopped run is not stopped twice"


async def test_the_owner_key_in_the_environment_is_never_used(service, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", KEYS["owner"])
    started = await call("start", "--file", await drafted(tmp_path))
    assert service.runs[started["run_id"]]["starter"] == ADDRESSES["agent"]


async def test_a_document_for_another_account_is_refused_before_any_request(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    doc = json.loads(Path(path).read_text()) | {"account_id": "999"}
    Path(path).write_text(json.dumps(doc))
    for command in ("validate", "preview", "start"):
        result = await call(command, "--file", path)
        assert "not your recorded Agent account" in result["error"]
    assert service.requests == []


async def test_runs_of_other_accounts_are_neither_shown_nor_stopped(service, tmp_path) -> None:
    service.add_account(999, ADDRESSES["owner"])
    service.runs["1"] = {**run_status(run_id="1", account_id="999")}
    service.events["1"] = []
    assert "not your recorded Agent account" in (await call("status", "1"))["error"]
    assert "not your recorded Agent account" in (await call("stop", "1"))["error"]
    assert not service.writes()


async def test_start_refusals_name_the_code_and_the_next_step(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.hosted_enabled.clear()
    refused = await call("start", "--file", path)
    assert refused["error_code"] == "HOSTED_SIGNER_NOT_ENABLED" and refused["http_status"] == 403
    assert "approve the hosted signer" in refused["next"]
    assert refused["request_id"] == "req-test"
    service.enable_hosted(ADDRESSES["owner"])
    assert (await call("start", "--file", path))["state"] == "active"
    busy = await call("start", "--file", path)
    assert busy["error_code"] == "ACCOUNT_HAS_ACTIVE_RUN"
    service.next_refusal = "BUSY"
    retry = await call("stop", "1")
    assert retry["error_code"] == "BUSY" and retry["retryable"] is True and retry["retry_after_s"] == 7


async def test_a_mode_the_environment_does_not_allow_is_refused_unsigned(service, tmp_path) -> None:
    result = await call("start", "--file", await drafted(tmp_path), "--mode", "production")
    assert result["error_code"] == "MODE_NOT_ALLOWED" and "allows shadow" in result["error"]
    assert not service.writes()


async def test_pilot_caps_are_checked_before_signing(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    doc = json.loads(Path(path).read_text())
    doc["limits"]["allocation_usd"] = "600"
    Path(path).write_text(json.dumps(doc))
    result = await call("start", "--file", path)
    assert result["error_code"] == "ABOVE_PILOT_LIMIT"
    assert not service.writes()


async def test_a_service_for_another_environment_is_refused(service, tmp_path) -> None:
    service.info_override = {"environment": "mainnet", "chain_id": "1729"}
    result = await call("start", "--file", await drafted(tmp_path))
    assert result["error_code"] == "WRONG_ENVIRONMENT"
    assert not service.writes()


async def test_a_tampered_canonical_form_is_never_signed(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    honest = FakeWorkflowService.canonical(json.loads(Path(path).read_text()))
    service.canonical_override = honest.replace('"level":"90000"', '"level":"99000"')
    result = await call("start", "--file", path)
    assert "changes steps.0.trigger.level" in result["error"]
    assert not service.writes()


async def test_an_invalid_document_lists_the_services_errors(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.marks.clear()
    result = await call("validate", "--file", path)
    assert result["ok"] is False and result["errors"][0]["code"] == "unknown_market"
    assert result["error"].startswith("The document is invalid: unknown market")


async def test_a_lost_answer_to_an_accepted_start_is_confirmed_by_reading(service, tmp_path) -> None:
    service.next_write = "lose"
    started = await call("start", "--file", await drafted(tmp_path))
    assert started["started"] is True and started["confirmed_by_read"] is True
    assert len(service.writes()) == 1, "never resent"


async def test_a_lost_answer_that_cannot_be_confirmed_is_reported_not_resent(service, tmp_path) -> None:
    service.next_write = "drop"
    result = await call("start", "--file", await drafted(tmp_path))
    assert result["outcome"] == "unknown" and "nothing is resent" in result["error"]
    assert "workflow status" in result["next"]
    assert len(service.writes()) == 1


async def test_a_stop_without_a_protective_order_says_there_is_nothing_to_cancel(service, tmp_path) -> None:
    path = str(tmp_path / "plain.json")
    await call(
        "draft",
        *("--symbol", "btc", "--direction", "down", "--level", "90000", "--notional", "25"),
        *("--allocation", "200", "--expires", "7d", "--out", path),
    )
    run_id = (await call("start", "--file", path))["run_id"]
    stopped = await call("stop", run_id)
    assert stopped["protection"] is None
    assert stopped["next"].startswith("The run armed no protective order")


async def test_a_lost_stop_answer_is_confirmed_by_reading(service, tmp_path) -> None:
    run_id = (await call("start", "--file", await drafted(tmp_path)))["run_id"]
    service.next_write = "lose"
    stopped = await call("stop", run_id)
    assert stopped["state"] == "stopped" and stopped["confirmed_by_read"] is True


# --------------------------------------------------------------------------
# Links to the run's pages in the Reya app
# --------------------------------------------------------------------------

DEVNET_APP = "https://app-devnet-cf.reya.xyz"


def run_page(run_id: str, app: str = DEVNET_APP) -> str:
    return f"{app}/agents/workflows/run?id={run_id}#access={agent.SETUP_PAGE_ACCESS_CODE}"


def runs_page(app: str = DEVNET_APP) -> str:
    return f"{app}/agents/workflows?account={ACCOUNT}#access={agent.SETUP_PAGE_ACCESS_CODE}"


def assert_code_only_in_fragment(url: str) -> None:
    parts = urlsplit(url)
    assert parts.fragment == f"access={agent.SETUP_PAGE_ACCESS_CODE}"
    assert agent.SETUP_PAGE_ACCESS_CODE not in parts.scheme + parts.netloc + parts.path + parts.query


async def test_every_reported_run_links_to_its_app_page_with_the_setup_access_code(service, tmp_path) -> None:
    # The same app as the setup link, and the same code, which the workflow pages also require.
    assert agent.app_base_url(DEVNET_API) == DEVNET_APP
    started = await call("start", "--file", await drafted(tmp_path))
    run_id = started["run_id"]
    assert started["app_run_url"] == run_page(run_id) and started["app_runs_url"] == runs_page()

    status = await call("status", run_id)
    listing = await call("status")
    assert (status["app_run_url"], status["app_runs_url"]) == (run_page(run_id), runs_page())
    assert listing["app_runs_url"] == runs_page()
    assert [r["app_run_url"] for r in listing["runs"]] == [run_page(run_id)]
    assert listing["active_run"]["app_run_url"] == run_page(run_id)

    stopped = await call("stop", run_id)
    again = await call("stop", run_id)
    assert again["already_stopped"] is True
    for result in (stopped, again):
        assert (result["app_run_url"], result["app_runs_url"]) == (run_page(run_id), runs_page())
    for url in (started["app_run_url"], started["app_runs_url"]):
        assert_code_only_in_fragment(url)


async def test_unknown_outcomes_link_to_the_pages_that_show_what_happened(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.next_write = "drop"
    lost_start = await call("start", "--file", path)
    assert lost_start["outcome"] == "unknown" and lost_start["app_runs_url"] == runs_page()
    run_id = (await call("start", "--file", path))["run_id"]
    service.next_write = "drop"
    lost_stop = await call("stop", run_id)
    assert lost_stop["outcome"] == "unknown"
    assert (lost_stop["app_run_url"], lost_stop["app_runs_url"]) == (run_page(run_id), runs_page())


async def test_app_links_follow_reya_app_url_and_never_fail_a_start(service, tmp_path, monkeypatch) -> None:
    path = await drafted(tmp_path)
    monkeypatch.setenv("REYA_APP_URL", "https://app.example.test/")
    run_id = (await call("start", "--file", path))["run_id"]
    status = await call("status", run_id)
    assert status["app_run_url"] == run_page(run_id, "https://app.example.test")
    assert status["app_runs_url"] == runs_page("https://app.example.test")
    # A malformed override costs the links, not the result of a run that already started or stopped.
    monkeypatch.setenv("REYA_APP_URL", "http://app.example.test")
    stopped = await call("stop", run_id)
    assert stopped["state"] == "stopped" and stopped["app_run_url"] is None and stopped["app_runs_url"] is None
    # Including overrides urlsplit cannot parse, or whose port is not a port.
    for bad in ("https://[broken", "https://app.example.test:abc", "https://app.example.test:99999"):
        monkeypatch.setenv("REYA_APP_URL", bad)
        started = await call("start", "--file", path)
        assert started["started"] is True and started["app_run_url"] is None and started["app_runs_url"] is None
        assert (await call("stop", started["run_id"]))["state"] == "stopped"


def test_no_app_link_without_a_known_app_or_with_an_odd_run_id(kit_env, monkeypatch) -> None:
    assert workflow._run_page(DEVNET_APP, str(2**64 - 1)) == run_page(str(2**64 - 1))
    for odd in (None, "", "07", "-1", "1e3", " 1", "1#x", "١", 2**64):
        assert workflow._run_page(DEVNET_APP, odd) is None
    monkeypatch.setenv("REYA_API_URL", "https://api-cronos.reya.xyz/v2")  # no app is known for testnet
    app = workflow._app()
    assert app is None and workflow._run_page(app, 1) is None and workflow._runs_page(app, ACCOUNT) is None


async def test_an_already_triggered_level_is_called_out_in_the_preview(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.marks["BTCRUSDPERP"] = "89000"
    preview = await call("preview", "--file", path)
    assert "--fire-now" in preview["already_triggered"] and "--fire-now" in preview["next"]
    assert preview["now"].startswith("the mark is 89000, already at or below the level of 90000")


def test_fire_now_belongs_to_start_not_preview() -> None:
    parser = workflow.build_parser()
    assert parser.parse_args(["start", "--file", "f.json", "--fire-now"]).fire_now is True
    with pytest.raises(SystemExit):
        parser.parse_args(["preview", "--file", "f.json", "--fire-now"])


def test_a_preview_without_an_order_says_why() -> None:
    view = wf.preview_view(
        {
            "market": {"symbol": "BTCRUSDPERP", "mark": "91420", "mark_at": "1792281600"},
            "trigger": {"level": "90000", "direction": "down", "distance_bps": "155", "side": "below"},
            "order": None,
            "order_error": {"code": "below_minimum_order", "message": "the order is below the market's minimum"},
            "funding": {"samples": "3", "latest": None, "average_15m_pct_per_hour": None},
            "lines": [],
        },
        {
            "market": "BTCRUSDPERP",
            "steps": [
                {
                    "trigger": {"kind": "price_cross", "source": "mark", "direction": "down", "level": "90000"},
                    "conditions": [],
                    "action": {"kind": "buy", "notional_usd": "1"},
                    "repeat": "once",
                }
            ],
            "limits": {"allocation_usd": "1", "expires_at": 1792886400},
        },
    )
    assert view["order_text"] == "no order at the level: the order is below the market's minimum"
    assert view["funding_text"] == "funding: no sample yet, no 15-minute average yet (3 samples in the window)"
    assert view["now"].endswith("the level is 1.55% (155 bps) below it")


@pytest.mark.parametrize(
    ("reason", "hint"),
    [
        ("not_tagged", "not created as the Agent account"),
        ("main_account", "the owner's main account"),
        ("spot_account", "a spot account"),
        ("something_new", "does not see this account as the Agent account"),
    ],
)
def test_not_agent_account_explains_its_reason(reason, hint) -> None:
    error = wf.ServiceError(403, "NOT_AGENT_ACCOUNT", "refused", False, {"reason": reason}, None, None)
    assert hint in error.result()["next"]


async def test_info_reports_the_hosted_signer_modes_and_caps(service) -> None:
    info = await call("info")
    assert info["hosted_signer"] == ADDRESSES["hosted"]
    assert info["modes"] == ["shadow"] and info["limits"]["max_allocation_usd"] == "500"


async def test_drafts_go_to_the_state_directory_by_default(kit_env, tmp_path) -> None:
    expires = str(int(time.time()) + 3600)
    result = await call(
        "draft", "--symbol", "eth", "--direction", "up", "--level", "4200.5", "--side", "sell",
        "--notional", "10", "--allocation", "50", "--expires", expires,
    )  # fmt: skip
    assert Path(result["file"]).parent == tmp_path / "writer-state" / "workflows"
    assert result["workflow"].startswith("Sell $10.00 of ETH each time the ETHRUSDPERP mark crosses up through 4,200.5")
    assert result["document"]["limits"]["price_bound_bps"] == 25


def test_draft_refuses_bad_inputs(kit_env) -> None:
    with pytest.raises(wf.WorkflowError, match="larger than the allocation"):
        wf.draft_document(
            environment="devnet1",
            account_id=ACCOUNT,
            market="BTCRUSDPERP",
            direction="down",
            level=wf.Decimal("90000"),
            side="buy",
            notional=wf.Decimal("300"),
            allocation=wf.Decimal("200"),
            expires_at=int(time.time()) + 60,
            repeat="once",
            bound_bps=25,
            conditions=[],
            stop_px=None,
        )
    with pytest.raises(wf.WorkflowError, match="more than 9 decimal places"):
        wf.canonical_amount(wf.Decimal("1.0000000001"), places=9)
    with pytest.raises(wf.WorkflowError, match="OP:VALUE"):
        wf.condition("funding_avg", "below:0.1")
    assert wf.condition("funding_avg", "gte:-0.00050") == {
        "kind": "funding_avg",
        "op": "gte",
        "pct_per_hour": "-0.0005",
    }
    assert wf.parse_expiry("7d", now=1000) == 1000 + 7 * 86400
    assert wf.parse_expiry("2026-10-26T00:00:00Z") == 1792972800
    with pytest.raises(wf.WorkflowError, match="time zone"):
        wf.parse_expiry("2026-10-26T00:00:00")


def test_documents_with_floats_or_repeated_keys_are_refused() -> None:
    with pytest.raises(wf.WorkflowError, match="decimals are strings"):
        wf.parse_document('{"limits": {"allocation_usd": 200.5}}')
    with pytest.raises(wf.WorkflowError, match="repeats the key"):
        wf.parse_document('{"market": "BTCRUSDPERP", "market": "ETHRUSDPERP"}')


def test_a_malformed_run_id_is_a_json_error(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["workflow", "stop", "abc"])
    with pytest.raises(SystemExit):
        workflow.main()
    assert "not a run ID" in parse_stdout(capsys)["error"]
