"""Hosted discretionary-mutation bypasses, at the worker HTTP boundary.

Phase 2 makes the owner's chosen mode (approval-based or autonomous) the only
way discretionary exposure changes. These tests pin the exact routes an issued
child credential used to reach directly:

* ``POST /worker/options/runs/{id}/enter`` (and the other options mutations);
* ``PATCH /worker/runs/{id}/protection`` (which can disable backend protection);
* ``PATCH /worker/runs/{id}/risk`` naming an owner-mandated policy key.

Every one of them is refused by name for a hosted child, and the EXTERNAL worker
contract is unchanged. The harness (real token/attempt authority over a SQLite
schema and the shared fake worker repository) is reused from the Phase 1 child
authority suite so the two cannot drift.
"""

from __future__ import annotations

import pytest

from tests.api.test_hosted_child_authority import (  # noqa: F401
    BASE,
    CHILD_ID,
    EXTERNAL_ID,
    _child_headers,
    _external_headers,
)
# Imported so pytest can resolve it by name through ``getfixturevalue`` below.
from tests.api.test_hosted_child_authority import (  # noqa: F401
    harness as _child_authority_harness,
)


@pytest.fixture()
def harness(request):
    """The Phase 1 child-authority harness, reused rather than copied."""
    return request.getfixturevalue("_child_authority_harness")


def _mount_protection(app) -> None:
    from backend.api.routers import worker_protection

    app.include_router(worker_protection.router, prefix="/api")


def _grant_action(worker, token_id: str, action: str) -> None:
    actions = list(worker.tokens[token_id].get("allowed_actions") or [])
    if action not in actions:
        actions.append(action)
    worker.tokens[token_id]["allowed_actions"] = actions


def _stub_writes(worker) -> None:
    """The fake repo has no risk/protection writers; the external path needs them."""

    async def _update_run_risk(strategy_run_id, patch):
        return {"strategy_run_id": strategy_run_id, "risk": dict(patch or {})}

    async def _update_run_backend_protection(
        strategy_run_id,
        protection,
        state,
        **_kwargs,
    ):
        return {
            "strategy_run_id": strategy_run_id,
            "runtime_state": {
                "backend_protection": dict(protection or {}),
                "backend_protection_state": dict(state or {}),
            },
        }

    worker.update_run_risk = _update_run_risk
    worker.update_run_backend_protection = _update_run_backend_protection


def _client(app):
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_hosted_options_entry_is_refused_while_manual_work_waits(harness):
    """A child cannot enter an option run directly; it must request execution."""
    _repo, worker, app, _job, run_id, _t, _f = harness
    _grant_action(worker, CHILD_ID, "intents:submit")
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/options/runs/{run_id}/enter",
            headers=_child_headers(),
            json={},
        )
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["rejection_reason"] == "HOSTED_RAW_MUTATION_FORBIDDEN", detail
        assert detail["operation"] == "options.enter", detail
        assert detail["governed_surface"] == "/api/algo-workers/worker/executions", detail


@pytest.mark.asyncio
async def test_hosted_protection_disable_is_refused_by_name(harness):
    """Backend protection may not be disabled or relaxed through a child token."""
    _repo, worker, app, _job, run_id, _t, _f = harness
    _mount_protection(app)
    _grant_action(worker, CHILD_ID, "risk:update")
    async with _client(app) as client:
        response = await client.patch(
            f"{BASE}/worker/runs/{run_id}/protection",
            headers=_child_headers(),
            json={"backend_protection": {"enabled": False}, "reason": "quiet the exits"},
        )
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["rejection_reason"] == "HOSTED_RAW_MUTATION_FORBIDDEN", detail
        assert detail["operation"] == "protection:patch", detail


@pytest.mark.asyncio
async def test_hosted_risk_patch_of_an_owner_mandated_key_is_refused(harness):
    """Even a tightening is an owner decision; the child may not write the key."""
    _repo, worker, app, _job, run_id, _t, _f = harness
    _mount_protection(app)
    _grant_action(worker, CHILD_ID, "risk:update")
    async with _client(app) as client:
        for patch in (
            {"daily_loss_budget_inr": 1.0},
            {"admission_window_seconds": 10**9},
            # An unreadable/None value is refused all the same: the check is a
            # key vocabulary, not a numeric comparison.
            {"allocation_inr": None},
        ):
            response = await client.patch(
                f"{BASE}/worker/runs/{run_id}/risk",
                headers=_child_headers(),
                json={"patch": patch, "reason": "try to move the ceiling"},
            )
            assert response.status_code == 409, (patch, response.text)
            detail = response.json()["detail"]
            assert detail["rejection_reason"] == "HOSTED_OWNER_POLICY_MUTATION_FORBIDDEN", detail
            assert detail["operation"] == "risk:update", detail
            assert detail["owner_mandated_keys"] == sorted(patch), detail


@pytest.mark.asyncio
async def test_external_risk_and_protection_patches_are_unchanged(harness):
    """The external worker contract is untouched by the hosted refusal."""
    _repo, worker, app, _job, _run_id, _t, _f = harness
    _mount_protection(app)
    _stub_writes(worker)
    _grant_action(worker, EXTERNAL_ID, "risk:update")
    async with _client(app) as client:
        risk = await client.patch(
            f"{BASE}/worker/runs/run_external_1/risk",
            headers=_external_headers(),
            json={"patch": {"daily_loss_budget_inr": 5000.0}, "reason": "external policy"},
        )
        assert risk.status_code == 200, risk.text
        assert risk.json()["risk"] == {"daily_loss_budget_inr": 5000.0}

        protection = await client.patch(
            f"{BASE}/worker/runs/run_external_1/protection",
            headers=_external_headers(),
            json={"backend_protection": {"enabled": False}, "reason": "external choice"},
        )
        assert protection.status_code == 200, protection.text
