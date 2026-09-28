"""Database-backed reasons that keep option-chain sessions running."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)


class PinRequirements(dict[str, set[str]]):
    """Pin map carrying whether the database read itself failed."""

    def __init__(self, *args: Any, read_failed: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.read_failed = read_failed


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _underlying(value: Any) -> str:
    return str(value or "").strip().upper()


def required_underlyings(session_factory: Any) -> dict[str, set[str]]:
    """Return option-chain underlyings pinned by positions or hosted strategy work.

    Reads are fail-safe: a database error returns an empty mapping and is marked
    on the concrete mapping so the session manager can retain its last good pins.
    """
    pins: PinRequirements = PinRequirements()
    session = None
    try:
        session = session_factory()
        position_rows = (
            session.execute(
                text(
                    """
                    SELECT s.legs, p.resolved_plan
                    FROM public.option_protection_owners o
                    JOIN public.option_run_states s
                      ON s.strategy_run_id = o.option_run_id
                    LEFT JOIN public.strategy_plan_option_runs r
                      ON r.option_run_id = o.option_run_id
                     AND r.phase = 'entry'
                    LEFT JOIN public.strategy_plans p
                      ON p.plan_id = r.plan_id
                    WHERE o.state = 'active'
                    """
                )
            )
            .mappings()
            .all()
        )
        for row in position_rows:
            underlying = ""
            for leg in _json_list(row.get("legs")):
                if isinstance(leg, dict):
                    underlying = _underlying(leg.get("underlying"))
                    if underlying:
                        break
            if not underlying:
                underlying = _underlying(
                    _json_object(row.get("resolved_plan")).get("underlying")
                )
            if underlying:
                pins.setdefault(underlying, set()).add("position")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        strategy_rows = (
            session.execute(
                text(
                    """
                    SELECT p.resolved_plan
                    FROM public.strategy_jobs j
                    JOIN public.strategy_proposals q
                      ON q.job_id = j.id
                    JOIN public.strategy_plans p
                      ON p.proposal_id = q.proposal_id
                    WHERE j.status IN ('queued', 'starting', 'running')
                      AND p.plan_kind = 'option_structure'
                      AND p.created_at >= :cutoff
                    """
                ),
                {"cutoff": cutoff},
            )
            .mappings()
            .all()
        )
        for row in strategy_rows:
            underlying = _underlying(
                _json_object(row.get("resolved_plan")).get("underlying")
            )
            if underlying:
                pins.setdefault(underlying, set()).add("strategy")
        return pins
    except Exception as exc:  # noqa: BLE001 - pin reads must never stop sessions
        logger.warning("Unable to read option session pins: %s", exc, exc_info=True)
        return PinRequirements(read_failed=True)
    finally:
        if session is not None:
            session.close()
