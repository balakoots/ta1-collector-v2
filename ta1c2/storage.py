"""Evidence storage backends.

Contract (both backends):
  * lock()            exclusive section for one run (local file lock; git also relies on push CAS)
  * read(path)        bytes or None
  * commit(files, new_ledger, expected_ledger_sha, msg)
        - compare-and-swap: refuses if the stored ledger hash != expected (concurrent writer)
        - append-only: refuses unless new_ledger starts with the stored ledger bytes
        - artifacts are create-only (an existing path with different bytes is a conflict)
        - artifacts are written before the ledger; a crash in between leaves only unreferenced
          files (reported as orphans by `verify`), never a ledger row without its evidence
  * readback(paths)   fresh read of what was persisted -> {path: sha256 or None}
GitBackend: one atomic commit per run; `git push` (never forced) is the cross-runner CAS;
readback is from the remote after `git fetch`. Server-side branch protection (no force-push,
no deletion) is an operational requirement documented in ARCHITECTURE.md.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import time
from pathlib import Path

from .core import sha256
from .ledger import LEDGER_PATH


class PersistenceError(Exception):
    pass


class ConcurrentModification(PersistenceError):
    pass


class AppendOnlyViolation(PersistenceError):
    pass


class ArtifactConflict(PersistenceError):
    pass


class SimulatedCrash(Exception):
    """Test-only: process death between artifact and ledger writes."""


def _fsync_dir(d: Path):
    fd = os.open(str(d), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_rel(path: str) -> str:
    p = Path(path)
    if p.is_absolute() or ".." in p.parts:
        raise PersistenceError("unsafe artifact path " + path)
    return str(p)


class DirBackend:
    name = "dir"

    def __init__(self, root, lock_timeout_s: float = 300):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_timeout_s = lock_timeout_s

    @contextlib.contextmanager
    def lock(self):
        with open(self.root / ".lock", "a+") as fh:
            deadline = time.monotonic() + self.lock_timeout_s
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise PersistenceError("storage lock timeout")
                    time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def read(self, path: str) -> bytes | None:
        p = self.root / _safe_rel(path)
        return p.read_bytes() if p.exists() else None

    def _write_create_only(self, rel: str, data: bytes):
        final = self.root / _safe_rel(rel)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(final.name + f".tmp{os.getpid()}")
        with open(tmp, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        try:
            os.link(tmp, final)
        except FileExistsError:
            if final.read_bytes() != data:
                raise ArtifactConflict("artifact exists with different bytes: " + rel)
        finally:
            tmp.unlink(missing_ok=True)
        _fsync_dir(final.parent)

    def _check_cas(self, new_ledger: bytes, expected_ledger_sha: str):
        current = self.read(LEDGER_PATH) or b""
        if sha256(current) != expected_ledger_sha:
            raise ConcurrentModification("ledger changed since it was read")
        if not new_ledger.startswith(current):
            raise AppendOnlyViolation("new ledger does not extend stored ledger")

    def commit(self, files: dict[str, bytes], new_ledger: bytes, expected_ledger_sha: str, msg: str = "") -> dict:
        self._check_cas(new_ledger, expected_ledger_sha)
        for rel, data in files.items():
            self._write_create_only(rel, data)
        self._after_artifacts()
        led = self.root / LEDGER_PATH
        led.parent.mkdir(parents=True, exist_ok=True)
        tmp = led.with_name("ledger.jsonl.tmp")
        with open(tmp, "wb") as f:
            f.write(new_ledger); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, led)
        _fsync_dir(led.parent)
        return {"backend": "dir", "ledger_sha256": sha256(new_ledger)}

    def _after_artifacts(self):  # hook for fault injection
        pass

    def readback(self, paths) -> dict:
        return {p: (sha256(b) if (b := self.read(p)) is not None else None) for p in paths}

    def list_files(self) -> list[str]:
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file() and p.name != ".lock" and ".tmp" not in p.name)


class GitBackend:
    name = "git"

    def __init__(self, workdir, branch: str = "evidence", remote: str = "origin"):
        self.wd, self.branch, self.remote = Path(workdir), branch, remote

    def _git(self, *args, check=True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.wd), "-c", "user.name=ta1-collector-v2", "-c", "user.email=ta1-collector-v2@invalid", *args],
                              capture_output=True, check=check)

    @contextlib.contextmanager
    def lock(self):
        with open(self.wd / ".git" / "ta1c2.lock", "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                self._sync()
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _sync(self):
        self._git("fetch", "--quiet", self.remote, self.branch)
        self._git("checkout", "--quiet", "-B", self.branch, f"{self.remote}/{self.branch}")
        self._git("reset", "--quiet", "--hard", f"{self.remote}/{self.branch}")
        self._git("clean", "-qfdx")

    def read(self, path: str) -> bytes | None:
        p = self.wd / _safe_rel(path)
        return p.read_bytes() if p.exists() else None

    def commit(self, files: dict[str, bytes], new_ledger: bytes, expected_ledger_sha: str, msg: str = "") -> dict:
        current = self.read(LEDGER_PATH) or b""
        if sha256(current) != expected_ledger_sha:
            raise ConcurrentModification("ledger changed since it was read")
        if not new_ledger.startswith(current):
            raise AppendOnlyViolation("new ledger does not extend stored ledger")
        try:
            for rel, data in files.items():
                p = self.wd / _safe_rel(rel)
                if p.exists():
                    raise ArtifactConflict("artifact path already exists: " + rel)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            led = self.wd / LEDGER_PATH
            led.parent.mkdir(parents=True, exist_ok=True)
            led.write_bytes(new_ledger)
            self._git("add", "-A")
            self._git("commit", "--quiet", "-m", msg or "ta1c2 run")
            push = self._git("push", "--quiet", self.remote, f"HEAD:refs/heads/{self.branch}", check=False)
            if push.returncode != 0:
                raise ConcurrentModification("push rejected: " + push.stderr.decode(errors="replace").strip()[:300])
            head = self._git("rev-parse", "HEAD").stdout.decode().strip()
            return {"backend": "git", "commit": head, "ledger_sha256": sha256(new_ledger)}
        except Exception:
            self._sync()  # discard local partial state; nothing unpushed survives
            raise

    def readback(self, paths) -> dict:
        self._git("fetch", "--quiet", self.remote, self.branch)
        out = {}
        for p in paths:
            r = self._git("show", f"{self.remote}/{self.branch}:{_safe_rel(p)}", check=False)
            out[p] = sha256(r.stdout) if r.returncode == 0 else None
        return out

    def list_files(self) -> list[str]:
        r = self._git("ls-tree", "-r", "--name-only", f"{self.remote}/{self.branch}")
        return sorted(x for x in r.stdout.decode().splitlines() if x)


class FaultyBackend:
    """Test-only wrapper injecting persistence faults into a DirBackend."""

    def __init__(self, inner: DirBackend, fault: str):
        self.inner, self.fault, self.name = inner, fault, "faulty-" + inner.name

    def lock(self):
        return self.inner.lock()

    def read(self, path):
        return self.inner.read(path)

    def list_files(self):
        return self.inner.list_files()

    def commit(self, files, new_ledger, expected, msg=""):
        if self.fault == "COMMIT_OSERROR":
            raise OSError(28, "No space left on device (simulated)")
        if self.fault == "CRASH_AFTER_ARTIFACTS":
            def boom():
                raise SimulatedCrash("process died after artifacts, before ledger")
            self.inner._after_artifacts = boom
            try:
                return self.inner.commit(files, new_ledger, expected, msg)
            finally:
                self.inner._after_artifacts = lambda: None
        if self.fault == "LOST_WRITE":
            dropped = sorted(files)[-1]
            rec = self.inner.commit({k: v for k, v in files.items() if k != dropped}, new_ledger, expected, msg)
            return rec  # claims success although one artifact never reached storage
        rec = self.inner.commit(files, new_ledger, expected, msg)
        if self.fault == "BITROT_AFTER_COMMIT":
            victim = self.inner.root / sorted(k for k in files if k.endswith(".raw"))[0]
            b = bytearray(victim.read_bytes()); b[len(b) // 2] ^= 0xFF; victim.write_bytes(bytes(b))
        return rec

    def readback(self, paths):
        return self.inner.readback(paths)
