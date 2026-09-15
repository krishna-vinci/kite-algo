"""Process containment for a single hosted child.

This module owns everything about *how* a child runs, and nothing about *why*.
It is deliberately independent of the lifecycle API so it can be unit-tested
with harmless local programs.

Containment properties (v1, a trusted single-operator setup — **not** a claim of
complete security isolation):

- the child runs in its **own process group/session** (``start_new_session``) so
  termination targets the whole tree;
- an explicit environment allowlist is supplied by the caller — no inherited
  environment, no supervisor/DB/broker credentials;
- the child's working directory is a child-owned scratch dir; the source and the
  process records are supervisor-owned;
- resource limits (address space, CPU seconds, open files, processes, file size)
  are applied in the child before ``exec``;
- optional ``setgid``/``setuid`` drop to a distinct child OS identity when the
  supervisor is granted the privilege (no Docker socket, no broad host caps);
- logs are captured to a supervisor-owned file with a hard byte cap;
- a process is identified by boot id + container id + pid + pgid + **process
  start time**, so a recycled PID is never mistaken for the managed process.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

__all__ = [
    "ChildProcessHandle",
    "ProcessIdentity",
    "identity_is_current",
    "read_boot_id",
    "read_container_id",
    "read_process_start_time",
    "spawn_child",
    "terminate_identity",
]

_PROC_STAT_FIELD_STARTTIME = 22


def read_boot_id() -> Optional[str]:
    try:
        with open("/proc/sys/kernel/random/boot_id", "r", encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def read_container_id() -> Optional[str]:
    """Best-effort container identity (cgroup path / hostname)."""
    try:
        with open("/proc/self/cgroup", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.strip().split("/")
                for part in reversed(parts):
                    if len(part) == 64 and all(c in "0123456789abcdef" for c in part):
                        return part
    except OSError:
        pass
    try:
        return os.uname().nodename or None
    except Exception:  # pragma: no cover - non-POSIX
        return None


def read_process_start_time(pid: int) -> Optional[str]:
    """Field 22 of ``/proc/<pid>/stat`` (clock ticks since boot)."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            data = handle.read()
    except OSError:
        return None
    # comm is in parentheses and may contain spaces/parens; split on the last ')'.
    close = data.rfind(")")
    if close == -1:
        return None
    fields = data[close + 2 :].split()
    index = _PROC_STAT_FIELD_STARTTIME - 3  # 1=pid, 2=comm, so field 3 is fields[0]
    if index < 0 or index >= len(fields):
        return None
    return fields[index]


@dataclass(frozen=True)
class ProcessIdentity:
    boot_id: Optional[str]
    container_id: Optional[str]
    pid: int
    pgid: int
    start_time: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "boot_id": self.boot_id,
            "container_id": self.container_id,
            "pid": self.pid,
            "pgid": self.pgid,
            "start_time": self.start_time,
        }


def identity_is_current(identity: ProcessIdentity) -> bool:
    """True only if the *same* process is still alive (PID reuse is rejected)."""
    if identity.pid <= 0:
        return False
    if identity.boot_id is not None and read_boot_id() not in (None, identity.boot_id):
        return False
    if identity.start_time is not None:
        current_start = read_process_start_time(identity.pid)
        if current_start is None or current_start != identity.start_time:
            return False
    else:
        # Without a start time we cannot prove identity; require pid+pgid match.
        if read_process_start_time(identity.pid) is None:
            return False
    try:
        if os.getpgid(identity.pid) != identity.pgid:
            return False
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _default_rlimits() -> List[tuple]:
    import resource

    return [
        (resource.RLIMIT_AS, (1 << 31, 1 << 31)),       # 2 GiB address space
        (resource.RLIMIT_CPU, (3600, 3600)),             # 1 hour CPU
        (resource.RLIMIT_NOFILE, (256, 256)),
        (resource.RLIMIT_NPROC, (128, 128)),
        (resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024)),
    ]


