"""Guided agent setup: signer selection, the idempotent state machine and secret hygiene.

Offline: the account relay is replaced by a fake; no network, no real .env. The
contract under test is that the kit never needs the owner's key, that `init`
resumes at the first incomplete step on every run, that the agent key only
reaches `.env` (0600) and never any output, and that the agent key is refused
for accounts `init` did not record.
"""

import hashlib
import json
import os
import stat
import subprocess

import pytest
from eth_account import Account

from agent_kit.scripts import _env, agent, doctor
from agent_kit.scripts._account_relay import AccountRelay

# The real checks, before the fixture below replaces them for the other tests.
BROWSER_UNAVAILABLE = agent.browser_unavailable
IN_WSL = agent.in_wsl
TEST_KEY = "0x" + "11" * 32
OWNER = Account.from_key("0x" + "22" * 32).address
API = "https://api.reya.xyz/v2"
SETUP_VARS = (
    "PERP_WALLET_ADDRESS_1",
    "PERP_ACCOUNT_ID_1",
    _env.AGENT_KEY_VAR,
    _env.AGENT_ADDRESS_VAR,
    _env.AGENT_ACCOUNTS_VAR,
)


class FakeRelay:
    """Owner reads only: accounts, approval status and balances come from a shared scenario."""

    scenario: dict = {}

    def __init__(self, config):
        self.config = config
        self.owner = config.owner_wallet_address

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def accounts(self):
        return dict(self.scenario["accounts"])

    async def request(self, method, path, **kwargs):
        assert method == "GET"
        if path.endswith("/apiWalletStatus"):
            return {"enabledApiWallets": list(self.scenario["approved"])}
        if path.endswith("/accountBalances"):
            return [dict(row) for row in self.scenario["balances"]]
        if path == "/v2/perpMarketDefinitions" and "market" in self.scenario:
            return [{"symbol": "ETHRUSDPERP", "minOrderQty": self.scenario["market"]["min_qty"]}]
        if path == "/v2/perpMarket/ETHRUSDPERP/summary" and "market" in self.scenario:
            return {"symbol": "ETHRUSDPERP", "markPrice": self.scenario["market"]["mark"]}
        if path.endswith("/accounts"):
            names = self.scenario.get("names", {})
            return [
                {"accountId": i, "type": kind, "name": names.get(i, f"Account {i}")}
                for i, kind in self.scenario["accounts"].items()
            ]
        raise AssertionError(f"unexpected read {path}")


async def fake_hosted_signer():
    FakeRelay.scenario["hosted_reads"] = FakeRelay.scenario.get("hosted_reads", 0) + 1
    return FakeRelay.scenario.get("hosted_signer")


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    """Isolated kit root, state dir and deployment; agent-written variables are removed afterwards."""
    kit_root = tmp_path / "kit"
    kit_root.mkdir()
    state = tmp_path / "state"
    monkeypatch.setattr(_env, "KIT_ROOT", kit_root)
    monkeypatch.setenv("REYA_STATE_DIR", str(state))
    monkeypatch.setenv("REYA_API_URL", API)
    monkeypatch.setenv("CHAIN_ID", "1729")
    for name in (
        *SETUP_VARS,
        "REYA_SIGNER",
        "REYA_MODE",
        "PERP_PRIVATE_KEY_1",
        "PERP_PRIVATE_KEY_FILE",
        "REYA_APP_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    FakeRelay.scenario = {"accounts": {}, "approved": [], "balances": []}
    monkeypatch.setattr(agent, "AccountRelay", FakeRelay)
    # The workflow service's hosted signer (scenario "hosted_signer"); none unless a test offers hosted runs.
    monkeypatch.setattr(agent, "hosted_signer", fake_hosted_signer)
    yield kit_root
    for name in SETUP_VARS:  # write_env_values exports into os.environ outside monkeypatch
        os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def a_desktop_with_a_browser(monkeypatch):
    """Tests run on a desktop unless they say otherwise (CI has no display; a developer may be on WSL)."""
    monkeypatch.setattr(agent, "browser_unavailable", lambda: None)
    monkeypatch.setattr(agent, "in_wsl", lambda: False)


def record_opens(monkeypatch, result: bool = True) -> list[str]:
    opened: list[str] = []

    def fake_open(url: str, new: int = 0) -> bool:
        opened.append(url)
        return result

    monkeypatch.setattr(agent.webbrowser, "open", fake_open)
    return opened


def run_by(monkeypatch, *, a_terminal: bool) -> None:
    """Who reads the output: a person at a terminal, or an assistant (a pipe, read when the command returns)."""
    monkeypatch.setattr(agent.sys.stdout, "isatty", lambda: a_terminal)


async def init(*argv):
    args = agent.build_parser().parse_args(["init", "--no-open", *argv])
    return await agent.run(args, mutate=True)


async def status(*argv):
    args = agent.build_parser().parse_args(["status", "--no-open", *argv])
    return await agent.run(args, mutate=False)


def env_file(kit_root) -> str:
    return (kit_root / ".env").read_text()


async def test_init_without_owner_asks_for_it_and_writes_nothing(setup_env):
    result = await init()
    assert result["step"] == "needs_owner"
    assert not (setup_env / ".env").exists()


async def test_init_walks_every_step_and_resumes_where_it_left_off(setup_env):
    kit_root = setup_env
    # A wallet that never onboarded: the kit must not create its first account.
    result = await init("--owner", OWNER)
    assert result["step"] == "needs_app_onboarding"
    assert result["url"] == "https://app.reya.xyz"
    assert "PERP_WALLET_ADDRESS_1=" + OWNER in env_file(kit_root)
    assert _env.AGENT_KEY_VAR not in env_file(kit_root)

    # Main account exists: generate the agent key, ask for approval.
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    result = await init()
    assert result["step"] == "needs_approval"
    agent_address = result["agent_address"]
    setup_url = (
        f"https://app.reya.xyz/agents/setup?agent={agent_address}&owner={OWNER}#access={agent.SETUP_PAGE_ACCESS_CODE}"
    )
    assert agent_address and result["url"] == setup_url
    assert result["step_url"] == f"https://app.reya.xyz/api-page?approve={agent_address}"
    assert result["progress"] == {"step": 1, "of": 3}
    assert "withdraw" in result["permission"]
    contents = env_file(kit_root)
    assert f"{_env.AGENT_ADDRESS_VAR}={agent_address}" in contents
    key_line = next(line for line in contents.splitlines() if line.startswith(_env.AGENT_KEY_VAR + "="))
    key = key_line.split("=", 1)[1]
    assert Account.from_key(key).address == agent_address
    assert key not in json.dumps(result)
    assert stat.S_IMODE((kit_root / ".env").stat().st_mode) == 0o600

    # Re-running before approval reuses the same key instead of generating another.
    again = await init()
    assert again["step"] == "needs_approval" and again["agent_address"] == agent_address
    assert env_file(kit_root).count(_env.AGENT_KEY_VAR + "=") == 1

    # Approved: ask for the Agent account.
    FakeRelay.scenario["approved"] = [agent_address.lower()]
    result = await init()
    assert result["step"] == "needs_agent_account"
    assert result["url"] == setup_url and result["progress"] == {"step": 2, "of": 3}
    assert result["step_url"] == "https://app.reya.xyz/portfolio?createAccount=1&name=Agent%20account&source=agent"

    # The setup page created the Agent account (AGENTPERP): record it, point PERP_ACCOUNT_ID_1 at it, ask for
    # funding. The setup link now names it.
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 2: "AGENTPERP"}
    result = await init("--amount", "250")
    assert result["step"] == "needs_funding"
    assert result["agent_account_id"] == 2 and result["from_account"] == 1
    assert result["url"] == setup_url.replace("#access", "&account=2&amount=250#access")
    assert result["step_url"] == "https://app.reya.xyz/portfolio?transfer=1&from=1&to=2&amount=250"
    assert result["progress"] == {"step": 3, "of": 3}
    assert f"{_env.AGENT_ACCOUNTS_VAR}=2" in env_file(kit_root)
    assert "PERP_ACCOUNT_ID_1=2" in env_file(kit_root)

    # Funded: done. Status agrees and changes nothing.
    FakeRelay.scenario["balances"] = [{"accountId": 2, "asset": "RUSD", "realBalance": "250"}]
    result = await init()
    assert result["step"] == "ready" and result["balances"] == {"RUSD": "250"}
    assert result["try_asking"] == agent.TRY_ASKING and "try_asking" in result["next"]
    assert result["kit"]["latest_release"] is None and result["kit"]["update_available"] is False
    before = env_file(kit_root)
    assert (await status())["step"] == "ready"
    assert env_file(kit_root) == before
    assert key not in json.dumps(await status())


