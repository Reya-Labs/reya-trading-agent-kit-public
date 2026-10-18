"""Release/update check: best effort, bounded, never prompts, never fails a command.

Offline: `subprocess.run` is replaced, so no git process or network call happens.
"""

from types import SimpleNamespace

import subprocess

import pytest

from agent_kit.scripts import _release

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
