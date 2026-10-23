"""Guided agent setup: an app-approved agent key, a dedicated agentic account, funding, and
optionally hosted runs (approving Reya's hosted runner for workflows).

The kit never asks for the owner's private key. `agent init` generates a signer
of its own, the owner approves that address once in the Reya app (the same
on-chain permission one-click trading uses), the app creates and funds a
dedicated agentic account through deep links, and the kit records which account
the agent key may trade. Every step is derived from API state, so `init` is
idempotent: re-run it after each app action and it resumes at the first
incomplete step. `status` reports the same state without changing anything.

Approval status is read from the legacy ``GET /api/trading/wallet/{owner}/apiWalletStatus``
(the only read that exists today); this non-V2 dependency is recorded in AGENTS.md
under a documented exception (14 October 2026). No private key is ever
printed, logged or included in results.
"""

from typing import Any

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit

from eth_account import Account
from eth_utils import is_address, to_checksum_address

from agent_kit.scripts import _env, _release
from agent_kit.scripts._account_relay import (
    ACCOUNT_TYPES,
    AGENT_PERP,
    SUB_PERP_TYPES,
    AccountRelay,
    SetupError,
    api_integer,
)
from agent_kit.scripts._common import JsonArgumentParser, output_error, output_result, positive_decimal_arg
from agent_kit.scripts._writer import _open_private, state_directory

# Deep links the kit prints for the owner to open in the app. The API page is
# the Next.js route `/api-page` (header: More -> API). The query parameters are
# provisional until the app handles them; keep them in one place.
APP_URLS = {
    "https://api.reya.xyz/v2": "https://app.reya.xyz",
    "https://api-devnet.reya-cronos.network/v2": "https://app-devnet-cf.reya.xyz",
}
DEEP_LINKS = {
    # One page walks the owner through all three steps (approve, create the Agent account, fund).
    # `owner` lets the page refuse to run the steps on any other connected wallet and ask for a switch.
    # `account` (once the kit has recorded one) is the agent account the page uses, whatever its name.
    "setup": "/agents/setup?agent={agent}&owner={owner}{account}{amount}#access={access}",
    # The app's regular page for each step, as a fallback if the setup page does not load.
    "approve": "/api-page?approve={agent}",
    "create_account": "/portfolio?createAccount=1&name={name}&source=agent",
    "transfer": "/portfolio?transfer=1&from={source}&to={target}{amount}",
    # Hosted workflow runs (the `workflow` commands): the Agent account's runs, and one run.
    "workflows": "/agents/workflows?account={account}#access={access}",
    "workflow_run": "/agents/workflows/run?id={run}#access={access}",
    # A drafted workflow to review and start in the app. The document travels in the fragment with the code;
    # `agent` and `owner` (as on the setup link) let the page link the setup page and ask for a wallet switch.
    "workflow_review": "/agents/workflows/run?draft{who}#access={access}&doc={doc}",
}
# The setup page and the workflow pages are unlisted: they only open with this code in the URL
# fragment, which the browser never sends to the server (or remembered in that browser tab). It
# keeps casual visitors out while the kit is private; it is not a secret and not a security
# control. Rotate it together with the app's AGENT_SETUP_ACCESS_SHA256.
SETUP_PAGE_ACCESS_CODE = "V1MZCBVGEhmzidMyjCdW0hqi"
# Steps the setup page covers, for progress reporting while the kit waits. Enable hosted runs is the
# fourth only for a user who chose it (agent init --hosted-runs now) where it is still to do.
SETUP_PAGE_STEPS = {"needs_approval": 1, "needs_agent_account": 2, "needs_funding": 3, "needs_hosted_runs": 4}
AGENT_ACCOUNT_NAME = "Agent account"
# Said when the API marks Agent accounts by type and the recorded one is not among them: the workflow
# service refuses it. Switching needs both .env lines gone, so agent init records the Agent account again.
AGENT_ACCOUNT_MISMATCH = (
    "Account {recorded} is recorded as the Agent account, but Reya marks {marked} as the Agent account. Trading "
    "works on account {recorded}; hosted workflows refuse it. To use the Agent account instead, remove the "
    "PERP_AGENT_ACCOUNT_IDS and PERP_ACCOUNT_ID_1 lines from the kit's .env and run agent init: it records the "
    "Agent account (asking which one when there are several)."
)
API_WALLET_STATUS_PATH = "/api/trading/wallet/{owner}/apiWalletStatus"
# Offered to the user once setup is complete: safe first things to ask the assistant.
TRY_ASKING = [
    "Show my Agent account balance and open positions on Reya.",
    "What is the ETH funding rate on Reya right now?",
    "Place a small post-only buy for ETH 5% below the market, then cancel it.",
    "Show my open orders on Reya.",
]
# Funding guidance: enough for a few minimum-size orders on the cheapest liquid market.
GUIDANCE_MARKET = "ETHRUSDPERP"
# The Reya app shows the rUSD collateral as "USDC"; user-facing text uses the app's name.
# JSON field names keep "rusd" (e.g. funding_guidance.min_order_rusd).
ASSET_LABEL = "USDC"
SUGGESTED_FUNDING_FLOOR = Decimal("50")
# A step's page is opened once; re-running init/status within this window does not reopen it.
REOPEN_AFTER_SECONDS = 30 * 60
PRACTICE_FIRST = {
    "question": "Practise with virtual money first, or set up real trading now?",
    "practice_command": "python -m agent_kit.scripts.paper init --collateral 1000 --fee-bps 3",
    "note": (
        "Paper trading needs no wallet or approval and uses live prices. Real trading continues with the steps below "
        "and uses real funds on mainnet."
    ),
}
# Added to `next` when the wait times out still at approval: the usual cause is the page being
# connected to a different wallet than the owner, which the kit cannot see.
WRONG_WALLET_HINT = (
    "If the setup page asks the user to switch wallets, or shows the steps done while this still waits for "
    "approval, the page is connected to a different wallet than {owner}: the user switches to {owner} there. "
    "If they meant the connected wallet instead, set that one up: remove the PERP_WALLET_ADDRESS_1 line from "
    "the kit's .env and run agent init --owner <that wallet> (the agent key is kept)."
)
# Put before `next` when the page could not be opened here (a server, SSH, WSL without Windows access).
BROWSER_UNAVAILABLE = {
    "ssh": "this is an SSH session",
    "no_display": "this machine has no display",
    "wsl_no_windows_browser": "WSL cannot start Windows programs here",
    "no_browser": "no browser could be started",
}
OPEN_ELSEWHERE = (
    "No browser opened ({why}). Show the user url and ask them to open it on any device where their wallet is: "
    "a phone wallet's in-app browser works, or another computer. "
)
WAIT_AFTER_SHOWING = (
    "Then run agent init --until-ready again: it waits while they finish the page (the link is not opened again)."
)
PERMISSION_COPY = (
    "Approving lets this agent place and cancel orders on your Reya accounts. "
    "It cannot withdraw, transfer or change permissions. Revoke it on the same page at any time."
)
# Hosted runs: the setup page's optional last step, approving the workflow service's hosted signer so
# Reya can run the user's workflows while this computer is off. Offered once setup is ready, never required.
HOSTED_RUNS_STEP = "needs_hosted_runs"
HOSTED_RUNS_CHOICES = ("now", "later")
# The read that finds the hosted signer is optional: it must never hold setup up.
HOSTED_RUNS_INFO_TIMEOUT_S = 5.0
HOSTED_RUNS_QUESTION = "Enable hosted runs now (Reya runs your workflows even when this computer is off), or later?"
HOSTED_RUNS_PERMISSION = (
    "Approving lets Reya's hosted runner ({signer}) place and cancel orders on every account this wallet owns, "
    "not just the Agent account. Reya's service only trades the Agent account, but that limit is the service's "
    "rule, not the approval's. It cannot withdraw, transfer or change permissions. Remove it any time on the API "
    "page."
)
HOSTED_RUNS_LATER = 'Hosted runs are off for now (the user chose later); they can say "enable hosted runs" any time.'


