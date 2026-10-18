"""Guided agent setup: an app-approved agent key, a dedicated agentic account, funding.

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
import time
import webbrowser
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit

from eth_account import Account
from eth_utils import is_address, to_checksum_address

from agent_kit.scripts import _env, _release
from agent_kit.scripts._account_relay import AccountRelay, SetupError, api_integer
from agent_kit.scripts._common import JsonArgumentParser, output_error, output_result, positive_decimal_arg
from agent_kit.scripts._writer import _open_private, state_directory

# Deep links the kit prints for the owner to complete in the app. The API page is
# the Next.js route `/api-page` (header: More -> API). The query parameters are
# provisional until the app handles them; keep them in one place.
APP_URLS = {
    "https://api.reya.xyz/v2": "https://app.reya.xyz",
    "https://api-devnet.reya-cronos.network/v2": "https://app-devnet-cf.reya.xyz",
}
DEEP_LINKS = {
    # One page walks the owner through all three steps (approve, create the Agent account, fund).
    # `owner` lets the page refuse to run the steps on any other connected wallet and ask for a switch.
    "setup": "/agents/setup?agent={agent}&owner={owner}{amount}#access={access}",
    # The app's regular page for each step, as a fallback if the setup page does not load.
    "approve": "/api-page?approve={agent}",
    "create_account": "/portfolio?createAccount=1&name={name}&source=agent",
    "transfer": "/portfolio?transfer=1&from={source}&to={target}{amount}",
}
# The setup page is unlisted: it only opens with this code in the URL fragment, which the browser
# never sends to the server. It keeps casual visitors out while the kit is private; it is not a
# secret and not a security control. Rotate it together with the app's AGENT_SETUP_ACCESS_SHA256.
SETUP_PAGE_ACCESS_CODE = "V1MZCBVGEhmzidMyjCdW0hqi"
# Steps the setup page covers, for progress reporting while the kit waits.
SETUP_PAGE_STEPS = {"needs_approval": 1, "needs_agent_account": 2, "needs_funding": 3}
AGENT_ACCOUNT_NAME = "Agent account"
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
PERMISSION_COPY = (
    "Approving lets this agent place and cancel orders on your Reya accounts. "
    "It cannot withdraw, transfer or change permissions. Revoke it on the same page at any time."
)


def owner_address(value: str) -> str:
    if not isinstance(value, str) or not is_address(value) or int(value, 16) == 0:
        raise SetupError("A nonzero EVM owner address is required (--owner 0x...)")
    return to_checksum_address(value)


def app_base_url(api_url: str) -> str | None:
    url = os.environ.get("REYA_APP_URL")
    if url:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise SetupError("REYA_APP_URL must be a plain https URL")
        return url.rstrip("/")
    return APP_URLS.get(api_url)


def deep_link(base: str | None, kind: str, **params) -> str | None:
    path = DEEP_LINKS[kind].format(**params)
    return base + path if base else None


def open_in_browser(url: str) -> bool:
    """Best effort: open the deep link in the default browser; False when there is none (headless)."""
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


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
        owner = self.resolve_owner()
        if not owner:
            return {
                **base,
                "step": "needs_owner",
                "next": "Pass --owner 0x<your wallet address> (public information; no key is needed).",
            }
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
            approved = bool(agent) and agent.lower() in await api_wallet_status(relay)
            base["approved"] = approved
            amount = getattr(self.args, "amount", None)
            amount_param = f"&amount={amount}" if amount is not None else ""
            setup_url = (
                deep_link(app, "setup", agent=agent, owner=owner, amount=amount_param, access=SETUP_PAGE_ACCESS_CODE)
                if agent
                else None
            )
            if not approved:
                return {
                    **base,
                    "step": "needs_approval",
                    "url": setup_url,
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
            recorded = {i for i in _env.agent_account_ids() if i in accounts}
            by_name_note = ""
            if not recorded:
                chosen = getattr(self.args, "account_id", None)
                snapshot = read_snapshot(owner)
                names = await account_names(relay)
                # The sub-account named exactly "Agent account" is the agent's, whenever and however it
                # was created (this setup, an earlier one, a reinstall): it wins over any other new account.
                named = [i for i, kind in accounts.items() if kind == "SUBPERP" and names.get(i) == AGENT_ACCOUNT_NAME]
                if chosen is not None:
                    if accounts.get(chosen) != "SUBPERP":
                        raise SetupError("--account-id must be one of this owner's sub-accounts (never Main or Spot)")
                    new_ids = [chosen]
                elif named:
                    new_ids = named
                elif snapshot:
                    before = {api_integer(i) for i in snapshot.get("before_accounts", [])}
                    new_ids = [i for i, kind in accounts.items() if i not in before and kind == "SUBPERP"]
                else:
                    new_ids = []
                by_name = chosen is None and bool(named)
                if len(new_ids) == 1 and self.mutate:
                    self.record_account(new_ids[0])
                    write_snapshot(owner, None)
                    recorded = {new_ids[0]}
                    if by_name:
                        by_name_note = f'Using your "{AGENT_ACCOUNT_NAME}" (account {new_ids[0]}). '
                elif len(new_ids) == 1:
                    # status is read-only: report the account init would record, without a create link.
                    which = f'your "{AGENT_ACCOUNT_NAME}"' if by_name else "the new sub-account"
                    return {
                        **base,
                        "step": "needs_agent_account",
                        "found_account_id": new_ids[0],
                        "next": f"Found {which} (account {new_ids[0]}). Run agent init to use it as the agent account.",
                    }
                elif len(new_ids) > 1:
                    ids = ", ".join(str(i) for i in sorted(new_ids))
                    return {
                        **base,
                        "step": "ambiguous_new_accounts",
                        "candidates": sorted(new_ids),
                        "next": (
                            f'This wallet has {len(new_ids)} accounts named "{AGENT_ACCOUNT_NAME}" ({ids}). Ask the '
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
                        "url": setup_url,
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
                    "url": setup_url,
                    "step_url": deep_link(
                        app, "transfer", source=main_id or "", target=account_id, amount=amount_param
                    ),
                    "from_account": main_id,
                    **({"funding_guidance": guidance} if guidance else {}),
                    "next": (
                        f"{by_name_note}Last step, on the setup page: move {ASSET_LABEL} from your main account into "
                        f"the agent account (one signature). {how_much}The page suggests an amount and the user enters "
                        "it there. Run agent init --until-ready to wait."
                    ),
                }
            return {
                **base,
                "step": "ready",
                "next": (
                    f"{by_name_note}Setup complete. Trade commands sign with the agent key. "
                    "Offer the user the try_asking examples."
                ),
                "try_asking": TRY_ASKING,
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
    while True:
        result = await setup.evaluate()
        first_step = first_step or result["step"]
        if result["step"] == "needs_owner" or ("owner_recorded" in setup.actions and result["step"] != "ready"):
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
            result["progress"] = {"step": SETUP_PAGE_STEPS[result["step"]], "of": len(SETUP_PAGE_STEPS)}
        url = result.get("url")
        if url and getattr(args, "open", True) and url != opened and "practice_first" not in result:
            # Open the user's default browser on the app page for this step; the
            # user still connects, clicks and signs there. Never auto-submits anything.
            # A link already opened by an earlier run is not reopened (one tab per step).
            # Remembering links is a convenience: a state-directory problem must never block setup.
            try:
                already = link_recently_opened(url)
            except (OSError, ValueError):
                already = False
            if already:
                result["opened"] = False
                result["link_already_opened"] = True
            else:
                result["opened"] = open_in_browser(url)
                if result["opened"]:
                    try:
                        remember_opened(url)
                    except (OSError, ValueError):
                        pass
            opened = url
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
            try:
                result["kit"] = _release.update_status(_env.KIT_ROOT)
            except Exception:
                result["kit"] = {"installed": _release.installed_version(), "latest_release": None}
            return result
        await asyncio.sleep(args.interval)


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
