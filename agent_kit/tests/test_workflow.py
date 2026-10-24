"""Workflow commands: request signing against the service's vectors, the Agent-account guard,
named reasons in the output, and the command flow against a local fake of the service.

The vectors in fixtures/workflow_requests are the service's own signed-request vectors,
copied unchanged; fixtures/workflow_version_hash.json is its document-hash vector file.
"""

from types import SimpleNamespace

import asyncio
import base64
import json
import re
import socket
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from aiohttp import web
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

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
        keep_position=case["expect"]["keep_position"],
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


def test_only_the_stop_with_keep_position_is_read() -> None:
    current = FakeWorkflowService().info_body()
    wf.parse_info(current, "devnet1", 89346162, "http://x")
    # The stop from before keepPosition, keepPosition of another type, or a stop missing a field.
    stop = current["eip712"]["types"]["WorkflowStop"]
    for fields in (
        [f for f in stop if f["name"] != "keepPosition"],
        [f | {"type": "string"} if f["name"] == "keepPosition" else f for f in stop],
        [f for f in stop if f["name"] != "runId"],
    ):
        changed = {
            **current,
            "eip712": {**current["eip712"], "types": {**current["eip712"]["types"], "WorkflowStop": fields}},
        }
        with pytest.raises(wf.WorkflowError, match="update the kit"):
            wf.parse_info(changed, "devnet1", 89346162, "http://x")


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
    assert view["summary"].startswith(f"Run 7 (Watch only) is stopped; stopped: {wf.STOP_REASONS[code]}")


@pytest.mark.parametrize("code", sorted(wf.COMPLETION_REASONS))
def test_each_completion_reason_is_named(code: str) -> None:
    view = wf.status_view(run_status(state="completed", completion={"reason": code, "at": "1792368120"}), [])
    assert f"completed because {wf.COMPLETION_REASONS[code]}" in view["summary"]


def test_a_run_that_expired_keeps_its_stop_and_says_so() -> None:
    protection = {"state": "armed", "trigger_px": "85000", "limit_px": "82450", "order_id": "1879"}
    completed = run_status(
        state="completed", phase="done", completion={"reason": "expired", "at": "1792368120"}, protection=protection
    )
    view = wf.status_view(completed, [])
    assert view["completion"]["reason_text"] == "it reached its expiry and sends no new orders"
    assert (
        "completed because it reached its expiry and sends no new orders; its protective stop at 85,000 stays "
        "on the position until it fires or the run is stopped" in view["summary"]
    )
    # Once the stop is down, the summary no longer claims one.
    gone = completed | {"protection": protection | {"state": "cancelled", "reason": "run_stopped"}}
    assert "protective stop" not in wf.status_view(gone, [])["summary"]


@pytest.mark.parametrize("completion_reason", ["expired", "fired_once", "allocation_used"])
@pytest.mark.parametrize(
    ("protection_state", "message"),
    [
        ("arming", "is still arming; protection is not confirmed"),
        ("unknown", "has an unknown status; protection is not confirmed"),
        ("cancel_pending", "is being cancelled; it may still fire until cancellation is confirmed"),
    ],
)
def test_completed_run_does_not_claim_uncertain_protection(
    completion_reason: str, protection_state: str, message: str
) -> None:
    run = run_status(
        state="completed",
        phase="done",
        completion={"reason": completion_reason, "at": "1792368120"},
        protection={"state": protection_state, "trigger_px": "85000"},
    )
    view = wf.status_view(run, [])
    assert f"its protective stop at 85,000 {message}" in view["summary"]
    assert "stays on the position" not in view["summary"]
    assert view["protection"] == run["protection"]


@pytest.mark.parametrize("protection_state", ["pending", "not_armed", "cancelled", "fired"])
def test_completed_run_without_a_live_stop_does_not_claim_retained_protection(protection_state: str) -> None:
    run = run_status(
        state="completed",
        completion={"reason": "expired", "at": "1792368120"},
        protection={"state": protection_state, "trigger_px": "85000"},
    )
    assert "protective stop" not in wf.status_view(run, [])["summary"]


def test_a_run_that_stopped_at_its_expiry_before_the_change_still_reads() -> None:
    stop = {"by": "system", "reason": "expired", "at": "1792368120"}
    view = wf.status_view(run_status(state="stopped", phase="done", stop=stop), [])
    assert view["summary"].startswith("Run 7 (Watch only) is stopped; stopped: the workflow expired")


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
            "triggered at 90,000 (mark 89,990): Watch only, recorded and not sent; the order would have been limited to "
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
        "Run 7 (Watch only) is active; armed at 90,000 (crossing down): mark 91,420 is 1.55% (155 bps) above it; "
        "$175.00 of $200.00 remaining ($25.00 spent, $0.00 reserved); 0 firing(s), 0 skip(s)."
    )


@pytest.mark.parametrize(
    "mode,name,text",
    [
        ("shadow", "Watch only", "Watch only: it records what it would do and sends no orders"),
        ("paper", "Paper", "Paper: fills are simulated into a labelled ledger, not sent"),
        ("production", "Live", "Live: it sends real orders on the Agent account"),
    ],
)
def test_modes_read_as_the_app_names_them(mode: str, name: str, text: str) -> None:
    view = wf.status_view(run_status(mode=mode), [])
    assert view["mode"] == mode, "the service's value is kept"
    assert view["mode_text"] == text
    assert view["summary"].startswith(f"Run 7 ({name}) is active;")


def test_the_services_shadow_sentences_read_watch_only() -> None:
    protection = {
        "state": "not_armed",
        "reason": "shadow",
        "text": "Not armed: a shadow run sends nothing. It would arm a stop-loss on the whole position, "
        "triggered at 85000 with its limit at 84575.",
    }
    started = event(1, "started", "started", f"Started by {ADDRESSES['agent']} in shadow mode.")
    view = wf.status_view(run_status(protection=protection), [started])
    assert view["recent_events"][0]["text"] == f"Started by {ADDRESSES['agent']} in Watch only mode."
    assert view["protection"]["text"].startswith("Not armed: a watch-only run sends nothing. It would arm")
    assert view["protection"]["reason"] == "shadow", "codes are the service's"
    assert wf.service_text("Nothing to close: a shadow run sends no orders, so it holds no position.") == (
        "Nothing to close: a watch-only run sends no orders, so it holds no position."
    )


def test_mode_flags_name_the_mode_when_the_value_differs() -> None:
    assert [wf.mode_flag(m) for m in wf.MODES] == ["shadow (Watch only)", "paper", "production (Live)"]


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
    monkeypatch.setenv("REYA_DEX_ID", "1")


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


