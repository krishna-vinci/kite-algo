"""The market session, as EVIDENCE, for the live execution suites.

Every live PG suite drives real execution on its own clock - a fixed instant, or
the wall clock - inside a disposable database that carries no imported NSE
calendar. The production gate would therefore refuse every live increase, which
is its honest answer (the market clock is not something a test may invent), so
these suites supply the session through the SAME two seams production uses
instead of weakening the gate:

* :func:`open_session_provider` plugs into ``AdmissionService``'s
  ``market_session_provider`` seam for suites that construct their services
  explicitly;
* :data:`open_market_session` is an autouse fixture that monkeypatches the shared
  ``market_session`` read, for suites whose services are built deep inside a
  request path;
* :func:`weekend_session_provider` is the REAL helper pinned to a known Saturday,
  so a test can prove the gate still refuses without weakening it.

There is no test backdoor in production code: these are the platform's own
seams, driven from the test side.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from backend.strategies import market_session

__all__ = [
    "open_market_session",
    "open_session_provider",
    "REAL_SESSION_STATE",
    "SATURDAY",
    "weekend_session_provider",
]

#: The production read, captured at import so that a stubbed module attribute
#: cannot change what "the real gate" means for a test that wants it.
REAL_SESSION_STATE = market_session.session_state

#: 2026-09-26 is a Saturday; 06:00 UTC is 11:30 IST, inside the session clock.
SATURDAY = datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc)


def _exchange(exchange: Any) -> str:
    return str(exchange or "").strip().upper()


def open_session_provider(exchange: Any, _now: Optional[datetime] = None) -> Dict[str, Any]:
    """Every gated exchange reads as OPEN: the evidence a suite supplies."""
    return {
        "exchange": _exchange(exchange),
        "open": True,
        "reason": "open",
        "session_date": None,
        "next_open": None,
    }


def weekend_session_provider(exchange: Any, _now: Optional[datetime] = None) -> Dict[str, Any]:
    """The REAL helper's answer for a known Saturday: a shut market, honestly."""
    return dict(REAL_SESSION_STATE(exchange, SATURDAY))


def stub_open_market_session(monkeypatch) -> None:
    """Make the shared market-session read say OPEN for this test.

    ``AdmissionService`` binds its default provider when it is CONSTRUCTED, so
    the patch has to be installed before the service is built - which it is: the
    fixture runs first, and every live PG suite builds its services (adapters,
    pipelines, routers) inside the test body. Only ``session_state`` is patched,
    so the REAL helper stays real for a test that drives it directly.
    """
    monkeypatch.setattr(market_session, "session_state", open_session_provider)


@pytest.fixture(autouse=True)
def open_market_session(monkeypatch):
    """The live suites' session: open, supplied as evidence, never guessed."""
    stub_open_market_session(monkeypatch)
