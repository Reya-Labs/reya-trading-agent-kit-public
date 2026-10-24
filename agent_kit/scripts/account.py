"""Preview or relay account creation and signed rUSD transfers through Reya APIs."""

import argparse
import asyncio
import os
import re
import time
from decimal import Decimal

from eth_account import Account
from eth_utils import keccak

from agent_kit.scripts._account_relay import (
    MAX_API_INTEGER,
    PERP_TYPES,
    AccountRelay,
    SetupError,
    address,
    api_integer,
    relay_payload,
)
from agent_kit.scripts._common import JsonArgumentParser, output_error, output_result, positive_decimal_arg
from agent_kit.scripts._env import get_trading_config, read_private_key_file
from agent_kit.scripts._writer import WriterLease, trading_resources


def _account_id(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 16 or not 0 < int(value) <= MAX_API_INTEGER:
        raise argparse.ArgumentTypeError("Account ID must be positive and exactly representable by the current APIs")
    return int(value)


def _request_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise argparse.ArgumentTypeError("Request ID must use 1..80 letters/digits/_/-")
    return value


def _tx_hash(value: str) -> str:
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise argparse.ArgumentTypeError("Transaction hash must be 32 hexadecimal bytes")
    return value.lower()


def transfer_units(amount: Decimal) -> int:
    if not amount.is_finite() or amount <= 0 or amount.adjusted() > 71:
        raise SetupError("rUSD amount must be positive and fit uint256")
    digits = amount.as_tuple()
    exponent = int(digits.exponent)
    if exponent < -6:
        raise SetupError("rUSD amount must have at most six decimals")
    units = int("".join(str(d) for d in digits.digits)) * 10 ** (exponent + 6)
    if units >= 2**256:
        raise SetupError("rUSD amount must fit uint256")
    return units


def require_margin_accounts(accounts: dict, source: int, target: int):
    if source == target or any(accounts.get(x) not in PERP_TYPES for x in (source, target)):
        raise SetupError("Source and destination must be distinct margin accounts owned by the configured owner")


def request_plan(config, args) -> dict:
    plan = {
        "action": args.action,
        "api_url": config.api_url,
        "chain_id": config.chain_id,
        "owner": address(config.owner_wallet_address),
    }
    if args.action == "transfer":
        units = transfer_units(args.amount)
        whole, fraction = divmod(units, 10**6)
        plan.update(
            from_account=args.from_account,
            to_account=args.to_account,
            asset="RUSD",
            amount_units=str(units),
            amount_rusd=f"{whole}.{fraction:06d}",
        )
    return plan


def unknown(record: dict, message: str) -> dict:
    return {
        **record["plan"],
        "request_id": record["request_id"],
        "tx_hash": record.get("tx_hash"),
        "status": "unknown",
        "error": message,
    }


def relay_evidence(data) -> dict:
    """Retain only typed, public outcome fields, never calldata/signatures/headers."""
    if not isinstance(data, dict) or type(data.get("txSubmitted")) is not bool:
        raise SetupError("Relay did not return a valid submission outcome")
    result = {"txSubmitted": data["txSubmitted"], "has_error": bool(data.get("errorMessage") or data.get("errorCode"))}
    if data.get("txHash") is not None:
        result["tx_hash"] = _tx_hash(data["txHash"])
    if data.get("errorCode") in ("TRANSACTION_REVERTED", "TRANSACTION_FAILED_TO_SEND", "RECEIPT_NOT_FOUND"):
        result["error_code"] = data["errorCode"]
    ids = [data["accountId"]] if data.get("accountId") is not None else []
    accounts = data.get("accounts", [])
    if not isinstance(accounts, list) or len(accounts) > 1000:
        raise SetupError("Invalid relay account evidence")
    ids.extend(item["account_id"] for item in accounts)
    result["account_ids"] = sorted({api_integer(value) for value in ids})
    if data.get("coreSigNonce") is not None:
        result["core_nonce"] = api_integer(data["coreSigNonce"])
    return result


async def matching_transfer(relay, record: dict) -> list[int] | None:
    plan = record["plan"]
    cursor = None
    seen_cursors = set()
    debit: set[int] = set()
    credit: set[int] = set()
    seen_entries: dict[int, dict] = {}
    for _ in range(10):  # Bounded, cursor-based reads; exhaustion is never proof of nonexecution.
        page = await relay.transfers(cursor)
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("data"), list)
            or not isinstance(page.get("meta"), dict)
        ):
            raise SetupError("Invalid transfer ledger response")
        if len(page["data"]) > 100:
            raise SetupError("Transfer ledger page exceeds the supported limit")
        for entry in page["data"]:
            sequence = api_integer(entry["sequenceNumber"])
            if sequence in seen_entries and seen_entries[sequence] != entry:
                raise SetupError("Conflicting transfer ledger identity")
            seen_entries[sequence] = entry
            if entry.get("transactionHash", "").lower() != record["tx_hash"]:
                continue
            if entry.get("type") != "TRANSFER" or entry.get("asset") != "RUSD":
                continue
            amount = Decimal(entry["amount"])
            if not amount.is_finite() or amount == 0 or transfer_units(amount.copy_abs()) != int(plan["amount_units"]):
                continue
            sequence = api_integer(entry["sequenceNumber"])
            source, target = api_integer(entry["accountId"]), api_integer(entry["counterpartyAccountId"])
            if source == plan["from_account"] and target == plan["to_account"] and amount < 0:
                debit.add(sequence)
            if source == plan["to_account"] and target == plan["from_account"] and amount > 0:
                credit.add(sequence)
        if len(debit) > 1 or len(credit) > 1:
            raise SetupError("Multiple matching transfer legs; operator reconciliation is required")
        cursor = page["meta"].get("nextCursor")
        if cursor is None:
            # Opaque sequence IDs: do not infer parity/adjacency. Traverse all
            # available matching history before claiming a unique pair.
            return [next(iter(debit)), next(iter(credit))] if len(debit) == len(credit) == 1 else None
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in seen_cursors:
            raise SetupError("Invalid or repeated transfer ledger cursor")
        seen_cursors.add(cursor)
    return None


