"""The per-lane live allowlist (``HOSTED_LIVE_LANES``), default deny.

Production runs ``HOSTED_LIVE_ENABLED=true``, so the master switch alone would
open EVERY registered lane at once. The C2 rollout opens one lane at a time
(CNC -> MIS -> futures -> options), so this file pins the three answers the
allowlist owes:

* the reader - unset/empty is deny, a named lane opens only that lane, and an
  unknown name is ignored rather than guessed;
* the capability surface (``hosted_live_lanes``) and the admission gate name a
  lane that is not open (``LIVE_LANE_NOT_ENABLED``);
* closing a lane after admission stops NEW exposure, and never blocks a
  reduction, an exit or the MIS square-off.

Everything here runs on fakes: no broker, no network, no database.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.strategies import live_service
from backend.strategies.execution import ExecutionRefusal
from backend.strategies.live_service import (
    enabled_live_lanes,
    hosted_live_lanes,
    live_lane_enabled,
)
from backend.strategies.live_sequence import (
    RULE_ALL_PREREQUISITES_FILLED,
    RULE_IMMEDIATE,
    RULE_MIS_SQUAREOFF,
    StepSpec,
)

#: 2026-09-25 10:00 UTC == 15:30 IST: past the NSE MIS square-off (15:20 IST).
NOW = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)

#: 2026-09-25 09:00 UTC == 14:30 IST: BEFORE the NSE MIS square-off (15:20 IST).
BEFORE_SQUAREOFF = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)


# -- the reader --------------------------------------------------------------


def test_unset_and_empty_are_default_deny():
    assert enabled_live_lanes({}) == []
    assert enabled_live_lanes({"HOSTED_LIVE_LANES": ""}) == []
    assert enabled_live_lanes({"HOSTED_LIVE_LANES": "  ,  "}) == []
    assert live_lane_enabled("cnc", {}) is False


def test_only_the_named_lanes_are_enabled():
    assert enabled_live_lanes({"HOSTED_LIVE_LANES": "cnc"}) == ["cnc"]
    assert live_lane_enabled("cnc", {"HOSTED_LIVE_LANES": "cnc"}) is True
    assert live_lane_enabled("mis", {"HOSTED_LIVE_LANES": "cnc"}) is False
    # An internal lane implementation name resolves to the lane that owns it.
    assert live_lane_enabled("target_weights", {"HOSTED_LIVE_LANES": "cnc"}) is True
    assert live_lane_enabled("target_weights", {"HOSTED_LIVE_LANES": "mis"}) is False
    assert (
        live_lane_enabled("futures_roll", {"HOSTED_LIVE_LANES": "cnc, futures"}) is True
    )


def test_unknown_names_are_ignored_and_never_a_wildcard():
    enabled = {"HOSTED_LIVE_LANES": "equities_only,CNC,cnc"}
    assert enabled_live_lanes(enabled) == ["cnc"]
    assert live_lane_enabled("equities_only", enabled) is False
    assert live_lane_enabled("options", {"HOSTED_LIVE_LANES": "*"}) is False


def test_hosted_live_lanes_reports_only_the_admitted_and_enabled_lanes(monkeypatch):
    monkeypatch.setenv("HOSTED_LIVE_ENABLED", "true")
    monkeypatch.delenv("HOSTED_LIVE_LANES", raising=False)
    assert hosted_live_lanes() == []

    monkeypatch.setenv("HOSTED_LIVE_LANES", "options")
    assert hosted_live_lanes() == ["options"]

    monkeypatch.setenv("HOSTED_LIVE_LANES", "cnc,mis,equities_only")
    assert hosted_live_lanes() == ["cnc", "mis"]


# -- the persisted row (``platform_live_settings``) ---------------------------


def test_an_explicit_environ_asks_the_env_and_never_touches_the_database(monkeypatch):
    """The deployment-settings contract is preserved exactly.

    Callers that pass an explicit mapping (every existing unit test, and any
    caller reasoning about a *hypothetical* deployment config) get the env
    answer, so the database is not consulted at all.
    """
    from backend.platform import settings as platform_settings

    def _explode():
        raise AssertionError("the lane gate must not read the database here")

    monkeypatch.setattr(platform_settings, "_session_factory_override", _explode)

    assert enabled_live_lanes({"HOSTED_LIVE_LANES": "cnc"}) == ["cnc"]
    assert enabled_live_lanes({}) == []
    assert enabled_live_lanes({"HOSTED_LIVE_LANES": "options"}) == ["options"]


def test_an_unreadable_persisted_row_keeps_the_env_answer_and_default_deny(monkeypatch):
    """A database that cannot be read is NOT an open lane.

    The live path falls back to the deployment env allowlist - the documented,
    default-deny source - so an outage can only ever preserve what the deployment
    already declared, never widen it.
    """
    from backend.platform import settings as platform_settings

    monkeypatch.setattr(
        platform_settings,
        "_session_factory_override",
        lambda: _unreadable_session_factory,
    )

    monkeypatch.setenv("HOSTED_LIVE_LANES", "cnc")
    assert enabled_live_lanes() == ["cnc"]
    assert live_lane_enabled("cnc") is True
    assert live_lane_enabled("options") is False

    monkeypatch.delenv("HOSTED_LIVE_LANES", raising=False)
    assert enabled_live_lanes() == []
    assert live_lane_enabled("cnc") is False


def _unreadable_session_factory():
    raise RuntimeError("no database here")


def test_a_refusal_names_the_persisted_setting_when_the_owner_row_gates(monkeypatch):
    """The operator is told the knob that is ACTUALLY closed, not the env one."""
    from backend.platform import settings as platform_settings
    from backend.strategies.live_service import live_lane_disabled_detail

    monkeypatch.setattr(
        platform_settings,
        "read_live_settings",
        lambda session_factory=None: platform_settings.PersistedLaneSettings(
            lanes={"cnc": True, "mis": False, "futures": False, "options": False},
            updated_at=None,
            updated_by="app:admin",
        ),
    )

    detail = live_lane_disabled_detail(lane="options")

    assert detail["lane"] == "options"
    assert detail["enabled_lanes"] == ["cnc"]
    assert detail["setting"] == "platform_live_settings"
    assert "platform live settings" in detail["message"]


# -- fakes -------------------------------------------------------------------


class _FakeLedger:
    def __init__(self, reservation=None, release_error=None):
        self._reservation = reservation
        self.release_error = release_error
        self.release_calls = []
        self.releases = []

    def for_plan(self, plan_id):
        return self._reservation

    def release(self, reservation_id, *, actor_id=None, reason=None):
        self.release_calls.append(
            {
                "reservation_id": reservation_id,
                "actor_id": actor_id,
                "reason": reason,
            }
        )
        if self.release_error is not None:
            raise self.release_error
        self.releases.append((reservation_id, reason))


class _FakeAdapter:
    """The broker boundary: it records what it was asked to submit/release."""

    def __init__(self):
        self.submissions = []
        self.releases = []

    async def submit(self, plan, **kwargs):
        self.submissions.append({"plan": plan, **kwargs})
        return SimpleNamespace(
            state="pending",
            steps=[{"step_no": 1, "state": "pending", "detail": {}}],
        )

    async def release_step(self, plan, spec, **kwargs):
        self.releases.append({"plan": plan, "spec": spec, **kwargs})
        return {"state": "submitted"}


class _FakeTrail:
    def __init__(self):
        self.events = []

    def _record_event(self, plan_id, **kwargs):
        self.events.append({"plan_id": plan_id, **kwargs})


class _FakeSubmissions:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def get(self, *, plan_id, step_no, db=None):
        return self.rows.get((str(plan_id), int(step_no)))


class _CountingSession:
    def __init__(self, submissions):
        self.submissions = int(submissions)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        return SimpleNamespace(scalar=lambda: self.submissions)


class _CountingSessionFactory:
    def __init__(self, submissions):
        self.session = _CountingSession(submissions)

    def __call__(self):
        return self.session


class _FakeSequence:
    """The durable parent store, faked down to what the release pass reads."""

    def __init__(self, *, states=None, withheld=(), rows=None):
        self.states = dict(states or {})
        self.withheld = [int(step_no) for step_no in withheld]
        self.blockers = []
        self.submissions = _FakeSubmissions(rows)

    def step_states(self, plan_id):
        return dict(self.states)

    def withheld_steps(self, plan_id):
        return [{"step_no": step_no} for step_no in self.withheld]

    def record_release_blocker(self, *, plan_id, step_no, reason_code, detail):
        self.blockers.append(
            {
                "plan_id": plan_id,
                "step_no": int(step_no),
                "reason_code": reason_code,
                "detail": dict(detail),
            }
        )


def _reservation(plan_id: str = "plan-1") -> dict:
    return {
        "plan_id": plan_id,
        "reservation_id": "res-1",
        "status": "active",
        "execution_environment": "live",
        "valid_until": None,
    }


def _executor(
    *,
    lanes=None,
    adapter=None,
    ledger=None,
    sequence=None,
    mis_clock=None,
    session_factory=None,
):
    from backend.strategies.live_service import LivePlanExecutor

    environ = {"HOSTED_LIVE_ENABLED": "true"}
    if lanes is not None:
        environ["HOSTED_LIVE_LANES"] = lanes
    executor = LivePlanExecutor(
        session_factory=session_factory or (lambda: None),
        adapter=adapter or _FakeAdapter(),
        ledger=ledger or _FakeLedger(),
        clock=lambda: NOW,
        mis_clock=mis_clock,
        environ=environ,
        quote_reader=lambda leg: {"ltp": 1500.0, "as_of": NOW.isoformat()},
        margin_reader=lambda account, plan: {
            "usable": 500000.0,
            "as_of": NOW.isoformat(),
        },
        session_id_reader=lambda account: "session-1",
        authorization=object(),
    )
    executor._trail = _FakeTrail()
    if sequence is not None:
        executor.sequence = sequence
    return executor


def _stub_authority(monkeypatch, executor) -> None:
    """The governed authority read and the proposal envelope, as persisted rows."""
    derived = {"binding": {"strategy_run_id": "run-1"}, "authority": {"attempt": 1}}
    monkeypatch.setattr(
        live_service,
        "derive_live_authority",
        lambda *args, **kwargs: derived,
    )
    executor._envelope = lambda plan: {"status": "validated"}


def _plan(*, plan_kind: str, legs, plan_id: str = "plan-1") -> dict:
    return {
        "plan_id": plan_id,
        "proposal_id": "prop-1",
        "account_id": "kite:A",
        "strategy_id": "stg-1",
        "plan_kind": plan_kind,
        "resolved_plan": {"legs": legs},
    }


def _spec(
    *,
    step_no: int = 2,
    lane: str = "target_weights",
    side: str = "BUY",
    quantity: int = 100,
    increases: bool = True,
    rule: str = RULE_IMMEDIATE,
    depends_on: tuple = (),
) -> StepSpec:
    return StepSpec(
        step_no=step_no,
        step_ref=f"live-plan:plan-1:step:{step_no}",
        lane=lane,
        domain="CNC",
        instrument_id="inst-REL",
        exchange="NSE",
        tradingsymbol="RELIANCE",
        broker_exchange="NSE",
        broker_symbol="RELIANCE",
        product="CNC",
        variety="regular",
        side=side,
        quantity=quantity,
        lot_size=1,
        target_quantity=quantity,
        current_quantity=0 if increases else quantity,
        delta=quantity,
        notional_inr=1500.0 * quantity,
        increases_exposure=increases,
        depends_on=depends_on,
        release_rule=rule,
        detail={"mis": {"exchange": "NSE", "product": "MIS"}},
    )


def _counts() -> dict:
    return {"scanned": 1, "released": 0, "blocked": 0, "no_op": 0, "errors": 0}


# -- admission ---------------------------------------------------------------


def test_a_new_plan_in_a_closed_lane_is_refused_by_name(monkeypatch):
    adapter = _FakeAdapter()
    ledger = _FakeLedger(_reservation())
    executor = _executor(
        lanes="cnc",
        adapter=adapter,
        ledger=ledger,
        session_factory=_CountingSessionFactory(0),
    )
    _stub_authority(monkeypatch, executor)
    plan = _plan(
        plan_kind="option_structure",
        legs=[{"signed_quantity": 75, "_current_quantity": 0, "product": "NRML"}],
    )

    with pytest.raises(ExecutionRefusal) as refused:
        asyncio.run(executor.execute(plan, actor="owner"))

    assert refused.value.reason_code == "LIVE_LANE_NOT_ENABLED"
    assert refused.value.detail["lane"] == "options"
    assert refused.value.detail["enabled_lanes"] == ["cnc"]
    assert refused.value.detail["setting"] == "HOSTED_LIVE_LANES"
    assert adapter.submissions == []
    assert executor._trail.events[0]["refusal_reason"] == "LIVE_LANE_NOT_ENABLED"
    assert ledger.releases == [("res-1", "refused_before_submission")]
    assert ledger.release_calls[0]["actor_id"] == "system"


def test_a_refusal_with_a_live_submission_keeps_the_reservation(monkeypatch):
    adapter = _FakeAdapter()
    ledger = _FakeLedger(_reservation())
    executor = _executor(
        lanes="cnc",
        adapter=adapter,
        ledger=ledger,
        session_factory=_CountingSessionFactory(1),
    )
    _stub_authority(monkeypatch, executor)
    plan = _plan(
        plan_kind="option_structure",
        legs=[{"signed_quantity": 75, "_current_quantity": 0, "product": "NRML"}],
    )

    with pytest.raises(ExecutionRefusal) as refused:
        asyncio.run(executor.execute(plan, actor="owner"))

    assert refused.value.reason_code == "LIVE_LANE_NOT_ENABLED"
    assert ledger.releases == []


def test_a_forbidden_release_never_masks_the_refusal(monkeypatch):
    from backend.strategies.reservations import ReleaseForbidden

    adapter = _FakeAdapter()
    reservation = _reservation()
    reservation["status"] = "consumed"
    ledger = _FakeLedger(
        reservation,
        release_error=ReleaseForbidden({"reservation_id": "res-1"}),
    )
    executor = _executor(
        lanes="cnc,options",
        adapter=adapter,
        ledger=ledger,
        session_factory=_CountingSessionFactory(0),
    )
    _stub_authority(monkeypatch, executor)
    plan = _plan(
        plan_kind="option_structure",
        legs=[{"signed_quantity": 75, "_current_quantity": 0, "product": "NRML"}],
    )

    with pytest.raises(ExecutionRefusal) as refused:
        asyncio.run(executor.execute(plan, actor="owner"))

    assert refused.value.reason_code == "RESERVATION_REQUIRED"
    assert ledger.releases == []
    assert len(ledger.release_calls) == 1


def test_the_same_plan_in_an_open_lane_is_admitted(monkeypatch):
    adapter = _FakeAdapter()
    executor = _executor(
        lanes="cnc,options", adapter=adapter, ledger=_FakeLedger(_reservation())
    )
    _stub_authority(monkeypatch, executor)
    plan = _plan(
        plan_kind="option_structure",
        legs=[{"signed_quantity": 75, "_current_quantity": 0, "product": "NRML"}],
    )

    result = asyncio.run(executor.execute(plan, actor="owner"))

    assert result["status"] == "submitted"
    assert adapter.submissions[0]["lane"] == "option_structure"


def test_a_reduce_only_plan_is_admitted_while_its_lane_is_closed(monkeypatch):
    """Closing a lane stops NEW exposure, never an exit or a reduction."""
    adapter = _FakeAdapter()
    executor = _executor(lanes="mis", adapter=adapter)
    _stub_authority(monkeypatch, executor)
    plan = _plan(
        plan_kind="target_weights",
        legs=[{"signed_quantity": 0, "_current_quantity": 100, "product": "CNC"}],
    )

    result = asyncio.run(executor.execute(plan, actor="owner"))

    assert result["status"] == "submitted"
    assert adapter.submissions[0]["lane"] == "target_weights"


# -- release -----------------------------------------------------------------


def test_an_increasing_release_in_a_lane_closed_after_admission_is_refused(monkeypatch):
    adapter = _FakeAdapter()
    sequence = _FakeSequence(states={1: "filled"}, withheld=[2])
    executor = _executor(lanes="mis", adapter=adapter, sequence=sequence)
    _stub_authority(monkeypatch, executor)
    spec = _spec(
        step_no=2, increases=True, rule=RULE_ALL_PREREQUISITES_FILLED, depends_on=(1,)
    )
    plan = _plan(
        plan_kind="target_weights",
        legs=[{"signed_quantity": 100, "_current_quantity": 0, "product": "CNC"}],
    )
    counts = _counts()

    asyncio.run(
        executor._release_parent(
            {"plan_id": plan["plan_id"], "lane": "cnc", "step_spec": [spec]},
            plan,
            counts=counts,
            actor="live-sequence",
        )
    )

    assert (counts["released"], counts["blocked"]) == (0, 1)
    assert adapter.releases == []
    assert sequence.blockers[0]["reason_code"] == "LIVE_LANE_NOT_ENABLED"
    assert sequence.blockers[0]["detail"]["lane"] == "cnc"
    assert sequence.blockers[0]["detail"]["enabled_lanes"] == ["mis"]


def test_a_reduction_release_still_proceeds_in_the_same_closed_lane(monkeypatch):
    adapter = _FakeAdapter()
    sequence = _FakeSequence(states={1: "filled"}, withheld=[2])
    executor = _executor(lanes="mis", adapter=adapter, sequence=sequence)
    _stub_authority(monkeypatch, executor)
    spec = _spec(
        step_no=2, increases=False, side="SELL", rule=RULE_IMMEDIATE, depends_on=(1,)
    )
    plan = _plan(
        plan_kind="target_weights",
        legs=[{"signed_quantity": 0, "_current_quantity": 100, "product": "CNC"}],
    )
    counts = _counts()

    asyncio.run(
        executor._release_parent(
            {"plan_id": plan["plan_id"], "lane": "cnc", "step_spec": [spec]},
            plan,
            counts=counts,
            actor="live-sequence",
        )
    )

    assert (counts["released"], counts["blocked"]) == (1, 0)
    assert sequence.blockers == []
    assert adapter.releases[0]["spec"] is spec


def test_mis_squareoff_still_releases_while_mis_is_closed(monkeypatch):
    adapter = _FakeAdapter()
    sequence = _FakeSequence(withheld=[1])
    executor = _executor(
        lanes="cnc", adapter=adapter, sequence=sequence, mis_clock=lambda: NOW
    )
    _stub_authority(monkeypatch, executor)
    spec = _spec(
        step_no=1,
        lane="mis",
        side="SELL",
        increases=False,
        rule=RULE_MIS_SQUAREOFF,
    )
    plan = _plan(plan_kind="single_instrument", legs=[{"product": "MIS"}])
    counts = _counts()

    asyncio.run(
        executor._release_parent(
            {"plan_id": plan["plan_id"], "lane": "mis", "step_spec": [spec]},
            plan,
            counts=counts,
            actor="live-sequence",
        )
    )

    assert (counts["released"], counts["blocked"]) == (1, 0)
    assert sequence.blockers == []
    assert adapter.releases[0]["spec"] is spec


def test_owner_flatten_mis_reduction_releases_immediately_before_squareoff():
    """An owner_flatten MIS reduction is the owner's explicit close: it must not
    wait for the 15:20 clock, the stale-worker exit or an operator stop of a
    running job - a run that has already stopped has none of those left."""
    executor = _executor(lanes="mis", mis_clock=lambda: BEFORE_SQUAREOFF)
    spec = _spec(step_no=1, lane="mis", side="SELL", increases=False, rule=RULE_MIS_SQUAREOFF)
    plan = _plan(plan_kind="single_instrument", legs=[{"product": "MIS"}])
    authority = {"authority_kind": "owner_flatten", "flatten_operation_id": "op-1"}

    allowed, reason, detail = executor._lane_release_rule(
        plan=plan, spec=spec, binding={"strategy_run_id": "run-1"}, authority=authority
    )

    assert allowed is True
    assert reason == ""
    assert detail["authority_source"] == "owner_flatten"


def test_ordinary_mis_reduction_stays_withheld_before_squareoff(monkeypatch):
    """The control case: an ordinary (non owner_flatten) MIS reduction is still
    governed by the platform's own conditions, not released on request."""
    executor = _executor(lanes="mis", mis_clock=lambda: BEFORE_SQUAREOFF)
    monkeypatch.setattr(executor, "_mis_context", lambda **kwargs: {})
    spec = _spec(step_no=1, lane="mis", side="SELL", increases=False, rule=RULE_MIS_SQUAREOFF)
    plan = _plan(plan_kind="single_instrument", legs=[{"product": "MIS"}])
    authority = {"attempt": 1}

    allowed, reason, detail = executor._lane_release_rule(
        plan=plan, spec=spec, binding={"strategy_run_id": "run-1"}, authority=authority
    )

    assert allowed is False
    assert reason == "MIS_SQUAREOFF_NOT_DUE"


