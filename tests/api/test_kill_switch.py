"""Platform kill switch: stop every job, flatten every exposed book, close lanes.

SQLite, fake broker, no PostgreSQL. The app mounts the SAME three routers the
real deployment uses (platform control plane, hosted strategies, owner actions)
so the kill switch is exercised through its public routes with the production
orchestration, and reuses the owner-action suite's SQLite fixture and seeding
helpers rather than inventing a second one.

Pinned properties:

- a body without the exact ``"confirm": "FLATTEN ALL"`` moves NOTHING;
- a confirmed call stops every active hosted job, flattens every strategy with
  live or paper exposure (cancelling pending entry work first, through the SAME
  governed owner actions), and closes every live lane with the reason audited;
- the operation is durable and idempotent: a second call while it is still open
  returns the SAME operation instead of starting a parallel one.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs  # noqa: E402

install_dependency_stubs()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import text  # noqa: E402

from backend.api.routers import platform as platform_router  # noqa: E402
from backend.api.routers import strategies as strategies_router  # noqa: E402
from backend.api.routers import strategy_owner_actions as owner_actions_router  # noqa: E402
from backend.app.auth import AppUser  # noqa: E402
from backend.platform.models import (  # noqa: E402
    PlatformLiveSetting,
    PlatformLiveSettingAudit,
)

# Reuse the owner-action suite's SQLite fixture (with the ``public.`` ATTACH
# tables the reduction/cancel paths read) and its seeding helpers.
from tests.api.test_strategy_owner_actions_api import (  # noqa: E402,F401
    ACCOUNT,
    EQ_ID,
    _FakeReductionPipeline,
    _canned_reduction_builder,
    _create,
    _rows,
    _seed_catalog,
    _seed_job,
    _seed_projection,
    session_factory,
)

KILL_URL = "/api/platform/kill-switch"
CONFIRM = "FLATTEN ALL"
LANES = ("cnc", "mis", "futures", "options")
SECOND_ID = "NSE:TCS"
SECOND = "TCS"


class _PerBookPipeline(_FakeReductionPipeline):
    """A pipeline that closes ONLY the book its plan targets.

    ``_FakeReductionPipeline`` zeroes every row, which would let one strategy's
    reduction hide another's. Each strategy here has its OWN instrument, so this
    variant proves every strategy's book was reduced by its own plan.
    """

    async def execute(self, plan, *, actor):
        legs = (plan.get("resolved_plan") or {}).get("legs") or []
        instrument = str((legs[0] if legs else {}).get("instrument_id") or "")
        self.executed.append({"plan_id": plan.get("plan_id"), "actor": str(actor)})
        self.events.append(f"reduce:{instrument}")
        with self.session_factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_position_projection SET net_quantity = 0 "
                    "WHERE canonical_instrument_id = :instrument"
                ),
                {"instrument": instrument},
            )
            session.commit()
        return {"status": "filled", "steps": [{"step_no": 1, "filled_quantity": 150}]}


def _app(session_factory, monkeypatch, username="admin", **state):
    from backend.app import auth as auth_module

    monkeypatch.setattr(
        auth_module,
        "get_optional_app_user",
        lambda _request: AppUser(username=username, role="admin"),
    )
    app = FastAPI()
    app.include_router(platform_router.router, prefix="/api")
    app.include_router(strategies_router.router, prefix="/api")
    app.include_router(owner_actions_router.router, prefix="/api")
    app.dependency_overrides[platform_router._platform_db] = lambda: session_factory
    app.dependency_overrides[strategies_router._strategies_db] = lambda: session_factory
    app.dependency_overrides[owner_actions_router._owner_actions_db] = lambda: (
        session_factory
    )
    app.state.platform_session_factory = session_factory
    for name, value in state.items():
        if value is not None:
            setattr(app.state, name, value)
    return app


def _client(session_factory, monkeypatch, **state):
    app = _app(session_factory, monkeypatch, **state)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _lane_row(session_factory):
    with session_factory() as session:
        return session.query(PlatformLiveSetting).one_or_none()


def _kill_rows(session_factory):
    return _rows(
        session_factory,
        "SELECT strategy_id, evaluation_id, detail FROM strategy_proposal_journal "
        "WHERE reason_code = 'kill_switch' ORDER BY created_at, id",
    )


async def _seed_paper_strategy_with_exposure(
    client, session_factory, name, *, job_status="queued", instrument_id=EQ_ID
):
    strategy_id = await _create(client, name=name)
    _seed_projection(
        session_factory,
        strategy_id=strategy_id,
        instrument_id=instrument_id,
        product="CNC",
        net_quantity=150,
    )
    job_id = _seed_job(session_factory, strategy_id=strategy_id, status=job_status)
    return strategy_id, job_id


@pytest.fixture(autouse=True)
def account_policy(monkeypatch):
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", ACCOUNT)
    yield


@pytest.mark.asyncio
async def test_kill_switch_stops_jobs_flattens_every_strategy_and_closes_lanes(
    session_factory, monkeypatch
):
    pipeline = _PerBookPipeline(session_factory)
    async with _client(session_factory, monkeypatch) as client:
        first_id, first_job = await _seed_paper_strategy_with_exposure(
            client, session_factory, "ks-one"
        )
        second_id, second_job = await _seed_paper_strategy_with_exposure(
            client, session_factory, "ks-two", instrument_id=SECOND_ID
        )
    _seed_catalog(session_factory)
    _seed_catalog(
        session_factory,
        instrument_id=SECOND_ID,
        symbol=SECOND,
        broker_token=738561,
        generation="gen-ks-2",
    )

    async with _client(
        session_factory,
        monkeypatch,
        owner_action_reduction_plan_builder=_canned_reduction_builder(),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        response = await client.post(
            KILL_URL, json={"reason": "operator_abort", "confirm": CONFIRM}
        )
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["status"] == "complete", body
    assert body["operation_id"]
    assert body["reason"] == "operator_abort"
    assert {row["outcome"] for row in body["jobs"]} == {"stopped"}
    assert {row["job_id"] for row in body["jobs"]} == {first_job, second_job}
    assert {row["strategy_id"] for row in body["strategies"]} == {first_id, second_id}
    assert all(row["status"] == "complete" for row in body["strategies"])
    # Both books were actually reduced through the governed pipeline.
    assert len(pipeline.executed) == 2
    # Every live lane is closed and reported closed.
    assert body["lanes_closed"] == {lane: False for lane in LANES}
    row = _lane_row(session_factory)
    assert row is not None
    assert {lane: bool(row.lanes.get(lane)) for lane in LANES} == {
        lane: False for lane in LANES
    }
    with session_factory() as session:
        audit = session.query(PlatformLiveSettingAudit).order_by(
            PlatformLiveSettingAudit.audit_id
        ).all()
    assert len(audit) == 1
    assert audit[0].reason == "operator_abort"
    assert audit[0].actor_id == "app:admin"
    # The jobs really stopped and the operation is durable.
    assert {
        row["status"]
        for row in _rows(session_factory, "SELECT status FROM strategy_jobs")
    } == {"stopped"}
    assert len(_kill_rows(session_factory)) == 3  # header + two targets

    # GET returns the same operation's per-strategy progress.
    async with _client(session_factory, monkeypatch) as client:
        fetched = await client.get(KILL_URL)
        assert fetched.status_code == 200, fetched.text
        latest = fetched.json()
    assert latest["operation_id"] == body["operation_id"]
    assert latest["status"] == "complete"
    assert {row["strategy_id"] for row in latest["strategies"]} == {
        first_id,
        second_id,
    }


@pytest.mark.asyncio
async def test_kill_switch_requires_the_exact_confirm_phrase(
    session_factory, monkeypatch
):
    """Without ``confirm`` nothing moves: no stop, no flatten, no lane change."""
    async with _client(session_factory, monkeypatch) as client:
        strategy_id, job_id = await _seed_paper_strategy_with_exposure(
            client, session_factory, "ks-unconfirmed"
        )
    _seed_catalog(session_factory)
    pipeline = _FakeReductionPipeline(session_factory)

    async with _client(
        session_factory,
        monkeypatch,
        owner_action_reduction_plan_builder=_canned_reduction_builder(),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        refused = await client.post(
            KILL_URL, json={"reason": "oops", "confirm": "flatten all"}
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["rejection_reason"] == (
            "KILL_SWITCH_CONFIRM_REQUIRED"
        )

    _ = strategy_id
    assert pipeline.executed == []
    assert _rows(session_factory, "SELECT operation_id FROM strategy_flatten_operations") == []
    assert _lane_row(session_factory) is None
    assert _rows(
        session_factory,
        "SELECT status FROM strategy_jobs WHERE id = :id",
        {"id": job_id},
    )[0]["status"] == "queued"


@pytest.mark.asyncio
async def test_kill_switch_is_idempotent_while_the_operation_is_open(
    session_factory, monkeypatch
):
    """A second confirmed call returns the SAME running operation."""
    # A book that never reaches zero leaves the flatten open, so the operation
    # stays open across the two calls.
    pipeline = _FakeReductionPipeline(session_factory, zero_book=False)
    async with _client(session_factory, monkeypatch) as client:
        strategy_id, _job_id = await _seed_paper_strategy_with_exposure(
            client, session_factory, "ks-idem"
        )
    _seed_catalog(session_factory)

    async with _client(
        session_factory,
        monkeypatch,
        owner_action_reduction_plan_builder=_canned_reduction_builder(),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        first = await client.post(
            KILL_URL, json={"reason": "abort", "confirm": CONFIRM}
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["status"] == "blocked", first_body
        assert first_body["idempotent"] is False

        second = await client.post(
            KILL_URL, json={"reason": "abort again", "confirm": CONFIRM}
        )
        assert second.status_code == 200, second.text
        second_body = second.json()

    assert second_body["idempotent"] is True
    assert second_body["operation_id"] == first_body["operation_id"]
    assert second_body["reason"] == "abort"  # the FIRST operation's reason stands
    assert {row["strategy_id"] for row in second_body["strategies"]} == {strategy_id}
    # No parallel operation was created, and the lanes were closed exactly once.
    assert len(_kill_rows(session_factory)) == 2  # header + one target
    with session_factory() as session:
        assert session.query(PlatformLiveSettingAudit).count() == 1


@pytest.mark.asyncio
async def test_kill_switch_spans_a_strategy_owned_by_another_operator(
    session_factory, monkeypatch
):
    """The kill switch is platform-wide, not only the caller's own strategies."""
    pipeline = _FakeReductionPipeline(session_factory)
    async with _client(session_factory, monkeypatch, username="other") as client:
        other_id = await _create(client, name="ks-other-owner")
    _seed_projection(
        session_factory,
        strategy_id=other_id,
        instrument_id=EQ_ID,
        product="CNC",
        net_quantity=150,
    )
    other_job = _seed_job(
        session_factory, strategy_id=other_id, owner_id="app:other", status="queued"
    )
    _seed_catalog(session_factory)

    # The ADMIN triggers the kill switch; the other operator's strategy is still
    # stopped and flattened.
    async with _client(
        session_factory,
        monkeypatch,
        owner_action_reduction_plan_builder=_canned_reduction_builder(),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        response = await client.post(
            KILL_URL, json={"reason": "abort", "confirm": CONFIRM}
        )
        assert response.status_code == 200, response.text
        body = response.json()

    assert {row["strategy_id"] for row in body["jobs"]} == {other_id}
    assert body["jobs"][0]["job_id"] == other_job
    assert {row["strategy_id"] for row in body["strategies"]} == {other_id}
    assert body["strategies"][0]["status"] == "complete"
    assert len(pipeline.executed) == 1
