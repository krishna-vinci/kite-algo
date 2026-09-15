"""Hosted-strategy process supervisor (dedicated runner).

The supervisor owns *process* lifecycle only. It holds **no** database
credentials and reaches the control plane exclusively through the
credential-authenticated lifecycle API (:mod:`backend.strategies.supervisor_api`).
It claims a job, asks the API to prepare the one-time child credential and to
deliver the pinned source, hash-verifies and writes the source to
supervisor-owned storage, spawns exactly one child in its own session with an
explicit environment, heartbeats the lease, watches child-reported progress, and
terminates / fences / recovers as required.

Design rules (v1, trusted single-operator):

- **No reattach / no replay.** A ``running`` job found after a supervisor restart
  is left as ``recovery_required`` by the control plane's reconciliation; the
  supervisor never adopts or restarts a child it did not just spawn.
- **Prepare once.** A lost/timed-out prepare response is *not* retried; the
  supervisor fails its own attempt closed (fence while the lease is live,
  recover once it has expired) and moves on.
- **Progress is the child's.** The heartbeat never writes progress; only accepted
  child progress updates ``last_progress_at``.
- **No false stop.** If the API is unreachable while cleaning up, the local
  attempt record is left ``cleanup_required`` and retryable; the supervisor never
  reports a clean stop it could not confirm.
- Normal script completion is **not** proof of flatness: exit triggers a
  runner-owned release, which leaves a launched attempt in ``recovery_required``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.strategies import supervisor_process as proc
from backend.strategies.supervisor_api import (
    LifecycleApiClient,
    SupervisorApiError,
    SupervisorTransportError,
)

logger = logging.getLogger(__name__)

__all__ = ["HostedSupervisor", "SupervisorConfig", "AttemptRecord", "main"]

# Child environment allowlist (kept in sync with kite_algo_worker.hosted).
ENV_BASE_URL = "KITE_ALGO_BASE_URL"
ENV_WORKER_TOKEN = "KITE_ALGO_WORKER_TOKEN"
ENV_RUN_ID = "KITE_ALGO_RUN_ID"
ENV_SESSION_NONCE = "KITE_ALGO_SESSION_NONCE"
ENV_TEMPLATE_ID = "KITE_ALGO_TEMPLATE_ID"
ENV_ACCOUNT_SCOPE = "KITE_ALGO_ACCOUNT_SCOPE"
ENV_MODE = "KITE_ALGO_MODE"
ENV_PARAMS = "KITE_ALGO_PARAMS"
ENV_SCRATCH = "KITE_ALGO_SCRATCH"

_DEFAULT_SDK_PATH = str(Path(__file__).resolve().parents[2] / "sdk" / "python")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SupervisorConfig:
    base_url: str
    credential: str
    workspace_root: str = "supervisor-workspace"
    lease_owner: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}")
    concurrency: int = 1
    lease_seconds: float = 120.0
    heartbeat_interval_s: float = 30.0
    startup_grace_s: float = 30.0
    progress_poll_s: float = 5.0
    term_grace_s: float = 10.0
    max_log_bytes: int = 5 * 1024 * 1024
    child_python: str = sys.executable
    child_pythonpath: str = _DEFAULT_SDK_PATH
    child_uid: Optional[int] = None
    child_gid: Optional[int] = None
    api_timeout_s: float = 10.0
    #: Bound on consecutive failed progress observations before failing closed
    #: (we must not renew the parent heartbeat forever while progress is unseen).
    progress_observation_max_failures: int = 3
    #: When true, startup fails unless child uid+gid are configured (identity
    #: separation is required, e.g. in the container).
    require_identity_separation: bool = False
    #: Test-only bound on how long to observe a child before treating it as hung.
    observe_timeout_s: Optional[float] = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Reject configurations that cannot be honoured safely."""
        problems: List[str] = []
        if self.lease_seconds <= 0:
            problems.append("lease_seconds must be > 0")
        if self.heartbeat_interval_s <= 0:
            problems.append("heartbeat_interval_s must be > 0")
        # The lease must comfortably outlive several heartbeat intervals.
        if self.heartbeat_interval_s > self.lease_seconds * 0.5:
            problems.append("heartbeat_interval_s must be <= half of lease_seconds")
        if self.progress_poll_s <= 0:
            problems.append("progress_poll_s must be > 0")
        if self.term_grace_s < 0:
            problems.append("term_grace_s must be >= 0")
        if self.startup_grace_s < 0:
            problems.append("startup_grace_s must be >= 0")
        if self.max_log_bytes < 1024:
            problems.append("max_log_bytes must be >= 1024")
        if self.concurrency < 1:
            problems.append("concurrency must be >= 1")
        if self.progress_observation_max_failures < 1:
            problems.append("progress_observation_max_failures must be >= 1")
        if (self.child_uid is None) != (self.child_gid is None):
            problems.append("child_uid and child_gid must be set together")
        if self.require_identity_separation and (self.child_uid is None or self.child_gid is None):
            problems.append(
                "identity separation is required but child_uid/child_gid are not configured"
            )
        if problems:
            raise ValueError("invalid supervisor configuration: " + "; ".join(problems))

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "SupervisorConfig":
        env = os.environ if environ is None else environ
        base_url = env.get("HOSTED_SUPERVISOR_BASE_URL")
        credential = env.get("HOSTED_SUPERVISOR_CREDENTIAL")
        if not base_url or not credential:
            raise RuntimeError(
                "HOSTED_SUPERVISOR_BASE_URL and HOSTED_SUPERVISOR_CREDENTIAL are required"
            )
        def _f(name: str, default: float) -> float:
            value = env.get(name)
            return float(value) if value else default

        def _i(name: str) -> Optional[int]:
            value = env.get(name)
            return int(value) if value else None

        def _b(name: str, default: bool = False) -> bool:
            value = env.get(name)
            if value is None:
                return default
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            base_url=base_url,
            credential=credential,
            workspace_root=env.get("HOSTED_SUPERVISOR_WORKSPACE", "supervisor-workspace"),
            lease_owner=env.get("HOSTED_SUPERVISOR_LEASE_OWNER") or f"{socket.gethostname()}:{os.getpid()}",
            lease_seconds=_f("HOSTED_SUPERVISOR_LEASE_SECONDS", 120.0),
            heartbeat_interval_s=_f("HOSTED_SUPERVISOR_HEARTBEAT_INTERVAL_S", 30.0),
            startup_grace_s=_f("HOSTED_SUPERVISOR_STARTUP_GRACE_S", 30.0),
            progress_poll_s=_f("HOSTED_SUPERVISOR_PROGRESS_POLL_S", 5.0),
            term_grace_s=_f("HOSTED_SUPERVISOR_TERM_GRACE_S", 10.0),
            max_log_bytes=int(_f("HOSTED_SUPERVISOR_MAX_LOG_BYTES", 5 * 1024 * 1024)),
            child_python=env.get("HOSTED_SUPERVISOR_CHILD_PYTHON", sys.executable),
            child_pythonpath=env.get("HOSTED_SUPERVISOR_CHILD_PYTHONPATH", _DEFAULT_SDK_PATH),
            child_uid=_i("HOSTED_SUPERVISOR_CHILD_UID"),
            child_gid=_i("HOSTED_SUPERVISOR_CHILD_GID"),
            api_timeout_s=_f("HOSTED_SUPERVISOR_API_TIMEOUT_S", 10.0),
            progress_observation_max_failures=int(_f("HOSTED_SUPERVISOR_PROGRESS_OBS_MAX_FAILURES", 3)),
            require_identity_separation=_b("HOSTED_SUPERVISOR_REQUIRE_IDENTITY_SEPARATION", False),
        )

    def workspace(self) -> Path:
        return Path(self.workspace_root)


