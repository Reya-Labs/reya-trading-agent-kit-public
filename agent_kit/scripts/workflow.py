"""Hosted trading workflows: draft, validate, preview, start, status and stop.

The kit is a thin client of Reya's hosted workflow service. `draft` writes a
document locally; `validate` and `preview` ask the service, which is the only
evaluator, and `draft` and `preview` return `app_review_url`, the Reya app's page
where the user reviews the draft and starts it with their own wallet; `start` and `stop` are signed with the agent key from `agent init`
(never the owner key) and refused for any account that is not the recorded
Agent account. Once started, the run lives on the service: this computer can be
off. Every start needs the owner to have approved the service's hosted signer once:
`info` says whether they have, and links the app's setup page, whose Enable hosted
runs step approves it (`info --open` opens it); a start refused for it opens that
page once.

    workflow draft --symbol btc --direction down --level 90000 --notional 25 --allocation 200 --expires 7d
    workflow validate --file <draft.json>
    workflow preview --file <draft.json>
    workflow start --file <draft.json>
    workflow status [RUN_ID]
    workflow stop RUN_ID
"""

from typing import Any

import argparse
import asyncio
import base64
import json
import os
import time
from decimal import Decimal
from pathlib import Path

import aiohttp

from agent_kit.scripts import _env
from agent_kit.scripts import _workflow as wf
from agent_kit.scripts import agent
from agent_kit.scripts._common import JsonArgumentParser, output_error, output_result, positive_decimal_arg, symbol_arg
from agent_kit.scripts._http import RegionUnavailableError


def _app() -> str | None:
    """The Reya app for this deployment, as the setup links use it; None when none is known."""
    try:
        return agent.app_base_url(_env.get_api_url())
    except agent.SetupError:
        return None  # a malformed REYA_APP_URL costs the links, never the result of a start or stop


def _run_page(app: str | None, run_id: Any) -> str | None:
    """The run's page in the app. Like the setup page, it opens with the access code in the fragment."""
    run = str(run_id)
    if not wf.UINT_RE.fullmatch(run) or int(run) >= 2**64:
        return None
    return agent.deep_link(app, "workflow_run", run=run, access=agent.SETUP_PAGE_ACCESS_CODE)


def _runs_page(app: str | None, account: int) -> str | None:
    """The Agent account's runs in the app, newest first, each linking to its run page."""
    return agent.deep_link(app, "workflows", account=account, access=agent.SETUP_PAGE_ACCESS_CODE)


def _review_page(app: str | None, doc: dict) -> str | None:
    """The app's page that reviews this draft from the service's own preview and starts it with the user's
    wallet. The document travels in the URL fragment beside the access code, as compact JSON in base64url
    without padding, so it never reaches a server in the URL. None when no app is known, or when the document
    is over the size the kit and the page both accept."""
    raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()
    if len(raw) > wf.MAX_DOCUMENT_BYTES:
        return None
    who = _setup_for()
    return agent.deep_link(
        app,
        "workflow_review",
        who=f"&agent={who[0]}&owner={who[1]}" if who else "",
        access=agent.SETUP_PAGE_ACCESS_CODE,
        doc=base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii"),
    )


def _setup_for() -> tuple[str, str] | None:
    """(agent key address, owner) the setup page is for; None until agent init has recorded both."""
    try:
        address = _env.agent_address()
        owner = os.environ.get("PERP_WALLET_ADDRESS_1")
        return (address, agent.owner_address(owner)) if address and owner else None
    except ValueError:
        return None


def _enable_page(account: int) -> str | None:
    """The app's setup page for the Agent account (the setup links' own URL). Once the agent is
    approved and its account funded, the page shows its Enable hosted runs step, where the owner
    approves the hosted signer; before that it finishes the step still open."""
    who = _setup_for()
    return agent.setup_link(_app(), *who, account) if who else None