TERMINAL = ("confirmed", "reverted", "rejected")
# Ledger timestamps are block times; allow for a block landing in the same second as the journal write.
CLOCK_SKEW_MS = 2000
UNIQUE_OBSERVATION_NOTE = (
    "No relay reply: exactly one matching operation appeared after this request and no other request "
    "claims it. An identical operation by another client at any time after the request would be indistinguishable."
)


def _competing(record: dict, journal: dict) -> bool:
    """Another unresolved request with the same parameters makes any observation ambiguous."""
    return any(
        rid != record["request_id"]
        and op.get("request") == record.get("request")
        and op.get("result", {}).get("status") not in TERMINAL
        for rid, op in journal.items()
    )


async def observed_transfer(relay, record: dict, journal: dict) -> tuple[str, list[int]] | None:
    """The single unclaimed debit/credit pair for this exact transfer recorded after the request."""
    plan, since = record["plan"], record.get("created_at_ms")
    if since is None or _competing(record, journal):
        return None
    claimed = {op.get("tx_hash") for rid, op in journal.items() if rid != record["request_id"]}
    legs: dict[str, dict[str, list[int]]] = {}
    cursor, seen_cursors, previous, descending, reached = None, set(), None, True, False
    for _ in range(10):
        page = await relay.transfers(cursor)
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("data"), list)
            or not isinstance(page.get("meta"), dict)
        ):
            raise SetupError("Invalid transfer ledger response")
        for entry in page["data"]:
            if entry.get("type") != "TRANSFER" or entry.get("asset") != "RUSD":
                continue
            if entry.get("timestamp") is None:
                return None  # Cannot place an entry relative to the request.
            stamp = api_integer(entry["timestamp"])
            descending = descending and (previous is None or stamp <= previous)
            previous = stamp
            if stamp < since - CLOCK_SKEW_MS:
                reached = True
                continue
            amount = Decimal(entry["amount"])
            if not amount.is_finite() or amount == 0 or transfer_units(amount.copy_abs()) != int(plan["amount_units"]):
                continue
            source, target = api_integer(entry["accountId"]), api_integer(entry["counterpartyAccountId"])
            side = None
            if source == plan["from_account"] and target == plan["to_account"] and amount < 0:
                side = "debit"
            if source == plan["to_account"] and target == plan["from_account"] and amount > 0:
                side = "credit"
            if side:
                tx_hash = _tx_hash(entry.get("transactionHash", ""))
                legs.setdefault(tx_hash, {"debit": [], "credit": []})[side].append(api_integer(entry["sequenceNumber"]))
        cursor = page["meta"].get("nextCursor")
        if cursor is None:
            reached = True
            break
        if (reached and descending) or not isinstance(cursor, str) or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
    if not (reached and (descending or cursor is None)):
        return None  # The scan did not provably cover everything since the request.
    unclaimed = {h: v for h, v in legs.items() if h not in claimed}
    if len(unclaimed) != 1:
        return None
    tx_hash, pair = next(iter(unclaimed.items()))
    if len(pair["debit"]) != 1 or len(pair["credit"]) != 1:
        return None
    return tx_hash, [pair["debit"][0], pair["credit"][0]]


