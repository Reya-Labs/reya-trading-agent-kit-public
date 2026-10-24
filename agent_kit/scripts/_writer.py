"""Cooperative host-local writer leases and durable monotonic trading nonces.

Every kit checkout uses the same state directory. External bots/other hosts must
participate in this coordination or use independently verified signing contexts.
"""

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path

from sdk.reya_rest_api import ReyaTradingClient


def state_directory() -> Path:
    path = Path(os.environ.get("REYA_STATE_DIR", str(Path.home() / ".local/state/reya-agent-kit")))
    if not path.is_absolute():
        raise ValueError("REYA_STATE_DIR must be absolute")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("REYA_STATE_DIR must be an owner-only directory (0700), not a symlink")
    return path


def _open_private(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise ValueError("Writer state must be a regular owner-only file (0600)")
    return fd


class WriterLease:
    def __init__(self, resources: list[str]):
        self.keys = list(dict.fromkeys(hashlib.sha256(r.encode()).hexdigest() for r in resources))
        self.fds: list[int] = []
        self.states: dict[str, dict] = {}
        self.directory: Path | None = None

    def __enter__(self):
        self.directory = state_directory()
        try:
            for key in sorted(self.keys):
                fd = _open_private(self.directory / (key + ".lock"), os.O_CREAT | os.O_RDWR)
                self.fds.append(fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError(
                        "Another kit writer holds this owner/signer lease; use the active runner"
                    ) from None
                path = self.directory / (key + ".json")
                try:
                    with os.fdopen(_open_private(path, os.O_RDONLY), "r") as stream:
                        state = json.load(stream)
                except FileNotFoundError:
                    state = {}
                if not isinstance(state, dict) or type(state.get("last_nonce", 0)) is not int:
                    raise ValueError("Invalid writer state; reconcile it before writing")
                self.states[key] = state
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    @property
    def state(self) -> dict:
        return self.states[self.keys[0]]

    def save(self) -> None:
        if self.directory is None or not self.fds:
            raise RuntimeError("Writer lease is not held")
        for key, state in self.states.items():
            fd, filename = tempfile.mkstemp(dir=self.directory, prefix=".state-")
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(state, stream, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(filename, self.directory / (key + ".json"))
            finally:
                if os.path.exists(filename):
                    os.unlink(filename)
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def next_nonce(self) -> int:
        nonce = max(int(time.time() * 1_000_000), max(s.get("last_nonce", 0) for s in self.states.values()) + 1)
        for state in self.states.values():
            state["last_nonce"] = nonce
        self.save()  # Persist before signing/submitting, even if the request later fails.
        return nonce

    def __exit__(self, exc_type, exc_value, traceback):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds = []


def trading_resources(config, signer: str) -> list[str]:
    # Lock both: same owner with different delegates AND same delegate with different owners.
    return [f"trade:{config.chain_id}:{address.lower()}" for address in (config.owner_wallet_address, signer)]


class CoordinatedTradingClient(ReyaTradingClient):
    """The pinned SDK's nonce seam is covered by real-signature regression tests."""

    async def __aenter__(self):
        self.lease = WriterLease(trading_resources(self.config, self.signer_wallet_address))
        try:
            self.lease.__enter__()
        except BaseException:
            await super().close()
            raise
        return self

    def _get_next_nonce(self) -> int:
        return self.lease.next_nonce()

    async def __aexit__(self, exc_type, exc_value, traceback):
        try:
            await super().close()
        finally:
            self.lease.__exit__(exc_type, exc_value, traceback)
