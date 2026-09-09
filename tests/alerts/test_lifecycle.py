"""Trigger semantics engine — plan Task 3, spec v2 §5, §6 E-9/E-10.

Pure unit tests: no DB, no redis, no network, no backend imports beyond
``backend.alerts``.
"""

from datetime import datetime, timedelta, timezone

from backend.alerts.engine import decide
from backend.alerts.predicates import Observation, evaluate_stage
from backend.alerts.types import AlertSpec, Condition, Operand, Stage

UTC = timezone.utc
T0 = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)


def alert(**kw):
    base = {"id": "breakout", "source": "px"}
    base.update(kw)
    return AlertSpec(**base)


def later(seconds):
    return T0 + timedelta(seconds=seconds)


# --- trigger: once ---------------------------------------------------------


def test_once_fires_once_then_suppressed_as_already_fired():
    a = alert(trigger="once")
    d1 = decide(a, True, {}, T0)
    assert d1.emit is True
    assert d1.rule_completed is True
    assert d1.suppression_reason is None
    assert d1.new_state["fired_once"] is True
    d2 = decide(a, True, d1.new_state, later(1))
    assert d2.emit is False
    assert d2.suppression_reason == "already_fired"
    assert d2.rule_completed is False


# --- trigger: on_transition ------------------------------------------------


def test_on_transition_emits_every_fire():
    a = alert()  # default trigger
    state = {}
    for i in range(3):
        d = decide(a, True, state, later(i))
        assert d.emit is True
        assert d.suppression_reason is None
        assert d.rule_completed is False
        state = d.new_state


def test_no_fire_means_no_emit_and_no_reason():
    a = alert()
    d = decide(a, False, {}, T0)
    assert d.emit is False
    assert d.suppression_reason is None
    assert d.rule_completed is False


# --- trigger: once_per_session ----------------------------------------------


def test_once_per_session_suppresses_same_session_emits_next():
    a = alert(trigger="once_per_session")
    d1 = decide(a, True, {}, T0, session_id="2026-09-08")
    assert d1.emit is True
    assert d1.new_state["last_session"] == "2026-09-08"
    d2 = decide(a, True, d1.new_state, later(1), session_id="2026-09-08")
    assert d2.emit is False
    assert d2.suppression_reason == "session_fired"
    d3 = decide(a, True, d1.new_state, later(2), session_id="2026-09-09")
    assert d3.emit is True
    assert d3.new_state["last_session"] == "2026-09-09"


def test_once_per_session_re_fires_when_session_id_changes():
    a = alert(trigger="once_per_session")
    state = {}
    sessions = ["s1", "s1", "s1", "s2", "s2"]
    emits = []
    for i, sid in enumerate(sessions):
        d = decide(a, True, state, later(i), session_id=sid)
        state = d.new_state
        emits.append(d.emit)
    assert emits == [True, False, False, True, False]


def test_once_per_session_without_session_id_never_gates():
    a = alert(trigger="once_per_session")
    d1 = decide(a, True, {}, T0, session_id=None)
    d2 = decide(a, True, d1.new_state, later(1), session_id=None)
    assert d1.emit is True
    assert d2.emit is True  # no session identity: cannot gate


# --- trigger: reminder -------------------------------------------------------


def test_reminder_re_emits_only_after_interval():
    a = alert(trigger="reminder", reminder_interval_s=300)
    d1 = decide(a, True, {}, T0)
    assert d1.emit is True
    d2 = decide(a, True, d1.new_state, later(299))
    assert d2.emit is False
    assert d2.suppression_reason == "reminder_interval"
    d3 = decide(a, True, d2.new_state, later(300))  # boundary: now >= last + interval
    assert d3.emit is True


