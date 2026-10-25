"""Hosted trading strategies: draft, validate, preview, start, status and stop.

The kit is a thin client of Reya's hosted strategy service. `draft` fills the
service's `basket` template from flags: the service expands it into the strategy
document (`reya.workflow/2`), checks it against the live markets, and the kit
writes that document once it has checked it is what the flags asked for. The kit
never writes a strategy document itself. `validate` and `preview` ask the service,
which is the only evaluator, and `draft` and `preview` return `app_review_url`, the
Reya app's page where the user reviews the draft and starts it with their own
wallet; `start` and `stop` are signed with the agent key from `agent init` (never
the owner key) and refused for any account that is not the recorded Agent account,
or that Reya does not report as an Agent account (`AGENTPERP`): the refusal's `next`
names the command that switches the kit to the wallet's Agent account. Once started,
a strategy lives on the service: this computer can be off. An account may run a few
strategies at once (`strategy info`), one per market. Every start needs the owner
to have approved the service's hosted signer once: `info` says whether they have,
and links the app's setup page, whose Enable hosted runs step approves it
(`info --open` opens it); a start refused for it opens that page once.

    strategy draft --trigger price:eth:up:3000 --order eth:sell:1000:3300 --order btc:buy:500 \
        --allocation 2000 --expires 7d
    strategy validate --file <draft.json>
    strategy preview --file <draft.json>
    strategy start --file <draft.json> [--label NAME]
    strategy status [RUN_ID]
    strategy stop RUN_ID | --all

`workflow` is an alias: `python -m agent_kit.scripts.workflow` runs the same commands.
"""

from typing import Any

import argparse
import asyncio
import base64
import json
import os
import shlex
import time
from decimal import Decimal
from pathlib import Path

import aiohttp

from agent_kit.scripts import _env
from agent_kit.scripts import _workflow as wf
from agent_kit.scripts import agent
from agent_kit.scripts._account_data import AccountDataClient
from agent_kit.scripts._common import JsonArgumentParser, output_error, output_result, positive_decimal_arg
from agent_kit.scripts._http import RegionUnavailableError
from agent_kit.scripts._margin import load_snapshot
from agent_kit.scripts._v2 import V2Client, rows


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
    "Hosted runs are not enabled for this wallet: every strategy start, watch-only ones included, needs the owner to "
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
    # Run reads carry the agent key's read grant (see wf.ReadGrants); writes are signed requests of their own.
    return wf.WorkflowClient(wf.service_url(), read_grants=wf.ReadGrants())


def _document(path: str) -> tuple[str, dict, int]:
    """The document's text and parsed form, after the schema, Agent-account and environment checks."""
    text, doc = wf.load_document(path)
    wf.check_schema(doc)
    account = wf.agent_account(wf.document_account(doc))
    environment, _ = wf.kit_environment()
    if wf.envelope(doc).get("environment") != environment:
        raise wf.WorkflowError(
            f"The document is for {wf.envelope(doc).get('environment')!r}, but the kit is configured for {environment}"
        )
    return text, doc, account


def _invalid(answer: dict) -> dict:
    errors = [e for e in answer.get("errors") or [] if isinstance(e, dict)]
    first = errors[0] if errors else {}
    where = f" ({first['path']})" if isinstance(first.get("path"), str) else ""
    message = first.get("message") or "the service listed no reason"
    return {"ok": False, "error": f"The strategy is invalid: {message}{where}", "errors": errors}


async def _refused(answer: Any, account: int) -> dict:
    """A validate or preview answer that is not `ok`. The service's `not_agent_account` (its Agent-account check,
    the start's next) gets the exact fix, as a refused start does."""
    result = _invalid(answer if isinstance(answer, dict) else {})
    not_agent = next((e for e in result["errors"] if e.get("code") == "not_agent_account"), None)
    if not_agent is not None:
        hint = wf.AGENT_ACCOUNT_REASONS.get(str(not_agent.get("reason")), wf.ERROR_HINTS["NOT_AGENT_ACCOUNT"])
        result["next"] = hint
        result.update(await _not_agent_account(account) or {})
    return result


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


def _draft_path(args, params: dict) -> Path:
    if args.out:
        path = Path(args.out).expanduser()
    else:
        markets = "-".join(wf.base_asset(m) for m in sorted({o["market"] for o in params["entry"]["orders"]}))
        path = wf.drafts_directory() / f"{markets}-{params['trigger']['kind']}-{int(time.time())}.json"
    if path.exists() and not args.force:
        raise wf.WorkflowError(f"{path} exists; pass --force to replace it or --out for another file")
    return path


