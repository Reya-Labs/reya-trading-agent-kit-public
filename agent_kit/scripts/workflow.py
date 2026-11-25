"""Hosted trading strategies: draft, validate, preview, start, status and stop.

The kit is a thin client of Reya's hosted strategy service, which runs `reya.workflow/3` strategies. `draft`
writes a basket from flags (immediate orders entered at Start, on a price crossing or every period, with a
stop-loss and a take-profit from the entry, a time exit and a loss limit), the same shape the in-app chat drafts,
and keeps it only once the service has checked it. The service takes whole documents and offers no template, so
this is the one document the kit writes; `validate`, `preview` and `start` also take one the user or their
assistant wrote (`--file`). The service is the only evaluator: `validate` and `preview` ask it.

A run's account, mode (Paper, the default, or Live), expiry and whether its first rule may fire at once are the
preview's terms, not the document's. `start` previews the document with those terms and signs that preview
(`StrategyStart`) with the agent key from `agent init` (never the owner key), so what starts is what the service
just checked; it signs nothing while the preview refuses. `stop` signs `StrategyStop`, which closes the run's
positions (or, with --keep-position, leaves them open). Both are refused for any account that is not the recorded
Agent account, or that Reya does not report as an Agent account (`AGENTPERP`): the refusal's `next` names the
command that switches the kit to the wallet's Agent account. Once started, a strategy lives on the service: this
computer can be off. An account runs one Live strategy at a time, and Paper ones beside it. Every start needs the
owner to have approved the service's hosted signer once: `info` says whether they have, and links the app's setup
page, whose Enable hosted runs step approves it (`info --open` opens it); a start refused for it opens that page
once.

    strategy draft --order eth:buy:0.1ETH --trigger every:1d --rounds 10 --stop-loss 10 --max-loss 300
    strategy validate --file <draft.json> [--mode draft|paper|live]
    strategy preview --file <draft.json> [--mode paper|live] [--expires 7d]
    strategy start --file <draft.json> [--mode paper|live] [--expires 7d] [--label NAME]
    strategy status [RUN_ID] | --all [--before RUN_ID]
    strategy stop RUN_ID [--keep-position] | --all

`workflow` is an alias: `python -m agent_kit.scripts.workflow` runs the same commands.
"""

from typing import Any

import argparse
import asyncio
import os
import shlex
import time
from pathlib import Path

import aiohttp

from agent_kit.scripts import _env
from agent_kit.scripts import _workflow as wf
from agent_kit.scripts import agent
from agent_kit.scripts._common import JsonArgumentParser, output_error, output_result
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
    """The Agent account's page in the app: its strategies, each linking to its run page, funds and position."""
    return agent.deep_link(app, "account_page", account=account, access=agent.SETUP_PAGE_ACCESS_CODE)


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
    "Hosted runs are not enabled for this wallet: every strategy start, paper ones included, needs the owner to "
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
        marked = await agent.owner_agent_accounts(_env.get_trading_config(include_private_key=False))
    except READ_FAILURES:
        marked = []  # unknown: only this exact link counts as already open
    outcome = agent.show_setup_link(url, agent.same_setup_page(_app(), *who, account, marked))
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
    again = " Then run the same strategy start again."
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
    # Run reads and account checks carry the agent key's read grant (see wf.ReadGrants); writes are signed
    # requests of their own.
    return wf.WorkflowClient(wf.service_url(), read_grants=wf.ReadGrants())


async def _agent_account_refusal(account: int) -> dict | None:
    """Hosted strategies run only on an Agent account: the refusal, with the fix, when `account` is not one. Checked
    before the service is asked, so nothing is signed for an account the service would refuse."""
    not_agent = await _not_agent_account(account)
    if not_agent is None:
        return None
    return {
        "ok": False,
        "error": f"Account {account} is not an Agent account, so the strategy service refuses strategies on it",
        "error_code": "NOT_AGENT_ACCOUNT",
        "account_id": account,
        **not_agent,
    }


async def _not_agent_account(account: int) -> dict | None:
    """Why hosted strategies refuse `account`, from the owner's accounts and their types (the V2 read agent status
    makes), with the fix: the command that switches the kit to the owner's Agent account, the accounts to ask about,
    or the setup page that creates one. None when it is an Agent account, or when the read fails: the service checks
    the account again before anything starts."""
    try:
        config = _env.get_trading_config(include_private_key=False)
        async with agent.AccountRelay(config) as relay:
            accounts = await relay.accounts()
    except READ_FAILURES:
        return None
    warning = agent.agent_account_warning(accounts, account)
    if warning is None:
        return None
    who = _setup_for()
    create = agent.setup_link(_app(), *who) if who else None
    return {
        "agent_account_warning": warning,
        "agent_account_fix": agent.agent_account_fix(accounts, account, create),
        "next": f"Nothing was signed or started. {warning}",
    }