class _BoundedLogReader(threading.Thread):
    """Drain a child's output to a file, capped at ``max_bytes``."""

    def __init__(self, stream, log_path: str, max_bytes: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._log_path = log_path
        self._max_bytes = max(1024, int(max_bytes))
        self._written = 0
        self.overflow = False

    def run(self) -> None:  # pragma: no cover - exercised via spawn tests
        try:
            with open(self._log_path, "ab", buffering=0) as handle:
                while True:
                    chunk = self._stream.read(4096)
                    if not chunk:
                        break
                    remaining = self._max_bytes - self._written
                    if remaining > 0:
                        handle.write(chunk[:remaining])
                        self._written += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        self.overflow = True
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._stream.close()
            except Exception:
                pass


class ChildProcessHandle:
    def __init__(
        self,
        popen: subprocess.Popen,
        identity: ProcessIdentity,
        *,
        log_path: str,
        reader: Optional[_BoundedLogReader],
        command: Sequence[str],
    ) -> None:
        self._popen = popen
        self.identity = identity
        self.log_path = log_path
        self._reader = reader
        self.command = list(command)
        self.started_monotonic = time.monotonic()

    @property
    def pid(self) -> int:
        return self.identity.pid

    @property
    def log_overflow(self) -> bool:
        return bool(self._reader is not None and self._reader.overflow)

    def poll(self) -> Optional[int]:
        return self._popen.poll()

    def wait(self, timeout: float) -> Optional[int]:
        try:
            return self._popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self, grace_seconds: float) -> str:
        """SIGTERM the group, bounded wait, then SIGKILL. No false 'stopped'.

        Returns ``terminated`` (exited after SIGTERM), ``killed`` (needed
        SIGKILL), ``exited`` (already gone), or ``identity_lost`` (the recorded
        process is no longer the one we started — we do NOT signal it).
        """
        if self._popen.poll() is not None:
            self._join_reader()
            return "exited"
        if not identity_is_current(self.identity):
            self._join_reader()
            return "identity_lost"
        try:
            os.killpg(self.identity.pgid, signal.SIGTERM)
        except ProcessLookupError:
            self._join_reader()
            return "exited"
        deadline = time.monotonic() + max(0.0, float(grace_seconds))
        while time.monotonic() < deadline:
            if self._popen.poll() is not None:
                self._join_reader()
                return "terminated"
            time.sleep(0.02)
        try:
            os.killpg(self.identity.pgid, signal.SIGKILL)
        except ProcessLookupError:
            self._join_reader()
            return "terminated"
        self._popen.wait(timeout=5)
        self._join_reader()
        return "killed"

    def _join_reader(self) -> None:
        if self._reader is not None:
            self._reader.join(timeout=2)


def spawn_child(
    command: Sequence[str],
    *,
    env: Dict[str, str],
    cwd: str,
    log_path: str,
    max_log_bytes: int = 5 * 1024 * 1024,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    rlimits: Optional[List[tuple]] = None,
    popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> ChildProcessHandle:
    """Spawn one child in its own session with an explicit environment.

    The environment is exactly ``env`` (the caller builds it from an allowlist);
    nothing is inherited. ``stdin`` is ``/dev/null`` and output is captured to a
    supervisor-owned, byte-capped log.
    """
    limits = _default_rlimits() if rlimits is None else rlimits

    def _preexec() -> None:  # pragma: no cover - runs in the forked child
        import resource

        # ``start_new_session=True`` already calls setsid(); do not call it again.
        for resource_id, value in limits:
            try:
                resource.setrlimit(resource_id, value)
            except (ValueError, OSError):
                pass
        if gid is not None:
            os.setgid(gid)
        if uid is not None:
            os.setuid(uid)

    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(os.devnull, "rb") as devnull:
        popen = popen_factory(
            list(command),
            env=dict(env),
            cwd=cwd,
            stdin=devnull,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
            preexec_fn=_preexec,
            close_fds=True,
        )
    reader = _BoundedLogReader(popen.stdout, log_path, max_log_bytes)
    reader.start()
    start_time = read_process_start_time(popen.pid)
    pgid = popen.pid
    try:
        pgid = os.getpgid(popen.pid)
    except ProcessLookupError:
        pass
    identity = ProcessIdentity(
        boot_id=read_boot_id(),
        container_id=read_container_id(),
        pid=popen.pid,
        pgid=pgid,
        start_time=start_time,
    )
    return ChildProcessHandle(popen, identity, log_path=log_path, reader=reader, command=command)


def terminate_identity(identity: ProcessIdentity, grace_seconds: float) -> str:
    """Terminate by identity when we do not own the ``Popen`` (crash recovery).

    Verifies the process is still the recorded one before signalling, so a
    recycled PID is never killed. Polls for exit rather than ``waitpid`` because
    the process may not be our child.
    """
    if not identity_is_current(identity):
        return "identity_lost"
    try:
        os.killpg(identity.pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "exited"
    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    while time.monotonic() < deadline:
        if not identity_is_current(identity):
            return "terminated"
        time.sleep(0.02)
    try:
        os.killpg(identity.pgid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except OSError as exc:  # pragma: no cover
        if exc.errno == errno.ESRCH:
            return "terminated"
        raise
    return "killed"