async def cmd_draft(args) -> dict:
    """Fill the service's `basket` template from the flags. The service expands it into the document and checks it
    against the live markets; the kit keeps that document, after checking it is what the flags asked for."""
    environment, _ = wf.kit_environment()
    account = wf.agent_account(args.account_id)
    orders = [wf.parse_order(spec) for spec in args.order or []]
    orders += [wf.parse_resting(spec, args.post_only) for spec in args.resting or []]
    expires_at = wf.parse_expiry(args.expires)
    if expires_at > int(time.time()) + wf.MAX_LIFETIME_S:
        raise wf.WorkflowError("--expires is at most 90 days ahead")
    params = wf.basket_params(
        environment=environment,
        account_id=account,
        allocation=args.allocation,
        expires_at=expires_at,
        bound_bps=args.bound_bps,
        max_resting=args.max_resting,
        trigger=wf.parse_trigger(args.trigger),
        conditions=[wf.parse_condition(spec) for spec in args.conditions or []],
        orders=orders,
        total=args.total,
        exits=wf.parse_exits(args.exit or []),
        repeat=wf.REPEATS[args.repeat],
        on_stop=wf.ON_STOP[args.on_stop] if args.on_stop else None,
    )
    path = _draft_path(args, params)
    async with _client() as client:
        if any(c["kind"] == "available_balance" for c in params.get("if") or []):
            # Only a service that prices the account's margin takes the balance condition.
            if not (await _service(client)).reads_balance:
                raise wf.WorkflowError(
                    "The strategy service does not read the account's balance yet, so it cannot take "
                    "--if balance:…; leave it out, or use a funding or position condition"
                )
        answer = await client.expand(params)
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        return {**await _refused(answer, account), "params": params}
    doc = wf.check_canonical(None, answer.get("canonical"), answer.get("version_hash"))
    wf.check_expansion(params, doc)
    try:
        # The canonical form itself: the file's bytes are what the version hash is over.
        path.write_text(answer["canonical"], encoding="utf-8")
    except OSError as exc:
        raise wf.WorkflowError(f"Cannot write {path}: {exc.strerror}") from None
    return {
        "file": str(path),
        **wf.document_view(doc),
        "document": doc,
        "version_hash": answer["version_hash"],
        "warnings": answer.get("warnings") or [],
        "account_id": account,
        "params": params,
        "app_review_url": _review_page(_app(), doc),
        "next": f"strategy preview --file {path} (the service sizes every order against the live market; nothing starts)",
    }


async def cmd_validate(args) -> dict:
    text, doc, account = _document(args.file)
    if not_agent := await _agent_account_refusal(account):
        return not_agent
    async with _client() as client:
        answer = await client.validate(text)
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        return await _refused(answer, account)
    canonical = wf.check_canonical(doc, answer.get("canonical"), answer.get("version_hash"))
    return {
        "ok": True,
        "file": args.file,
        "account_id": account,
        **wf.document_view(canonical),
        "version_hash": answer["version_hash"],
        "canonical": answer["canonical"],
        "warnings": answer.get("warnings") or [],
        "next": f"strategy preview --file {args.file}",
    }


async def market_steps(symbols: list[str]) -> dict[str, Decimal]:
    """Each market's lot (`qtyStepSize`) from its public V2 definition, for the preview's next step up.

    Best effort: any failure returns what it has (nothing), and the preview goes on without the next step."""
    try:
        async with V2Client() as client:
            definitions = rows(await client.get("/perpMarketDefinitions"))
    except Exception:
        return {}
    steps = {}
    for row in definitions:
        if isinstance(row, dict) and row.get("symbol") in symbols:
            try:
                step = Decimal(str(row["qtyStepSize"]))
            except Exception:
                continue
            if step.is_finite() and step > 0:
                steps[row["symbol"]] = step
    return steps


async def available_balance(account: int) -> Decimal | None:
    """The account's available balance: equity less initial margin, the `initial_margin_buffer_usd` that
    `query margin` reports, from the same V2 reads. Best effort: None when it cannot be read, and the preview goes
    on without the balance warnings."""
    try:
        async with AccountDataClient(config=_env.get_trading_config(include_private_key=False)) as client:
            snapshot = await load_snapshot(client, account)
        return snapshot.margin.margin_balance - snapshot.settled().im
    except Exception:
        return None