@pytest.fixture(autouse=True)
def opened(monkeypatch) -> list[str]:
    """Pages the kit opens in the user's browser: a desktop with one, unless a test says otherwise."""
    pages: list[str] = []

    def record(url: str, new: int = 0) -> bool:
        pages.append(url)
        return True

    monkeypatch.setattr(agent, "browser_unavailable", lambda: None)
    monkeypatch.setattr(agent, "in_wsl", lambda: False)
    monkeypatch.setattr(agent.webbrowser, "open", record)
    return pages


@pytest.fixture
async def service(kit_env, loopback, monkeypatch):
    fake = FakeWorkflowService(hosted=ADDRESSES["hosted"])
    fake.add_account(ACCOUNT, ADDRESSES["owner"])
    fake.approve(ADDRESSES["owner"], ADDRESSES["agent"])
    fake.enable_hosted(ADDRESSES["owner"])
    await fake.start_server()
    monkeypatch.setenv("REYA_WORKFLOWS_URL", fake.url)
    # The trading API's owner reads (approvals, accounts) come from the same fake.
    monkeypatch.setattr(agent, "AccountRelay", fake.relay())
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
    assert started["mode_text"] == "Watch only: it records what it would do and sends no orders"
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
    assert status["protection"] is None, "no stop at start, nor while the first fill is provisional"
    # The service arms the stop only after the first fill settles.
    service.events[run_id][-1]["firing"]["settlement"] = "settled"
    run["protection"] = {"state": "armed", "order_id": "77"}
    status = await call("status", run_id)
    assert status["firings"][0]["settlement"] == "settled"
    assert status["protection"] == {"state": "armed", "order_id": "77"}
    assert status["armed"]["text"] == "armed at 90,000 (crossing down): mark 91,420 is 1.55% (155 bps) above it"
    assert status["firings"][0]["text"] == "triggered at 90,000, executed at 90,012 (+1.3 bps), 0.0002 BTC for $18.00"
    assert status["skips"][0]["reason"] == "funding condition not met"
    assert status["allocation"]["text"] == "$200.00 of $200.00 remaining ($0.00 spent, $0.00 reserved)"

    assert status["recent_events"][0]["text"] == f"Started by {ADDRESSES['agent']} in Watch only mode."

    listing = await call("status")
    assert [r["run_id"] for r in listing["runs"]] == [run_id]
    assert listing["runs"][0]["mode"] == "shadow" and listing["runs"][0]["mode_text"].startswith("Watch only:")
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
    assert result["error_code"] == "MODE_NOT_ALLOWED"
    assert "allows shadow (Watch only) mode(s), not production (Live)" in result["error"]
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


def last_stop(service) -> dict:
    stops = [body for (_, path, body) in service.writes() if path.endswith("/stop")]
    return stops[-1]["request"]


async def test_a_stop_closes_what_the_run_opened_by_default(service, tmp_path) -> None:
    run_id = (await call("start", "--file", await drafted(tmp_path)))["run_id"]
    stopped = await call("stop", run_id)
    assert last_stop(service)["keepPosition"] is False
    assert stopped["stop"]["keep_position"] is False
    # A shadow run holds nothing to close.
    assert stopped["close"]["state"] == "nothing_to_close"
    assert stopped["close"]["text"] == "nothing the run opened was still open (simulated: nothing was sent)"
    assert "closes what this run opened" in stopped["note"]
    status = await call("status", run_id)
    assert status["close"]["state"] == "nothing_to_close"
    assert "nothing the run opened was still open" in status["summary"]


async def test_a_production_stop_follows_its_close(service, tmp_path) -> None:
    service.modes = ["shadow", "production"]
    run_id = (await call("start", "--file", await drafted(tmp_path), "--mode", "production"))["run_id"]
    stopped = await call("stop", run_id)
    assert stopped["close"]["state"] == "pending"
    assert stopped["next"] == f"workflow status {run_id} follows the close, then the protective order's cancellation"


async def test_stopping_again_says_what_the_first_stop_chose(service, tmp_path) -> None:
    service.modes = ["shadow", "production"]
    run_id = (await call("start", "--file", await drafted(tmp_path), "--mode", "production"))["run_id"]
    await call("stop", run_id)
    again = await call("stop", run_id, "--keep-position")
    assert again["already_stopped"] is True and "signer" not in again
    assert again["stop"]["keep_position"] is False and again["close"]["state"] == "pending"
    assert again["note"].startswith(
        "It was already stopped, and that stop closes what the run opened: stopping again changes nothing."
    )
    assert "already stopped" not in (await call("stop", run_id))["note"]


