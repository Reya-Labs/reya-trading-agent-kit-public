"""Strategy commands (`reya.workflow/3`): request signing against the service's vectors, the basket the kit drafts
against the service's example, the Agent-account and agent-key guards, and the command flow against a local fake
of the service.

The vectors in fixtures/workflow_requests are the service's own signed-request vectors (crates/workflow-service/
vectors/requests), and fixtures/workflow_v3_examples the format's examples (crates/workflow-core/fixtures/v3/
examples), both copied unchanged.
"""

from types import SimpleNamespace

import asyncio
import base64
import copy
import json
import re
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web
from eth_account import Account
from eth_account.messages import encode_typed_data

from agent_kit.scripts import _workflow as wf
from agent_kit.scripts import agent, strategy, workflow
from agent_kit.tests.conftest import parse_stdout
from agent_kit.tests.workflow_fake_service import FakeWorkflowService

FIXTURES = Path(__file__).parent / "fixtures"
REQUESTS = FIXTURES / "workflow_requests"
START = json.loads((REQUESTS / "strategy_start.json").read_text())
STOP = json.loads((REQUESTS / "strategy_stop.json").read_text())
GRANTS = json.loads((REQUESTS / "read_grant.json").read_text())
EXAMPLES = {path.stem: path.read_text().strip() for path in sorted((FIXTURES / "workflow_v3_examples").glob("*.json"))}
KEYS = {name: entry["private_key"] for name, entry in START["keys"].items()}
ADDRESSES = {name: entry["address"] for name, entry in START["keys"].items()}
CHAINS = {"devnet1": 89346162, "testnet": 89346162, "mainnet": 1729}
ACCOUNT = 123456
DEVNET_API = "https://api-devnet.reya-cronos.network/v2"
DEVNET_APP = "https://app-devnet-cf.reya.xyz"
# Captured at import, before the suite's offline guard replaces it for each test.
REAL_CONNECT = socket.socket.connect
WALLET_STATUS = f"/api/trading/wallet/{ADDRESSES['owner']}/apiWalletStatus"
OWNER_ACCOUNTS = f"/v2/wallet/{ADDRESSES['owner']}/accounts"
# The flags that draft the format's example e1: 0.1 ETH every day for 10 rounds, a 10% stop and a $300 loss limit.
E1_FLAGS = ["--order", "eth:buy:0.1ETH", "--trigger", "every:1d", "--rounds", "10", "--stop-loss", "10"]
E1_FLAGS += ["--max-loss", "300"]


def vector_info(environment: str = "devnet1") -> wf.ServiceInfo:
    return wf.ServiceInfo(
        environment=environment,
        chain_id=CHAINS[environment],
        domain=wf.parse_domain(START["domain"]),
        types=wf.parse_types({**START["types"], **STOP["types"]}),
        hosted_signers=[{"address": ADDRESSES["hosted"], "role": "active", "can_sign": True}],
        schemas=(wf.SCHEMA_TAG,),
        live_enabled=True,
        paper_enabled=True,
    )


def normalized(signature: str) -> str:
    raw = bytes.fromhex(signature[2:])
    return "0x" + (raw[:64] + bytes([raw[64] + 27 if raw[64] < 27 else raw[64]])).hex()


def verdict(check) -> dict:
    try:
        return {"ok": True, "signer": check()}
    except wf.WorkflowError as exc:
        return {"error": exc.code, **({"reason": exc.details["reason"]} if "reason" in exc.details else {})}


# --------------------------------------------------------------------------
# Signing: byte for byte against the service's vectors
# --------------------------------------------------------------------------


def test_the_type_hashes_are_the_services() -> None:
    info = vector_info()
    for vectors, primary in ((START, "StrategyStart"), (STOP, "StrategyStop")):
        assert [f["name"] for f in info.types[primary]] == [f["name"] for f in vectors["types"][primary]]
        case = next(c for c in vectors["cases"] if c["expect"].get("ok"))
        body = json.loads(case["body"]) if isinstance(case["body"], str) else case["body"]
        assert wf.typed_data(primary, body["request"], info).header == bytes.fromhex(vectors["domain_separator"][2:])


@pytest.mark.parametrize("case", [c for c in START["cases"]], ids=lambda c: c["name"])
def test_every_start_vector_reaches_the_services_verdict(case: dict) -> None:
    info = vector_info(case["context"]["environment"])
    now = int(case["context"]["now"])
    expect = {k: v for k, v in case["expect"].items() if k != "field"}
    assert verdict(lambda: wf.verify_start_body(case["body"], info, now)) == expect
    if case["expect"].get("ok"):
        body = json.loads(case["body"])
        request = body["request"]
        assert wf.struct_hash("StrategyStart", request, info) == case["struct_hash"]
        assert wf.digest("StrategyStart", request, info) == case["digest"]
        assert wf.sign("StrategyStart", request, KEYS[case["signed_by"]], info) == normalized(body["signature"])


@pytest.mark.parametrize("case", [c for c in STOP["cases"]], ids=lambda c: c["name"])
def test_every_stop_vector_reaches_the_services_verdict(case: dict) -> None:
    info = vector_info(case["context"]["environment"])
    now = int(case["context"]["now"])
    expect = {k: v for k, v in case["expect"].items() if k != "field"}
    got = verdict(lambda: wf.verify_stop_body(case["path_run_id"], case["body"], info, now))
    assert {k: v for k, v in got.items() if k != "reason"} == expect
    if case["expect"].get("ok"):
        request = case["body"]["request"]
        assert wf.digest("StrategyStop", request, info) == case["digest"]
        assert wf.sign("StrategyStop", request, KEYS[case["signed_by"]], info) == normalized(case["body"]["signature"])


@pytest.mark.parametrize(
    "case",
    [
        c
        for c in GRANTS["cases"]
        if c["expect"].get("ok") or c["expect"].get("reason") in ("bad_signature", "malformed")
    ],
    ids=lambda c: c["name"],
)
def test_the_read_grant_vectors_parse_and_recover_as_the_service_does(case: dict) -> None:
    """The stateless half of the service's checks: the token's shape and the signature. Its clock, environment
    and delegate checks need state; the kit never signs a grant they refuse (see the ReadGrants tests)."""
    header = case["authorization"]
    token = header.split(" ", 1)[1] if " " in header else header
    if not header.lower().startswith("bearer "):
        assert case["expect"].get("error") == "INVALID_READ_GRANT"
        return
    got = verdict(lambda: wf.parse_read_grant_token(token.rstrip("="))[2])
    if case["expect"].get("ok"):
        assert got == case["expect"]
        if case["name"].startswith("the agent key grants"):
            grant = case["token"]["grant"]
            assert wf.read_grant_digest(grant) == case["digest"]
            assert wf.sign_read_grant(grant, KEYS["agent"]) == normalized(case["token"]["signature"])
    else:
        assert "error" in got, case["name"]