async def test_status_never_generates_a_key_or_touches_env(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    result = await status()
    assert result["step"] == "needs_approval"
    assert result["agent_address"] is None and result["url"] is None
    assert not (setup_env / ".env").exists()


async def test_a_new_main_spot_or_untagged_sub_account_is_never_taken_as_the_agent_account(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario = {"accounts": {1: "MAINPERP"}, "approved": [address.lower()], "balances": []}
    assert (await init())["step"] == "needs_agent_account"
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 5: "SPOT"}
    assert (await init())["step"] == "needs_agent_account"
    # Sub-accounts created after init asked for the Agent account are not it either: only the type says so.
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 5: "SPOT", 7: "SUBPERP", 8: "SUBPERP"}
    FakeRelay.scenario["names"] = {7: "Agent1", 8: "Agent2"}
    result = await init()
    assert result["step"] == "needs_agent_account" and "createAccount=1" in result["step_url"]
    with pytest.raises(agent.SetupError):
        await init("--account-id", "1")
    assert (await init("--account-id", "8"))["agent_account_id"] == 8


async def test_a_sub_account_named_agent_account_is_not_the_agent_account(setup_env, monkeypatch):
    # Earlier kits took a SUBPERP named exactly "Agent account" when the wallet had no AGENTPERP account.
    # Now that wallet has no Agent account: setup asks for one on the setup page and leaves the others alone.
    names = {4: "Agent account", 5: "Agent account", 6: "Agent account 2"}
    approved_owner(monkeypatch, {1: "MAINPERP", 4: "SUBPERP", 5: "SUBPERP", 6: "SUBPERP"}, names)
    for evaluate in (status, init):
        result = await evaluate()
        assert result["step"] == "needs_agent_account" and "found_account_id" not in result
        assert result["step_url"] == "https://app.reya.xyz/portfolio?createAccount=1&name=Agent%20account&source=agent"
        assert "the kit never takes another sub-account for it, whatever its name" in result["next"]
    env = setup_env / ".env"
    assert f"{_env.AGENT_ACCOUNTS_VAR}=" not in (env.read_text() if env.exists() else "")
    # The setup page creates the Agent account: init records it, whatever the others are called.
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 4: "SUBPERP", 5: "SUBPERP", 6: "SUBPERP", 9: "AGENTPERP"}
    result = await init()
    assert result["step"] == "needs_funding" and result["agent_account_id"] == 9
    assert result["next"].startswith("Using your Agent account (account 9). Last step")
    assert "agent_account_warning" not in result and f"{_env.AGENT_ACCOUNTS_VAR}=9" in env_file(setup_env)


async def test_status_reports_an_account_created_before_init_ran_and_init_records_it(setup_env):
    # The README's flow: install runs init once (at approval), then the assistant polls status.
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    result = await init("--owner", OWNER)
    assert result["step"] == "needs_approval"
    FakeRelay.scenario["approved"] = [result["agent_address"].lower()]
    result = await status()
    assert result["step"] == "needs_agent_account" and "createAccount=1" in result["step_url"]
    # The account is created while only status polls.
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 2: "AGENTPERP"}
    result = await status()
    assert result["step"] == "needs_agent_account" and result["found_account_id"] == 2
    assert "url" not in result and result["next"].startswith("Found your Agent account (account 2). Run agent init")
    result = await init()
    assert result["step"] == "needs_funding" and result["agent_account_id"] == 2


def approved_owner(monkeypatch, accounts: dict, names: dict) -> None:
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario = {"accounts": accounts, "approved": [address.lower()], "balances": [], "names": names}


# AGENTPERP (API spec 3.7.3): the API marks the Agent account by type, and the kit finds it by type alone.


async def test_the_agentperp_account_is_the_agent_account_whatever_its_name(setup_env, monkeypatch):
    # The renamed tagged account is still the Agent account; an untagged one named "Agent account" is not.
    approved_owner(monkeypatch, {1: "MAINPERP", 4: "SUBPERP", 5: "AGENTPERP"}, {4: "Agent account", 5: "Bot"})
    result = await status()
    assert result["step"] == "needs_agent_account" and result["found_account_id"] == 5
    assert result["next"].startswith("Found your Agent account (account 5). Run agent init")
    result = await init()
    assert result["step"] == "needs_funding" and result["agent_account_id"] == 5
    assert result["next"].startswith("Using your Agent account (account 5). Last step")
    assert "createAccount" not in result["step_url"] and "agent_account_warning" not in result
    assert f"{_env.AGENT_ACCOUNTS_VAR}=5" in env_file(setup_env) and "PERP_ACCOUNT_ID_1=5" in env_file(setup_env)
    # Already funded: straight to ready, still the same account.
    FakeRelay.scenario["balances"] = [{"accountId": 5, "asset": "RUSD", "realBalance": "80"}]
    result = await init()
    assert result["step"] == "ready" and result["agent_account_id"] == 5 and "agent_account_warning" not in result


async def test_several_agentperp_accounts_ask_which_one_and_create_nothing(setup_env, monkeypatch):
    approved_owner(monkeypatch, {1: "MAINPERP", 4: "AGENTPERP", 5: "AGENTPERP"}, {4: "Agent account", 5: "Bot"})
    result = await init()
    assert result["step"] == "ambiguous_new_accounts" and result["candidates"] == [4, 5]
    assert result["next"].startswith("This wallet has 2 Agent accounts (4, 5). Ask the user")
    assert "the others are left alone" in result["next"] and "url" not in result
    assert (await init("--account-id", "5"))["agent_account_id"] == 5