def _maybe_account(requested: int | None) -> int | None:
    """The recorded Agent account for a check that may name one: the one asked for (which must be recorded), else
    the only or configured one, else none (several and none chosen, or none recorded)."""
    if requested is not None:
        return wf.agent_account(requested)
    try:
        return wf.agent_account()
    except wf.WorkflowError:
        return None


async def cmd_info(args) -> dict:
    async with _client() as client:
        info = await _service(client)
    result = {"service": client.base_url, **info.view(), **await _hosted_runs(info.active_signer, args.open)}
    try:
        account = wf.agent_account()
    except wf.WorkflowError:
        return result  # none recorded, or several and none chosen: _hosted_runs said what to do
    not_agent = await _not_agent_account(account)
    if not_agent is not None:
        # Approved or not, hosted strategies refuse this account: the fix comes first.
        warning = not_agent["agent_account_warning"]
        result.update(not_agent, next=f"{warning} {result['next']}" if result.get("next") else warning)
    return result


def _draft_path(args, spec: wf.Basket) -> Path:
    if args.out:
        path = Path(args.out).expanduser()
    else:
        markets = "-".join(wf.base_asset(m) for m in sorted(spec.markets()))
        trigger = "price" if spec.entry["kind"] == "price" else spec.entry["kind"]
        path = wf.drafts_directory() / f"{markets}-{trigger}-{int(time.time())}.json"
    if path.exists() and not args.force:
        raise wf.WorkflowError(f"{path} exists; pass --force to replace it or --out for another file")
    return path


def _first_refusal(view: dict) -> str:
    first = view["refusals"][0] if view.get("refusals") else {}
    where = f" ({first['path']})" if isinstance(first.get("path"), str) and first["path"] else ""
    return f"{first.get('message') or first.get('code') or 'the service listed no reason'}{where}"


def _check_result(view: dict) -> dict:
    """What every check command returns of the service's answer."""
    keys = ("verdict", "check_mode", "version_hash", "refusals", "warnings", "notes", "figures", "readouts", "what_if")
    return {key: view[key] for key in keys} | {
        key: view[key]
        for key in (
            "takes_over",
            "paper",
            "paper_to_live",
            "preview_id",
            "strategy_id",
            "valid_until",
            "valid_until_iso",
        )
        if key in view
    }


async def cmd_draft(args) -> dict:
    """Write a basket from the flags, checked by the service in draft mode (against the Agent account when one is
    recorded); the file is written only when the check does not refuse it."""
    spec = wf.basket(
        orders=[wf.parse_order(spec) for spec in args.order or []],
        trigger=wf.parse_trigger(args.trigger),
        rounds=args.rounds,
        first_now=args.first_now,
        stop_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        exit_after_secs=wf.duration_secs(args.exit_after, "--exit-after") if args.exit_after else None,
        max_loss=args.max_loss,
        max_slippage_bps=args.max_slippage_bps,
    )
    doc = wf.basket_document(spec)
    text = wf.canonical_json(doc)
    path = _draft_path(args, spec)
    account = _maybe_account(args.account_id)
    async with _client() as client:
        info = await _service(client)
        info.check_schema()
        request: dict[str, Any] = {"document": text, "mode": "draft"}
        if account is not None and info.account_checks:
            request["account_id"] = str(account)
        view = wf.check_view(await client.validate(request), text)
    result: dict[str, Any] = {
        **wf.document_view(doc),
        "document": doc,
        **_check_result(view),
        "account_id": int(request["account_id"]) if "account_id" in request else None,
    }
    if wf.refused(view):
        return {
            "ok": False,
            "error": f"The service refuses this strategy: {_first_refusal(view)}. Nothing was written.",
            **result,
            "next": "Change the flags each refusal names, then draft it again.",
        }
    try:
        # The canonical form itself: the file's bytes are what the version hash is over.
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise wf.WorkflowError(f"Cannot write {path}: {exc.strerror}") from None
    return {
        "file": str(path),
        **result,
        "next": f"strategy preview --file {shlex.quote(str(path))} (a Paper preview against the Agent account: what "
        "it would do now, its margin and what blocks a start; nothing starts)",
    }


def _paper_settings(args) -> dict | None:
    settings: dict[str, Any] = {}
    if args.paper_slippage_bps is not None:
        settings["slippage_bps"] = args.paper_slippage_bps
    if args.paper_fee_tier:
        settings["fee_tier"] = args.paper_fee_tier
    if args.paper_balance:
        settings["balance"] = args.paper_balance
    if args.paper_start:
        settings["start"] = args.paper_start
    return settings or None


def _terms(args, text: str, account: int, mode: str, now: int | None = None) -> dict:
    """The check request for `mode`: a Paper or Live one carries the run's terms, which its preview records and its
    Start signs (the expiry, whether the first rule may fire at once), and its label; a Paper one its settings."""
    request: dict[str, Any] = {"document": text, "account_id": str(account), "mode": mode}
    paper = _paper_settings(args)
    if paper and mode != "paper":
        raise wf.WorkflowError("The --paper-* settings are for a Paper run (--mode paper)")
    if mode == "draft":
        return request
    request["expires_at"] = str(wf.parse_expiry(args.expires, now))
    request["fire_now"] = bool(args.fire_now)
    if args.label:
        request["label"] = args.label
    if paper:
        request["paper"] = paper
    return request