def test_a_start_signs_the_preview_and_nothing_else() -> None:
    info = vector_info()
    request = wf.start_request(
        info,
        account_id=1001,
        version_hash="0x" + "AB" * 32,
        expires_at=1792371600,
        mode="paper",
        fire_now=True,
        preview_id=17,
        signer=ADDRESSES["agent"].lower(),
        nonce=1792368000000000,
        deadline=1792368300,
    )
    case = START["cases"][0]
    assert json.loads(case["body"])["request"] == request, "the vector's own Start, field for field"
    assert list(request) == [f["name"] for f in START["types"]["StrategyStart"]]
    assert request["handoverStops"] == [], "the kit moves no exchange stop into a strategy"
    with pytest.raises(wf.WorkflowError):
        wf.check_message("StrategyStart", {**request, "handoverStops": [{"market": "ETHRUSDPERP"}]})
    with pytest.raises(wf.WorkflowError, match="unknown mode"):
        wf.check_message("StrategyStart", {**request, "mode": "production"})


# --------------------------------------------------------------------------
# /v1/info
# --------------------------------------------------------------------------


def test_info_is_read_from_the_v3_section_and_its_types_checked() -> None:
    service = FakeWorkflowService(hosted=ADDRESSES["hosted"])
    info = wf.parse_info(service.info_body(), "devnet1", 89346162, "x")
    assert info.modes == ["paper", "live"] and info.active_signer == ADDRESSES["hosted"]
    assert info.types["StrategyStart"] == START["types"]["StrategyStart"]
    assert info.version == "0.20.10" and info.account_checks is True
    view = info.view()
    assert view["mode_names"] == {"paper": "Paper", "live": "Live"} and view["refused_shapes"]
    service.paper_enabled, service.live_enabled = True, False
    assert wf.parse_info(service.info_body(), "devnet1", 89346162, "x").modes == ["paper"]
    # A Start type with one more field, a domain with a chain, or another ReadGrant: the kit signs none of them.
    for edit in (
        lambda b: b["v3"]["eip712"]["types"]["StrategyStart"].append({"name": "extra", "type": "string"}),
        lambda b: b["v3"]["eip712"]["types"]["HandoverStop"].pop(),
        lambda b: b["v3"]["eip712"]["domain"].update(chainId="1"),
        lambda b: b["eip712"]["types"]["ReadGrant"].pop(),
    ):
        body = copy.deepcopy(service.info_body())
        edit(body)
        with pytest.raises(wf.WorkflowError, match="update the kit"):
            wf.parse_info(body, "devnet1", 89346162, "x")
    with pytest.raises(wf.WorkflowError, match="WRONG|serves devnet1"):
        wf.parse_info(service.info_body(), "mainnet", 1729, "x")


def test_a_service_from_before_v3_is_told_apart() -> None:
    previous = {"environment": "devnet1", "chain_id": "89346162", "eip712": {}, "limits": {}, "modes": ["paper"]}
    with pytest.raises(wf.WorkflowError, match="reya.workflow/3; update the kit"):
        wf.parse_info(previous, "devnet1", 89346162, "x")
    info = wf.parse_info(
        FakeWorkflowService(hosted=ADDRESSES["hosted"]).info_body() | {"schemas": ["reya.workflow/2"]},
        "devnet1",
        89346162,
        "x",
    )
    with pytest.raises(wf.WorkflowError, match="nothing was signed"):
        info.check_schema()


# --------------------------------------------------------------------------
# Documents and drafting
# --------------------------------------------------------------------------


def test_the_draft_flags_write_the_formats_example_byte_for_byte() -> None:
    args = workflow.build_parser().parse_args(["draft", *E1_FLAGS])
    spec = wf.basket(
        orders=[wf.parse_order(s) for s in args.order],
        trigger=wf.parse_trigger(args.trigger),
        rounds=args.rounds,
        stop_pct=args.stop_loss,
        max_loss=args.max_loss,
    )
    text = wf.canonical_json(wf.basket_document(spec))
    assert text == EXAMPLES["e1"]
    assert wf.version_hash(text) == "0x8f2dd3ef5b940048f28661e9732bd99cb644e2fe92e8e7402dbbb9b62edf5568"


@pytest.mark.parametrize("name", ["e1", "e3", "e6", "e7"])
def test_reminting_an_example_keeps_it_and_reminting_shuffled_ids_restores_it(name: str) -> None:
    doc = json.loads(EXAMPLES[name])
    assert wf.canonical_json(wf.remint(doc)) == EXAMPLES[name]
    # The same document with every id renumbered backwards is numbered as the example again.
    ids = wf.walk_order(doc)
    renamed = {old: f"{old.split('_')[0]}_{9000 - n}" for n, old in enumerate(ids)}
    text = EXAMPLES[name]
    for old in sorted(renamed, key=len, reverse=True):
        text = re.sub(rf'"{old}"', f'"{renamed[old]}"', text)
    assert wf.canonical_json(wf.remint(json.loads(text))) == EXAMPLES[name]


def test_every_example_reads_as_plain_sentences() -> None:
    for name, text in EXAMPLES.items():
        doc = wf.parse_document(text)
        lines = wf.rules(doc)
        assert lines[0].startswith("Starts in") and all(isinstance(line, str) and line for line in lines), name
    e1 = wf.rules(json.loads(EXAMPLES["e1"]))
    assert "In st_1: Every 1 day: buy 0.1 ETH at once (max slippage 25 bps), then go to another round of st_1." in e1
    assert any("stop-loss on ETH is hit (10% from the entry)" in line for line in e1)
    assert wf.document_markets(json.loads(EXAMPLES["e3"])) == ["ETHRUSDPERP", "BTCRUSDPERP"]


