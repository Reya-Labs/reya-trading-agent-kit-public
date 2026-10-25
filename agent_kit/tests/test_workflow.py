"""Strategy commands: request signing against the service's vectors, the basket template's
vectors, the Agent-account guard, named reasons and sentences in the output, and the command
flow against a local fake of the service.

The vectors in fixtures/workflow_requests are the service's own signed-request vectors,
copied unchanged. fixtures/workflow_v2_examples holds the basket template's examples, copied
unchanged from the service's core: each holds the parameters, the document they expand to,
its canonical JSON and its hash.
"""

from types import SimpleNamespace
from typing import Any

import asyncio
import base64
import copy
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
from agent_kit.scripts import agent, strategy, workflow
from agent_kit.scripts.workflow import available_balance as real_available_balance
from agent_kit.tests.conftest import parse_stdout
from agent_kit.tests.workflow_fake_service import ENVELOPE_PARAMS, FakeWorkflowService

FIXTURES = Path(__file__).parent / "fixtures"
START = json.loads((FIXTURES / "workflow_requests" / "start.json").read_text())
STOP = json.loads((FIXTURES / "workflow_requests" / "stop.json").read_text())
EXAMPLES = [json.loads(path.read_text()) for path in sorted((FIXTURES / "workflow_v2_examples").glob("*.json"))]
CASES = {case["name"]: case for case in EXAMPLES}
KEYS = {name: entry["private_key"] for name, entry in START["keys"].items()}
ADDRESSES = {name: entry["address"] for name, entry in START["keys"].items()}
CHAINS = {"devnet1": 89346162, "testnet": 89346162, "mainnet": 1729}
ACCOUNT = 123456
DEVNET_API = "https://api-devnet.reya-cronos.network/v2"
# Captured at import, before the suite's offline guard replaces it for each test.
REAL_CONNECT = socket.socket.connect

# The draft flags each example's parameters come from (the envelope's values aside: see drafted()).
FLAGS = {
    "basket_at_price": [
        *("--trigger", "price:eth:up:3000", "--order", "eth:sell:1000:3300"),
        *("--order", "btc:buy:500:80000", "--order", "sol:buy:500"),
    ],
    "basket_at_start": [
        *("--trigger", "start", "--order", "btc:buy:500:80000", "--order", "sol:buy:500"),
        *("--resting", "eth:sell:1000:3000:3300"),
    ],
    "weekly_dca": [
        *("--trigger", "every:1w@2026-10-26T09:00:00Z", "--order", "btc:buy:50", "--order", "eth:buy:50"),
        *("--repeat", "each"),
    ],
    "three_exits": [
        *("--trigger", "price:eth:down:2800", "--order", "eth:buy:1000:2500"),
        *("--exit", "target:eth:up:3200", "--exit", "profit:10", "--exit", "after:3d"),
    ],
    "weighted_basket": [
        *("--trigger", "at:daily@14:00", "--if", "funding:btc:below:0.001", "--order", "btc:buy:50%:80000"),
        *("--order", "eth:buy:30%:2500", "--order", "sol:buy:20%:120", "--total", "1000", "--repeat", "each"),
    ],
    "btc_exits": [
        *("--trigger", "price:btc:down:88000", "--order", "btc:buy:1000:80000"),
        *("--exit", "target:btc:up:96000", "--exit", "profit:5", "--exit", "after:2d"),
    ],
    "resting_offset_bid": [
        *("--trigger", "at:wed@15:00", "--resting", "eth:buy:100:-50bps", "--no-post-only", "--repeat", "each"),
    ],
}
assert set(FLAGS) == set(CASES), "every example has its flags"
READING_A, READING_B, WEEKLY_DCA, THREE_EXITS, WEIGHTED = (
    "basket_at_price",
    "basket_at_start",
    "weekly_dca",
    "three_exits",
    "weighted_basket",
)
# Strategies on separate markets (BTC only, ETH only), and a resting offset that may take liquidity.
EXITS, WEEKLY = "btc_exits", "resting_offset_bid"
DOC_A = json.loads(CASES[READING_A]["canonical"])


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
# Strategy documents: the basket template's vectors, the canonical form and the expansion check
# --------------------------------------------------------------------------


def without_defaults(doc: dict) -> dict:
    """The document as an author could write it: every default the canonical form fills in left out."""
    doc = copy.deepcopy(doc)
    for key, default in (("price_bound_bps", 25), ("max_resting_orders", 20)):
        if doc["envelope"].get(key) == default:
            del doc["envelope"][key]
    for state in doc["states"].values():
        for transition in state["on"]:
            if transition.get("if") == []:
                del transition["if"]
        if state["on"] == []:
            del state["on"]
    for action in doc["actions"].values():
        if action.get("keep") is False:
            del action["keep"]
        for order in action.get("orders") or []:
            for key, default in (("post_only", True), ("reduce_only", False)):
                if order.get(key) is default:
                    del order[key]
    return doc


@pytest.mark.parametrize("case", EXAMPLES, ids=ids(EXAMPLES))
def test_template_vectors_hash_and_read_back(case: dict) -> None:
    # The document the example lists is the canonical form, parsed.
    assert json.loads(case["canonical"]) == case["document"]
    assert wf.canonical_json(json.loads(case["canonical"])) == case["canonical"]
    assert wf.version_hash(case["canonical"]) == case["version_hash"]
    doc = wf.check_canonical(None, case["canonical"], case["version_hash"])
    # The expansion keeps the envelope, every order and the trigger the parameters asked for (the examples leave
    # the defaulted bound and resting cap out, as the kit does not).
    wf.check_expansion({"price_bound_bps": 25, "max_resting_orders": 20} | case["request"]["params"], doc)
    # The same document with its defaults left out (any key order) is what the canonical form says.
    wf.check_canonical(without_defaults(doc), case["canonical"], case["version_hash"])


def test_a_canonical_form_that_changes_the_document_is_never_signed() -> None:
    canonical = CASES[READING_A]["canonical"]
    tampered = canonical.replace('"level":"3000"', '"level":"3100"')
    with pytest.raises(wf.WorkflowError, match="changes signals.trigger.level"):
        wf.check_canonical(DOC_A, tampered, wf.version_hash(tampered))
    with pytest.raises(wf.WorkflowError, match="not the hash"):
        wf.check_canonical(DOC_A, canonical, "0x" + "00" * 32)
    # A default the author left out may be filled in, but only with the default.
    wider = canonical.replace('"price_bound_bps":25', '"price_bound_bps":100')
    with pytest.raises(wf.WorkflowError, match="envelope.price_bound_bps"):
        wf.check_canonical(without_defaults(DOC_A), wider, wf.version_hash(wider))
    # Nor may it add a stop the author did not write.
    stopped = canonical.replace('"size":{"usd":"500"}}', '"size":{"usd":"500"},"stop":"100"}')
    assert stopped != canonical
    with pytest.raises(wf.WorkflowError, match="changes actions.entry.orders.2.stop"):
        wf.check_canonical(DOC_A, stopped, wf.version_hash(stopped))


def changed(edit) -> dict:
    doc = copy.deepcopy(DOC_A)
    edit(doc)
    return doc


@pytest.mark.parametrize(
    ("edit", "where"),
    [
        (lambda d: d["actions"]["entry"]["orders"][0].update(size={"usd": "1001"}), "order o1"),
        (lambda d: d["actions"]["entry"]["orders"][1].update(stop=None), "order o2"),
        (lambda d: d["actions"]["entry"]["orders"][2].update(side="sell"), "order o3"),
        (lambda d: d["actions"]["entry"]["orders"].pop(), "the orders"),
        (lambda d: d["envelope"].update(allocation_usd="2001"), "envelope.allocation_usd"),
        (lambda d: d["envelope"].update(markets=["BTCRUSDPERP", "ETHRUSDPERP"]), "envelope.markets"),
        (lambda d: d["envelope"].update(account_id="999"), "envelope.account_id"),
        (lambda d: d["signals"]["trigger"].update(level="3100"), "the trigger"),
        (lambda d: d["signals"]["trigger"].update(direction="down"), "the trigger"),
        (lambda d: d["signals"]["trigger_mark"].update(market="BTCRUSDPERP"), "the trigger"),
        (lambda d: d.update(template={"id": "basket", "version": 2}), "the template"),
    ],
)
def test_an_expansion_that_changes_what_was_asked_is_never_written(edit, where) -> None:
    params = CASES[READING_A]["request"]["params"]
    with pytest.raises(wf.WorkflowError, match=f"changes {where} from what was asked"):
        wf.check_expansion(params, changed(edit))


def test_an_order_moved_to_another_action_kind_is_refused() -> None:
    params = CASES[READING_B]["request"]["params"]
    doc = json.loads(CASES[READING_B]["canonical"])
    doc["actions"]["entry"]["orders"].append(doc["actions"]["entry_resting"]["orders"].pop())
    with pytest.raises(wf.WorkflowError, match="changes order r1"):
        wf.check_expansion(params, doc)


def test_an_older_or_foreign_document_is_refused_with_the_way_forward() -> None:
    with pytest.raises(wf.WorkflowError, match="no longer runs. Draft it again with strategy draft"):
        wf.check_schema({"schema": "reya.workflow/1"})
    with pytest.raises(wf.WorkflowError, match="Not a strategy document"):
        wf.check_schema({"schema": "something/else"})
    v1 = '{"account_id":"1","schema":"reya.workflow/1"}'
    with pytest.raises(wf.WorkflowError, match="older single-market"):
        wf.check_canonical(None, v1, wf.version_hash(v1))


# --------------------------------------------------------------------------
# The document and the strategy in sentences, and named reasons
# --------------------------------------------------------------------------


def test_the_states_read_as_sentences_and_each_order_as_a_row() -> None:
    assert wf.rules(DOC_A) == [
        "In waiting (the start): When the ETH mark crosses up through 3,000: send 3 immediate orders (sell $1,000.00 "
        "of ETH at the mark within 25 bps, stop at 3,300; buy $500.00 of BTC at the mark within 25 bps, stop at "
        "80,000; buy $500.00 of SOL at the mark within 25 bps), then it moves to done, where it ends.",
        "done is final: the strategy ends there once none of its resting orders is left.",
        "If a stop-loss or take-profit fires, it closes its other legs and ends.",
    ]
    assert wf.describe(DOC_A).startswith(
        "May trade BTC, ETH and SOL with up to $2,000.00 in total until 2026-11-01T03:46:40Z, each immediate order "
        "within 25 bps of the mark. In waiting (the start): When the ETH mark crosses"
    )
    assert [(r["order"], r["type"], r["market"], r["side"], r["usd"], r["stop"]) for r in wf.order_rows(DOC_A)] == [
        ("o1", "immediate", "ETHRUSDPERP", "sell", "1000", "3300"),
        ("o2", "immediate", "BTCRUSDPERP", "buy", "500", "80000"),
        ("o3", "immediate", "SOLRUSDPERP", "buy", "500", None),
    ]