@dataclass
class AttemptRecord:
    """Nonsecret local attempt identity, persisted before it is needed.

    Never contains the child token, the session nonce or the supervisor
    credential. It exists so a crash between steps is visible and cleanup is
    retryable.
    """

    job_id: str
    strategy_id: Optional[str] = None
    attempt: Optional[int] = None
    lease_owner: Optional[str] = None
    lease_epoch: Optional[int] = None
    run_id: Optional[str] = None
    version_id: Optional[str] = None
    source_sha256: Optional[str] = None
    phase: str = "claimed"
    identity: Optional[Dict[str, Any]] = None
    log_ref: Optional[str] = None
    error: Optional[str] = None
    updated_at: str = field(default_factory=lambda: _utcnow().isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


class HostedSupervisor:
    def __init__(
        self,
        config: SupervisorConfig,
        *,
        api: Optional[LifecycleApiClient] = None,
        spawn: Callable[..., Any] = proc.spawn_child,
        terminate_identity: Callable[..., str] = proc.terminate_identity,
        identity_is_current: Callable[..., bool] = proc.identity_is_current,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.api = api or LifecycleApiClient(
            config.base_url, config.credential, timeout=config.api_timeout_s
        )
        self._spawn = spawn
        self._terminate_identity = terminate_identity
        self._identity_is_current = identity_is_current
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._workspace = config.workspace()
        self._active: Dict[str, Any] = {}
        self._stopping = False
        self._prepare_workspace()

    def _prepare_workspace(self) -> None:
        """Create the supervisor-owned layout with child-safe permissions.

        - workspace root: ``0711`` (traversable, not listable)
        - ``attempts``: ``0700`` — authoritative records, child-inaccessible
        - ``logs``: ``0700``
        - ``source``: ``0755`` dirs (child can traverse/read), files ``0444``
        - ``scratch``: per-job, chowned to the child uid/gid when configured
        """
        root = self._workspace
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o711)
        for name, mode in (("attempts", 0o700), ("logs", 0o700), ("source", 0o755), ("scratch", 0o755)):
            directory = root / name
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, mode)

    # -- paths --------------------------------------------------------------

    def _attempt_path(self, job_id: str) -> Path:
        return self._workspace / "attempts" / f"{job_id}.json"

    def _log_path(self, job_id: str) -> Path:
        return self._workspace / "logs" / f"{job_id}.log"

    def _scratch_path(self, job_id: str) -> Path:
        return self._workspace / "scratch" / job_id

    def _source_path(self, job_id: str, version_id: str) -> Path:
        return self._workspace / "source" / job_id / f"{version_id}.py"

    def _persist(self, record: AttemptRecord) -> None:
        record.updated_at = self._wall_clock().isoformat()
        path = self._attempt_path(record.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(record.to_json(), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _load_records(self) -> List[AttemptRecord]:
        directory = self._workspace / "attempts"
        if not directory.is_dir():
            return []
        records: List[AttemptRecord] = []
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                records.append(AttemptRecord(**payload))
            except Exception:  # pragma: no cover - corrupt record is skipped
                logger.warning("supervisor_attempt_record_unreadable", extra={"path": str(path)})
        return records

    # -- discovery ----------------------------------------------------------

    def discover(self) -> List[Dict[str, Any]]:
        return self.api.list_jobs(status="queued", limit=50)

    # -- cleanup helper -----------------------------------------------------

    _TERMINAL_STATUSES = {"recovery_required", "stopped", "failed"}

    def _state_or_none(self, job_id: str, epoch: int, attempt: int) -> Optional[Dict[str, Any]]:
        try:
            return self.api.job_state(
                job_id, lease_owner=self.config.lease_owner, lease_epoch=epoch, attempt=attempt
            )
        except (SupervisorApiError, SupervisorTransportError):
            return None

    def _report_process_cleanup(self, job_id: str, epoch: int, attempt: int, state: str) -> bool:
        """Best-effort attempt-bound cleanup evidence report (never raises)."""
        try:
            self.api.process_cleanup(
                job_id,
                lease_owner=self.config.lease_owner,
                lease_epoch=epoch,
                attempt=attempt,
                state=state,
            )
            return True
        except Exception as exc:  # best effort; local control flow is unaffected
            logger.warning(
                "hosted_supervisor_process_cleanup_report_failed",
                extra={"job_id": job_id, "state": state, "error": type(exc).__name__},
            )
            return False

    def _authority_check(self, job_id: str, epoch: int, attempt: int) -> Dict[str, Any]:
        """Full pre-spawn authority check: state, desired state, lease, attempt.

        Returns ``{"ok": bool, "reason": str|None, "state": dict|None}``. This
        **fails closed**: any required authority field that is missing,
        unreadable or mismatched refuses spawning.
        """
        state = self._state_or_none(job_id, epoch, attempt)
        if state is None:
            return {"ok": False, "reason": "HOSTED_AUTHORITY_UNREADABLE", "state": None}
        if str(state.get("status") or "") not in {"starting", "running"}:
            return {"ok": False, "reason": "HOSTED_ATTEMPT_FENCED", "state": state}
        if str(state.get("desired_state") or "") != "started":
            return {"ok": False, "reason": "HOSTED_ATTEMPT_STOPPED", "state": state}

        epoch_field = state.get("lease_epoch")
        attempt_field = state.get("attempt")
        if epoch_field is None or attempt_field is None:
            # Missing epoch/attempt cannot prove authority: refuse.
            return {"ok": False, "reason": "HOSTED_AUTHORITY_INCOMPLETE", "state": state}
        try:
            if int(epoch_field) != int(epoch) or int(attempt_field) != int(attempt):
                return {"ok": False, "reason": "HOSTED_LEASE_AUTHORITY_MISMATCH", "state": state}
        except (TypeError, ValueError):
            return {"ok": False, "reason": "HOSTED_AUTHORITY_INCOMPLETE", "state": state}

        lease_until = state.get("lease_until")
        if not lease_until:
            # A missing/unreadable lease cannot prove a live lease: refuse.
            return {"ok": False, "reason": "HOSTED_LEASE_UNREADABLE", "state": state}
        try:
            if datetime.fromisoformat(str(lease_until).replace("Z", "+00:00")) <= self._wall_clock():
                return {"ok": False, "reason": "HOSTED_LEASE_EXPIRED", "state": state}
        except ValueError:
            return {"ok": False, "reason": "HOSTED_LEASE_UNREADABLE", "state": state}
        return {"ok": True, "reason": None, "state": state}

    def _fail_closed(
        self, job_id: str, epoch: int, attempt: int, *, reason: str
    ) -> Dict[str, Any]:
        """Fence while the lease is live, recover once expired, then confirm.

        Cleanup is considered resolved **only** when the API confirms it with a
        2xx response or an **authenticated state read** proves a terminal
        condition. An error code such as ``HOSTED_ATTEMPT_FENCED``/``..STOPPED``
        is *not* treated as proof — the state read is required. Any 403/409/5xx
        or transport error, and a still-live lease, leaves ``cleanup_required``
        so the caller keeps the retry record and never claims success.
        """
        report: Dict[str, Any] = {
            "fence": "not_attempted",
            "recover": "not_attempted",
            "terminal_confirmed": False,
            "cleanup_required": True,
        }

        def _try_fence() -> str:
            try:
                self.api.fence(
                    job_id, lease_owner=self.config.lease_owner, lease_epoch=epoch, attempt=attempt, reason=reason
                )
                return "ok"
            except SupervisorApiError as exc:
                return exc.reason or f"http_{exc.status_code}"
            except SupervisorTransportError:
                return "transport_error"

        def _try_recover() -> str:
            try:
                self.api.recover(
                    job_id, lease_owner=self.config.lease_owner, lease_epoch=epoch, attempt=attempt, reason=reason
                )
                return "ok"
            except SupervisorApiError as exc:
                return exc.reason or f"http_{exc.status_code}"
            except SupervisorTransportError:
                return "transport_error"

        outcome = _try_fence()
        report["fence"] = outcome
        if outcome == "ok":
            report["cleanup_required"] = False
            report["terminal_confirmed"] = True
            return report
        if outcome in {"HOSTED_LEASE_EXPIRED", "HOSTED_LEASE_STILL_LIVE"}:
            recover_outcome = _try_recover()
            report["recover"] = recover_outcome
            if recover_outcome == "ok":
                report["cleanup_required"] = False
                report["terminal_confirmed"] = True
                return report

        # Everything else (including HOSTED_ATTEMPT_FENCED/STOPPED) must be
        # confirmed by an authenticated state read.
        state = self._state_or_none(job_id, epoch, attempt)
        if state is not None and str(state.get("status") or "") in self._TERMINAL_STATUSES:
            report["cleanup_required"] = False
            report["terminal_confirmed"] = True
            report["state_status"] = state.get("status")
        return report

    def _resolve_record(self, record: AttemptRecord, *, reason: str) -> Dict[str, Any]:
        """Resolve one interrupted record: terminate any process, then confirm.

        Returns ``{"resolved": bool, "process": str|None, "notes": [...]}``.
        """
        notes: List[str] = []
        process_result: Optional[str] = None
        if record.identity:
            try:
                identity = proc.ProcessIdentity.from_dict(record.identity)
                process_result = self._terminate_identity(identity, self.config.term_grace_s)
            except Exception as exc:  # pragma: no cover - defensive
                process_result = "identity_unreadable"
                notes.append(f"identity_unreadable:{type(exc).__name__}")
            if process_result in {"group_unresolved"}:
                notes.append("process_group_unresolved")
            elif process_result == "identity_lost":
                # We did not signal anything we could not prove; record it.
                notes.append("process_identity_lost")

        authority_ok = True
        if record.lease_epoch is not None and record.attempt is not None:
            cleanup = self._fail_closed(
                record.job_id, int(record.lease_epoch), int(record.attempt), reason=reason
            )
            authority_ok = not cleanup["cleanup_required"]
            # Report attempt-bound process-cleanup evidence for the operator.
            if record.phase == "spawning" and not record.identity:
                process_state = "unresolved"  # an unrecorded child may exist
            elif record.identity is None:
                process_state = "confirmed"  # nothing was spawned
            elif process_result in {"exited", "terminated", "killed", "group_terminated", "group_killed", "clean"}:
                process_state = "confirmed"
            else:
                process_state = "unresolved"
            self._report_process_cleanup(
                record.job_id, int(record.lease_epoch), int(record.attempt), process_state
            )
        elif record.phase not in {"claiming", "claim_refused", "api_unreachable"}:
            authority_ok = True

        resolved = authority_ok and "process_group_unresolved" not in notes
        return {"resolved": resolved, "process": process_result, "notes": notes}

    def retry_cleanup(self) -> List[Dict[str, Any]]:
        """Retry termination and fencing/recovering of unresolved records."""
        results: List[Dict[str, Any]] = []
        for record in self._load_records():
            if record.phase != "cleanup_required":
                continue
            outcome = self._resolve_record(record, reason="cleanup_retry")
            if outcome["resolved"]:
                record.phase = "fenced" if not outcome["notes"] else "cleanup_resolved"
                record.error = ",".join(outcome["notes"]) or None
            else:
                record.error = ",".join(outcome["notes"] + ["cleanup_unresolved"])
            self._persist(record)
            results.append({"job_id": record.job_id, **outcome, "phase": record.phase})
        return results

    def startup_recover(self) -> List[Dict[str, Any]]:
        """Inspect persisted records after a restart and resolve interrupted work.

        Never reattaches or replays. Verifies recorded identity before touching a
        process, terminates surviving managed work, and explicitly resolves
        interrupted pre-spawn/post-spawn states (including the
        spawn-to-identity-persistence window, where the record is ``spawning``
        with no identity: no signal is sent — the attempt is fenced so any
        unrecorded child loses its authority).
        """
        actions: List[Dict[str, Any]] = []
        for record in self._load_records():
            phase = record.phase
            if phase in {"conflict", "claim_refused", "prepare_failed"}:
                # Not our attempt / already handled server-side: do not touch.
                continue
            if phase in {"recovered", "fenced", "failed_closed", "cleanup_resolved", "authority_lost", "spawn_failed"}:
                continue
            if phase == "cleanup_required":
                outcome = self._resolve_record(record, reason="startup_cleanup")
            elif phase == "running":
                outcome = self._resolve_record(record, reason="startup_recovered_running")
            elif phase in {"claiming", "claimed", "prepared", "spawning"}:
                # No (or an unrecorded) process; fence/recover the authority.
                if phase == "spawning" and not record.identity:
                    actions.append({"job_id": record.job_id, "phase": phase, "note": "identity_unrecorded"})
                outcome = self._resolve_record(record, reason=f"startup_interrupted_{phase}")
            else:
                continue
            if outcome["resolved"]:
                record.phase = "cleanup_resolved" if outcome["notes"] else "fenced"
                record.error = ",".join(outcome["notes"]) or None
            else:
                record.phase = "cleanup_required"
                record.error = ",".join(outcome["notes"] + ["cleanup_unresolved"])
            self._persist(record)
            actions.append({"job_id": record.job_id, "phase": record.phase, **outcome})
        return actions

    # -- child environment --------------------------------------------------

    def _child_env(self, config: Dict[str, Any], scratch: Path) -> Dict[str, str]:
        # Explicit allowlist built from scratch: no inherited environment, no
        # supervisor credential, no DB/broker secrets.
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(scratch),
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": self.config.child_pythonpath,
            ENV_BASE_URL: self.config.base_url,
            ENV_WORKER_TOKEN: config["worker_token"],
            ENV_RUN_ID: config["run_id"],
            ENV_SESSION_NONCE: config["session_nonce"],
            ENV_TEMPLATE_ID: config["template_id"],
            ENV_ACCOUNT_SCOPE: config["account_scope"],
            ENV_MODE: config["execution_mode"],
            ENV_PARAMS: json.dumps(config.get("params") or {}, separators=(",", ":")),
            ENV_SCRATCH: str(scratch),
        }

    def _child_command(self, source_path: Path) -> List[str]:
        return [self.config.child_python, "-m", "kite_algo_worker.hosted", str(source_path)]

    # -- source -------------------------------------------------------------

    def _write_source(self, job_id: str, version_id: str, source: str, expected_sha: str) -> Path:
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if digest != expected_sha:
            raise ValueError("source hash mismatch")
        path = self._source_path(job_id, version_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Traversable/readable by the child, owned and writable only by the
        # supervisor: the child can read its source but cannot alter it.
        os.chmod(self._workspace / "source", 0o755)
        os.chmod(path.parent, 0o755)
        path.write_text(source, encoding="utf-8")
        os.chmod(path, 0o444)
        return path

    def _prepare_scratch(self, scratch: Path) -> None:
        """Create a child-usable scratch dir (owned by the child uid when set)."""
        scratch.mkdir(parents=True, exist_ok=True)
        if self.config.child_uid is not None:
            try:
                os.chown(scratch, self.config.child_uid, self.config.child_gid)
            except OSError as exc:
                if self.config.require_identity_separation:
                    raise RuntimeError(
                        f"cannot set child ownership on scratch {scratch}: {exc}"
                    ) from exc
                logger.warning("supervisor_scratch_chown_failed", extra={"path": str(scratch)})
        os.chmod(scratch, 0o700)

    # -- supervision --------------------------------------------------------

    def _progress_stale(self, state: Dict[str, Any], started_monotonic: float) -> bool:
        deadline = float(state.get("progress_deadline_s") or 0)
        if deadline <= 0:
            return False
        graceful = deadline + self.config.startup_grace_s
        marker = state.get("last_progress_at")
        if not marker:
            return (self._monotonic() - started_monotonic) > graceful
        try:
            last = datetime.fromisoformat(str(marker).replace("Z", "+00:00"))
        except ValueError:
            return False
        age = (self._wall_clock() - last).total_seconds()
        return age > graceful

    def run_once(self, job_id: Optional[str] = None) -> Dict[str, Any]:
        jobs = self.discover()
        if job_id is not None:
            jobs = [entry for entry in jobs if str(entry.get("job_id")) == job_id]
            if not jobs:
                return {"status": "not_found", "job_id": job_id}
        if not jobs:
            return {"status": "idle"}
        return self._supervise(jobs[0])

    def _supervise(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(entry["job_id"])
        attempt = int(entry["attempt"])
        epoch = int(entry["lease_epoch"])
        record = AttemptRecord(
            job_id=job_id,
            strategy_id=str(entry.get("strategy_id") or "") or None,
            attempt=attempt,
            lease_owner=self.config.lease_owner,
            lease_epoch=epoch,
            phase="claiming",
        )
        self._persist(record)

        lease_until = (self._wall_clock() + timedelta(seconds=self.config.lease_seconds)).isoformat()
        try:
            claimed = self.api.claim(
                job_id,
                lease_owner=self.config.lease_owner,
                expected_lease_epoch=epoch,
                expected_attempt=attempt,
                lease_until=lease_until,
            )
        except SupervisorApiError as exc:
            record.phase = "claim_refused"
            record.error = exc.reason
            self._persist(record)
            return {"status": "claim_refused", "job_id": job_id, "reason": exc.reason}
        except SupervisorTransportError as exc:
            record.phase = "api_unreachable"
            record.error = str(exc)
            self._persist(record)
            return {"status": "api_unreachable", "job_id": job_id}

        epoch = int(claimed.get("lease_epoch", epoch + 1))
        record.lease_epoch = epoch
        record.phase = "claimed"
        self._persist(record)

        # Prepare EXACTLY once. Never replay on timeout, lost response or 409.
        try:
            launch = self.api.prepare(job_id, lease_owner=self.config.lease_owner, lease_epoch=epoch, attempt=attempt)
        except SupervisorApiError as exc:
            if exc.is_conflict:
                record.phase = "conflict"
                record.error = exc.reason
                self._persist(record)
                return {"status": "conflict", "job_id": job_id, "reason": exc.reason}
            record.phase = "prepare_failed"
            record.error = exc.reason
            self._persist(record)
            return {"status": "prepare_failed", "job_id": job_id, "reason": exc.reason}
        except SupervisorTransportError as exc:
            report = self._fail_closed(job_id, epoch, attempt, reason="prepare_response_lost")
            record.phase = "cleanup_required" if report["cleanup_required"] else "failed_closed"
            record.error = str(exc)
            self._persist(record)
            return {"status": "prepare_response_lost", "job_id": job_id, **report}

        record.run_id = launch.get("run_id")
        record.version_id = launch.get("version_id")
        record.source_sha256 = launch.get("source_sha256")
        record.phase = "prepared"
        self._persist(record)

        # Source: authorized fetch, hash verification, supervisor-owned write.
        try:
            source_payload = self.api.source(
                job_id, lease_owner=self.config.lease_owner, lease_epoch=epoch, attempt=attempt
            )
        except (SupervisorApiError, SupervisorTransportError) as exc:
            report = self._fail_closed(job_id, epoch, attempt, reason="source_fetch_failed")
            record.phase = "cleanup_required" if report["cleanup_required"] else "failed_closed"
            record.error = getattr(exc, "reason", str(exc))
            self._persist(record)
            return {"status": "source_fetch_failed", "job_id": job_id, **report}

        try:
            source_path = self._write_source(
                job_id,
                str(launch.get("version_id")),
                str(source_payload.get("source") or ""),
                str(launch.get("source_sha256") or ""),
            )
        except ValueError as exc:
            report = self._fail_closed(job_id, epoch, attempt, reason="source_hash_mismatch")
            record.phase = "cleanup_required" if report["cleanup_required"] else "failed_closed"
            record.error = str(exc)
            self._persist(record)
            return {"status": "source_hash_mismatch", "job_id": job_id, **report}

        # Refuse to spawn unless live authority is positively established:
        # status, desired state, lease epoch/attempt and lease expiry.
        authority = self._authority_check(job_id, epoch, attempt)
        if not authority["ok"]:
            report = self._fail_closed(job_id, epoch, attempt, reason=authority["reason"] or "authority_lost")
            record.phase = "cleanup_required" if report["cleanup_required"] else "authority_lost"
            record.error = authority["reason"]
            self._persist(record)
            return {"status": "authority_lost", "job_id": job_id, "reason": authority["reason"], **report}

        scratch = self._scratch_path(job_id)
        log_path = self._log_path(job_id)
        env = self._child_env(launch, scratch)
        try:
            self._prepare_scratch(scratch)
        except Exception as exc:
            report = self._fail_closed(job_id, epoch, attempt, reason="scratch_prepare_failed")
            record.phase = "cleanup_required" if report["cleanup_required"] else "failed_closed"
            record.error = str(exc)
            self._persist(record)
            return {"status": "scratch_prepare_failed", "job_id": job_id, **report}

        # Record the spawn intent *before* spawning so the spawn-to-identity
        # window is visible on restart (no identity yet → never signal blind).
        record.phase = "spawning"
        record.log_ref = str(log_path)
        self._persist(record)

        try:
            handle = self._spawn(
                self._child_command(source_path),
                env=env,
                cwd=str(scratch),
                log_path=str(log_path),
                max_log_bytes=self.config.max_log_bytes,
                uid=self.config.child_uid,
                gid=self.config.child_gid,
            )
        except Exception as exc:
            report = self._fail_closed(job_id, epoch, attempt, reason="spawn_failed")
            record.phase = "cleanup_required" if report["cleanup_required"] else "failed_closed"
            record.error = str(exc)
            self._persist(record)
            return {"status": "spawn_failed", "job_id": job_id, **report}

        self._active[job_id] = handle
        started = self._monotonic()
        outcome = "supervisor_exception"
        detail: Dict[str, Any] = {}
        stop_result = "not_attempted"
        try:
            record.identity = handle.identity.to_dict()
            record.phase = "running"
            self._persist(record)
            outcome, detail = self._observe(
                job_id, epoch, attempt, handle, started, max_duration_s=launch.get("max_duration_s")
            )
        except BaseException as exc:  # ensure the child is always stopped
            outcome = "supervisor_exception"
            detail = {"error": f"{type(exc).__name__}: {exc}"}
            logger.exception("hosted_supervisor_observation_failed", extra={"job_id": job_id})
        finally:
            try:
                stop_result = handle.stop(self.config.term_grace_s)
            except Exception:  # pragma: no cover - defensive
                stop_result = "stop_failed"
                logger.exception("hosted_supervisor_stop_failed", extra={"job_id": job_id})
            self._active.pop(job_id, None)

        result: Dict[str, Any] = {
            "job_id": job_id,
            "outcome": outcome,
            "stop": stop_result,
            "log_overflow": handle.log_overflow,
            "log_ref": str(log_path),
        }
        if detail:
            result.update(detail)

        # Report supervisor-owned process-cleanup evidence for this attempt so an
        # operator can distinguish "process stopped" from "cleanup unknown". This
        # is best-effort and never changes the local control flow; a failure
        # leaves the job's cleanup evidence unknown (reconciliation stays blocked).
        cleanup_confirmed = stop_result in {
            "exited",
            "terminated",
            "killed",
            "group_terminated",
            "group_killed",
        }
        result["process_cleanup_reported"] = self._report_process_cleanup(
            job_id, epoch, attempt, "confirmed" if cleanup_confirmed else "unresolved"
        )

        process_unresolved = stop_result in {"group_unresolved", "stop_failed"}
        if outcome == "exited" and not process_unresolved:
            # Normal completion is NOT proof of flatness: a launched attempt is
            # released into recovery_required by the server.
            try:
                released = self.api.release(
                    job_id, lease_owner=self.config.lease_owner, lease_epoch=epoch, attempt=attempt
                )
                result["terminal"] = released.get("status")
                result["replacement_blocked"] = released.get("replacement_blocked")
                record.phase = str(released.get("status") or "terminal")
            except SupervisorApiError as exc:
                cleanup = self._fail_closed(job_id, epoch, attempt, reason=f"release_refused:{exc.reason}")
                result.update(cleanup)
                record.phase = "cleanup_required" if cleanup["cleanup_required"] else "failed_closed"
            except SupervisorTransportError:
                record.phase = "cleanup_required"
                result["cleanup_required"] = True
        elif outcome == "observe_timeout" and not process_unresolved:
            try:
                released = self.api.release(
                    job_id, lease_owner=self.config.lease_owner, lease_epoch=epoch, attempt=attempt
                )
                result["terminal"] = released.get("status")
                result["replacement_blocked"] = released.get("replacement_blocked")
                record.phase = str(released.get("status") or "terminal")
            except (SupervisorApiError, SupervisorTransportError):
                result["cleanup_required"] = True
                record.phase = "cleanup_required"
        else:
            # authority_lost / heartbeat_unreachable / progress_stale /
            # progress_unobserved / max_duration_reached / supervisor_stopping /
            # supervisor_exception / unresolved process: fail closed — no replay,
            # and the server keeps the replacement block for launched work.
            cleanup = self._fail_closed(job_id, epoch, attempt, reason=outcome)
            result.update(cleanup)
            if process_unresolved:
                result["cleanup_required"] = True
            record.phase = "cleanup_required" if result.get("cleanup_required") else "fenced"
        if process_unresolved:
            result["cleanup_required"] = True
            record.phase = "cleanup_required"
            record.error = f"process_{stop_result}"
        self._persist(record)
        return result

    def _observe(
        self,
        job_id: str,
        epoch: int,
        attempt: int,
        handle: Any,
        started: float,
        *,
        max_duration_s: Optional[float] = None,
    ) -> tuple:
        next_heartbeat = self._monotonic() + self.config.heartbeat_interval_s
        timeout_at = (
            self._monotonic() + float(self.config.observe_timeout_s)
            if self.config.observe_timeout_s
            else None
        )
        duration_deadline = (
            started + float(max_duration_s)
            if max_duration_s is not None and float(max_duration_s) > 0
            else None
        )
        progress_failures = 0
        while True:
            if self._stopping:
                return "supervisor_stopping", {}
            if handle.poll() is not None:
                return "exited", {"exit_code": handle.poll()}
            now = self._monotonic()
            if duration_deadline is not None and now >= duration_deadline:
                return "max_duration_reached", {"max_duration_s": max_duration_s}
            if timeout_at is not None and now >= timeout_at:
                return "observe_timeout", {}
            if now >= next_heartbeat:
                try:
                    self.api.heartbeat(
                        job_id,
                        lease_owner=self.config.lease_owner,
                        lease_epoch=epoch,
                        attempt=attempt,
                        lease_until=(self._wall_clock() + timedelta(seconds=self.config.lease_seconds)).isoformat(),
                    )
                except SupervisorApiError as exc:
                    return "authority_lost", {"reason": exc.reason}
                except SupervisorTransportError:
                    # Uncertain heartbeat: stop the local child within the bounded
                    # deadline even though the API is unreachable.
                    return "heartbeat_unreachable", {}
                next_heartbeat = self._monotonic() + self.config.heartbeat_interval_s
                try:
                    state = self.api.job_state(
                        job_id, lease_owner=self.config.lease_owner, lease_epoch=epoch, attempt=attempt
                    )
                    progress_failures = 0
                except (SupervisorApiError, SupervisorTransportError):
                    state = None
                    # We must not renew the parent heartbeat indefinitely while
                    # progress is unobservable.
                    progress_failures += 1
                    if progress_failures >= self.config.progress_observation_max_failures:
                        return "progress_unobserved", {"failures": progress_failures}
                if state is not None and self._progress_stale(state, started):
                    return "progress_stale", {}
            wait_for = max(0.02, min(self.config.progress_poll_s, next_heartbeat - self._monotonic()))
            self._sleep(wait_for)

    # -- signals ------------------------------------------------------------

    def request_stop(self) -> None:
        """Ask the running loop to stop; observed between supervision steps."""
        self._stopping = True

    def install_signal_handlers(self) -> None:
        import signal

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle_signal)
            except ValueError:  # not the main thread
                logger.warning("hosted_supervisor_signal_handler_unavailable", extra={"signal": int(sig)})

    def _handle_signal(self, signum, frame) -> None:  # pragma: no cover - signal path
        logger.warning("hosted_supervisor_signal", extra={"signal": int(signum)})
        self.request_stop()

    def terminate_active(self) -> List[Dict[str, Any]]:
        """Bounded process-group termination of any child the loop still owns."""
        results: List[Dict[str, Any]] = []
        for job_id, handle in list(self._active.items()):
            try:
                outcome = handle.stop(self.config.term_grace_s)
            except Exception:  # pragma: no cover - defensive
                outcome = "stop_failed"
                logger.exception("hosted_supervisor_signal_stop_failed", extra={"job_id": job_id})
            results.append({"job_id": job_id, "stop": outcome})
        return results

    def run_forever(self, *, idle_sleep_s: float = 5.0) -> None:
        logger.info("hosted_supervisor_started", extra={"lease_owner": self.config.lease_owner})
        while not self._stopping:
            try:
                result = self.run_once()
            except Exception:  # pragma: no cover - keep the runner alive
                logger.exception("hosted_supervisor_run_failed")
                result = {"status": "error"}
            logger.info("hosted_supervisor_cycle", extra={"result": result})
            if result.get("status") == "idle" and not self._stopping:
                self._sleep(idle_sleep_s)


def _result_is_unresolved(result: Dict[str, Any]) -> bool:
    if result.get("cleanup_required"):
        return True
    if str(result.get("stop") or "") in {"group_unresolved", "stop_failed", "identity_lost"}:
        return True
    return str(result.get("status") or "") in {"api_unreachable"}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Hosted-strategy process supervisor")
    parser.add_argument("--once", action="store_true", help="claim and supervise at most one job, then exit")
    parser.add_argument("--job", default=None, help="supervise a specific queued job id")
    parser.add_argument("--retry-cleanup", action="store_true", help="retry pending fence/recover cleanups and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    config = SupervisorConfig.from_env()
    supervisor = HostedSupervisor(config)
    supervisor.install_signal_handlers()

    # Wire startup inspection: resolve interrupted work before doing anything new.
    startup = supervisor.startup_recover()
    if startup:
        logger.info("hosted_supervisor_startup_recovery", extra={"actions": startup})
    startup_unresolved = any(
        str(action.get("phase") or "") == "cleanup_required" or not action.get("resolved", True)
        for action in startup
    )

    if args.retry_cleanup:
        results = supervisor.retry_cleanup()
        logger.info("hosted_supervisor_cleanup_retry", extra={"results": results})
        unresolved = any(str(item.get("phase") or "") == "cleanup_required" for item in results)
        return 2 if unresolved else 0
    if args.once or args.job:
        try:
            result = supervisor.run_once(job_id=args.job)
        finally:
            supervisor.terminate_active()
        logger.info("hosted_supervisor_once", extra={"result": result})
        return 2 if (_result_is_unresolved(result) or startup_unresolved) else 0

    try:
        supervisor.run_forever()
    finally:
        stops = supervisor.terminate_active()
        if stops:
            logger.info("hosted_supervisor_stop_active", extra={"stops": stops})
    unresolved = any(str(stop.get("stop") or "") in {"group_unresolved", "stop_failed"} for stop in stops)
    return 2 if (unresolved or startup_unresolved) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