@pytest.mark.parametrize(
    ("flags", "shape"),
    [
        (["--order", "eth:buy:100"], "start"),
        (["--order", "sol:buy:200", "--order", "btc:sell:0.01BTC", "--trigger", "price:sol:down:120"], "cross"),
        (["--order", "eth:buy:100", "--trigger", "every:4h", "--rounds", "3", "--first-now"], "first_now"),
        (
            ["--order", "eth:buy:100", "--trigger", "every:4h", "--rounds", "1", "--first-now", "--stop-loss", "3"],
            "one",
        ),
    ],
)
def test_each_draft_shape_numbers_its_blocks_from_one_in_walk_order(flags: list, shape: str) -> None:
    args = workflow.build_parser().parse_args(["draft", *flags])
    spec = wf.basket(
        orders=[wf.parse_order(s) for s in args.order],
        trigger=wf.parse_trigger(args.trigger),
        rounds=args.rounds,
        first_now=args.first_now,
        stop_pct=args.stop_loss,
    )
    doc = wf.basket_document(spec)
    order = wf.walk_order(doc)
    assert [int(i.split("_")[1]) for i in order] == list(range(1, len(order) + 1)), "numbered in walk order"
    assert doc["start"] == "st_1" and doc["on_liquidation"] == "close_and_end"
    assert all(a["max_slippage_bps"] == 25 for a in doc["actions"].values())
    finals = [s for s in doc["states"].values() if s["kind"] == "final"]
    assert all(
        set(s.get("on", [])) <= {r for r, rule in doc["rules"].items() if "levels" in rule["when"]} for s in finals
    )
    if shape == "cross":
        entry = doc["rules"]["r_2"]["when"]
        assert entry == {
            "kind": "cross",
            "value": {"kind": "series", "market": "SOLRUSDPERP", "series": "mark"},
            "direction": "down",
            "level": {"kind": "num", "value": "120"},
        }
        assert [a["size"] for a in doc["actions"].values()] == [{"rusd": "200"}, {"qty": "0.01"}]
    if shape == "one":
        assert all(s["kind"] != "loop_head" for s in doc["states"].values()), "one round in all: no loop"


@pytest.mark.parametrize(
    ("flags", "error"),
    [
        ([], "at least one --order"),
        (["--order", "eth:buy:100", "--order", "eth:sell:50"], "ETH is given twice"),
        (["--order", "eth:buy:100", "--trigger", "every:1d"], "needs --rounds"),
        (["--order", "eth:buy:100", "--rounds", "3"], "go with --trigger every"),
        (["--order", "eth:buy:100", "--trigger", "every:1d", "--rounds", "2", "--exit-after", "1d"], "--exit-after"),
        (["--order", "eth:buy:1SOL"], "not ETHRUSDPERP's base asset"),
        (["--order", "eth:buy:100USD"], "USD is a plain number"),
        (["--order", "eth:buy:25%"], "a size is USD"),
        (["--order", "eth:hold:100"], "buy or sell"),
        (["--order", "eth:buy:100", "--trigger", "at:daily@09:00"], "--trigger takes"),
        (["--order", "eth:buy:100", "--order", "btc:buy:100", "--order", "sol:buy:100", "--order", "xau:buy:10",
          "--stop-loss", "5", "--take-profit", "5"], "at most 6"),
    ],
)  # fmt: skip
async def test_a_draft_the_format_would_refuse_is_refused_before_anything_is_asked(
    service, tmp_path, flags: list, error: str
) -> None:
    result = await call("draft", *flags, "--out", str(tmp_path / "d.json"))
    assert error in result["error"] and not (tmp_path / "d.json").exists()
    assert service.requests == [], "nothing was asked"


def test_documents_of_the_previous_versions_are_refused_with_what_to_do(tmp_path) -> None:
    for schema in ("reya.workflow/2", "reya.workflow/1"):
        path = tmp_path / "old.json"
        path.write_text(json.dumps({"schema": schema, "envelope": {}}))
        with pytest.raises(wf.WorkflowError, match="previous version of strategies") as refused:
            wf.load_document(str(path))
        assert refused.value.code == "PREVIOUS_VERSION" and "strategy draft" in str(refused.value)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('{"schema":"reya.workflow/3","schema":"x"}', "repeats the key"),
        ('{"schema":"reya.workflow/3","x":1.5}', "decimals are strings"),
        ('{"schema":"reya.workflow/3","x":9007199254740992}', "2^53"),
        ("[]", "JSON object"),
    ],
)
def test_a_document_is_read_as_strictly_as_the_service_reads_it(text: str, error: str) -> None:
    with pytest.raises(wf.WorkflowError, match=re.escape(error)):
        wf.parse_document(text)


def test_a_file_spelled_another_way_is_sent_in_its_canonical_form(tmp_path) -> None:
    path = tmp_path / "pretty.json"
    path.write_text(json.dumps(json.loads(EXAMPLES["e1"]), indent=2))
    text, doc = wf.load_document(str(path))
    assert text == EXAMPLES["e1"] and doc == json.loads(EXAMPLES["e1"])


def test_a_version_hash_the_kit_cannot_reproduce_is_never_signed() -> None:
    text = EXAMPLES["e1"]
    assert wf.check_version_hash(text, wf.version_hash(text).upper().replace("0X", "0x")) == wf.version_hash(text)
    for claimed in ("0x" + "00" * 32, None, "not a hash"):
        with pytest.raises(wf.WorkflowError, match="nothing was signed"):
            wf.check_version_hash(text, claimed)


def test_expiry_is_an_hour_to_365_days_ahead() -> None:
    now = 1_800_000_000
    assert wf.parse_expiry("7d", now) == now + 7 * 86400
    assert wf.parse_expiry("2026-12-01T00:00:00Z", 1_795_000_000) == 1_796_083_200
    for text, error in (("30m", "at least an hour"), ("400d", "at most 365"), ("2026-12-01T00:00:00", "time zone")):
        with pytest.raises(wf.WorkflowError, match=error):
            wf.parse_expiry(text, 1_795_000_000)


# --------------------------------------------------------------------------
# The Agent account and the agent key
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
    doc = tmp_path / "e1.json"
    doc.write_text(EXAMPLES["e1"])
    for argv in (
        ["start", "--file", str(doc), "--account-id", "999"],
        ["stop", "3000001", "--account-id", "999"],
        ["status", "--account-id", "999"],
    ):
        monkeypatch.setattr(sys, "argv", ["strategy", *argv])
        with pytest.raises(SystemExit) as exited:
            workflow.main()
        assert exited.value.code == 1
        out = capsys.readouterr()
        assert "Agent account" in json.loads(out.out)["error"]
        assert KEYS["agent"][2:] not in out.out + out.err


