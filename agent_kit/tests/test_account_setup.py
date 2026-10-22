"""Relay contract tests: real EIP-712 signatures/ABI bytes; only API I/O is stubbed."""

from types import SimpleNamespace

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from eth_abi import decode
from eth_account import Account
from eth_utils import keccak

from agent_kit.scripts import account
from agent_kit.scripts._account_relay import (
    CONTRACTS,
    RELAY_PATH,
    AccountRelay,
    SetupError,
    api_integer,
    relay_payload,
    transfer_message,
)
from agent_kit.scripts._writer import WriterLease, trading_resources

KEY = "0x" + "11" * 32
OWNER = Account.from_key(KEY).address
API = "https://api.reya.xyz/v2"
TX = "0x" + "ab" * 32
VECTORS = json.loads((Path(__file__).parent / "fixtures/account_relay_vectors.json").read_text())["vectors"]


def config(api_url=API, chain_id=1729):
    return SimpleNamespace(api_url=api_url, chain_id=chain_id, owner_wallet_address=OWNER)


def args(*values):
    return account.build_parser().parse_args(list(values))


def transfer_args(request_id="fund-1"):
    return args(
        "transfer",
        "--from-account",
        "10",
        "--to-account",
        "11",
        "--amount",
        "1.123456",
        "--execute",
        "--request-id",
        request_id,
    )


def ledger():
    return [
        dict(
            sequenceNumber=80,
            accountId=10,
            counterpartyAccountId=11,
            asset="RUSD",
            amount="-1.123456",
            type="TRANSFER",
            transactionHash=TX,
        ),
        dict(
            sequenceNumber=81,
            accountId=11,
            counterpartyAccountId=10,
            asset="RUSD",
            amount="1.123456",
            type="TRANSFER",
            transactionHash=TX,
        ),
    ]


@pytest.fixture
def http(monkeypatch, tmp_path):
    monkeypatch.setattr(account, "get_trading_config", lambda **kwargs: config())
    key = tmp_path / "owner.key"
    key.write_text(KEY)
    key.chmod(0o600)
    monkeypatch.setenv("REYA_ACCOUNT_OWNER_KEY_FILE", str(key))
    state = SimpleNamespace(
        accounts=[
            {"accountId": 10, "type": "MAINPERP"},
            {"accountId": 11, "type": "SUBPERP"},
            {"accountId": 900, "type": "SPOT"},
        ],
        nonce=7,
        rows=ledger(),
        response={"txHash": TX, "txSubmitted": True, "coreSigNonce": "8"},
        failure=None,
        before_post=None,
        calls=[],
        pages=None,
    )

    async def request(client, method, path, **kwargs):
        state.calls.append((method, path, kwargs))
        if method == "POST":
            assert path == RELAY_PATH
            # Must be durable BEFORE an external request. No keys, signatures or calldata.
            files = list((tmp_path / "writer-state").glob("*.json"))
            states = [json.loads(p.read_text()) for p in files]
            saved = next(s for s in states if s.get("pending_setup"))
            assert saved["setup_operations"][saved["pending_setup"]]["calldata_hash"].startswith("0x")
            stored = json.dumps(states)
            assert KEY not in stored and kwargs["json"]["txData"]["data"] not in stored
            if state.before_post:
                state.before_post()
            if state.failure:
                raise state.failure
            return state.response
        if path.endswith("/accounts"):
            return state.accounts
        if path.endswith("/metadata"):
            return {"address": OWNER, "coreSigNonce": state.nonce}
        if path.endswith("/transfers"):
            assert kwargs["params"]["type"] == "TRANSFER"
            if state.pages is not None:
                return state.pages[kwargs["params"].get("cursor", "")]
            return {"data": state.rows, "meta": {"count": len(state.rows), "limit": 100}}
        pytest.fail("Unexpected endpoint: " + path)

    monkeypatch.setattr(AccountRelay, "request", request)
    return state