def test_reminder_parses_last_emitted_iso_robustly():
    a = alert(trigger="reminder", reminder_interval_s=300)
    state = {"last_emitted_ts": "2026-09-08T09:55:00Z"}  # Z suffix
    d = decide(a, True, state, T0)  # exactly 300s later
    assert d.emit is True
    state2 = {"last_emitted_ts": "2026-09-08T09:56:00+00:00"}  # offset form
    d2 = decide(a, True, state2, T0)
    assert d2.emit is False
    assert d2.suppression_reason == "reminder_interval"


def test_reminder_re_emits_while_level_condition_merely_holds():
    # Fault 3: level ops never produce fired, so a reminder alert must be
    # driven by matched=True too — emit at t0, again only after the interval.
    a = alert(trigger="reminder", reminder_interval_s=300)
    d1 = decide(a, False, {}, T0, matched=True)  # holding, never fired
    assert d1.emit is True
    assert d1.new_state["last_emitted_ts"] == T0.isoformat()
    d2 = decide(a, False, d1.new_state, later(299), matched=True)
    assert d2.emit is False
    assert d2.suppression_reason == "reminder_interval"
    d3 = decide(a, False, d2.new_state, later(300), matched=True)
    assert d3.emit is True
    # an unknown condition must not emit or advance the cycle
    d4 = decide(a, False, d3.new_state, later(301), matched=None)
    assert d4.emit is False
    assert d4.suppression_reason is None


def test_reminder_without_matched_or_fired_does_not_emit():
    a = alert(trigger="reminder", reminder_interval_s=300)
    d = decide(a, False, {}, T0, matched=False)
    assert d.emit is False
    assert d.suppression_reason is None


def test_reminder_crossing_alert_still_works_on_transitions():
    a = alert(trigger="reminder", reminder_interval_s=300)
    d1 = decide(a, True, {}, T0, matched=True)  # a crossing fire
    assert d1.emit is True
    d2 = decide(a, False, d1.new_state, later(60), matched=True)  # still holding
    assert d2.emit is False and d2.suppression_reason == "reminder_interval"
    d3 = decide(a, False, d2.new_state, later(300), matched=True)
    assert d3.emit is True


def test_reminder_interval_gate_only_no_reset_on_unmatch():
    # matched=False does NOT clear the reminder cycle (interval gate only).
    a = alert(trigger="reminder", reminder_interval_s=300)
    d1 = decide(a, False, {}, T0, matched=True)
    assert d1.emit is True
    d2 = decide(a, False, d1.new_state, later(100), matched=False)
    assert d2.emit is False and d2.suppression_reason is None
    assert d2.new_state.get("last_emitted_ts") == T0.isoformat()  # untouched
    d3 = decide(a, False, d2.new_state, later(299), matched=True)
    assert d3.emit is False and d3.suppression_reason == "reminder_interval"
    d4 = decide(a, False, d3.new_state, later(300), matched=True)
    assert d4.emit is True


def test_matched_true_never_drives_non_reminder_triggers():
    for trigger in ("once", "on_transition", "once_per_session"):
        a = alert(trigger=trigger)
        d = decide(a, False, {}, T0, matched=True)
        assert d.emit is False
        assert d.suppression_reason is None


def test_reminder_subject_to_expiry_cooldown_and_rearm():
    # expiry wins
    expired = alert(trigger="reminder", reminder_interval_s=300,
                    expires_at="2026-09-08T09:00:00+00:00")
    d = decide(expired, False, {}, T0, matched=True)
    assert d.suppression_reason == "expired"
    # cooldown suppresses delivery while state advanced
    cooled = alert(trigger="reminder", reminder_interval_s=60, cooldown_s=600)
    d1 = decide(cooled, False, {}, T0, matched=True)
    assert d1.emit is True
    d2 = decide(cooled, False, d1.new_state, later(60), matched=True)
    assert d2.emit is False and d2.suppression_reason == "cooldown"
    # rearm: disarmed holding match does not emit until rearm level is hit
    rearmed = alert(trigger="reminder", reminder_interval_s=60,
                    rearm_level=95.0, rearm_direction="below")
    r1 = decide(rearmed, False, {}, T0, matched=True, current_value=101.0)
    assert r1.emit is True and r1.new_state["armed"] is False
    r2 = decide(rearmed, False, r1.new_state, later(60), matched=True, current_value=101.0)
    assert r2.emit is False and r2.suppression_reason == "not_armed"
    r3 = decide(rearmed, False, r2.new_state, later(61), matched=True, current_value=94.0)
    assert r3.emit is True