def test_strategy_is_the_command_and_workflow_its_alias(capsys, monkeypatch) -> None:
    assert strategy.main is workflow.main, "both module names run the same commands"
    assert workflow.build_parser().prog == "strategy"
    assert workflow.build_parser("workflow").prog == "workflow"

    async def broken(args):
        raise RuntimeError("a request body")

    monkeypatch.setattr(workflow, "run", broken)
    monkeypatch.setattr(workflow, "asyncio", SimpleNamespace(run=own_loop_run))
    for prog in ("strategy", "workflow"):
        monkeypatch.setattr(sys, "argv", [prog, "status"])
        with pytest.raises(SystemExit):
            workflow.main(prog)
        assert parse_stdout(capsys)["error"] == f"RuntimeError: {prog} status failed", "never the exception's text"


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


async def drafted(tmp_path: Path, *flags: str, name: str = "draft.json") -> str:
    result = await call("draft", *(flags or E1_FLAGS), "--out", str(tmp_path / name))
    assert result.get("file") == str(tmp_path / name), result
    return result["file"]


def run_page(run_id: str, app: str = DEVNET_APP) -> str:
    return f"{app}/agents/strategies?id={run_id}#access={agent.SETUP_PAGE_ACCESS_CODE}"


def runs_page(app: str = DEVNET_APP) -> str:
    return f"{app}/agents/accounts?account={ACCOUNT}#access={agent.SETUP_PAGE_ACCESS_CODE}"


def enable_page(app: str = DEVNET_APP, *, account: bool = True) -> str:
    """agent init's setup link once the Agent account is recorded; the page's last step enables hosted runs."""
    recorded = f"&account={ACCOUNT}" if account else ""
    return (
        f"{app}/agents/setup?agent={ADDRESSES['agent']}&owner={ADDRESSES['owner']}{recorded}"
        f"#access={agent.SETUP_PAGE_ACCESS_CODE}"
    )


def assert_code_only_in_fragment(url: str) -> None:
    parts = urlsplit(url)
    assert parts.fragment == f"access={agent.SETUP_PAGE_ACCESS_CODE}"
    assert agent.SETUP_PAGE_ACCESS_CODE not in parts.scheme + parts.netloc + parts.path + parts.query


def recovered(primary: str, request: dict, signature: str) -> str:
    """The signer, recovered with eth_account from the service's own types, never with the kit's code."""
    types = {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}]}
    types[primary] = (START["types"] | STOP["types"])[primary]
    if primary == "StrategyStart":
        types["HandoverStop"] = START["types"]["HandoverStop"]
    values = {
        f["name"]: (
            int(request[f["name"]])
            if f["type"].startswith("uint")
            else bytes.fromhex(request[f["name"]][2:]) if f["type"] == "bytes32" else request[f["name"]]
        )
        for f in types[primary]
    }
    message = encode_typed_data(
        full_message={"types": types, "primaryType": primary, "domain": START["domain"], "message": values}
    )
    return Account.recover_message(message, signature=bytes.fromhex(signature[2:]))


async def test_draft_writes_the_document_the_service_checked(service, tmp_path, monkeypatch) -> None:
    result = await call("draft", *E1_FLAGS, "--out", str(tmp_path / "d.json"))
    assert (tmp_path / "d.json").read_text() == EXAMPLES["e1"]
    assert result["version_hash"] == wf.version_hash(EXAMPLES["e1"]) and result["verdict"] == "pass"
    assert result["account_id"] == ACCOUNT and result["markets"] == ["ETHRUSDPERP"]
    assert result["figures"]["margin_needed"] == "at most $6.23"
    assert result["next"].startswith(f"strategy preview --file {tmp_path / 'd.json'}")
    [(method, path, body)] = service.requests[1:]
    assert (method, path) == ("POST", "/v1/validate")
    assert body == {"document": EXAMPLES["e1"], "mode": "draft", "account_id": str(ACCOUNT)}
    # Without an Agent account recorded, the draft is checked without one, and needs no setup.
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", "")
    service.requests.clear()
    service.authorizations.clear()
    alone = await call("draft", *E1_FLAGS, "--out", str(tmp_path / "d.json"), "--force")
    assert service.requests[-1][2] == {"document": EXAMPLES["e1"], "mode": "draft"}
    assert alone["account_id"] is None and alone["warnings"][0]["code"] == "analysis_incomplete"
    assert all(a is None for _, _, a in service.authorizations), "a check without an account carries no grant"


async def test_draft_writes_nothing_the_service_refuses(service, tmp_path) -> None:
    service.issues = [
        {"code": "market_unknown", "outcome": "refusal", "message": "no market XYZ", "path": "actions.a_3"}
    ]
    result = await call("draft", *E1_FLAGS, "--out", str(tmp_path / "d.json"))
    assert result["ok"] is False and "no market XYZ (actions.a_3)" in result["error"]
    assert "Nothing was written" in result["error"] and not (tmp_path / "d.json").exists()
    assert result["refusals"][0]["code"] == "market_unknown"


async def test_draft_refuses_a_hash_it_cannot_reproduce(service, tmp_path) -> None:
    service.hash_override = "0x" + "11" * 32
    result = await call("draft", *E1_FLAGS, "--out", str(tmp_path / "d.json"))
    assert "nothing was signed" in result["error"] and not (tmp_path / "d.json").exists()


async def test_validate_checks_a_document_in_each_mode(service, tmp_path) -> None:
    path = tmp_path / "e3.json"
    path.write_text(json.dumps(json.loads(EXAMPLES["e3"]), indent=1))
    draft = await call("validate", "--file", str(path))
    assert draft["ok"] is True and draft["check_mode"] == "draft" and "preview_id" not in draft
    assert draft["next"] == f"strategy preview --file {path}"
    paper = await call("validate", "--file", str(path), "--mode", "paper", "--paper-balance", "500")
    assert paper["check_mode"] == "paper" and "preview_id" not in paper, "validate keeps no preview record"
    sent = service.requests[-1][2]
    assert sent["document"] == EXAMPLES["e3"] and sent["paper"] == {"balance": "500"}
    assert sent["fire_now"] is False and int(sent["expires_at"]) > time.time() + 6 * 86400
    refused = await call("validate", "--file", str(path), "--mode", "live", "--paper-balance", "500")
    assert "for a Paper run" in refused["error"]