# Why a read for the hosted-runs check failed; the check is skipped and the command still answers.
READ_FAILURES = (aiohttp.ClientError, asyncio.TimeoutError, RegionUnavailableError, ValueError)
NOT_ENABLED = (
    "Hosted runs are not enabled for this wallet: every workflow start, shadow included, needs the owner to "
    "approve the hosted signer (hosted_signer) once."
)
ON_THE_PAGE = (
    "There, the owner approves it with one signature from {owner}; the page shows the address, which matches "
    "hosted_signer. The page first finishes any setup step still open."
)


async def _hosted_runs(hosted: str | None, open_page: bool = False) -> dict:
    """Whether the owner approved the active hosted signer, read the way agent status reads the agent
    key's approval, and the setup page that approves it when not, opened once with `open_page`.
    Never fails `info`."""
    if hosted is None:
        return {}  # no new run can start anywhere: nothing to enable
    try:
        recorded = _env.agent_account_ids()
        who = _setup_for()
        if not recorded or who is None:
            return {
                "hosted_signer_enabled": None,
                "next": "Hosted runs need agent init's setup first: the agent key and the Agent account. Run agent status.",
            }
        try:
            account = wf.agent_account()
        except wf.WorkflowError:
            account = min(recorded)  # several recorded and none chosen: the setup page's own rule
        approved = await agent.approved_signers(_env.get_trading_config(include_private_key=False))
    except READ_FAILURES as exc:
        why = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return {"hosted_signer_enabled": None, "hosted_signer_note": f"Could not read the owner's approvals: {why}"}
    if hosted.lower() in approved:
        return {"hosted_signer_enabled": True}
    url = _enable_page(account)
    agent_too = (
        "" if who[0].lower() in approved else " The agent key is not approved yet either: the page starts there."
    )
    result: dict[str, Any] = {"hosted_signer_enabled": False, "enable_url": url}
    if url is None:
        result["next"] = (
            f"{NOT_ENABLED} The owner approves it in the Reya app; set REYA_APP_URL for a link to the page."
        )
        return result
    page = ON_THE_PAGE.format(owner=who[1])
    if not open_page:
        result["next"] = (
            f"{NOT_ENABLED} Give the user enable_url, the Reya app's Enable hosted runs step, exactly as returned "
            f"(the part after # is the page's access code). {page}{agent_too}"
        )
        return result
    outcome, where = await _open_enable_page(url, who, account)
    result.update(outcome)
    result["next"] = f"{NOT_ENABLED} {where} {page}{agent_too}"
    return result


async def _open_enable_page(url: str, who: tuple[str, str], account: int) -> tuple[dict, str]:
    """Open the setup page once, like agent init's links: a page already open in a tab, under this link
    or one showing the same page, is not opened again. The outcome, and where the user finds the page."""
    try:
        named = await agent.owner_agent_accounts(_env.get_trading_config(include_private_key=False))
    except READ_FAILURES:
        named = []  # unknown: only this exact link counts as already open
    outcome = agent.show_setup_link(url, agent.same_setup_page(_app(), *who, account, named))
    if outcome.get("opened"):
        return outcome, "The setup page is now open at its Enable hosted runs step."
    if outcome.get("link_already_opened"):
        return outcome, (
            "The setup page is already open in a browser tab, or was already given to the user "
            "(link_already_opened): point them there, or give them enable_url."
        )
    why = agent.BROWSER_UNAVAILABLE[outcome["browser_unavailable"]]
    return outcome, (
        f"No browser opened ({why}). Show the user enable_url and ask them to open it on any device where their "
        "wallet is."
    )


async def _not_enabled(refusal: wf.ServiceError, account: int, hosted: str, open_page: bool) -> dict:
    """A start the service refused because the owner has not approved the hosted signer. Nothing
    started. The setup page's Enable hosted runs step opens once (see _open_enable_page)."""
    result: dict[str, Any] = {**refusal.result(), "hosted_signer": hosted, "enable_url": _enable_page(account)}
    who = _setup_for()
    url = result["enable_url"]
    if url is None or who is None:
        return result
    page = ON_THE_PAGE.format(owner=who[1])
    again = " Then run the same workflow start again."
    if not open_page:
        result["next"] = (
            f"Nothing was started. {NOT_ENABLED} Give the user enable_url, exactly as returned. {page}{again}"
        )
        return result
    outcome, where = await _open_enable_page(url, who, account)
    result.update(outcome)
    result["next"] = f"Nothing was started. {NOT_ENABLED} {where} {page}{again}"
    return result


