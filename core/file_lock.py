"""
Cross-process exclusive file lock (v0.2.3.3).

Used to serialise Antigravity CLI builds across processes (dashboard thread vs. director.py CLI):
the builder rewrites the user-global ~/.gemini/antigravity-cli/settings.json for the duration of a run,
so two concurrent builds must never overlap.

Properties
- exclusive: the lock file is created atomically with O_CREAT | O_EXCL
- owner recorded: JSON {pid, token, created_at, note}
- released on normal exit, exception (context manager / try-finally) and interpreter exit (atexit)
- stale policy: a lock whose owner PID is ALIVE is never stale, whatever its age.
  Only a lock whose owner PID no longer exists (or whose metadata is unreadable for more than a few
  seconds) is a stale-recovery candidate.
- TOCTOU safe: before deleting a stale lock the file is re-read and its token compared with the one
  observed during the staleness check; if another process replaced the lock meanwhile, nothing is deleted.
"""
import os
import json
import time
import uuid
import atexit
from pathlib import Path
from typing import Optional

try:
    import psutil
except Exception:  # pragma: no cover - psutil is a hard dependency elsewhere in the project
    psutil = None


class LockBusy(Exception):
    """Raised when the lock is held by another live process."""


class FileLock:
    def __init__(self, path: Path, max_age_sec: float = 3600.0, note: str = ""):
        self.path = Path(path)
        # kept for API compatibility: age is informational only and never justifies deleting a live owner's lock
        self.max_age_sec = max_age_sec
        self.note = note
        self.acquired = False
        self.token: Optional[str] = None

    # ------------------------------------------------------------------ inspection
    def read_owner(self) -> Optional[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _owner_alive(self, owner: Optional[dict]) -> bool:
        if not owner:
            return False
        pid = owner.get("pid")
        if not isinstance(pid, int):
            return False
        if pid == os.getpid():
            return True
        if psutil is not None:
            try:
                return psutil.pid_exists(pid)
            except Exception:
                return True  # cannot tell: assume alive (never steal a lock we cannot inspect)
        return True

    def _age(self) -> float:
        try:
            return time.time() - self.path.stat().st_mtime
        except OSError:
            return 0.0

    def is_stale(self) -> bool:
        """True only when the recorded owner process is dead (or the metadata is unreadable for > 5 s)."""
        if not self.path.exists():
            return False
        owner = self.read_owner()
        if owner is None:
            return self._age() > 5.0
        return not self._owner_alive(owner)

    # ------------------------------------------------------------------ acquire / release
    def _try_recover_stale(self, observed: Optional[dict]) -> bool:
        """Deletes the lock only if it is still the exact stale lock we observed (same token)."""
        current = self.read_owner()
        if observed is not None:
            if current is None or current.get("token") != observed.get("token"):
                return False  # replaced by another process meanwhile: do not touch
            if self._owner_alive(current):
                return False  # owner came alive / different live owner: do not touch
        else:
            # unreadable metadata: only recover if it is still unreadable and old
            if current is not None or self._age() <= 5.0:
                return False
        try:
            self.path.unlink()
            return True
        except OSError:
            return False

    def try_acquire(self) -> bool:
        """Non-blocking. Returns True when acquired. Recovers a dead-owner stale lock once (token-checked)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (0, 1):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if attempt == 0:
                    observed = self.read_owner()
                    if self.is_stale() and self._try_recover_stale(observed):
                        continue
                return False
            self.token = uuid.uuid4().hex
            payload = {"pid": os.getpid(), "token": self.token, "created_at": time.time(), "note": self.note}
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload))
            self.acquired = True
            atexit.register(self.release)
            return True
        return False

    def acquire(self, timeout: float = 0.0, poll: float = 0.2) -> bool:
        deadline = time.time() + max(timeout, 0.0)
        while True:
            if self.try_acquire():
                return True
            if time.time() >= deadline:
                return False
            time.sleep(poll)

    def release(self) -> None:
        """Deletes the lock only if it still carries our token (never a lock someone else re-created)."""
        if not self.acquired:
            return
        owner = self.read_owner()
        try:
            if owner is not None and owner.get("token") == self.token and owner.get("pid") == os.getpid():
                self.path.unlink()
        except OSError:
            pass
        finally:
            self.acquired = False

    def __enter__(self):
        if not self.try_acquire():
            owner = self.read_owner() or {}
            raise LockBusy(f"lock {self.path.name} held by PID {owner.get('pid', '?')}")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
