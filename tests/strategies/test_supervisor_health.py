"""The strategy-runner health contract: healthy, stalled, denied, unreachable.

These tests drive the published snapshot and the read-only verdict directly, so
they pin the properties the container depends on: a transient blip keeps the
container healthy, a stalled loop and a rejected credential do not, a failed
child is not a supervisor failure, and the contract never carries the supervisor
credential.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.strategies import supervisor_health as health
from backend.strategies.supervisor import (
    HostedSupervisor,
    SupervisorApiError,
    SupervisorConfig,
    SupervisorTransportError,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _state(*, at: datetime = NOW, **overrides):
    """A published snapshot relative to ``at`` (defaults to the fixed NOW)."""
    payload = {
        "component": "hosted-supervisor",
        "pid": 4242,
        "started_at": (at - timedelta(minutes=30)).isoformat(),
        "cycle_count": 120,
        "last_cycle_at": (at - timedelta(seconds=3)).isoformat(),
        "last_success_at": (at - timedelta(seconds=3)).isoformat(),
        "last_error": None,
        "consecutive_failures": 0,
        "auth_failed": False,
        "active_children": 0,
        "lease_owner": "runner:1",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------


def test_healthy_loop_is_healthy():
    verdict = health.evaluate_health(_state(), now=NOW)
    assert verdict.healthy is True
    assert verdict.reason == health.REASON_OK


def test_a_fresh_container_is_starting_not_unhealthy():
    verdict = health.evaluate_health(
        _state(started_at=(NOW - timedelta(seconds=5)).isoformat(), last_cycle_at=None),
        now=NOW,
        startup_grace_s=60,
    )
    assert verdict.healthy is True
    assert verdict.reason == health.REASON_STARTING


def test_a_stalled_loop_is_unhealthy():
    verdict = health.evaluate_health(
        _state(last_cycle_at=(NOW - timedelta(seconds=90)).isoformat()),
        now=NOW,
        loop_stale_after_s=45,
    )
    assert verdict.healthy is False
    assert verdict.reason == health.REASON_LOOP_STALLED


def test_a_transient_control_plane_blip_does_not_restart_the_container():
    verdict = health.evaluate_health(
        _state(
            last_success_at=(NOW - timedelta(seconds=60)).isoformat(),
            last_cycle_at=(NOW - timedelta(seconds=2)).isoformat(),
            consecutive_failures=3,
            last_error="transport",
        ),
        now=NOW,
        control_plane_stale_after_s=150,
    )
    assert verdict.healthy is True
    assert verdict.reason == health.REASON_OK


def test_persistent_control_plane_unavailability_is_unhealthy():
    verdict = health.evaluate_health(
        _state(
            last_success_at=(NOW - timedelta(seconds=400)).isoformat(),
            last_cycle_at=(NOW - timedelta(seconds=2)).isoformat(),
            consecutive_failures=12,
            last_error="transport",
        ),
        now=NOW,
        control_plane_stale_after_s=150,
    )
    assert verdict.healthy is False
    assert verdict.reason == health.REASON_CONTROL_PLANE_UNAVAILABLE


def test_a_credential_that_was_never_accepted_is_unhealthy_after_grace():
    verdict = health.evaluate_health(
        _state(
            started_at=(NOW - timedelta(minutes=10)).isoformat(),
            last_success_at=None,
            consecutive_failures=4,
            auth_failed=True,
            last_error="http_401",
        ),
        now=NOW,
    )
    assert verdict.healthy is False
    assert verdict.reason == health.REASON_AUTH_REJECTED
    assert "http_401" in (verdict.detail or "")


def test_auth_failure_is_tolerated_inside_the_startup_grace():
    verdict = health.evaluate_health(
        _state(
            started_at=(NOW - timedelta(seconds=10)).isoformat(),
            last_success_at=None,
            consecutive_failures=1,
            auth_failed=True,
        ),
        now=NOW,
        startup_grace_s=60,
    )
    assert verdict.healthy is True
    assert verdict.reason == health.REASON_STARTING


def test_missing_and_corrupt_files_are_unhealthy_with_distinct_reasons():
    assert health.evaluate_health(None, now=NOW).reason == health.REASON_FILE_ABSENT
    tmp = Path("/tmp") / "nonexistent-health-file.json"
    state, reason = health.read_state(tmp)
    assert state is None and reason == health.REASON_FILE_ABSENT


def test_an_unparseable_snapshot_is_unhealthy_not_healthy(tmp_path):
    path = tmp_path / "health.json"
    path.write_text("{not json", encoding="utf-8")
    state, reason = health.read_state(path)
    assert state is None
    assert reason == health.REASON_FILE_INVALID
    assert health.evaluate_health(state, now=NOW, unreadable_reason=reason).healthy is False


def test_check_reads_the_configured_file_without_touching_anything(tmp_path):
    path = tmp_path / "health.json"
    path.write_text(json.dumps(_state()), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    verdict = health.check(
        path=path,
        environ={"HOSTED_SUPERVISOR_HEALTH_FILE": str(path)},
        now=NOW,
    )
    assert verdict.healthy is True
    assert path.read_text(encoding="utf-8") == before  # read-only


def test_windows_come_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps(_state(last_cycle_at=(NOW - timedelta(seconds=30)).isoformat())),
        encoding="utf-8",
    )
    strict = health.check(
        path=path,
        environ={
            "HOSTED_SUPERVISOR_HEALTH_FILE": str(path),
            "HOSTED_SUPERVISOR_HEALTH_LOOP_STALE_S": "10",
        },
        now=NOW,
    )
    relaxed = health.check(
        path=path,
        environ={
            "HOSTED_SUPERVISOR_HEALTH_FILE": str(path),
            "HOSTED_SUPERVISOR_HEALTH_LOOP_STALE_S": "120",
        },
        now=NOW,
    )
    assert strict.healthy is False and strict.reason == health.REASON_LOOP_STALLED
    assert relaxed.healthy is True


def test_cli_exit_codes_follow_the_verdict(tmp_path, monkeypatch, capsys):
    path = tmp_path / "health.json"
    live_now = datetime.now(timezone.utc)
    path.write_text(json.dumps(_state(at=live_now)), encoding="utf-8")
    monkeypatch.setenv("HOSTED_SUPERVISOR_HEALTH_FILE", str(path))
    assert health.main([]) == 0
    assert "healthy" in capsys.readouterr().out

    path.write_text(
        json.dumps(_state(at=live_now, auth_failed=True, consecutive_failures=2)),
        encoding="utf-8",
    )
    assert health.main([]) == 1
    assert "auth_rejected" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the published snapshot
# ---------------------------------------------------------------------------


def test_snapshot_never_contains_the_supervisor_credential(tmp_path):
    secret = "super-secret-credential-value"
    publisher = health.SupervisorHealth(tmp_path / "state" / "health.json")
    publisher.record_start(lease_owner="runner:1")
    publisher.record_success(active_children=2)
    publisher.record_failure("api_unreachable", auth_failed=False)

    raw = (tmp_path / "state" / "health.json").read_text(encoding="utf-8")
    assert secret not in raw
    payload = json.loads(raw)
    assert set(payload) == {
        "component", "pid", "started_at", "cycle_count", "last_cycle_at",
        "last_success_at", "last_error", "consecutive_failures", "auth_failed",
        "active_children", "lease_owner",
    }


def test_health_directory_is_private_to_the_supervisor(tmp_path):
    path = tmp_path / "state" / "health.json"
    health.SupervisorHealth(path).record_success()
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert (os.stat(path.parent).st_mode & 0o777) == 0o700


def test_publish_is_atomic_so_a_reader_never_sees_a_torn_file(tmp_path):
    path = tmp_path / "state" / "health.json"
    publisher = health.SupervisorHealth(path)
    publisher.record_success()
    first = json.loads(path.read_text(encoding="utf-8"))
    publisher.record_failure("api_unreachable")
    second = json.loads(path.read_text(encoding="utf-8"))
    assert first["consecutive_failures"] == 0
    assert second["consecutive_failures"] == 1
    # no temporary files left behind
    assert [p.name for p in path.parent.iterdir()] == ["health.json"]


# ---------------------------------------------------------------------------
# the loop's classification
# ---------------------------------------------------------------------------


def _supervisor(tmp_path, api):
    config = SupervisorConfig(
        base_url="http://finance-app:8777/api",
        credential="credential-value",
        workspace_root=str(tmp_path / "ws"),
        lease_owner="runner:1",
    )
    return HostedSupervisor(config, api=api, sleep=lambda _s: None)


class _DeniedApi:
    def list_jobs(self, **_kwargs):
        raise SupervisorApiError(401, "Supervisor authentication required", None)


class _UnreachableApi:
    def list_jobs(self, **_kwargs):
        raise SupervisorTransportError("connection refused")


class _IdleApi:
    def list_jobs(self, **_kwargs):
        return []


def test_an_authentication_rejection_is_reported_as_auth_failed(tmp_path):
    supervisor = _supervisor(tmp_path, _DeniedApi())

    result = supervisor.run_once()

    assert result["status"] == "api_denied"
    state = json.loads(supervisor.config.health_path().read_text(encoding="utf-8"))
    assert state["auth_failed"] is True
    assert state["last_error"] == "api_denied"
    # A fresh container is inside the startup grace, so the verdict is only
    # "unhealthy" once that grace has passed (a slow boot must not restart-loop).
    assert health.evaluate_health(state).reason == health.REASON_STARTING
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    # (a stalled loop is reported first, so give the loop a wide window here and
    # isolate the credential verdict)
    assert health.evaluate_health(
        state, now=later, startup_grace_s=60, loop_stale_after_s=600
    ).reason == health.REASON_AUTH_REJECTED


def test_transport_failure_is_reported_as_unreachable_not_denied(tmp_path):
    supervisor = _supervisor(tmp_path, _UnreachableApi())

    assert supervisor.run_once()["status"] == "api_unreachable"

    state = json.loads(supervisor.config.health_path().read_text(encoding="utf-8"))
    assert state["auth_failed"] is False
    assert state["consecutive_failures"] == 1


def test_an_idle_cycle_records_success(tmp_path):
    supervisor = _supervisor(tmp_path, _IdleApi())

    assert supervisor.run_once()["status"] == "idle"

    state = json.loads(supervisor.config.health_path().read_text(encoding="utf-8"))
    assert state["last_success_at"] is not None
    assert state["consecutive_failures"] == 0
    assert state["auth_failed"] is False


def test_a_failed_child_keeps_the_supervisor_healthy(tmp_path, monkeypatch):
    """A job that fails is the job's outcome; the loop still did its work."""

    class _JobApi:
        def list_jobs(self, **_kwargs):
            return [
                {
                    "job_id": "hsj_1",
                    "attempt": 1,
                    "lease_epoch": 1,
                    "strategy_id": "hs_1",
                }
            ]

        def claim(self, job_id, **kwargs):
            return {"lease_epoch": 2}

        def prepare(self, job_id, **kwargs):
            return {"source": "print('x')", "source_sha256": "0" * 64, "version_id": "v1"}

    supervisor = _supervisor(tmp_path, _JobApi())
    monkeypatch.setattr(
        supervisor, "_supervise", lambda entry: {"status": "failed", "job_id": entry["job_id"]}
    )

    result = supervisor.run_once()

    assert result["status"] == "failed"
    state = json.loads(supervisor.config.health_path().read_text(encoding="utf-8"))
    assert state["consecutive_failures"] == 0
    assert health.evaluate_health(state, now=datetime.now(timezone.utc)).healthy is True


def test_health_recording_never_breaks_supervision(tmp_path):
    supervisor = _supervisor(tmp_path, _IdleApi())

    class _Exploding:
        def record_success(self, **_kwargs):
            raise RuntimeError("disk full")

        def record_failure(self, *_args, **_kwargs):
            raise RuntimeError("disk full")

        def record_start(self, **_kwargs):
            raise RuntimeError("disk full")

    supervisor.health = _Exploding()
    assert supervisor.run_once()["status"] == "idle"  # no exception escapes