async def _service(client: wf.WorkflowClient) -> wf.ServiceInfo:
    environment, chain = wf.kit_environment()
    return wf.parse_info(await client.info(), environment, chain, client.base_url)


def _client() -> wf.WorkflowClient:
    # Run reads carry the agent key's read grant (see wf.ReadGrants); writes are signed requests of their own.
    return wf.WorkflowClient(wf.service_url(), read_grants=wf.ReadGrants())


def _document(path: str) -> tuple[str, dict, int]:
    """The document's text and parsed form, after the Agent-account guard and the environment check."""
    text, doc = wf.load_document(path)
    account = wf.agent_account(wf.document_account(doc))
    environment, _ = wf.kit_environment()
    if doc.get("environment") != environment:
        raise wf.WorkflowError(
            f"The document is for {doc.get('environment')!r}, but the kit is configured for {environment}"
        )
    return text, doc, account


def _invalid(answer: dict) -> dict:
    errors = [e for e in answer.get("errors") or [] if isinstance(e, dict)]
    first = errors[0].get("message") if errors else "the service listed no reason"
    return {"ok": False, "error": f"The document is invalid: {first}", "errors": errors}


async def cmd_info(args) -> dict:
    async with _client() as client:
        info = await _service(client)
    return {"service": client.base_url, **info.view(), **await _hosted_runs(info.active_signer, args.open)}


async def cmd_draft(args) -> dict:
    environment, _ = wf.kit_environment()
    account = wf.agent_account(args.account_id)
    conditions = [wf.condition("funding_avg", spec) for spec in args.funding or []]
    conditions += [wf.condition("position_qty", spec) for spec in args.position or []]
    doc = wf.draft_document(
        environment=environment,
        account_id=account,
        market=args.symbol,
        direction=args.direction,
        level=args.level,
        side=args.side,
        notional=args.notional,
        allocation=args.allocation,
        expires_at=wf.parse_expiry(args.expires),
        repeat=args.repeat,
        bound_bps=args.bound_bps,
        conditions=conditions,
        stop_px=args.stop_px,
    )
    if args.out:
        path = Path(args.out).expanduser()
    else:
        level = doc["steps"][0]["trigger"]["level"].replace(".", "_")
        path = wf.drafts_directory() / f"{doc['market']}-{args.direction}-{level}-{int(time.time())}.json"
    if path.exists() and not args.force:
        raise wf.WorkflowError(f"{path} exists; pass --force to replace it or --out for another file")
    try:
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise wf.WorkflowError(f"Cannot write {path}: {exc.strerror}") from None
    return {
        "file": str(path),
        "document": doc,
        "workflow": wf.describe(doc),
        "account_id": account,
        "app_review_url": _review_page(_app(), doc),
        "next": f"workflow preview --file {path} (the service checks it against the live market; nothing starts)",
    }


async def cmd_validate(args) -> dict:
    text, doc, account = _document(args.file)
    async with _client() as client:
        answer = await client.validate(text)
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        return _invalid(answer if isinstance(answer, dict) else {})
    wf.check_canonical(doc, answer.get("canonical"), answer.get("version_hash"))
    return {
        "ok": True,
        "file": args.file,
        "account_id": account,
        "workflow": wf.describe(doc),
        "version_hash": answer["version_hash"],
        "canonical": answer["canonical"],
        "warnings": answer.get("warnings") or [],
        "next": f"workflow preview --file {args.file}",
    }


