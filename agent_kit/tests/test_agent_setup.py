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

import pytest
from eth_account import Account

from agent_kit.scripts import _env, agent, doctor

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
    yield kit_root
    for name in SETUP_VARS:  # write_env_values exports into os.environ outside monkeypatch
        os.environ.pop(name, None)


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

    # Approved: ask for the agent account and remember which accounts existed before.
    FakeRelay.scenario["approved"] = [agent_address.lower()]
    result = await init()
    assert result["step"] == "needs_agent_account"
    assert result["url"] == setup_url and result["progress"] == {"step": 2, "of": 3}
    assert result["step_url"] == "https://app.reya.xyz/portfolio?createAccount=1&name=Agent%20account&source=agent"
    snapshot = agent.read_snapshot(OWNER)
    assert snapshot is not None and snapshot["before_accounts"] == [1]

    # A new sub-account appeared: record it, point PERP_ACCOUNT_ID_1 at it, ask for funding.
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 2: "SUBPERP"}
    result = await init("--amount", "250")
    assert result["step"] == "needs_funding"
    assert result["agent_account_id"] == 2 and result["from_account"] == 1
    assert result["url"] == setup_url.replace("#access", "&amount=250#access")
    assert result["step_url"] == "https://app.reya.xyz/portfolio?transfer=1&from=1&to=2&amount=250"
    assert result["progress"] == {"step": 3, "of": 3}
    assert f"{_env.AGENT_ACCOUNTS_VAR}=2" in env_file(kit_root)
    assert "PERP_ACCOUNT_ID_1=2" in env_file(kit_root)
    assert agent.read_snapshot(OWNER) is None

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


async def test_new_main_or_spot_account_is_never_taken_as_the_agent_account(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario = {"accounts": {1: "MAINPERP"}, "approved": [address.lower()], "balances": []}
    assert (await init())["step"] == "needs_agent_account"
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 5: "SPOT"}
    assert (await init())["step"] == "needs_agent_account"
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 5: "SPOT", 7: "SUBPERP", 8: "SUBPERP"}
    FakeRelay.scenario["names"] = {7: "Agent1", 8: "Agent2"}
    result = await init()
    assert result["step"] == "ambiguous_new_accounts" and result["candidates"] == [7, 8]
    with pytest.raises(agent.SetupError):
        await init("--account-id", "1")
    assert (await init("--account-id", "8"))["agent_account_id"] == 8


async def test_several_new_accounts_resolve_to_the_exact_agent_account_name(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario = {"accounts": {1: "MAINPERP"}, "approved": [address.lower()], "balances": []}
    assert (await init())["step"] == "needs_agent_account"
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 7: "SUBPERP", 8: "SUBPERP", 9: "SUBPERP"}
    FakeRelay.scenario["names"] = {7: "Agent1", 8: "Agent account.", 9: "Agent account"}
    result = await init()
    assert result["step"] == "needs_funding" and result["agent_account_id"] == 9
    # Two exact matches stay ambiguous.
    FakeRelay.scenario = {"accounts": {1: "MAINPERP"}, "approved": [address.lower()], "balances": []}
    monkeypatch.delenv(_env.AGENT_ACCOUNTS_VAR, raising=False)
    os.environ.pop(_env.AGENT_ACCOUNTS_VAR, None)
    assert (await init())["step"] == "needs_agent_account"
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 7: "SUBPERP", 8: "SUBPERP"}
    FakeRelay.scenario["names"] = {7: "Agent account", 8: "Agent account"}
    assert (await init())["step"] == "ambiguous_new_accounts"


async def test_status_reports_an_account_created_before_init_ran_and_init_records_it(setup_env):
    # The README's flow: install runs init once (at approval), then the assistant polls status.
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    result = await init("--owner", OWNER)
    assert result["step"] == "needs_approval"
    FakeRelay.scenario["approved"] = [result["agent_address"].lower()]
    result = await status()
    assert result["step"] == "needs_agent_account" and "createAccount=1" in result["step_url"]
    # The account is created before any init recorded which accounts existed.
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 2: "SUBPERP"}
    FakeRelay.scenario["names"] = {2: "Agent account"}
    result = await status()
    assert result["step"] == "needs_agent_account" and result["found_account_id"] == 2
    assert "url" not in result and result["next"].startswith('Found your "Agent account" (account 2). Run agent init')
    result = await init()
    assert result["step"] == "needs_funding" and result["agent_account_id"] == 2


