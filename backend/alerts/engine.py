"""Trigger semantics engine: decide whether a fired rule emits a signal.

Implements spec v2 §5 and §6 E-9: triggers ``once`` / ``on_transition`` /
``once_per_session`` / ``reminder``, cooldown (suppresses delivery, never
state), rearm gating, expiry, quiet-session gating and the
notify-if-already-true opt-in.

``once_per_session`` identity is supplied per evaluation via ``session_id``
(the caller resolves the trading session for the observation); the first
emit of a session is recorded in ``state["last_session"]`` and later fires
in the same session are suppressed until the session id changes.

``reminder`` alerts emit not only on transitions (``fired``) but also while
a level condition merely HOLDS (``matched is True``), re-emitting at most
once per ``reminder_interval_s``. ``matched`` is therefore part of the
pinned signature; other triggers ignore it.

``decide`` is pure: the input ``state`` dict is never mutated; a new state
dict is always returned. All state values are JSON-serializable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.alerts.types import AlertSpec

__all__ = ["EngineDecision", "decide"]


@dataclass(frozen=True)
class EngineDecision:
    emit: bool                       # create signal event?
    suppression_reason: Optional[str]  # cooldown | not_armed | already_fired |
                                     # session_fired | reminder_interval | quiet_session |
                                     # expired | already_true_at_activation | None
    rule_completed: bool             # trigger == "once" and fired (emitted or state-advanced)
    new_state: dict


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_dt(value) -> Optional[datetime]:
    """Parse an ISO-8601 string (or datetime) robustly; naive means UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)


def decide(
    alert: AlertSpec,
    fired: bool,
    state: dict,
    now: datetime,
    session_active: bool = True,
    current_value: Optional[float] = None,
    already_true: Optional[bool] = None,
    session_id: Optional[str] = None,
    matched: Optional[bool] = None,
) -> EngineDecision:
    """Decide emission for one evaluation of an alert rule.

    ``current_value``: the operand value used for rearming (required only when
    the alert configures ``rearm_level``).
    ``already_true``: whether the stage condition already matched on this
    observation (used for the E-9 activation decision on the first call).
    ``session_id``: the trading-session identity of this observation, used by
    ``once_per_session`` (a new id re-arms the alert).
    ``matched``: whether the stage condition holds on this observation; only
    ``reminder`` triggers use it (a held level re-emits after the interval).
    """
    new_state = dict(state)
    now = _aware(now)

    # 1. Expiry suppresses regardless of everything else.
    expires_at = _parse_dt(alert.expires_at)
    if expires_at is not None and now >= expires_at:
        return EngineDecision(False, "expired", False, new_state)

    # 2. Quiet session: no state advance at all.
    if not session_active:
        return EngineDecision(False, "quiet_session", False, new_state)

    # 3. Activation decision (E-9): first decision for this subscription.
    if not new_state.get("initialized"):
        new_state["initialized"] = True
        if already_true:
            if not alert.notify_if_already_true:
                return EngineDecision(False, "already_true_at_activation", False, new_state)
            fired = True  # opt-in: activation match counts as a fire

    # Reminder alerts are due on a real transition OR while the condition
    # merely holds; every other trigger only advances on `fired`.
    due = fired or (alert.trigger == "reminder" and matched is True)

    # 4. Rearm gating: after an emit the rule is disarmed until the value
    #    passes rearm_level from the configured side.
    armed = new_state.get("armed", True)
    if not armed and alert.rearm_level is not None and current_value is not None:
        direction = alert.rearm_direction or "below"
        rearm_hit = (
            current_value <= alert.rearm_level
            if direction == "below"
            else current_value >= alert.rearm_level
        )
        if rearm_hit:
            armed = True
            new_state["armed"] = True
    if due and not armed:
        return EngineDecision(False, "not_armed", False, new_state)

    if not due:
        return EngineDecision(False, None, False, new_state)

    # 5. Trigger gating + bookkeeping (cooldown suppresses delivery, not state,
    #    so bookkeeping is applied before the cooldown check).
    rule_completed = False
    if alert.trigger == "once":
        if new_state.get("fired_once"):
            return EngineDecision(False, "already_fired", False, new_state)
        new_state["fired_once"] = True
        rule_completed = True
    elif alert.trigger == "once_per_session":
        if session_id is not None:
            if new_state.get("last_session") == session_id:
                return EngineDecision(False, "session_fired", False, new_state)
            new_state["last_session"] = session_id
    elif alert.trigger == "reminder":
        last_emitted = _parse_dt(new_state.get("last_emitted_ts"))
        interval = alert.reminder_interval_s or 0
        if last_emitted is not None and now < last_emitted + timedelta(seconds=interval):
            return EngineDecision(False, "reminder_interval", False, new_state)
    # "on_transition": emits on every fired observation.

    # 6. Cooldown gates delivery only.
    if alert.cooldown_s:
        cooldown_until = _parse_dt(new_state.get("cooldown_until"))
        if cooldown_until is not None and now < cooldown_until:
            return EngineDecision(False, "cooldown", rule_completed, new_state)

    # 7. Emit and advance delivery bookkeeping.
    new_state["last_emitted_ts"] = now.isoformat()
    if alert.cooldown_s:
        new_state["cooldown_until"] = (now + timedelta(seconds=alert.cooldown_s)).isoformat()
    if alert.rearm_level is not None:
        new_state["armed"] = False

    return EngineDecision(True, None, rule_completed, new_state)