def observed_account(accounts: dict, record: dict, journal: dict) -> int | None:
    """The single unclaimed new margin account owned after a create request."""
    if record.get("created_at_ms") is None or _competing(record, journal):
        return None
    if any(
        rid != record["request_id"]
        and op["plan"]["action"] == "create"
        and op.get("result", {}).get("status") not in TERMINAL
        for rid, op in journal.items()
    ):
        return None
    claimed = {op.get("result", {}).get("account_id") for rid, op in journal.items() if rid != record["request_id"]}
    before = set(record["before_accounts"])
    new = [a for a, kind in accounts.items() if kind in PERP_TYPES and a not in before and a not in claimed]
    return new[0] if len(new) == 1 else None


async def reconcile(relay, record: dict, journal: dict | None = None) -> dict:
    plan, evidence = record["plan"], record.get("relay", {})
    base = {**plan, "request_id": record["request_id"], "tx_hash": record.get("tx_hash")}
    if (
        evidence.get("error_code") == "TRANSACTION_REVERTED"
        and evidence.get("txSubmitted") is True
        and record.get("tx_hash")
    ):
        return {
            **base,
            "status": "reverted",
            "error": "Relay reports a reverted transaction; this request will not be resubmitted",
        }
    if (
        evidence.get("error_code") == "TRANSACTION_FAILED_TO_SEND"
        and evidence.get("txSubmitted") is False
        and not record.get("tx_hash")
    ):
        return {
            **base,
            "status": "rejected",
            "error": "Relay reports a pre-submission failure; this request will not be resubmitted",
        }
    if not record.get("tx_hash"):
        if not evidence and journal is not None:
            # Lost reply: only a unique, unclaimed observation can confirm. Nothing here ever
            # concludes "not executed", so this path cannot lead to a repeated submission.
            if plan["action"] == "transfer":
                observed = await observed_transfer(relay, record, journal)
                if observed:
                    return {
                        **base,
                        "tx_hash": observed[0],
                        "status": "confirmed",
                        "transfer_sequence_numbers": observed[1],
                        "verification": "unique_observation_after_request",
                        "verification_note": UNIQUE_OBSERVATION_NOTE,
                    }
            else:
                account_id = observed_account(await relay.accounts(), record, journal)
                if account_id is not None:
                    return {
                        **base,
                        "status": "confirmed",
                        "account_id": account_id,
                        "verification": "unique_observation_after_request",
                        "verification_note": UNIQUE_OBSERVATION_NOTE,
                    }
        return unknown(
            record,
            "Relay outcome has no transaction hash and no unique matching observation. API gap: no request-status/idempotency lookup; operator reconciliation is required",
        )
    accounts = await relay.accounts()
    if plan["action"] == "create":
        if evidence.get("txSubmitted") is not True or evidence.get("has_error"):
            return unknown(
                record,
                "API gap: no creation-by-transaction lookup. A new account in wallet reads alone cannot prove this request succeeded",
            )
        candidates = set(evidence.get("account_ids", [])) - set(record["before_accounts"])
        margin = [value for value in candidates if accounts.get(value) in PERP_TYPES]
        if len(margin) != 1:
            return unknown(
                record,
                "Waiting for exactly one relay-returned margin account to appear in the owner's account read; never infer the latest/global account ID",
            )
        return {
            **base,
            "status": "confirmed",
            "account_id": margin[0],
            "verification": "relay_receipt_and_owned_account_read",
        }
    require_margin_accounts(accounts, plan["from_account"], plan["to_account"])
    entries = await matching_transfer(relay, record)
    if entries is None:
        return unknown(
            record,
            "Matching debit/credit ledger entries are unavailable within ten pages; indexing may lag. Status only reads, and missing history never authorizes resubmission",
        )
    return {
        **base,
        "status": "confirmed",
        "transfer_sequence_numbers": entries,
        "verification": "transaction_hash_and_exact_owned_transfer_ledger_pair",
    }