@pytest.mark.parametrize(
    ("case", "sentences", "orders"),
    [
        (
            READING_B,
            [
                "In waiting (the start): Right after the start: send 2 immediate orders (buy $500.00 of BTC at the mark "
                "within 25 bps, stop at 80,000; buy $500.00 of SOL at the mark within 25 bps); then place 1 resting "
                "order (sell $1,000.00 of ETH resting at 3,000, post-only, stop at 3,300), then it moves to done, "
                "where it ends.",
                "done is final: the strategy ends there once none of its resting orders is left.",
                "If a stop-loss or take-profit fires, it closes its other legs and ends.",
            ],
            ["o1", "o2", "r1"],
        ),
        (
            WEEKLY_DCA,
            [
                "In waiting (the start): Every week, counted from 2026-10-26T09:00:00Z: send 2 immediate orders (buy "
                "$50.00 of BTC at the mark within 25 bps; buy $50.00 of ETH at the mark within 25 bps), and it keeps "
                "watching for the next time."
            ],
            ["o1", "o2"],
        ),
        (
            THREE_EXITS,
            [
                "In waiting (the start): When the ETH mark crosses down through 2,800: send 1 immediate order (buy "
                "$1,000.00 of ETH at the mark within 25 bps, stop at 2,500, take profit at 3,200), then it moves to "
                "in_trade.",
                "In in_trade: When the ETH mark crosses up through 3,200: close this strategy's own share in all its "
                "markets, then it moves to done, where it ends.",
                "In in_trade: When this strategy's price move over all its markets rises to +10% (unleveraged, before "
                "fees and funding): close this strategy's own share in all its markets, then it moves to done, where it "
                "ends.",
                "In in_trade: 3 days after it enters in_trade: close this strategy's own share in all its markets, "
                "then it moves to done, where it ends.",
                "done is final: the strategy ends there once none of its resting orders is left.",
                "If its stop-loss or take-profit fires, it ends once nothing is left to do.",
            ],
            ["o1"],
        ),
        (
            WEIGHTED,
            [
                "In waiting (the start): Every day at 14:00 UTC, if the 15-minute average BTC funding is below "
                "0.001%/h: send 3 immediate orders (buy $500.00 of BTC (50% of $1,000.00) at the mark within 25 bps, "
                "stop at 80,000; buy $300.00 of ETH (30% of $1,000.00) at the mark within 25 bps, stop at 2,500; buy "
                "$200.00 of SOL (20% of $1,000.00) at the mark within 25 bps, stop at 120), and it keeps watching for "
                "the next time.",
                "If a stop-loss or take-profit fires, its other legs keep running, and it ends once nothing is left.",
            ],
            ["o1", "o2", "o3"],
        ),
        (
            EXITS,
            [
                "In waiting (the start): When the BTC mark crosses down through 88,000: send 1 immediate order (buy "
                "$1,000.00 of BTC at the mark within 25 bps, stop at 80,000, take profit at 96,000), then it moves to "
                "in_trade.",
                "In in_trade: When the BTC mark crosses up through 96,000: close this strategy's own share in all its "
                "markets, then it moves to done, where it ends.",
                "In in_trade: When this strategy's price move over all its markets rises to +5% (unleveraged, before "
                "fees and funding): close this strategy's own share in all its markets, then it moves to done, where it "
                "ends.",
                "In in_trade: 2 days after it enters in_trade: close this strategy's own share in all its markets, "
                "then it moves to done, where it ends.",
                "done is final: the strategy ends there once none of its resting orders is left.",
                "If its stop-loss or take-profit fires, it ends once nothing is left to do.",
            ],
            ["o1"],
        ),
        (
            WEEKLY,
            [
                "In waiting (the start): Every Wednesday at 15:00 UTC: place 1 resting order (buy $100.00 of ETH "
                "resting 50 bps below the mark when placed), and it keeps watching for the next time."
            ],
            ["r1"],
        ),
    ],
    ids=["reading b", "weekly dca", "three exits", "weighted", "btc exits", "weekly resting"],
)
def test_every_example_reads_as_sentences(case: str, sentences: list, orders: list) -> None:
    doc = json.loads(CASES[case]["canonical"])
    assert wf.rules(doc) == sentences
    assert [row["order"] for row in wf.order_rows(doc)] == orders


def test_a_state_nothing_reaches_is_not_described() -> None:
    doc = json.loads(CASES[WEEKLY_DCA]["canonical"])
    assert "done" not in doc["states"], "with --repeat each and no exit there is no done state"
    doc["states"]["done"] = {"on": []}
    assert wf.rules(doc) == wf.rules(json.loads(CASES[WEEKLY_DCA]["canonical"]))


def market_row(market: str, **changes) -> dict:
    row = {
        "market": market,
        "own_qty": "0",
        "own_entry": None,
        "own_profit_pct": None,
        "position_qty": "0",
        "protection": None,
        "close": None,
    }
    return row | changes


def run_status(**changes) -> dict:
    status = {
        "run_id": "7",
        "workflow_id": "3",
        "account_id": str(ACCOUNT),
        "owner": ADDRESSES["owner"],
        "label": None,
        "state": "active",
        "mode": "shadow",
        "graph_state": "waiting",
        "entered_at": "1792368000",
        "final": False,
        "stale": None,
        "allocation": {"total_usd": "2000", "reserved_usd": "0", "spent_usd": "25", "remaining_usd": "1975"},
        "markets": [market_row("BTCRUSDPERP"), market_row("ETHRUSDPERP"), market_row("SOLRUSDPERP")],
        "signals": {"trigger_mark": {"kind": "mark", "value": "2950", "age_secs": 1, "stale": False}},
        "resting_orders": [],
        "next_wake_at": None,
        "starter": ADDRESSES["agent"],
        "hosted_signer": ADDRESSES["hosted"],
        "created_at": "1792368000",
        "expires_at": "1792886400",
        "completion": None,
        "stop": None,
        "document": {"canonical": CASES[READING_A]["canonical"], "version_hash": CASES[READING_A]["version_hash"]},
        "last_seq": "1",
    }
    return status | changes


def event(seq: int, kind: str, code: str, text: str, **extra) -> dict:
    entry = {"seq": str(seq), "at": "1792368060", "kind": kind, "actor": "system", "code": code, "text": text}
    return entry | {"inputs": {}} | extra


@pytest.mark.parametrize("code", sorted(wf.SKIP_REASONS))
def test_each_named_skip_reason_is_shown_with_the_services_text(code: str) -> None:
    text = f"triggered, skipped: {code} detail"
    view = wf.status_view(run_status(), [event(1, "skipped", code, text, transition={"state": "waiting", "index": 0})])
    assert view["skips"] == [
        {
            "at": "2026-10-19T00:01:00Z",
            "code": code,
            "reason": wf.SKIP_REASONS[code],
            "transition": {"state": "waiting", "index": 0},
            "market": None,
            "text": text,
        }
    ]
    assert view["skip_counts"] == {code: 1}
    assert "1 skip." in view["summary"]


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
    assert f"not acting: {wf.STALE_REASONS[code]}" in view["summary"]


@pytest.mark.parametrize("code", sorted(wf.STOP_REASONS))
def test_each_stop_reason_is_named(code: str) -> None:
    stop = {"by": "user" if code == "user" else "system", "reason": code, "at": "1792368120"}
    view = wf.status_view(run_status(state="stopped", stop=stop), [])
    assert view["stop"]["reason_text"] == wf.STOP_REASONS[code]
    assert view["summary"].startswith(
        f"Strategy #7 (Watch only) is stopped; it trades BTC, ETH and SOL; stopped: {wf.STOP_REASONS[code]}"
    )


@pytest.mark.parametrize("code", sorted(wf.COMPLETION_REASONS))
def test_each_completion_reason_is_named(code: str) -> None:
    view = wf.status_view(run_status(state="completed", completion={"reason": code, "at": "1792368120"}), [])
    assert f"completed because {wf.COMPLETION_REASONS[code]}" in view["summary"]


def test_a_stop_that_fired_names_its_market() -> None:
    completion = {"reason": "protection_fired", "at": "1792368120", "market": "ETHRUSDPERP"}
    view = wf.status_view(run_status(state="completed", completion=completion), [])
    assert view["completion"]["market"] == "ETHRUSDPERP"
    assert "completed because a stop it armed fired on ETH" in view["summary"]


def test_a_completed_strategy_keeps_its_stops_and_says_so_per_market() -> None:
    eth = {"state": "armed", "trigger_px": "3300", "limit_px": "3399", "order_id": "1879", "market": "ETHRUSDPERP"}
    btc = {"state": "cancelled", "trigger_px": "80000", "reason": "position_closed", "market": "BTCRUSDPERP"}
    markets = [market_row("BTCRUSDPERP", protection=btc), market_row("ETHRUSDPERP", protection=eth)]
    completed = run_status(state="completed", completion={"reason": "expired", "at": "1792368120"}, markets=markets)
    view = wf.status_view(completed, [])
    assert view["completion"]["reason_text"] == "it reached its expiry and sends no new orders"
    assert (
        "completed because it reached its expiry and sends no new orders; its stop on ETH at 3,300 stays on the "
        "position until it fires or the strategy is stopped" in view["summary"]
    )
    assert "on BTC" not in view["summary"], "a stop already down is not claimed"


@pytest.mark.parametrize("completion_reason", ["expired", "final_state", "allocation_used"])
@pytest.mark.parametrize(
    ("protection_state", "message"),
    [
        ("arming", "is still arming; protection is not confirmed"),
        ("unknown", "has an unknown status; protection is not confirmed"),
        ("cancel_pending", "is being cancelled; it may still fire until cancellation is confirmed"),
    ],
)
def test_completed_strategy_does_not_claim_uncertain_protection(
    completion_reason: str, protection_state: str, message: str
) -> None:
    protection = {"state": protection_state, "trigger_px": "3300", "market": "ETHRUSDPERP"}
    run = run_status(
        state="completed",
        completion={"reason": completion_reason, "at": "1792368120"},
        markets=[market_row("ETHRUSDPERP", protection=protection)],
    )
    view = wf.status_view(run, [])
    assert f"its stop on ETH at 3,300 {message}" in view["summary"]
    assert "stays on the position" not in view["summary"]
    assert view["markets"][0]["protection"] == protection


@pytest.mark.parametrize("protection_state", ["pending", "not_armed", "cancelled", "fired"])
def test_completed_strategy_without_a_live_stop_does_not_claim_retained_protection(protection_state: str) -> None:
    run = run_status(
        state="completed",
        completion={"reason": "expired", "at": "1792368120"},
        markets=[market_row("ETHRUSDPERP", protection={"state": protection_state, "trigger_px": "3300"})],
    )
    assert "its stop on" not in wf.status_view(run, [])["summary"]


def firing(status: str, **changes) -> dict:
    base = {
        "transition": {"state": "waiting", "index": 0},
        "action": "entry",
        "order": "o2",
        "market": "BTCRUSDPERP",
        "side": "buy",
        "qty": "0.0055",
        "client_order_id": "6286",
        "order_status": status,
        "mark": "90000",
        "limit_px": "90225",
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
            {"exec_qty": "0.0055", "avg_fill_px": "90012", "settlement": "settled"},
            "buy BTC: executed 0.0055 at 90,012 (+1.3 bps from the mark 90,000) for $495.07",
        ),
        (
            "partial",
            {"exec_qty": "0.0001", "avg_fill_px": "89991", "settlement": "provisional"},
            "buy BTC: partly executed 0.0001 at 89,991 (-1.0 bps from the mark 90,000) for $9.00",
        ),
        (
            "shadow",
            {},
            "buy 0.0055 BTC at the mark 90,000: Watch only, recorded and not sent; the order would have been limited "
            "to 90,225 (25 bps bound)",
        ),
        ("past_bound", {}, "buy 0.0055 BTC: not filled, the market had moved past the 25 bps bound"),
        ("zero_fill", {}, "buy 0.0055 BTC: not filled, nothing traded within the 25 bps bound"),
        ("rejected", {"cancel_reason": "RISK"}, "buy 0.0055 BTC: rejected by the exchange: RISK"),
        ("unknown", {}, "buy 0.0055 BTC: outcome unknown; the strategy is blocked"),
        ("sent", {}, "buy 0.0055 BTC (mark 90,000), order sent: waiting for its outcome"),
        ("filled", {"exec_qty": "a lot", "avg_fill_px": "90012"}, "buy 0.0055 BTC (mark 90,000), order filled"),
    ],
)
def test_each_order_a_step_sent_reads_as_a_sentence(status: str, changes: dict, text: str) -> None:
    fired = event(1, "fired", "fired", "Sent 1 order", firings=[firing(status, **changes)])
    view = wf.status_view(run_status(), [fired])
    assert view["firings"][0]["orders"][0]["text"].startswith(text)
    assert view["firings"][0]["orders"][0]["status"] == status


