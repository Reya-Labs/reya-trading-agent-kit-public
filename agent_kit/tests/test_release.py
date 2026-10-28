"""Release/update check: best effort, bounded, never prompts, never fails a command.

Offline: `subprocess.run` is replaced, so no git process or network call happens.
"""

from types import SimpleNamespace

import json
import stat
import subprocess
import time

import pytest

from agent_kit.scripts import _common, _release

LS_REMOTE = (
    "aaa\trefs/tags/v0.2.0\n"
    "bbb\trefs/tags/v0.10.0\n"
    "ccc\trefs/tags/v0.9.3\n"
    "ddd\trefs/tags/v1.0.0-rc1\n"
    "eee\trefs/tags/not-a-release\n"
)


@pytest.fixture
def git_checkout(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(_release.UPDATE_CHECK_VAR, "1")
    return tmp_path


def fake_run(stdout="", returncode=0, raises=None, seen=None):
    def run(cmd, **kwargs):
        if seen is not None:
            seen.append((cmd, kwargs))
        if raises:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return run


def test_latest_release_is_the_highest_plain_semver_tag(git_checkout, monkeypatch):
    seen: list = []
    monkeypatch.setattr(_release.subprocess, "run", fake_run(LS_REMOTE, seen=seen))
    assert _release.latest_release_tag(git_checkout) == "v0.10.0"
    cmd, kwargs = seen[0]
    assert cmd[-5:] == ["ls-remote", "--tags", "--refs", "origin", "v*"]
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0" and "BatchMode=yes" in kwargs["env"]["GIT_SSH_COMMAND"]
    assert kwargs["timeout"] <= 5 and kwargs["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    "run",
    [
        fake_run(returncode=128),
        fake_run(raises=subprocess.TimeoutExpired(cmd="git", timeout=3)),
        fake_run(raises=FileNotFoundError("git")),
        fake_run(stdout="xyz\trefs/tags/nightly\n"),
    ],
)
def test_any_failure_reports_unknown_instead_of_raising(git_checkout, monkeypatch, run):
    monkeypatch.setattr(_release.subprocess, "run", run)
    assert _release.latest_release_tag(git_checkout) is None


def test_without_git_credentials_the_signed_in_github_cli_is_used_for_one_command(git_checkout, monkeypatch):
    # The private repository with only `gh auth login` done: plain git has no credentials.
    seen: list = []
    answers = iter([SimpleNamespace(returncode=128, stdout="", stderr="terminal prompts disabled"), None])

    def run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return next(answers) or SimpleNamespace(returncode=0, stdout=LS_REMOTE, stderr="")

    monkeypatch.setattr(_release.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    monkeypatch.setattr(_release.subprocess, "run", run)
    assert _release.latest_release_tag(git_checkout) == "v0.10.0"
    (first, _), (second, kwargs) = seen
    assert "credential.https://github.com.helper=" not in first
    assert second[3:7] == list(_release.GH_CREDENTIALS) and second[-5:] == first[-5:]
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0", "the retry still never prompts"


@pytest.mark.parametrize(
    "first,gh",
    [
        (fake_run(returncode=128), None),  # no GitHub CLI: nothing to borrow
        (fake_run(raises=subprocess.TimeoutExpired(cmd="git", timeout=3)), "/usr/bin/gh"),  # slow network: no retry
        (fake_run(LS_REMOTE), "/usr/bin/gh"),  # git's own credentials worked
    ],
)
def test_the_github_cli_is_tried_only_after_a_plain_failure(git_checkout, monkeypatch, first, gh):
    calls: list = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return first(cmd, **kwargs)

    monkeypatch.setattr(_release.shutil, "which", lambda name: gh)
    monkeypatch.setattr(_release.subprocess, "run", run)
    _release.latest_release_tag(git_checkout)
    assert len(calls) == 1


def test_disabled_or_not_a_checkout_never_runs_git(tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(_release.subprocess, "run", fake_run(LS_REMOTE, seen=seen))
    monkeypatch.setenv(_release.UPDATE_CHECK_VAR, "1")
    assert _release.latest_release_tag(tmp_path) is None  # no .git here
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(_release.UPDATE_CHECK_VAR, "0")
    assert _release.latest_release_tag(tmp_path) is None
    assert seen == []


def test_update_status_compares_versions_numerically(git_checkout, monkeypatch):
    monkeypatch.setattr(_release.subprocess, "run", fake_run(LS_REMOTE))
    monkeypatch.setattr(_release, "installed_version", lambda: "0.9.3")
    status = _release.update_status(git_checkout)
    assert status == {
        "installed": "0.9.3",
        "latest_release": "0.10.0",
        "update_available": True,
        "how_to_update": _release.UPDATE_HOW,
    }
    monkeypatch.setattr(_release, "installed_version", lambda: "0.10.0")
    assert _release.update_status(git_checkout) == {
        "installed": "0.10.0",
        "latest_release": "0.10.0",
        "update_available": False,
    }


def test_both_tries_share_one_bound(git_checkout, monkeypatch):
    # A slow plain failure leaves the GitHub CLI retry only the rest of the 3 s.
    clock = iter([100.0, 102.6])
    timeouts: list = []

    def run(cmd, **kwargs):
        timeouts.append(kwargs["timeout"])
        return SimpleNamespace(returncode=128, stdout="", stderr="")

    monkeypatch.setattr(_release.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(_release.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(_release.subprocess, "run", run)
    assert _release.latest_release_tag(git_checkout) is None
    # The first try failed after 2.6 s: 2.6 + 0.4 is the one 3 s bound.
    assert timeouts[0] == 3.0 and timeouts[1] == pytest.approx(0.4)


def test_no_retry_when_the_bound_is_spent(git_checkout, monkeypatch):
    clock = iter([100.0, 102.95])
    calls: list = []
    monkeypatch.setattr(_release.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(_release.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(_release.subprocess, "run", fake_run(returncode=128, seen=calls))
    assert _release.latest_release_tag(git_checkout) is None
    assert len(calls) == 1


# The daily update notice on every command's result.


@pytest.fixture
def daily(git_checkout, monkeypatch, tmp_path):
    """A checkout on 0.9.3 whose origin has v0.10.0; every git call is recorded."""
    monkeypatch.setenv("REYA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(_release, "installed_version", lambda: "0.9.3")
    calls: list = []
    monkeypatch.setattr(_release.subprocess, "run", fake_run(LS_REMOTE, seen=calls))
    return SimpleNamespace(root=git_checkout, calls=calls, cache=tmp_path / "state" / _release.UPDATE_CACHE_FILE)


NOTICE = {
    "installed": "0.9.3",
    "latest_release": "0.10.0",
    "update_available": True,
    "how_to_update": _release.UPDATE_HOW,
}


def test_the_notice_checks_once_a_day_and_caches_the_answer(daily, monkeypatch):
    assert _release.update_notice(daily.root) == NOTICE
    assert len(daily.calls) == 1
    cached = json.loads(daily.cache.read_text())
    assert cached["kit_root"] == str(daily.root) and cached["latest_release"] == "v0.10.0"
    assert stat.S_IMODE(daily.cache.stat().st_mode) == 0o600
    for _ in range(3):
        assert _release.update_notice(daily.root) == NOTICE
    assert len(daily.calls) == 1, "within a day the cache answers; no git, no network"

    later = time.time() + _release.UPDATE_CHECK_EVERY_S + 1
    monkeypatch.setattr(_release.time, "time", lambda: later)
    assert _release.update_notice(daily.root) == NOTICE
    assert len(daily.calls) == 2, "a day later it checks again"


def test_the_cache_belongs_to_one_checkout(daily, tmp_path):
    _release.update_notice(daily.root)
    other = tmp_path / "other-kit"
    (other / ".git").mkdir(parents=True)
    _release.update_notice(other)
    assert len(daily.calls) == 2


def test_a_failed_check_is_cached_too(daily, monkeypatch):
    monkeypatch.setattr(_release.subprocess, "run", fake_run(returncode=128, seen=daily.calls))
    monkeypatch.setattr(_release.shutil, "which", lambda name: None)
    assert _release.update_notice(daily.root) is None
    assert _release.update_notice(daily.root) is None
    assert len(daily.calls) == 1, "offline: one try a day, not one per command"


def test_up_to_date_gives_no_notice(daily, monkeypatch):
    monkeypatch.setattr(_release, "installed_version", lambda: "0.10.0")
    assert _release.update_notice(daily.root) is None


def test_without_refresh_only_the_cache_is_read(daily):
    assert _release.update_notice(daily.root, refresh=False) is None
    assert daily.calls == [] and not daily.cache.exists()
    _release.update_notice(daily.root)
    assert _release.update_notice(daily.root, refresh=False) == NOTICE
    assert len(daily.calls) == 1


def test_disabled_check_reads_and_writes_nothing(daily, monkeypatch):
    monkeypatch.setenv(_release.UPDATE_CHECK_VAR, "0")
    assert _release.update_notice(daily.root) is None
    assert daily.calls == [] and not daily.cache.parent.exists()


@pytest.mark.parametrize("cache", ["not json", "[]", '{"kit_root": 1}', '{"checked_at": "x", "kit_root": "."}'])
def test_a_damaged_cache_is_checked_again(daily, cache):
    daily.cache.parent.mkdir(mode=0o700)
    daily.cache.write_text(cache)
    assert _release.update_notice(daily.root) == NOTICE
    assert len(daily.calls) == 1


def test_a_cached_answer_from_the_future_is_not_trusted(daily):
    daily.cache.parent.mkdir(mode=0o700)
    daily.cache.write_text(
        json.dumps({"kit_root": str(daily.root), "checked_at": time.time() + 3600, "latest_release": "v9.9.9"})
    )
    assert _release.update_notice(daily.root) == NOTICE
    assert len(daily.calls) == 1


def test_any_failure_gives_no_notice_and_never_raises(daily, monkeypatch):
    monkeypatch.setenv("REYA_STATE_DIR", "relative/state")  # state_directory refuses it
    assert _release.update_notice(daily.root) is None
    monkeypatch.setattr(_release, "installed_version", lambda: None)
    assert _release.update_notice(daily.root) is None


def test_every_command_result_carries_the_notice(monkeypatch, capsys):
    seen: list = []

    def notice(root, refresh=True):
        seen.append(refresh)
        return NOTICE

    monkeypatch.setattr(_release, "update_notice", notice)
    _common.output_json({"order_id": "1"})
    assert json.loads(capsys.readouterr().out) == {"order_id": "1", "kit_update": NOTICE}
    _common.output_json([{"symbol": "ETHRUSDPERP"}])  # a list has no place for it
    assert json.loads(capsys.readouterr().out) == [{"symbol": "ETHRUSDPERP"}]
    _common.output_json({"kit": {"installed": "0.9.3"}})  # agent status reports the kit itself
    assert "kit_update" not in json.loads(capsys.readouterr().out)
    with _common.capture_output() as buffer:  # inside the runner: results stay as they are
        _common.output_json({"order_id": "2"})
    assert buffer == [{"order_id": "2"}] and seen == [True]


def test_a_command_result_is_unchanged_without_an_update(monkeypatch, capsys):
    monkeypatch.setattr(_release, "update_notice", lambda root, refresh=True: None)
    _common.output_json({"order_id": "1"})
    assert json.loads(capsys.readouterr().out) == {"order_id": "1"}
