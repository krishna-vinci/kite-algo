"""Read-only health for the hosted-strategy supervisor (strategy-runner).

Docker cannot ask a process "are you well?" without a contract, and the runner
had none: the container stayed `starting`/unhealthy-ish even while it was
functionally supervising jobs. This module is that contract, in two halves:

* :class:`SupervisorHealth` — the loop publishes a small, **nonsecret** snapshot
  after every cycle (atomic replace, so a reader never sees a torn file):
  liveness, when the control plane was last reached, the last failure reason
  code, consecutive failures, and how many children are running.
* :func:`evaluate_health` / ``python -m backend.strategies.supervisor_health`` —
  a *read-only* verdict over that file. It performs no API call, claims no job,
  renews no lease, mutates no lifecycle state, needs no database or broker
  credential, and never reads or prints the supervisor credential.

Verdict rules (all windows configurable):

* health file missing/unreadable/invalid → unhealthy (nothing is known);
* within the startup grace after the loop's first start → healthy ("starting"),
  so a slow boot does not restart-loop the container;
* the loop's last completed cycle older than ``loop_stale_after_s`` → unhealthy
  (stalled loop, e.g. a hung child or a blocked call);
* an **authentication** failure (401/403 from the lifecycle API) → unhealthy as
  soon as the grace has passed: a wrong credential never heals itself;
* the control plane unreachable for longer than
  ``control_plane_stale_after_s`` → unhealthy (persistent unavailability), while
  a shorter outage stays healthy so a network blip does not cause restart
  thrashing;
* a failed **child** is NOT a supervisor failure: the job's own state carries
  that outcome (``active_children`` is informational), and the loop is expected
  to keep supervising.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

HEALTH_FILE_ENV = "HOSTED_SUPERVISOR_HEALTH_FILE"
DEFAULT_HEALTH_FILE = "state/health.json"

STARTUP_GRACE_ENV = "HOSTED_SUPERVISOR_HEALTH_STARTUP_GRACE_S"
LOOP_STALE_ENV = "HOSTED_SUPERVISOR_HEALTH_LOOP_STALE_S"
CONTROL_PLANE_STALE_ENV = "HOSTED_SUPERVISOR_HEALTH_CONTROL_PLANE_STALE_S"

DEFAULT_STARTUP_GRACE_S = 60.0
DEFAULT_LOOP_STALE_S = 45.0
DEFAULT_CONTROL_PLANE_STALE_S = 120.0

#: Reason codes. Stable strings: the checker prints them, tests assert them.
REASON_OK = "ok"
REASON_STARTING = "starting"
REASON_FILE_ABSENT = "health_file_absent"
REASON_FILE_UNREADABLE = "health_file_unreadable"
REASON_FILE_INVALID = "health_file_invalid"
REASON_LOOP_STALLED = "loop_stalled"
REASON_AUTH_REJECTED = "auth_rejected"
REASON_CONTROL_PLANE_UNAVAILABLE = "control_plane_unavailable"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class HealthVerdict:
    healthy: bool
    reason: str
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {"healthy": self.healthy, "reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        return payload

    def line(self) -> str:
        return f"{'healthy' if self.healthy else 'unhealthy'}: {self.reason}" + (
            f" ({self.detail})" if self.detail else ""
        )


class SupervisorHealth:
    """Publish the loop's health snapshot. Never contains the credential."""

    def __init__(
        self,
        path: Path,
        *,
        component: str = "hosted-supervisor",
        clock=_utcnow,
    ) -> None:
        self.path = Path(path)
        self.component = component
        self._clock = clock
        self._state: Dict[str, Any] = {
            "component": component,
            "pid": os.getpid(),
            "started_at": self._clock().isoformat(),
            "cycle_count": 0,
            "last_cycle_at": None,
            "last_success_at": None,
            "last_error": None,
            "consecutive_failures": 0,
            "auth_failed": False,
            "active_children": 0,
            "lease_owner": None,
        }
        self._published_once = False

    # -- state -----------------------------------------------------------

    def record_start(self, *, lease_owner: Optional[str] = None) -> None:
        self._state["started_at"] = self._clock().isoformat()
        self._state["lease_owner"] = lease_owner
        self.publish()

    def record_success(self, *, active_children: Optional[int] = None) -> None:
        now = self._clock().isoformat()
        self._state.update(
            {
                "cycle_count": int(self._state.get("cycle_count") or 0) + 1,
                "last_cycle_at": now,
                "last_success_at": now,
                "last_error": None,
                "consecutive_failures": 0,
                "auth_failed": False,
            }
        )
        if active_children is not None:
            self._state["active_children"] = int(active_children)
        self.publish()

    def record_failure(
        self,
        reason: str,
        *,
        auth_failed: bool = False,
        active_children: Optional[int] = None,
    ) -> None:
        self._state.update(
            {
                "cycle_count": int(self._state.get("cycle_count") or 0) + 1,
                "last_cycle_at": self._clock().isoformat(),
                "last_error": str(reason)[:200],
                "consecutive_failures": int(self._state.get("consecutive_failures") or 0) + 1,
                "auth_failed": bool(auth_failed),
            }
        )
        if active_children is not None:
            self._state["active_children"] = int(active_children)
        self.publish()

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._state)

    # -- publication -----------------------------------------------------

    def publish(self) -> None:
        """Atomic write: a reader sees the previous or the new file, never half."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:  # pragma: no cover - best effort on odd filesystems
                pass
            payload = json.dumps(self._state, sort_keys=True)
            handle, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".health-")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._published_once = True
        except Exception:  # pragma: no cover - health must never break the loop
            import logging

            logging.getLogger(__name__).warning("supervisor_health_publish_failed", exc_info=True)


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------


def health_file_path(environ: Optional[Dict[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get(HEALTH_FILE_ENV)
    if explicit:
        return Path(explicit)
    workspace = env.get("HOSTED_SUPERVISOR_WORKSPACE", "supervisor-workspace")
    return Path(workspace) / DEFAULT_HEALTH_FILE


def evaluate_health(
    state: Optional[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    startup_grace_s: float = DEFAULT_STARTUP_GRACE_S,
    loop_stale_after_s: float = DEFAULT_LOOP_STALE_S,
    control_plane_stale_after_s: float = DEFAULT_CONTROL_PLANE_STALE_S,
    unreadable_reason: Optional[str] = None,
) -> HealthVerdict:
    """The verdict for one published snapshot (``None`` means unreadable)."""
    moment = now or _utcnow()
    if state is None:
        return HealthVerdict(False, unreadable_reason or REASON_FILE_ABSENT)

    started_at = _parse(state.get("started_at"))
    last_cycle = _parse(state.get("last_cycle_at"))
    last_success = _parse(state.get("last_success_at"))
    consecutive = int(state.get("consecutive_failures") or 0)
    auth_failed = bool(state.get("auth_failed"))

    if started_at is not None and (moment - started_at).total_seconds() < startup_grace_s:
        # A brand-new loop is allowed to be imperfect: it has not had time to
        # reach the control plane, and restarting it would only delay that.
        return HealthVerdict(
            True, REASON_STARTING, f"grace {int(startup_grace_s)}s"
        )

    if last_cycle is not None and (moment - last_cycle).total_seconds() > loop_stale_after_s:
        return HealthVerdict(
            False,
            REASON_LOOP_STALLED,
            f"last cycle {(moment - last_cycle).total_seconds():.0f}s ago",
        )

    if auth_failed and consecutive > 0:
        # Permanent by nature: the credential will not fix itself, and every
        # cycle keeps failing the same way.
        return HealthVerdict(False, REASON_AUTH_REJECTED, str(state.get("last_error") or ""))

    if last_success is None:
        if last_cycle is None:
            return HealthVerdict(True, REASON_STARTING)
        # Cycles are happening but never reached the control plane.
        if consecutive > 0:
            return HealthVerdict(
                False,
                REASON_CONTROL_PLANE_UNAVAILABLE,
                f"{consecutive} failed cycle(s), never reached it",
            )
        return HealthVerdict(True, REASON_STARTING)

    silence = (moment - last_success).total_seconds()
    if silence > control_plane_stale_after_s and consecutive > 0:
        return HealthVerdict(
            False,
            REASON_CONTROL_PLANE_UNAVAILABLE,
            f"no successful contact for {silence:.0f}s",
        )

    return HealthVerdict(True, REASON_OK)


def read_state(path: Path) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, REASON_FILE_ABSENT
    except OSError:
        return None, REASON_FILE_UNREADABLE
    try:
        payload = json.loads(raw)
    except ValueError:
        return None, REASON_FILE_INVALID
    if not isinstance(payload, dict):
        return None, REASON_FILE_INVALID
    return payload, None


def check(
    *,
    path: Optional[Path] = None,
    environ: Optional[Dict[str, str]] = None,
    now: Optional[datetime] = None,
) -> HealthVerdict:
    """Read the published snapshot and return its verdict (no side effects)."""
    env = os.environ if environ is None else environ
    target = path or health_file_path(env)

    def _f(name: str, default: float) -> float:
        value = env.get(name)
        try:
            return float(value) if value else default
        except ValueError:
            return default

    state, unreadable = read_state(target)
    return evaluate_health(
        state,
        now=now,
        startup_grace_s=_f(STARTUP_GRACE_ENV, DEFAULT_STARTUP_GRACE_S),
        loop_stale_after_s=_f(LOOP_STALE_ENV, DEFAULT_LOOP_STALE_S),
        control_plane_stale_after_s=_f(CONTROL_PLANE_STALE_ENV, DEFAULT_CONTROL_PLANE_STALE_S),
        unreadable_reason=unreadable,
    )


def main(argv: Optional[list] = None) -> int:
    """Entry point: 0 healthy, 1 unhealthy (Docker healthcheck convention)."""
    _ = argv
    verdict = check()
    print(verdict.line())
    return 0 if verdict.healthy else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
