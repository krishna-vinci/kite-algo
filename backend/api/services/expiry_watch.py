"""Periodic expiry watch for open hosted option runs and futures rolls.

Two escalations, and neither one closes anything on its own initiative:

* **Warning** - once per day, inside the window - is a timeline event plus a
  notification to the owner. It never mutates a position.
* **Cutoff** - options only, on expiry day itself, past a configurable IST
  time, and only for a structure whose FROZEN policy is
  ``exit_before_cutoff`` - submits the run's own staged exit through the
  existing protection structure-exit path. Rolling is never attempted here:
  that stays the strategy's own decision, so the only actions this module can
  take are "warn" and "exit what the structure's own policy already committed
  to exiting."

State is durable in the worker run's own ``runtime_state`` (``expiry_watch_state``
for option runs) so a restart does not repeat a warning already sent today, or
resubmit a cutoff exit already claimed.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from backend.options.protection.expiry_policy import days_to_expiry
from backend.options.protection.ownership import TERMINAL_RUN_STATUSES

#: Default cutoff, IST, matching the reference doc: 14:45.
DEFAULT_CUTOFF_TIME_IST = "14:45"
_IST_OFFSET = timedelta(hours=5, minutes=30)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def cutoff_time_ist() -> str:
    raw = str(os.environ.get("OPTION_EXPIRY_CUTOFF_TIME_IST") or "").strip()
    return raw or DEFAULT_CUTOFF_TIME_IST


def _parse_hhmm(value: str) -> Optional[tuple]:
    try:
        hour_str, minute_str = str(value).strip().split(":", 1)
        return int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        return None


def is_past_cutoff_ist(now: datetime, *, cutoff: str) -> bool:
    """Whether ``now`` (any tz) is at or past ``cutoff`` (``HH:MM``) IST."""

    parsed = _parse_hhmm(cutoff)
    if parsed is None:
        parsed = _parse_hhmm(DEFAULT_CUTOFF_TIME_IST)
    hour, minute = parsed
    moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    ist = moment.astimezone(timezone.utc) + _IST_OFFSET
    return (ist.hour, ist.minute) >= (hour, minute)


class ExpiryWatchService:
    """Evaluate every open option run (and futures roll) for its expiry deadline.

    Every collaborator is injected so the decision logic here never touches a
    database or a broker directly: production wiring (``backend/app/background.py``)
    supplies the real repo, the real staged-exit submitter and the real
    notifier; tests supply fakes.
    """

    def __init__(
        self,
        *,
        repo: Any,
        roll_lister: Optional[Callable[[], Awaitable[List[Dict[str, Any]]]]] = None,
        roll_escalator: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = None,
        structure_exit_submitter: Optional[
            Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]
        ] = None,
        notify_option_run: Optional[
            Callable[..., Awaitable[bool]]
        ] = None,
        publish_timeline: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        now_fn: Callable[[], datetime] = _utcnow,
        cutoff_time: Optional[str] = None,
    ) -> None:
        self.repo = repo
        self.roll_lister = roll_lister
        self.roll_escalator = roll_escalator
        self.structure_exit_submitter = structure_exit_submitter
        self.notify_option_run = notify_option_run
        self.publish_timeline = publish_timeline
        self.now_fn = now_fn
        self.cutoff_time = cutoff_time or cutoff_time_ist()

    async def evaluate_once(self) -> Dict[str, int]:
        evaluated = 0
        warned = 0
        escalated = 0
        errors = 0

        owner_runs = await self.repo.list_protection_owners()
        for run in list(owner_runs or []):
            evaluated += 1
            try:
                outcome = await self._evaluate_option_run(dict(run))
                warned += int(outcome.get("warned") or 0)
                escalated += int(outcome.get("escalated") or 0)
            except Exception:  # noqa: BLE001 - one run's failure never stalls the rest
                errors += 1

        if self.roll_lister is not None:
            rolls = await self.roll_lister()
            for roll in list(rolls or []):
                evaluated += 1
                try:
                    did_warn = await self._evaluate_futures_roll(dict(roll))
                    warned += int(bool(did_warn))
                except Exception:  # noqa: BLE001
                    errors += 1

        return {"evaluated": evaluated, "warned": warned, "escalated": escalated, "errors": errors}

    # -- options --------------------------------------------------------

    async def _evaluate_option_run(self, run: Dict[str, Any]) -> Dict[str, int]:
        owner = dict(run.get("protection_owner") or {})
        option_status = str(owner.get("option_run_status") or "")
        if option_status in TERMINAL_RUN_STATUSES:
            return {"warned": 0, "escalated": 0}

        policy = dict(owner.get("policy") or {})
        expiry = policy.get("expiry")
        if not expiry:
            return {"warned": 0, "escalated": 0}
        policy_name = str(policy.get("expiry_policy") or "exit_before_cutoff")

        now = self.now_fn()
        remaining = days_to_expiry(expiry, now=now)
        if remaining is None:
            return {"warned": 0, "escalated": 0}

        strategy_run_id = str(run.get("strategy_run_id") or "")
        runtime_state = dict(run.get("runtime_state") or {})
        watch_state = dict(runtime_state.get("expiry_watch_state") or {})
        today = now.date().isoformat()
        outcome = {"warned": 0, "escalated": 0}
        changed = False

        # (a) Warn once per day, expiry day and T-1 only.
        if 0 <= remaining <= 1 and watch_state.get("last_warned_date") != today:
            await self._emit_option_warning(
                run, remaining=remaining, expiry=expiry, policy_name=policy_name
            )
            watch_state["last_warned_date"] = today
            changed = True
            outcome["warned"] = 1

        # (b) Escalate on expiry day itself, past cutoff, ONLY for a structure
        # frozen with exit_before_cutoff, and only once.
        if (
            remaining <= 0
            and policy_name == "exit_before_cutoff"
            and not watch_state.get("cutoff_submitted")
            and is_past_cutoff_ist(now, cutoff=self.cutoff_time)
        ):
            await self._emit_cutoff_escalation(run, expiry=expiry)
            # Claimed BEFORE the submit call: a crash mid-submit must not retry
            # forever, and the underlying staged-exit protocol is itself the
            # thing that resumes an incomplete stage - not this loop re-firing.
            watch_state["cutoff_submitted"] = True
            watch_state["cutoff_claimed_at"] = now.isoformat()
            changed = True
            submit_state = {
                "status": "triggered",
                "triggered_rule": "expiry_cutoff",
                "exit_idempotency_key": f"expiry-cutoff:{strategy_run_id}",
            }
            if self.structure_exit_submitter is not None:
                try:
                    result = await self.structure_exit_submitter(run, submit_state)
                except Exception as exc:  # noqa: BLE001 - recorded, never raised
                    result = {"submitted": False, "reason": "error", "error": str(exc)}
            else:
                result = {"submitted": False, "reason": "not_wired"}
            watch_state["cutoff_exit_result"] = {
                "submitted": bool(result.get("submitted")),
                "reason": str(result.get("reason") or ""),
            }
            outcome["escalated"] = 1

        if changed:
            runtime_state["expiry_watch_state"] = watch_state
            await self.repo.update_run_runtime_state(strategy_run_id, runtime_state)

        return outcome

    async def _emit_option_warning(
        self, run: Dict[str, Any], *, remaining: int, expiry: Any, policy_name: str
    ) -> None:
        strategy_run_id = str(run.get("strategy_run_id") or "")
        await self._publish(
            {
                "event_kind": "expiry_watch",
                "event_source": "expiry_watch",
                "event_type": "EXPIRY_WARNING",
                "related_resource_type": "strategy_run",
                "related_resource_id": strategy_run_id,
                "strategy_run_id": strategy_run_id,
                "summary": "Option structure approaching expiry",
                "payload": {
                    "days_to_expiry": int(remaining),
                    "expiry": str(expiry),
                    "expiry_policy": policy_name,
                },
            }
        )
        if self.notify_option_run is not None:
            try:
                await self.notify_option_run(
                    run=run,
                    text=(
                        f"Option structure {strategy_run_id} expires on {expiry} "
                        f"({int(remaining)} day(s) away, policy {policy_name})."
                    ),
                    subject="Option expiry approaching",
                    idempotency_key=f"expiry-warning:{strategy_run_id}:{self.now_fn().date().isoformat()}",
                )
            except Exception:  # noqa: BLE001 - notification failure never blocks the watch
                pass

    async def _emit_cutoff_escalation(self, run: Dict[str, Any], *, expiry: Any) -> None:
        strategy_run_id = str(run.get("strategy_run_id") or "")
        await self._publish(
            {
                "event_kind": "expiry_watch",
                "event_source": "expiry_watch",
                "event_type": "EXPIRY_CUTOFF_REACHED",
                "related_resource_type": "strategy_run",
                "related_resource_id": strategy_run_id,
                "strategy_run_id": strategy_run_id,
                "summary": "Option expiry cutoff reached; submitting staged exit",
                "payload": {"expiry": str(expiry), "cutoff_time_ist": self.cutoff_time},
            }
        )
        if self.notify_option_run is not None:
            try:
                await self.notify_option_run(
                    run=run,
                    text=(
                        f"Option structure {strategy_run_id} reached its expiry cutoff "
                        f"({self.cutoff_time} IST); its staged exit is being submitted."
                    ),
                    subject="Option expiry cutoff reached",
                    idempotency_key=f"expiry-cutoff:{strategy_run_id}",
                )
            except Exception:  # noqa: BLE001
                pass

    async def _publish(self, row: Dict[str, Any]) -> None:
        if self.publish_timeline is None:
            return
        try:
            await self.publish_timeline(row)
        except Exception:  # noqa: BLE001 - the watch itself must not fail on this
            pass

    # -- futures ----------------------------------------------------------

    async def _evaluate_futures_roll(self, roll: Dict[str, Any]) -> bool:
        if self.roll_escalator is None:
            return False
        return bool(await self.roll_escalator(roll))


def build_roll_escalator(
    machine: Any, *, now_fn: Callable[[], datetime] = _utcnow
) -> Callable[[Dict[str, Any]], Awaitable[bool]]:
    """A once-per-day wrapper around the existing, frozen ``check_expiry_cutoff``.

    ``check_expiry_cutoff`` itself has no daily gate - it escalates (and
    notifies) on every call inside the window - so the gate lives here, read
    from the roll's OWN append-only event log rather than new state: an
    ``escalated`` event already recorded today means the owner was already told
    today, and a second notification would only teach them to ignore it.
    """

    from backend.strategies.expiry_policy import check_expiry_cutoff

    async def _escalate(roll: Dict[str, Any]) -> bool:
        roll_id = str(roll.get("roll_id") or "")
        if not roll_id:
            return False
        now = now_fn()
        today = now.date().isoformat()
        events = await asyncio.to_thread(machine.events, roll_id)
        for event in events:
            if str(event.get("event") or "") != "escalated":
                continue
            created_at = event.get("created_at")
            stamp = created_at.date().isoformat() if isinstance(created_at, datetime) else None
            if stamp == today:
                return False
        result = await asyncio.to_thread(
            check_expiry_cutoff, machine, roll_id, now=now, notify=True
        )
        return bool(result.get("escalated"))

    return _escalate