def posts(http):
    return [c for c in http.calls if c[0] == "POST"]


@pytest.mark.parametrize(
    "amount,units", [("0.000001", 1), ("1.123456", 1123456), ("1000000000000000000000000000000", 10**36)]
)
def test_exact_six_decimal_scaling(amount, units):
    assert account.transfer_units(Decimal(amount)) == units


@pytest.mark.parametrize("amount", ["0", "-1", "0.0000001", "NaN", "Infinity", "1e100"])
def test_invalid_transfer_amount(amount):
    with pytest.raises(SetupError):
        account.transfer_units(Decimal(amount))


@pytest.mark.parametrize("vector", VECTORS)
def test_signature_and_calldata_match_independent_ethers_vectors(vector):
    cfg = config(vector["api_url"], vector["chain_id"])
    plan = dict(
        action="transfer",
        owner=OWNER,
        from_account=10,
        to_account=11,
        amount_units="1123456",
        nonce="8",
        deadline=2000000060,
    )
    message = transfer_message(cfg, 10, 11, 1123456, 8, 2000000060)
    assert "0x" + keccak(b"\x19" + message.version + message.header + message.body).hex() == vector["digest"]
    payload = relay_payload(cfg, plan, Account.from_key(KEY))
    assert payload["txData"]["data"] == vector["calldata"]
    assert payload["contractAddress"].lower() == CONTRACTS[cfg.api_url][1].lower()
    assert Account.recover_message(message, signature=Account.from_key(KEY).sign_message(message).signature) == OWNER
    create = relay_payload(cfg, dict(action="create", owner=OWNER))
    assert create["txData"]["data"] == vector["create_calldata"]
    assert create["contractAddress"] == CONTRACTS[cfg.api_url][0]


@pytest.mark.parametrize("action", ["create", "transfer"])
async def test_preview_never_reads_key_or_posts(http, monkeypatch, action):
    monkeypatch.setattr(account, "read_private_key_file", lambda _: pytest.fail("preview loaded key"))
    command = (
        args("create")
        if action == "create"
        else args("transfer", "--from-account", "10", "--to-account", "11", "--amount", "1.123456")
    )
    result = await account.run(command)
    assert result["preview"] and result["gas_paid_by"] == "reya_executor"
    assert result["requires_owner_signature"] == (action == "transfer")
    assert "gas_limit" not in result and "max_gas_cost_native" not in result
    assert not posts(http)


async def test_create_needs_no_key_and_resolves_margin_not_first_spot(http, monkeypatch):
    monkeypatch.delenv("REYA_ACCOUNT_OWNER_KEY_FILE")
    monkeypatch.setattr(account, "read_private_key_file", lambda _: pytest.fail("create loaded key"))
    http.accounts = []
    http.response = {
        "txHash": TX,
        "txSubmitted": True,
        "accountId": "900",
        "accounts": [{"account_id": "42"}, {"account_id": "900"}],
    }
    http.before_post = lambda: setattr(
        http, "accounts", [{"accountId": 42, "type": "MAINPERP"}, {"accountId": 900, "type": "SPOT"}]
    )
    result = await account.run(args("create", "--execute", "--request-id", "create-1"))
    assert result["status"] == "confirmed" and result["account_id"] == 42
    payload = posts(http)[0][2]["json"]
    assert set(payload) == {"txData", "contractAddress", "metadata"}
    assert len(bytes.fromhex(payload["txData"]["data"][2:])) == 36
    assert decode(["address"], bytes.fromhex(payload["txData"]["data"][10:]))[0] == OWNER.lower()
    assert not any(c[1].endswith("/metadata") for c in http.calls)
    again = await account.run(args("create", "--execute", "--request-id", "create-1"))
    assert again == result and len(posts(http)) == 1