def _command(name: str, args, mode: str) -> str:
    """The same command with the same terms, for a `next`."""
    words = ["strategy", name, "--file", args.file]
    if mode != wf.DEFAULT_MODE:
        words += ["--mode", mode]
    if args.account_id is not None:
        words += ["--account-id", str(args.account_id)]
    if args.expires != wf.DEFAULT_EXPIRES:
        words += ["--expires", args.expires]
    if args.fire_now:
        words.append("--fire-now")
    if args.label:
        words += ["--label", args.label]
    for flag, value in (
        ("--paper-balance", args.paper_balance),
        ("--paper-start", args.paper_start),
        ("--paper-slippage-bps", args.paper_slippage_bps),
        ("--paper-fee-tier", args.paper_fee_tier),
    ):
        if value is not None:
            words += [flag, str(value)]
    return shlex.join(words)


async def _not_agent_answer(refusal: wf.ServiceError, account: int) -> dict:
    return {**refusal.result(), "account_id": account, **(await _not_agent_account(account) or {})}


async def cmd_validate(args) -> dict:
    text, doc = wf.load_document(args.file)
    mode = args.mode
    account = wf.agent_account(args.account_id) if mode != "draft" else _maybe_account(args.account_id)
    if account is not None and (not_agent := await _agent_account_refusal(account)):
        return not_agent
    async with _client() as client:
        info = await _service(client)
        info.check_schema()
        if account is not None and info.account_checks:
            request = _terms(args, text, account, mode)
        elif mode == "draft":
            request = {"document": text, "mode": "draft"}
        else:
            raise wf.WorkflowError("The strategy service does not check against accounts now; only a draft check runs")
        try:
            view = wf.check_view(await client.validate(request), text)
        except wf.ServiceError as refusal:
            if refusal.code == "NOT_AGENT_ACCOUNT" and account is not None:
                return await _not_agent_answer(refusal, account)
            raise
    result = {
        "ok": not wf.refused(view),
        "file": args.file,
        "account_id": int(request["account_id"]) if "account_id" in request else None,
        **wf.document_view(doc),
        **_check_result(view),
    }
    if wf.refused(view):
        result["error"] = f"The service refuses this strategy: {_first_refusal(view)}"
        result["next"] = "Change what each refusal names, then validate it again."
    else:
        result["next"] = _command("preview", args, mode if mode != "draft" else wf.DEFAULT_MODE)
    return result


async def cmd_preview(args) -> dict:
    text, doc = wf.load_document(args.file)
    account = wf.agent_account(args.account_id)
    if not_agent := await _agent_account_refusal(account):
        return not_agent
    async with _client() as client:
        info = await _service(client)
        info.check_schema()
        request = _terms(args, text, account, args.mode)
        try:
            view = wf.check_view(await client.preview(request), text)
        except wf.ServiceError as refusal:
            if refusal.code == "NOT_AGENT_ACCOUNT":
                return await _not_agent_answer(refusal, account)
            raise
    result: dict[str, Any] = {
        "ok": not wf.refused(view),
        "file": args.file,
        "account_id": account,
        "mode": args.mode,
        "mode_text": wf.MODE_TEXTS[args.mode],
        "expires_at": wf.iso(request["expires_at"]),
        "fire_now": request["fire_now"],
        **wf.document_view(doc),
        **_check_result(view),
    }
    if args.mode not in info.modes:
        result["mode_allowed"] = False
    if wf.refused(view):
        result["error"] = f"A {wf.MODE_NAMES[args.mode]} start is refused now: {_first_refusal(view)}"
        result["next"] = "Clear what each refusal names (change the strategy or the account), then preview again."
        return result
    result["next"] = (
        "After the user confirms the rules, the figures and the terms above: "
        f"{_command('start', args, args.mode)} (it previews again and signs that preview, so what starts is what the "
        "service checks then; nothing is signed while that preview refuses)"
    )
    if args.mode not in info.modes:
        result["next"] = (
            f"This environment does not start {wf.MODE_NAMES[args.mode]} strategies now (strategy info lists the "
            "modes it allows)."
        )
    return result


async def _confirm_start(client: wf.WorkflowClient, account: int, strategy_id: str, mode: str) -> dict | None:
    """After a lost answer, one read: the account's newest runs, for the one with the strategy id the preview gave
    (a preview record starts at most one run)."""
    listing = await client.account_runs(account, state="all", limit=20)
    for summary in listing.get("runs") or []:
        if isinstance(summary, dict) and str(summary.get("strategy_id")) == strategy_id and summary.get("mode") == mode:
            return summary
    return None


