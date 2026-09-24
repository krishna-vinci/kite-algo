"""Bounded background dispatcher for governed hosted execution (Phase 2).

A queued execution request has to become work without a human holding the HTTP
connection open, and without a test-only polling loop. This dispatcher is what
the app lifecycle starts: a bounded pass that (a) inspects claims abandoned by a
dead dispatcher and (b) claims and runs at most ``limit`` queued requests, with a
health snapshot published through the ordinary component-status surface.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from backend.strategies.execution_requests import (
    ExecutionRequestService,
    ExecutionRequestStateError,
)

#: Deployment switch. On by default: a governed request is inert until an owner
#: approves it (or an autonomous grant authorises it), so "no work" is the normal
#: state of a deployment with no governed requests.
DISPATCH_ENABLED_ENV = "HOSTED_EXECUTION_DISPATCH_ENABLED"

#: Bounded pass geometry.
DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_BATCH_LIMIT = 10
DEFAULT_CLAIM_TIMEOUT_SECONDS = 900

_FALSY = ("0", "false", "no", "off", "disabled")


def hosted_execution_dispatch_enabled(environ: Optional[Dict[str, str]] = None) -> bool:
    """False only for an explicit falsy spelling; anything else dispatches."""
    source = os.environ if environ is None else environ
    raw = str(source.get(DISPATCH_ENABLED_ENV, "") or "").strip().lower()
    return raw not in _FALSY


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HostedExecutionDispatcher:
    """One bounded pass at a time; no unbounded transaction, no blind replay."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        service: Optional[ExecutionRequestService] = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        limit: int = DEFAULT_BATCH_LIMIT,
        claim_timeout_seconds: int = DEFAULT_CLAIM_TIMEOUT_SECONDS,
        enabled: Optional[Callable[[], bool]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.session_factory = session_factory
        self._service = service
        self.interval_seconds = max(0.25, float(interval_seconds))
        self.limit = max(1, int(limit))
        self.claim_timeout_seconds = max(1, int(claim_timeout_seconds))
        self._enabled = enabled or hosted_execution_dispatch_enabled
        self._clock = clock or _utcnow
        self._state = "starting"
        self._last_pass_at: Optional[datetime] = None
        self._last_counts: Dict[str, Any] = {}
        self._last_error: Optional[str] = None

    @property
    def service(self) -> ExecutionRequestService:
        if self._service is None:
            self._service = ExecutionRequestService(self.session_factory)
        return self._service

    def health(self) -> Dict[str, Any]:
        return {
            "state": self._state,
            "enabled": bool(self._enabled()),
            "interval_seconds": self.interval_seconds,
            "limit": self.limit,
            "claim_timeout_seconds": self.claim_timeout_seconds,
            "last_pass_at": self._last_pass_at.isoformat() if self._last_pass_at else None,
            "last_counts": dict(self._last_counts),
            "last_error": self._last_error,
        }

    def _note(self, **fields: Any) -> None:
        if "state" in fields:
            self._state = str(fields["state"])
        if "last_error" in fields:
            self._last_error = fields["last_error"]

    async def poll_once(self) -> Dict[str, Any]:
        """One bounded pass. Never raises: a degraded pass is reported."""
        counts: Dict[str, Any] = {
            "recovered_submitted": 0,
            "recovered_unresolved": 0,
            "claimed": 0,
            "executed": 0,
            "refused": 0,
            "unresolved": 0,
            "stale": 0,
            "errors": 0,
        }
        if not self._enabled():
            counts["disabled"] = True
            self._last_pass_at = self._clock()
            self._last_counts = counts
            self._state = "disabled"
            return counts
        moment = self._clock()
        try:
            recovery = self.service.recover_abandoned_claims(
                timeout_seconds=self.claim_timeout_seconds, now=moment
            )
            counts["recovered_submitted"] = int(recovery.get("proved_submitted") or 0)
            counts["recovered_unresolved"] = int(recovery.get("unresolved") or 0)
        except Exception as exc:  # noqa: BLE001 - recovery is retried next pass
            counts["errors"] += 1
            counts["recovery_error"] = str(exc)

        try:
            claimed = self.service.claim_next(limit=self.limit, now=moment)
        except Exception as exc:  # noqa: BLE001 - one bad claim pass is not fatal
            counts["errors"] += 1
            counts["claim_error"] = str(exc)
            claimed = []
        counts["claimed"] = len(claimed)

        for row in claimed:
            request_id = str(row.get("request_id") or "")
            claim_id = str(row.get("dispatch_claim_id") or "") or None
            try:
                result = await self.service.dispatch(
                    request_id, claim_id=claim_id, now=self._clock()
                )
            except ExecutionRequestStateError as exc:
                # This worker's claim lost (another claim, recovery, or a
                # terminal state). Its finish was refused by the CAS, so nothing
                # was overwritten: the row's own answer stands.
                counts["stale"] += 1
                counts["stale_detail"] = str(exc.detail)
                continue
            except Exception as exc:  # noqa: BLE001 - unknown outcome stays unresolved
                counts["errors"] += 1
                try:
                    self.service.finish(
                        request_id,
                        status="dispatch_unresolved",
                        refusal_code="EXECUTION_OUTCOME_UNKNOWN",
                        detail={"message": str(exc), "stage": "dispatch"},
                        claim_id=claim_id,
                        expected_status="dispatching",
                    )
                except ExecutionRequestStateError:
                    counts["stale"] += 1
                except Exception:  # noqa: BLE001 - reported below, never silent
                    counts.setdefault("unrecorded", []).append(request_id)
                continue
            status = str(result.get("status") or "")
            if status == "executed":
                counts["executed"] += 1
            elif status == "dispatch_unresolved":
                counts["unresolved"] += 1
            elif status == "refused":
                counts["refused"] += 1

        self._last_pass_at = self._clock()
        self._last_counts = counts
        self._note(state="degraded" if counts["errors"] else "ok")
        if counts["errors"]:
            self._note(
                last_error="; ".join(
                    str(counts[key])
                    for key in ("recovery_error", "claim_error")
                    if counts.get(key)
                )
                or "dispatch error"
            )
        else:
            self._note(last_error=None)
        return counts

    async def run_forever(
        self, *, health_sink: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> None:
        """Poll until cancelled; the handler covers the idle sleep too."""
        self._note(state="ok" if self._enabled() else "disabled")
        try:
            while True:
                try:
                    await self.poll_once()
                except Exception as exc:  # noqa: BLE001 - one bad pass never kills the loop
                    self._note(state="degraded", last_error=str(exc))
                if health_sink is not None:
                    try:
                        health_sink(self.health())
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            self._note(state="stopped")
            raise