@pytest.mark.parametrize("scenario", ["indexing", "foreign", "existing", "ambiguous", "spot-only"])
async def test_create_never_guesses_account_from_wallet_difference(http, scenario):
    http.response = {"txHash": TX, "txSubmitted": True, "accountId": "42"}
    if scenario == "existing":
        http.response["accountId"] = "10"
    if scenario == "ambiguous":
        http.response["accounts"] = [{"account_id": "42"}, {"account_id": "43"}]
        http.before_post = lambda: http.accounts.extend(
            [{"accountId": 42, "type": "SUBPERP"}, {"accountId": 43, "type": "SUBPERP"}]
        )
    if scenario == "spot-only":
        http.before_post = lambda: http.accounts.append({"accountId": 42, "type": "SPOT"})
    result = await account.run(args("create", "--execute", "--request-id", "new-account"))
    assert result["status"] == "unknown"
    if scenario == "indexing":
        http.accounts.append({"accountId": 42, "type": "SUBPERP"})
        result = await account.run(args("status"))
        assert result["status"] == "confirmed" and result["account_id"] == 42
    assert len(posts(http)) == 1


async def test_signed_transfer_and_exact_ledger_pair(http, monkeypatch):
    monkeypatch.setattr(account.time, "time", lambda: 2000000000)
    result = await account.run(transfer_args())
    assert result["status"] == "confirmed" and result["amount_units"] == "1123456"
    assert result["transfer_sequence_numbers"] == [80, 81]
    assert posts(http)[0][2]["json"]["txData"]["data"] == VECTORS[0]["calldata"]
    assert await account.run(transfer_args()) == result
    assert await account.run(args("status", "--request-id", "fund-1")) == result
    assert len(posts(http)) == 1


@pytest.mark.parametrize(
    "change",
    ["same-account", "foreign-account", "spot-account", "delegate", "missing-key", "unsafe-key", "nonce-overflow"],
)
async def test_transfer_preconditions_prevent_any_post(http, monkeypatch, tmp_path, change):
    command = transfer_args()
    if change == "same-account":
        command.to_account = 10
    if change == "foreign-account":
        http.accounts = http.accounts[:1]
    if change == "spot-account":
        command.to_account = 900
    if change == "delegate":
        monkeypatch.setattr(account, "read_private_key_file", lambda _: "0x" + "22" * 32)
    if change == "missing-key":
        monkeypatch.delenv("REYA_ACCOUNT_OWNER_KEY_FILE")
    if change == "unsafe-key":
        (tmp_path / "owner.key").chmod(0o644)
    if change == "nonce-overflow":
        http.nonce = 2**53 - 1
    with pytest.raises(ValueError):
        await account.run(command)
    assert not posts(http)


@pytest.mark.parametrize(
    "change", ["amount", "asset", "direction", "account", "counterparty", "hash", "type", "one-sided"]
)
async def test_inexact_or_unrelated_ledger_cannot_confirm(http, change):
    if change == "one-sided":
        http.rows.pop()
    else:
        key, value = {
            "amount": ("amount", "1.123455"),
            "asset": ("asset", "ETH"),
            "direction": ("amount", "-1.123456"),
            "account": ("accountId", 12),
            "counterparty": ("counterpartyAccountId", 12),
            "hash": ("transactionHash", "0x" + "cd" * 32),
            "type": ("type", "DEPOSIT"),
        }[change]
        http.rows[1][key] = value
    result = await account.run(transfer_args())
    assert result["status"] == "unknown"
    assert (await account.run(transfer_args("another-id")))["status"] == "unknown"
    assert len(posts(http)) == 1


async def test_reconciliation_walks_cursors_and_deduplicates_entries(http):
    pair = ledger()
    http.pages = {
        "": {"data": [pair[0]], "meta": {"nextCursor": "page-2"}},
        "page-2": {"data": [pair[0], pair[1]], "meta": {}},
    }
    assert (await account.run(transfer_args()))["status"] == "confirmed"
    assert len([c for c in http.calls if c[1].endswith("/transfers")]) == 2


