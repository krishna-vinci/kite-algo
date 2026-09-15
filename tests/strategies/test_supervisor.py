"""Supervisor orchestration tests with a fake lifecycle API.

The child programs are harmless local processes; the lifecycle API is a fake.
These pin preparation-once/conflict semantics, fail-closed behaviour on lease
loss and API unavailability, progress-based fencing, and the child environment
allowlist. No database, broker, orders or notifications are involved.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.strategies import supervisor_process as proc
from backend.strategies.supervisor import AttemptRecord, HostedSupervisor, SupervisorConfig
from backend.strategies.supervisor_api import SupervisorApiError, SupervisorTransportError

PY = sys.executable
SOURCE = "def main(ctx):\n    return 0\n"
SOURCE_SHA = hashlib.sha256(SOURCE.encode()).hexdigest()


class FakeApi:
    def __init__(self) -> None:
        self.calls = []
        self.jobs = [{"job_id": "hsj_1", "strategy_id": "hs_1", "attempt": 1, "lease_epoch": 0, "status": "queued"}]
        self.lease_epoch = 1
        self.prepare_error = None
        self.source_error = None
        self.heartbeat_error = None
        self.release_error = None
        self.fence_error = None
        self.fence_ok = True
        self.job_state_error = None
        self.job_state_fail_after = None
        self._job_state_calls = 0
        self.job_state_payload = {
            "status": "running",
            "desired_state": "started",
            "lease_epoch": 1,
            "attempt": 1,
            "lease_until": (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
            "progress_deadline_s": 600,
            "last_progress_at": None,
        }

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))

    def _maybe(self, error):
        if error is not None:
            raise error

    def list_jobs(self, *, status="queued", limit=50):
        self._record("list_jobs", status=status)
        return list(self.jobs)

    def claim(self, job_id, **kwargs):
        self._record("claim", job_id=job_id, **kwargs)
        return {"job_id": job_id, "lease_epoch": self.lease_epoch}

    def prepare(self, job_id, **kwargs):
        self._record("prepare", job_id=job_id, **kwargs)
        self._maybe(self.prepare_error)
        return {
            "job_id": job_id,
            "strategy_id": "hs_1",
            "attempt": 1,
            "lease_epoch": self.lease_epoch,
            "run_id": "run_1",
            "worker_token": "kwa_secret",
            "session_nonce": "wsn_secret",
            "template_id": "hosted:hs_1",
            "execution_mode": "paper",
            "account_scope": "kite:paper",
            "params": {"lots": 1},
            "max_duration_s": 21600,
            "progress_deadline_s": 600,
            "stale_exit_policy": "exit_on_worker_stale",
            "version_id": "hsv_1",
            "source_sha256": SOURCE_SHA,
        }

    def source(self, job_id, **kwargs):
        self._record("source", job_id=job_id, **kwargs)
        self._maybe(self.source_error)
        return {"source": SOURCE, "source_sha256": SOURCE_SHA, "version_id": "hsv_1"}

    def job_state(self, job_id, **kwargs):
        self._record("job_state", job_id=job_id, **kwargs)
        self._job_state_calls += 1
        if self.job_state_error is not None:
            raise self.job_state_error
        if self.job_state_fail_after is not None and self._job_state_calls > self.job_state_fail_after:
            raise SupervisorTransportError("state unavailable")
        return dict(self.job_state_payload)

    def heartbeat(self, job_id, **kwargs):
        self._record("heartbeat", job_id=job_id, **kwargs)
        self._maybe(self.heartbeat_error)
        return {"status": "ok"}

    def release(self, job_id, **kwargs):
        self._record("release", job_id=job_id, **kwargs)
        self._maybe(self.release_error)
        return {"status": "recovery_required", "replacement_blocked": True}

    def fence(self, job_id, **kwargs):
        self._record("fence", job_id=job_id, **kwargs)
        self._maybe(self.fence_error)
        if not self.fence_ok:
            raise SupervisorTransportError("unreachable")
        return {"status": "recovery_required", "replacement_blocked": True}

    def recover(self, job_id, **kwargs):
        self._record("recover", job_id=job_id, **kwargs)
        return {"status": "recovery_required", "replacement_blocked": True}

    def process_cleanup(self, job_id, **kwargs):
        self._record("process_cleanup", job_id=job_id, **kwargs)
        return {"job_id": job_id, "process_cleanup_state": kwargs.get("state")}


class _HarnessSupervisor(HostedSupervisor):
    """Runs a harmless local program instead of the SDK bootstrap."""

    child_script = "import sys; sys.exit(0)"

    def _child_command(self, source_path):  # noqa: D401
        return [PY, "-c", self.child_script]


def _config(tmp_path: Path, **overrides) -> SupervisorConfig:
    defaults = dict(
        base_url="http://example.invalid",
        credential="cred",
        workspace_root=str(tmp_path / "ws"),
        lease_owner="sup-test",
        lease_seconds=120.0,
        heartbeat_interval_s=30.0,
        progress_poll_s=0.5,
        term_grace_s=1.0,
    )
    defaults.update(overrides)
    return SupervisorConfig(**defaults)


def _supervisor(tmp_path, api, *, heartbeat_interval_s=30.0, observe_timeout_s=None):
    return _HarnessSupervisor(
        _config(tmp_path, heartbeat_interval_s=heartbeat_interval_s, observe_timeout_s=observe_timeout_s),
        api=api,
        sleep=lambda _s: None,
    )


def test_happy_path_releases_and_does_not_replay(tmp_path):
    api = FakeApi()
    api.job_state_payload = {
        **api.job_state_payload,
        "last_progress_at": datetime.now(timezone.utc).isoformat(),
    }
    sup = _supervisor(tmp_path, api)

    result = sup.run_once()
    assert result["outcome"] == "exited", result
    assert result["terminal"] == "recovery_required"
    assert result["replacement_blocked"] is True
    names = [c[0] for c in api.calls]
    assert names.count("prepare") == 1
    assert names.count("source") == 1
    assert "release" in names
    assert "fence" not in names
    # Source written read-only under the supervisor workspace.
    assert list((Path(sup.config.workspace_root) / "source").rglob("*.py"))


def test_conflict_does_not_fence_or_spawn(tmp_path):
    api = FakeApi()
    api.prepare_error = SupervisorApiError(409, "HOSTED_HANDOFF_ALREADY_COMPLETED")
    sup = _supervisor(tmp_path, api)

    result = sup.run_once()
    assert result["status"] == "conflict"
    assert result["reason"] == "HOSTED_HANDOFF_ALREADY_COMPLETED"
    names = [c[0] for c in api.calls]
    assert names.count("prepare") == 1
    assert "fence" not in names and "recover" not in names and "release" not in names


def test_lost_prepare_response_fails_closed_without_replay(tmp_path):
    api = FakeApi()
    api.prepare_error = SupervisorTransportError("timeout")
    sup = _supervisor(tmp_path, api)

    result = sup.run_once()
    assert result["status"] == "prepare_response_lost"
    names = [c[0] for c in api.calls]
    assert names.count("prepare") == 1
    assert names.count("fence") == 1


def test_authority_loss_stops_child_and_fences(tmp_path):
    api = FakeApi()
    api.heartbeat_error = SupervisorApiError(409, "HOSTED_LEASE_EXPIRED")
    sup = _supervisor(tmp_path, api, heartbeat_interval_s=0.001)
    sup.child_script = "import time; time.sleep(30)"

    result = sup.run_once()
    assert result["outcome"] == "authority_lost"
    assert result["stop"] in {"terminated", "killed"}
    # Fence is preferred; when it reports the lease expired, recover is used.
    names = [c[0] for c in api.calls]
    assert "fence" in names


def test_api_unreachable_cleanup_is_visible_and_retryable(tmp_path):
    api = FakeApi()
    api.heartbeat_error = SupervisorTransportError("down")
    api.fence_ok = False  # fence also unreachable
    sup = _supervisor(tmp_path, api, heartbeat_interval_s=0.001)
    sup.child_script = "import time; time.sleep(30)"

    result = sup.run_once()
    assert result["outcome"] == "heartbeat_unreachable"
    assert result["cleanup_required"] is True
    assert result["stop"] in {"terminated", "killed"}

    # When the API returns, cleanup is retried and no longer pending.
    api.fence_ok = True
    retried = sup.retry_cleanup()
    assert retried and retried[0]["job_id"] == "hsj_1"


def test_progress_stall_fences(tmp_path):
    api = FakeApi()
    api.job_state_payload = {
        **api.job_state_payload,
        "progress_deadline_s": 1,
        "last_progress_at": (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat(),
    }
    sup = _supervisor(tmp_path, api, heartbeat_interval_s=0.001)
    sup.child_script = "import time; time.sleep(30)"

    result = sup.run_once()
    assert result["outcome"] == "progress_stale"
    names = [c[0] for c in api.calls]
    assert "fence" in names
    # The supervisor never reports progress itself.
    assert all(name not in {"progress", "run_progress"} for name, _ in api.calls)


def test_source_hash_mismatch_refuses_spawn(tmp_path):
    api = FakeApi()
    original = api.prepare

    def _tampered(job_id, **kwargs):
        payload = original(job_id, **kwargs)
        payload["source_sha256"] = "0" * 64
        return payload

    api.prepare = _tampered  # type: ignore[assignment]
    sup = _supervisor(tmp_path, api)

    result = sup.run_once()
    assert result["status"] == "source_hash_mismatch"
    names = [c[0] for c in api.calls]
    assert "fence" in names
    assert not (Path(sup.config.workspace_root) / "source").exists() or not list(
        (Path(sup.config.workspace_root) / "source").rglob("*.py")
    )


def test_spawn_refused_when_authority_not_live(tmp_path):
    api = FakeApi()
    api.job_state_payload = {"status": "recovery_required", "progress_deadline_s": 600, "last_progress_at": None}
    sup = _supervisor(tmp_path, api)

    result = sup.run_once()
    assert result["status"] == "authority_lost"


def test_child_environment_allowlist(tmp_path):
    api = FakeApi()
    sup = _supervisor(tmp_path, api)
    env = sup._child_env(api.prepare("hsj_1"), tmp_path)

    expected_keys = {
        "PATH", "HOME", "PYTHONUNBUFFERED", "PYTHONPATH",
        "KITE_ALGO_BASE_URL", "KITE_ALGO_WORKER_TOKEN", "KITE_ALGO_RUN_ID",
        "KITE_ALGO_SESSION_NONCE", "KITE_ALGO_TEMPLATE_ID", "KITE_ALGO_ACCOUNT_SCOPE",
        "KITE_ALGO_MODE", "KITE_ALGO_PARAMS", "KITE_ALGO_SCRATCH",
    }
    assert set(env) == expected_keys
    # No supervisor credential, no DB/broker secrets.
    for forbidden in ("HOSTED_SUPERVISOR_CREDENTIAL", "DB_PASSWORD", "DATABASE_URL", "KITE_API_KEY"):
        assert forbidden not in env
        assert forbidden not in env.values()
    assert env["KITE_ALGO_WORKER_TOKEN"] == "kwa_secret"


# ---------------------------------------------------------------------------
# exception-safe / signal-safe shutdown
# ---------------------------------------------------------------------------


def test_exception_immediately_after_spawn_still_stops_child(tmp_path):
    api = FakeApi()
    sup = _supervisor(tmp_path, api)
    sup.child_script = "import time; time.sleep(30)"

    def _boom(*_a, **_k):
        raise RuntimeError("observer exploded")

    sup._observe = _boom  # type: ignore[assignment]
    result = sup.run_once()
    assert result["outcome"] == "supervisor_exception"
    assert result["stop"] in {"terminated", "killed"}
    names = [c[0] for c in api.calls]
    assert "fence" in names  # unknown state ⇒ fail closed, no replay


def test_request_stop_stops_child_and_fences(tmp_path):
    api = FakeApi()
    sup = _supervisor(tmp_path, api)
    sup.child_script = "import time; time.sleep(30)"
    sup.request_stop()
    result = sup.run_once()
    assert result["outcome"] == "supervisor_stopping"
    assert result["stop"] in {"terminated", "killed"}
    assert "fence" in [c[0] for c in api.calls]


def test_signal_handler_sets_stop_flag(tmp_path):
    api = FakeApi()
    sup = _supervisor(tmp_path, api)
    import signal as _signal

    sup._handle_signal(_signal.SIGTERM, None)
    assert sup._stopping is True


# ---------------------------------------------------------------------------
# restart cleanup
# ---------------------------------------------------------------------------


def test_startup_recover_terminates_surviving_child(tmp_path):
    api = FakeApi()
    sup = _supervisor(tmp_path, api)
    handle = proc.spawn_child(
        [PY, "-c", "import time; time.sleep(60)"],
        env={"PATH": os.environ.get("PATH", "")},
        cwd=str(tmp_path),
        log_path=str(tmp_path / "child.log"),
        rlimits=[],
    )
    sup._persist(
        AttemptRecord(
            job_id="hsj_1",
            strategy_id="hs_1",
            attempt=1,
            lease_epoch=1,
            lease_owner="sup-test",
            phase="running",
            identity=handle.identity.to_dict(),
        )
    )
    actions = sup.startup_recover()
    assert handle.poll() is not None  # the surviving child was terminated
    assert any(a["job_id"] == "hsj_1" for a in actions)
    assert sup._load_records()[0].phase in {"fenced", "cleanup_resolved"}


def test_startup_recover_spawning_without_identity_never_signals(tmp_path):
    api = FakeApi()
    sup = _supervisor(tmp_path, api)
    sup._persist(
        AttemptRecord(job_id="hsj_1", attempt=1, lease_epoch=1, lease_owner="sup-test", phase="spawning")
    )
    actions = sup.startup_recover()
    assert any(a.get("note") == "identity_unrecorded" for a in actions)
    assert sup._load_records()[0].phase in {"fenced", "cleanup_resolved"}


def test_startup_recover_does_not_touch_conflict(tmp_path):
    api = FakeApi()
    sup = _supervisor(tmp_path, api)
    sup._persist(AttemptRecord(job_id="hsj_1", attempt=1, lease_epoch=1, phase="conflict"))
    actions = sup.startup_recover()
    assert actions == []
    assert "fence" not in [c[0] for c in api.calls]


# ---------------------------------------------------------------------------
# limits and authority
# ---------------------------------------------------------------------------


def test_max_duration_reached_fences(tmp_path):
    api = FakeApi()
    original = api.prepare

    def _short(job_id, **kwargs):
        payload = original(job_id, **kwargs)
        payload["max_duration_s"] = 0.2
        return payload

    api.prepare = _short  # type: ignore[assignment]
    sup = _supervisor(tmp_path, api)
    sup.child_script = "import time; time.sleep(30)"
    result = sup.run_once()
    assert result["outcome"] == "max_duration_reached"
    assert "fence" in [c[0] for c in api.calls]
    assert result["stop"] in {"terminated", "killed"}


def test_progress_observation_failure_budget_fails_closed(tmp_path):
    api = FakeApi()
    # The pre-spawn authority read succeeds; subsequent progress reads do not.
    api.job_state_fail_after = 1
    sup = _supervisor(tmp_path, api, heartbeat_interval_s=0.001)
    sup.child_script = "import time; time.sleep(30)"
    sup.config.progress_observation_max_failures = 2
    result = sup.run_once()
    assert result["outcome"] == "progress_unobserved"
    assert "fence" in [c[0] for c in api.calls]


def test_expired_lease_refuses_spawn(tmp_path):
    api = FakeApi()
    api.job_state_payload = {
        **api.job_state_payload,
        "lease_until": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
    }
    sup = _supervisor(tmp_path, api)
    result = sup.run_once()
    assert result["status"] == "authority_lost"
    assert result["reason"] == "HOSTED_LEASE_EXPIRED"
    assert "stop" not in result  # no child was ever spawned
    assert "release" not in [c[0] for c in api.calls]


def test_config_validation_rejects_bad_relationships(tmp_path):
    with pytest.raises(ValueError):
        _config(tmp_path, heartbeat_interval_s=200.0)
    with pytest.raises(ValueError):
        _config(tmp_path, require_identity_separation=True)
    # Valid separation config is accepted.
    _config(tmp_path, child_uid=1, child_gid=1, require_identity_separation=True)


# ---------------------------------------------------------------------------
# honest cleanup results
# ---------------------------------------------------------------------------


def test_cleanup_api_refusal_is_retryable(tmp_path):
    api = FakeApi()
    api.heartbeat_error = SupervisorTransportError("down")
    api.fence_error = SupervisorApiError(500, None)
    sup = _supervisor(tmp_path, api, heartbeat_interval_s=0.001)
    sup.child_script = "import time; time.sleep(30)"

    result = sup.run_once()
    assert result["cleanup_required"] is True
    assert sup._load_records()[0].phase == "cleanup_required"

    api.fence_error = None
    retried = sup.retry_cleanup()
    assert retried and retried[0]["resolved"] is True
    assert sup._load_records()[0].phase in {"fenced", "cleanup_resolved"}


def test_unresolved_cleanup_result_marks_supervisor_failure(tmp_path):
    from backend.strategies.supervisor import _result_is_unresolved

    assert _result_is_unresolved({"cleanup_required": True}) is True
    assert _result_is_unresolved({"stop": "group_unresolved"}) is True
    assert _result_is_unresolved({"cleanup_required": False, "stop": "terminated"}) is False


def test_cleanup_error_code_is_not_treated_as_proof(tmp_path):
    """HOSTED_ATTEMPT_FENCED must be confirmed by a read, not inferred."""
    api = FakeApi()
    api.fence_error = SupervisorApiError(409, "HOSTED_ATTEMPT_FENCED")
    # The authenticated read still shows a live attempt → cleanup unresolved.
    api.job_state_payload = {**api.job_state_payload, "status": "running"}
    sup = _supervisor(tmp_path, api)

    report = sup._fail_closed("hsj_1", 1, 1, reason="test")
    assert report["fence"] == "HOSTED_ATTEMPT_FENCED"
    assert report["cleanup_required"] is True
    assert report["terminal_confirmed"] is False


def test_cleanup_error_code_confirmed_by_authenticated_read(tmp_path):
    api = FakeApi()
    api.fence_error = SupervisorApiError(409, "HOSTED_ATTEMPT_FENCED")
    api.job_state_payload = {**api.job_state_payload, "status": "recovery_required"}
    sup = _supervisor(tmp_path, api)

    report = sup._fail_closed("hsj_1", 1, 1, reason="test")
    assert report["cleanup_required"] is False
    assert report["terminal_confirmed"] is True
    assert report["state_status"] == "recovery_required"


@pytest.mark.parametrize(
    "mutate",
    [
        {"lease_epoch": None},
        {"attempt": None},
        {"lease_until": None},
        {"desired_state": None},
    ],
)
def test_authority_check_fails_closed_on_missing_fields(tmp_path, mutate):
    api = FakeApi()
    api.job_state_payload = {**api.job_state_payload, **mutate}
    sup = _supervisor(tmp_path, api)

    authority = sup._authority_check("hsj_1", 1, 1)
    assert authority["ok"] is False
    assert authority["reason"] in {
        "HOSTED_AUTHORITY_INCOMPLETE",
        "HOSTED_LEASE_UNREADABLE",
        "HOSTED_ATTEMPT_STOPPED",
    }


# ---------------------------------------------------------------------------
# distinct-identity layout
# ---------------------------------------------------------------------------


def test_workspace_permissions_are_child_safe(tmp_path):
    api = FakeApi()
    api.job_state_payload = {
        **api.job_state_payload,
        "last_progress_at": datetime.now(timezone.utc).isoformat(),
    }
    sup = _supervisor(tmp_path, api)
    sup.run_once()
    ws = Path(sup.config.workspace_root)
    assert (ws.stat().st_mode & 0o777) == 0o711
    assert ((ws / "attempts").stat().st_mode & 0o777) == 0o700
    sources = list((ws / "source").rglob("*.py"))
    assert sources and (sources[0].stat().st_mode & 0o777) == 0o444