def test_a_steps_orders_are_grouped_under_the_services_sentence() -> None:
    sent = [
        firing("filled", order="o1", market="ETHRUSDPERP", side="sell", qty="0.3398", exec_qty="0.3398",
               avg_fill_px="2949", mark="2950"),
        firing("filled", exec_qty="0.0055", avg_fill_px="90012"),
        firing("rejected", order="o3", market="SOLRUSDPERP", qty="3.5", cancel_reason="MARGIN"),
    ]  # fmt: skip
    placed = [
        {"action": "entry_resting", "order": "r1", "market": "ETHRUSDPERP", "side": "sell", "qty": "0.3333",
         "limit_px": "3000", "post_only": True, "reduce_only": False, "status": "resting", "filled_qty": "0"}
    ]  # fmt: skip
    text = "Sent 3 orders: 2 filled, SOL rejected for margin"
    fired = event(3, "fired", "fired", text, transition={"state": "waiting", "index": 0}, firings=sent,
                  resting_orders=placed)  # fmt: skip
    view = wf.status_view(run_status(), [fired, event(4, "skipped", "no_value", "no own position")])
    step = view["firings"][0]
    assert step["text"] == text and step["transition"] == {"state": "waiting", "index": 0}
    assert [o["order"] for o in step["orders"]] == ["o1", "o2", "o3"]
    assert (
        step["orders"][0]["text"] == "sell ETH: executed 0.3398 at 2,949 (-3.4 bps from the mark 2,950) for $1,002.07"
    )
    assert step["orders"][2]["text"] == "buy 3.5 SOL: rejected by the exchange: MARGIN"
    assert step["resting_orders"][0]["text"] == "sell 0.3333 ETH at 3,000 (order r1, post-only): resting on the book"
    assert view["summary"].endswith("1 step fired, 1 skip.")


def test_status_shows_each_market_its_resting_orders_and_signals() -> None:
    eth = market_row(
        "ETHRUSDPERP",
        own_qty="-0.3398",
        own_entry="2949",
        own_profit_pct="0.5",
        own_profit_usd="5.01",
        position_qty="-0.5",
        protection={"state": "armed", "trigger_px": "3300", "market": "ETHRUSDPERP"},
        take_profit={"state": "armed", "trigger_px": "2700", "market": "ETHRUSDPERP"},
    )
    resting = [
        {"action": "entry_resting", "order": "r1", "market": "ETHRUSDPERP", "side": "sell", "qty": "0.3333",
         "limit_px": "3000", "post_only": True, "reduce_only": False, "status": "partial", "filled_qty": "0.1",
         "expires_at": "1793491200", "placed_at": "1792368001", "reason_code": None, "reason_message": None},
        {"action": "entry_resting", "order": "r2", "market": "ETHRUSDPERP", "side": "sell", "qty": "0.3",
         "limit_px": "3100", "post_only": True, "reduce_only": False, "status": "rejected", "filled_qty": "0",
         "reason_code": "post_only_would_cross", "reason_message": "it would have taken liquidity"},
    ]  # fmt: skip
    view = wf.status_view(run_status(label="ETH breakout", markets=[eth], resting_orders=resting), [])
    assert view["markets"][0]["text"] == (
        "ETH: this strategy holds -0.3398 ETH from an average entry of 2,949; a +0.5% price move (+$5.01) at the mark, "
        "unleveraged, before fees and funding; the account's position is -0.5 ETH; its stop at 3,300 is armed; its "
        "take-profit at 2,700 is armed"
    )
    assert view["signals"]["trigger_mark"]["text"] == "the ETH mark is 2,950 (1 s old)"
    assert [r["text"] for r in view["resting_orders"]] == [
        "sell 0.3333 ETH at 3,000 (order r1, post-only): partly filled, the rest still resting, 0.1 filled",
        "sell 0.3 ETH at 3,100 (order r2, post-only): rejected (it would have taken liquidity)",
    ]
    assert view["resting_orders"][0]["expires_at"] == "2026-11-01T00:00:00Z"  # the order's own, from the row
    assert view["summary"] == (
        'Strategy #7 "ETH breakout" (Watch only) is active; it trades ETH; in waiting since 2026-10-19T00:00:00Z; '
        "1 resting order on the book; $1,975.00 of $2,000.00 remaining ($25.00 spent, $0.00 reserved); "
        "0 steps fired, 0 skips."
    )


@pytest.mark.parametrize(
    ("state", "text"),
    [
        ("blocked", "blocked: an order's outcome is unknown, and it sends nothing new until it is known"),
        ("held", "held by a Reya operator: it sends nothing until it is resumed"),
    ],
)
def test_a_blocked_or_held_strategy_says_why_it_sends_nothing(state: str, text: str) -> None:
    assert text in wf.status_view(run_status(state=state), [])["summary"]


def test_a_scheduled_strategy_says_when_it_next_looks() -> None:
    view = wf.status_view(run_status(next_wake_at="1792371600"), [])
    assert view["next_wake_at"] == "2026-10-19T01:00:00Z"
    assert "next scheduled at 2026-10-19T01:00:00Z" in view["summary"]


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
    assert view["summary"].startswith(f"Strategy #7 ({name}) is active;")


def test_mode_flags_name_the_mode_when_the_value_differs() -> None:
    assert [wf.mode_flag(m) for m in wf.MODES] == ["shadow (Watch only)", "paper", "production (Live)"]


def test_bps_and_usd_round_like_the_services_text() -> None:
    assert wf.bps_from("90000", "90012") == "+1.3 bps"
    assert wf.bps_from("90000", "89991") == "-1.0 bps"
    assert wf.bps_from("90000", "90000") == "0.0 bps"
    assert wf.usd_text("25.005") == "$25.01"
    assert wf.usd_text("1234567.891") == "$1,234,567.89"
    assert wf.group("90012.5") == "90,012.5"


@pytest.mark.parametrize(
    ("details", "text"),
    [
        (
            {"run_id": "41", "market": "BTCRUSDPERP"},
            "Strategy #41 already trades BTC, and one market belongs to one strategy on an account: stop it first "
            "(strategy stop 41), or draft this strategy without BTC.",
        ),
        ({"run_id": "41; rm -rf /", "market": "BTCRUSDPERP"}, wf.ERROR_HINTS["MARKET_IN_USE"]),
        ({"run_id": "41", "market": "<b>BTC</b>"}, wf.ERROR_HINTS["MARKET_IN_USE"]),
    ],
)
def test_market_in_use_names_the_strategy_in_the_way_and_nothing_else(details: dict, text: str) -> None:
    error = wf.ServiceError(409, "MARKET_IN_USE", "refused", False, details, None, None)
    assert error.result()["next"] == text


@pytest.mark.parametrize(
    ("details", "text"),
    [
        (
            {"limit": 3, "live": ["12", "15", "19"]},
            "This account already runs 3 strategies, the most it may (#12, #15, #19): stop one first (strategy stop "
            "RUN_ID), then start this one again.",
        ),
        ({"limit": 1, "live": [12]}, "This account already runs 1 strategy, the most it may (#12)"),
        ({"limit": "3", "live": ["12"]}, "This account already runs 3 strategies, the most it may (#12)"),
        ({"limit": "three", "live": ["12"]}, wf.ERROR_HINTS["ACCOUNT_STRATEGY_LIMIT"]),
        ({"limit": 3, "live": []}, wf.ERROR_HINTS["ACCOUNT_STRATEGY_LIMIT"]),
    ],
)
def test_the_strategy_limit_names_the_live_strategies(details: dict, text: str) -> None:
    error = wf.ServiceError(409, "ACCOUNT_STRATEGY_LIMIT", "refused", False, details, None, None)
    assert error.result()["next"].startswith(text)


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
    doc.write_text(json.dumps(changed(lambda d: d["envelope"].update(environment="devnet1", account_id="999"))))
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


async def draft(tmp_path: Path, case: str = READING_A, *extra: str, name: str = "draft.json") -> dict:
    """`strategy draft` with an example's flags, $2,000 for 7 days unless `extra` says otherwise."""
    return await call(
        "draft", *FLAGS[case], "--allocation", "2000", "--expires", "7d", "--out", str(tmp_path / name), *extra
    )


async def drafted(tmp_path: Path, *extra: str, case: str = READING_A, name: str = "draft.json") -> str:
    result = await draft(tmp_path, case, *extra, name=name)
    assert result.get("file") == str(tmp_path / name), result
    return result["file"]


def without_envelope(params: dict) -> dict:
    return {key: value for key, value in params.items() if key not in ENVELOPE_PARAMS}


@pytest.mark.parametrize("case", list(FLAGS))
async def test_each_example_is_drafted_from_its_flags(service, tmp_path, case: str) -> None:
    result = await draft(tmp_path, case)
    example = CASES[case]
    (sent,) = [body for method, path, body in service.requests if path == "/v1/validate"]
    assert set(sent) == {"template", "params"} and sent["template"] == {"id": "basket", "version": 1}
    assert without_envelope(sent["params"]) == without_envelope(example["request"]["params"])
    assert {key: sent["params"][key] for key in ENVELOPE_PARAMS} == {
        "environment": "devnet1",
        "account_id": str(ACCOUNT),
        "allocation_usd": "2000",
        "expires_at": sent["params"]["expires_at"],
        "price_bound_bps": 25,
        "max_resting_orders": 20,
    }
    assert abs(sent["params"]["expires_at"] - (time.time() + 7 * 86400)) < 60
    expected = json.loads(example["canonical"])
    expected["envelope"].update({key: sent["params"][key] for key in ENVELOPE_PARAMS})
    assert result["document"] == expected and result["params"] == sent["params"]
    # The file is the canonical form itself: its bytes are what the version hash is over.
    text = Path(result["file"]).read_text()
    assert text == wf.canonical_json(expected) and wf.version_hash(text) == result["version_hash"]
    assert result["rules"] == wf.rules(expected) and result["markets"] == expected["envelope"]["markets"]
    assert result["workflow"] == wf.describe(expected) and result["orders"] == wf.order_rows(expected)
    assert result["next"].startswith(f"strategy preview --file {result['file']}")
    assert not service.writes(), "drafting starts nothing"


@pytest.mark.parametrize("case", list(FLAGS))
async def test_each_example_is_drafted_byte_for_byte_with_its_own_envelope(
    service, tmp_path, monkeypatch, case
) -> None:
    """With the example's own account, allocation and expiry, the draft file is its canonical JSON exactly."""
    example = CASES[case]
    params = example["request"]["params"]
    monkeypatch.setenv("PERP_AGENT_ACCOUNT_IDS", params["account_id"])
    monkeypatch.setenv("PERP_ACCOUNT_ID_1", params["account_id"])
    service.add_account(int(params["account_id"]), ADDRESSES["owner"])
    result = await draft(
        tmp_path, case, "--allocation", params["allocation_usd"], "--expires", str(params["expires_at"])
    )
    assert Path(result["file"]).read_bytes() == example["canonical"].encode()
    assert result["version_hash"] == example["version_hash"] and result["document"] == example["document"]
    (sent,) = [body["params"] for method, path, body in service.requests if path == "/v1/validate"]
    # The kit spells the defaulted bound and resting cap out; everything else is the example's parameters.
    assert sent == {"price_bound_bps": 25, "max_resting_orders": 20} | params