@pytest.mark.parametrize("kind", ["transfer", "create"])
async def test_lost_response_is_never_replayed_even_with_new_request_id(http, kind):
    http.failure = TimeoutError("unknown send outcome")
    command = transfer_args() if kind == "transfer" else args("create", "--execute", "--request-id", "create-1")
    assert (await account.run(command))["status"] == "unknown"
    assert (await account.run(command))["status"] == "unknown"
    command.request_id = "new-id"
    assert (await account.run(command))["status"] == "unknown"
    if kind == "transfer":
        # Untimestamped ledger rows cannot be placed after the request: no unique observation.
        http.nonce = 8
        status = await account.run(args("status"))
        assert status["status"] == "unknown" and "API gap" in status["error"]
        status = await account.run(args("status", "--tx-hash", TX))
        assert status["status"] == "confirmed"
    else:
        with pytest.raises(SetupError, match="API gap"):
            await account.run(args("status", "--tx-hash", TX))
        # Exactly one new margin account after the request confirms it, labelled as an observation.
        http.accounts.append({"accountId": 42, "type": "SUBPERP"})
        status = await account.run(args("status"))
        assert status["status"] == "confirmed" and status["account_id"] == 42
        assert status["verification"] == "unique_observation_after_request"
    assert len(posts(http)) == 1


async def test_lagging_indexer_can_be_reconciled_without_key_or_resubmission(http, monkeypatch):
    http.rows = []
    result = await account.run(transfer_args())
    assert result["status"] == "unknown" and result["tx_hash"] == TX
    http.rows = ledger()
    monkeypatch.delenv("REYA_ACCOUNT_OWNER_KEY_FILE")
    assert (await account.run(args("status")))["status"] == "confirmed"
    assert len(posts(http)) == 1


@pytest.mark.parametrize(
    "response,status",
    [
        ({"txHash": TX, "txSubmitted": True, "errorCode": "TRANSACTION_REVERTED"}, "reverted"),
        ({"txHash": None, "txSubmitted": False, "errorCode": "TRANSACTION_FAILED_TO_SEND"}, "rejected"),
        ({"txHash": None, "txSubmitted": False, "errorMessage": "Generic server catch"}, "unknown"),
        ({"txHash": TX, "txSubmitted": True, "errorCode": "RECEIPT_NOT_FOUND"}, "confirmed"),
        ({"txHash": TX}, "unknown"),
    ],
)
async def test_relay_response_is_not_mistaken_for_verified_success(http, response, status):
    http.response = response
    result = await account.run(transfer_args())
    assert result["status"] == status
    if response.get("txHash"):
        assert result["tx_hash"] == TX  # Retain even a partially malformed response's usable reference.
    assert len(posts(http)) == 1


async def test_request_id_cannot_be_rebound_to_other_parameters(http):
    await account.run(transfer_args())
    command = transfer_args()
    command.amount = Decimal("2")
    with pytest.raises(SetupError, match="different parameters"):
        await account.run(command)
    assert len(posts(http)) == 1


async def test_missing_request_id_and_live_runner_block_setup(http):
    with pytest.raises(SetupError, match="request-id"):
        await account.run(args("create", "--execute"))
    with WriterLease(trading_resources(config(), OWNER)):
        with pytest.raises(ValueError, match="Another kit writer"):
            await account.run(transfer_args())
    assert not posts(http)


async def test_legacy_pending_transaction_is_not_silently_discarded(http):
    with WriterLease([f"rpc:1729:{OWNER.lower()}", *trading_resources(config(), OWNER)]) as lease:
        lease.state["pending_transaction"] = {"tx_hash": TX}
        lease.save()
    with pytest.raises(SetupError, match="older setup"):
        await account.run(args("create", "--execute", "--request-id", "new-id"))
    assert not posts(http)


@pytest.mark.parametrize("value", [True, 1.0, -1, 2**53, "9007199254740992", "1e2"])
def test_api_numbers_must_be_lossless(value):
    with pytest.raises(SetupError):
        api_integer(value)