# --- cooldown ---------------------------------------------------------------


def test_cooldown_suppresses_delivery_but_state_still_advances():
    a = alert(cooldown_s=60)
    d1 = decide(a, True, {}, T0)
    assert d1.emit is True
    assert d1.new_state["cooldown_until"] == later(60).isoformat()
    d2 = decide(a, True, d1.new_state, later(10))
    assert d2.emit is False
    assert d2.suppression_reason == "cooldown"
    # suppressed call returns full state; prior bookkeeping intact
    assert d2.new_state["cooldown_until"] == later(60).isoformat()
    assert d2.new_state["last_emitted_ts"] == T0.isoformat()
    d3 = decide(a, True, d2.new_state, later(60))  # boundary: now >= cooldown_until
    assert d3.emit is True


def test_cooldown_suppresses_delivery_never_state_bookkeeping():
    # once_per_session + cooldown: a new-session fire inside the cooldown window
    # is delivery-suppressed, but the fired-state bookkeeping still advances.
    a = alert(trigger="once_per_session", cooldown_s=600)
    d1 = decide(a, True, {"session": "d1"}, T0, session_id="d1")
    assert d1.emit is True
    suppressed = decide(a, True, d1.new_state, later(10), session_id="d2")
    assert suppressed.emit is False
    assert suppressed.suppression_reason == "cooldown"
    assert suppressed.new_state["last_session"] == "d2"


# --- state isolation --------------------------------------------------------


def test_one_symbols_suppression_never_touches_another():
    a = alert(cooldown_s=60)
    da = decide(a, True, {}, T0)
    suppressed_a = decide(a, True, da.new_state, later(10))
    assert suppressed_a.emit is False and suppressed_a.suppression_reason == "cooldown"
    db = decide(a, True, {}, later(10))  # stock B: own fresh state
    assert db.emit is True
    assert db.suppression_reason is None
    # A's state was never touched by B's evaluation (engine is pure).
    assert da.new_state != db.new_state


def test_decide_never_mutates_input_state():
    a = alert(trigger="once")
    state = {}
    decide(a, True, state, T0)
    assert state == {}
    state2 = {"fired_once": True, "armed": False, "initialized": True}
    decide(a, True, state2, T0)
    assert state2 == {"fired_once": True, "armed": False, "initialized": True}


# --- expiry -----------------------------------------------------------------


def test_expires_at_in_past_suppresses_regardless():
    a = alert(expires_at="2026-09-08T09:00:00+00:00")
    d = decide(a, True, {}, T0)
    assert d.emit is False
    assert d.suppression_reason == "expired"
    assert d.rule_completed is False


def test_expiry_takes_precedence_over_quiet_session():
    a = alert(expires_at="2026-09-08T09:00:00+00:00")
    d = decide(a, True, {}, T0, session_active=False)
    assert d.suppression_reason == "expired"


def test_future_expiry_does_not_block():
    a = alert(expires_at="2026-09-08T11:00:00Z")
    d = decide(a, True, {}, T0)
    assert d.emit is True


# --- E-9: already true at activation -----------------------------------------


def test_already_true_at_activation_suppressed_without_optin():
    a = alert(trigger="once", notify_if_already_true=False)
    d = decide(a, False, {}, T0, already_true=True)
    assert d.emit is False
    assert d.suppression_reason == "already_true_at_activation"
    assert d.rule_completed is False
    assert d.new_state["initialized"] is True
    assert "fired_once" not in d.new_state
    # Later transitions fire normally.
    d2 = decide(a, True, d.new_state, later(1))
    assert d2.emit is True
    assert d2.rule_completed is True


