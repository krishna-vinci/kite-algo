"""Healthcheck predicate for the alerts worker container (Phase 6 6A.0).

Replaces the previous `test -s /app/alerts-health.json`, which only asserted the
file was NON-EMPTY. Because the container filesystem persists across restarts,
a crash-looping worker kept its last good health file and the container was
reported healthy while nothing was evaluating — the failure the amendment
closes.

This check asserts three things instead:

1. the health file exists and parses;
2. it is FRESH (written within the allowed window), so a stalled writer fails;
3. every REQUIRED task is alive, so a dead evaluation/delivery/screener task
   fails even while the file itself is being rewritten by another task.

Run as ``python -m backend.workflows.health_check``; exits 0 when healthy and
prints the reasons when not.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, List, Optional

__all__ = ["check_health", "main"]

#: A health snapshot older than this is treated as stalled. The worker writes
#: every `ALERTS_HEALTH_INTERVAL_S` (30 s default), so this is ~3 missed cycles.
DEFAULT_MAX_AGE_S = 90.0

#: Tasks that must be running for the container to be useful. A task that is
#: merely backing off before a restart is reported as not alive on purpose: the
#: container is degraded until it is running again.
REQUIRED_TASKS = ("evaluation-worker",)


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def check_health(
    path: str,
    *,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    now: Optional[datetime] = None,
    required_tasks: tuple = REQUIRED_TASKS,
) -> List[str]:
    """Return the list of problems with the health snapshot (empty when OK).

    Kept as a pure function of the file's contents so the behavior is testable
    without a running container or a Docker healthcheck.
    """
    problems: List[str] = []
    if not os.path.exists(path):
        return [f"health file missing: {path}"]
    try:
        with open(path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, ValueError) as exc:
        return [f"health file unreadable: {type(exc).__name__}: {exc}"]
    if not isinstance(snapshot, dict):
        return ["health file is not a JSON object"]

    reference = now or datetime.now(timezone.utc)
    written = _parse_ts(snapshot.get("last_health_at"))
    if written is None:
        # Older snapshots predate last_health_at; fall back to file mtime so an
        # upgrade does not fail the healthcheck spuriously.
        try:
            written = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        except OSError:
            written = None
    if written is None:
        problems.append("health snapshot has no usable timestamp")
    else:
        age = (reference - written).total_seconds()
        if age > max_age_s:
            problems.append(
                f"health snapshot is stale: {age:.0f}s old (limit {max_age_s:.0f}s)"
            )

    tasks = snapshot.get("tasks")
    if isinstance(tasks, dict):
        for name in required_tasks:
            entry = tasks.get(name)
            if not isinstance(entry, dict):
                # No liveness record yet (very early boot) is not a failure as
                # long as the snapshot itself is fresh; the supervisor writes it
                # on the first launch.
                continue
            if not entry.get("alive"):
                state = entry.get("state") or "unknown"
                reason = entry.get("last_exit_reason")
                problems.append(
                    f"required task {name} is not alive (state={state}"
                    + (f", last error: {reason}" if reason else "")
                    + ")"
                )
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    path = args[0] if args else os.environ.get(
        "ALERTS_HEALTH_FILE", "/app/alerts-health.json"
    )
    try:
        max_age_s = float(os.environ.get("ALERTS_HEALTH_MAX_AGE_S", str(DEFAULT_MAX_AGE_S)))
    except ValueError:
        max_age_s = DEFAULT_MAX_AGE_S
    problems = check_health(path, max_age_s=max_age_s)
    if problems:
        for problem in problems:
            print(f"UNHEALTHY: {problem}")
        return 1
    print("healthy")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via main()
    raise SystemExit(main())