def owner_address(value: str) -> str:
    if not isinstance(value, str) or not is_address(value) or int(value, 16) == 0:
        raise SetupError("A nonzero EVM owner address is required (--owner 0x...)")
    return to_checksum_address(value)


def app_base_url(api_url: str) -> str | None:
    url = os.environ.get("REYA_APP_URL")
    if url:
        try:
            parsed = urlsplit(url)
            port = parsed.port  # urlsplit accepts any port text; reading it is what checks it
        except ValueError:
            raise SetupError("REYA_APP_URL must be a plain https URL") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username
            or port == 0
        ):
            raise SetupError("REYA_APP_URL must be a plain https URL")
        return url.rstrip("/")
    return APP_URLS.get(api_url)


def deep_link(base: str | None, kind: str, **params) -> str | None:
    path = DEEP_LINKS[kind].format(**params)
    return base + path if base else None


def setup_link(
    app: str | None, agent: str, owner: str, account_id: int | None = None, amount: Decimal | None = None
) -> str | None:
    """The setup page for this agent and owner; `account_id` is the recorded Agent account, if any.

    The page resumes at the first step not done, from live data: approve, the Agent account,
    funding, then Enable hosted runs where the deployment has a workflow service.
    """
    return deep_link(
        app,
        "setup",
        agent=agent,
        owner=owner,
        account=f"&account={account_id}" if account_id is not None else "",
        amount=f"&amount={amount}" if amount is not None else "",
        access=SETUP_PAGE_ACCESS_CODE,
    )


def same_setup_page(
    app: str | None, agent: str, owner: str, account_id: int, named: list[int], amount: Decimal | None = None
) -> list[str]:
    """Other links that show the same page as the setup link naming `account_id`.

    Without `account` the page uses the owner's one Agent account (`named`, see agent_accounts). When
    that is the recorded account, both links show the same page: a tab opened before the account was
    recorded is not opened again for it (see show_setup_link).
    """
    without_account = setup_link(app, agent, owner, amount=amount)
    return [without_account] if named == [account_id] and without_account else []


def agent_accounts(accounts: dict[int, str], names: dict[int, str]) -> list[int]:
    """The owner's Agent accounts, as the setup page finds them: the accounts the API reports as AGENTPERP,
    whatever their name. Until the API reports that type (no AGENTPERP account in the list), the
    sub-accounts named exactly "Agent account"."""
    marked = [i for i, kind in accounts.items() if kind == AGENT_PERP]
    return marked or [i for i, kind in accounts.items() if kind == "SUBPERP" and names.get(i) == AGENT_ACCOUNT_NAME]