async def cmd_preview(args) -> dict:
    text, doc, account = _document(args.file)
    async with _client() as client:
        answer = await client.preview(text)
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        return _invalid(answer if isinstance(answer, dict) else {})
    view = wf.preview_view(answer, doc)
    view["file"] = args.file
    view["account_id"] = account
    view["app_review_url"] = _review_page(_app(), doc)
    view["next"] = (
        f"After the user confirms the rules and allocation above: workflow start --file {args.file}"
        + (
            " with --fire-now to fire on the first fresh mark, or --wait-for-rearm to wait for a fresh crossing: the "
            "user's choice (without either, start signs nothing and asks)"
            if view.get("already_triggered")
            else ""
        )
        + (
            ". If you cannot or should not start it from this session, give the user app_review_url exactly as "
            "returned instead: they review it and start it in the Reya app with their own wallet"
            if view["app_review_url"]
            else ""
        )
    )
    return view


def _local_caps(doc: dict, info: wf.ServiceInfo) -> None:
    limits = doc.get("limits") or {}
    allocation = Decimal(wf.decimal_value(limits.get("allocation_usd")))
    if allocation > info.max_allocation_usd:
        raise wf.WorkflowError(
            f"The allocation {wf.usd_text(allocation)} is above the pilot cap of {wf.usd_text(info.max_allocation_usd)}",
            code="ABOVE_PILOT_LIMIT",
        )
    notional = Decimal(wf.decimal_value((wf.first_step(doc).get("action") or {}).get("notional_usd")))
    if notional > info.max_order_usd:
        raise wf.WorkflowError(
            f"The order size {wf.usd_text(notional)} is above the pilot cap of {wf.usd_text(info.max_order_usd)}",
            code="ABOVE_PILOT_LIMIT",
        )


async def _confirm_start(client: wf.WorkflowClient, account: int, version_hash: str, starter: str, since: int):
    """After a lost answer: the account's active run, if it is this document started by this key."""
    listing = await client.account_runs(account, state="active", limit=5)
    for summary in listing.get("runs") or []:
        run = await client.run(int(summary["run_id"]))
        if (
            (run.get("document") or {}).get("version_hash", "").lower() == version_hash.lower()
            and str(run.get("starter", "")).lower() == starter.lower()
            and int(run.get("created_at", 0)) >= since - 60
        ):
            return run
    return None


async def cmd_start(args) -> dict:
    text, doc, account = _document(args.file)
    key, address = wf.agent_signer()
    async with _client() as client:
        info = await _service(client)
        if args.mode not in info.modes:
            raise wf.WorkflowError(
                f"This environment allows {', '.join(info.modes) or 'no'} mode(s), not {args.mode}",
                code="MODE_NOT_ALLOWED",
            )
        hosted = info.active_signer
        if hosted is None:
            raise wf.WorkflowError("The service has no active hosted signer, so no new run can start now")
        if info.is_hosted(address):
            raise wf.WorkflowError("The agent key is one of the service's hosted signers; it cannot start runs")
        _local_caps(doc, info)
        answer = await client.validate(text)
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            return _invalid(answer if isinstance(answer, dict) else {})
        canonical = wf.check_canonical(doc, answer.get("canonical"), answer.get("version_hash"))
        # The mark can cross the level between the user's preview and this start. Ask again, unsigned: a run
        # started past its level only waits for the re-arm band, which is not what the user said yes to.
        preview = await client.preview(text)
        if not isinstance(preview, dict) or preview.get("ok") is not True:
            return _invalid(preview if isinstance(preview, dict) else {})
        view = wf.preview_view(preview, doc)
        if view.get("already_triggered") and not (args.fire_now or args.wait_for_rearm):
            return _already_triggered(args, view)
        now = int(time.time())
        request = wf.start_request(
            info,
            account_id=account,
            version_hash=answer["version_hash"],
            allocation_usd=canonical["limits"]["allocation_usd"],
            expires_at=canonical["limits"]["expires_at"],
            hosted_signer=hosted,
            mode=args.mode,
            fire_now=args.fire_now,
            signer=address,
            nonce=wf.fresh_nonce(),
            deadline=now + min(wf.REQUEST_LIFETIME_S, info.max_deadline_secs),
        )
        body = wf.signed_body("WorkflowStart", request, key, address, info, document=answer["canonical"])
        try:
            run = await client.start(body)
        except wf.ServiceError as refusal:
            if refusal.code != "HOSTED_SIGNER_NOT_ENABLED":
                raise
            return await _not_enabled(refusal, account, hosted, args.open)
        except wf.OutcomeUnknown as exc:
            found = None
            try:
                found = await _confirm_start(client, account, answer["version_hash"], address, now)
            except (wf.WorkflowError, wf.ServiceError, wf.OutcomeUnknown, KeyError, TypeError, ValueError):
                pass
            if found is None:
                return {
                    "error": f"{exc}. The start may or may not have been accepted; nothing is resent automatically.",
                    "outcome": "unknown",
                    "account_id": account,
                    "version_hash": answer["version_hash"],
                    "app_runs_url": _runs_page(_app(), account),
                    "next": "Run workflow status before starting again: it lists this account's active run.",
                }
            run = {"run_id": found["run_id"], "state": found["state"], "mode": found["mode"], "confirmed_by_read": True}
    app = _app()
    result = {
        "started": True,
        "run_id": run.get("run_id"),
        "state": run.get("state"),
        "mode": run.get("mode"),
        "account_id": account,
        "workflow": wf.describe(canonical),
        "version_hash": answer["version_hash"],
        "signer": address,
        "hosted_signer": hosted,
        "allocation_usd": canonical["limits"]["allocation_usd"],
        "expires_at": wf.iso(canonical["limits"]["expires_at"]),
        "fire_now": args.fire_now,
        "now": view["now"],
        "status_url": run.get("status_url"),
        "app_run_url": _run_page(app, run.get("run_id")),
        "app_runs_url": _runs_page(app, account),
        "next": f"workflow status {run.get('run_id')} (the run arms on the next fresh mark; this computer can be off)",
    }
    if view.get("already_triggered"):
        result["already_triggered"] = (
            "The mark was already past the level at start: the run fires on the first fresh mark."
            if args.fire_now
            else "The mark was already past the level at start: the run waits until the mark moves back past "
            "the re-arm band, then fires on the next crossing."
        )
    for flag in ("replayed", "confirmed_by_read"):
        if run.get(flag):
            result[flag] = True
    return result