def _blocked(args, account: int, view: dict) -> dict:
    """A start refused before signing: the service's fresh preview refuses it, or gave no preview record."""
    name = wf.MODE_NAMES[args.mode]
    app = _app()
    reason = _first_refusal(view) if view.get("refusals") else "the service gave no preview record to sign"
    return {
        "error": f"A {name} start is refused now: {reason}. Nothing was signed.",
        "error_code": "START_BLOCKED",
        "mode": args.mode,
        "account_id": account,
        "file": args.file,
        **_check_result(view),
        "app_runs_url": _runs_page(app, account),
        "next": "Clear what each refusal names, then run the same strategy start again. "
        + _command("preview", args, args.mode)
        + " shows the check without starting.",
    }


async def cmd_start(args) -> dict:
    text, doc = wf.load_document(args.file)
    key, address = wf.agent_signer()
    account = wf.agent_account(args.account_id)
    mode = args.mode
    async with _client() as client:
        info = await _service(client)
        info.check_schema()
        if mode not in info.modes:
            raise wf.WorkflowError(
                f"This environment starts {' and '.join(wf.MODE_NAMES[m] for m in info.modes) or 'no'} strategies "
                f"now, not {wf.MODE_NAMES[mode]}",
                code="MODE_NOT_ALLOWED",
            )
        hosted = info.active_signer
        if hosted is None:
            raise wf.WorkflowError("The service has no active hosted signer, so no new strategy can start now")
        if info.is_hosted(address):
            raise wf.WorkflowError("The agent key is one of the service's hosted signers; it cannot start strategies")
        if not_agent := await _agent_account_refusal(account):
            return not_agent
        request = _terms(args, text, account, mode)
        try:
            view = wf.check_view(await client.preview(request), text)
        except wf.ServiceError as refusal:
            if refusal.code == "NOT_AGENT_ACCOUNT":
                return await _not_agent_answer(refusal, account)
            raise
        if wf.refused(view) or "preview_id" not in view or "strategy_id" not in view:
            return _blocked(args, account, view)
        now = int(time.time())
        message = wf.start_request(
            info,
            account_id=account,
            version_hash=view["version_hash"],
            expires_at=int(request["expires_at"]),
            mode=mode,
            fire_now=request["fire_now"],
            preview_id=int(view["preview_id"]),
            signer=address,
            nonce=wf.fresh_nonce(),
            deadline=now + min(wf.REQUEST_LIFETIME_S, info.max_deadline_secs),
        )
        body = wf.signed_body("StrategyStart", message, key, address, info, document=text)
        try:
            receipt = await client.start(body)
        except wf.ServiceError as refusal:
            if refusal.code == "NOT_AGENT_ACCOUNT":
                return await _not_agent_answer(refusal, account)
            if refusal.code == "HOSTED_SIGNER_NOT_ENABLED":
                return await _not_enabled(refusal, account, hosted, args.open)
            if refusal.code == "ACCOUNT_STRATEGY_LIMIT":
                return _limit(refusal, account)
            raise
        except wf.OutcomeUnknown as exc:
            found = None
            try:
                found = await _confirm_start(client, account, view["strategy_id"], mode)
            except (wf.WorkflowError, wf.ServiceError, wf.OutcomeUnknown, KeyError, TypeError, ValueError):
                pass
            if found is None:
                return {
                    "error": f"{exc}. The start may or may not have been accepted; nothing is resent automatically.",
                    "outcome": "unknown",
                    "account_id": account,
                    "strategy_id": view["strategy_id"],
                    "version_hash": view["version_hash"],
                    "app_runs_url": _runs_page(_app(), account),
                    "next": f"Run strategy status --all --account-id {account} before starting again: it lists this "
                    f"account's strategies, and one with strategy_id {view['strategy_id']} is this start.",
                }
            receipt = {**found, "confirmed_by_read": True}
    app = _app()
    run_id = receipt.get("run_id")
    result = {
        "started": True,
        "run_id": run_id,
        "strategy_id": receipt.get("strategy_id"),
        "label": args.label,
        "mode": receipt.get("mode"),
        "mode_text": wf.MODE_TEXTS.get(str(receipt.get("mode")), receipt.get("mode")),
        "account_id": account,
        **wf.document_view(doc),
        "version_hash": view["version_hash"],
        "preview_id": view["preview_id"],
        "signer": address,
        "hosted_signer": hosted,
        "expires_at": wf.iso(request["expires_at"]),
        "fire_now": request["fire_now"],
        "warnings": view["warnings"],
        "figures": view["figures"],
        "status_url": receipt.get("status_url"),
        "app_run_url": _run_page(app, run_id),
        "app_runs_url": _runs_page(app, account),
        "next": f"strategy status {run_id} (the service runs it; this computer can be off)",
    }
    if receipt.get("confirmed_by_read"):
        result["confirmed_by_read"] = True
    return result