def approved_owner(monkeypatch, accounts: dict, names: dict) -> None:
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario = {"accounts": accounts, "approved": [address.lower()], "balances": [], "names": names}


async def test_an_existing_agent_account_is_used_instead_of_asking_for_another(setup_env, monkeypatch):
    approved_owner(monkeypatch, {1: "MAINPERP", 4: "SUBPERP", 5: "SUBPERP"}, {4: "Bot", 5: "Agent account"})
    result = await init()
    assert result["step"] == "needs_funding" and result["agent_account_id"] == 5
    assert result["next"].startswith('Using your "Agent account" (account 5). Last step')
    assert "createAccount" not in result["step_url"]
    assert f"{_env.AGENT_ACCOUNTS_VAR}=5" in env_file(setup_env)
    # Already funded: straight to ready, still saying which account it trades.
    FakeRelay.scenario["balances"] = [{"accountId": 5, "asset": "RUSD", "realBalance": "80"}]
    result = await init()
    assert result["step"] == "ready" and result["agent_account_id"] == 5


async def test_the_agent_account_name_wins_over_another_new_sub_account(setup_env, monkeypatch):
    approved_owner(monkeypatch, {1: "MAINPERP", 5: "SUBPERP"}, {5: "Agent account"})
    # A snapshot from an earlier run lists account 5; the user then creates another sub-account.
    agent.write_snapshot(OWNER, {"before_accounts": [1, 5], "created_at_ms": 0})
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 5: "SUBPERP", 6: "SUBPERP"}
    FakeRelay.scenario["names"] = {5: "Agent account", 6: "Scalper"}
    result = await init()
    assert result["step"] == "needs_funding" and result["agent_account_id"] == 5


async def test_several_existing_agent_accounts_ask_which_one_and_create_nothing(setup_env, monkeypatch):
    approved_owner(monkeypatch, {1: "MAINPERP", 4: "SUBPERP", 5: "SUBPERP"}, {4: "Agent account", 5: "Agent account"})
    result = await init()
    assert result["step"] == "ambiguous_new_accounts" and result["candidates"] == [4, 5]
    assert result["next"].startswith('This wallet has 2 accounts named "Agent account" (4, 5). Ask the user')
    assert "the others are left alone" in result["next"] and "url" not in result
    assert (await init("--account-id", "4"))["agent_account_id"] == 4


async def test_a_differently_named_sub_account_is_not_taken(setup_env, monkeypatch):
    approved_owner(monkeypatch, {1: "MAINPERP", 4: "SUBPERP"}, {4: "Agent account 2"})
    result = await init()
    assert result["step"] == "needs_agent_account" and "createAccount=1" in result["step_url"]


async def test_own_key_mode_is_not_managed_by_init(setup_env, monkeypatch):
    monkeypatch.setenv("REYA_SIGNER", "own-key")
    with pytest.raises(agent.SetupError, match="own-key"):
        await init("--owner", OWNER)


async def test_app_url_override_must_be_plain_https(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv("REYA_APP_URL", "http://example.test")
    with pytest.raises(agent.SetupError):
        await status()
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
    FakeRelay.scenario["accounts"] = {1: "MAINPERP", 2: "SUBPERP"}
    FakeRelay.scenario["names"] = {2: "Agent account"}
    third = await agent.run(agent.build_parser().parse_args(["init"]), mutate=True)
    assert [second["step"], third["step"]] == ["needs_agent_account", "needs_funding"]
    assert second["url"] == third["url"] == first["url"]
    assert second["link_already_opened"] is True and third["link_already_opened"] is True
    assert len(opened) == 1, "the whole setup is one browser tab"


async def test_until_ready_waits_through_every_step_on_the_page(setup_env, monkeypatch):
    monkeypatch.setenv("PERP_WALLET_ADDRESS_1", OWNER)
    monkeypatch.setenv(_env.AGENT_KEY_VAR, TEST_KEY)
    address = Account.from_key(TEST_KEY).address
    FakeRelay.scenario["accounts"] = {1: "MAINPERP"}
    # The user works through the page while the kit polls: approve, create, fund.
    moves = [
        lambda: FakeRelay.scenario.update(approved=[address.lower()]),
        lambda: FakeRelay.scenario.update(accounts={1: "MAINPERP", 2: "SUBPERP"}, names={2: "Agent account"}),
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