async def test_preview_records_a_preview_and_names_the_start_with_the_same_terms(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    result = await call("preview", "--file", path, "--expires", "3d", "--label", "DCA ETH", "--paper-start", "empty")
    assert result["ok"] is True and result["mode"] == "paper" and result["preview_id"] == "101"
    assert result["strategy_id"] == "101" and result["valid_until_iso"]
    assert result["paper"]["margin"] == {"source": "account_balance", "amount": "200"}
    assert result["next"].startswith("After the user confirms")
    assert f"strategy start --file {path} --expires 3d --label 'DCA ETH' --paper-start empty" in result["next"]
    sent = service.requests[-1][2]
    assert sent["label"] == "DCA ETH" and sent["paper"] == {"start": "empty"} and sent["mode"] == "paper"
    assert not service.writes()


async def test_a_preview_that_refuses_says_so_and_names_no_start(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.issues = [{"code": "first_firing_margin_short", "outcome": "refusal", "message": "short of margin"}]
    result = await call("preview", "--file", path, "--mode", "live")
    assert result["ok"] is False and "A Live start is refused now: short of margin" in result["error"]
    assert "strategy start" not in result["next"] and "preview_id" not in result


async def test_start_previews_then_signs_that_preview_with_the_agent_key(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.requests.clear()
    result = await call("start", "--file", path, "--label", "DCA ETH")
    assert result["started"] is True and result["run_id"] == "3000007" and result["mode"] == "paper"
    assert result["strategy_id"] == "101" and result["preview_id"] == "101"
    assert result["signer"] == ADDRESSES["agent"] and result["hosted_signer"] == ADDRESSES["hosted"]
    assert result["app_run_url"] == run_page("3000007") and result["app_runs_url"] == runs_page()
    assert result["next"] == "strategy status 3000007 (the service runs it; this computer can be off)"
    kinds = [(m, p) for m, p, _ in service.requests]
    assert kinds == [("GET", "/v1/info"), ("POST", "/v1/preview"), ("POST", "/v1/runs")]
    body = service.requests[-1][2]
    assert list(body) == ["document", "request", "signature"] and body["document"] == EXAMPLES["e1"]
    request = body["request"]
    assert request["previewId"] == "101" and request["mode"] == "paper" and request["fireNow"] is False
    assert request["accountId"] == str(ACCOUNT) and request["versionHash"] == wf.version_hash(EXAMPLES["e1"])
    assert request["expiresAt"] == service.previews["101"]["expires_at"]
    assert recovered("StrategyStart", request, body["signature"]) == ADDRESSES["agent"]
    assert service.runs["3000007"]["label"] == "DCA ETH", "the label travels on the preview, not the Start"


async def test_start_signs_nothing_while_its_preview_refuses(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.issues = [{"code": "first_firing_margin_short", "outcome": "refusal", "message": "short of margin"}]
    result = await call("start", "--file", path)
    assert result["error_code"] == "START_BLOCKED" and "Nothing was signed" in result["error"]
    assert "short of margin" in result["error"] and "strategy preview --file" in result["next"]
    assert not service.writes()


async def test_start_in_a_mode_the_service_does_not_start_now_signs_nothing(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.live_enabled = False
    result = await call("start", "--file", path, "--mode", "live")
    assert result["error_code"] == "MODE_NOT_ALLOWED" and "not Live" in result["error"]
    assert not service.writes() and not [r for r in service.requests if r[1] == "/v1/preview"]


async def test_a_lost_start_answer_is_confirmed_by_one_read_and_never_resent(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.next_write = "lose"
    service.requests.clear()
    result = await call("start", "--file", path)
    assert result["started"] is True and result["confirmed_by_read"] is True and result["run_id"] == "3000007"
    assert len(service.writes()) == 1, "never resent"
    assert len(service.reads(r"/v1/accounts/[0-9]+/runs")) == 1, "one read"


async def test_a_start_whose_answer_never_came_says_so_and_resends_nothing(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.next_write = "drop"
    service.requests.clear()
    result = await call("start", "--file", path)
    assert result["outcome"] == "unknown" and "nothing is resent automatically" in result["error"]
    assert result["strategy_id"] == "101" and f"strategy status --all --account-id {ACCOUNT}" in result["next"]
    assert len(service.writes()) == 1 and len(service.reads(r"/v1/accounts/[0-9]+/runs")) == 1
    assert service.runs == {}


async def test_an_account_runs_one_live_strategy(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    first = await call("start", "--file", path, "--mode", "live")
    assert first["mode"] == "live" and first["mode_text"].startswith("Live")
    refused = await call("start", "--file", path, "--mode", "live")
    assert refused["error_code"] == "ACCOUNT_STRATEGY_LIMIT"
    assert f"already runs Live strategy #{first['run_id']}" in refused["next"]
    assert refused["app_run_url"] == run_page(first["run_id"])
    assert (await call("start", "--file", path))["mode"] == "paper", "Paper ones run beside it"


@pytest.mark.parametrize(
    ("code", "details", "hint"),
    [
        ("START_REFUSED", {"reason": "card_expired"}, "previews afresh"),
        ("START_REFUSED", {"reason": "start_checks_refused", "issues": ["x"]}, "strategy preview shows why"),
        ("NOT_WHITELISTED", {}, "not whitelisted"),
        ("INVALID_REQUEST", {"reason": "resting_limits_unavailable"}, "immediate orders only"),
        ("NONCE_TOO_LOW", {}, "fresh nonce"),
    ],
)
async def test_a_refused_start_says_what_to_do(service, tmp_path, code: str, details: dict, hint: str) -> None:
    path = await drafted(tmp_path)
    service.next_refusal = (code, details)
    result = await call("start", "--file", path)
    assert result["error_code"] == code and hint in result["next"]
    assert service.runs == {}


async def test_start_and_stop_refuse_the_owners_key(service, tmp_path, monkeypatch) -> None:
    path = await drafted(tmp_path)
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", KEYS["owner"])
    for argv in (["start", "--file", path], ["stop", "3000007"]):
        assert "never with your own key" in (await call(*argv))["error"]
    assert not service.writes()


async def test_a_preview_for_an_account_that_is_not_an_agent_account_names_the_fix(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.agent_accounts.clear()
    result = await call("preview", "--file", path)
    assert result["error_code"] == "NOT_AGENT_ACCOUNT" and "Nothing was signed or started" in result["next"]
    assert not [r for r in service.requests if r[1] == "/v1/preview"], "checked before the service is asked"


async def test_stop_signs_the_runs_strategy_and_closes_its_positions(service, tmp_path) -> None:
    run_id = (await call("start", "--file", await drafted(tmp_path)))["run_id"]
    service.requests.clear()
    result = await call("stop", run_id)
    assert result["state"] == "stopped" and result["status"] == "ended" and result["end_reason"] == "stop_close"
    assert result["keep_position"] is False and "closes the strategy's positions" in result["note"]
    [(_, path, body)] = service.writes()
    assert path == f"/v1/runs/{run_id}/stop" and list(body) == ["request", "signature"]
    assert body["request"]["strategyId"] == "101" and body["request"]["runId"] == run_id
    assert recovered("StrategyStop", body["request"], body["signature"]) == ADDRESSES["agent"]
    again = await call("stop", run_id)
    assert again["already_ended"] is True and len(service.writes()) == 1, "nothing signed for an ended strategy"
    closed = await call("status", run_id)
    assert [p["qty"] for p in closed["positions"]] == ["0"], "after its close it holds nothing"


async def test_a_paper_strategy_stops_only_by_closing(service, tmp_path) -> None:
    run_id = (await call("start", "--file", await drafted(tmp_path)))["run_id"]
    result = await call("stop", run_id, "--keep-position")
    assert result["error_code"] == "PAPER_KEEP_REFUSED" and "Nothing was signed" in result["error"]
    assert not service.writes("/v1/runs/")
    live = (await call("start", "--file", await drafted(tmp_path, name="l.json"), "--mode", "live"))["run_id"]
    kept = await call("stop", live, "--keep-position")
    assert kept["end_reason"] == "stop_keep" and "stay open as yours" in kept["note"]


async def test_a_lost_stop_answer_is_confirmed_by_one_read_and_never_resent(service, tmp_path) -> None:
    run_id = (await call("start", "--file", await drafted(tmp_path)))["run_id"]
    service.next_write = "lose"
    service.requests.clear()
    result = await call("stop", run_id)
    assert result["confirmed_by_read"] is True and result["state"] == "stopped"
    assert len(service.writes("/v1/runs/")) == 1
    # One read of the run before signing, and one after the lost answer.
    run_path, stop_path = f"/v1/runs/{run_id}", f"/v1/runs/{run_id}/stop"
    assert [(m, p) for m, p, _ in service.requests if p in (run_path, stop_path)] == [
        ("GET", run_path),
        ("POST", stop_path),
        ("GET", run_path),
    ]
    service.runs[run_id].update(state="active", status="running")
    service.next_write = "drop"
    service.requests.clear()
    unknown = await call("stop", run_id)
    assert unknown["outcome"] == "unknown" and "nothing is resent automatically" in unknown["error"]
    assert len(service.writes("/v1/runs/")) == 1 and len(service.reads(rf"/v1/runs/{run_id}")) == 2


async def test_stop_all_stops_every_running_strategy(service, tmp_path) -> None:
    first = (await call("start", "--file", await drafted(tmp_path)))["run_id"]
    second = (await call("start", "--file", await drafted(tmp_path, name="b.json")))["run_id"]
    result = await call("stop", "--all")
    assert sorted(r["run_id"] for r in result["stopped"]) == sorted([first, second])
    assert result["note"] == "Stopped 2 of 2 running strategies."
    assert (await call("stop", "--all"))["note"] == "No strategy is running on this account: nothing was signed."


async def test_status_reads_a_run_with_its_detail_under_the_grant(service, tmp_path) -> None:
    run_id = (await call("start", "--file", await drafted(tmp_path), "--label", "DCA ETH"))["run_id"]
    status = await call("status", run_id)
    assert status["text"] == f"#{run_id} DCA ETH: Paper, running, ETH" and status["state"] == "active"
    assert status["positions"][0]["qty"] == "0.01" and status["result"]["total"] == "-0.004"
    assert status["rules"][0] == "Starts in st_1 (a loop of at most 10 rounds, then st_7)."
    assert status["activity"][0]["code"] == "start" and status["app_run_url"] == run_page(run_id)
    listing = await call("status")
    assert [s["run_id"] for s in listing["strategies"]] == [run_id]
    assert listing["running_strategy"]["run_id"] == run_id and listing["live_strategy"] is None
    await call("stop", run_id)
    assert (await call("status"))["strategies"] == []
    assert [s["state"] for s in (await call("status", "--all"))["strategies"]] == ["stopped"]


async def test_status_pages_the_history_with_the_services_cursor(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    runs = [(await call("start", "--file", path))["run_id"] for _ in range(3)]
    page = await call("status", "--all", "--limit", "2")
    assert [s["run_id"] for s in page["strategies"]] == runs[::-1][:2] and page["next_before"] == runs[1]
    rest = await call("status", "--all", "--limit", "2", "--before", page["next_before"])
    assert [s["run_id"] for s in rest["strategies"]] == [runs[0]] and "next_before" not in rest


async def test_a_run_of_the_previous_version_says_so(service) -> None:
    result = await call("status", "42")
    assert result["error_code"] == "RUN_ON_PREVIOUS_VERSION" and "previous version" in result["next"]


async def test_a_run_read_without_a_verified_grant_says_why_it_has_no_detail(service, tmp_path, monkeypatch) -> None:
    run_id = (await call("start", "--file", await drafted(tmp_path)))["run_id"]
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", "")  # no grant can be made
    monkeypatch.setattr(wf, "agent_account", lambda requested=None: ACCOUNT)
    status = await call("status", run_id)
    assert status["detail"] is None and status["detail_reason"] == "read_grant_required"
    assert "read grant" in status["note"]


# --------------------------------------------------------------------------
# Enable hosted runs
# --------------------------------------------------------------------------


async def test_info_links_the_enable_hosted_runs_step_until_the_owner_approves(service, opened) -> None:
    service.hosted_enabled.clear()
    info = await call("info")
    assert info["hosted_signer"] == ADDRESSES["hosted"] and info["hosted_signer_enabled"] is False
    assert info["enable_url"] == enable_page() and info["modes"] == ["paper", "live"]
    assert_code_only_in_fragment(info["enable_url"])
    assert "Give the user enable_url" in info["next"] and ADDRESSES["owner"] in info["next"]
    assert service.trading_reads == [WALLET_STATUS, OWNER_ACCOUNTS]
    assert opened == [] and not service.writes()
    service.enable_hosted(ADDRESSES["owner"])
    enabled = await call("info")
    assert enabled["hosted_signer_enabled"] is True and "enable_url" not in enabled and "next" not in enabled


async def test_info_still_answers_when_hosted_runs_cannot_be_checked(service, monkeypatch) -> None:
    service.trading_failure = aiohttp.ClientConnectionError("connection refused")
    info = await call("info")
    assert info["hosted_signer_enabled"] is None and "enable_url" not in info
    assert info["hosted_signer_note"] == "Could not read the owner's approvals: ClientConnectionError"
    service.trading_failure = None
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", "")
    assert (await call("info"))["hosted_signer_enabled"] is None


async def test_a_start_refused_until_hosted_runs_are_enabled_opens_that_step_once(service, tmp_path, opened) -> None:
    path = await drafted(tmp_path)
    service.hosted_enabled.clear()
    refused = await call("start", "--file", path)
    assert refused["error_code"] == "HOSTED_SIGNER_NOT_ENABLED" and refused["hosted_signer"] == ADDRESSES["hosted"]
    assert refused["enable_url"] == enable_page() and refused["opened"] is True and opened == [enable_page()]
    assert refused["next"].startswith("Nothing was started.")
    again = await call("start", "--file", path)
    assert again["link_already_opened"] is True and opened == [enable_page()]
    service.enable_hosted(ADDRESSES["owner"])
    assert (await call("start", "--file", path))["started"] is True


async def test_agent_setup_reads_the_active_hosted_signer_from_the_v3_info(service, monkeypatch) -> None:
    """agent init's optional Enable hosted runs step: the signer comes from the service's /v1/info, and any
    trouble reading it means no offer, never a failed setup."""
    assert await agent.hosted_signer() == ADDRESSES["hosted"]
    service.info_override = {"environment": "mainnet", "chain_id": "1729"}
    assert await agent.hosted_signer() is None
    service.info_override = service.info_body() | {"hosted_signers": []}
    assert await agent.hosted_signer() is None, "no active signer: nothing to enable"
    service.info_override = None
    await service.close()
    assert await agent.hosted_signer() is None, "an unreachable service offers nothing"


async def test_a_slow_workflow_service_never_holds_agent_setup_up(service, monkeypatch) -> None:
    async def slow_info(request):
        await asyncio.sleep(5)
        return web.json_response(service.info_body())

    monkeypatch.setattr(service, "info", slow_info)
    await service.close()
    await service.start_server()
    monkeypatch.setenv("REYA_WORKFLOWS_URL", service.url)
    monkeypatch.setattr(agent, "HOSTED_RUNS_INFO_TIMEOUT_S", 0.2)
    began = time.monotonic()
    assert await agent.hosted_signer() is None
    assert time.monotonic() - began < 2, "the optional read gives up at its timeout"


# --------------------------------------------------------------------------
# Read grants
# --------------------------------------------------------------------------


def decode_token(header: str) -> dict:
    assert header.startswith("Bearer ")
    token = header.removeprefix("Bearer ")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token), "base64url without padding"
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def test_the_header_is_bearer_base64url_json_of_the_grant_and_its_signature(kit_env) -> None:
    header = wf.ReadGrants(now=lambda: 1_800_000_000).header()
    assert header is not None and KEYS["agent"][2:] not in header
    body = decode_token(header)
    grant = body["grant"]
    assert list(grant) == [f["name"] for f in GRANTS["types"]["ReadGrant"]]
    assert grant["environment"] == "devnet1" and grant["accountIds"] == [str(ACCOUNT)]
    assert grant["expiresAt"] == str(1_800_000_000 + 3600)
    assert wf.parse_read_grant_token(header.removeprefix("Bearer "))[2] == ADDRESSES["agent"]


def test_a_grant_is_kept_for_the_next_command_and_replaced_at_80_percent_of_its_life(kit_env) -> None:
    start = 1_800_000_000
    clock = [float(start)]

    def header() -> str | None:
        return wf.ReadGrants(now=lambda: clock[0]).header()

    first = header()
    assert first is not None and header() == first
    assert (wf.state_directory() / wf.READ_GRANT_FILE).stat().st_mode & 0o777 == 0o600
    clock[0] = start + 0.81 * 3600
    second = header()
    assert second is not None and second != first


def test_no_grant_when_the_kit_cannot_sign_one_as_a_delegate(kit_env, monkeypatch) -> None:
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", KEYS["owner"])
    assert wf.ReadGrants().header() is None, "never the owner's key"
    monkeypatch.delenv("REYA_SIGNER")
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", "")
    assert wf.ReadGrants().header() is None


def grant_headers(service: FakeWorkflowService) -> dict:
    seen: dict = {}
    for method, path, authorization in service.authorizations:
        kind = re.sub(r"/[0-9]+", "/{id}", path)
        seen.setdefault((method, kind), set()).add(authorization)
    return seen


async def test_run_reads_and_account_checks_carry_the_agent_keys_grant_and_nothing_else_does(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    run_id = (await call("start", "--file", path))["run_id"]
    await call("status", run_id)
    await call("status")
    await call("stop", run_id)
    await call("info")
    seen = grant_headers(service)
    carrying = {
        ("GET", "/v1/runs/{id}"),
        ("GET", "/v1/runs/{id}/activity"),
        ("GET", "/v1/accounts/{id}/runs"),
        ("POST", "/v1/validate"),
        ("POST", "/v1/preview"),
    }
    assert carrying <= set(seen)
    tokens = set().union(*(seen[kind] for kind in carrying))
    assert len(tokens) == 1 and None not in tokens, "one grant, kept across the commands"
    assert all(seen[kind] == {None} for kind in set(seen) - carrying), "the signed writes and info carry none"
    assert set(service.grant_signers) == {ADDRESSES["agent"]}


async def test_a_gateway_that_refuses_the_header_still_answers(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    run_id = (await call("start", "--file", path))["run_id"]
    service.read_grants = "refuse"
    service.authorizations.clear()
    status = await call("status", run_id)
    assert status["run_id"] == run_id and "error" not in status
    # A refusal without the service's error envelope: the read went once with the grant and once more without it,
    # and the rest of the command sends the dropped grant no more.
    assert [(p, a is not None) for _, p, a in service.authorizations] == [
        (f"/v1/runs/{run_id}", True),
        (f"/v1/runs/{run_id}", False),
        (f"/v1/runs/{run_id}/activity", False),
    ]
    assert not (wf.state_directory() / wf.READ_GRANT_FILE).exists(), "a refused grant is not kept"


async def test_a_grant_the_service_refuses_is_not_retried_without_it_and_says_what_to_do(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    run_id = (await call("start", "--file", path))["run_id"]
    service.read_grants = "require"
    service.delegates[ADDRESSES["owner"].lower()].discard(ADDRESSES["agent"].lower())  # the owner revoked it
    service.authorizations.clear()
    result = await call("status", run_id)
    assert result["error_code"] == "INVALID_READ_GRANT" and result["details"] == {"reason": "not_delegate"}
    assert "re-approve the agent key" in result["next"]
    assert [a is not None for _, _, a in service.authorizations] == [True], "never asked again without it"


async def test_without_a_grant_an_enforcing_service_says_what_to_do(service, tmp_path, monkeypatch) -> None:
    path = await drafted(tmp_path)
    run_id = (await call("start", "--file", path))["run_id"]
    service.read_grants = "require"
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", KEYS["owner"])
    result = await call("status", run_id)
    assert result["error_code"] == "READ_GRANT_REQUIRED" and "agent status" in result["next"]


# --------------------------------------------------------------------------
# Review follow-ups
# --------------------------------------------------------------------------


async def test_a_lost_stop_whose_read_back_also_fails_stays_unknown(service, tmp_path, monkeypatch) -> None:
    run_id = (await call("start", "--file", await drafted(tmp_path)))["run_id"]
    real_run = wf.WorkflowClient.run
    reads = []

    async def flaky(self, run):
        reads.append(run)
        if len(reads) > 1:
            raise wf.WorkflowError("The strategy service did not answer")
        return await real_run(self, run)

    monkeypatch.setattr(wf.WorkflowClient, "run", flaky)
    service.next_write = "lose"
    result = await call("stop", run_id)
    assert result["outcome"] == "unknown" and "confirmed_by_read" not in result
    assert len(service.writes("/v1/runs/")) == 1, "never resent"


async def test_stop_all_says_so_at_the_top_when_one_is_not_stopped(service, tmp_path) -> None:
    await call("start", "--file", await drafted(tmp_path))
    result = await call("stop", "--all", "--keep-position")
    assert result["error_code"] == "STOP_INCOMPLETE" and "1 of 1 running strategy was not stopped" in result["error"]
    assert result["stopped"][0]["error_code"] == "PAPER_KEEP_REFUSED"


async def test_a_listing_cut_short_never_says_nothing_is_running(service, tmp_path, monkeypatch) -> None:
    path = await drafted(tmp_path)
    for _ in range(3):
        await call("start", "--file", path)
    first = sorted(service.runs)[0]
    await call("stop", sorted(service.runs)[-1])
    await call("stop", sorted(service.runs)[-2])
    monkeypatch.setattr(workflow, "PAGE_RUNS", 1)
    monkeypatch.setattr(workflow, "MAX_RUN_PAGES", 2)
    listing = await call("status")
    assert listing["listing_truncated"] is True and "only the newest 2 were checked" in listing["next"]
    stopped = await call("stop", "--all")
    assert (
        stopped["error_code"] == "LISTING_TRUNCATED" and "No strategy is running among the newest 2" in stopped["note"]
    )
    assert service.runs[first]["state"] == "active", "the unread one is untouched and reported as unchecked"


def test_a_discarded_grant_is_not_sent_again_by_the_same_command(kit_env) -> None:
    grants = wf.ReadGrants(now=lambda: 1_800_000_000)
    assert grants.header() is not None
    grants.discard()
    assert grants.header() is None


async def test_a_preview_the_gateway_refused_with_the_grant_goes_on_without_it(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.read_grants = "refuse"
    service.authorizations.clear()
    assert (await call("start", "--file", path))["started"] is True
    sent = [(p, a is not None) for m, p, a in service.authorizations if m == "POST"]
    assert sent == [("/v1/preview", True), ("/v1/preview", False), ("/v1/runs", False)]


@pytest.mark.parametrize(
    ("text", "error"),
    [("1.00000000000000000000000000001", "more than 18 decimal places"), ("1e999999", "too large")],
)
def test_an_amount_is_checked_exactly_and_bounded(text: str, error: str) -> None:
    with pytest.raises(wf.WorkflowError, match=error):
        wf.positive_amount(text, "--order")
    assert wf.positive_amount("1.000", "x") == "1" and wf.positive_amount("1e2", "x") == "100"


def test_nan_is_not_json_and_never_canonical() -> None:
    with pytest.raises(wf.WorkflowError, match="NaN, which is not JSON"):
        wf.parse_document('{"schema":"reya.workflow/3","x":NaN}')
    with pytest.raises(ValueError):
        wf.canonical_json({"x": float("nan")})


def test_one_round_at_the_start_adds_no_timed_rule_to_the_count() -> None:
    orders = [wf.parse_order(f"{m}:buy:100") for m in ("eth", "btc", "sol")]
    every = wf.parse_trigger("every:1d")
    fits = wf.basket(orders=orders, trigger=every, rounds=1, first_now=True, stop_pct="5", take_profit_pct="5")
    doc = wf.basket_document(fits)
    assert max(len(s.get("on", [])) for s in doc["states"].values()) == 6
    with pytest.raises(wf.WorkflowError, match="make 7 rules"):
        wf.basket(orders=orders, trigger=every, rounds=2, first_now=True, stop_pct="5", take_profit_pct="5")


def test_the_rules_keep_an_expressions_grouping() -> None:
    def above(market: str, level: str) -> dict:
        mark = {"kind": "series", "market": market, "series": "mark"}
        return {"kind": "op", "op": "gt", "args": [mark, {"kind": "num", "value": level}]}

    either = {"kind": "op", "op": "or", "args": [above("ETHRUSDPERP", "1"), above("BTCRUSDPERP", "2")]}
    both = {"kind": "op", "op": "and", "args": [either, above("SOLRUSDPERP", "3")]}
    assert wf.expr_text(both) == "((ETH mark price > 1) or (BTC mark price > 2)) and (SOL mark price > 3)"


async def test_a_malformed_document_still_shows_the_services_refusal(service, tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"schema": "reya.workflow/3", "start": "st_1", "states": {"st_1": {"kind": "normal", "on": 1}}})
    )
    service.issues = [
        {"code": "one_of_violated", "outcome": "refusal", "message": "on must be a list", "path": "states.st_1.on"}
    ]
    result = await call("validate", "--file", str(path))
    assert result["ok"] is False and "on must be a list (states.st_1.on)" in result["error"]
    assert result["rules"] == ["Starts in st_1."]


async def test_the_paper_terms_asked_for_are_the_ones_the_preview_and_the_run_carry(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    terms = ("--paper-balance", "500", "--paper-start", "empty", "--paper-fee-tier", "vip1")
    preview = await call("preview", "--file", path, *terms)
    assert preview["paper"]["margin"] == {"source": "paper_balance", "amount": "500"}
    assert preview["paper"]["fee_tier"] == "vip1" and preview["paper"]["start"] == "empty"
    run_id = (await call("start", "--file", path, *terms))["run_id"]
    paper = (await call("status", run_id))["paper"]
    assert paper["settings"]["margin"]["amount"] == "500" and paper["settings"]["fee_tier"] == "vip1"
    assert paper["balance"] == "500", "the run's book starts from the balance asked for"
    plain = (await call("start", "--file", path))["run_id"]
    assert (await call("status", plain))["paper"]["balance"] == "200", "without one, the account's balance"