@pytest.mark.parametrize("url,chain", [(API, 89346162), ("https://example.invalid/v2", 1729)])
def test_unsupported_deployment_has_no_fallback(url, chain):
    with pytest.raises(SetupError):
        AccountRelay(config(url, chain))


@pytest.mark.parametrize("flag", ["--max-gas-cost", "--rpc-url"])
def test_rpc_and_gas_options_are_removed(flag, capsys):
    with pytest.raises(SystemExit):
        args("create", flag, "1")
    assert "unrecognized arguments" in capsys.readouterr().out


async def test_transport_posts_once_without_redirects_or_rpc(monkeypatch):
    response = SimpleNamespace(
        status=200,
        content=SimpleNamespace(
            readexactly=AsyncMock(
                side_effect=asyncio.IncompleteReadError(b'{"txSubmitted":true,"txHash":null}', 1024 * 1024 + 1)
            )
        ),
    )

    class Context:
        async def __aenter__(self):
            return response

        async def __aexit__(self, *exc):
            pass

    calls = []

    class Session:
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
            return Context()

    relay = AccountRelay(config())
    monkeypatch.setattr(relay, "session", Session(), raising=False)
    body = relay_payload(config(), {"action": "create", "owner": OWNER})
    await relay.submit(body)
    assert calls == [
        (("POST", "https://api.reya.xyz/api/transaction-gelato/executeReya"), {"allow_redirects": False, "json": body})
    ]
    response.status = 503
    with pytest.raises(SetupError, match="HTTP 503"):
        await relay.submit(body)
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["missing", "malformed", "cycle", "page-limit"])
async def test_unavailable_or_bounded_history_never_unlocks_an_unknown_write(http, failure):
    if failure == "missing":
        http.pages = {"": {"error": "route unavailable"}}
    elif failure == "malformed":
        http.pages = {"": {"data": [], "meta": {"nextCursor": 123}}}
    elif failure == "cycle":
        http.pages = {
            "": {"data": [], "meta": {"nextCursor": "same"}},
            "same": {"data": [], "meta": {"nextCursor": "same"}},
        }
    else:
        http.pages = {("" if n == 0 else str(n)): {"data": [], "meta": {"nextCursor": str(n + 1)}} for n in range(11)}
    result = await account.run(transfer_args())
    assert result["status"] == "unknown"
    assert (await account.run(transfer_args("retry-with-new-id")))["status"] == "unknown"
    assert len(posts(http)) == 1
    assert len([c for c in http.calls if c[1].endswith("/transfers")]) <= 10


@pytest.mark.parametrize(
    "payload",
    [
        {"address": "0x" + "22" * 20, "coreSigNonce": 7},
        {"address": OWNER, "coreSigNonce": "9007199254740992"},
        {"address": OWNER, "coreSigNonce": 1.5},
        {"coreSigNonce": 0},
    ],
)
async def test_bad_nonce_response_prevents_signing(payload, monkeypatch):
    relay = AccountRelay(config())
    monkeypatch.setattr(relay, "request", AsyncMock(return_value=payload))
    with pytest.raises(SetupError):
        await relay.nonce()


async def test_known_hash_cannot_be_replaced_during_reconciliation(http):
    http.rows = []
    await account.run(transfer_args())
    with pytest.raises(SetupError, match="conflicts"):
        await account.run(args("status", "--tx-hash", "0x" + "cd" * 32))
    assert len(posts(http)) == 1


async def test_status_requires_original_deployment(http, monkeypatch):
    http.rows = []
    monkeypatch.setattr(
        account, "get_trading_config", lambda **kwargs: config("https://api-devnet.reya-cronos.network/v2", 89346162)
    )
    await account.run(transfer_args())
    # Devnet/Cronos share a chain ID and owner lease, but not a relay deployment.
    monkeypatch.setattr(
        account, "get_trading_config", lambda **kwargs: config("https://api-cronos.reya.xyz/v2", 89346162)
    )
    with pytest.raises(SetupError, match="original API deployment"):
        await account.run(args("status"))
    assert len(posts(http)) == 1


