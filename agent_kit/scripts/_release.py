"""Which kit release is installed, and whether a newer one exists.

Best effort and bounded: one ``git ls-remote`` against this checkout's own
``origin`` with a short timeout, no credential prompts and no SSH passphrase
prompts. While the repository is private and git has no GitHub credentials of
its own, a second try borrows the signed-in GitHub CLI's for that one command,
the way ``gh`` does, without changing git configuration. Any failure (offline, no access, not a git checkout, check disabled
with ``REYA_KIT_UPDATE_CHECK=0``) reports ``latest_release: null`` and never
fails the calling command. Nothing is downloaded or changed here; updating is
``./install.sh --release=latest``, run only when the user asks for it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PACKAGE = "reya-trading-agent-kit"
UPDATE_CHECK_VAR = "REYA_KIT_UPDATE_CHECK"
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


def latest_release_tag(kit_root: Path, timeout: float = 3.0) -> str | None:
    """Highest vX.Y.Z tag on the checkout's origin, or None when it cannot be determined."""
    if os.environ.get(UPDATE_CHECK_VAR, "1").strip() == "0":
        return None
    if not (kit_root / ".git").exists():
        return None
    completed = _ls_remote(kit_root, timeout)
    if completed is not None and completed.returncode != 0 and shutil.which("gh"):
        completed = _ls_remote(kit_root, timeout, GH_CREDENTIALS)
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