def _already_triggered(args, view: dict) -> dict:
    """A start refused before signing: the level is already past and the user has not chosen what to do."""
    command = f"workflow start --file {args.file}" + (f" --mode {args.mode}" if args.mode != "shadow" else "")
    return {
        "error": "The mark is already past the level, so this run would not fire until the mark moves back past "
        "the re-arm band and crosses again. Nothing was signed.",
        "error_code": "ALREADY_TRIGGERED",
        "now": view["now"],
        "trigger": view["trigger"],
        "file": args.file,
        "next": (
            f"Ask the user which they want: fire on the first fresh mark ({command} --fire-now), wait for a fresh "
            f"crossing ({command} --wait-for-rearm), or draft a level further from the mark and preview it again."
        ),
    }


async def _run_status(client: wf.WorkflowClient, run_id: int) -> dict:
    run = await client.run(run_id)
    account = int(wf.uint_text(run.get("account_id"), 128))
    wf.agent_account(account)
    events, truncated = await client.activity(run_id)
    app = _app()
    return {
        **wf.status_view(run, events, truncated=truncated),
        "app_run_url": _run_page(app, run_id),
        "app_runs_url": _runs_page(app, account),
    }


async def cmd_status(args) -> dict:
    if args.account_id is not None or not _env.agent_account_ids():
        wf.agent_account(args.account_id)
    async with _client() as client:
        if args.run_id is not None:
            return await _run_status(client, args.run_id)
        account = wf.agent_account(args.account_id)
        listing = await client.account_runs(account, state="all" if args.all else "active", limit=args.limit)
        runs = [r for r in listing.get("runs") or [] if isinstance(r, dict)]
        app = _app()
        result: dict[str, Any] = {
            "account_id": account,
            "app_runs_url": _runs_page(app, account),
            "runs": [
                {
                    **r,
                    "created_at": wf.iso(r.get("created_at")),
                    "stop": wf.stop_view(r.get("stop")),
                    "app_run_url": _run_page(app, r.get("run_id")),
                }
                for r in runs
            ],
        }
        active = [r for r in runs if r.get("state") == "active"]
        if len(active) == 1:
            result["active_run"] = await _run_status(client, int(active[0]["run_id"]))
        elif not runs:
            result["next"] = "No workflow runs here yet: workflow draft, then preview and start."
        return result


