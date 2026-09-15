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
    #: Test-only bound on how long to observe a child before treating it as hung.
    observe_timeout_s: Optional[float] = None

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

    def _fail_closed(
        self, job_id: str, epoch: int, attempt: int, *, reason: str
    ) -> Dict[str, Any]:
        """Fence while the lease is live, recover once it has expired.

        Returns an honest report; ``cleanup_required`` is set when the API was
        unreachable so the caller does not claim a stop it could not confirm.
        """
        report: Dict[str, Any] = {
            "fence": "not_attempted",
            "recover": "not_attempted",
            "cleanup_required": False,
        }
        try:
            self.api.fence(job_id, lease_owner=self.config.lease_owner, lease_epoch=epoch, attempt=attempt, reason=reason)
            report["fence"] = "ok"
            return report
        except SupervisorApiError as exc:
            report["fence"] = exc.reason or f"http_{exc.status_code}"
            if exc.reason in {"HOSTED_LEASE_EXPIRED", "HOSTED_ATTEMPT_FENCED"}:
                try:
                    self.api.recover(
                        job_id, lease_owner=self.config.lease_owner, lease_epoch=epoch, attempt=attempt, reason=reason
                    )
                    report["recover"] = "ok"
                except SupervisorApiError as recover_exc:
                    report["recover"] = recover_exc.reason or f"http_{recover_exc.status_code}"
                except SupervisorTransportError:
                    report["cleanup_required"] = True
            elif exc.reason == "HOSTED_LEASE_STILL_LIVE":
                report["cleanup_required"] = True
            return report
        except SupervisorTransportError:
            report["cleanup_required"] = True
            return report

    def retry_cleanup(self) -> List[Dict[str, Any]]:
        """Retry fencing/recovering any attempt left ``cleanup_required``."""
        results = []
        for record in self._load_records():
            if record.phase != "cleanup_required":
                continue
            if record.lease_epoch is None or record.attempt is None:
                continue
            result = self._fail_closed(
                record.job_id, int(record.lease_epoch), int(record.attempt), reason="cleanup_retry"
            )
            if not result["cleanup_required"]:
                record.phase = "recovered" if result["recover"] == "ok" else "fenced"
                record.error = None
            self._persist(record)
            results.append({"job_id": record.job_id, **result})
        return results

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
        # Supervisor-owned and read-only: the child cannot rewrite its source.
        path.write_text(source, encoding="utf-8")
        os.chmod(path, 0o400)
        return path

    # -- supervision --------------------------------------------------------

    def _authority_live(self, job_id: str, epoch: int, attempt: int) -> bool:
        try:
            state = self.api.job_state(job_id, lease_owner=self.config.lease_owner, lease_epoch=epoch, attempt=attempt)
            return state.get("status") in {"starting", "running"}
        except (SupervisorApiError, SupervisorTransportError):
            return False

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

        # Refuse to spawn if authority is not live.
        if not self._authority_live(job_id, epoch, attempt):
            record.phase = "authority_lost"
            self._persist(record)
            return {"status": "authority_lost", "job_id": job_id}

        scratch = self._scratch_path(job_id)
        scratch.mkdir(parents=True, exist_ok=True)
        os.chmod(scratch, 0o700)
        log_path = self._log_path(job_id)
        env = self._child_env(launch, scratch)
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
        except Exception as exc:  # pragma: no cover - spawn failure
            report = self._fail_closed(job_id, epoch, attempt, reason="spawn_failed")
            record.phase = "cleanup_required" if report["cleanup_required"] else "failed_closed"
            record.error = str(exc)
            self._persist(record)
            return {"status": "spawn_failed", "job_id": job_id, **report}

        self._active[job_id] = handle
        record.identity = handle.identity.to_dict()
        record.log_ref = str(log_path)
        record.phase = "running"
        self._persist(record)
        started = self._monotonic()

        outcome, detail = self._observe(job_id, epoch, attempt, handle, started)
        stop_result = handle.stop(self.config.term_grace_s)
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

        if outcome in {"heartbeat_unreachable"} or stop_result == "identity_lost":
            cleanup = self._fail_closed(job_id, epoch, attempt, reason=outcome)
            result.update(cleanup)
            record.phase = "cleanup_required" if cleanup["cleanup_required"] else "failed_closed"
        elif outcome in {"progress_stale", "authority_lost"}:
            # A stalled or lost attempt is fenced/recovered: no replay, and the
            # server keeps the replacement block for launched work.
            cleanup = self._fail_closed(job_id, epoch, attempt, reason=outcome)
            result.update(cleanup)
            record.phase = "cleanup_required" if cleanup["cleanup_required"] else "fenced"
        else:
            # Normal completion (or the test-only observe bound) is NOT proof of
            # flatness: a launched attempt is released into recovery_required.
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
        self._persist(record)
        return result

    def _observe(
        self, job_id: str, epoch: int, attempt: int, handle: Any, started: float
    ) -> tuple:
        next_heartbeat = self._monotonic() + self.config.heartbeat_interval_s
        timeout_at = (
            self._monotonic() + float(self.config.observe_timeout_s)
            if self.config.observe_timeout_s
            else None
        )
        while True:
            if handle.poll() is not None:
                return "exited", {"exit_code": handle.poll()}
            now = self._monotonic()
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
                except (SupervisorApiError, SupervisorTransportError):
                    state = None
                if state is not None and self._progress_stale(state, started):
                    return "progress_stale", {}
            wait_for = max(0.02, min(self.config.progress_poll_s, next_heartbeat - self._monotonic()))
            self._sleep(wait_for)

    def run_forever(self, *, idle_sleep_s: float = 5.0) -> None:
        logger.info("hosted_supervisor_started", extra={"lease_owner": self.config.lease_owner})
        while True:
            try:
                result = self.run_once()
            except Exception:  # pragma: no cover - keep the runner alive
                logger.exception("hosted_supervisor_run_failed")
                result = {"status": "error"}
            logger.info("hosted_supervisor_cycle", extra={"result": result})
            if result.get("status") == "idle":
                self._sleep(idle_sleep_s)


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
    if args.retry_cleanup:
        results = supervisor.retry_cleanup()
        logger.info("hosted_supervisor_cleanup_retry", extra={"results": results})
        return 0
    if args.once or args.job:
        result = supervisor.run_once(job_id=args.job)
        logger.info("hosted_supervisor_once", extra={"result": result})
        return 0
    supervisor.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