async def test_default_status_lists_all_local_unresolved_requests(http, tmp_path):
    from copy import deepcopy

    await account.run(transfer_args("done"))
    http.failure = TimeoutError()
    await account.run(transfer_args("lost"))
    journal = next(
        path for path in (tmp_path / "writer-state").glob("*.json") if json.loads(path.read_text()).get("pending_setup")
    )
    state = json.loads(journal.read_text())
    # A historical unresolved record must not disappear behind the latest pointer.
    old = deepcopy(state["setup_operations"]["lost"])
    old["request_id"] = "older-unknown"
    state["setup_operations"]["older-unknown"] = old
    journal.write_text(json.dumps(state))
    http.calls.clear()
    result = await account.run(args("status"))
    assert {r["request_id"] for r in result["pending_requests"]} == {"lost", "older-unknown"}
    assert all(r["status"] == "unknown" for r in result["pending_requests"])
    assert not any(call[0] == "POST" for call in http.calls)
    targeted = await account.run(args("status", "--request-id", "lost"))
    assert "pending_requests" not in targeted  # targeted response remains unchanged


async def test_transfer_sequence_ids_are_opaque(http):
    http.rows[0]["sequenceNumber"] = 93
    http.rows[1]["sequenceNumber"] = 802
    result = await account.run(transfer_args())
    assert result["status"] == "confirmed"
    assert result["transfer_sequence_numbers"] == [93, 802]


async def test_second_equal_transfer_on_later_page_keeps_request_unknown(http):
    pair = ledger()
    other = [{**row, "sequenceNumber": row["sequenceNumber"] + 10} for row in pair]
    http.pages = {"": {"data": pair, "meta": {"nextCursor": "p2"}}, "p2": {"data": other, "meta": {}}}
    assert (await account.run(transfer_args()))["status"] == "unknown"
    assert len(posts(http)) == 1


async def test_matching_pair_does_not_confirm_if_rest_of_history_is_unavailable(http):
    pair = ledger()
    http.pages = {"": {"data": pair, "meta": {"nextCursor": "missing"}}}
    assert (await account.run(transfer_args()))["status"] == "unknown"
    assert len(posts(http)) == 1


async def test_read_only_mode_prevents_unsigned_creation(http, monkeypatch):
    monkeypatch.setenv("REYA_MODE", "read-only")
    with pytest.raises(ValueError, match="read-only"):
        await account.run(args("create", "--execute", "--request-id", "readonly"))
    assert not posts(http)


async def test_hash_confirming_one_request_cannot_confirm_another_equal_transfer(http):
    assert (await account.run(transfer_args("fund-1")))["status"] == "confirmed"
    http.failure = TimeoutError("reply lost after the relay accepted it")
    assert (await account.run(transfer_args("fund-2")))["status"] == "unknown"
    with pytest.raises(SetupError, match="already recorded for request fund-1"):
        await account.run(args("status", "--request-id", "fund-2", "--tx-hash", TX))
    assert (await account.run(args("status", "--request-id", "fund-2")))["status"] == "unknown"
    assert len(posts(http)) == 2


async def test_unverified_operator_hash_is_not_saved_and_the_correct_hash_still_verifies(http):
    http.failure = TimeoutError("reply lost after the relay accepted it")
    assert (await account.run(transfer_args()))["status"] == "unknown"
    wrong = "0x" + "cd" * 32
    result = await account.run(args("status", "--request-id", "fund-1", "--tx-hash", wrong))
    assert result["status"] == "unknown" and result["tx_hash"] is None and result["supplied_tx_hash"] == wrong
    assert "not saved" in result["error"]
    confirmed = await account.run(args("status", "--request-id", "fund-1", "--tx-hash", TX))
    assert confirmed["status"] == "confirmed" and confirmed["tx_hash"] == TX
    assert len(posts(http)) == 1