@pytest.mark.parametrize(
    ("close", "text"),
    [
        ({"state": "pending"}, "waiting for the run's last order and fills before closing what it opened"),
        (
            {"state": "closing", "attempts": "1", "max_attempts": "3", "closed_qty": "0.01"},
            "closing what the run opened: attempt 1 of 3 made, 0.01 closed so far",
        ),
        ({"state": "closed", "closed_qty": "0.03"}, "closed what the run opened (0.03 in all)"),
        ({"state": "nothing_to_close"}, "nothing the run opened was still open"),
        (
            {"state": "stopped_still_open", "attempts": "3", "open_qty": "-0.02"},
            "0.02 of what the run opened is still open after 3 close attempts: close it by hand",
        ),
        (
            {"state": "stopped_still_open", "attempts": "3", "open_qty": None},
            "part of what the run opened is still open after 3 close attempts: close it by hand",
        ),
        ({"state": "unknown"}, "a close order's outcome is unknown, so the service stopped trying: check the position"),
        (
            {"state": "closed", "closed_qty": "0.03", "simulated": True},
            "closed what the run opened (0.03 in all) (simulated: nothing was sent)",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else value["state"],
)
def test_every_close_state_reads_as_a_sentence(close: dict, text: str) -> None:
    view = wf.close_view(close)
    assert view is not None and view["text"] == text


def test_a_close_s_next_attempt_is_a_time() -> None:
    closing = wf.close_view({"state": "closing", "attempts": "1", "max_attempts": "3", "next_attempt_at": "1792368020"})
    closed = wf.close_view({"state": "closed", "next_attempt_at": None})
    assert closing is not None and closing["next_attempt_at"] == "2026-10-19T00:00:20Z"
    assert closed is not None and closed["next_attempt_at"] is None


async def test_keep_position_signs_it_and_says_no_stop_order_is_left(service, tmp_path) -> None:
    run_id = (await call("start", "--file", await drafted(tmp_path)))["run_id"]
    stopped = await call("stop", run_id, "--keep-position")
    assert last_stop(service)["keepPosition"] is True
    assert stopped["stop"]["keep_position"] is True and stopped["close"] is None
    assert "no stop order from this run" in stopped["note"]


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


def review_link(url: str) -> tuple[str, dict, dict]:
    """The review page, its query and its fragment, each value a list as parse_qs gives it."""
    parts = urlsplit(url)
    page = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return page, parse_qs(parts.query, keep_blank_values=True), parse_qs(parts.fragment, keep_blank_values=True)


def decoded(doc: str) -> str:
    """The document text from the link's `doc`, base64url without padding, as the app reads it."""
    assert "=" not in doc and "+" not in doc and "/" not in doc
    return base64.urlsafe_b64decode(doc + "=" * (-len(doc) % 4)).decode()


async def test_draft_and_preview_link_the_app_review_page_with_the_document_in_the_fragment(service, tmp_path) -> None:
    path = str(tmp_path / "linked.json")
    draft = await call(
        "draft",
        *("--symbol", "btc", "--direction", "down", "--level", "90000", "--notional", "25"),
        *("--allocation", "200", "--expires", "7d", "--funding", "lt:0.001", "--stop-px", "85000", "--out", path),
    )
    assert not service.requests, "draft asks the service nothing"
    url = draft["app_review_url"]
    page, query, fragment = review_link(url)
    assert page == f"{DEVNET_APP}/agents/workflows/run"
    # As on the setup link: the agent key and the owner, so the page can link setup and ask for a switch.
    assert query == {"draft": [""], "agent": [ADDRESSES["agent"]], "owner": [ADDRESSES["owner"]]}
    assert list(fragment) == ["access", "doc"] and fragment["access"] == [agent.SETUP_PAGE_ACCESS_CODE]
    # The code and the document stay in the fragment, which the browser never sends to a server.
    parts = urlsplit(url)
    assert agent.SETUP_PAGE_ACCESS_CODE not in parts.netloc + parts.path + parts.query
    assert "doc" not in query

    text = decoded(fragment["doc"][0])
    file_doc = json.loads(Path(path).read_text())
    assert json.loads(text) == file_doc == draft["document"]
    assert text == json.dumps(file_doc, separators=(",", ":")), "compact: the link stays short"
    # The page previews and starts exactly this text, which the service reads as the file's workflow.
    assert wf.version_hash(wf.canonical_json(json.loads(text))) == wf.version_hash(wf.canonical_json(file_doc))

    preview = await call("preview", "--file", path)
    assert preview["app_review_url"] == url
    assert preview["next"].startswith(
        f"After the user confirms the rules and allocation above: workflow start --file {path}"
    )
    assert "give the user app_review_url exactly as returned instead" in preview["next"]
    assert not service.writes(), "a review link starts nothing"


async def test_the_review_link_carries_the_document_the_preview_read(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    doc = json.loads(Path(path).read_text())
    doc["limits"]["allocation_usd"] = "150"
    Path(path).write_text(json.dumps(doc, indent=4))
    preview = await call("preview", "--file", path)
    _, _, fragment = review_link(preview["app_review_url"])
    assert json.loads(decoded(fragment["doc"][0]))["limits"]["allocation_usd"] == "150"


def test_no_review_link_without_a_known_app_or_for_an_oversized_document(kit_env, monkeypatch) -> None:
    doc = {"schema": wf.SCHEMA_TAG}
    assert workflow._review_page(None, doc) is None
    # The cap is the kit's own document cap, which the app's page shares.
    fits = {"pad": "x" * (wf.MAX_DOCUMENT_BYTES - len('{"pad":""}'))}
    assert workflow._review_page(DEVNET_APP, fits) is not None
    assert workflow._review_page(DEVNET_APP, {"pad": fits["pad"] + "x"}) is None
    monkeypatch.setenv("REYA_APP_URL", "https://app.example.test/")
    overridden = workflow._review_page(workflow._app(), doc)
    assert overridden is not None and overridden.startswith("https://app.example.test/agents/workflows/run?draft&")
    # Before agent init has recorded the agent key and the owner, the link names neither.
    monkeypatch.delenv("PERP_WALLET_ADDRESS_1")
    anonymous = workflow._review_page(DEVNET_APP, doc)
    assert anonymous is not None
    _, query, _ = review_link(anonymous)
    assert query == {"draft": [""]}


def test_no_app_link_without_a_known_app_or_with_an_odd_run_id(kit_env, monkeypatch) -> None:
    assert workflow._run_page(DEVNET_APP, str(2**64 - 1)) == run_page(str(2**64 - 1))
    for odd in (None, "", "07", "-1", "1e3", " 1", "1#x", "١", 2**64):
        assert workflow._run_page(DEVNET_APP, odd) is None
    monkeypatch.setenv("REYA_API_URL", "https://api-cronos.reya.xyz/v2")  # no app is known for testnet
    app = workflow._app()
    assert app is None and workflow._run_page(app, 1) is None and workflow._runs_page(app, ACCOUNT) is None


# --------------------------------------------------------------------------
# Enable hosted runs: the setup page's last step, for a wallet already set up
# --------------------------------------------------------------------------


def enable_page(app: str = DEVNET_APP, *, account: bool = True) -> str:
    """agent init's setup link once the Agent account is recorded; the page's last step enables hosted runs."""
    recorded = f"&account={ACCOUNT}" if account else ""
    return (
        f"{app}/agents/setup?agent={ADDRESSES['agent']}&owner={ADDRESSES['owner']}{recorded}"
        f"#access={agent.SETUP_PAGE_ACCESS_CODE}"
    )


WALLET_STATUS = f"/api/trading/wallet/{ADDRESSES['owner']}/apiWalletStatus"


async def test_info_links_the_enable_hosted_runs_step_until_the_owner_approves(service, opened) -> None:
    service.hosted_enabled.clear()
    info = await call("info")
    assert info["hosted_signer"] == ADDRESSES["hosted"] and info["hosted_signer_enabled"] is False
    assert info["enable_url"] == enable_page()
    assert_code_only_in_fragment(info["enable_url"])
    assert "Give the user enable_url" in info["next"] and ADDRESSES["owner"] in info["next"]
    assert "not approved yet either" not in info["next"]
    # Read the way agent status reads the agent key's approval; nothing opened, signed or sent.
    assert service.trading_reads == [WALLET_STATUS]
    assert opened == [] and not service.writes()

    service.enable_hosted(ADDRESSES["owner"])
    enabled = await call("info")
    assert enabled["hosted_signer_enabled"] is True
    assert "enable_url" not in enabled and "next" not in enabled


async def test_info_says_when_the_agent_key_needs_approving_first(service) -> None:
    service.hosted_enabled.clear()
    service.delegates.clear()
    info = await call("info")
    assert info["hosted_signer_enabled"] is False and info["enable_url"] == enable_page()
    assert "The agent key is not approved yet either: the page starts there." in info["next"]


async def test_info_still_answers_when_hosted_runs_cannot_be_checked(service, monkeypatch) -> None:
    service.trading_failure = aiohttp.ClientConnectionError("connection refused")
    info = await call("info")
    assert info["hosted_signer"] == ADDRESSES["hosted"] and info["modes"] == ["shadow"]
    assert info["hosted_signer_enabled"] is None and "enable_url" not in info
    assert info["hosted_signer_note"] == "Could not read the owner's approvals: ClientConnectionError"
    # Before agent init has recorded the Agent account there is nothing to check yet.
    service.trading_failure = None
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", "")
    unset = await call("info")
    assert unset["hosted_signer_enabled"] is None and "Run agent status" in unset["next"]
    assert service.trading_reads == [WALLET_STATUS]
    # A service with no active hosted signer starts nothing: there is nothing to enable.
    service.info_override = {"hosted_signers": []}
    assert "hosted_signer_enabled" not in await call("info")


async def test_info_open_opens_the_enable_step_once_and_only_while_needed(service, tmp_path, opened) -> None:
    service.hosted_enabled.clear()
    first = await call("info", "--open")
    assert first["hosted_signer_enabled"] is False and first["enable_url"] == enable_page()
    assert first["opened"] is True and opened == [enable_page()]
    assert "now open at its Enable hosted runs step" in first["next"] and not service.writes()
    # Asked again, or a start refused for the same reason: that tab is already open.
    assert (await call("info", "--open"))["link_already_opened"] is True
    refused = await call("start", "--file", await drafted(tmp_path))
    assert refused["link_already_opened"] is True and opened == [enable_page()]
    service.enable_hosted(ADDRESSES["owner"])
    enabled = await call("info", "--open")
    assert enabled["hosted_signer_enabled"] is True and "opened" not in enabled and opened == [enable_page()]


async def test_info_open_without_a_browser_says_where_to_open_the_link(service, opened, monkeypatch) -> None:
    service.hosted_enabled.clear()
    monkeypatch.setattr(agent, "browser_unavailable", lambda: "no_display")
    result = await call("info", "--open")
    assert result["opened"] is False and result["browser_unavailable"] == "no_display" and opened == []
    assert "No browser opened (this machine has no display). Show the user enable_url" in result["next"]


async def test_a_start_refused_until_hosted_runs_are_enabled_opens_that_step_once(service, tmp_path, opened) -> None:
    path = await drafted(tmp_path)
    service.hosted_enabled.clear()
    refused = await call("start", "--file", path)
    assert refused["error_code"] == "HOSTED_SIGNER_NOT_ENABLED" and refused["hosted_signer"] == ADDRESSES["hosted"]
    assert refused["enable_url"] == enable_page() and refused["opened"] is True and opened == [enable_page()]
    assert refused["next"].startswith("Nothing was started.") and "now open at its Enable hosted runs step" in (
        refused["next"]
    )
    # Started again before the owner approves: the page is already open, so no second tab.
    again = await call("start", "--file", path)
    assert again["enable_url"] == enable_page() and again["link_already_opened"] is True
    assert "already open in a browser tab" in again["next"] and opened == [enable_page()]
    service.enable_hosted(ADDRESSES["owner"])
    assert (await call("start", "--file", path))["state"] == "active"
    assert opened == [enable_page()]


async def test_the_setup_tab_agent_init_opened_counts_as_the_enable_step(service, tmp_path, opened) -> None:
    # agent init opened the setup page before it recorded the account, so its link has no `account`. The
    # page then uses the owner's one Agent account (AGENTPERP), the recorded one: that tab shows the same page.
    agent.remember_opened(enable_page(account=False))
    path = await drafted(tmp_path)
    service.hosted_enabled.clear()
    refused = await call("start", "--file", path)
    assert refused["link_already_opened"] is True and opened == []


async def test_a_setup_tab_showing_another_account_does_not_count(service, tmp_path, opened) -> None:
    # The owner has a second Agent account, so the link without `account` need not show the recorded one.
    service.add_account(ACCOUNT + 1, ADDRESSES["owner"])
    agent.remember_opened(enable_page(account=False))
    path = await drafted(tmp_path)
    service.hosted_enabled.clear()
    refused = await call("start", "--file", path)
    assert refused["opened"] is True and opened == [enable_page()]


async def test_without_the_account_names_only_the_exact_link_counts_as_open(service, tmp_path, opened) -> None:
    agent.remember_opened(enable_page(account=False))
    path = await drafted(tmp_path)
    service.hosted_enabled.clear()
    service.trading_failure = aiohttp.ClientConnectionError("connection refused")
    refused = await call("start", "--file", path)
    assert refused["error_code"] == "HOSTED_SIGNER_NOT_ENABLED" and refused["enable_url"] == enable_page()
    assert refused["opened"] is True and opened == [enable_page()]


async def test_the_enable_step_link_when_nothing_opens(service, tmp_path, opened, monkeypatch) -> None:
    path = await drafted(tmp_path)
    service.hosted_enabled.clear()
    quiet = await call("start", "--file", path, "--no-open")
    assert quiet["enable_url"] == enable_page() and "opened" not in quiet and opened == []
    assert "Give the user enable_url" in quiet["next"]
    monkeypatch.setattr(agent, "browser_unavailable", lambda: "ssh")
    remote = await call("start", "--file", path)
    assert remote["opened"] is False and remote["browser_unavailable"] == "ssh" and opened == []
    assert "No browser opened (this is an SSH session). Show the user enable_url" in remote["next"]
    # Without a known app there is no link; the refusal still says what to do.
    monkeypatch.setenv("REYA_APP_URL", "http://app.example.test")
    unlinked = await call("start", "--file", path)
    assert unlinked["enable_url"] is None and unlinked["next"] == wf.ERROR_HINTS["HOSTED_SIGNER_NOT_ENABLED"]
    assert opened == [] and not [r for r in service.runs.values() if r["state"] == "active"]


async def test_an_already_triggered_level_is_called_out_in_the_preview(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.marks["BTCRUSDPERP"] = "89000"
    preview = await call("preview", "--file", path)
    assert "--fire-now" in preview["already_triggered"] and "--fire-now" in preview["next"]
    assert preview["now"].startswith("the mark is 89000, already at or below the level of 90000")


def sent_fire_now(service) -> bool:
    (_, _, body) = service.writes()[-1]
    return body["request"]["fireNow"]


async def test_a_level_crossed_since_the_preview_is_not_started_unasked(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    assert "already_triggered" not in await call("preview", "--file", path)
    # The mark crosses the level between the user's yes and the start (seen live on mainnet: 15 bps in 90 s).
    service.marks["BTCRUSDPERP"] = "89000"
    refused = await call("start", "--file", path)
    assert refused["error_code"] == "ALREADY_TRIGGERED" and "Nothing was signed" in refused["error"]
    assert refused["now"].startswith("the mark is 89000, already at or below the level of 90000")
    assert refused["trigger"]["already_triggered"] is True and refused["file"] == path
    assert f"workflow start --file {path} --fire-now" in refused["next"]
    assert f"workflow start --file {path} --wait-for-rearm" in refused["next"]
    assert not service.writes(), "nothing is signed or sent until the user chooses"


async def test_the_users_choice_for_a_crossed_level_is_started_and_said_back(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.marks["BTCRUSDPERP"] = "89000"
    fired = await call("start", "--file", path, "--fire-now")
    assert fired["started"] is True and fired["fire_now"] is True and sent_fire_now(service) is True
    assert fired["already_triggered"].endswith("fires on the first fresh mark.")
    assert (await call("stop", fired["run_id"]))["state"] == "stopped"
    waiting = await call("start", "--file", path, "--wait-for-rearm")
    assert waiting["started"] is True and waiting["fire_now"] is False and sent_fire_now(service) is False
    assert "waits until the mark moves back past the re-arm band" in waiting["already_triggered"]


async def test_a_start_says_where_the_run_arms(service, tmp_path) -> None:
    started = await call("start", "--file", await drafted(tmp_path))
    assert started["now"] == "the mark is 91420; this would arm 1.55% below, at 90000"
    assert "already_triggered" not in started


async def test_a_crossed_level_keeps_the_mode_in_the_suggested_commands(service, tmp_path) -> None:
    service.modes = ["shadow", "production"]
    path = await drafted(tmp_path)
    service.marks["BTCRUSDPERP"] = "89000"
    refused = await call("start", "--file", path, "--mode", "production")
    assert f"workflow start --file {path} --mode production --fire-now" in refused["next"]
    assert not service.writes()


def test_fire_now_belongs_to_start_not_preview() -> None:
    parser = workflow.build_parser()
    assert parser.parse_args(["start", "--file", "f.json", "--fire-now"]).fire_now is True
    assert parser.parse_args(["start", "--file", "f.json", "--wait-for-rearm"]).wait_for_rearm is True
    with pytest.raises(SystemExit):
        parser.parse_args(["preview", "--file", "f.json", "--fire-now"])
    with pytest.raises(SystemExit):
        parser.parse_args(["start", "--file", "f.json", "--fire-now", "--wait-for-rearm"])


def test_draft_offers_no_balance_condition() -> None:
    # The service cannot check the available balance, so the kit drafts no condition on it.
    parser = workflow.build_parser()
    base = ["draft", "--symbol", "btc", "--direction", "down", "--level", "90000"]
    base += ["--notional", "25", "--allocation", "200", "--expires", "7d"]
    assert parser.parse_args(base + ["--position", "lt:0.5"]).position == ["lt:0.5"]
    with pytest.raises(SystemExit):
        parser.parse_args(base + ["--balance", "gte:50"])
    with pytest.raises(ValueError, match="available_balance"):
        wf.condition("available_balance", "gte:50")


def test_a_stored_runs_balance_condition_is_still_described() -> None:
    # A run drafted before --balance went (or written by hand) can still carry one.
    doc = json.loads(HASHES["cases"][0]["canonical"])
    doc["steps"][0]["conditions"] = [{"kind": "available_balance", "op": "gte", "usd": "50"}]
    assert "if the available balance is at or above $50.00" in wf.describe(doc)
    view = wf.status_view(
        run_status(document={"canonical": json.dumps(doc, separators=(",", ":")), "version_hash": "0x" + "ab" * 32}),
        [event(1, "skipped", "skip_balance_unavailable", "triggered, skipped: available balance unknown")],
    )
    assert "if the available balance is at or above $50.00" in view["workflow"]
    assert view["skips"][0]["reason"] == "available balance unavailable"


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


# --------------------------------------------------------------------------
# Where the service is: a path on the API host
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "api, chain, expected",
    [
        ("https://api.reya.xyz/v2", "1729", "https://api.reya.xyz/workflows"),
        (DEVNET_API, "89346162", "https://api-devnet.reya-cronos.network/workflows"),
    ],
)
def test_each_deployment_defaults_to_the_workflows_path_on_its_api_host(api, chain, expected, monkeypatch) -> None:
    monkeypatch.setenv("REYA_API_URL", api)
    monkeypatch.setenv("CHAIN_ID", chain)
    monkeypatch.delenv("REYA_WORKFLOWS_URL", raising=False)
    assert wf.service_url() == expected


def test_a_deployment_without_a_known_service_needs_reya_workflows_url(monkeypatch) -> None:
    monkeypatch.setenv("REYA_API_URL", "https://api-cronos.reya.xyz/v2")
    monkeypatch.setenv("CHAIN_ID", "89346162")
    monkeypatch.delenv("REYA_WORKFLOWS_URL", raising=False)
    with pytest.raises(wf.WorkflowError, match="No workflow service is known .* set REYA_WORKFLOWS_URL"):
        wf.service_url()


@pytest.mark.parametrize(
    "configured, expected",
    [
        ("https://api.reya.xyz/workflows", "https://api.reya.xyz/workflows"),
        ("https://api.reya.xyz/workflows/", "https://api.reya.xyz/workflows"),
        ("https://api.reya.xyz/workflows//", "https://api.reya.xyz/workflows"),
        (" https://api.reya.xyz/workflows ", "https://api.reya.xyz/workflows"),
        ("https://gateway.example/reya/workflows", "https://gateway.example/reya/workflows"),
        ("https://workflows-devnet.reya-cronos.network", "https://workflows-devnet.reya-cronos.network"),
        ("https://workflows-devnet.reya-cronos.network/", "https://workflows-devnet.reya-cronos.network"),
        ("http://127.0.0.1:8081", "http://127.0.0.1:8081"),
        ("http://localhost:8080/workflows", "http://localhost:8080/workflows"),
    ],
)
def test_reya_workflows_url_may_carry_a_base_path(configured, expected, kit_env, monkeypatch) -> None:
    monkeypatch.setenv("REYA_WORKFLOWS_URL", configured)
    assert wf.service_url() == expected


@pytest.mark.parametrize(
    "configured, error",
    [
        ("ftp://api.reya.xyz/workflows", "must be an HTTP"),
        ("https:///workflows", "must be an HTTP"),
        ("https://user:secret@api.reya.xyz/workflows", "must be an HTTP"),
        ("https://api.reya.xyz/workflows?env=mainnet", "must be an HTTP"),
        ("https://api.reya.xyz/workflows?", "must be an HTTP"),
        ("https://api.reya.xyz/workflows#v1", "must be an HTTP"),
        ("https://api.reya.xyz//workflows", "must be an HTTP"),
        ("https://api.reya.xyz/./workflows", "must be an HTTP"),
        ("https://api.reya.xyz/workflows/..", "must be an HTTP"),
        ("https://api.reya.xyz/work%2Fflows", "must be an HTTP"),
        ("https://api.reya.xyz/work flows", "must be an HTTP"),
        ("https://api.reya.xyz/workflows/v1", "leave out /v1"),
        ("https://api.reya.xyz/workflows/v1/", "leave out /v1"),
        ("http://api.reya.xyz/workflows", "must use HTTPS except on localhost"),
    ],
)
def test_reya_workflows_url_is_refused_when_a_path_join_would_be_ambiguous(
    configured, error, kit_env, monkeypatch
) -> None:
    monkeypatch.setenv("REYA_WORKFLOWS_URL", configured)
    with pytest.raises(wf.WorkflowError, match=error):
        wf.service_url()


async def test_every_request_is_the_base_path_plus_the_api_path(service, tmp_path) -> None:
    """The fake answers only under /workflows, as the API host's gateway does."""
    assert service.url.endswith("/workflows")
    path = await drafted(tmp_path)
    await call("info")
    await call("validate", "--file", path)
    await call("preview", "--file", path)
    run_id = (await call("start", "--file", path))["run_id"]
    service.add_event(run_id, "skipped", "skip_condition_funding", "triggered, skipped: funding")
    await call("status", run_id)
    await call("status")
    await call("stop", run_id)
    assert service.raw_paths and all(p.startswith("/workflows/v1/") for p in service.raw_paths), service.raw_paths
    sent = {p.split("?")[0] for p in service.raw_paths}
    assert {
        "/workflows/v1/info",
        "/workflows/v1/validate",
        "/workflows/v1/preview",
        "/workflows/v1/runs",
        f"/workflows/v1/runs/{run_id}",
        f"/workflows/v1/runs/{run_id}/activity",
        f"/workflows/v1/accounts/{ACCOUNT}/runs",
        f"/workflows/v1/runs/{run_id}/stop",
    } <= sent, sent
    # Query strings are joined after the path, never into it.
    assert any(p.startswith(f"/workflows/v1/accounts/{ACCOUNT}/runs?") for p in service.raw_paths), service.raw_paths


async def test_a_service_at_the_root_of_its_host_still_works(kit_env, loopback, monkeypatch) -> None:
    fake = FakeWorkflowService(hosted=ADDRESSES["hosted"], base_path="")
    await fake.start_server()
    try:
        monkeypatch.setenv("REYA_WORKFLOWS_URL", fake.url)
        monkeypatch.setattr(agent, "AccountRelay", fake.relay())
        assert urlsplit(fake.url).path == ""
        info = await call("info")
        assert info["hosted_signer"] == ADDRESSES["hosted"] and info["service"] == fake.url
        assert fake.raw_paths == ["/v1/info"]
    finally:
        await fake.close()


async def test_a_base_url_without_the_prefix_reaches_nothing_and_says_so(service, monkeypatch) -> None:
    origin = service.url.removesuffix("/workflows")
    monkeypatch.setenv("REYA_WORKFLOWS_URL", origin)
    result = await call("info")
    assert "HTTP 404" in result["error"], result
    assert service.raw_paths == [], "the gateway-like fake answered outside its prefix"


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


async def test_agent_setup_reads_the_active_hosted_signer_or_offers_nothing(service, monkeypatch) -> None:
    """agent init's optional Enable hosted runs step: the signer comes from the service's /v1/info, and any
    trouble reading it means no offer, never a failed setup."""
    assert await agent.hosted_signer() == ADDRESSES["hosted"]
    service.info_override = {"environment": "mainnet", "chain_id": "1729"}
    assert await agent.hosted_signer() is None, "a service for another deployment offers nothing"
    service.info_override = {"hosted_signers": []}
    assert await agent.hosted_signer() is None, "no active signer: no new run can start, so nothing to enable"
    service.info_override = None
    await service.close()
    assert await agent.hosted_signer() is None, "an unreachable service offers nothing"
    monkeypatch.delenv("REYA_WORKFLOWS_URL")
    monkeypatch.setenv("REYA_API_URL", "https://api-cronos.reya.xyz/v2")
    monkeypatch.setenv("CHAIN_ID", "89346162")
    assert await agent.hosted_signer() is None, "a deployment with no known service (testnet) offers nothing"
    monkeypatch.setenv("REYA_API_URL", "https://api.example.test/v2")
    assert await agent.hosted_signer() is None, "a custom deployment with no environment offers nothing"


async def test_reading_the_hosted_signer_never_raises(service, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("something nobody expected")

    monkeypatch.setattr(wf, "parse_info", broken)
    assert await agent.hosted_signer() is None
    monkeypatch.setitem(sys.modules, "agent_kit.scripts._workflow", None)  # the import itself fails
    assert await agent.hosted_signer() is None


async def test_info_open_reuses_the_setup_tab_opened_with_an_amount(service, opened) -> None:
    service.hosted_enabled.clear()
    funding_tab = enable_page().replace("#access", "&amount=50#access")
    agent.remember_opened(funding_tab)  # agent init --amount 50 opened the setup page at funding
    result = await call("info", "--open")
    assert result["hosted_signer_enabled"] is False and result["link_already_opened"] is True
    assert opened == [], "the tab left at the funding step moves on to Enable hosted runs"


async def test_a_slow_workflow_service_never_holds_agent_setup_up(service, monkeypatch) -> None:
    async def slow_info(request):
        await asyncio.sleep(5)
        return web.json_response(service.info_body())

    monkeypatch.setattr(service, "info", slow_info)
    await service.close()
    await service.start_server()  # re-registers the routes with the slow /v1/info
    monkeypatch.setenv("REYA_WORKFLOWS_URL", service.url)
    monkeypatch.setattr(agent, "HOSTED_RUNS_INFO_TIMEOUT_S", 0.2)
    began = time.monotonic()
    assert await agent.hosted_signer() is None
    assert time.monotonic() - began < 2, "the optional read gives up at its timeout"


# --------------------------------------------------------------------------
# Read grants: the agent key's grant on run reads
# --------------------------------------------------------------------------

# The build contract's ReadGrant, written out here rather than taken from the kit.
READ_GRANT_TYPE = [
    {"name": "verifyingChainId", "type": "uint256"},
    {"name": "environment", "type": "string"},
    {"name": "owner", "type": "address"},
    {"name": "accountIds", "type": "uint128[]"},
    {"name": "expiresAt", "type": "uint64"},
    {"name": "salt", "type": "bytes16"},
]
SALT = bytes(range(16))


def a_grant(**changes) -> dict:
    fields = dict(
        chain_id=1729,
        environment="mainnet",
        owner=ADDRESSES["owner"].lower(),
        account_ids={136341, 7},
        expires_at=1798761600,
        salt=SALT,
    )
    return wf.read_grant(**(fields | changes))


def decode_token(header: str) -> dict:
    assert header.startswith("Bearer ")
    token = header.removeprefix("Bearer ")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token), "base64url without padding"
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def test_a_read_grant_is_spelled_as_the_contract_says() -> None:
    assert a_grant() == {
        "verifyingChainId": "1729",
        "environment": "mainnet",
        "owner": ADDRESSES["owner"],
        "accountIds": ["7", "136341"],
        "expiresAt": "1798761600",
        "salt": "0x000102030405060708090a0b0c0d0e0f",
    }
    with pytest.raises(wf.WorkflowError, match="16 bytes"):
        a_grant(salt=b"short")


@pytest.mark.parametrize("accounts", [{136341, 7}, set()], ids=["accounts", "all-accounts"])
def test_the_read_grant_digest_matches_a_hand_encoding_of_the_struct(accounts: set) -> None:
    grant = a_grant(account_ids=accounts)
    type_hash = keccak(
        text="ReadGrant(uint256 verifyingChainId,string environment,address owner,uint128[] accountIds,"
        "uint64 expiresAt,bytes16 salt)"
    )
    domain = keccak(
        abi_encode(
            ["bytes32", "bytes32", "bytes32"],
            [keccak(text="EIP712Domain(string name,string version)"), keccak(text="Reya Workflows"), keccak(text="1")],
        )
    )
    account_list = keccak(b"".join(abi_encode(["uint128"], [a]) for a in sorted(accounts)))
    struct = keccak(
        abi_encode(
            ["bytes32", "uint256", "bytes32", "address", "bytes32", "uint64", "bytes16"],
            [type_hash, 1729, keccak(text="mainnet"), ADDRESSES["owner"], account_list, 1798761600, SALT],
        )
    )
    assert wf.read_grant_digest(grant) == "0x" + keccak(b"\x19\x01" + domain + struct).hex()


def test_a_read_grant_signed_with_the_agent_key_recovers_to_the_agent_key() -> None:
    grant = a_grant()
    signature = wf.sign_read_grant(grant, KEYS["agent"])
    raw = bytes.fromhex(signature[2:])
    assert len(raw) == 65 and raw[64] in (27, 28) and int.from_bytes(raw[32:64], "big") <= wf.SECP256K1_N // 2
    # Recovered with eth_account's own typed-data encoder from the contract's struct, not the kit's encoding.
    values = {
        "verifyingChainId": 1729,
        "environment": "mainnet",
        "owner": ADDRESSES["owner"],
        "accountIds": [7, 136341],
        "expiresAt": 1798761600,
        "salt": SALT,
    }
    signable = encode_typed_data(
        full_message={
            "types": {
                "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}],
                "ReadGrant": READ_GRANT_TYPE,
            },
            "primaryType": "ReadGrant",
            "domain": {"name": "Reya Workflows", "version": "1"},
            "message": values,
        }
    )
    assert Account.recover_message(signable, signature=signature) == ADDRESSES["agent"]
    assert wf.recover_digest(wf.read_grant_digest(grant), signature) == ADDRESSES["agent"]
    # Any changed field recovers someone else.
    assert wf.recover_digest(wf.read_grant_digest(a_grant(account_ids=set())), signature) != ADDRESSES["agent"]
    # Pinned, for the service's and the app's own implementations to check against (RFC 6979 signatures).
    assert wf.read_grant_digest(grant) == "0x92c4b5042e15c8d71bc0d390c924b709e5daf9c04d80b433b56ec1cb67ab7aff"
    assert signature == (
        "0x52753a649067a9f72b2991bde59cbd6ca75ce4a8c2a3226ffd942d5a431bb7e0"
        "4b9988a328fc9239b69bfaf1965456b2af0d113497031b27d28e0b128cb30cf31b"
    )


def test_the_header_is_bearer_base64url_json_of_the_grant_and_its_signature(kit_env) -> None:
    header = wf.ReadGrants(now=lambda: 1_800_000_000).header()
    assert header is not None and KEYS["agent"][2:] not in header
    body = decode_token(header)
    assert list(body) == ["grant", "signature"]
    grant = body["grant"]
    assert list(grant) == [f["name"] for f in READ_GRANT_TYPE]
    assert grant["verifyingChainId"] == "89346162" and grant["environment"] == "devnet1"
    assert grant["owner"] == ADDRESSES["owner"] and grant["accountIds"] == [str(ACCOUNT)]
    assert grant["expiresAt"] == str(1_800_000_000 + 3600)
    assert re.fullmatch(r"0x[0-9a-f]{32}", grant["salt"]) and re.fullmatch(r"0x[0-9a-f]{130}", body["signature"])
    assert wf.parse_read_grant_token(header.removeprefix("Bearer "))[2] == ADDRESSES["agent"]


def test_a_grant_is_kept_for_the_next_command_and_replaced_at_80_percent_of_its_life(kit_env, monkeypatch) -> None:
    start = 1_800_000_000
    clock = [float(start)]

    def header() -> str | None:
        return wf.ReadGrants(now=lambda: clock[0]).header()  # a new command each time

    first = header()
    assert first is not None and header() == first
    path = wf.state_directory() / wf.READ_GRANT_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    clock[0] = start + 0.79 * 3600
    assert header() == first
    clock[0] = start + 0.81 * 3600
    second = header()
    assert second is not None and second != first
    assert decode_token(second)["grant"]["expiresAt"] == str(int(clock[0]) + 3600)
    # Another set of recorded accounts, or a kept grant another key signed, gets a new grant.
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", f"{ACCOUNT},654321")
    third = header()
    assert third is not None and decode_token(third)["grant"]["accountIds"] == [str(ACCOUNT), "654321"]
    grant = decode_token(third)["grant"]
    path.write_text(json.dumps({"token": wf.read_grant_token(grant, wf.sign_read_grant(grant, KEYS["owner"]))}))
    fourth = header()
    assert fourth is not None and wf.parse_read_grant_token(fourth.removeprefix("Bearer "))[2] == ADDRESSES["agent"]
    # A kept file that is not a grant is replaced, never an error.
    path.write_text("not json")
    assert header() is not None


def test_no_grant_when_the_kit_cannot_sign_one_as_a_delegate(kit_env, monkeypatch) -> None:
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", KEYS["owner"])
    assert wf.ReadGrants().header() is None, "never the owner's key"
    monkeypatch.delenv("REYA_SIGNER")
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", "")
    assert wf.ReadGrants().header() is None
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", str(ACCOUNT))
    monkeypatch.delenv("PERP_WALLET_ADDRESS_1")
    assert wf.ReadGrants().header() is None
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", ADDRESSES["owner"])
    monkeypatch.setenv("REYA_STATE_DIR", "relative/state")  # unusable: the read still goes, without a grant
    assert wf.ReadGrants().header() is None


def grant_headers(service: FakeWorkflowService) -> dict:
    """Each request kind -> the set of Authorization headers it carried."""
    seen: dict = {}
    for method, path, authorization in service.authorizations:
        kind = re.sub(r"/[0-9]+", "/{id}", path)
        seen.setdefault((method, kind), set()).add(authorization)
    return seen


async def test_run_reads_carry_the_agent_keys_grant_and_nothing_else_does(service, tmp_path) -> None:
    service.read_grants = "check"
    path = await drafted(tmp_path)
    run_id = (await call("start", "--file", path))["run_id"]
    assert (await call("status", run_id))["run_id"] == run_id
    assert (await call("status"))["active_run"]["run_id"] == run_id
    assert (await call("stop", run_id))["state"] == "stopped"
    await call("info")
    seen = grant_headers(service)
    reads = {("GET", "/v1/runs/{id}"), ("GET", "/v1/runs/{id}/activity"), ("GET", "/v1/accounts/{id}/runs")}
    assert reads <= set(seen)
    tokens = set().union(*(seen[kind] for kind in reads))
    assert len(tokens) == 1 and None not in tokens, "one grant, kept across the commands"
    assert all(seen[kind] == {None} for kind in set(seen) - reads), "only the run reads carry it"
    assert service.grant_signers and set(service.grant_signers) == {ADDRESSES["agent"]}
    assert len(service.grant_signers) == sum(1 for _, p, a in service.authorizations if a is not None)


async def test_a_gateway_that_refuses_the_header_still_answers(service, tmp_path) -> None:
    service.read_grants = "refuse"
    path = await drafted(tmp_path)
    run_id = (await call("start", "--file", path))["run_id"]
    service.authorizations.clear()
    status = await call("status", run_id)
    assert status["run_id"] == run_id and "error" not in status
    # A refusal without the service's error envelope: each read went once with the grant and once more
    # without it; nothing else was repeated.
    assert [(p, a is not None) for _, p, a in service.authorizations] == [
        (f"/v1/runs/{run_id}", True),
        (f"/v1/runs/{run_id}", False),
        (f"/v1/runs/{run_id}/activity", True),
        (f"/v1/runs/{run_id}/activity", False),
    ]
    assert not (wf.state_directory() / wf.READ_GRANT_FILE).exists(), "a refused grant is not kept"


def kept_grant() -> bool:
    return (wf.state_directory() / wf.READ_GRANT_FILE).exists()


async def test_a_run_the_grant_does_not_cover_is_not_found_once_and_the_grant_is_kept(service, tmp_path) -> None:
    service.read_grants = "require"
    path = await drafted(tmp_path)
    run_id = (await call("start", "--file", path))["run_id"]
    assert (await call("status", run_id))["run_id"] == run_id and kept_grant()
    # A missing run, and a run on an account the grant does not name: the service's answer stands.
    service.add_account(654321, ADDRESSES["owner"])
    service.runs[run_id]["account_id"] = "654321"
    for missing in ("999", run_id):
        service.authorizations.clear()
        result = await call("status", missing)
        assert result["error_code"] == "RUN_NOT_FOUND"
        assert [a is not None for _, _, a in service.authorizations] == [True], "never asked again without it"
        assert kept_grant(), "a genuine not-found keeps the grant"


async def test_a_grant_the_service_refuses_is_not_retried_without_it_and_says_what_to_do(service, tmp_path) -> None:
    service.read_grants = "require"
    path = await drafted(tmp_path)
    run_id = (await call("start", "--file", path))["run_id"]
    assert (await call("status", run_id))["run_id"] == run_id
    service.delegates[ADDRESSES["owner"].lower()].discard(ADDRESSES["agent"].lower())  # the owner revoked it
    service.authorizations.clear()
    result = await call("status", run_id)
    assert result["error_code"] == "INVALID_READ_GRANT" and result["http_status"] == 401
    assert result["details"] == {"reason": "not_delegate"}
    assert "re-approve the agent key" in result["next"] and "clock" in result["next"]
    assert [a is not None for _, _, a in service.authorizations] == [True], "never asked again without it"
    assert not kept_grant(), "a refused grant is not sent again"
    assert KEYS["agent"][2:] not in json.dumps(result)


async def test_without_a_grant_an_enforcing_service_says_what_to_do(service, tmp_path, monkeypatch) -> None:
    service.read_grants = "require"
    path = await drafted(tmp_path)
    run_id = (await call("start", "--file", path))["run_id"]
    monkeypatch.setenv("REYA_SIGNER", "own-key")  # the kit never signs a grant with this key
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", KEYS["owner"])
    result = await call("status", run_id)
    assert result["error_code"] == "READ_GRANT_REQUIRED"
    assert "agent status" in result["next"] and "clock" in result["next"]
    assert all(a is None for _, _, a in service.authorizations)