def _limit(refusal: wf.ServiceError, account: int) -> dict:
    """A start refused because the account already runs its one Live strategy. Nothing started; the result links
    the strategy in the way, and the account's strategies."""
    result: dict[str, Any] = {**refusal.result(), "account_id": account}
    app = _app()
    live = refusal.details.get("live_strategy")
    holder = wf._run_ref(live.get("run_id") if isinstance(live, dict) else live)
    if holder:
        result["app_run_url"] = _run_page(app, holder)
    result["app_runs_url"] = _runs_page(app, account)
    return result


async def _run_status(client: wf.WorkflowClient, run_id: int, activity: int = 20) -> dict:
    run = await client.run(run_id)
    account = int(wf.uint_text(str(run.get("account_id")), 64))
    wf.agent_account(account)
    page = await client.activity(run_id, limit=activity)
    app = _app()
    view = wf.run_view(run)
    view["activity"] = [wf.activity_view(e) for e in page.get("activity") or [] if isinstance(e, dict)]
    if isinstance(page.get("next_before"), str):
        view["activity_next_before"] = page["next_before"]
    view["app_run_url"] = _run_page(app, run_id)
    view["app_runs_url"] = _runs_page(app, account)
    if view.get("detail") is None and view.get("detail_reason") == "read_grant_required":
        view["note"] = (
            "The service shows a run's result, positions and protection only under the agent key's read grant, and "
            "did not verify one: run agent status, then try again."
        )
    return view


# Pages of the account's runs a listing reads (100 each) before it says it stopped short.
MAX_RUN_PAGES = 10
PAGE_RUNS = 100


async def _account_runs(client: wf.WorkflowClient, account: int) -> tuple[list[dict], Any, bool]:
    """The account's runs, newest first, every state: up to MAX_RUN_PAGES pages of PAGE_RUNS, its Live strategy, and
    whether older runs were left unread. The running ones are told apart here (wf.is_running): the service's
    `active` filter lists only the Live one."""
    runs: list[dict] = []
    live = None
    before: str | None = None
    for page_number in range(MAX_RUN_PAGES):
        page = await client.account_runs(account, state="all", limit=PAGE_RUNS, before=before)
        if page_number == 0:
            live = page.get("live_strategy")
        runs += [r for r in page.get("runs") or [] if isinstance(r, dict)]
        cursor = page.get("next_before")
        before = str(cursor) if isinstance(cursor, (str, int)) and wf.UINT_RE.fullmatch(str(cursor)) else None
        if before is None:
            return runs, live, False
    return runs, live, True


def _unread_text(runs: list[dict]) -> str:
    return (
        f"The account has more strategies than the kit reads at once: only the newest {len(runs)} were checked, so "
        "an older one may still be running (strategy status --all --before RUN_ID pages through the rest)."
    )


async def cmd_status(args) -> dict:
    if args.before is not None and not args.all:
        raise wf.WorkflowError("--before pages strategy status --all: add --all")
    if args.account_id is not None or not _env.agent_account_ids():
        wf.agent_account(args.account_id)
    async with _client() as client:
        if args.run_id is not None:
            return await _run_status(client, args.run_id)
        account = wf.agent_account(args.account_id)
        older = None
        unread = False
        if args.all:
            before = str(args.before) if args.before is not None else None
            listing = await client.account_runs(account, state="all", limit=args.limit, before=before)
            runs = [r for r in listing.get("runs") or [] if isinstance(r, dict)]
            live = listing.get("live_strategy")
            # The service's cursor for the next, older page: without it, a longer history would end here unsaid.
            cursor = listing.get("next_before")
            older = str(cursor) if isinstance(cursor, (str, int)) and wf.UINT_RE.fullmatch(str(cursor)) else None
        else:
            all_runs, live, unread = await _account_runs(client, account)
            runs = [r for r in all_runs if wf.is_running(r)]
        app = _app()
        result: dict[str, Any] = {
            "account_id": account,
            "app_runs_url": _runs_page(app, account),
            "live_strategy": live if isinstance(live, dict) else None,
            "strategies": [{**wf.summary_row(r), "app_run_url": _run_page(app, r.get("run_id"))} for r in runs],
        }
        running = [r for r in runs if wf.is_running(r)]
        if len(running) == 1:
            result["running_strategy"] = await _run_status(client, int(running[0]["run_id"]))
        if unread:
            result["listing_truncated"] = True
            result["next"] = _unread_text(all_runs)
        elif not runs:
            if args.before is not None:
                result["next"] = f"No strategy on this account is older than #{args.before}."
            else:
                result["next"] = (
                    "No strategies here yet: strategy draft, then preview and start."
                    if args.all
                    else "No strategy is running on this account (strategy status --all lists ended ones). To add "
                    "one: strategy draft, then preview and start."
                )
        elif len(running) > 1:
            result["next"] = "strategy status RUN_ID shows one strategy's rules, positions, result and activity."
        if older is not None:
            result["next_before"] = older
            more = f"Older strategies follow: strategy status --all --before {older} lists the next {args.limit}."
            result["next"] = f"{more} {result['next']}" if result.get("next") else more
        return result