async def finish(relay, lease, record: dict, *, supplied_hash: str | None = None) -> dict:
    try:
        result = await reconcile(relay, record, lease.state.get("setup_operations", {}))
    except Exception:
        result = unknown(
            record, "Supported API reads could not verify the outcome; retry account status, never the submission"
        )
    if supplied_hash is not None and result["status"] != "confirmed":
        # An operator hash is a claim to verify, not relay evidence: an unverified
        # one must neither finalize the request nor block the correct hash later.
        record.pop("tx_hash", None)
        result = {
            **unknown(record, "The supplied hash did not verify this request's exact transfer and was not saved"),
            "supplied_tx_hash": supplied_hash,
        }
    if result["status"] == "confirmed" and not record.get("tx_hash") and result.get("tx_hash"):
        record["tx_hash"] = result["tx_hash"]  # An observed hash now belongs to this request.
    record["result"] = result
    if result["status"] in ("confirmed", "reverted", "rejected"):
        lease.state.pop("pending_setup", None)
    lease.save()
    return result


async def run(args) -> dict:
    config = get_trading_config(include_private_key=False)
    if getattr(args, "execute", False) and os.environ.get("REYA_MODE") == "read-only":
        raise SetupError("REYA_MODE=read-only disables account writes")
    relay = AccountRelay(config)
    owner = relay.owner
    # Retain the old state key so an unfinished pre-relay operation cannot be lost.
    # This is a file-lock namespace only, never an RPC connection.
    resources = [f"rpc:{config.chain_id}:{owner.lower()}", *trading_resources(config, owner)]
    with WriterLease(resources) as lease:
        if lease.state.get("pending_transaction"):
            raise SetupError(
                "An older setup transaction remains unresolved. No RPC fallback: reconcile through supported APIs/operator evidence before migrating its state"
            )
        records = lease.state.setdefault("setup_operations", {})
        pending_id = lease.state.get("pending_setup")
        if args.action == "status":

            def with_pending(result):
                if args.request_id:
                    return result
                # A local journal inventory, not a claim of API reconciliation.
                return {
                    **result,
                    "pending_requests": [
                        {
                            "request_id": request_id,
                            "action": operation["plan"]["action"],
                            "api_url": operation["plan"]["api_url"],
                            "status": operation.get("result", {}).get("status", "unknown"),
                            "tx_hash": operation.get("tx_hash"),
                        }
                        for request_id, operation in records.items()
                        if operation.get("result", {}).get("status") not in ("confirmed", "reverted", "rejected")
                    ],
                }

            request_id = args.request_id or pending_id or lease.state.get("last_setup")
            if not request_id:
                return with_pending({"status": "no_setup_operation"})
            if request_id not in records:
                raise SetupError("Request ID is not in this owner's local setup journal")
            record = records[request_id]
            if record["plan"]["api_url"] != config.api_url:
                raise SetupError("Select the original API deployment to reconcile this request")
            if record.get("result", {}).get("status") in TERMINAL:
                if args.tx_hash and args.tx_hash != record.get("tx_hash"):
                    raise SetupError(
                        "This request is already resolved with a different transaction hash; the supplied hash was not used"
                    )
                return with_pending(record["result"])
            supplied = None
            if args.tx_hash:
                if record["plan"]["action"] != "transfer":
                    raise SetupError("API gap: a supplied hash cannot recover creation receipt evidence")
                if record.get("tx_hash") not in (None, args.tx_hash):
                    raise SetupError("Supplied hash conflicts with the hash already saved for this request")
                other = next(
                    (rid for rid, op in records.items() if rid != request_id and op.get("tx_hash") == args.tx_hash),
                    None,
                )
                if other is not None:
                    raise SetupError(
                        f"Supplied hash is already recorded for request {other}; one transaction cannot confirm two requests"
                    )
                if record.get("tx_hash") is None:
                    # Verified by finish(); saved only if it proves this exact request.
                    supplied = args.tx_hash
                    record["tx_hash"] = supplied
            async with relay:
                return with_pending(await finish(relay, lease, record, supplied_hash=supplied))
        plan = request_plan(config, args)
        if args.execute and not args.request_id:
            raise SetupError("Execution requires --request-id; reuse that ID for the same intent")
        if args.request_id in records:
            record = records[args.request_id]
            if record["request"] != plan:
                raise SetupError("Request ID already belongs to different parameters")
            if record.get("result", {}).get("status") in ("confirmed", "reverted", "rejected"):
                return record["result"]
            return unknown(
                record, "This request was already attempted; use account status. It will not be submitted again"
            )
        if pending_id:
            return unknown(records[pending_id], "An earlier setup request needs reconciliation; run account status")
        async with relay:
            accounts = await relay.accounts()
            if args.action == "transfer":
                require_margin_accounts(accounts, args.from_account, args.to_account)
                nonce = await relay.nonce()
                if nonce >= MAX_API_INTEGER:
                    raise SetupError("Lossless nonce API support is required before another transfer")
                plan = {**plan, "nonce": str(nonce + 1), "deadline": int(time.time()) + 60}
            if not args.execute:
                return {
                    **plan,
                    "preview": True,
                    "requires_owner_signature": args.action == "transfer",
                    "gas_paid_by": "reya_executor",
                }
            signer = None
            if args.action == "transfer":
                filename = os.environ.get("REYA_ACCOUNT_OWNER_KEY_FILE")
                if not filename:
                    raise SetupError("Set REYA_ACCOUNT_OWNER_KEY_FILE to sign transfers; creation needs no key")
                signer = Account.from_key(read_private_key_file(filename))
                if signer.address.lower() != owner.lower():
                    raise SetupError("Transfer signer must match the configured account owner, not a trading delegate")
            payload = relay_payload(config, plan, signer)
            record = {
                "request_id": args.request_id,
                "request": request_plan(config, args),
                "plan": plan,
                "before_accounts": list(accounts),
                "calldata_hash": "0x" + keccak(hexstr=payload["txData"]["data"]).hex(),
                "created_at_ms": int(time.time() * 1000),
            }
            records[args.request_id] = record
            lease.state.update(pending_setup=args.request_id, last_setup=args.request_id)
            lease.save()  # Durable intent BEFORE the single POST; no signatures or raw calldata persisted.
            try:
                response = await relay.submit(payload)
                if isinstance(response, dict) and response.get("txHash") is not None:
                    record["tx_hash"] = _tx_hash(response["txHash"])
                    lease.save()
                evidence = relay_evidence(response)
                record["relay"] = evidence
                if evidence.get("tx_hash"):
                    record["tx_hash"] = evidence["tx_hash"]
                lease.save()  # Persist the relay reference before follow-up reads.
            except Exception:
                result = unknown(
                    record,
                    "Relay submission outcome is unknown; use account status. Never repeat with a new request ID",
                )
                record["result"] = result
                lease.save()
                return result
            return await finish(relay, lease, record)