def agent_account_warning(accounts: dict[int, str], account_id: int) -> str | None:
    """Why the recorded account is not the Agent account the workflow service takes, or None.

    Only once the API reports the type: before that, every sub-account reads SUBPERP and nothing can be told.
    """
    marked = sorted(i for i, kind in accounts.items() if kind == AGENT_PERP)
    if not marked or account_id in marked:
        return None
    which = f"account {marked[0]}" if len(marked) == 1 else "accounts " + ", ".join(map(str, marked))
    return AGENT_ACCOUNT_MISMATCH.format(recorded=account_id, marked=which)


def in_wsl(osrelease: Path = Path("/proc/sys/kernel/osrelease")) -> bool:
    """True inside Windows Subsystem for Linux, where the user's browser is the Windows one."""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in osrelease.read_text().lower()
    except OSError:
        return False


def windows_opener(binfmt: Path = Path("/proc/sys/fs/binfmt_misc")) -> list[str] | None:
    """From WSL, a command that opens a URL in the Windows browser; None when Windows programs cannot run."""
    if not (os.environ.get("WSL_INTEROP") or list(binfmt.glob("WSLInterop*"))):
        return None
    wslview = shutil.which("wslview")
    if wslview:
        return [wslview]
    explorer = shutil.which("explorer.exe") or "/mnt/c/Windows/explorer.exe"
    return [explorer] if os.path.isfile(explorer) else None


def browser_unavailable() -> str | None:
    """Why this machine cannot show the user a web page, or None when it probably can.

    Python's own detection is not enough: on a server it picks a text browser in the terminal,
    and under WSL it picks Linux openers that fail, and it reports success either way.
    """
    if os.environ.get("BROWSER"):
        return None  # the user (or e.g. a remote editor) named an opener: trust it
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return "ssh"  # a browser would open on the remote machine's screen, not the user's
    if in_wsl():
        return None if windows_opener() else "wsl_no_windows_browser"
    if sys.platform not in ("darwin", "win32") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "no_display"
    return None