# -- send-time re-evaluation -------------------------------------------------


class _CapturingAdmission:
    """Records the exact keyword arguments the live send gate hands admission."""

    def __init__(self):
        self.calls = []

    def evaluate(self, plan, **kwargs):
        from backend.strategies.admission import AdmissionVerdict

        self.calls.append({"plan": plan, **kwargs})
        return AdmissionVerdict(True)


def test_the_live_send_gate_excludes_the_plans_own_reservation():
    """The send gate re-evaluates an already-reserved plan.

    Its OWN reservation must be excluded, or the requirement is charged twice
    (the live send-time double-count). The adapter passes the plan's own id, not
    a constant.
    """
    from backend.strategies.live_adapter import LivePlanAdapter

    admission = _CapturingAdmission()
    adapter = LivePlanAdapter(
        session_factory=lambda: None,
        admission=admission,
        clock=lambda: NOW,
    )
    plan = _plan(
        plan_kind="single_instrument",
        legs=[{"signed_quantity": 1, "_current_quantity": 0, "product": "CNC"}],
        plan_id="plan-77",
    )

    result = adapter._check_admission(plan, margin_evidence=None, catalog_state=None)

    assert result["admitted"] is True
    assert len(admission.calls) == 1
    assert admission.calls[0]["exclude_plan_id"] == "plan-77"
    assert admission.calls[0]["execution_environment"] == "live"
