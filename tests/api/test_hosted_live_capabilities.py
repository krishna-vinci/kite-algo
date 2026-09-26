"""The hosted capability surface must never advertise what the server refuses.

``GET /api/strategies/options`` is what a client renders its mode and lane pickers
from, so it is a SAFETY surface: advertising ``live`` while the deployment has
hosted live disabled, or naming a lane whose builder is not wired, would invite a
user into a configuration the server will reject. These tests pin the contract at
the handler (authenticated owner, no database needed).
"""

from __future__ import annotations

import asyncio

import pytest

from backend.api.routers import strategies as strategies_router


@pytest.fixture(autouse=True)
def _authorized_scopes(monkeypatch):
    monkeypatch.setattr(strategies_router, "authorized_account_scopes", lambda: ["kite:paper"])


def test_live_is_not_advertised_while_the_deployment_flag_is_off(monkeypatch):
    monkeypatch.delenv("HOSTED_LIVE_ENABLED", raising=False)
    # Even a deployment whose per-lane allowlist names every lane advertises
    # nothing while the master gate is off.
    monkeypatch.setenv("HOSTED_LIVE_LANES", "cnc,mis,futures,options")

    body = asyncio.run(strategies_router.get_hosted_options(owner="app:admin"))

    assert body.account_scopes == ["kite:paper"]
    # The ordinary modes are retained exactly as before, and live is withheld.
    assert set(body.execution_modes) == {"paper", "dry_run"}
    assert body.job_kinds == ["continuous", "finite"]
    assert body.stale_exit_policies == ["none", "exit_on_worker_stale"]
    assert body.live_lanes == []
    assert body.live_requires_owner_approval is True


def test_live_is_advertised_with_its_wired_lanes_when_enabled(monkeypatch):
    monkeypatch.setenv("HOSTED_LIVE_ENABLED", "true")
    monkeypatch.setenv("HOSTED_LIVE_LANES", "cnc,mis,futures,options")

    body = asyncio.run(strategies_router.get_hosted_options(owner="app:admin"))

    assert set(body.execution_modes) == {"paper", "dry_run", "live"}
    assert body.live_lanes == ["cnc", "mis", "futures", "options"]
    assert body.live_requires_owner_approval is True
    # Nothing invented: only the lanes the server actually wired.
    assert set(body.live_lanes) <= {"cnc", "mis", "futures", "options"}


def test_an_unwired_lane_is_not_advertised(monkeypatch):
    """A lane whose builder is missing disappears, even with live enabled."""
    monkeypatch.setenv("HOSTED_LIVE_ENABLED", "true")
    monkeypatch.setenv("HOSTED_LIVE_LANES", "cnc,mis,futures,options")

    from backend.strategies import live_sequence

    builders = live_sequence.live_lane_builders()
    trimmed = {
        lane: builder
        for lane, builder in builders.items()
        if lane != live_sequence.LANE_OPTION_STRUCTURE
    }
    monkeypatch.setattr(live_sequence, "_LANE_BUILDERS", trimmed)

    body = asyncio.run(strategies_router.get_hosted_options(owner="app:admin"))

    assert "options" not in body.live_lanes, body.live_lanes
    assert body.live_lanes == ["cnc", "mis", "futures"], body.live_lanes
    # And an arbitrary lane name is never reported, whatever a client asks for.
    assert "equities_only" not in body.live_lanes


def test_only_the_allowed_lanes_are_advertised(monkeypatch):
    """``HOSTED_LIVE_LANES`` narrows the capability surface, default deny."""
    monkeypatch.setenv("HOSTED_LIVE_ENABLED", "true")
    monkeypatch.delenv("HOSTED_LIVE_LANES", raising=False)

    body = asyncio.run(strategies_router.get_hosted_options(owner="app:admin"))
    assert body.live_lanes == [], body.live_lanes

    monkeypatch.setenv("HOSTED_LIVE_LANES", "options,equities_only")
    body = asyncio.run(strategies_router.get_hosted_options(owner="app:admin"))
    assert body.live_lanes == ["options"], body.live_lanes