def timed_ledger(pairs):
    """Debit/credit rows for each (tx_hash, amount) placed well after any request in the test."""
    rows, seq = [], 200
    for tx_hash, amount in pairs:
        for account_id, counterparty, signed in ((10, 11, "-" + amount), (11, 10, amount)):
            seq += 1
            rows.append(
                dict(
                    sequenceNumber=seq,
                    accountId=account_id,
                    counterpartyAccountId=counterparty,
                    asset="RUSD",
                    amount=signed,
                    type="TRANSFER",
                    transactionHash=tx_hash,
                    timestamp=4102444800000 - seq,
                )
            )
    return rows


async def test_lost_transfer_reply_confirms_from_a_unique_observation(http):
    http.failure = TimeoutError("reply lost after the relay accepted it")
    http.rows = []
    assert (await account.run(transfer_args()))["status"] == "unknown"
    http.rows = timed_ledger([(TX, "1.123456")])
    result = await account.run(args("status", "--request-id", "fund-1"))
    assert result["status"] == "confirmed" and result["tx_hash"] == TX
    assert result["verification"] == "unique_observation_after_request"
    assert result["transfer_sequence_numbers"] == [201, 202]
    assert len(posts(http)) == 1


async def test_equal_observed_transfers_stay_unknown(http):
    http.failure = TimeoutError("reply lost after the relay accepted it")
    http.rows = []
    assert (await account.run(transfer_args()))["status"] == "unknown"
    http.rows = timed_ledger([(TX, "1.123456"), ("0x" + "cd" * 32, "1.123456")])
    assert (await account.run(args("status", "--request-id", "fund-1")))["status"] == "unknown"
    assert len(posts(http)) == 1


async def test_observation_ignores_transfers_claimed_by_other_requests(http):
    http.rows = timed_ledger([(TX, "1.123456")])
    assert (await account.run(transfer_args("fund-1")))["status"] == "confirmed"
    http.failure = TimeoutError("reply lost after the relay accepted it")
    assert (await account.run(transfer_args("fund-2")))["status"] == "unknown"
    # Only fund-1's own transfer is visible: it is claimed, so fund-2 stays unknown.
    assert (await account.run(args("status", "--request-id", "fund-2")))["status"] == "unknown"
    other = "0x" + "ef" * 32
    http.rows = timed_ledger([(TX, "1.123456"), (other, "1.123456")])
    result = await account.run(args("status", "--request-id", "fund-2"))
    assert result["status"] == "confirmed" and result["tx_hash"] == other
    assert len(posts(http)) == 2


async def test_lost_create_reply_needs_exactly_one_new_margin_account(http):
    http.failure = TimeoutError("reply lost after the relay accepted it")
    assert (await account.run(args("create", "--execute", "--request-id", "create-1")))["status"] == "unknown"
    http.accounts += [{"accountId": 42, "type": "SUBPERP"}, {"accountId": 43, "type": "SUBPERP"}]
    assert (await account.run(args("status")))["status"] == "unknown"
    http.accounts = [a for a in http.accounts if a["accountId"] != 43] + [{"accountId": 901, "type": "SPOT"}]
    result = await account.run(args("status"))
    assert result["status"] == "confirmed" and result["account_id"] == 42
    assert len(posts(http)) == 1


async def test_resolved_request_refuses_a_different_supplied_hash(http):
    assert (await account.run(transfer_args()))["status"] == "confirmed"
    with pytest.raises(SetupError, match="already resolved with a different transaction hash"):
        await account.run(args("status", "--request-id", "fund-1", "--tx-hash", "0x" + "cd" * 32))
    assert (await account.run(args("status", "--request-id", "fund-1", "--tx-hash", TX)))["status"] == "confirmed"
    assert len(posts(http)) == 1
