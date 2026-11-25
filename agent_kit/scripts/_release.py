"""Which kit release is installed, and whether a newer one exists.

Best effort and bounded: one ``git ls-remote`` against this checkout's own
``origin`` with a short timeout, no credential prompts and no SSH passphrase
prompts. While the repository is private and git has no GitHub credentials of
its own, a second try borrows the signed-in GitHub CLI's for that one command,
the way ``gh`` does, without changing git configuration. Both tries share one
3 s bound. Any failure (offline, no access, not a git checkout, check disabled
with ``REYA_KIT_UPDATE_CHECK=0``) reports ``latest_release: null`` and never
fails the calling command. Nothing is downloaded or changed here; updating is
``./install.sh --release=latest``, run only when the user asks for it.

``agent status`` / ``agent init`` check every time. Every other command's result
carries ``kit_update`` when a newer release exists (``update_notice``); that check
runs at most once a day and its answer is cached in the state directory, so on
other days it costs one small file read.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from agent_kit.scripts._writer import state_directory

PACKAGE = "reya-trading-agent-kit"
UPDATE_CHECK_VAR = "REYA_KIT_UPDATE_CHECK"
UPDATE_CHECK_TIMEOUT_S = 3.0
# The daily check's answer, in the state directory (REYA_STATE_DIR).
UPDATE_CACHE_FILE = "kit-update-check.json"
UPDATE_CHECK_EVERY_S = 24 * 60 * 60
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# GitHub credentials from the signed-in GitHub CLI, for one git command (an empty value
# first clears any other helper for github.com, as `gh` does itself).
GH_CREDENTIALS = (
    "-c",
    "credential.https://github.com.helper=",
    "-c",
    "credential.https://github.com.helper=!gh auth git-credential",
)
UPDATE_HOW = "Ask your assistant to update the Reya agent kit; it runs ./install.sh --release=latest in the kit folder."


def installed_version() -> str | None:
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return None


def _parse(tag: str) -> tuple[int, int, int] | None:
    match = _RELEASE_TAG.match(tag)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _ls_remote(kit_root: Path, timeout: float, config: tuple[str, ...] = ()) -> subprocess.CompletedProcess | None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
    try:
        return subprocess.run(
            ["git", "-C", str(kit_root), *config, "ls-remote", "--tags", "--refs", "origin", "v*"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _check_disabled() -> bool:
    return os.environ.get(UPDATE_CHECK_VAR, "1").strip() == "0"


def latest_release_tag(kit_root: Path, timeout: float = UPDATE_CHECK_TIMEOUT_S) -> str | None:
    """Highest vX.Y.Z tag on the checkout's origin, or None when it cannot be determined.

    `timeout` bounds both tries together: the GitHub CLI retry gets only what the first left.
    """
    if _check_disabled():
        return None
    if not (kit_root / ".git").exists():
        return None
    deadline = time.monotonic() + timeout
    completed = _ls_remote(kit_root, timeout)
    if completed is not None and completed.returncode != 0 and shutil.which("gh"):
        remaining = deadline - time.monotonic()
        completed = _ls_remote(kit_root, remaining, GH_CREDENTIALS) if remaining > 0.1 else None
    if completed is None or completed.returncode != 0:
        return None
    tags = [line.rsplit("refs/tags/", 1)[-1] for line in completed.stdout.splitlines() if "refs/tags/" in line]
    releases = [(parsed, tag) for tag in tags if (parsed := _parse(tag)) is not None]
    return max(releases)[1] if releases else None


def update_status(kit_root: Path) -> dict:
    installed = installed_version()
    latest = latest_release_tag(kit_root)
    available = False
    if installed and latest:
        current, newest = _parse(f"v{installed}"), _parse(latest)
        available = bool(current and newest and newest > current)
    return {
        "installed": installed,
        "latest_release": latest[1:] if latest else None,
        "update_available": available,
        **({"how_to_update": UPDATE_HOW} if available else {}),
    }


def _cached_latest_release(kit_root: Path, refresh: bool) -> str | None:
    """The latest release tag from the daily check: cached, or checked now when a day has passed.

    The cache is per checkout (`kit_root`). A failed check is cached too, so an offline
    machine tries again the next day, not on every command. Without `refresh` this only
    reads the cache.
    """
    path = state_directory() / UPDATE_CACHE_FILE
    now = time.time()
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("kit_root") == str(kit_root) and 0 <= now - float(cached["checked_at"]) < UPDATE_CHECK_EVERY_S:
            latest = cached.get("latest_release")
            return latest if isinstance(latest, str) and _parse(latest) else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    if not refresh:
        return None
    latest = latest_release_tag(kit_root)
    temporary = path.with_name(f".{UPDATE_CACHE_FILE}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"kit_root": str(kit_root), "checked_at": now, "latest_release": latest}, handle)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
    return latest


def update_notice(kit_root: Path, refresh: bool = True) -> dict | None:
    """`kit_update` for a command's result when a newer release exists, else None.

    Never raises and never takes longer than one bounded check (at most once a day).
    `refresh=False` only reads the last answer, for callers that must not wait at all.
    """
    if _check_disabled():
        return None
    try:
        installed = installed_version()
        current = _parse(f"v{installed}") if installed else None
        if current is None:
            return None
        latest = _cached_latest_release(kit_root, refresh)
    except Exception:
        return None
    newest = _parse(latest) if latest else None
    if latest is None or newest is None or newest <= current:
        return None
    return {"installed": installed, "latest_release": latest[1:], "update_available": True, "how_to_update": UPDATE_HOW}