async def cmd_stop(args) -> dict:
    if args.all and args.run_id is not None:
        raise wf.WorkflowError("Stop one strategy (strategy stop RUN_ID) or every running one (--all), not both")
    if not args.all and args.run_id is None:
        raise wf.WorkflowError("Name the strategy to stop (strategy stop RUN_ID), or stop every running one with --all")
    key, address = wf.agent_signer()
    # Refuse a named account before asking the service anything; otherwise the run's own account is checked.
    if args.account_id is not None or not _env.agent_account_ids():
        wf.agent_account(args.account_id)
    if args.all:
        return await _stop_all(args, key, address)
    async with _client() as client:
        run = await client.run(args.run_id)
        account = wf.agent_account(int(wf.uint_text(str(run.get("account_id")), 64)))
        if args.account_id is not None and args.account_id != account:
            raise wf.WorkflowError(f"Strategy #{args.run_id} belongs to account {account}, not {args.account_id}")
        if not wf.is_running(run):
            return _stop_result(args.run_id, run, None, account, args.keep_position, already=True)
        info = await _service(client)
        return await _stop_one(client, info, key, address, account, run, args.keep_position)


async def _stop_one(
    client: wf.WorkflowClient, info: wf.ServiceInfo, key: str, address: str, account: int, run: dict, keep: bool
) -> dict:
    """Sign and send one stop, then read the strategy back for its state."""
    run_id = int(wf.uint_text(str(run.get("run_id")), 64))
    if keep and run.get("mode") == "paper":
        raise wf.WorkflowError(
            f"Strategy #{run_id} is a Paper strategy, which stops only by closing: leave out --keep-position. "
            "Nothing was signed.",
            code="PAPER_KEEP_REFUSED",
        )
    request = wf.stop_request(
        info,
        account_id=account,
        strategy_id=int(wf.uint_text(str(run.get("strategy_id")), 64)),
        run_id=run_id,
        signer=address,
        nonce=wf.fresh_nonce(),
        deadline=int(time.time()) + min(wf.REQUEST_LIFETIME_S, info.max_deadline_secs),
        keep_position=keep,
    )
    body = wf.signed_body("StrategyStop", request, key, address, info)
    try:
        await client.stop(run_id, body)
    except wf.OutcomeUnknown as exc:
        try:
            after = await client.run(run_id)
        except (wf.WorkflowError, wf.ServiceError):
            after = {}
        # Only a run read back as ended, or closing, confirms the stop; an unreadable one is still unknown.
        if not after or (wf.is_running(after) and after.get("status") != "closing"):
            app = _app()
            return {
                "error": f"{exc}. The stop may or may not have been accepted; nothing is resent automatically.",
                "outcome": "unknown",
                "run_id": str(run_id),
                "app_run_url": _run_page(app, run_id),
                "app_runs_url": _runs_page(app, account),
                "next": f"Run strategy status {run_id}: if it is still running, run strategy stop {run_id} again "
                "(a second stop of an ended strategy changes nothing).",
            }
        return {**_stop_result(run_id, after, address, account, keep), "confirmed_by_read": True}
    try:
        after = await client.run(run_id)
    except (wf.WorkflowError, wf.ServiceError):
        after = run  # the stop stands; strategy status follows its close
    return _stop_result(run_id, after, address, account, keep)


async def _stop_all(args, key: str, address: str) -> dict:
    """Stop every running strategy on the account, one signed stop each. A refusal of one does not hold up the
    others; each result says what happened to its strategy."""
    account = wf.agent_account(args.account_id)
    app = _app()
    async with _client() as client:
        runs, _, unread = await _account_runs(client, account)
        running = [r for r in runs if wf.is_running(r)]
        results = []
        if running:
            info = await _service(client)
            for summary in running:
                try:
                    results.append(await _stop_one(client, info, key, address, account, summary, args.keep_position))
                except (wf.ServiceError, wf.WorkflowError) as refusal:
                    if isinstance(refusal, wf.ServiceError):
                        error = refusal.result()
                    else:
                        error = {"error": str(refusal), **({"error_code": refusal.code} if refusal.code else {})}
                    results.append({"run_id": str(summary.get("run_id")), **error})
    result: dict[str, Any] = {"account_id": account, "stopped": results, "app_runs_url": _runs_page(app, account)}
    if unread:
        # Never "nothing is running": a strategy older than what was read may be.
        result["listing_truncated"] = True
        result["error"] = _unread_text(runs)
        result["error_code"] = "LISTING_TRUNCATED"
    if not running:
        result["note"] = (
            f"No strategy is running among the newest {len(runs)}: nothing was signed."
            if unread
            else "No strategy is running on this account: nothing was signed."
        )
        return result
    failed = [r["run_id"] for r in results if r.get("error")]
    if failed and "error" not in result:
        result["error"] = (
            f"{len(failed)} of {len(results)} running strateg{'y was' if len(results) == 1 else 'ies were'} not "
            "stopped (see each one's error and next)"
        )
        result["error_code"] = "STOP_INCOMPLETE"
    result["note"] = (
        f"Stopped {len(results) - len(failed)} of {len(results)} running strateg{'y' if len(results) == 1 else 'ies'}"
        + (
            f"; not stopped: {', '.join('#' + str(r) for r in failed)} (see each one's error and next)"
            if failed
            else ""
        )
        + "."
    )
    result["next"] = "strategy status RUN_ID follows each one's close."
    return result