async def test_draft_preview_start_status_stop(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    doc = json.loads(Path(path).read_text())
    assert doc["envelope"]["account_id"] == str(ACCOUNT) and doc["envelope"]["environment"] == "devnet1"

    validated = await call("validate", "--file", path)
    assert validated["ok"] is True and validated["canonical"] == Path(path).read_text()
    assert validated["version_hash"] == wf.version_hash(validated["canonical"])
    assert validated["rules"] == wf.rules(doc) and validated["markets"] == doc["envelope"]["markets"]

    preview = await call("preview", "--file", path)
    assert preview["now"] == "the ETH mark is 2,950; the level 3,000 is 1.69% (169 bps) above it"
    assert preview["fire_now_needed"] is False and "already_triggered" not in preview
    (step,) = preview["start_transitions"]
    assert [o["order"] for o in step["orders"]] == ["o1", "o2", "o3"]
    assert step["orders"][0]["text"] == (
        "sell 0.3398 ETH at a limit of 2,942.625 (25 bps max slippage), reserving $999.90, with a stop at 3,300"
    )
    assert step["orders"][2]["text"].startswith("buy 3.5625 SOL at a limit of 140.35")
    assert step["text"].startswith("sends 3 immediate orders, reserving $")
    assert preview["markets"][0] == {
        "symbol": "BTCRUSDPERP",
        "mark": "91420",
        "mark_at": preview["markets"][0]["mark_at"],
    }
    assert preview["signals"]["trigger_mark"]["text"] == "the ETH mark is 2,950 (1 s old)"
    # The service's own sentence for each transition comes first.
    assert preview["rules"] == ["In waiting: trigger: entry, then done"]
    assert [w["code"] for w in preview["warnings"]] == ["order_without_stop"]
    assert [body for method, p, body in service.requests if p == "/v1/preview"] == [
        {"document": Path(path).read_text()}
    ], "preview sends the document and nothing else"
    assert not service.writes(), "validate and preview never start anything"

    started = await call("start", "--file", path, "--label", "ETH breakout")
    assert started["started"] is True and started["state"] == "active" and started["mode"] == "shadow"
    assert started["label"] == "ETH breakout" and started["markets"] == ["BTCRUSDPERP", "ETHRUSDPERP", "SOLRUSDPERP"]
    assert started["mode_text"] == "Watch only: it records what it would do and sends no orders"
    assert started["signer"] == ADDRESSES["agent"] and started["hosted_signer"] == ADDRESSES["hosted"]
    assert started["allocation_usd"] == "2000" and started["now"] == preview["now"]
    run_id = started["run_id"]
    (_, _, body) = service.writes()[-1]
    assert body["label"] == "ETH breakout" and set(body) == {"document", "label", "request", "signature"}
    assert body["request"]["allocationUsd"] == "2000"
    assert body["request"]["expiresAt"] == str(doc["envelope"]["expires_at"])
    run = service.runs[run_id]
    assert run["starter"] == ADDRESSES["agent"]  # recovered by the fake from the signature
    assert run["document"]["canonical"] == validated["canonical"]

    service.add_event(
        run_id, "skipped", "skip_no_value", "no own position yet", transition={"state": "waiting", "index": "0"}
    )
    service.add_event(
        run_id,
        "fired",
        "fired",
        "Sent 3 orders: 2 filled, SOL rejected for margin",
        transition={"state": "waiting", "index": 0},
        firings=[firing("filled", exec_qty="0.0055", avg_fill_px="90012", settlement="provisional")],
    )
    status = await call("status", run_id)
    assert status["label"] == "ETH breakout" and status["graph_state"] == "waiting"
    assert [m["market"] for m in status["markets"]] == ["BTCRUSDPERP", "ETHRUSDPERP", "SOLRUSDPERP"]
    assert all(m["protection"] is None for m in status["markets"]), "no stop at start, nor before a fill settles"
    assert status["firings"][0]["text"] == "Sent 3 orders: 2 filled, SOL rejected for margin"
    assert status["firings"][0]["orders"][0]["text"].startswith("buy BTC: executed 0.0055 at 90,012")
    assert status["skips"][0]["reason"] == wf.SKIP_REASONS["skip_no_value"]
    assert status["allocation"]["text"] == "$2,000.00 of $2,000.00 remaining ($0.00 spent, $0.00 reserved)"
    assert status["recent_events"][0]["text"] == f"Started by {ADDRESSES['agent']} in Watch only mode."
    assert status["summary"].startswith('Strategy #1 "ETH breakout" (Watch only) is active; it trades BTC, ETH and SOL')

    listing = await call("status")
    assert [r["run_id"] for r in listing["strategies"]] == [run_id]
    assert (
        listing["strategies"][0]["text"]
        == 'Strategy #1 "ETH breakout" (Watch only) trades BTC, ETH and SOL: active, in waiting'
    )
    assert listing["strategies"][0]["mode_text"].startswith("Watch only:")
    assert listing["active_run"]["run_id"] == run_id

    stopped = await call("stop", run_id)
    assert stopped["state"] == "stopped" and stopped["stop"]["reason"] == "user"
    assert stopped["stop"]["signer"] == ADDRESSES["agent"] and stopped["label"] == "ETH breakout"
    assert [m["close"]["state"] for m in stopped["markets"]] == ["nothing_to_close"] * 3
    again = await call("stop", run_id)
    assert again["already_stopped"] is True
    assert len(service.writes()) == 2, "a stopped strategy is not stopped twice"
    assert (await call("status"))["strategies"] == [], "a stopped strategy is no longer live"
    assert [r["run_id"] for r in (await call("status", "--all"))["strategies"]] == [run_id]


async def test_the_owner_key_in_the_environment_is_never_used(service, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", KEYS["owner"])
    started = await call("start", "--file", await drafted(tmp_path))
    assert service.runs[started["run_id"]]["starter"] == ADDRESSES["agent"]


async def test_a_document_for_another_account_is_refused_before_any_request(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.requests.clear()
    Path(path).write_text(json.dumps(changed(lambda d: d["envelope"].update(account_id="999"))))
    for command in ("validate", "preview", "start"):
        result = await call(command, "--file", path)
        assert "not your recorded Agent account" in result["error"]
    assert service.requests == []
    assert "not your recorded Agent account" in (await draft(tmp_path, READING_A, "--account-id", "999"))["error"]
    assert service.requests == []


async def test_an_older_document_is_refused_before_any_request(service, tmp_path) -> None:
    path = tmp_path / "v1.json"
    path.write_text(json.dumps({"schema": "reya.workflow/1", "account_id": str(ACCOUNT), "environment": "devnet1"}))
    for command in ("validate", "preview", "start"):
        assert "Draft it again with strategy draft" in (await call(command, "--file", str(path)))["error"]
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
    service.next_refusal = "BUSY"
    retry = await call("stop", "1")
    assert retry["error_code"] == "BUSY" and retry["retryable"] is True and retry["retry_after_s"] == 7


async def test_one_market_belongs_to_one_strategy(service, tmp_path) -> None:
    basket = await drafted(tmp_path)
    btc = await drafted(tmp_path, "--allocation", "2000", case=EXITS, name="btc.json")
    first = (await call("start", "--file", basket))["run_id"]
    # The preview warns before anything is signed, and names the strategy in the way.
    preview = await call("preview", "--file", btc)
    (warning,) = [w for w in preview["warnings"] if w["code"] == "market_in_use"]
    assert warning["next"].startswith(f"Strategy #{first} already trades BTC")
    refused = await call("start", "--file", btc)
    assert refused["error_code"] == "MARKET_IN_USE" and refused["http_status"] == 409
    assert refused["details"] == {"run_id": first, "market": "BTCRUSDPERP"}
    assert refused["next"] == (
        f"Strategy #{first} already trades BTC, and one market belongs to one strategy on an account: stop it first "
        f"(strategy stop {first}), or draft this strategy without BTC."
    )
    assert refused["app_run_url"] == run_page(first) and refused["app_runs_url"] == runs_page()
    assert [r["state"] for r in service.runs.values()] == ["active"], "nothing else started"
    await call("stop", first)
    assert (await call("start", "--file", btc))["started"] is True, "a stopped strategy frees its markets"


def balance_of(amount: str):
    async def read(account: int):
        return wf.Decimal(amount)

    return read


def kit_warnings(preview: dict) -> dict:
    return {w["code"]: w for w in preview["warnings"] if w.get("source") == "kit"}


async def test_a_start_step_above_the_available_balance_is_called_out(service, tmp_path, monkeypatch) -> None:
    path = await drafted(tmp_path)
    monkeypatch.setattr(workflow, "available_balance", balance_of("1500"))
    preview = await call("preview", "--file", path)
    reserved = preview["start_transitions"][0]["reserved_usd"]
    assert wf.Decimal(reserved) > 1500 and preview["available_balance_usd"] == "1500"
    warnings = kit_warnings(preview)
    assert list(warnings) == ["size_above_balance"], "no other live strategy: the budgets warning would say the same"
    assert warnings["size_above_balance"]["message"] == (
        f"Its largest step at the start reserves {wf.usd_text(reserved)}, more than the account's available balance "
        "of $1,500.00 (equity less initial margin): at 1x, orders may be refused for margin."
    )
    assert warnings["size_above_balance"]["details"] == {"reserved_usd": reserved, "available_balance_usd": "1500"}
    monkeypatch.setattr(workflow, "available_balance", balance_of("5000"))
    assert kit_warnings(await call("preview", "--file", path)) == {}


async def test_live_budgets_and_the_start_step_above_the_balance_are_called_out(service, tmp_path, monkeypatch) -> None:
    btc = await drafted(tmp_path, "--allocation", "2000", case=EXITS, name="btc.json")
    eth = await drafted(tmp_path, "--allocation", "500", case=WEEKLY, name="eth.json")
    await call("start", "--file", btc)
    monkeypatch.setattr(workflow, "available_balance", balance_of("2050"))
    preview = await call("preview", "--file", eth)
    warnings = kit_warnings(preview)
    assert list(warnings) == ["budgets_above_balance"]
    budgets = warnings["budgets_above_balance"]
    assert budgets["details"]["other_budgets_usd"] == "2000" and budgets["details"]["available_balance_usd"] == "2050"
    assert "the remaining budgets of the account's 1 other live strategy ($2,000.00) add up to" in budgets["message"]
    assert "they share that margin" in budgets["message"]
    assert not service.writes("/v1/runs/"), "previewing reads, it never stops or starts anything"


async def test_no_balance_warning_when_the_balance_cannot_be_read(service, tmp_path) -> None:
    preview = await call("preview", "--file", await drafted(tmp_path))
    assert preview["available_balance_usd"] is None and kit_warnings(preview) == {}
    assert not [p for _, p, _ in service.requests if p.startswith("/v1/accounts/")], "no budget reads without it"


def test_a_balance_warning_the_service_gave_is_not_repeated() -> None:
    step = {"start_transitions": [{"reserved_usd": "100"}]}
    both = wf.balance_warnings(step | {"warnings": []}, wf.Decimal("50"), (wf.Decimal("10"), 1))
    assert [w["code"] for w in both] == ["size_above_balance", "budgets_above_balance"]
    given = step | {"warnings": [{"code": "size_above_balance", "message": "the service's"}]}
    assert [w["code"] for w in wf.balance_warnings(given, wf.Decimal("50"), (wf.Decimal("10"), 1))] == [
        "budgets_above_balance"
    ]
    assert wf.balance_warnings(given, None, None) == []


async def test_the_available_balance_is_equity_less_initial_margin(monkeypatch) -> None:
    class Client:
        def __init__(self, config) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

    snapshot = SimpleNamespace(
        margin=SimpleNamespace(margin_balance=wf.Decimal("1200.5")),
        settled=lambda: SimpleNamespace(im=wf.Decimal("200.25")),
    )

    async def load(client, account):
        assert account == ACCOUNT
        return snapshot

    monkeypatch.setattr(workflow, "AccountDataClient", Client)
    monkeypatch.setattr(workflow, "load_snapshot", load)
    monkeypatch.setattr(workflow._env, "get_trading_config", lambda include_private_key: None)
    assert await real_available_balance(ACCOUNT) == wf.Decimal("1000.25")

    async def broken(client, account):
        raise ValueError("no price for an asset")

    monkeypatch.setattr(workflow, "load_snapshot", broken)
    assert await real_available_balance(ACCOUNT) is None, "best effort: the preview goes on without it"


async def test_an_account_runs_up_to_its_limit_of_strategies(service, tmp_path) -> None:
    service.max_live = 1
    btc = await drafted(tmp_path, "--allocation", "2000", case=EXITS, name="btc.json")
    eth = await drafted(tmp_path, "--allocation", "500", case=WEEKLY, name="eth.json")
    first = (await call("start", "--file", btc))["run_id"]
    refused = await call("start", "--file", eth)
    assert refused["error_code"] == "ACCOUNT_STRATEGY_LIMIT"
    assert refused["next"].startswith(f"This account already runs 1 strategy, the most it may (#{first})")
    assert refused["app_runs_url"] == runs_page()
    service.max_live = 3
    second = (await call("start", "--file", eth))["run_id"]
    listing = await call("status")
    assert [r["run_id"] for r in listing["strategies"]] == [second, first]
    assert [r["markets"] for r in listing["strategies"]] == [["ETHRUSDPERP"], ["BTCRUSDPERP"]]
    assert "active_run" not in listing and listing["next"].startswith("strategy status RUN_ID")


async def test_stop_all_stops_every_live_strategy(service, tmp_path) -> None:
    service.modes = ["shadow", "production"]
    btc = await drafted(tmp_path, "--allocation", "2000", case=EXITS, name="btc.json")
    eth = await drafted(tmp_path, "--allocation", "500", case=WEEKLY, name="eth.json")
    first = (await call("start", "--file", btc, "--mode", "production"))["run_id"]
    second = (await call("start", "--file", eth))["run_id"]
    # A completed strategy whose stop is still live keeps its market, and --all leaves it alone.
    service.runs["9"] = run_status(
        run_id="9",
        state="completed",
        completion={"reason": "final_state", "at": "1792368120"},
        markets=[market_row("SOLRUSDPERP", protection={"state": "armed", "trigger_px": "120"})],
    )
    service.events["9"] = []
    result = await call("stop", "--all")
    assert [r["run_id"] for r in result["stopped"]] == [second, first]
    assert all(r["stop"]["keep_position"] is False for r in result["stopped"])
    assert [r["state"] for r in service.runs.values()] == ["stopped", "stopped", "completed"]
    assert result["note"].startswith("Stopped 2 of 2 live strategies.")
    assert (
        "Strategy #9 (Watch only) trades SOL: completed; still holds SOL while its stop there is live" in result["note"]
    )
    assert [r["run_id"] for r in result["holding"]] == ["9"]
    assert [m["close"]["state"] for m in result["stopped"][1]["markets"]] == ["pending"], "the Live one closes"
    assert len(service.writes("/v1/runs/")) == 2
    again = await call("stop", "--all")
    assert again["stopped"] == [] and again["note"].startswith("No live strategy on this account: nothing was signed.")
    assert len(service.writes("/v1/runs/")) == 2


async def test_stop_all_goes_on_past_a_refusal_and_says_which(service, tmp_path) -> None:
    btc = await drafted(tmp_path, "--allocation", "2000", case=EXITS, name="btc.json")
    eth = await drafted(tmp_path, "--allocation", "500", case=WEEKLY, name="eth.json")
    first = (await call("start", "--file", btc))["run_id"]
    second = (await call("start", "--file", eth))["run_id"]
    service.next_refusal = "BUSY"
    result = await call("stop", "--all", "--keep-position")
    assert result["stopped"][0]["run_id"] == second and result["stopped"][0]["error_code"] == "BUSY"
    assert result["stopped"][1]["run_id"] == first and result["stopped"][1]["stop"]["keep_position"] is True
    assert f"not stopped: #{second}" in result["note"]


def test_stop_takes_one_strategy_or_all_of_them() -> None:
    parser = workflow.build_parser()
    assert parser.parse_args(["stop", "--all"]).all is True
    assert parser.parse_args(["stop", "7"]).run_id == 7


async def test_stop_needs_a_strategy_or_all(service) -> None:
    assert "or stop every live one with --all" in (await call("stop"))["error"]
    assert "not both" in (await call("stop", "1", "--all"))["error"]
    assert not service.requests


async def test_a_mode_the_environment_does_not_allow_is_refused_unsigned(service, tmp_path) -> None:
    result = await call("start", "--file", await drafted(tmp_path), "--mode", "production")
    assert result["error_code"] == "MODE_NOT_ALLOWED"
    assert "allows shadow (Watch only) mode(s), not production (Live)" in result["error"]
    assert not service.writes()


async def test_pilot_caps_are_checked_on_every_order_before_signing(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.max_allocation_usd = "1000"
    result = await call("start", "--file", path)
    assert result["error_code"] == "ABOVE_PILOT_LIMIT" and "allocation $2,000.00" in result["error"]
    service.max_allocation_usd = "500000"
    service.max_order_usd = "600"
    result = await call("start", "--file", path)
    assert result["error_code"] == "ABOVE_PILOT_LIMIT"
    assert result["error"] == "Order o1 is $1,000.00, above the pilot cap of $600.00 per order"
    # A weight counts as its share of the action's total.
    weighted = await drafted(tmp_path, "--allocation", "3000", case=WEIGHTED, name="weighted.json")
    service.max_order_usd = "400"
    assert "Order o1 is $500.00" in (await call("start", "--file", weighted))["error"]
    assert not service.writes()


async def test_a_service_for_another_environment_is_refused(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.info_override = {"environment": "mainnet", "chain_id": "1729"}
    result = await call("start", "--file", path)
    assert result["error_code"] == "WRONG_ENVIRONMENT"
    assert not service.writes()


async def test_a_service_that_does_not_run_these_documents_signs_nothing(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.info_override = {"schemas": ["reya.workflow/1"]}
    result = await call("start", "--file", path)
    assert "this kit drafts reya.workflow/2; nothing was signed" in result["error"]
    assert not service.writes()


async def test_a_tampered_canonical_form_is_never_signed(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    honest = FakeWorkflowService.canonical(json.loads(Path(path).read_text()))
    service.canonical_override = honest.replace('"level":"3000"', '"level":"3100"')
    result = await call("start", "--file", path)
    assert "changes signals.trigger.level" in result["error"]
    assert not service.writes()


async def test_a_draft_the_service_expands_into_something_else_is_never_written(service, tmp_path) -> None:
    expires = str(int(time.time()) + 86400)
    honest = json.loads(Path(await drafted(tmp_path, "--expires", expires, name="honest.json")).read_text())
    honest["actions"]["entry"]["orders"][1]["size"] = {"usd": "5000"}
    service.canonical_override = wf.canonical_json(honest)
    result = await draft(tmp_path, READING_A, "--expires", expires)
    assert result["error"] == "The service's document changes order o2 from what was asked; nothing was written"
    assert not (tmp_path / "draft.json").exists()


async def test_an_invalid_draft_lists_the_services_errors_and_writes_nothing(service, tmp_path) -> None:
    service.marks.pop("SOLRUSDPERP")
    result = await draft(tmp_path)
    assert result["ok"] is False and result["errors"][0]["code"] == "market_unknown"
    assert result["error"] == "The strategy is invalid: unknown market SOLRUSDPERP (params.entry.orders.2.market)"
    assert result["params"]["entry"]["orders"][2]["market"] == "SOLRUSDPERP"
    assert not (tmp_path / "draft.json").exists()


async def test_an_invalid_document_lists_the_services_errors(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.marks.pop("SOLRUSDPERP")
    result = await call("validate", "--file", path)
    assert result["ok"] is False and result["errors"][0]["code"] == "market_unknown"
    assert result["error"] == "The strategy is invalid: unknown market SOLRUSDPERP (envelope.markets.2)"


async def test_a_lost_answer_to_an_accepted_start_is_confirmed_by_reading(service, tmp_path) -> None:
    service.next_write = "lose"
    started = await call("start", "--file", await drafted(tmp_path))
    assert started["started"] is True and started["confirmed_by_read"] is True
    assert len(service.writes()) == 1, "never resent"
    # It looked among as many active strategies as the account may run.
    reads = [p for p in service.raw_paths if "/accounts/" in p]
    assert reads == [f"/workflows/v1/accounts/{ACCOUNT}/runs?state=active&limit=3"]


async def test_a_lost_answer_that_cannot_be_confirmed_is_reported_not_resent(service, tmp_path) -> None:
    service.next_write = "drop"
    result = await call("start", "--file", await drafted(tmp_path))
    assert result["outcome"] == "unknown" and "nothing is resent" in result["error"]
    assert "strategy status" in result["next"]
    assert len(service.writes()) == 1


async def test_a_stop_without_a_protective_order_says_there_is_nothing_to_cancel(service, tmp_path) -> None:
    run_id = (await call("start", "--file", await drafted(tmp_path)))["run_id"]
    stopped = await call("stop", run_id)
    assert all(m["protection"] is None for m in stopped["markets"])
    assert stopped["next"].startswith("It armed no stop, so there is nothing to cancel")


def last_stop(service) -> dict:
    stops = [body for (_, path, body) in service.writes() if path.endswith("/stop")]
    return stops[-1]["request"]


async def test_a_stop_closes_the_strategys_own_share_by_default(service, tmp_path) -> None:
    run_id = (await call("start", "--file", await drafted(tmp_path)))["run_id"]
    service.runs[run_id]["markets"][1]["protection"] = {"state": "armed", "trigger_px": "3300", "market": "ETHRUSDPERP"}
    stopped = await call("stop", run_id)
    assert last_stop(service)["keepPosition"] is False
    assert stopped["stop"]["keep_position"] is False
    # A Watch only strategy holds nothing to close.
    assert [m["close"]["text"] for m in stopped["markets"]] == [
        f"{asset}: nothing of its own share was still open (simulated: nothing was sent)"
        for asset in ("BTC", "ETH", "SOL")
    ]
    assert (
        "closes this strategy's own share" in stopped["note"]
        and "Its resting orders are cancelled first" in stopped["note"]
    )
    assert stopped["next"] == f"strategy status {run_id} shows the cancellation of each market's stop"
    status = await call("status", run_id)
    assert "BTC: nothing of its own share was still open" in status["summary"]


async def test_a_production_stop_follows_its_closes(service, tmp_path) -> None:
    service.modes = ["shadow", "production"]
    run_id = (await call("start", "--file", await drafted(tmp_path), "--mode", "production"))["run_id"]
    stopped = await call("stop", run_id)
    assert [m["close"]["state"] for m in stopped["markets"]] == ["pending"] * 3
    assert (
        stopped["next"] == f"strategy status {run_id} follows the closes, then the cancellation of each market's stop"
    )


async def test_stopping_again_says_what_the_first_stop_chose(service, tmp_path) -> None:
    service.modes = ["shadow", "production"]
    run_id = (await call("start", "--file", await drafted(tmp_path), "--mode", "production"))["run_id"]
    await call("stop", run_id)
    again = await call("stop", run_id, "--keep-position")
    assert again["already_stopped"] is True and "signer" not in again
    assert again["stop"]["keep_position"] is False and again["markets"][0]["close"]["state"] == "pending"
    assert again["note"].startswith(
        "It was already stopped, and that stop closes this strategy's own share: stopping again changes nothing."
    )
    assert "already stopped" not in (await call("stop", run_id))["note"]


@pytest.mark.parametrize(
    ("close", "text"),
    [
        ({"state": "pending"}, "waiting for the strategy's last orders and fills before closing its own share"),
        (
            {"state": "closing", "attempts": "1", "max_attempts": "3", "closed_qty": "0.01"},
            "closing its own share: attempt 1 of 3 made, 0.01 closed so far",
        ),
        ({"state": "closed", "closed_qty": "0.03"}, "closed its own share (0.03 in all)"),
        ({"state": "nothing_to_close"}, "nothing of its own share was still open"),
        (
            {"state": "stopped_still_open", "attempts": "3", "open_qty": "-0.02"},
            "0.02 of its own share is still open after 3 close attempts: close it by hand",
        ),
        (
            {"state": "stopped_still_open", "attempts": "3", "open_qty": None},
            "part of its own share is still open after 3 close attempts: close it by hand",
        ),
        ({"state": "unknown"}, "a close order's outcome is unknown, so the service stopped trying: check the position"),
        (
            {"state": "closed", "closed_qty": "0.03", "simulated": True},
            "closed its own share (0.03 in all) (simulated: nothing was sent)",
        ),
        ({"state": "closed", "closed_qty": "0.3", "market": "ETHRUSDPERP"}, "ETH: closed its own share (0.3 in all)"),
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
    assert stopped["stop"]["keep_position"] is True and all(m["close"] is None for m in stopped["markets"])
    assert "no stop order from this strategy" in stopped["note"]


async def test_the_stops_own_answer_reports_the_markets_when_the_read_back_fails(service, tmp_path) -> None:
    service.modes = ["shadow", "production"]
    run_id = (await call("start", "--file", await drafted(tmp_path), "--mode", "production"))["run_id"]
    service.refuse_stopped_reads = True
    stopped = await call("stop", run_id)
    assert stopped["state"] == "stopped" and stopped["stop"]["keep_position"] is False
    assert [m["close"]["text"] for m in stopped["markets"]] == [
        f"{asset}: waiting for the strategy's last orders and fills before closing its own share"
        for asset in ("BTC", "ETH", "SOL")
    ], "a Live close is real: no simulated note"
    assert stopped["resting_orders"] == []
    assert stopped["next"].startswith(f"strategy status {run_id} follows the closes")


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
    assert [r["app_run_url"] for r in listing["strategies"]] == [run_page(run_id)]
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
    drafted_result = await draft(tmp_path, READING_A, name="linked.json")
    path = drafted_result["file"]
    assert [(m, p) for m, p, _ in service.requests] == [
        ("POST", "/v1/validate")
    ], "draft only asks the service to expand"
    url = drafted_result["app_review_url"]
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
    assert json.loads(text) == file_doc == drafted_result["document"]
    assert text == json.dumps(file_doc, separators=(",", ":")), "compact: the link stays short"
    # The page previews and starts exactly this document: the same hash as the file's.
    assert wf.version_hash(wf.canonical_json(json.loads(text))) == drafted_result["version_hash"]

    preview = await call("preview", "--file", path)
    assert preview["app_review_url"] == url
    assert preview["next"].startswith(
        f"After the user confirms the rules, the orders and the allocation above: strategy start --file {path}"
    )
    assert "give the user app_review_url exactly as returned instead" in preview["next"]
    assert not service.writes(), "a review link starts nothing"


async def test_the_review_link_carries_the_document_the_preview_read(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    doc = json.loads(Path(path).read_text())
    doc["envelope"]["allocation_usd"] = "1500"
    Path(path).write_text(json.dumps(doc, indent=4))
    preview = await call("preview", "--file", path)
    _, _, fragment = review_link(preview["app_review_url"])
    assert json.loads(decoded(fragment["doc"][0]))["envelope"]["allocation_usd"] == "1500"


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
# The V2 read of the owner's accounts and their types: is the recorded account an Agent account.
OWNER_ACCOUNTS = f"/v2/wallet/{ADDRESSES['owner']}/accounts"


async def test_info_links_the_enable_hosted_runs_step_until_the_owner_approves(service, opened) -> None:
    service.hosted_enabled.clear()
    info = await call("info")
    assert info["hosted_signer"] == ADDRESSES["hosted"] and info["hosted_signer_enabled"] is False
    assert info["enable_url"] == enable_page()
    assert_code_only_in_fragment(info["enable_url"])
    assert "Give the user enable_url" in info["next"] and ADDRESSES["owner"] in info["next"]
    assert "not approved yet either" not in info["next"]
    # Read the way agent status reads the agent key's approval and the Agent account; nothing opened, signed or sent.
    assert service.trading_reads == [WALLET_STATUS, OWNER_ACCOUNTS]
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
    assert service.trading_reads == [WALLET_STATUS, OWNER_ACCOUNTS]
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


async def test_an_already_met_trigger_is_called_out_in_the_preview(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.marks["ETHRUSDPERP"] = "3010"
    preview = await call("preview", "--file", path)
    assert preview["fire_now_needed"] is True and preview["start_transitions"][0]["already_met"] is True
    assert "--fire-now" in preview["already_triggered"] and "--fire-now" in preview["next"]
    assert preview["now"] == "the ETH mark is 3,010, already past the level of 3,000"
    assert preview["start_transitions"][0]["text"].startswith("its trigger is already met; it sends 3 immediate orders")


def sent_fire_now(service) -> bool:
    (_, _, body) = service.writes()[-1]
    return body["request"]["fireNow"]


async def test_a_trigger_met_since_the_preview_is_not_started_unasked(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    assert "already_triggered" not in await call("preview", "--file", path)
    # The mark crosses the level between the user's yes and the start (seen live on mainnet: 15 bps in 90 s).
    service.marks["ETHRUSDPERP"] = "3010"
    refused = await call("start", "--file", path, "--label", "ETH's breakout")
    assert refused["error_code"] == "ALREADY_TRIGGERED" and "Nothing was signed" in refused["error"]
    assert refused["now"] == "the ETH mark is 3,010, already past the level of 3,000"
    assert refused["start_transitions"][0]["already_met"] is True and refused["file"] == path
    command = f"strategy start --file {path} --label 'ETH'\"'\"'s breakout'"
    assert f"{command} --fire-now" in refused["next"] and f"{command} --wait-for-rearm" in refused["next"]
    assert not service.writes(), "nothing is signed or sent until the user chooses"


async def test_the_users_choice_for_a_met_trigger_is_started_and_said_back(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.marks["ETHRUSDPERP"] = "3010"
    fired = await call("start", "--file", path, "--fire-now")
    assert fired["started"] is True and fired["fire_now"] is True and sent_fire_now(service) is True
    assert fired["already_triggered"].endswith("acts on the first fresh value.")
    assert (await call("stop", fired["run_id"]))["state"] == "stopped"
    waiting = await call("start", "--file", path, "--wait-for-rearm")
    assert waiting["started"] is True and waiting["fire_now"] is False and sent_fire_now(service) is False
    assert "waits until the value moves back past the re-arm band" in waiting["already_triggered"]


async def test_a_start_says_where_the_trigger_stands(service, tmp_path) -> None:
    started = await call("start", "--file", await drafted(tmp_path))
    assert started["now"] == "the ETH mark is 2,950; the level 3,000 is 1.69% (169 bps) above it"
    assert "already_triggered" not in started


@pytest.mark.parametrize(
    ("case", "now"),
    [
        (READING_B, "it acts right after the start"),
        (WEEKLY_DCA, "Every week, counted from 2026-10-26T09:00:00Z: next at "),
        (WEIGHTED, "Every day at 14:00 UTC: next at "),
        (WEEKLY, "Every Wednesday at 15:00 UTC: next at "),
    ],
)
async def test_a_start_or_schedule_trigger_says_when_it_acts(service, tmp_path, case: str, now: str) -> None:
    preview = await call("preview", "--file", await drafted(tmp_path, case=case))
    assert preview["now"].startswith(now) and preview["fire_now_needed"] is False
    if case != READING_B:
        assert len(preview["schedule"][0]["next"]) == 3 and preview["schedule"][0]["signal"] == "schedule"


async def test_a_resting_order_is_previewed_at_its_price(service, tmp_path) -> None:
    preview = await call("preview", "--file", await drafted(tmp_path, case=READING_B))
    (step,) = preview["start_transitions"]
    assert step["resting"][0]["text"] == (
        "sell 0.3333 ETH resting at 3,000 (post-only), reserving $999.90, with a stop at 3,300"
    )
    assert step["text"].startswith("sends 2 immediate orders and places 1 resting order, reserving $")


async def test_a_crossed_level_keeps_the_mode_in_the_suggested_commands(service, tmp_path) -> None:
    service.modes = ["shadow", "production"]
    path = await drafted(tmp_path)
    service.marks["ETHRUSDPERP"] = "3010"
    refused = await call("start", "--file", path, "--mode", "production")
    assert f"strategy start --file {path} --mode production --fire-now" in refused["next"]
    assert not service.writes()


def test_fire_now_belongs_to_start_not_preview() -> None:
    parser = workflow.build_parser()
    assert parser.parse_args(["start", "--file", "f.json", "--fire-now"]).fire_now is True
    assert parser.parse_args(["start", "--file", "f.json", "--wait-for-rearm"]).wait_for_rearm is True
    with pytest.raises(SystemExit):
        parser.parse_args(["preview", "--file", "f.json", "--fire-now"])
    with pytest.raises(SystemExit):
        parser.parse_args(["start", "--file", "f.json", "--fire-now", "--wait-for-rearm"])


def test_conditions_are_on_funding_a_position_or_the_balance() -> None:
    assert wf.parse_condition("funding:eth:below:0.001") == {
        "kind": "funding_avg",
        "market": "ETHRUSDPERP",
        "op": "below",
        "level": "0.001",
    }
    assert wf.parse_condition("position:BTCRUSDPERP:above:-0.50") == {
        "kind": "position",
        "market": "BTCRUSDPERP",
        "op": "above",
        "level": "-0.5",
    }
    # The account's available balance, which only a service that prices margins can read (see the draft test).
    assert wf.parse_condition("balance:above:500") == {"kind": "available_balance", "op": "above", "level": "500"}
    with pytest.raises(wf.WorkflowError, match="comparison is above or below"):
        wf.parse_condition("funding:eth:lt:0.001")
    with pytest.raises(wf.WorkflowError, match="--if takes"):
        wf.parse_condition("volume:eth:above:5")


def test_a_preview_order_that_cannot_be_sized_says_why() -> None:
    preview = {
        "markets": [{"symbol": "BTCRUSDPERP", "mark": "91420", "mark_at": "1792281600"}],
        "signals": {"trigger_mark": {"kind": "mark", "value": "2950", "age_secs": 1, "stale": False}},
        "start_transitions": [
            {
                "index": 0,
                "already_met": False,
                "orders": [],
                "resting": [],
                "closes": [],
                "order_errors": [
                    {"action": "entry", "order": "o3", "market": "SOLRUSDPERP", "code": "order_below_minimum",
                     "message": "the order is below the market's minimum"}
                ],  # fmt: skip
                "reserved_usd": "0",
            }
        ],
    }
    view = wf.preview_view(preview, DOC_A)
    step = view["start_transitions"][0]
    assert step["order_errors"][0]["text"] == "order o3 (SOL): the order is below the market's minimum"
    assert step["text"] == (
        "sends nothing, reserving $0.00; 1 order could not be sized now: order o3 (SOL): the order is below the "
        "market's minimum"
    )
    assert view["rules"] == wf.rules(DOC_A), "without the service's sentences, the kit's own"


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
# An account the service refuses as not the Agent account
# --------------------------------------------------------------------------

DOCUMENT_POSTS = ("/v1/validate", "/v1/preview", "/v1/runs")


async def test_an_account_that_is_not_an_agent_account_is_refused_before_anything_is_signed(service, tmp_path) -> None:
    # The canary's case: an earlier kit recorded an untagged sub-account, and the app made the Agent account since.
    path = await drafted(tmp_path)  # drafted while the account still counts as the Agent account
    service.agent_accounts.discard(str(ACCOUNT))
    service.add_account(ACCOUNT + 1, ADDRESSES["owner"])
    service.requests.clear()
    for command in ("validate", "preview", "start"):
        result = await call(command, "--file", path)
        assert result["ok"] is False and result["error_code"] == "NOT_AGENT_ACCOUNT", command
        assert result["agent_account_fix"] == {
            "command": f"agent init --account-id {ACCOUNT + 1}",
            "account_id": ACCOUNT + 1,
        }
        warning = result["agent_account_warning"]
        assert f"run agent init --account-id {ACCOUNT + 1}: the kit switches to it" in warning
        assert result["next"] == f"Nothing was signed or started. {warning}" and ".env" not in warning
    # Refused before the service was asked to check, preview or start anything.
    assert not [r for r in service.requests if r[0] == "POST" and r[1] in DOCUMENT_POSTS]
    # A draft asks the service to expand it, and the service's own check refuses it with the same fix.
    drafted_again = await draft(tmp_path, READING_A, name="again.json")
    assert drafted_again["ok"] is False and drafted_again["errors"][0]["code"] == "not_agent_account"
    assert drafted_again["agent_account_fix"]["command"] == f"agent init --account-id {ACCOUNT + 1}"
    assert not (tmp_path / "again.json").exists()


async def test_the_service_s_not_agent_account_answers_get_a_next_that_fixes_them(service, tmp_path) -> None:
    path = await drafted(tmp_path)
    service.agent_accounts.discard(str(ACCOUNT))
    # The owner's accounts cannot be read, so the service is asked, and its own check answers before signing.
    service.trading_failure = aiohttp.ClientConnectionError("connection refused")
    for command in ("validate", "preview", "start"):
        result = await call(command, "--file", path)
        assert result["ok"] is False and result["errors"][0]["code"] == "not_agent_account", command
        assert result["next"] == wf.AGENT_ACCOUNT_REASONS["not_tagged"]
        assert "Run agent status: it names the command that switches the kit" in result["next"]
    assert not service.writes()
    # A service from before that check: the signed start is refused, with the same next.
    service.checks_agent_account = False
    result = await call("start", "--file", path)
    assert result["error_code"] == "NOT_AGENT_ACCOUNT" and result["next"] == wf.AGENT_ACCOUNT_REASONS["not_tagged"]
    assert len(service.writes()) == 1
    # Once the owner's accounts can be read again, that refusal names the fix too.
    service.trading_failure = None
    service.add_account(ACCOUNT + 1, ADDRESSES["owner"])
    result = await call("validate", "--file", path)
    assert result["agent_account_fix"]["command"] == f"agent init --account-id {ACCOUNT + 1}"


async def test_info_puts_the_agent_account_fix_first(service) -> None:
    service.agent_accounts.discard(str(ACCOUNT))
    service.add_account(ACCOUNT + 1, ADDRESSES["owner"])
    service.add_account(ACCOUNT + 2, ADDRESSES["owner"])
    info = await call("info")
    assert info["hosted_signer_enabled"] is True and info["next"] == info["agent_account_warning"]
    assert info["agent_account_fix"] == {
        "command": "agent init --account-id <id>",
        "candidates": [ACCOUNT + 1, ACCOUNT + 2],
    }
    # No Agent account at all: the setup page, without the recorded account, creates one.
    service.agent_accounts.clear()
    info = await call("info")
    assert info["agent_account_fix"] == {"url": enable_page(account=False), "then": "agent status"}
    # The Agent account: nothing to say.
    service.agent_accounts.add(str(ACCOUNT))
    assert "agent_account_warning" not in await call("info")


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
    with pytest.raises(wf.WorkflowError, match="No strategy service is known .* set REYA_WORKFLOWS_URL"):
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
    assert info["hosted_signer"] == ADDRESSES["hosted"] and info["schemas"] == ["reya.workflow/2"]
    assert info["modes"] == ["shadow"] and info["limits"]["max_allocation_usd"] == "500000"
    assert info["limits"]["max_live_strategies_per_account"] == 3 and info["limits"]["max_orders_per_action"] == 14


def test_info_without_the_v2_limits_still_reads() -> None:
    body = FakeWorkflowService().info_body()
    body["limits"] = {
        k: v for k, v in body["limits"].items() if k.startswith(("max_allocation", "max_order_usd", "max_deadline"))
    }
    del body["schemas"]
    info = wf.parse_info(body, "devnet1", 89346162, "http://x")
    assert info.live_strategies == 3 and info.schemas is None
    info.check_schema()  # a service that does not say is asked; it refuses a document it does not run


async def test_drafts_go_to_the_state_directory_by_default(service, tmp_path) -> None:
    result = await call("draft", *FLAGS[EXITS], "--allocation", "2000", "--expires", "7d")
    path = Path(result["file"])
    assert path.parent == tmp_path / "writer-state" / "workflows"
    assert path.name.startswith("BTC-price-") and path.read_text() == wf.canonical_json(result["document"])
    assert result["document"]["envelope"]["price_bound_bps"] == 25
    again = await call("draft", *FLAGS[EXITS], "--allocation", "2000", "--expires", "7d", "--out", str(path))
    assert "exists; pass --force" in again["error"]
    assert (
        await call("draft", *FLAGS[EXITS], "--allocation", "2000", "--expires", "7d", "--out", str(path), "--force")
    )["file"] == str(path)


NOW = 1792368000 + 37 * 60  # 2026-10-19T00:37:00Z


@pytest.mark.parametrize(
    ("spec", "trigger"),
    [
        ("price:btc:down:90000", {"kind": "price", "market": "BTCRUSDPERP", "direction": "down", "level": "90000"}),
        (
            "PRICE:ETHRUSDPERP:UP:3000.50",
            {"kind": "price", "market": "ETHRUSDPERP", "direction": "up", "level": "3000.5"},
        ),
        ("every:4h", {"kind": "every", "period_secs": 14400, "anchor": 1792368000}),
        ("every:30m@09:15", {"kind": "every", "period_secs": 1800, "anchor": 1792368000 + 9 * 3600 + 15 * 60}),
        ("every:1w@1793404800", {"kind": "every", "period_secs": 604800, "anchor": 1793404800}),
        ("every:1d@2026-10-31T00:00:00Z", {"kind": "every", "period_secs": 86400, "anchor": 1793404800}),
        ("at:daily@09:30", {"kind": "at", "cadence": "daily", "time": "09:30"}),
        ("at:fri@23:59", {"kind": "at", "cadence": "weekly", "day": "fri", "time": "23:59"}),
        ("start", {"kind": "start"}),
    ],
)
def test_each_trigger_spelling(spec: str, trigger: dict) -> None:
    assert wf.parse_trigger(spec, now=NOW) == trigger


@pytest.mark.parametrize(
    ("spec", "error"),
    [
        ("price:btc:sideways:90000", "direction is up or down"),
        ("price:btc:down", "takes price:SYMBOL:up|down:LEVEL"),
        ("price:btc:down:-1", "must be positive"),
        ("price:btc:down:1.0000000001", "more than 9 decimal places"),
        ("every:4m", "outside 5 minutes to 90 days"),
        ("every:91d", "outside 5 minutes to 90 days"),
        ("every:1.5h", "a whole number of m, h, d or w"),
        ("every:1h@25:00", "the anchor is HH:MM"),
        ("every:1h@2026-10-31T00:00:00", "needs a time zone"),
        ("at:daily@9:30", "HH:MM in UTC"),
        ("at:someday@09:30", "daily or a day of the week"),
        ("when:btc", "--trigger takes"),
        ("start:now", "--trigger takes"),
    ],
)
def test_a_trigger_that_does_not_parse_says_how_to_spell_it(spec: str, error: str) -> None:
    with pytest.raises(wf.WorkflowError, match=re.escape(error)):
        wf.parse_trigger(spec, now=NOW)


def test_orders_resting_orders_and_exits_spell_out_every_field() -> None:
    assert wf.parse_order("eth:sell:1000:3300") == {
        "type": "immediate",
        "market": "ETHRUSDPERP",
        "side": "sell",
        "size": {"usd": "1000"},
        "stop": "3300",
    }
    assert wf.parse_order("btc:BUY:25%") == {
        "type": "immediate",
        "market": "BTCRUSDPERP",
        "side": "buy",
        "size": {"weight": "0.25"},
    }, "an absent stop is left out"
    assert wf.parse_order("btc:buy:33.3333%")["size"] == {"weight": "0.333333"}
    assert wf.parse_resting("eth:buy:200:+25bps:2800", post_only=True) == {
        "type": "resting",
        "market": "ETHRUSDPERP",
        "side": "buy",
        "size": {"usd": "200"},
        "price": {"offset_bps": 25},
        "stop": "2800",
    }, "post-only is the default, so it is left out"
    resting = wf.parse_resting("eth:sell:50%:3000.25", post_only=False)
    assert resting["price"] == {"fixed": "3000.25"} and resting["post_only"] is False and "stop" not in resting
    assert wf.parse_exits(["after:90m", "target:eth:down:2700"]) == {
        "after_secs": 5400,
        "target": {"market": "ETHRUSDPERP", "direction": "down", "level": "2700"},
    }
    assert (wf.parse_exits(["profit:7.5%"]) or {})["profit_pct"] == "7.5"
    assert wf.parse_exits([]) is None


@pytest.mark.parametrize(
    ("parse", "error"),
    [
        (lambda: wf.parse_order("eth:sell"), "--order takes SYMBOL:buy|sell:USD|WEIGHT%[:STOP[:TAKE_PROFIT]]"),
        (lambda: wf.parse_order("eth:sell:100:3300:"), "--order takes"),
        (lambda: wf.parse_order("eth:sell:100:3300:2700:1"), "--order takes"),
        (lambda: wf.parse_order("eth:sell:100::"), "--order takes"),
        (lambda: wf.parse_order("eth:short:100"), "the side is buy or sell"),
        (lambda: wf.parse_order("eth:sell:0"), "must be positive"),
        (lambda: wf.parse_order("eth:sell:120%"), "at most 100%"),
        (lambda: wf.parse_order("eth:sell:100:"), "--order takes"),
        (lambda: wf.parse_resting("eth:sell:100", True), "--resting takes"),
        (lambda: wf.parse_resting("eth:sell:100:0bps", True), "not 0"),
        (lambda: wf.parse_resting("eth:sell:100:-6000bps", True), "-5000bps to 5000bps"),
        (lambda: wf.parse_exits(["profit:5", "profit:6"]), "given twice"),
        (lambda: wf.parse_exits(["after:30s"]), "a whole number of m, h, d or w"),
        (lambda: wf.parse_exits(["after:91d"]), "outside 1 minute to 90 days"),
        (lambda: wf.parse_exits(["stop:3000"]), "--exit takes"),
    ],
)
def test_a_flag_that_does_not_parse_says_how_to_spell_it(parse, error: str) -> None:
    with pytest.raises(wf.WorkflowError, match=re.escape(error)):
        parse()


def params(**changes) -> dict:
    values: dict[str, Any] = dict(
        environment="devnet1",
        account_id=ACCOUNT,
        allocation=wf.Decimal("2000"),
        expires_at=1793491200,
        bound_bps=25,
        max_resting=20,
        trigger={"kind": "price", "market": "ETHRUSDPERP", "direction": "up", "level": "3000"},
        conditions=[],
        orders=[wf.parse_order("eth:sell:1000:3300")],
        total=None,
        exits=None,
        repeat="once",
    )
    return wf.basket_params(**(values | changes))


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"orders": []}, "at least one --order or --resting"),
        ({"orders": [wf.parse_order("eth:sell:50%")]}, "pass --total USD"),
        ({"total": wf.Decimal("100")}, "--total goes with weights"),
        ({"repeat": "each", "exits": {"profit_pct": "5"}}, "takes no --exit"),
        ({"repeat": "each", "trigger": {"kind": "start"}}, "needs a price or schedule trigger"),
        (
            {
                "orders": [wf.parse_order("eth:sell:50%"), wf.parse_resting("btc:buy:50%:80000", post_only=True)],
                "total": wf.Decimal("100"),
            },
            "A weighted entry is all --order or all --resting orders",
        ),
        (
            {"orders": [wf.parse_order("eth:sell:50%"), wf.parse_order("btc:buy:500")], "total": wf.Decimal("100")},
            "every one a weight of --total",
        ),
    ],
)
def test_the_kit_refuses_what_it_can_say_more_plainly(changes: dict, error: str) -> None:
    with pytest.raises(wf.WorkflowError, match=re.escape(error)):
        params(**changes)


def test_an_order_takes_a_take_profit_beside_its_stop() -> None:
    assert wf.parse_order("eth:buy:1000:2800:3200") == {
        "type": "immediate",
        "market": "ETHRUSDPERP",
        "side": "buy",
        "size": {"usd": "1000"},
        "stop": "2800",
        "take_profit": "3200",
    }
    alone = wf.parse_order("eth:buy:1000::3200")
    assert alone["take_profit"] == "3200" and "stop" not in alone, "an empty stop leaves only the take-profit"
    resting = wf.parse_resting("eth:sell:1000:3000::2700", post_only=True)
    assert resting["take_profit"] == "2700" and resting["price"] == {"fixed": "3000"} and "stop" not in resting


def test_the_expansion_check_expects_the_templates_take_profit_and_on_stop() -> None:
    case = CASES[THREE_EXITS]
    params = {"price_bound_bps": 25, "max_resting_orders": 20} | case["request"]["params"]
    doc = json.loads(case["canonical"])
    assert doc["actions"]["entry"]["orders"][0]["take_profit"] == "3200", "the exit target on the order's own market"
    without = copy.deepcopy(doc)
    del without["actions"]["entry"]["orders"][0]["take_profit"]
    with pytest.raises(wf.WorkflowError, match="changes order o1's take-profit"):
        wf.check_expansion(params, without)
    # The order's own take-profit wins over the target's.
    own = copy.deepcopy(params)
    own["entry"]["orders"][0]["take_profit"] = "3100"
    with pytest.raises(wf.WorkflowError, match="take-profit"):
        wf.check_expansion(own, doc)
    # A buy and a sell hedge each other: a fired stop closes the other legs unless the draft says otherwise.
    hedge = {"price_bound_bps": 25, "max_resting_orders": 20} | CASES[READING_A]["request"]["params"]
    hedged = json.loads(CASES[READING_A]["canonical"])
    assert hedged["actions"]["entry"]["on_stop"] == "close_all"
    wf.check_expansion(hedge, hedged)
    with pytest.raises(wf.WorkflowError, match="what a fired stop does"):
        wf.check_expansion(hedge | {"on_stop": "keep_others"}, hedged)
    assert wf.default_on_stop(hedge["entry"]["orders"]) == "close_all"
    assert wf.default_on_stop(params["entry"]["orders"]) == "keep_others"


def test_on_stop_is_sent_only_when_the_draft_says() -> None:
    parser = workflow.build_parser()
    base = ["draft", "--trigger", "start", "--order", "btc:buy:50", "--allocation", "100", "--expires", "7d"]
    assert parser.parse_args(base).on_stop is None
    assert wf.ON_STOP[parser.parse_args(base + ["--on-stop", "keep-others"]).on_stop] == "keep_others"
    with pytest.raises(SystemExit):
        parser.parse_args(base + ["--on-stop", "keep"])
    assert "on_stop" not in params()
    assert params(on_stop="close_all")["on_stop"] == "close_all"


async def test_the_balance_condition_needs_a_service_that_reads_the_balance(service, tmp_path) -> None:
    flags = ["--if", "balance:above:500"]
    refused = await draft(tmp_path, READING_A, *flags)
    assert "does not read the account's balance yet" in refused["error"]
    assert not [p for _, p, _ in service.requests if p == "/v1/validate"], "nothing was asked to expand"
    service.prices_margins = True
    asked = await draft(tmp_path, READING_A, *flags)
    (sent,) = [body["params"] for _, p, body in service.requests if p == "/v1/validate"]
    assert sent["if"] == [{"kind": "available_balance", "op": "above", "level": "500"}]
    assert asked["errors"][0]["code"] == "fake_no_example", "the fake has no example with it; the service decides"
    assert (await call("info"))["reads_balance"] is True


async def test_the_balance_warnings_come_from_the_service_once_it_prices_margins(
    service, tmp_path, monkeypatch
) -> None:
    path = await drafted(tmp_path)
    monkeypatch.setattr(workflow, "available_balance", balance_of("1500"))
    assert (await call("preview", "--file", path))["balance_warnings_from"] == "kit"
    service.prices_margins = True
    preview = await call("preview", "--file", path)
    assert preview["balance_warnings_from"] == "service" and kit_warnings(preview) == {}
    assert preview["available_balance_usd"] is None, "the kit reads no balance then: never both"


async def test_a_price_move_exit_shows_its_dollars(service, tmp_path) -> None:
    preview = await call("preview", "--file", await drafted(tmp_path, case=THREE_EXITS))
    (exit_,) = preview["profit_exits"]
    assert exit_["level_pct"] == "10" and exit_["profit_usd"] == "100"
    assert exit_["text"] == (
        "exit at a +10% price move, about +$100.00 on $1,000.00 of entry (unleveraged, before fees and funding)"
    )
    started = await call("start", "--file", await drafted(tmp_path, case=THREE_EXITS, name="again.json"))
    run = service.runs[started["run_id"]]
    run["profit"] = {"pct": "2.5", "usd": "25", "entry_usd": "1000"}
    run["profit_exits"][0] |= {"current_usd": "25", "target_usd": "100"}
    status = await call("status", started["run_id"])
    assert status["profit"]["text"] == (
        "a +2.5% price move (+$25.00) over all its markets, on $1,000.00 of positions, unleveraged, before fees and "
        "funding"
    )
    assert status["profit_exits"][0]["text"] == (
        "exit at a +10% price move, about +$100.00; the move now is +$25.00 (unleveraged, before fees and funding)"
    )
    assert "a +2.5% price move (+$25.00) so far, before fees and funding" in status["summary"]


def test_no_price_move_while_the_strategy_holds_nothing() -> None:
    view = wf.status_view(run_status(profit={"pct": None, "usd": None, "entry_usd": None}), [])
    assert view["profit"]["text"].startswith("no price move to show") and "price move" not in view["summary"]


def test_a_completed_strategy_keeps_its_take_profit_and_says_so() -> None:
    take_profit = {"state": "armed", "trigger_px": "3200", "market": "ETHRUSDPERP"}
    run = run_status(
        state="completed",
        completion={"reason": "expired", "at": "1792368120"},
        markets=[market_row("ETHRUSDPERP", take_profit=take_profit)],
    )
    assert (
        "its take-profit on ETH at 3,200 stays on the position until it fires or the strategy is stopped"
        in wf.status_view(run, [])["summary"]
    )


def test_a_protection_event_names_which_one() -> None:
    armed = event(
        5, "protection", "armed", "take-profit armed at 3,200", market="ETHRUSDPERP", protection_kind="take_profit"
    )
    view = wf.status_view(run_status(), [armed])
    assert view["recent_events"][0]["protection_kind"] == "take_profit"
    assert view["recent_events"][0]["market"] == "ETHRUSDPERP"


@pytest.mark.parametrize("code", ["skip_margin", "skip_take_profit_crossed"])
def test_the_new_skips_are_named(code: str) -> None:
    view = wf.status_view(run_status(), [event(1, "skipped", code, "skipped")])
    assert view["skips"][0]["reason"] == wf.SKIP_REASONS[code]


async def test_a_strategy_lives_up_to_90_days(service, tmp_path) -> None:
    assert "at most 90 days ahead" in (await draft(tmp_path, READING_A, "--expires", "91d"))["error"]
    assert (await draft(tmp_path, READING_A, "--expires", "90d"))["file"]
    assert wf.parse_trigger("every:90d", now=NOW)["period_secs"] == 90 * 86400
    assert wf.parse_exits(["after:90d"]) == {"after_secs": 90 * 86400}


def test_an_absent_optional_parameter_is_left_out() -> None:
    assert params() == {
        "environment": "devnet1",
        "account_id": str(ACCOUNT),
        "allocation_usd": "2000",
        "expires_at": 1793491200,
        "price_bound_bps": 25,
        "max_resting_orders": 20,
        "trigger": {"kind": "price", "market": "ETHRUSDPERP", "direction": "up", "level": "3000"},
        "entry": {
            "orders": [
                {"type": "immediate", "market": "ETHRUSDPERP", "side": "sell", "size": {"usd": "1000"}, "stop": "3300"}
            ],
        },
        "repeat": "once",
    }
    weighted = params(
        orders=[wf.parse_order("eth:sell:50%"), wf.parse_order("btc:buy:50%")],
        total=wf.Decimal("100"),
        conditions=[wf.parse_condition("funding:eth:below:0.001")],
        exits=wf.parse_exits(["profit:5"]),
    )
    assert weighted["entry"]["total_usd"] == "100" and weighted["if"][0]["kind"] == "funding_avg"
    assert weighted["exit"] == {"profit_pct": "5"}
    assert wf.template_request(params())["template"] == {"id": "basket", "version": 1}


def test_draft_flags_parse_as_documented() -> None:
    parser = workflow.build_parser()
    base = ["draft", "--trigger", "start", "--allocation", "100", "--expires", "7d"]
    args = parser.parse_args(
        base + ["--order", "btc:buy:50", "--resting", "eth:buy:50:-10bps", "--if", "position:eth:below:1"]
    )
    assert (
        args.order == ["btc:buy:50"]
        and args.resting == ["eth:buy:50:-10bps"]
        and args.conditions == ["position:eth:below:1"]
    )
    assert args.post_only is True and args.repeat == "once" and args.max_resting == 20 and args.bound_bps == 25
    assert parser.parse_args(base + ["--no-post-only"]).post_only is False
    assert wf.REPEATS[parser.parse_args(base + ["--repeat", "each_crossing"]).repeat] == "each"
    for bad in (["--max-resting", "41"], ["--bound-bps", "5"], ["--repeat", "twice"], ["--symbol", "btc"]):
        with pytest.raises(SystemExit):
            parser.parse_args(base + bad)
    start = ["start", "--file", "f.json"]
    assert parser.parse_args(start + ["--label", "  ETH breakout "]).label == "ETH breakout"
    assert parser.parse_args(start).label is None
    for label in ("x" * 61, "line\nbreak", "   "):
        with pytest.raises(SystemExit):
            parser.parse_args(start + ["--label", label])


def test_parse_expiry_reads_each_spelling() -> None:
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