def test_already_true_with_optin_emits_at_activation():
    a = alert(trigger="once", notify_if_already_true=True)
    d = decide(a, False, {}, T0, already_true=True)
    assert d.emit is True
    assert d.rule_completed is True
    assert d.suppression_reason is None


def test_already_true_ignored_after_initialization():
    a = alert(notify_if_already_true=False)
    state = {"initialized": True}
    d = decide(a, False, state, T0, already_true=True)
    assert d.emit is False
    assert d.suppression_reason is None  # not an activation decision anymore


# --- quiet session ------------------------------------------------------------


def test_quiet_session_suppresses_without_state_advance():
    a = alert()
    state = {"initialized": True, "prev": 100.0}
    d = decide(a, True, state, T0, session_active=False)
    assert d.emit is False
    assert d.suppression_reason == "quiet_session"
    assert d.rule_completed is False
    assert d.new_state == state  # nothing written


# --- rearm --------------------------------------------------------------------


def test_rearm_disarms_after_emit_and_rearms_below_level():
    a = alert(rearm_level=95.0, rearm_direction="below")
    d1 = decide(a, True, {}, T0, current_value=101.0)
    assert d1.emit is True
    assert d1.new_state["armed"] is False
    d2 = decide(a, True, d1.new_state, later(1), current_value=101.0)
    assert d2.emit is False
    assert d2.suppression_reason == "not_armed"
    d3 = decide(a, True, d2.new_state, later(2), current_value=94.0)
    assert d3.emit is True  # cur <= rearm_level -> re-armed, this fire emits
    assert d3.new_state["armed"] is False  # re-disarmed by the new emit
    d4 = decide(a, True, d3.new_state, later(3), current_value=101.0)
    assert d4.emit is False
    assert d4.suppression_reason == "not_armed"


def test_rearm_direction_above():
    a = alert(rearm_level=105.0, rearm_direction="above")
    d1 = decide(a, True, {}, T0, current_value=120.0)
    assert d1.emit is True
    d2 = decide(a, True, d1.new_state, later(1), current_value=104.9)
    assert d2.emit is False and d2.suppression_reason == "not_armed"
    d3 = decide(a, True, d2.new_state, later(2), current_value=105.0)
    assert d3.emit is True


def test_rearm_direction_defaults_to_below():
    a = alert(rearm_level=95.0)
    d1 = decide(a, True, {}, T0, current_value=101.0)
    assert d1.emit is True
    d2 = decide(a, True, d1.new_state, later(1), current_value=95.0)
    assert d2.emit is True


def test_armed_by_default_without_rearm_config():
    a = alert()
    d = decide(a, True, {}, T0, current_value=None)
    assert d.emit is True
    assert "armed" not in d.new_state


# --- end-to-end with predicates (canonical F3 case) ---------------------------


def test_pipeline_threshold_100_crossing_emits_exactly_once():
    st = Stage(
        id="px",
        type="signal",
        clock="ltp",
        timeframe=None,
        conditions=(
            Condition(
                left=Operand(kind="field", name="ltp"),
                op="crosses_above",
                right=Operand(kind="value", value=100.0),
            ),
        ),
    )
    a = alert(trigger="once")
    state = {}
    emitted = []
    for ltp, ts in ((99.0, T0), (101.0, later(1)), (102.0, later(2))):
        pr = evaluate_stage(st, Observation(ts=ts, epoch_id="e1", ltp=ltp), state)
        state = pr.state
        d = decide(a, pr.fired, state, ts, already_true=pr.matched)
        state = d.new_state
        if d.emit:
            emitted.append(ltp)
    assert emitted == [101.0]


# --- public surface ------------------------------------------------------------


def test_engine_public_surface():
    import backend.alerts.engine as engine

    assert set(engine.__all__) == {"EngineDecision", "decide"}