def _stop_result(run_id: int, after: dict, signer: str | None, account: int, keep: bool, already: bool = False) -> dict:
    """A stop's result from a read of the strategy after it, or from the read alone when it had already ended
    (`signer` None: nothing was signed)."""
    app = _app()
    row = wf.summary_row(after) if after else {}
    result: dict[str, Any] = {
        "run_id": str(run_id),
        "label": row.get("label"),
        "mode": row.get("mode"),
        "state": row.get("state"),
        "status": row.get("status"),
        "end_reason": row.get("end_reason"),
        "keep_position": keep,
        **({"signer": signer} if signer else {}),
        "app_run_url": _run_page(app, run_id),
        "app_runs_url": _runs_page(app, account),
    }
    if already:
        result["already_ended"] = True
        result["note"] = "It had already ended: nothing was signed."
        result["next"] = f"strategy status {run_id} shows how it ended."
        return result
    if keep:
        result["note"] = (
            "It ends now. Its positions stay open as yours, with no stop-loss from the strategy; a stop you placed "
            "on the exchange yourself stays. It does not cancel other orders on the account."
        )
    else:
        result["note"] = (
            "It closes the strategy's positions, then ends (status closing, then ended). It does not cancel other "
            "orders on the account."
        )
    result["next"] = f"strategy status {run_id} follows it until it has ended."
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


def label_arg(text: str) -> str:
    """A strategy's name as the user gives it: plain text up to 64 characters, shown as text and never followed."""
    label = text.strip()
    if not label or len(label) > wf.MAX_LABEL_CHARS or any(not c.isprintable() for c in label):
        raise argparse.ArgumentTypeError(f"a label is 1 to {wf.MAX_LABEL_CHARS} printable characters")
    return label


def percent_arg(text: str) -> str:
    try:
        return wf.positive_amount(text.strip().removesuffix("%"), "the percentage", places=9)
    except wf.WorkflowError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def usd_arg(text: str) -> str:
    try:
        return wf.positive_amount(text.strip(), "the amount", places=6)
    except wf.WorkflowError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def bps_arg(low: int, high: int):
    def parse(text: str) -> int:
        if not text.isascii() or not text.isdecimal() or not low <= int(text) <= high:
            raise argparse.ArgumentTypeError(f"{low} to {high} bps")
        return int(text)

    return parse


def rounds_arg(text: str) -> int:
    if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= wf.MAX_ROUNDS + 1:
        raise argparse.ArgumentTypeError(f"1 to {wf.MAX_ROUNDS}")
    return int(text)


DRAFT_HELP = """Write a basket strategy from flags, checked by the service (nothing starts). The same shape as the in-app
chat's basket: every order is sent at once when the entry fires.

  --order SYMBOL:buy|sell:USD|QTY<ASSET>   an immediate order: USD as a plain number (500), or a base quantity with
                                        the market's base asset after it (1SOL, 0.05BTC); repeatable, one per market
  --trigger start                       the entry: right after the start (the default)
            price:SYMBOL:up|down:LEVEL  when SYMBOL's mark price crosses LEVEL
            every:PERIOD                every PERIOD (90m, 4h, 1d, 1w), counted from the start, for --rounds rounds
                                        (--first-now: the first round at the start); then it holds what it bought
  --stop-loss PCT, --take-profit PCT    from the entry price, on every market; when one is hit, it closes what the
                                        strategy holds and ends
  --exit-after DURATION                 after the entry, close everything and end once this long has passed
  --max-loss USD                        the strategy's loss limit: past it, it closes everything and ends

e.g. strategy draft --order eth:buy:0.1ETH --trigger every:1d --rounds 10 --stop-loss 10 --max-loss 300
     strategy draft --order sol:buy:200 --trigger price:sol:down:120 --stop-loss 5 --take-profit 8 --exit-after 3d"""


def _add_terms(command, modes: tuple[str, ...], default: str) -> None:
    command.add_argument("--file", required=True, help="the strategy document (from strategy draft, or your own)")
    command.add_argument(
        "--mode",
        choices=modes,
        default=default,
        help="paper is Paper: fills are simulated and nothing is sent; live is Live"
        + ("; draft checks the document alone" if "draft" in modes else ""),
    )
    command.add_argument("--account-id", type=int, help="the recorded Agent account (default: the only one)")
    command.add_argument(
        "--expires",
        default=wf.DEFAULT_EXPIRES,
        help=f"when the run ends: 7d, 12h, 90m, Unix seconds or an ISO time (1 hour to 365 days less 5 minutes; default "
        f"{wf.DEFAULT_EXPIRES})",
    )
    command.add_argument(
        "--fire-now",
        action="store_true",
        help="a price crossing already met at the start fires at once (by default it waits for a fresh crossing)",
    )
    command.add_argument("--label", type=label_arg, help=f"a name for it, up to {wf.MAX_LABEL_CHARS} characters")
    command.add_argument("--paper-balance", type=usd_arg, help="Paper: the USD balance its margin starts from")
    command.add_argument(
        "--paper-start", choices=("empty", "copy"), help="Paper: start flat, or copy the account's positions"
    )
    command.add_argument(
        "--paper-slippage-bps", type=bps_arg(0, 100), help="Paper: the slippage each simulated fill assumes"
    )
    command.add_argument("--paper-fee-tier", help="Paper: a named fee tier (the preview lists them)")