async def test_a_recorded_account_that_is_not_agentperp_is_warned_about_with_the_fix(setup_env, monkeypatch):
    # An earlier kit recorded an untagged sub-account (by its name): it stays recorded and still trades.
    approved_owner(monkeypatch, {1: "MAINPERP", 4: "SUBPERP", 5: "SUBPERP"}, {4: "Agent account"})
    monkeypatch.setenv(_env.AGENT_ACCOUNTS_VAR, "4")
    monkeypatch.setenv("PERP_ACCOUNT_ID_1", "4")
    FakeRelay.scenario["balances"] = [{"accountId": 4, "asset": "RUSD", "realBalance": "80"}]
    # The wallet has no Agent account: ready, with a warning that says how to get one.
    result = await status()
    assert result["step"] == "ready" and result["agent_account_id"] == 4
    warning = result["agent_account_warning"]
    assert warning.startswith(
        "Account 4 is recorded as the Agent account, but Reya does not mark it as an Agent account, and this wallet "
        "has none."
    )
    assert "hosted strategies refuse it" in warning and "the setup page creates one" in warning
    assert "remove the PERP_AGENT_ACCOUNT_IDS and PERP_ACCOUNT_ID_1 lines" in warning
    assert result["next"].startswith(warning + " Setup complete.")
    # The canary's case: the app's tagged account is another one. Still ready, with the account to switch to.
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 4: "SUBPERP", 5: "AGENTPERP"}
    result = await status()
    assert result["step"] == "ready" and result["agent_account_id"] == 4
    warning = result["agent_account_warning"]
    assert warning.startswith("Account 4 is recorded as the Agent account, but Reya marks account 5 as the Agent")
    assert "hosted strategies refuse it" in warning
    assert "remove the PERP_AGENT_ACCOUNT_IDS and PERP_ACCOUNT_ID_1 lines" in warning
    assert result["next"].startswith(warning + " Setup complete.")
    # Following it: with both lines gone, init records the AGENTPERP account.
    monkeypatch.delenv(_env.AGENT_ACCOUNTS_VAR)
    monkeypatch.delenv("PERP_ACCOUNT_ID_1")
    FakeRelay.scenario["balances"] = [{"accountId": 5, "asset": "RUSD", "realBalance": "80"}]
    result = await init()
    assert result["step"] == "ready" and result["agent_account_id"] == 5 and "agent_account_warning" not in result
    assert f"{_env.AGENT_ACCOUNTS_VAR}=5" in env_file(setup_env) and "PERP_ACCOUNT_ID_1=5" in env_file(setup_env)


async def test_account_id_takes_an_agentperp_account_and_warns_for_another_sub_account(setup_env, monkeypatch):
    approved_owner(monkeypatch, {1: "MAINPERP", 4: "SUBPERP", 5: "AGENTPERP"}, {})
    result = await init("--account-id", "4")
    assert result["step"] == "needs_funding" and result["agent_account_id"] == 4
    assert result["agent_account_warning"].startswith("Account 4 is recorded as the Agent account, but Reya marks")
    assert result["next"].startswith(result["agent_account_warning"] + " Last step")

    def fresh(accounts: dict, names: dict) -> None:
        approved_owner(monkeypatch, accounts, names)
        monkeypatch.delenv(_env.AGENT_ACCOUNTS_VAR, raising=False)
        os.environ.pop(_env.AGENT_ACCOUNTS_VAR, None)
        os.environ.pop("PERP_ACCOUNT_ID_1", None)
        (setup_env / ".env").unlink()

    fresh({1: "MAINPERP", 5: "AGENTPERP"}, {})
    result = await init("--account-id", "5")
    assert result["agent_account_id"] == 5 and "agent_account_warning" not in result
    # With no Agent account at all, the user can still choose a sub-account to trade; it is warned about too.
    fresh({1: "MAINPERP", 4: "SUBPERP"}, {4: "Agent account"})
    result = await init("--account-id", "4")
    assert result["step"] == "needs_funding" and result["agent_account_id"] == 4
    assert result["agent_account_warning"].startswith("Account 4 is recorded as the Agent account, but Reya does not")
    assert result["next"].startswith(result["agent_account_warning"] + " Last step")


async def test_an_agentperp_account_created_after_init_asked_is_recorded(setup_env, monkeypatch):
    approved_owner(monkeypatch, {1: "MAINPERP"}, {})
    assert (await init())["step"] == "needs_agent_account"
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 6: "AGENTPERP"}
    assert (await init())["agent_account_id"] == 6


class ParsingRelay(AccountRelay):
    """The real AccountRelay's parsing of the owner's accounts, over FakeRelay's answers."""

    scenario = property(lambda self: FakeRelay.scenario)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def request(self, method, path, **kwargs):
        return await FakeRelay.request(self, method, path, **kwargs)  # type: ignore[arg-type]


async def test_a_recorded_account_of_a_type_the_kit_does_not_know_is_never_replaced(setup_env, monkeypatch):
    # A later API type for the recorded account: init must not record the AGENTPERP account instead, which
    # would move trading (PERP_ACCOUNT_ID_1) to another account without a word.
    approved_owner(monkeypatch, {1: "MAINPERP", 4: "AGENTPERP", 5: "AGENTPERP_V2"}, {4: "Agent account"})
    (setup_env / ".env").write_text(f"{_env.AGENT_ACCOUNTS_VAR}=5\nPERP_ACCOUNT_ID_1=5\n")
    (setup_env / ".env").chmod(0o600)
    monkeypatch.setenv(_env.AGENT_ACCOUNTS_VAR, "5")
    monkeypatch.setenv("PERP_ACCOUNT_ID_1", "5")
    before = env_file(setup_env)
    for relay in (FakeRelay, ParsingRelay):  # the scenario as given, and as the real account read parses it
        monkeypatch.setattr(agent, "AccountRelay", relay)
        for evaluate in (init, status):
            with pytest.raises(agent.SetupError, match="'AGENTPERP_V2', which this kit version does not support"):
                await evaluate()
    monkeypatch.setattr(agent, "AccountRelay", FakeRelay)
    assert env_file(setup_env) == before
    assert os.environ["PERP_ACCOUNT_ID_1"] == "5" and os.environ[_env.AGENT_ACCOUNTS_VAR] == "5"
    # An unknown type that is not the recorded account is simply never taken.
    approved_owner(monkeypatch, {1: "MAINPERP", 4: "SUBPERP", 6: "AGENTPERP_V2"}, {4: "Agent account"})
    monkeypatch.setenv(_env.AGENT_ACCOUNTS_VAR, "4")
    monkeypatch.setenv("PERP_ACCOUNT_ID_1", "4")
    assert (await status())["agent_account_id"] == 4
    monkeypatch.setenv(_env.AGENT_ACCOUNTS_VAR, "")
    with pytest.raises(agent.SetupError, match="sub-accounts"):
        await init("--account-id", "6")
    result = await init()
    assert result["step"] == "needs_agent_account" and "agent_account_id" not in result, "neither 6 nor 4 by name"


def test_agent_accounts_by_type_alone():
    assert agent.agent_accounts({1: "MAINPERP", 4: "SUBPERP", 5: "SUBPERP"}) == []
    assert agent.agent_accounts({1: "MAINPERP", 4: "SUBPERP", 6: "AGENTPERP", 5: "AGENTPERP"}) == [5, 6]
    assert agent.agent_accounts({4: "MAINPERP", 6: "SPOT", 7: "AGENTPERP_V2"}) == []
    assert agent.agent_account_warning({4: "SUBPERP", 5: "AGENTPERP"}, 5) is None
    unmarked = agent.agent_account_warning({4: "SUBPERP", 5: "SUBPERP"}, 4)
    assert unmarked is not None and "does not mark it as an Agent account" in unmarked
    several = agent.agent_account_warning({4: "SUBPERP", 5: "AGENTPERP", 6: "AGENTPERP"}, 4)
    assert several is not None and "accounts 5, 6" in several


async def test_own_key_mode_is_not_managed_by_init(setup_env, monkeypatch):
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    with pytest.raises(agent.SetupError, match="own-key"):
        await init("--owner", OWNER)