async def _service_reads_balance(client: wf.WorkflowClient) -> bool:
    """Whether the service prices the account's margin (and so sends the balance warnings itself). False when
    `/v1/info` cannot be read: the kit's own warnings never repeat one the service already gave."""
    try:
        return (await _service(client)).reads_balance
    except (wf.WorkflowError, wf.ServiceError):
        return False


async def _other_budgets(client: wf.WorkflowClient, account: int) -> tuple[Decimal, int] | None:
    """The remaining budgets of the account's live strategies, and how many: the other half of
    `budgets_above_balance`. None when they cannot be read."""
    try:
        live = [r for r in await _account_runs(client, account) if r.get("state") in wf.LIVE_STATES]
        total = Decimal(0)
        for summary in live:
            run = await client.run(int(wf.uint_text(str(summary.get("run_id")), 64)))
            total += Decimal(wf.decimal_value((run.get("allocation") or {}).get("remaining_usd")))
        return total, len(live)
    except (wf.WorkflowError, wf.ServiceError, KeyError, TypeError, ValueError):
        return None


async def cmd_preview(args) -> dict:
    text, doc, account = _document(args.file)
    if not_agent := await _agent_account_refusal(account):
        return not_agent
    async with _client() as client:
        answer = await client.preview(text)
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            return await _refused(answer, account)
        sized = any(
            isinstance(t, dict) and (t.get("orders") or t.get("resting")) for t in answer.get("start_transitions") or []
        )
        view = wf.preview_view(answer, doc, await market_steps(wf.document_markets(doc)) if sized else None)
        # A service that prices the account's margin sends the two balance warnings itself; otherwise they are the
        # kit's own, from the balance the kit reads. Never both.
        service_warns = await _service_reads_balance(client)
        balance = None if service_warns else await available_balance(account)
        budgets = await _other_budgets(client, account) if balance is not None else None
    view["warnings"] += wf.balance_warnings(view, balance, budgets)
    view["available_balance_usd"] = None if balance is None else format(balance.normalize(), "f")
    view["balance_warnings_from"] = "service" if service_warns else ("kit" if balance is not None else None)
    view["file"] = args.file
    view["account_id"] = account
    view["app_review_url"] = _review_page(_app(), doc)
    view["next"] = (
        f"After the user confirms the rules, the orders and the allocation above: strategy start --file {args.file}"
        + (
            " with --fire-now to act on the first fresh value, or --wait-for-rearm to wait for a fresh crossing: the "
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
    """The pilot caps, before anything is signed: the allocation, and every order the document can send."""
    allocation = Decimal(wf.decimal_value(wf.envelope(doc).get("allocation_usd")))
    if allocation > info.max_allocation_usd:
        raise wf.WorkflowError(
            f"The allocation {wf.usd_text(allocation)} is above the pilot cap of {wf.usd_text(info.max_allocation_usd)}",
            code="ABOVE_PILOT_LIMIT",
        )
    for order, usd in wf.order_caps(doc):
        if usd > info.max_order_usd:
            raise wf.WorkflowError(
                f"Order {order} is {wf.usd_text(usd)}, above the pilot cap of {wf.usd_text(info.max_order_usd)} per "
                "order",
                code="ABOVE_PILOT_LIMIT",
            )


async def _confirm_start(
    client: wf.WorkflowClient, account: int, version_hash: str, starter: str, since: int, live: int
):
    """After a lost answer: the account's active strategy that is this document started by this key, among the
    newest `live` (as many as may be live at once)."""
    listing = await client.account_runs(account, state="active", limit=live)
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
        info.check_schema()
        if args.mode not in info.modes:
            raise wf.WorkflowError(
                f"This environment allows {', '.join(map(wf.mode_flag, info.modes)) or 'no'} mode(s), "
                f"not {wf.mode_flag(args.mode)}",
                code="MODE_NOT_ALLOWED",
            )
        hosted = info.active_signer
        if hosted is None:
            raise wf.WorkflowError("The service has no active hosted signer, so no new strategy can start now")
        if info.is_hosted(address):
            raise wf.WorkflowError("The agent key is one of the service's hosted signers; it cannot start strategies")
        _local_caps(doc, info)
        if not_agent := await _agent_account_refusal(account):
            return not_agent
        answer = await client.validate(text)
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            return await _refused(answer, account)
        canonical = wf.check_canonical(doc, answer.get("canonical"), answer.get("version_hash"))
        # A value can cross a level between the user's preview and this start. Ask again, unsigned: a strategy
        # started past its level only waits for the re-arm band, which is not what the user said yes to.
        preview = await client.preview(text)
        if not isinstance(preview, dict) or preview.get("ok") is not True:
            return await _refused(preview, account)
        view = wf.preview_view(preview, canonical)
        if view["fire_now_needed"] and not (args.fire_now or args.wait_for_rearm):
            return _already_triggered(args, view)
        now = int(time.time())
        env = canonical["envelope"]
        request = wf.start_request(
            info,
            account_id=account,
            version_hash=answer["version_hash"],
            allocation_usd=env["allocation_usd"],
            expires_at=env["expires_at"],
            hosted_signer=hosted,
            mode=args.mode,
            fire_now=args.fire_now,
            signer=address,
            nonce=wf.fresh_nonce(),
            deadline=now + min(wf.REQUEST_LIFETIME_S, info.max_deadline_secs),
        )
        extra = {"document": answer["canonical"]} | ({"label": args.label} if args.label else {})
        body = wf.signed_body("WorkflowStart", request, key, address, info, **extra)
        try:
            run = await client.start(body)
        except wf.ServiceError as refusal:
            if refusal.code == "NOT_AGENT_ACCOUNT":
                not_agent = await _not_agent_account(account) or {}
                return {**refusal.result(), "account_id": account, **not_agent}
            if refusal.code in ("MARKET_IN_USE", "ACCOUNT_STRATEGY_LIMIT"):
                return _taken(refusal, account)
            if refusal.code != "HOSTED_SIGNER_NOT_ENABLED":
                raise
            return await _not_enabled(refusal, account, hosted, args.open)
        except wf.OutcomeUnknown as exc:
            found = None
            try:
                found = await _confirm_start(
                    client, account, answer["version_hash"], address, now, info.live_strategies
                )
            except (wf.WorkflowError, wf.ServiceError, wf.OutcomeUnknown, KeyError, TypeError, ValueError):
                pass
            if found is None:
                return {
                    "error": f"{exc}. The start may or may not have been accepted; nothing is resent automatically.",
                    "outcome": "unknown",
                    "account_id": account,
                    "version_hash": answer["version_hash"],
                    "app_runs_url": _runs_page(_app(), account),
                    "next": "Run strategy status before starting again: it lists this account's live strategies.",
                }
            run = {"run_id": found["run_id"], "state": found["state"], "mode": found["mode"], "confirmed_by_read": True}
    app = _app()
    result = {
        "started": True,
        "run_id": run.get("run_id"),
        "label": args.label,
        "state": run.get("state"),
        "mode": run.get("mode"),
        "mode_text": wf.reason(wf.MODE_TEXTS, run.get("mode")),
        "account_id": account,
        "workflow": wf.describe(canonical),
        "markets": wf.document_markets(canonical),
        "rules": wf.rules(canonical),
        "version_hash": answer["version_hash"],
        "signer": address,
        "hosted_signer": hosted,
        "allocation_usd": env["allocation_usd"],
        "expires_at": wf.iso(env["expires_at"]),
        "fire_now": args.fire_now,
        "now": view["now"],
        "status_url": run.get("status_url"),
        "app_run_url": _run_page(app, run.get("run_id")),
        "app_runs_url": _runs_page(app, account),
        "next": f"strategy status {run.get('run_id')} (the service watches its triggers; this computer can be off)",
    }
    if view["fire_now_needed"]:
        result["already_triggered"] = (
            "The start trigger was already met: the strategy acts on the first fresh value."
            if args.fire_now
            else "The start trigger was already met: the strategy waits until the value moves back past the re-arm "
            "band, then acts on the next crossing."
        )
    for flag in ("replayed", "confirmed_by_read"):
        if run.get(flag):
            result[flag] = True
    return result


def _taken(refusal: wf.ServiceError, account: int) -> dict:
    """A start refused because another strategy holds a market, or the account runs as many as it may. Nothing
    started; the result links the strategy in the way, or the account's strategies."""
    result: dict[str, Any] = {**refusal.result(), "account_id": account}
    app = _app()
    holder = wf._run_ref(refusal.details.get("run_id"))
    if refusal.code == "MARKET_IN_USE" and holder:
        result["app_run_url"] = _run_page(app, holder)
    result["app_runs_url"] = _runs_page(app, account)
    return result


def _already_triggered(args, view: dict) -> dict:
    """A start refused before signing: the start trigger is already met and the user has not chosen what to do."""
    command = f"strategy start --file {args.file}" + (f" --mode {args.mode}" if args.mode != "shadow" else "")
    command += f" --label {shlex.quote(args.label)}" if args.label else ""
    return {
        "error": "The start trigger is already met, so this strategy would act on the first fresh value, or, if it "
        "waits, only once the value moves back past the re-arm band and crosses again. Nothing was signed.",
        "error_code": "ALREADY_TRIGGERED",
        "now": view["now"],
        "start_transitions": view["start_transitions"],
        "file": args.file,
        "next": (
            f"Ask the user which they want: act on the first fresh value ({command} --fire-now), wait for a fresh "
            f"crossing ({command} --wait-for-rearm), or draft a level further away and preview it again."
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


async def _account_runs(client: wf.WorkflowClient, account: int, max_pages: int = 5) -> list[dict]:
    """The account's strategies, newest first, every state: up to `max_pages` pages of 200. Live ones are told
    apart here (wf.is_live), so this does not depend on what the service's `active` filter includes."""
    runs: list[dict] = []
    before: str | None = None
    for _ in range(max_pages):
        page = await client.account_runs(account, state="all", limit=200, before=before)
        runs += [r for r in page.get("runs") or [] if isinstance(r, dict)]
        before = page.get("next_before")
        if not isinstance(before, str) or not wf.UINT_RE.fullmatch(before):
            break
    return runs


async def cmd_status(args) -> dict:
    if args.account_id is not None or not _env.agent_account_ids():
        wf.agent_account(args.account_id)
    async with _client() as client:
        if args.run_id is not None:
            return await _run_status(client, args.run_id)
        account = wf.agent_account(args.account_id)
        if args.all:
            listing = await client.account_runs(account, state="all", limit=args.limit)
            runs = [r for r in listing.get("runs") or [] if isinstance(r, dict)]
        else:
            runs = [r for r in await _account_runs(client, account) if wf.is_live(r)]
        app = _app()
        result: dict[str, Any] = {
            "account_id": account,
            "app_runs_url": _runs_page(app, account),
            "strategies": [{**wf.summary_row(r), "app_run_url": _run_page(app, r.get("run_id"))} for r in runs],
        }
        live = [r for r in runs if r.get("state") in wf.LIVE_STATES]
        if len(live) == 1:
            result["active_run"] = await _run_status(client, int(live[0]["run_id"]))
        if not runs:
            result["next"] = (
                "No strategies here yet: strategy draft, then preview and start."
                if args.all
                else "No live strategy on this account (strategy status --all lists ended ones). To add one: "
                "strategy draft, then preview and start."
            )
        elif len(live) > 1:
            result["next"] = "strategy status RUN_ID shows one strategy's rules, markets, resting orders and firings."
        return result


async def cmd_stop(args) -> dict:
    if args.all and args.run_id is not None:
        raise wf.WorkflowError("Stop one strategy (strategy stop RUN_ID) or every live one (--all), not both")
    if not args.all and args.run_id is None:
        raise wf.WorkflowError("Name the strategy to stop (strategy stop RUN_ID), or stop every live one with --all")
    key, address = wf.agent_signer()
    # Refuse a named account before asking the service anything; otherwise the run's own account is checked.
    if args.account_id is not None or not _env.agent_account_ids():
        wf.agent_account(args.account_id)
    if args.all:
        return await _stop_all(args, key, address)
    async with _client() as client:
        run = await client.run(args.run_id)
        account = wf.agent_account(int(wf.uint_text(run.get("account_id"), 128)))
        if args.account_id is not None and args.account_id != account:
            raise wf.WorkflowError(f"Strategy #{args.run_id} belongs to account {account}, not {args.account_id}")
        if run.get("state") == "stopped":
            return _stop_result(args.run_id, {**run, "already_stopped": True}, run, None, account, args.keep_position)
        info = await _service(client)
        return await _stop_one(client, info, key, address, account, args.run_id, args.keep_position)


async def _stop_one(
    client: wf.WorkflowClient, info: wf.ServiceInfo, key: str, address: str, account: int, run_id: int, keep: bool
) -> dict:
    """Sign and send one stop, then read the strategy back for its markets' closes and stops."""
    request = wf.stop_request(
        info,
        account_id=account,
        run_id=run_id,
        signer=address,
        nonce=wf.fresh_nonce(),
        deadline=int(time.time()) + min(wf.REQUEST_LIFETIME_S, info.max_deadline_secs),
        keep_position=keep,
    )
    body = wf.signed_body("WorkflowStop", request, key, address, info)
    try:
        answer = await client.stop(run_id, body)
    except wf.OutcomeUnknown as exc:
        try:
            after = await client.run(run_id)
        except (wf.WorkflowError, wf.ServiceError):
            after = {}
        if after.get("state") != "stopped":
            app = _app()
            return {
                "error": f"{exc}. The stop may or may not have been accepted.",
                "outcome": "unknown",
                "run_id": str(run_id),
                "app_run_url": _run_page(app, run_id),
                "app_runs_url": _runs_page(app, account),
                "next": f"Run strategy stop {run_id} again: stopping a stopped strategy changes nothing.",
            }
        return _stop_result(run_id, {**after, "confirmed_by_read": True}, after, address, account, keep)
    try:
        after = await client.run(run_id)
    except (wf.WorkflowError, wf.ServiceError):
        after = {}  # the stop stands; its closes show in strategy status
    return _stop_result(run_id, answer, after, address, account, keep)


async def _stop_all(args, key: str, address: str) -> dict:
    """Stop every live strategy on the account, one signed stop each. A refusal of one does not hold up the
    others; each result says what happened to its strategy."""
    account = wf.agent_account(args.account_id)
    app = _app()
    async with _client() as client:
        runs = await _account_runs(client, account)
        live = [r for r in runs if r.get("state") in wf.LIVE_STATES]
        holding = [wf.summary_row(r) for r in runs if r.get("state") not in wf.LIVE_STATES and wf.is_live(r)]
        results = []
        if live:
            info = await _service(client)
            for summary in live:
                run_id = int(wf.uint_text(str(summary.get("run_id")), 64))
                try:
                    results.append(await _stop_one(client, info, key, address, account, run_id, args.keep_position))
                except wf.ServiceError as refusal:
                    results.append({"run_id": str(run_id), **refusal.result()})
    result: dict[str, Any] = {
        "account_id": account,
        "stopped": results,
        "app_runs_url": _runs_page(app, account),
    }
    if not live:
        result["note"] = "No live strategy on this account: nothing was signed."
    else:
        failed = [r["run_id"] for r in results if r.get("error")]
        result["note"] = (
            f"Stopped {len(results) - len(failed)} of {len(results)} live strateg{'y' if len(results) == 1 else 'ies'}"
            + (
                f"; not stopped: {', '.join('#' + str(r) for r in failed)} (see each one's error and next)"
                if failed
                else ""
            )
            + "."
        )
        result["next"] = "strategy status RUN_ID follows each one's closes and the cancellation of its stops."
    if holding:
        result["holding"] = holding
        result["note"] += (
            " Completed strategies that still keep a stop: "
            + "; ".join(row["text"] for row in holding)
            + ". strategy stop RUN_ID cancels one's stop."
        )
    return result


def _stop_result(run_id: int, answer: dict, after: dict, signer: str | None, account: int, asked_keep: bool) -> dict:
    """A stop's result from the service's answer and a read of the strategy after it (its markets' closes and
    stops), or from the read alone when it was already stopped (`signer` None: nothing was signed)."""
    app = _app()
    stop = wf.stop_view(answer.get("stop") or after.get("stop"))
    markets = [wf.market_view(m) for m in after.get("markets") or answer.get("markets") or [] if isinstance(m, dict)]
    resting = [wf.resting_view(r) for r in after.get("resting_orders") or [] if isinstance(r, dict)]
    result = {
        "run_id": answer.get("run_id") or str(run_id),
        "label": after.get("label") or answer.get("label"),
        "state": answer.get("state") or after.get("state"),
        "stop": stop,
        "markets": markets,
        "resting_orders": resting,
        **({"signer": signer} if signer else {}),
        "app_run_url": _run_page(app, run_id),
        "app_runs_url": _runs_page(app, account),
        "note": _stop_note(stop, markets, asked_keep=asked_keep, already=bool(answer.get("already_stopped"))),
        "next": _stop_next(run_id, markets),
    }
    for flag in ("already_stopped", "replayed", "confirmed_by_read"):
        if answer.get(flag):
            result[flag] = True
    return result


def _stop_note(stop: dict | None, markets: list[dict], *, asked_keep: bool, already: bool) -> str:
    """What the stop did, for the user, from what the service says it did."""
    keeps = stop is None or stop["keep_position"]
    if keeps:
        note = (
            "No further step fires; an order already sent completes, and its resting orders are cancelled. Positions "
            "stay open with no stop order from this strategy: its stops are cancelled, and nothing closes a position "
            "unless the user does."
        )
    else:
        note = (
            "No further step fires; an order already sent completes. Its resting orders are cancelled first. Then, "
            "market by market, the service closes this strategy's own share (its own fills, up to the live position) "
            "and cancels that market's stop. Anything held before the strategy, or traded by hand or by another "
            "strategy, stays open."
        )
        closes = [m["close"]["text"] for m in markets if m.get("close")]
        if closes:
            note += " The closes: " + "; ".join(closes) + "."
    if already and keeps != asked_keep:
        chosen = "kept the positions" if keeps else "closes this strategy's own share"
        note = f"It was already stopped, and that stop {chosen}: stopping again changes nothing. {note}"
    return note


def _stop_next(run_id: int, markets: list[dict]) -> str:
    if any((m.get("close") or {}).get("state") in ("pending", "closing") for m in markets):
        return f"strategy status {run_id} follows the closes, then the cancellation of each market's stop"
    if any(isinstance(m.get("protection"), dict) for m in markets):
        return f"strategy status {run_id} shows the cancellation of each market's stop"
    return f"It armed no stop, so there is nothing to cancel. strategy status {run_id}"


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


def resting_cap_arg(text: str) -> int:
    if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= wf.MAX_RESTING:
        raise argparse.ArgumentTypeError(f"at most 1 to {wf.MAX_RESTING} resting orders")
    return int(text)


def label_arg(text: str) -> str:
    """A strategy's name as the user gives it: plain text up to 60 characters, shown as text and never followed."""
    label = text.strip()
    if not label or len(label) > wf.MAX_LABEL_CHARS or any(not c.isprintable() for c in label):
        raise argparse.ArgumentTypeError(f"a label is 1 to {wf.MAX_LABEL_CHARS} printable characters")
    return label


DRAFT_HELP = """Fill the service's basket template: the service expands it into the strategy and checks it against the
live markets, and the kit writes that document. Nothing starts.

  --trigger price:SYMBOL:up|down:LEVEL   when SYMBOL's mark crosses LEVEL
            every:PERIOD[@ANCHOR]       every 5m to 90d, counted from ANCHOR (HH:MM UTC today, Unix seconds or an
                                        ISO time; default 00:00 UTC today)
            at:daily@HH:MM | at:mon..sun@HH:MM   every day, or every week on that day, at HH:MM UTC
            start                       right after the start
  --order SYMBOL:buy|sell:USD|WEIGHT%[:STOP[:TAKE_PROFIT]]   an immediate order (IOC within --bound-bps of the mark)
  --resting SYMBOL:buy|sell:USD|WEIGHT%:PRICE|±OFFSETbps[:STOP[:TAKE_PROFIT]]   a resting limit order (post-only
                                        unless --no-post-only), at PRICE or OFFSET bps from the mark when placed
                                        STOP and TAKE_PROFIT close the whole position in that market on Reya; leave
                                        STOP empty for a take-profit alone: eth:buy:1000::3200
  --exit target:SYMBOL:up|down:LEVEL | profit:PCT | after:DURATION   after entry, the first one met closes the
                                        strategy's own share in every market and ends it. profit:PCT closes at a
                                        +PCT% price move of the strategy's own positions, unleveraged, before fees
                                        and funding. after: up to 90d
  --if funding:SYMBOL:above|below:PCT_PER_HOUR | position:SYMBOL:above|below:QTY | balance:above|below:USD
                                        conditions on the trigger (balance only where strategy info says
                                        reads_balance)
  --on-stop keep-others|close-all       when a stop-loss or take-profit fires: keep the other legs running, or
                                        close them and end (default: close-all when the entry both buys and sells)

e.g. strategy draft --trigger price:eth:up:3000 --order eth:sell:1000:3300 --order btc:buy:500:80000 \\
       --order sol:buy:500 --allocation 2000 --expires 7d"""


def build_parser(prog: str = "strategy"):
    parser = JsonArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
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

    draft = actions.add_parser(
        "draft",
        help="Draft a strategy: the service expands the flags into the document; nothing starts",
        description=DRAFT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    draft.add_argument("--trigger", required=True, metavar="SPEC", help="what starts the entry (see above)")
    draft.add_argument(
        "--order", action="append", metavar="SYMBOL:SIDE:USD|WEIGHT%[:STOP[:TAKE_PROFIT]]", help="repeatable"
    )
    draft.add_argument(
        "--resting",
        action="append",
        metavar="SYMBOL:SIDE:USD|WEIGHT%:PRICE|±OFFSETbps[:STOP[:TAKE_PROFIT]]",
        help="repeatable",
    )
    draft.add_argument("--total", type=positive_decimal_arg, help="USD the weights (25%%) are shares of")
    draft.add_argument(
        "--no-post-only",
        dest="post_only",
        action="store_false",
        help="resting orders may take liquidity when placed (by default they only rest)",
    )
    draft.add_argument("--exit", action="append", metavar="SPEC", help="repeatable, one of each kind")
    draft.add_argument(
        "--on-stop",
        choices=tuple(wf.ON_STOP),
        help="when a stop-loss or take-profit fires: keep-others or close-all (default: close-all for a hedge)",
    )
    draft.add_argument(
        "--repeat",
        choices=tuple(wf.REPEATS),
        default="once",
        help="once (the default), or each: again at every trigger (no --exit; each_crossing is the same)",
    )
    draft.add_argument("--if", dest="conditions", action="append", metavar="SPEC", help="repeatable")
    draft.add_argument("--allocation", required=True, type=positive_decimal_arg, help="USD in total, every order")
    draft.add_argument(
        "--expires", required=True, help="7d, 12h, 90m, Unix seconds or an ISO time with a zone (at most 90 days)"
    )
    draft.add_argument(
        "--bound-bps", type=bound_arg, default=wf.DEFAULT_BOUND_BPS, help="max slippage per immediate order"
    )
    draft.add_argument(
        "--max-resting",
        type=resting_cap_arg,
        default=wf.DEFAULT_MAX_RESTING,
        help=f"resting orders it may have at once (1 to {wf.MAX_RESTING}, default {wf.DEFAULT_MAX_RESTING})",
    )
    draft.add_argument("--account-id", type=int, help="the recorded Agent account (default: the only one)")
    draft.add_argument("--out", help="file to write (default: the kit's state directory)")
    draft.add_argument("--force", action="store_true", help="replace an existing --out file")

    for name, text in (
        ("validate", "Check a document with the service (unsigned; writes nothing)"),
        ("preview", "What the strategy would do right now, sized by the service (unsigned)"),
        ("start", "Start a strategy: signs WorkflowStart with the agent key"),
    ):
        command = actions.add_parser(name, help=text)
        command.add_argument("--file", required=True, help="the strategy document (from strategy draft)")
        if name == "start":
            past = command.add_mutually_exclusive_group()
            past.add_argument(
                "--fire-now", action="store_true", help="if the start trigger is already met, act on the first value"
            )
            past.add_argument(
                "--wait-for-rearm",
                action="store_true",
                help="if the start trigger is already met, start anyway and wait for a fresh crossing",
            )
            command.add_argument(
                "--mode",
                choices=wf.MODES,
                default="shadow",
                help="shadow (the default) is Watch only and sends no orders; paper is Paper; production is Live",
            )
            command.add_argument(
                "--label", type=label_arg, help=f"a name for it, up to {wf.MAX_LABEL_CHARS} characters"
            )
            command.add_argument(
                "--no-open",
                dest="open",
                action="store_false",
                help="if refused until hosted runs are enabled, do not open the app's Enable hosted runs step",
            )

    status = actions.add_parser(
        "status", help="A strategy's status, or the Agent account's live strategies (read-only)"
    )
    status.add_argument("run_id", nargs="?", type=run_id_arg)
    status.add_argument("--account-id", type=int)
    status.add_argument("--all", action="store_true", help="list stopped and completed strategies too")
    status.add_argument("--limit", type=int, default=20, choices=range(1, 201), metavar="1-200")

    stop = actions.add_parser(
        "stop",
        help="Stop a strategy, or every live one with --all, and close what it opened (signs WorkflowStop with the "
        "agent key); --keep-position leaves positions open",
    )
    stop.add_argument("run_id", nargs="?", type=run_id_arg)
    stop.add_argument("--all", action="store_true", help="stop every live strategy on the account")
    stop.add_argument("--account-id", type=int)
    stop.add_argument(
        "--keep-position",
        action="store_true",
        help="leave positions open, with no stop order from the strategy; by default a stop closes the strategy's "
        "own share",
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