def build_parser():
    parser = JsonArgumentParser(prog="account", description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="Create an owned margin account through the unsigned relay")
    transfer = actions.add_parser("transfer", help="Relay a signed rUSD transfer between owned margin accounts")
    transfer.add_argument("--from-account", required=True, type=_account_id)
    transfer.add_argument("--to-account", required=True, type=_account_id)
    transfer.add_argument("--amount", required=True, type=positive_decimal_arg)
    status = actions.add_parser("status", help="Reconcile through supported API reads; never resubmit")
    status.add_argument("--request-id", type=_request_id)
    status.add_argument(
        "--tx-hash", type=_tx_hash, help="Operator-recovered transfer hash when the relay response was lost"
    )
    for command in (create, transfer):
        command.add_argument(
            "--execute", action="store_true", help="Submit once through the Reya relay; default is preview"
        )
        command.add_argument(
            "--request-id", type=_request_id, help="Stable intent ID, required for execution; reuse on retry"
        )
    return parser


def main():
    args = build_parser().parse_args()
    try:
        output_result(asyncio.run(run(args)))
    except SetupError as exc:
        output_error(str(exc))
    except Exception as exc:
        output_error(f"{type(exc).__name__}: account API operation failed; no automatic retry")


if __name__ == "__main__":
    main()