def build_parser(prog: str = "strategy"):
    parser = JsonArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)

    info = actions.add_parser(
        "info",
        help="The service's environment, hosted signer, modes and what it runs, and whether hosted runs are "
        "enabled (read-only)",
    )
    info.add_argument(
        "--open",
        action="store_true",
        help="if hosted runs are not enabled, open the app's Enable hosted runs step (once, like the setup links)",
    )

    draft = actions.add_parser(
        "draft",
        help="Draft a basket strategy from flags, checked by the service; nothing starts",
        description=DRAFT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    draft.add_argument("--order", action="append", metavar="SYMBOL:SIDE:USD|QTY<ASSET>", help="repeatable")
    draft.add_argument("--trigger", default="start", metavar="SPEC", help="the entry (see above; default start)")
    draft.add_argument("--rounds", type=rounds_arg, help="with --trigger every: how many times it buys in all")
    draft.add_argument("--first-now", action="store_true", help="with --trigger every: the first round at the start")
    draft.add_argument("--stop-loss", type=percent_arg, metavar="PCT", help="percent from the entry")
    draft.add_argument("--take-profit", type=percent_arg, metavar="PCT", help="percent from the entry")
    draft.add_argument("--exit-after", metavar="DURATION", help="close everything and end this long after the entry")
    draft.add_argument("--max-loss", type=usd_arg, metavar="USD", help="the strategy's loss limit")
    draft.add_argument(
        "--max-slippage-bps",
        type=bps_arg(1, wf.MAX_SLIPPAGE_BPS),
        default=wf.DEFAULT_MAX_SLIPPAGE_BPS,
        help=f"the most each immediate order may move from the mark (default {wf.DEFAULT_MAX_SLIPPAGE_BPS})",
    )
    draft.add_argument("--account-id", type=int, help="check against this recorded Agent account")
    draft.add_argument("--out", help="file to write (default: the kit's state directory)")
    draft.add_argument("--force", action="store_true", help="replace an existing --out file")

    validate = actions.add_parser("validate", help="Check a document with the service (unsigned; writes nothing)")
    _add_terms(validate, wf.CHECK_MODES, "draft")
    preview = actions.add_parser(
        "preview", help="What a Paper or Live run of the strategy would do now, with its margin (unsigned)"
    )
    _add_terms(preview, wf.MODES, wf.DEFAULT_MODE)
    start = actions.add_parser(
        "start", help="Start a strategy: previews it, then signs StrategyStart with the agent key"
    )
    _add_terms(start, wf.MODES, wf.DEFAULT_MODE)
    start.add_argument(
        "--no-open",
        dest="open",
        action="store_false",
        help="if refused until hosted runs are enabled, do not open the app's Enable hosted runs step",
    )

    status = actions.add_parser(
        "status", help="A strategy's status, or the Agent account's running strategies (read-only)"
    )
    status.add_argument("run_id", nargs="?", type=run_id_arg)
    status.add_argument("--account-id", type=int)
    status.add_argument("--all", action="store_true", help="list ended strategies too")
    status.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1-100")
    status.add_argument(
        "--before",
        type=run_id_arg,
        metavar="RUN_ID",
        help="with --all, the strategies older than this one: the next page, from next_before",
    )

    stop = actions.add_parser(
        "stop",
        help="Stop a strategy, or every running one with --all, and close its positions (signs StrategyStop with the "
        "agent key); --keep-position leaves them open",
    )
    stop.add_argument("run_id", nargs="?", type=run_id_arg)
    stop.add_argument("--all", action="store_true", help="stop every running strategy on the account")
    stop.add_argument("--account-id", type=int)
    stop.add_argument(
        "--keep-position",
        action="store_true",
        help="end it leaving its positions open, with no stop-loss from the strategy (Live only); by default a stop "
        "closes the strategy's positions",
    )
    return parser


def main(prog: str = "strategy"):
    args = build_parser(prog).parse_args()
    try:
        _env.load_env()
        output_result(asyncio.run(run(args)))
    except Exception as exc:
        # Never render an unexpected exception's text: it could carry request bodies.
        output_error(f"{type(exc).__name__}: {prog} {args.action} failed")


if __name__ == "__main__":
    main("workflow")