async def cmd_stop(args) -> dict:
    key, address = wf.agent_signer()
    # Refuse a named account before asking the service anything; otherwise the run's own account is checked.
    if args.account_id is not None or not _env.agent_account_ids():
        wf.agent_account(args.account_id)
    async with _client() as client:
        run = await client.run(args.run_id)
        account = wf.agent_account(int(wf.uint_text(run.get("account_id"), 128)))
        if args.account_id is not None and args.account_id != account:
            raise wf.WorkflowError(f"Run {args.run_id} belongs to account {account}, not {args.account_id}")
        app = _app()
        pages = {"app_run_url": _run_page(app, args.run_id), "app_runs_url": _runs_page(app, account)}
        if run.get("state") == "stopped":
            return {
                "run_id": run.get("run_id"),
                "state": "stopped",
                "already_stopped": True,
                "stop": wf.stop_view(run.get("stop")),
                "protection": run.get("protection"),
                **pages,
            }
        info = await _service(client)
        request = wf.stop_request(
            info,
            account_id=account,
            run_id=args.run_id,
            signer=address,
            nonce=wf.fresh_nonce(),
            deadline=int(time.time()) + min(wf.REQUEST_LIFETIME_S, info.max_deadline_secs),
        )
        body = wf.signed_body("WorkflowStop", request, key, address, info)
        try:
            answer = await client.stop(args.run_id, body)
        except wf.OutcomeUnknown as exc:
            try:
                after = await client.run(args.run_id)
            except (wf.WorkflowError, wf.ServiceError):
                after = {}
            if after.get("state") != "stopped":
                return {
                    "error": f"{exc}. The stop may or may not have been accepted.",
                    "outcome": "unknown",
                    "run_id": str(args.run_id),
                    **pages,
                    "next": f"Run workflow stop {args.run_id} again: stopping a stopped run changes nothing.",
                }
            answer = {**after, "confirmed_by_read": True}
    result = {
        "run_id": answer.get("run_id"),
        "state": answer.get("state"),
        "stop": wf.stop_view(answer.get("stop")),
        "protection": answer.get("protection"),
        "signer": address,
        **pages,
        "note": "No further firing is sent; an order already sent completes; positions stay open.",
        "next": (
            f"workflow status {args.run_id} shows the protective order's cancellation"
            if answer.get("protection")
            else f"The run armed no protective order, so there is nothing to cancel. workflow status {args.run_id}"
        ),
    }
    for flag in ("already_stopped", "replayed", "confirmed_by_read"):
        if answer.get(flag):
            result[flag] = True
    return result


COMMANDS = {
    "info": cmd_info,
    "draft": cmd_draft,
    "validate": cmd_validate,
    "preview": cmd_preview,
    "start": cmd_start,
    "status": cmd_status,
    "stop": cmd_stop,
}


async def run(args) -> dict:
    """One command's result. Refusals come back as {"error": ...}; key material never appears."""
    try:
        return await COMMANDS[args.action](args)
    except wf.ServiceError as exc:
        return exc.result()
    except RegionUnavailableError as exc:
        return {"error": str(exc), "error_code": "REGION_UNAVAILABLE"}
    except wf.WorkflowError as exc:
        result: dict[str, Any] = {"error": str(exc)}
        if exc.code:
            result["error_code"] = exc.code
        return result
    except ValueError as exc:
        return {"error": str(exc)}


def run_id_arg(text: str) -> int:
    if not text.isascii() or not text.isdecimal() or int(text) >= 2**63:
        raise argparse.ArgumentTypeError(f"not a run ID: '{text}'")
    return int(text)


def bound_arg(text: str) -> int:
    if not text.isascii() or not text.isdecimal() or not wf.MIN_BOUND_BPS <= int(text) <= wf.MAX_BOUND_BPS:
        raise argparse.ArgumentTypeError(f"the price bound is {wf.MIN_BOUND_BPS} to {wf.MAX_BOUND_BPS} bps")
    return int(text)