async def test_app_url_override_must_be_plain_https(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    # Unparseable hosts and ports are the same refusal, not a raw ValueError from urlsplit.
    for bad in ("http://example.test", "https://[broken", "https://app.example.test:abc", "https://a.test:99999"):
        monkeypatch.setenv("REYA_APP_URL", bad)
        with pytest.raises(agent.SetupError, match="plain https URL"):
            await status()
    monkeypatch.setenv("REYA_APP_URL", "https://app.example.test:0")
    with pytest.raises(agent.SetupError, match="plain https URL"):
        agent.app_base_url("https://api.reya.xyz/v2")
    monkeypatch.setenv("REYA_APP_URL", "https://app.example.test:8443")
    assert agent.app_base_url("https://api.reya.xyz/v2") == "https://app.example.test:8443"
    monkeypatch.setenv("REYA_APP_URL", "https://app.example.test/")
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    assert (await status())["app_url"] == "https://app.example.test"


def test_write_env_values_preserves_other_lines_and_rejects_newlines(setup_env):
    (setup_env / ".env").write_text("REYA_MODE=devnet\n# keep me\n")
    _env.write_env_values({"PERP_WALLET_ADDRESS_1": OWNER})
    contents = env_file(setup_env)
    assert "REYA_MODE=devnet" in contents and "# keep me" in contents and OWNER in contents
    assert stat.S_IMODE((setup_env / ".env").stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        _env.write_env_values({"PERP_WALLET_ADDRESS_1": "0x1\nPERP_PRIVATE_KEY_1=evil"})


class TestSignerSelection:
    @pytest.fixture(autouse=True)
    def deployment(self, setup_env, monkeypatch):
        monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)

    def test_own_key_without_opt_in_is_refused(self, monkeypatch):
        monkeypatch.setenv("PERP_PRIVATE_KEY_1", TEST_KEY)
        with pytest.raises(ValueError, match="REYA_SIGNER"):
            _env.get_trading_config(require_private_key=True)

    def test_own_key_opt_in_trades_any_owned_account(self, monkeypatch):
        monkeypatch.setenv("REYA_SIGNER", "own-key")
        monkeypatch.setenv("PERP_PRIVATE_KEY_1", TEST_KEY)
        monkeypatch.setenv("PERP_ACCOUNT_ID_1", "9")
        assert _env.get_trading_config(require_private_key=True).private_key == TEST_KEY

    def test_agent_key_is_refused_for_unrecorded_accounts(self, monkeypatch):
        monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
        monkeypatch.setenv(_env.AGENT_ACCOUNTS_VAR, "7")
        monkeypatch.setenv("PERP_ACCOUNT_ID_1", "9")
        with pytest.raises(ValueError, match="agent key only trades"):
            _env.get_trading_config(require_private_key=True)
        monkeypatch.setenv(_env.AGENT_ACCOUNTS_VAR, "7,9")
        assert _env.get_trading_config(require_private_key=True).private_key == TEST_KEY

    def test_missing_signer_message_names_the_guided_setup(self):
        with pytest.raises(ValueError, match="agent init"):
            _env.get_trading_config(require_private_key=True)

    def test_unknown_signer_mode_is_rejected(self, monkeypatch):
        monkeypatch.setenv("REYA_SIGNER", "keychain")
        with pytest.raises(ValueError, match="REYA_SIGNER"):
            _env.get_trading_config(require_private_key=True)


def test_doctor_reports_the_agent_signer_without_the_key(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    monkeypatch.setenv(_env.AGENT_ACCOUNTS_VAR, "7")
    result = doctor.diagnose()
    assert result["signer_mode"] == "agent"
    assert result["credential_source"] == "agent"
    assert result["agent_address"] == Account.from_key(TEST_KEY).address
    assert result["agent_account_ids"] == [7]
    assert result["errors"] == []
    assert TEST_KEY not in json.dumps(result) and "11" * 32 not in json.dumps(result)


def test_doctor_flags_an_own_key_without_opt_in(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", TEST_KEY)
    result = doctor.diagnose()
    assert result["status"] == "error"
    assert any("REYA_SIGNER" in error for error in result["errors"])
    assert TEST_KEY not in json.dumps(result)


async def test_wait_returns_as_soon_as_the_current_step_completes(setup_env, monkeypatch):
    """--wait is 'wait for this step', not 'wait for ready': the caller must see the next instruction."""
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    calls = {"n": 0}
    original = FakeRelay.accounts

    async def accounts_that_appear(self):
        calls["n"] += 1
        if calls["n"] >= 2:
            FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
        return await original(self)

    monkeypatch.setattr(FakeRelay, "accounts", accounts_that_appear)
    result = await status("--wait", "--timeout", "5", "--interval", "0")
    assert result["step"] == "needs_approval"
    assert result["waited"] is True and result["timed_out"] is False
    assert calls["n"] == 2


async def test_wait_times_out_on_an_unchanged_step(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    result = await status("--wait", "--timeout", "0.05", "--interval", "0")
    assert result["step"] == "needs_app_onboarding"
    assert result["waited"] is True and result["timed_out"] is True


async def test_init_opens_the_step_link_once_and_never_the_key(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    opened: list[str] = []

    def fake_open(url: str, new: int = 0) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(agent.webbrowser, "open", fake_open)
    args = agent.build_parser().parse_args(["init"])
    result = await agent.run(args, mutate=True)
    assert result["step"] == "needs_approval" and result["opened"] is True
    assert opened == [result["url"]]
    assert "PERP_AGENT_PRIVATE_KEY" not in opened[0] and result["agent_address"] in opened[0]
    opened.clear()
    args = agent.build_parser().parse_args(["status", "--no-open"])
    result = await agent.run(args, mutate=False)
    assert "opened" not in result and opened == []


async def test_funding_step_gives_minimum_order_and_suggested_amount(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    await init()  # generates the agent key
    agent_addr = os.environ[_env.AGENT_ADDRESS_VAR]
    FakeRelay.scenario.update(
        accounts={1: "MAINPERP", 7: "SUBPERP"},
        approved=[agent_addr.lower()],
        balances=[],
        market={"min_qty": "0.01", "mark": "3000"},
    )
    result = await init("--account-id", "7")
    assert result["step"] == "needs_funding"
    assert result["funding_guidance"] == {
        "market": "ETHRUSDPERP",
        "min_order_qty": "0.01",
        "min_order_rusd": "30.00",
        "suggested_amount_rusd": "60",
    }
    assert "Last step" in result["next"] and "at least 60 USDC" in result["next"] and "0.01" in result["next"]
    assert "rUSD" not in result["next"]  # the app calls the asset USDC
    FakeRelay.scenario["market"] = {"min_qty": "0.01", "mark": "1200"}  # 12 rUSD minimum: the floor applies
    assert (await status())["funding_guidance"]["suggested_amount_rusd"] == "50"
    for bad_mark in ("NaN", "Infinity", "0"):  # nonsense market data: no guidance, no crash
        FakeRelay.scenario["market"] = {"min_qty": "0.01", "mark": bad_mark}
        assert "funding_guidance" not in await status()
    del FakeRelay.scenario["market"]  # market reads fail: no guidance, setup still proceeds
    result = await status()
    assert result["step"] == "needs_funding" and "funding_guidance" not in result and "Last step" in result["next"]


async def test_a_step_link_is_opened_once_across_runs(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    opened: list[str] = []

    def fake_open(url: str, new: int = 0) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(agent.webbrowser, "open", fake_open)
    first = await agent.run(agent.build_parser().parse_args(["init"]), mutate=True)
    second = await agent.run(agent.build_parser().parse_args(["status"]), mutate=False)
    third = await agent.run(agent.build_parser().parse_args(["init"]), mutate=True)
    assert first["opened"] is True and opened == [first["url"]]
    assert second["opened"] is False and second["link_already_opened"] is True
    assert third["opened"] is False and len(opened) == 1, "re-running the setup must not open another tab"
    later = agent.time.time() + agent.REOPEN_AFTER_SECONDS + 1
    monkeypatch.setattr(agent.time, "time", lambda: later)
    fourth = await agent.run(agent.build_parser().parse_args(["status"]), mutate=False)
    assert fourth["opened"] is True and len(opened) == 2, "after the window the page may open again"


async def test_practice_first_is_offered_only_at_the_start(setup_env):
    assert (await init())["practice_first"] == agent.PRACTICE_FIRST  # needs_owner
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    first = await init("--owner", OWNER)  # fresh setup: owner recorded in this run
    assert first["step"] == "needs_approval" and first["practice_first"]["practice_command"].startswith(
        "python -m agent_kit.scripts.paper init"
    )
    assert "practice_first" not in await init(), "later runs do not ask again"


async def test_a_broken_state_directory_never_blocks_opening_a_step(setup_env, monkeypatch, tmp_path):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    loose = tmp_path / "loose-state"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    monkeypatch.setenv("REYA_STATE_DIR", str(loose))
    opened: list[str] = []

    def fake_open(url: str, new: int = 0) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(agent.webbrowser, "open", fake_open)
    result = await agent.run(agent.build_parser().parse_args(["init"]), mutate=True)
    assert result["step"] == "needs_approval" and result["opened"] is True and opened == [result["url"]]


def test_the_setup_page_access_code_matches_the_app():
    # The app accepts the code whose SHA-256 is this; change both together (see SETUP_PAGE_ACCESS_CODE).
    expected = "6cb2c58e7fa7fc206d53229e99cdf0ac327cf68a70174e899775cb1c7cb49179"
    assert hashlib.sha256(agent.SETUP_PAGE_ACCESS_CODE.encode()).hexdigest() == expected


async def test_one_setup_page_covers_every_step_and_opens_once(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    opened: list[str] = []

    def fake_open(url: str, new: int = 0) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(agent.webbrowser, "open", fake_open)
    first = await agent.run(agent.build_parser().parse_args(["init"]), mutate=True)
    assert first["step"] == "needs_approval" and first["opened"] is True
    assert opened == [first["url"]] and "/agents/setup?agent=" in first["url"] and "#access=" in first["url"]
    FakeRelay.scenario["approved"] = [first["agent_address"].lower()]
    second = await agent.run(agent.build_parser().parse_args(["init"]), mutate=True)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 2: "AGENTPERP"}
    third = await agent.run(agent.build_parser().parse_args(["init"]), mutate=True)
    fourth = await agent.run(agent.build_parser().parse_args(["init"]), mutate=True)
    assert [second["step"], third["step"], fourth["step"]] == ["needs_agent_account", "needs_funding", "needs_funding"]
    assert second["url"] == first["url"]
    # The link names the recorded account; the page already open finds that same Agent account by type.
    assert third["url"] == fourth["url"] == first["url"].replace("#access", "&account=2#access")
    assert second["link_already_opened"] is True and third["link_already_opened"] is True
    assert fourth["link_already_opened"] is True
    assert len(opened) == 1, "the whole setup is one browser tab"


def init_args(*argv):
    return agent.build_parser().parse_args(["init", *argv])


async def test_with_no_browser_until_ready_returns_the_link_at_once_then_waits(setup_env, monkeypatch, capsys):
    # A server, an SSH session or WSL without Windows access: the user opens the link on another
    # device, and an assistant sees the link only when the command returns.
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    opened = record_opens(monkeypatch)
    monkeypatch.setattr(agent, "browser_unavailable", lambda: "no_display")
    run_by(monkeypatch, a_terminal=False)
    sleeps: list[float] = []

    async def no_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(agent.asyncio, "sleep", no_sleep)
    first = await agent.run(init_args("--until-ready", "--timeout", "30", "--interval", "0"), mutate=True)
    assert first["step"] == "needs_approval" and first["opened"] is False and first["url"]
    assert first["browser_unavailable"] == "no_display" and first["waited"] is False and "timed_out" not in first
    assert first["next"].startswith("No browser opened (this machine has no display). Show the user url")
    assert "phone wallet" in first["next"] and "agent init --until-ready again" in first["next"]
    assert sleeps == [] and opened == [], "returned before waiting, and never tried a text browser"
    assert first["url"] in capsys.readouterr().err
    # The assistant has shown the link: the next run waits for the page instead of returning again.
    second = await agent.run(init_args("--until-ready", "--timeout", "0.05", "--interval", "0"), mutate=True)
    assert second["waited"] is True and second["timed_out"] is True and sleeps
    assert second["opened"] is False and second["link_already_opened"] is True
    assert "browser_unavailable" not in second and not second["next"].startswith("No browser opened")
    assert capsys.readouterr().err == ""


async def test_at_a_terminal_the_link_is_printed_and_the_wait_goes_on(setup_env, monkeypatch, capsys):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    monkeypatch.setattr(agent, "browser_unavailable", lambda: "ssh")
    run_by(monkeypatch, a_terminal=True)
    result = await agent.run(init_args("--until-ready", "--timeout", "0.05", "--interval", "0"), mutate=True)
    assert result["waited"] is True and result["timed_out"] is True
    assert result["opened"] is False and result["browser_unavailable"] == "ssh"
    assert result["next"].startswith("No browser opened (this is an SSH session).")
    assert capsys.readouterr().err.count(result["url"]) == 1, "printed once, when the link first came up"


async def test_the_open_result_is_still_reported_after_waiting(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    opened = record_opens(monkeypatch)
    result = await agent.run(init_args("--until-ready", "--timeout", "0.05", "--interval", "0"), mutate=True)
    assert result["timed_out"] is True and result["opened"] is True and opened == [result["url"]]


async def test_a_browser_that_fails_to_start_is_reported_and_tried_again_next_run(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    opened = record_opens(monkeypatch, result=False)
    first = await agent.run(init_args(), mutate=True)
    assert first["opened"] is False and first["browser_unavailable"] == "no_browser"
    assert first["next"].startswith("No browser opened (no browser could be started).")
    second = await agent.run(init_args(), mutate=True)
    assert second["browser_unavailable"] == "no_browser" and len(opened) == 2


@pytest.mark.parametrize(
    "platform,env,wsl,opener,expected",
    [
        ("linux", {}, False, None, "no_display"),
        ("linux", {"DISPLAY": ":0"}, False, None, None),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, False, None, None),
        ("darwin", {}, False, None, None),
        ("darwin", {"SSH_CONNECTION": "10.0.0.2 52000 10.0.0.1 22"}, False, None, "ssh"),
        ("linux", {"DISPLAY": "localhost:10.0", "SSH_TTY": "/dev/pts/1"}, False, None, "ssh"),
        # A remote editor names its own opener, which shows the page on the user's machine.
        ("linux", {"SSH_CONNECTION": "x", "BROWSER": "/home/u/.vscode-server/helpers/browser.sh"}, False, None, None),
        # WSLg sets DISPLAY, but the Linux openers fail there ("gio: Operation not supported").
        ("linux", {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}, True, None, "wsl_no_windows_browser"),
        ("linux", {}, True, ["/usr/bin/wslview"], None),
    ],
)
def test_which_machines_can_show_the_user_a_page(monkeypatch, platform, env, wsl, opener, expected):
    for name in ("BROWSER", "SSH_CONNECTION", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(agent.sys, "platform", platform)
    monkeypatch.setattr(agent, "in_wsl", lambda: wsl)
    monkeypatch.setattr(agent, "windows_opener", lambda: opener)
    assert BROWSER_UNAVAILABLE() == expected


def test_wsl_is_recognised_by_its_environment_or_its_kernel(monkeypatch, tmp_path):
    for name in ("WSL_DISTRO_NAME", "WSL_INTEROP"):
        monkeypatch.delenv(name, raising=False)
    release = tmp_path / "osrelease"
    release.write_text("6.6.87.2-microsoft-standard-WSL2\n")
    assert IN_WSL(release) is True
    release.write_text("6.8.0-1017-azure\n")
    assert IN_WSL(release) is False
    assert IN_WSL(tmp_path / "missing") is False  # macOS has no /proc
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert IN_WSL(tmp_path / "missing") is True


def test_from_wsl_the_page_opens_with_wslview_or_explorer(monkeypatch, tmp_path):
    binfmt = tmp_path / "binfmt_misc"
    binfmt.mkdir()
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    assert agent.windows_opener(binfmt) is None, "interop is off: Windows programs cannot start"
    (binfmt / "WSLInterop").write_text("enabled\n")
    explorer = tmp_path / "explorer.exe"
    explorer.write_text("")
    monkeypatch.setattr(agent.shutil, "which", lambda name: str(explorer) if name == "explorer.exe" else None)
    assert agent.windows_opener(binfmt) == [str(explorer)]
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/wslview" if name == "wslview" else None)
    assert agent.windows_opener(binfmt) == ["/usr/bin/wslview"]


@pytest.mark.parametrize(
    "program,code,expected",
    [
        ("explorer.exe", 1, True),  # explorer.exe exits with 1 even when it opened the page
        ("wslview", 0, True),
        ("wslview", 1, False),
        ("missing.exe", None, False),  # the program could not start
    ],
)
def test_on_wsl_the_link_goes_to_the_windows_browser(monkeypatch, program, code, expected):
    launched: list = []

    class FakeProcess:
        def __init__(self, args, **kwargs):
            if code is None:
                raise FileNotFoundError(args[0])
            launched.append((args, kwargs))

        def wait(self, timeout):
            return code

    command = f"/mnt/c/Windows/{program}"
    monkeypatch.setattr(agent, "in_wsl", lambda: True)
    monkeypatch.setattr(agent, "windows_opener", lambda: [command])
    monkeypatch.setattr(agent.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(agent.webbrowser, "open", lambda *a, **k: pytest.fail("Linux openers fail under WSL"))
    url = "https://app.reya.xyz/agents/setup?agent=0x1&owner=0x2#access=code"
    assert agent.open_in_browser(url) is expected
    if code is not None:
        ((args, kwargs),) = launched
        assert args == [command, url], "the URL is one argument, never passed through a shell"
        assert kwargs["stdin"] is subprocess.DEVNULL and not kwargs.get("shell")


async def test_until_ready_waits_through_every_step_on_the_page(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    # The user works through the page while the kit polls: approve, create, fund.
    moves = [
        lambda: FakeRelay.scenario.update(approved=[address.lower()]),
        lambda: FakeRelay.scenario.update(accounts={1: "MAINPERP", 2: "AGENTPERP"}),
        lambda: FakeRelay.scenario.update(balances=[{"accountId": 2, "asset": "RUSD", "realBalance": "60"}]),
    ]

    async def user_acts(_seconds):
        if moves:
            moves.pop(0)()

    monkeypatch.setattr(agent.asyncio, "sleep", user_acts)
    result = await init("--until-ready", "--timeout", "30", "--interval", "0")
    assert result["step"] == "ready" and result["agent_account_id"] == 2
    assert result["waited"] is True and result["timed_out"] is False and not moves
    # Plain --wait still returns at the first step change.
    FakeRelay.scenario = {"accounts": {1: "MAINPERP"}, "approved": [], "balances": []}
    monkeypatch.delenv(_env.AGENT_ACCOUNTS_VAR, raising=False)
    os.environ.pop(_env.AGENT_ACCOUNTS_VAR, None)
    os.environ.pop("PERP_ACCOUNT_ID_1", None)
    moves[:] = [lambda: FakeRelay.scenario.update(approved=[address.lower()])]
    assert (await init("--wait", "--timeout", "30", "--interval", "0"))["step"] == "needs_agent_account"


async def test_until_ready_times_out_with_progress(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    result = await init("--until-ready", "--timeout", "0.05", "--interval", "0")
    assert result["step"] == "needs_approval" and result["progress"] == {"step": 1, "of": 3}
    assert result["waited"] is True and result["timed_out"] is True
    # Still unapproved after the wait: the usual cause is the page on another wallet, which the kit cannot see.
    assert f"connected to a different wallet than {OWNER}" in result["next"]
    assert "remove the PERP_WALLET_ADDRESS_1 line" in result["next"]


async def test_wrong_wallet_hint_only_while_approval_is_awaited(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    FakeRelay.scenario["approved"] = [Account.from_key(TEST_KEY).address.lower()]
    result = await init("--until-ready", "--timeout", "0.05", "--interval", "0")
    assert result["step"] == "needs_agent_account" and result["timed_out"] is True
    assert "different wallet" not in result["next"]
    # Not waiting at all: no hint either.
    FakeRelay.scenario["approved"] = []
    assert "different wallet" not in (await init())["next"]


async def test_setup_link_names_the_owner_checksummed(setup_env):
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    result = await init("--owner", OWNER.lower())
    assert result["step"] == "needs_approval"
    assert f"&owner={OWNER}#access=" in result["url"] and OWNER != OWNER.lower()


async def test_setup_link_names_the_recorded_account_whatever_its_name(setup_env, monkeypatch):
    approved_owner(monkeypatch, {1: "MAINPERP"}, {})
    FakeRelay.scenario["approved"] = []
    page = f"https://app.reya.xyz/agents/setup?agent={Account.from_key(TEST_KEY).address}&owner={OWNER}"
    access = f"#access={agent.SETUP_PAGE_ACCESS_CODE}"
    # No account recorded yet: the page finds the Agent account by type.
    result = await init()
    assert result["step"] == "needs_approval" and result["url"] == page + access
    FakeRelay.scenario["approved"] = [Account.from_key(TEST_KEY).address.lower()]
    result = await init()
    assert result["step"] == "needs_agent_account" and result["url"] == page + access
    # Recorded: the link names it, so the page uses it although it is not an Agent account (AGENTPERP).
    FakeRelay.scenario.update(accounts={1: "MAINPERP", 7: "SUBPERP"}, names={7: "Bot"})
    result = await init("--account-id", "7")
    assert result["step"] == "needs_funding" and result["url"] == page + "&account=7" + access
    result = await init("--amount", "250")
    assert result["url"] == page + "&account=7&amount=250" + access
    assert result["step_url"] == "https://app.reya.xyz/portfolio?transfer=1&from=1&to=7&amount=250"
    # Approval gone (a reinstall, or revoked): the page uses the recorded account from the first step.
    FakeRelay.scenario["approved"] = []
    result = await init()
    assert result["step"] == "needs_approval" and result["url"] == page + "&account=7" + access


async def test_until_ready_keeps_one_tab_when_the_link_gains_the_account(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    moves = [
        lambda: FakeRelay.scenario.update(approved=[address.lower()]),
        lambda: FakeRelay.scenario.update(accounts={1: "MAINPERP", 2: "AGENTPERP"}),
        lambda: FakeRelay.scenario.update(balances=[{"accountId": 2, "asset": "RUSD", "realBalance": "60"}]),
    ]

    async def user_acts(_seconds):
        if moves:
            moves.pop(0)()

    opened: list[str] = []
    seen: list[dict] = []

    def fake_open(url: str, new: int = 0) -> bool:
        opened.append(url)
        return True

    evaluate = agent.Setup.evaluate

    async def evaluate_and_keep_result(self):
        result = await evaluate(self)
        seen.append(result)
        return result

    monkeypatch.setattr(agent.asyncio, "sleep", user_acts)
    monkeypatch.setattr(agent.webbrowser, "open", fake_open)
    monkeypatch.setattr(agent.Setup, "evaluate", evaluate_and_keep_result)
    args = agent.build_parser().parse_args(["init", "--until-ready", "--timeout", "30", "--interval", "0"])
    result = await agent.run(args, mutate=True)
    assert result["step"] == "ready" and not moves
    page = seen[0]["url"]
    assert page and [r.get("url") for r in seen[1:]] == [page, page.replace("#access", "&account=2#access"), None]
    assert opened == [page], "the page opened at approval finds the new Agent account by type: one tab"
    assert seen[2]["step"] == "needs_funding" and seen[2]["opened"] is True, "still the page that was opened"


async def test_a_link_naming_an_account_the_open_page_cannot_find_opens(setup_env, monkeypatch):
    # The page already open looks for an Agent account (AGENTPERP) and would ask for a new one: open the
    # page for account 7.
    approved_owner(monkeypatch, {1: "MAINPERP", 7: "SUBPERP"}, {7: "Bot"})
    opened: list[str] = []

    def fake_open(url: str, new: int = 0) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(agent.webbrowser, "open", fake_open)
    first = await agent.run(agent.build_parser().parse_args(["init"]), mutate=True)
    assert first["step"] == "needs_agent_account" and opened == [first["url"]]
    second = await agent.run(agent.build_parser().parse_args(["init", "--account-id", "7"]), mutate=True)
    assert second["step"] == "needs_funding" and second["opened"] is True
    assert opened == [first["url"], second["url"]] and "&account=7#access=" in second["url"]
    third = await agent.run(agent.build_parser().parse_args(["init"]), mutate=True)
    assert third["link_already_opened"] is True and len(opened) == 2


async def test_switching_owner_says_how(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    other = Account.from_key("0x" + "33" * 32).address
    with pytest.raises(agent.SetupError) as raised:
        await init("--owner", other)
    message = str(raised.value)
    assert f"different owner ({OWNER})" in message and "remove that line from the kit's .env" in message


async def test_practice_is_asked_before_any_page_opens(setup_env, monkeypatch):
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    opened: list[str] = []

    def fake_open(url: str, new: int = 0) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(agent.webbrowser, "open", fake_open)
    fresh = await agent.run(agent.build_parser().parse_args(["init", "--owner", OWNER]), mutate=True)
    assert fresh["step"] == "needs_approval" and fresh["practice_first"] == agent.PRACTICE_FIRST
    assert fresh["opened"] is False and opened == []
    assert fresh["next"].startswith("First ask the user practice_first.question.")
    # The user chose real trading: the next run opens the setup page.
    live = await agent.run(agent.build_parser().parse_args(["init"]), mutate=True)
    assert live["opened"] is True and opened == [live["url"]]


# ---------------------------------------------------------------- Enable hosted runs (optional)

HOSTED = Account.from_key("0x" + "33" * 32).address


def funded_setup(monkeypatch) -> str:
    """An owner whose agent key is approved and whose Agent account (2) is funded; the agent address."""
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario.update(
        accounts={1: "MAINPERP", 2: "AGENTPERP"},
        approved=[address.lower()],
        balances=[{"accountId": 2, "asset": "RUSD", "realBalance": "60"}],
    )
    return address


async def test_hosted_runs_are_not_offered_without_a_workflow_service(setup_env, monkeypatch):
    funded_setup(monkeypatch)
    result = await init()
    assert result["step"] == "ready" and result["hosted_runs"] == {"status": "unavailable"}
    assert "hosted" not in result["next"].lower(), "nothing to offer: say nothing about it"
    assert result["try_asking"] == agent.TRY_ASKING


async def test_hosted_runs_are_offered_once_setup_is_ready(setup_env, monkeypatch):
    funded_setup(monkeypatch)
    FakeRelay.scenario["hosted_signer"] = HOSTED
    result = await init()
    # Trading is ready either way: the offer never holds setup up.
    assert result["step"] == "ready" and result["try_asking"] == agent.TRY_ASKING
    hosted = result["hosted_runs"]
    assert hosted["status"] == "not_enabled" and hosted["hosted_signer"] == HOSTED
    offer = hosted["offer"]
    assert offer["question"] == agent.HOSTED_RUNS_QUESTION
    assert HOSTED in offer["permission"] and "every account this wallet owns" in offer["permission"]
    assert "cannot withdraw, transfer or change permissions" in offer["permission"]
    assert offer["now_command"] == "agent init --hosted-runs now --until-ready --timeout 100"
    assert offer["later_command"] == "agent init --hosted-runs later"
    assert "hosted_runs.offer.question once" in result["next"]
    assert "progress" not in result
    # status reports the same offer and records nothing.
    assert (await status())["hosted_runs"]["offer"] == offer
    assert agent.read_hosted_runs_choice(OWNER) is None


async def test_later_is_remembered_and_setup_stays_ready(setup_env, monkeypatch):
    funded_setup(monkeypatch)
    FakeRelay.scenario["hosted_signer"] = HOSTED
    result = await init("--hosted-runs", "later")
    assert result["step"] == "ready" and "hosted_runs_later" in result["actions"]
    assert result["hosted_runs"] == {"status": "not_enabled", "choice": "later", "hosted_signer": HOSTED}
    assert agent.HOSTED_RUNS_LATER in result["next"]
    path = agent._hosted_runs_choice_path(OWNER)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Asked once: later runs, init and status alike, do not offer it again.
    for again in (await init(), await status()):
        assert again["step"] == "ready" and "offer" not in again["hosted_runs"]
        assert again["hosted_runs"]["choice"] == "later"
    # Enabled later anyway (from the app or strategy info --open): on, whatever was answered.
    FakeRelay.scenario["approved"].append(HOSTED.lower())
    assert (await init())["hosted_runs"] == {"status": "enabled", "hosted_signer": HOSTED}


async def test_an_unreadable_answer_asks_again(setup_env, monkeypatch):
    funded_setup(monkeypatch)
    FakeRelay.scenario["hosted_signer"] = HOSTED
    await init("--hosted-runs", "later")
    agent._hosted_runs_choice_path(OWNER).write_text("not json")
    assert "offer" in (await init())["hosted_runs"]


async def test_already_enabled_is_ready_with_hosted_runs_on(setup_env, monkeypatch):
    address = funded_setup(monkeypatch)
    FakeRelay.scenario.update(hosted_signer=HOSTED, approved=[address.lower(), HOSTED.lower()])
    result = await init("--hosted-runs", "now")
    assert result["step"] == "ready"
    assert result["hosted_runs"] == {"status": "enabled", "hosted_signer": HOSTED}
    assert "Hosted runs are on" in result["next"]


async def test_now_waits_through_the_hosted_step_on_the_same_tab(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario.update(accounts={1: "MAINPERP"}, hosted_signer=HOSTED)
    # The user works through the whole page while the kit polls: approve, create, fund, enable hosted runs.
    moves = [
        lambda: FakeRelay.scenario.update(approved=[address.lower()]),
        lambda: FakeRelay.scenario.update(accounts={1: "MAINPERP", 2: "AGENTPERP"}),
        lambda: FakeRelay.scenario.update(balances=[{"accountId": 2, "asset": "RUSD", "realBalance": "60"}]),
        lambda: None,  # one poll at the hosted step before the signature
        lambda: FakeRelay.scenario["approved"].append(HOSTED.lower()),
    ]

    async def user_acts(_seconds):
        if moves:
            moves.pop(0)()

    seen: list[dict] = []
    evaluate = agent.Setup.evaluate

    async def evaluate_and_keep_result(self):
        result = await evaluate(self)
        seen.append(result)
        return result

    opened = record_opens(monkeypatch)
    monkeypatch.setattr(agent.asyncio, "sleep", user_acts)
    monkeypatch.setattr(agent.Setup, "evaluate", evaluate_and_keep_result)
    args = agent.build_parser().parse_args(
        ["init", "--hosted-runs", "now", "--until-ready", "--timeout", "30", "--interval", "0"]
    )
    result = await agent.run(args, mutate=True)
    assert result["step"] == "ready" and result["timed_out"] is False and not moves
    assert result["hosted_runs"] == {"status": "enabled", "hosted_signer": HOSTED}
    steps = [r["step"] for r in seen]
    assert steps == [
        "needs_approval",
        "needs_agent_account",
        "needs_funding",
        "needs_hosted_runs",
        "needs_hosted_runs",
        "ready",
    ]
    hosted_step = seen[3]
    assert hosted_step["hosted_signer"] == HOSTED and HOSTED in hosted_step["permission"]
    assert hosted_step["url"] == seen[2]["url"], "the funding step's page moves on to Enable hosted runs"
    assert len(opened) == 1, "one tab for the whole setup, Enable hosted runs included"
    assert FakeRelay.scenario["hosted_reads"] == 1, "the service is read once per run, not on every poll"


async def test_progress_counts_four_steps_only_while_hosted_runs_are_still_to_do(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    assert (await init("--hosted-runs", "now"))["progress"] == {"step": 1, "of": 3}, "no service: no 4th step"
    FakeRelay.scenario["hosted_signer"] = HOSTED
    assert (await init())["progress"] == {"step": 1, "of": 3}, "not chosen: 3 steps"
    assert (await init("--hosted-runs", "now"))["progress"] == {"step": 1, "of": 4}
    FakeRelay.scenario["approved"] = [HOSTED.lower()]
    assert (await init("--hosted-runs", "now"))["progress"] == {"step": 1, "of": 3}, "already enabled"


async def test_with_no_browser_the_hosted_step_keeps_its_own_command(setup_env, monkeypatch):
    funded_setup(monkeypatch)
    FakeRelay.scenario["hosted_signer"] = HOSTED
    monkeypatch.setattr(agent, "browser_unavailable", lambda: "ssh")
    run_by(monkeypatch, a_terminal=False)
    args = agent.build_parser().parse_args(["init", "--hosted-runs", "now", "--until-ready", "--timeout", "30"])
    result = await agent.run(args, mutate=True)
    assert result["step"] == "needs_hosted_runs" and result["waited"] is False
    assert result["browser_unavailable"] == "ssh" and result["url"]
    assert result["next"].startswith("No browser opened (this is an SSH session).")
    assert "agent init --hosted-runs now --until-ready --timeout 100" in result["next"]
    assert "Relay the permission text" in result["next"]


async def test_a_reinstall_that_chooses_now_is_not_asked_to_practise(setup_env, monkeypatch):
    # The owner is given again after a reinstall (owner_recorded), on a setup that is already funded.
    funded_setup(monkeypatch)
    monkeypatch.delenv("PERP_WALLET_ADDRESS_1")
    FakeRelay.scenario["hosted_signer"] = HOSTED
    result = await init("--owner", OWNER, "--hosted-runs", "now")
    assert result["step"] == "needs_hosted_runs" and "owner_recorded" in result["actions"]
    assert "practice_first" not in result


async def test_a_setup_link_differing_only_by_amount_is_not_opened_again(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    opened = record_opens(monkeypatch)
    run = agent.build_parser().parse_args
    assert (await agent.run(run(["init"]), mutate=True))["opened"] is True
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario.update(approved=[address.lower()], accounts={1: "MAINPERP", 2: "AGENTPERP"})
    funding = await agent.run(run(["init", "--amount", "50"]), mutate=True)
    assert funding["step"] == "needs_funding" and "&amount=50" in funding["url"]
    assert funding["link_already_opened"] is True and len(opened) == 1, "the page opened at approval is the same page"
    assert "may not show this amount" in funding["next"]


async def test_now_after_a_funding_link_with_an_amount_opens_no_second_tab(setup_env, monkeypatch):
    address = funded_setup(monkeypatch)
    FakeRelay.scenario.update(hosted_signer=HOSTED, balances=[])
    opened = record_opens(monkeypatch)
    run = agent.build_parser().parse_args
    funding = await agent.run(run(["init", "--amount", "50"]), mutate=True)
    assert funding["step"] == "needs_funding" and funding["opened"] is True and "&amount=50" in opened[0]
    FakeRelay.scenario["balances"] = [{"accountId": 2, "asset": "RUSD", "realBalance": "50"}]
    assert "offer" in (await agent.run(run(["init"]), mutate=True))["hosted_runs"]
    # The user says now: the tab left at the funding step already shows Enable hosted runs.
    result = await agent.run(run(["init", "--hosted-runs", "now"]), mutate=True)
    assert result["step"] == "needs_hosted_runs" and result["progress"] == {"step": 4, "of": 4}
    assert result["url"] == opened[0].replace("&amount=50", "")
    assert result["opened"] is False and result["link_already_opened"] is True and len(opened) == 1
    assert "agent init --hosted-runs later" in result["next"] and address not in result["permission"]


async def test_now_clears_an_earlier_later(setup_env, monkeypatch):
    funded_setup(monkeypatch)
    FakeRelay.scenario["hosted_signer"] = HOSTED
    await init("--hosted-runs", "later")
    result = await init("--hosted-runs", "now")
    assert result["step"] == "needs_hosted_runs" and agent.read_hosted_runs_choice(OWNER) is None
    # Chosen but never signed: the next plain run offers it again rather than waiting forever.
    assert (await init())["hosted_runs"]["offer"]["question"] == agent.HOSTED_RUNS_QUESTION


async def test_a_broken_state_directory_never_blocks_the_hosted_runs_step(setup_env, monkeypatch, tmp_path):
    funded_setup(monkeypatch)
    FakeRelay.scenario["hosted_signer"] = HOSTED
    assert (await init())["agent_account_id"] == 2  # recorded first: only the hosted-runs files are at stake
    loose = tmp_path / "loose-state"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    monkeypatch.setenv("REYA_STATE_DIR", str(loose))
    later = await init("--hosted-runs", "later")
    assert later["step"] == "ready" and "hosted_runs_later" in later["actions"]
    assert (await init("--hosted-runs", "now"))["step"] == "needs_hosted_runs"


async def test_the_hosted_step_follows_an_agent_account_found_by_type(setup_env, monkeypatch):
    # The API marks the Agent account AGENTPERP whatever its name: the hosted step uses that account's page.
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario.update(
        accounts={1: "MAINPERP", 3: "AGENTPERP"},
        names={3: "My bot"},
        approved=[address.lower()],
        balances=[{"accountId": 3, "asset": "RUSD", "realBalance": "60"}],
        hosted_signer=HOSTED,
    )
    ready = await init()
    assert ready["step"] == "ready" and ready["agent_account_id"] == 3 and "offer" in ready["hosted_runs"]
    result = await init("--hosted-runs", "now")
    assert result["step"] == "needs_hosted_runs" and "&account=3#access" in result["url"]
