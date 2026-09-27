"""Hosted strategies -> auto-journal: decisions land on the fills' journal run.

The journal writes itself from execution truth. Fills already reach it (live via
``live_order_intents`` -> ``LiveJournalProjector``, paper via the paper runtime's
attribution). This module adds the WHY: each governed decision on a hosted
execution request is appended as a ``JournalDecisionEvent`` on the SAME journal
run, found by the same source keys the fill projectors use.

Best effort by design: a journal outage must never fail or delay execution, so
``record_decision`` swallows every error, and services call it only after their
own commit. The default recorder is a no-op; the app process installs the real
one at startup (``backend/app/bootstrap.py``).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

#: event -> (DecisionType value, DecisionActorType value)
DECISION_KINDS: Dict[str, tuple] = {
    "request_awaiting_approval": ("algo_trigger", "algo"),
    "request_auto_queued": ("algo_trigger", "system"),
    "request_refused": ("algo_trigger", "system"),
    "request_approved": ("review", "user"),
    "request_rejected": ("review", "user"),
    "protection_exit": ("exit", "system"),
}

_JOURNALED_ENVIRONMENTS = ("live", "paper")


def _no_op(**_kwargs: Any) -> None:
    return None


_recorder: Callable[..., None] = _no_op


def set_recorder(recorder: Optional[Callable[..., None]]) -> None:
    """Install the process's recorder; ``None`` restores the no-op."""
    global _recorder
    _recorder = recorder or _no_op


def record_decision(
    *,
    event: str,
    environment: str,
    strategy_run_id: str,
    account_id: str,
    summary: str,
    context: Mapping[str, Any],
) -> None:
    """Append one decision. Never raises."""
    try:
        _recorder(
            event=event,
            environment=environment,
            strategy_run_id=strategy_run_id,
            account_id=account_id,
            summary=summary,
            context=dict(context or {}),
        )
    except Exception:  # noqa: BLE001 - the journal must never break execution
        logger.warning("journal decision not recorded", extra={"event": event}, exc_info=True)


def request_event_for(view: Mapping[str, Any]) -> Optional[str]:
    """The decision an execution-request state represents, if any."""
    status = str(view.get("status") or "")
    if status == "awaiting_approval":
        return "request_awaiting_approval"
    if status == "queued":
        kind = str(view.get("decision_kind") or "")
        return "request_auto_queued" if kind == "automatic" else "request_approved"
    if status == "rejected":
        return "request_rejected"
    if status == "refused":
        return "request_refused"
    return None


def record_request_decision(view: Mapping[str, Any], *, actor: str = "") -> None:
    """Journal an execution request's current decision (after its commit)."""
    event = request_event_for(view)
    if event is None:
        return
    record_decision(
        event=event,
        environment=str(view.get("execution_environment") or ""),
        strategy_run_id=str(view.get("strategy_run_id") or ""),
        account_id=str(view.get("account_id") or ""),
        summary=f"{event}: plan {view.get('plan_id')}",
        context={
            "request_id": view.get("request_id"),
            "strategy_id": view.get("strategy_id"),
            "plan_id": view.get("plan_id"),
            "status": view.get("status"),
            "refusal_code": view.get("refusal_code"),
            "decision_kind": view.get("decision_kind"),
            "decision_actor": view.get("decision_actor") or actor or None,
            "grant_id": view.get("grant_id"),
            "authorization_mode": view.get("authorization_mode"),
        },
    )


class JournalDecisionRecorder:
    """The production recorder: resolve the fills' journal run, append the event."""

    def __init__(self, journal_service: Any = None) -> None:
        self._journal_service = journal_service

    def _service(self) -> Any:
        if self._journal_service is None:
            from backend.journaling.service import JournalService

            self._journal_service = JournalService()
        return self._journal_service

    def _run_id(self, *, environment: str, strategy_run_id: str, account_id: str) -> Optional[str]:
        service = self._service()
        if environment == "live":
            return service.repository.ensure_live_strategy_run_for_intent(
                intent={"account_id": account_id, "strategy_run_id": strategy_run_id}
            )
        return service.ensure_paper_strategy_run(
            attribution={
                "strategy_run_id": strategy_run_id,
                "account_ref": account_id,
                "execution_mode": "paper",
            }
        )

    def __call__(
        self,
        *,
        event: str,
        environment: str,
        strategy_run_id: str,
        account_id: str,
        summary: str,
        context: Mapping[str, Any],
    ) -> None:
        if environment not in _JOURNALED_ENVIRONMENTS or not strategy_run_id or not account_id:
            return
        kinds = DECISION_KINDS.get(event)
        if kinds is None:
            return
        from backend.journaling.models import JournalDecisionEvent

        run_id = self._run_id(
            environment=environment, strategy_run_id=strategy_run_id, account_id=account_id
        )
        if not run_id:
            return
        decision_type, actor_type = kinds
        self._service().append_decision_event(
            str(run_id),
            JournalDecisionEvent(
                run_id=str(run_id),
                decision_type=decision_type,
                actor_type=actor_type,
                summary=summary,
                context={**dict(context or {}), "event": event, "source": "hosted_strategy"},
            ),
        )