def _open_on_windows(url: str) -> bool:
    command = windows_opener()
    if not command:
        return False
    try:
        process = subprocess.Popen(
            [*command, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        code = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return True  # still handing the page to the browser
    except OSError:
        return False
    # explorer.exe exits with 1 even when it opened the page; wslview's status is reliable.
    return code == 0 or Path(command[0]).name == "explorer.exe"


def open_in_browser(url: str) -> bool:
    """Best effort: open the deep link in the user's browser; False when it could not be opened."""
    try:
        if in_wsl():
            return _open_on_windows(url)
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def show_setup_link(url: str, same_page: list[str] | None = None) -> dict:
    """Open the step's page once across runs, or say why it cannot be opened here. Never raises.

    `same_page` holds other links to the same page: when one of them was opened, the page is open.
    """
    # Remembering links is a convenience: a state-directory problem must never block setup.
    pages = [url, *(same_page or [])]
    try:
        # A link that differs only by `&amount=` shows the same page (the amount only pre-fills funding).
        already = any(link_recently_opened(link) or opened_amount_variants(link) for link in pages)
    except (OSError, ValueError):
        already = False
    if already:
        return {"opened": False, "link_already_opened": True}
    reason = browser_unavailable()
    if reason is None and open_in_browser(url):
        try:
            remember_opened(url)
        except (OSError, ValueError):
            pass
        return {"opened": True}
    # For someone running the command in a terminal; an assistant reads it from the result.
    print(f"No browser can open here. Open this link on any device where your wallet is: {url}", file=sys.stderr)
    return {"opened": False, "browser_unavailable": reason or "no_browser"}


def _opened_links_path() -> Path:
    return state_directory() / "agent-setup-opened-links.json"


def link_recently_opened(url: str, now: float | None = None) -> bool:
    """True when this exact link was opened by a previous init/status run within REOPEN_AFTER_SECONDS."""
    path = _opened_links_path()
    if not path.exists():
        return False
    try:
        with os.fdopen(_open_private(path, os.O_RDONLY), "r") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return False
    opened_at = data.get(url) if isinstance(data, dict) else None
    current = time.time() if now is None else now
    return isinstance(opened_at, (int, float)) and current - opened_at < REOPEN_AFTER_SECONDS


def opened_amount_variants(url: str, now: float | None = None) -> list[str]:
    """Setup links opened recently that differ from `url` only by `&amount=`.

    The amount only pre-fills the funding form, so such a link shows the same page: a tab opened at
    the funding step (`init --amount 50`) is not opened again for the Enable hosted runs step (by
    init or by `workflow info --open`), nor is one opened before funding for `init --amount 50`.
    """
    try:
        path = _opened_links_path()
        if not path.exists():
            return []
        with os.fdopen(_open_private(path, os.O_RDONLY), "r") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return []  # remembering links is a convenience: a state-directory problem never blocks setup
    current = time.time() if now is None else now
    target = _without_amount(url)
    return [
        link
        for link, opened_at in (data.items() if isinstance(data, dict) else [])
        if link != url
        and _without_amount(link) == target
        and isinstance(opened_at, (int, float))
        and current - opened_at < REOPEN_AFTER_SECONDS
    ]


def _without_amount(url: str) -> str:
    page, _, access = url.partition("#")
    query = "&".join(part for part in page.split("&") if not part.startswith("amount="))
    return f"{query}#{access}"


def remember_opened(url: str, now: float | None = None) -> None:
    path = _opened_links_path()
    data: dict = {}
    if path.exists():
        try:
            with os.fdopen(_open_private(path, os.O_RDONLY), "r") as stream:
                loaded = json.load(stream)
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            data = {}
    data[url] = time.time() if now is None else now
    fd = _open_private(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)


async def funding_guidance(relay: AccountRelay) -> dict | None:
    """Smallest order on the guidance market and a suggested first deposit; None when it cannot be read."""
    try:
        definitions = await relay.request("GET", "/v2/perpMarketDefinitions")
        definition = next(d for d in definitions if isinstance(d, dict) and d.get("symbol") == GUIDANCE_MARKET)
        summary = await relay.request("GET", f"/v2/perpMarket/{GUIDANCE_MARKET}/summary")
        min_qty = Decimal(str(definition["minOrderQty"]))
        mark = Decimal(str(summary["markPrice"]))
    except Exception:
        return None
    if not (min_qty.is_finite() and mark.is_finite()) or min_qty <= 0 or mark <= 0:
        return None
    min_notional = (min_qty * mark).quantize(Decimal("0.01"))
    # Room for two minimum orders plus fees, never below the floor, rounded up to a round number.
    suggested = max(SUGGESTED_FUNDING_FLOOR, ((min_notional * 2) / 10).to_integral_value(rounding="ROUND_CEILING") * 10)
    return {
        "market": GUIDANCE_MARKET,
        "min_order_qty": str(min_qty),
        "min_order_rusd": str(min_notional),
        "suggested_amount_rusd": str(suggested),
    }


def setup_snapshot_path(owner: str) -> Path:
    return state_directory() / f"agent-setup-{owner.lower()}.json"


def read_snapshot(owner: str) -> dict | None:
    path = setup_snapshot_path(owner)
    if not path.exists():
        return None
    with os.fdopen(_open_private(path, os.O_RDONLY), "r") as stream:
        data = json.load(stream)
    return data if isinstance(data, dict) else None


def write_snapshot(owner: str, data: dict | None) -> None:
    path = setup_snapshot_path(owner)
    if data is None:
        path.unlink(missing_ok=True)
        return
    fd = _open_private(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)


async def api_wallet_status(relay: AccountRelay) -> list[str]:
    data = await relay.request("GET", API_WALLET_STATUS_PATH.format(owner=relay.owner))
    wallets = data.get("enabledApiWallets") if isinstance(data, dict) else None
    if not isinstance(wallets, list) or any(not isinstance(item, str) or not is_address(item) for item in wallets):
        raise SetupError("Invalid API-wallet status response")
    return [item.lower() for item in wallets]


async def account_names(relay: AccountRelay) -> dict[int, str]:
    """Account id -> display name from the same V2 wallet read `relay.accounts()` validates."""
    data = await relay.request("GET", f"/v2/wallet/{relay.owner}/accounts")
    result = {}
    for row in data if isinstance(data, list) else []:
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            result[api_integer(row.get("accountId", 0))] = row["name"]
    return result


async def approved_signers(config) -> list[str]:
    """Every signer the owner approved, lower case: the agent key and, for hosted runs, the hosted signer.

    The same legacy read as the agent's approval (and as the app's setup page uses for both).
    """
    async with AccountRelay(config) as relay:
        return await api_wallet_status(relay)


async def hosted_signer() -> str | None:
    """The workflow service's active hosted signer for this deployment, which Enable hosted runs approves.

    None when the deployment has no workflow service, or it cannot be read in time, or it serves another
    deployment: then setup simply does not offer hosted runs. Never raises.
    """
    try:
        # Imported here: the guided setup works without the workflow client, which only this optional read needs.
        from agent_kit.scripts import _workflow as wf

        environment, chain = wf.kit_environment()
        async with wf.WorkflowClient(wf.service_url(), timeout_s=HOSTED_RUNS_INFO_TIMEOUT_S) as client:
            info = wf.parse_info(await client.info(), environment, chain, client.base_url)
        return info.active_signer
    except Exception:
        return None  # optional by contract: whatever went wrong, setup goes on without the offer


def _hosted_runs_choice_path(owner: str) -> Path:
    return state_directory() / f"agent-setup-hosted-runs-{owner.lower()}.json"


def read_hosted_runs_choice(owner: str) -> str | None:
    """What the user answered to the hosted-runs offer for this owner ("now" or "later"); None if never asked.

    Remembering the answer is a convenience: an unreadable file asks again rather than blocking setup.
    """
    try:
        path = _hosted_runs_choice_path(owner)
        if not path.exists():
            return None
        with os.fdopen(_open_private(path, os.O_RDONLY), "r") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    choice = data.get("choice") if isinstance(data, dict) else None
    return choice if choice in HOSTED_RUNS_CHOICES else None


def write_hosted_runs_choice(owner: str, choice: str) -> None:
    fd = _open_private(_hosted_runs_choice_path(owner), os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w") as stream:
        json.dump({"choice": choice, "at": int(time.time())}, stream)


async def owner_agent_accounts(config) -> list[int]:
    """The owner's Agent accounts (see agent_accounts), to tell which setup links show the same page."""
    async with AccountRelay(config) as relay:
        return agent_accounts(await relay.accounts(), await account_names(relay))


async def account_balances(relay: AccountRelay, account_id: int) -> dict[str, str]:
    data = await relay.request("GET", f"/v2/wallet/{relay.owner}/accountBalances")
    if not isinstance(data, list):
        raise SetupError("Invalid account balance response")
    result = {}
    for row in data:
        if not isinstance(row, dict) or api_integer(row.get("accountId", 0)) != account_id:
            continue
        asset = row.get("asset")
        balance = row.get("realBalance")
        if not isinstance(asset, str) or not isinstance(balance, (str, Decimal)):
            raise SetupError("Invalid account balance entry")
        result[asset] = str(Decimal(str(balance)))
    return result


def funded(balances: dict[str, str]) -> bool:
    return any(Decimal(value) > 0 for value in balances.values())


class Setup:
    """One evaluation of the setup state machine; `mutate` enables init's side effects."""

    def __init__(self, args, *, mutate: bool):
        self.args = args
        self.mutate = mutate
        self.actions: list[str] = []
        # Other links to the page the last evaluation's `url` shows (see page_link in evaluate).
        self.same_page_links: list[str] = []
        # The user's answer to the hosted-runs offer: this run's --hosted-runs, else a recorded "later".
        self.hosted_runs_choice: str | None = None
        self._hosted_runs_answered = False
        self._hosted_signer: tuple[str | None] | None = None  # read once per run: (address or None,)
        # The user chose hosted runs and the step will come (a service, and not approved yet): progress of 4.
        self.hosted_step_ahead = False

    def persist(self, values: dict[str, str]) -> None:
        if self.mutate:
            _env.write_env_values(values)

    def resolve_owner(self) -> str | None:
        configured = os.environ.get("PERP_WALLET_ADDRESS_1")
        supplied = getattr(self.args, "owner", None)
        if supplied:
            supplied = owner_address(supplied)
            if configured and configured.lower() != supplied.lower():
                raise SetupError(
                    f"PERP_WALLET_ADDRESS_1 is already set to a different owner ({owner_address(configured)}). To set "
                    "up this wallet instead, remove that line from the kit's .env and run agent init --owner again; "
                    "the agent key is kept"
                )
            if not configured:
                self.persist({"PERP_WALLET_ADDRESS_1": supplied})
                self.actions.append("owner_recorded")
            return supplied
        return owner_address(configured) if configured else None

    def ensure_agent_key(self) -> str:
        address = _env.agent_address()
        if address:
            return address
        if not self.mutate:
            return ""
        account = Account.create()
        key = account.key.hex()
        key = key if key.startswith("0x") else "0x" + key
        self.persist({_env.AGENT_KEY_VAR: key, _env.AGENT_ADDRESS_VAR: account.address})
        self.actions.append("agent_key_generated")
        del key
        return account.address

    def answer_hosted_runs(self, owner: str) -> None:
        """Take this run's --hosted-runs answer, recording "later" so the offer is not repeated; "now"
        lives only in this run (a user who chose it but never signed is offered it again next time)."""
        chosen = getattr(self.args, "hosted_runs", None)
        if chosen and self.mutate and not self._hosted_runs_answered:
            self._hosted_runs_answered = True
            # Remembering the answer is a convenience: a state-directory problem never blocks setup.
            try:
                if chosen == "later":
                    write_hosted_runs_choice(owner, "later")
                else:
                    _hosted_runs_choice_path(owner).unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            self.actions.append(f"hosted_runs_{chosen}")
        self.hosted_runs_choice = chosen or read_hosted_runs_choice(owner)

    async def hosted_runs_signer(self) -> str | None:
        if self._hosted_signer is None:
            self._hosted_signer = (await hosted_signer(),)
        return self._hosted_signer[0]

    def record_account(self, account_id: int) -> None:
        recorded = _env.agent_account_ids() | {account_id}
        values = {_env.AGENT_ACCOUNTS_VAR: ",".join(str(i) for i in sorted(recorded))}
        configured = os.environ.get("PERP_ACCOUNT_ID_1")
        if not configured or int(configured) in recorded:
            values["PERP_ACCOUNT_ID_1"] = str(account_id)
        self.persist(values)
        self.actions.append("agent_account_recorded")

    async def evaluate(self) -> dict:
        base: dict[str, Any] = {"actions": self.actions, "signer_mode": _env.signer_mode()}
        self.same_page_links = []
        owner = self.resolve_owner()
        if not owner:
            return {
                **base,
                "step": "needs_owner",
                "next": "Pass --owner 0x<your wallet address> (public information; no key is needed).",
            }
        self.answer_hosted_runs(owner)
        config = _env.get_trading_config(include_private_key=False)
        app = app_base_url(config.api_url)
        base.update(owner=owner, api_url=config.api_url, app_url=app)
        if app is None:
            base["app_url_note"] = "Set REYA_APP_URL to the app for this deployment to get clickable links."
        async with AccountRelay(config) as relay:
            accounts = await relay.accounts()
            if not accounts:
                return {
                    **base,
                    "step": "needs_app_onboarding",
                    "url": app,
                    "next": (
                        "This wallet has no Reya accounts yet. Connect it in the Reya app and accept the terms; "
                        "the app creates your main account. Then re-run agent init."
                    ),
                }
            main_id = next((i for i, kind in accounts.items() if kind == "MAINPERP"), None)
            agent = self.ensure_agent_key()
            base["agent_address"] = agent or None
            # The owner's approved signers: the agent key's approval here, the hosted signer's at the end.
            approvals = await api_wallet_status(relay) if agent else []
            approved = bool(agent) and agent.lower() in approvals
            if self.hosted_runs_choice == "now":
                hosted = await self.hosted_runs_signer()
                self.hosted_step_ahead = hosted is not None and hosted.lower() not in approvals
            base["approved"] = approved
            recorded = {i for i in _env.agent_account_ids() if i in accounts}
            unsupported = sorted(i for i in recorded if accounts[i] not in ACCOUNT_TYPES)
            if unsupported:
                # Never record another account in its place: the user's trading would move without a word.
                raise SetupError(
                    f"Account {unsupported[0]}, recorded as the Agent account, has account type "
                    f"{accounts[unsupported[0]]!r}, which this kit version does not support. Update the kit, then "
                    "run agent status again; nothing was changed"
                )
            # Names are read once approved (to find the agent account) or with one recorded (for the setup link).
            names = await account_names(relay) if approved or recorded else {}
            # The Agent account (AGENTPERP, or until the API reports that type, the sub-account named exactly
            # "Agent account") is the agent's, whenever and however it was created (this setup, an earlier
            # one, a reinstall): it wins over any other new account.
            named = agent_accounts(accounts, names)
            marked = any(kind == AGENT_PERP for kind in accounts.values())
            your_agent_account = "your Agent account" if marked else f'your "{AGENT_ACCOUNT_NAME}"'
            amount = getattr(self.args, "amount", None)
            amount_param = f"&amount={amount}" if amount is not None else ""

            def page_link(account_id: int | None = None) -> str | None:
                if not agent:
                    return None
                if account_id is not None:
                    self.same_page_links = same_setup_page(app, agent, owner, account_id, named, amount)
                return setup_link(app, agent, owner, account_id, amount)

            if not approved:
                return {
                    **base,
                    "step": "needs_approval",
                    # A reinstall or a revoked approval keeps the recorded account: the page uses it throughout.
                    "url": page_link(min(recorded) if recorded else None),
                    "step_url": deep_link(app, "approve", agent=agent) if agent else None,
                    "permission": PERMISSION_COPY,
                    "next": (
                        "Open the setup page: one page in the Reya app takes the user through all three steps. "
                        "Approve the agent (check the address matches agent_address; one signature), create the "
                        "Agent account (one click) and fund it (one signature). Run agent init --until-ready to "
                        "wait while they do it."
                        if agent
                        else "Run agent init to generate the agent key."
                    ),
                }
            note = ""
            if not recorded:
                chosen = getattr(self.args, "account_id", None)
                snapshot = read_snapshot(owner)
                if chosen is not None:
                    if accounts.get(chosen) not in SUB_PERP_TYPES:
                        raise SetupError("--account-id must be one of this owner's sub-accounts (never Main or Spot)")
                    new_ids = [chosen]
                elif named:
                    new_ids = named
                elif snapshot:
                    before = {api_integer(i) for i in snapshot.get("before_accounts", [])}
                    new_ids = [i for i, kind in accounts.items() if i not in before and kind in SUB_PERP_TYPES]
                else:
                    new_ids = []
                by_name = chosen is None and bool(named)
                if len(new_ids) == 1 and self.mutate:
                    self.record_account(new_ids[0])
                    write_snapshot(owner, None)
                    recorded = {new_ids[0]}
                    if by_name:
                        note = f"Using {your_agent_account} (account {new_ids[0]}). "
                elif len(new_ids) == 1:
                    # status is read-only: report the account init would record, without a create link.
                    which = your_agent_account if by_name else "the new sub-account"
                    return {
                        **base,
                        "step": "needs_agent_account",
                        "found_account_id": new_ids[0],
                        "next": f"Found {which} (account {new_ids[0]}). Run agent init to use it as the agent account.",
                    }
                elif len(new_ids) > 1:
                    ids = ", ".join(str(i) for i in sorted(new_ids))
                    these = (
                        f"{len(new_ids)} Agent accounts"
                        if marked
                        else f'{len(new_ids)} accounts named "{AGENT_ACCOUNT_NAME}"'
                    )
                    return {
                        **base,
                        "step": "ambiguous_new_accounts",
                        "candidates": sorted(new_ids),
                        "next": (
                            f"This wallet has {these} ({ids}). Ask the "
                            "user which one the agent should trade, then re-run agent init --account-id <id>. The "
                            "kit only ever trades the account recorded, so the others are left alone."
                            if by_name
                            else "Several new sub-accounts appeared; re-run agent init --account-id <id> to choose."
                        ),
                    }
                else:
                    if self.mutate and not snapshot:
                        write_snapshot(
                            owner, {"before_accounts": sorted(accounts), "created_at_ms": int(time.time() * 1000)}
                        )
                    return {
                        **base,
                        "step": "needs_agent_account",
                        "url": page_link(),
                        "step_url": deep_link(app, "create_account", name=quote(AGENT_ACCOUNT_NAME)),
                        "next": (
                            "The agent is approved. On the setup page, create the Agent account (one click, no "
                            "signature), then fund it (one signature). Run agent init --until-ready to wait; it "
                            "records the new account automatically."
                        ),
                    }
            account_id = min(recorded)
            balances = await account_balances(relay, account_id)
            base.update(agent_account_id=account_id, agent_account_ids=sorted(recorded), balances=balances)
            warning = agent_account_warning(accounts, account_id)
            if warning:
                base["agent_account_warning"] = warning
                note = warning + " " + note
            if not funded(balances):
                guidance = await funding_guidance(relay)
                how_much = (
                    f"The smallest {guidance['market'].removesuffix('RUSDPERP')} order is {guidance['min_order_qty']} "
                    f"(about {guidance['min_order_rusd']} {ASSET_LABEL}), so fund at least "
                    f"{guidance['suggested_amount_rusd']} {ASSET_LABEL} to place a few small orders. "
                    if guidance
                    else ""
                )
                return {
                    **base,
                    "step": "needs_funding",
                    "url": page_link(account_id),
                    "step_url": deep_link(
                        app, "transfer", source=main_id or "", target=account_id, amount=amount_param
                    ),
                    "from_account": main_id,
                    **({"funding_guidance": guidance} if guidance else {}),
                    "next": (
                        f"{note}Last step, on the setup page: move {ASSET_LABEL} from your main account into "
                        f"the agent account (one signature). {how_much}The page suggests an amount and the user enters "
                        "it there. Run agent init --until-ready to wait."
                    ),
                }
            done = f"{note}Setup complete. Trade commands sign with the agent key. "
            try_these = "Offer the user the try_asking examples."
            ready = {**base, "step": "ready", "try_asking": TRY_ASKING}
            hosted = await self.hosted_runs_signer()
            if hosted is None:
                # No workflow service here, or it could not be read: nothing to offer, and setup is done.
                return {**ready, "hosted_runs": {"status": "unavailable"}, "next": done + try_these}
            if hosted.lower() in approvals:
                return {
                    **ready,
                    "hosted_runs": {"status": "enabled", "hosted_signer": hosted},
                    "next": done
                    + "Hosted runs are on: Reya can run the user's workflows on the Agent account. "
                    + try_these,
                }
            permission = HOSTED_RUNS_PERMISSION.format(signer=hosted)
            if self.hosted_runs_choice == "now":
                # The tab left open at the funding step shows this step now: show_setup_link does not reopen it.
                url = page_link(account_id)
                where = (
                    "on the same setup page: its Enable hosted runs step"
                    if url
                    else "in the Reya app's setup page (set REYA_APP_URL for a link)"
                )
                return {
                    **base,
                    "step": HOSTED_RUNS_STEP,
                    "url": url,
                    "hosted_signer": hosted,
                    "permission": permission,
                    "next": (
                        f"{note}The kit can already trade. Optional last step, {where}: one signature from "
                        f"{owner} approves Reya's hosted runner; the page shows its address, which matches "
                        "hosted_signer. Relay the permission text. Run agent init --hosted-runs now --until-ready "
                        "--timeout 100 to wait (again after a time-out); if the user changes their mind, run agent "
                        "init --hosted-runs later."
                    ),
                }
            if self.hosted_runs_choice == "later":
                return {
                    **ready,
                    "hosted_runs": {"status": "not_enabled", "choice": "later", "hosted_signer": hosted},
                    "next": done + HOSTED_RUNS_LATER + " " + try_these,
                }
            return {
                **ready,
                "hosted_runs": {
                    "status": "not_enabled",
                    "hosted_signer": hosted,
                    "offer": {
                        "question": HOSTED_RUNS_QUESTION,
                        "permission": permission,
                        "now_command": "agent init --hosted-runs now --until-ready --timeout 100",
                        "later_command": "agent init --hosted-runs later",
                    },
                },
                "next": (
                    done + "Then ask hosted_runs.offer.question once, with its permission text. On now, run "
                    "agent init --hosted-runs now --until-ready --timeout 100: the setup page moves on to its Enable "
                    "hosted runs "
                    "step (a tab already open is not opened again) and the command waits for the one signature. "
                    "On later, run agent init --hosted-runs later, which records the answer so it is not asked "
                    "again. " + try_these
                ),
            }


async def run(args, *, mutate: bool) -> dict:
    _env.load_env()
    if mutate and _env.signer_mode() == "own-key":
        raise SetupError("REYA_SIGNER=own-key is set; agent init manages the agent key only. Unset it to continue")
    setup = Setup(args, mutate=mutate)
    until_ready = getattr(args, "until_ready", False)
    waiting = until_ready or getattr(args, "wait", False)
    deadline = time.monotonic() + args.timeout if waiting else None
    first_step = None
    opened: str | None = None
    open_outcome: dict = {}  # what happened to the link this run handled; reported until the link changes
    while True:
        result = await setup.evaluate()
        first_step = first_step or result["step"]
        if result["step"] == "needs_owner" or (
            "owner_recorded" in setup.actions and result["step"] not in ("ready", HOSTED_RUNS_STEP)
        ):
            # A fresh setup: offer practice with virtual money before any real-funds step, and open
            # nothing until the user has chosen real trading (the next init run opens the page).
            result["practice_first"] = PRACTICE_FIRST
            if result.get("url"):
                result["opened"] = False
                result["next"] = (
                    "First ask the user practice_first.question. For real trading, run agent init --until-ready: "
                    "it opens the page and waits. " + result["next"]
                )
        if result["step"] in SETUP_PAGE_STEPS:
            steps = len(SETUP_PAGE_STEPS) if setup.hosted_step_ahead else len(SETUP_PAGE_STEPS) - 1
            result["progress"] = {"step": SETUP_PAGE_STEPS[result["step"]], "of": steps}
        url = result.get("url")
        same_page = [url, *setup.same_page_links] if url else []
        if url and getattr(args, "open", True) and opened not in same_page and "practice_first" not in result:
            # Open the user's default browser on the app page for this step; the
            # user still connects, clicks and signs there. Never auto-submits anything.
            # A link already opened by an earlier run is not reopened (one tab per step), nor is
            # one that shows the same page as a link already opened (the setup page once it names
            # the account the page found by name anyway).
            open_outcome = show_setup_link(url, setup.same_page_links)
            opened = url
            if "browser_unavailable" in open_outcome and waiting and not sys.stdout.isatty():
                # Nobody has seen the link, and an assistant reads this output only when the
                # command returns: return now instead of waiting for the user to act on a page
                # they cannot see. The link counts as given, so the next --until-ready waits.
                try:
                    remember_opened(url)
                except (OSError, ValueError):
                    pass
                result.update(open_outcome, waited=False)
                why = BROWSER_UNAVAILABLE[open_outcome["browser_unavailable"]]
                # The hosted step's own `next` already says which command waits (it carries --hosted-runs now).
                then = result["next"] if result["step"] == HOSTED_RUNS_STEP else WAIT_AFTER_SHOWING
                result["next"] = OPEN_ELSEWHERE.format(why=why) + then
                return _with_kit_status(result)
        if url and opened in same_page:
            result.update(open_outcome)
            if "browser_unavailable" in open_outcome:
                why = BROWSER_UNAVAILABLE[open_outcome["browser_unavailable"]]
                result["next"] = OPEN_ELSEWHERE.format(why=why) + result["next"]
            elif open_outcome.get("link_already_opened") and result["step"] == "needs_funding" and "&amount=" in url:
                # The tab already open came from a link with another amount, or none: it is not pre-filled.
                result["next"] += " The setup page already open may not show this amount: the user enters it there."
        terminal = result["step"] in ("ready", "needs_owner", "ambiguous_new_accounts")
        # --wait means "wait for the current step to complete": return as soon as the
        # step changes (or the run is terminal), so the caller can show the next instruction.
        # --until-ready keeps waiting through the setup page's steps and returns only at a terminal
        # step or the timeout, so the user is not asked to report back after every click.
        advanced = result["step"] != first_step and not until_ready
        if deadline is None or terminal or advanced or time.monotonic() >= deadline:
            if deadline is not None:
                result["waited"] = True
                result["timed_out"] = not (terminal or advanced)
                if result["timed_out"] and result["step"] == "needs_approval" and result.get("owner"):
                    result["next"] += " " + WRONG_WALLET_HINT.format(owner=result["owner"])
            return _with_kit_status(result)
        await asyncio.sleep(args.interval)


def _with_kit_status(result: dict) -> dict:
    try:
        result["kit"] = _release.update_status(_env.KIT_ROOT)
    except Exception:
        result["kit"] = {"installed": _release.installed_version(), "latest_release": None}
    return result


def build_parser():
    parser = JsonArgumentParser(prog="agent", description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init", help="Create/resume the guided setup; idempotent")
    init.add_argument("--owner", help="Owner wallet address (public); recorded in .env on first run")
    init.add_argument(
        "--amount", type=positive_decimal_arg, help="amount of USDC (rUSD) to pre-fill in the funding link"
    )
    init.add_argument("--account-id", type=int, help="Record an existing sub-account as the agent account")
    init.add_argument(
        "--hosted-runs",
        choices=HOSTED_RUNS_CHOICES,
        help="answer the optional Enable hosted runs step: now (wait through it on the setup page) or later "
        "(recorded; not offered again)",
    )
    init.add_argument(
        "--until-ready",
        action="store_true",
        help="Poll until setup is ready or needs a decision, through every step on the setup page",
    )
    status = actions.add_parser("status", help="Report setup state without changing anything")
    for command in (init, status):
        command.add_argument(
            "--no-open", dest="open", action="store_false", help="Do not open the step's app link in the browser"
        )
        command.add_argument(
            "--wait", action="store_true", help="Poll until the current step completes (the step changes), then return"
        )
        command.add_argument("--timeout", type=float, default=600.0, help="Seconds to wait with --wait/--until-ready")
        command.add_argument("--interval", type=float, default=5.0, help="Polling interval with --wait")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        output_result(asyncio.run(run(args, mutate=args.action == "init")))
    except (SetupError, ValueError) as exc:
        output_error(str(exc))
    except Exception as exc:
        output_error(f"{type(exc).__name__}: agent setup read failed; nothing was submitted")


if __name__ == "__main__":
    main()