def build_parser():
    parser = JsonArgumentParser(prog="workflow", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)

    info = actions.add_parser(
        "info",
        help="The service's environment, hosted signer, modes and pilot caps, and whether hosted runs are enabled "
        "(read-only)",
    )
    info.add_argument(
        "--open",
        action="store_true",
        help="if hosted runs are not enabled, open the app's Enable hosted runs step (once, like the setup links)",
    )

    draft = actions.add_parser("draft", help="Write a workflow document locally; nothing is sent")
    draft.add_argument("--symbol", required=True, type=symbol_arg, help="market, e.g. btc or BTCRUSDPERP")
    draft.add_argument("--direction", required=True, choices=("down", "up"), help="crossing direction of the mark")
    draft.add_argument("--level", required=True, type=positive_decimal_arg, help="trigger level in USD")
    draft.add_argument("--side", choices=("buy", "sell"), default="buy", help="order side when it fires")
    draft.add_argument("--notional", required=True, type=positive_decimal_arg, help="USD per firing")
    draft.add_argument("--allocation", required=True, type=positive_decimal_arg, help="USD in total, all firings")
    draft.add_argument("--expires", required=True, help="7d, 12h, 90m, Unix seconds or an ISO time with a zone")
    draft.add_argument("--repeat", choices=("each_crossing", "once"), default="each_crossing")
    draft.add_argument("--bound-bps", type=bound_arg, default=wf.DEFAULT_BOUND_BPS, help="max slippage per order")
    draft.add_argument("--funding", action="append", metavar="OP:PCT_PER_HOUR", help="e.g. lt:0.001 (15-min average)")
    draft.add_argument("--position", action="append", metavar="OP:QTY", help="e.g. lt:0.5 (base units, long > 0)")
    draft.add_argument(
        "--stop-px",
        type=positive_decimal_arg,
        help="optional whole-position stop price, armed once the run's first fill settles",
    )
    draft.add_argument("--account-id", type=int, help="the recorded Agent account (default: the only one)")
    draft.add_argument("--out", help="file to write (default: the kit's state directory)")
    draft.add_argument("--force", action="store_true", help="replace an existing --out file")

    for name, text in (
        ("validate", "Check a document with the service (unsigned; writes nothing)"),
        ("preview", "What the document would do right now, from the service's evaluator (unsigned)"),
        ("start", "Start a run: signs WorkflowStart with the agent key"),
    ):
        command = actions.add_parser(name, help=text)
        command.add_argument("--file", required=True, help="the workflow document (from workflow draft)")
        if name == "start":
            past = command.add_mutually_exclusive_group()
            past.add_argument(
                "--fire-now", action="store_true", help="if the mark is already past the level, fire on the first mark"
            )
            past.add_argument(
                "--wait-for-rearm",
                action="store_true",
                help="if the mark is already past the level, start anyway and wait for a fresh crossing",
            )
            command.add_argument("--mode", choices=wf.MODES, default="shadow", help="shadow (default) sends no orders")
            command.add_argument(
                "--no-open",
                dest="open",
                action="store_false",
                help="if refused until hosted runs are enabled, do not open the app's Enable hosted runs step",
            )

    status = actions.add_parser("status", help="A run's status, or the Agent account's runs (read-only)")
    status.add_argument("run_id", nargs="?", type=run_id_arg)
    status.add_argument("--account-id", type=int)
    status.add_argument("--all", action="store_true", help="list stopped and completed runs too")
    status.add_argument("--limit", type=int, default=20, choices=range(1, 201), metavar="1-200")

    stop = actions.add_parser("stop", help="Stop a run: signs WorkflowStop with the agent key")
    stop.add_argument("run_id", type=run_id_arg)
    stop.add_argument("--account-id", type=int)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        _env.load_env()
        output_result(asyncio.run(run(args)))
    except Exception as exc:
        # Never render an unexpected exception's text: it could carry request bodies.
        output_error(f"{type(exc).__name__}: workflow {args.action} failed")


if __name__ == "__main__":
    main()
