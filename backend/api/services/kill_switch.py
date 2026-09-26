"""Platform kill switch: stop every job, flatten every exposed book, close lanes.

One operator action that spans the whole platform. It does not invent a second
execution engine: each step is the governed path the per-strategy surfaces
already use.

* **Stop every running hosted job.** The hosted job ledger's own stop verbs
  (``stop_queued_job`` / ``request_stop_active``), the same ones the operator
  Stop route uses.
* **Flatten every strategy with live or paper exposure.** ``OwnerActionsService
  .flatten`` per ``(strategy, account, environment)``: the same stop-proof,
  cancel-pending, option-run exit and non-option reduction orchestration the
  owner-actions flatten route runs, so pending entry work is cancelled first and
  what remains is reduced through the live/paper executor.
* **Close every live lane.** ``platform_live_settings`` set to all-closed with
  the operator's reason in the audit row, so no NEW live exposure can open while
  the operator investigates. Reductions, exits and repair keep working.

The operation is DURABLE before any work moves and RESUMABLE: the platform's
append-only ``strategy_proposal_journal`` carries one header row (the operation
identity) and one row per target strategy, and the per-strategy progress is
re-derived from each strategy's own flatten operation. A second POST while the
operation is still open returns the SAME operation rather than starting a
parallel one.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from fastapi import HTTPException
from sqlalchemy import select

from backend.api.services.owner_actions import OwnerActionRefusal, owner_action_scope
from backend.platform.settings import LIVE_LANE_KEYS, update_live_settings
from backend.strategies.attribution_models import (
    StrategyPositionProjection,
    StrategyProposalJournal,
)
from backend.strategies.models import HostedStrategy

__all__ = [
    "KILL_SWITCH_CONFIRM",
    "KILL_SWITCH_CONFIRM_REQUIRED",
    "KillSwitchRefusal",
    "KillSwitchService",
    "KillSwitchStore",
    "strategy_exposure_targets",
]

#: The exact body the operator must send; a call without it moves nothing.
KILL_SWITCH_CONFIRM = "FLATTEN ALL"
KILL_SWITCH_CONFIRM_REQUIRED = "KILL_SWITCH_CONFIRM_REQUIRED"

#: The journal vocabulary the operation is recorded under. ``owner_action`` is
#: the existing event for an operator action; the reason code names the operation.
KILL_SWITCH_REASON_CODE = "kill_switch"
KILL_SWITCH_EVENT = "owner_action"
KILL_SWITCH_HEADER_KIND = "kill_switch"
KILL_SWITCH_TARGET_KIND = "kill_switch_target"
#: The header row's ``strategy_id``. Empty on purpose: the header is a
#: platform-scoped record, and no real strategy id equals the empty string.
KILL_SWITCH_HEADER_STRATEGY = ""

STATUS_COMPLETE = "complete"
STATUS_IN_PROGRESS = "in_progress"
STATUS_BLOCKED = "blocked"

#: The hosted job statuses this action stops.
ACTIVE_JOB_STATUSES = ("queued", "starting", "running")
#: The environments whose exposure the kill switch flattens.
EXPOSURE_ENVIRONMENTS = ("live", "paper")

#: Serializes the "is one open? - else create" decision inside ONE process.
#: The hosted deployment is a single app process, and the append-only journal
#: cannot carry a partial unique index without a migration; this keeps two
#: simultaneous operator clicks from starting two parallel kill switches. One
#: lock per running loop, so a test suite that drives several loops is unaffected.
_START_LOCKS: Dict[Any, asyncio.Lock] = {}


def _start_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _START_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _START_LOCKS[loop] = lock
    return lock


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class KillSwitchRefusal(RuntimeError):
    """A named kill-switch refusal, mapped by the router to an HTTP error."""

    def __init__(
        self,
        reason_code: str,
        detail: Optional[Mapping[str, Any]] = None,
        *,
        status_code: int = 409,
    ) -> None:
        super().__init__(str(reason_code))
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})
        self.status_code = int(status_code)

    def as_detail(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"rejection_reason": self.reason_code}
        payload.update(self.detail)
        return payload


def strategy_exposure_targets(
    session_factory: Any, strategy_ids: Sequence[str]
) -> List[Dict[str, Any]]:
    """Every ``(strategy, account, environment)`` with a NON-ZERO attributed book.

    The projection is the platform's own attributed truth, so a strategy with no
    live or paper exposure is simply not a flatten target. A read that fails
    raises rather than reporting an empty platform: the caller must not conclude
    "nothing to do" from an unreadable book.
    """
    wanted = sorted({str(value) for value in strategy_ids if str(value or "")})
    if not wanted:
        return []
    with session_factory() as session:
        rows = session.execute(
            select(
                StrategyPositionProjection.strategy_id,
                StrategyPositionProjection.account_id,
                StrategyPositionProjection.execution_environment,
                StrategyPositionProjection.identity_kind,
                StrategyPositionProjection.identity_key,
                StrategyPositionProjection.product,
                StrategyPositionProjection.net_quantity,
            ).where(
                StrategyPositionProjection.strategy_id.in_(wanted),
                StrategyPositionProjection.execution_environment.in_(
                    EXPOSURE_ENVIRONMENTS
                ),
            )
        ).all()
    exposed: set[tuple] = set()
    for strategy_id, account_id, environment, _kind, _key, _product, quantity in rows:
        if int(quantity or 0) != 0:
            exposed.add((str(strategy_id), str(account_id), str(environment)))
    return [
        {
            "strategy_id": strategy_id,
            "account_id": account_id,
            "execution_environment": environment,
        }
        for strategy_id, account_id, environment in sorted(exposed)
    ]


class KillSwitchStore:
    """Durable, resumable kill-switch operations over ``strategy_proposal_journal``.

    One header row plus one row per target strategy, all sharing
    ``evaluation_id = operation_id``. The journal is append-only, so the
    operation identity is written once and the per-strategy progress is always
    re-derived from the strategy's own flatten operation - a stored item outcome
    could only go stale.
    """

    def __init__(self, *, session_factory: Any) -> None:
        self.session_factory = session_factory

    def record(
        self,
        *,
        operation_id: str,
        actor_id: str,
        reason: str,
        confirm: str,
        targets: Sequence[Mapping[str, Any]],
        created_at: Optional[datetime] = None,
    ) -> None:
        moment = created_at or _utcnow()
        rows = [
            StrategyProposalJournal(
                id=str(uuid.uuid4()),
                strategy_id=KILL_SWITCH_HEADER_STRATEGY,
                evaluation_id=str(operation_id),
                proposal_id=None,
                event=KILL_SWITCH_EVENT,
                reason_code=KILL_SWITCH_REASON_CODE,
                detail={
                    "kind": KILL_SWITCH_HEADER_KIND,
                    "actor_id": str(actor_id),
                    "reason": str(reason or ""),
                    "confirm": str(confirm),
                    "target_count": len(list(targets)),
                },
                created_at=moment,
            )
        ]
        for target in targets:
            rows.append(
                StrategyProposalJournal(
                    id=str(uuid.uuid4()),
                    strategy_id=str(target["strategy_id"]),
                    evaluation_id=str(operation_id),
                    proposal_id=None,
                    event=KILL_SWITCH_EVENT,
                    reason_code=KILL_SWITCH_REASON_CODE,
                    detail={
                        "kind": KILL_SWITCH_TARGET_KIND,
                        "owner_id": str(target.get("owner_id") or ""),
                        "account_id": str(target.get("account_id") or ""),
                        "execution_environment": str(
                            target.get("execution_environment") or ""
                        ),
                    },
                    created_at=moment,
                )
            )
        with self.session_factory() as session:
            session.add_all(rows)
            session.commit()

    def latest(self) -> Optional[Dict[str, Any]]:
        """The most recent kill-switch operation, header + targets, or ``None``."""
        with self.session_factory() as session:
            operation_id = session.execute(
                select(StrategyProposalJournal.evaluation_id)
                .where(
                    StrategyProposalJournal.reason_code == KILL_SWITCH_REASON_CODE
                )
                .order_by(
                    StrategyProposalJournal.created_at.desc(),
                    StrategyProposalJournal.id.desc(),
                )
                .limit(1)
            ).scalars().first()
            if operation_id is None:
                return None
            rows = session.execute(
                select(
                    StrategyProposalJournal.strategy_id,
                    StrategyProposalJournal.detail,
                    StrategyProposalJournal.created_at,
                ).where(
                    StrategyProposalJournal.reason_code == KILL_SWITCH_REASON_CODE,
                    StrategyProposalJournal.evaluation_id == str(operation_id),
                )
            ).all()
        header: Optional[Dict[str, Any]] = None
        targets: List[Dict[str, Any]] = []
        for strategy_id, detail, created_at in rows:
            payload = dict(detail or {})
            kind = str(payload.get("kind") or "")
            if kind == KILL_SWITCH_HEADER_KIND and header is None:
                header = {
                    "operation_id": str(operation_id),
                    "actor_id": str(payload.get("actor_id") or ""),
                    "reason": str(payload.get("reason") or ""),
                    "confirm": str(payload.get("confirm") or ""),
                    "created_at": (
                        created_at.isoformat()
                        if hasattr(created_at, "isoformat")
                        else None
                    ),
                }
            elif kind == KILL_SWITCH_TARGET_KIND:
                targets.append(
                    {
                        "strategy_id": str(strategy_id),
                        "owner_id": str(payload.get("owner_id") or ""),
                        "account_id": str(payload.get("account_id") or ""),
                        "execution_environment": str(
                            payload.get("execution_environment") or ""
                        ),
                    }
                )
        if header is None:
            return None
        targets.sort(
            key=lambda row: (
                row["strategy_id"],
                row["account_id"],
                row["execution_environment"],
            )
        )
        return {"header": header, "targets": targets}


class KillSwitchService:
    """Orchestrate one kill switch over the platform's governed owner actions."""

    def __init__(
        self,
        *,
        session_factory: Any,
        repository: Any,
        flatten: Callable[..., Awaitable[Dict[str, Any]]],
        flatten_status: Callable[[Mapping[str, Any]], Optional[Dict[str, Any]]],
        close_lanes: Optional[Callable[..., Any]] = None,
        store: Optional[KillSwitchStore] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.session_factory = session_factory
        self.repository = repository
        #: ``async (scope, *, reason, actor) -> dict``: the SAME governed flatten
        #: the owner-actions route runs. Absent, the platform cannot flatten.
        self._flatten = flatten
        #: ``(scope) -> dict | None``: the strategy's latest flatten operation, or
        #: ``None`` when it has none. Read-only.
        self._flatten_status = flatten_status
        self._close_lanes = close_lanes or self._default_close_lanes
        self.store = store or KillSwitchStore(session_factory=session_factory)
        self._clock = clock or _utcnow

    # ------------------------------------------------------------- helpers

    def _default_close_lanes(
        self, reason: str, actor: str, session_factory: Any
    ) -> Dict[str, bool]:
        settings = update_live_settings(
            {lane: False for lane in LIVE_LANE_KEYS},
            actor_id=str(actor),
            reason=str(reason or ""),
            session_factory=session_factory,
        )
        return dict(settings.lanes)

    def _scope(
        self, target: Mapping[str, Any]
    ) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """The target's ``(account, environment)`` scope, or the refusal to name."""
        try:
            return (
                owner_action_scope(
                    self.repository,
                    str(target.get("owner_id") or ""),
                    str(target["strategy_id"]),
                    str(target["execution_environment"]),
                ),
                None,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, Mapping) else {}
            return None, {
                "rejection_reason": str(
                    detail.get("rejection_reason") or "STRATEGY_SCOPE_UNAVAILABLE"
                ),
                "status_code": int(exc.status_code),
                "message": str(
                    detail.get("message") or exc.detail or ""
                ),
            }

    def _all_strategies(self) -> List[Any]:
        """Every hosted strategy on the platform, owner included.

        The kill switch spans the whole platform (like the lane settings it also
        writes), not only the calling operator's own strategies; each strategy's
        own owner scopes its book and its job ledger.
        """
        with self.session_factory() as session:
            return list(session.execute(select(HostedStrategy)).scalars())

    def _stop_jobs(
        self, strategies: Sequence[Any], *, actor: str
    ) -> List[Dict[str, Any]]:
        """Stop every active hosted job across the platform, with evidence."""
        outcomes: List[Dict[str, Any]] = []
        for strategy in strategies:
            strategy_id = str(getattr(strategy, "id", "") or "")
            owner = str(getattr(strategy, "owner_id", "") or "")
            try:
                jobs = self.repository.list_jobs_for_strategy(
                    owner, strategy_id, limit=200
                )
            except Exception as exc:  # noqa: BLE001 - an unreadable ledger is named
                outcomes.append(
                    {
                        "strategy_id": strategy_id,
                        "job_id": None,
                        "outcome": "unreadable",
                        "error": type(exc).__name__,
                    }
                )
                continue
            for job in jobs or []:
                status = str(getattr(job, "status", "") or "")
                if status not in ACTIVE_JOB_STATUSES:
                    continue
                job_id = str(getattr(job, "id", "") or "")
                attempt = int(getattr(job, "attempt", 0) or 0)
                try:
                    if status == "queued":
                        stopped = self.repository.stop_queued_job(
                            job_id,
                            owner_id=owner,
                            expected_attempt=attempt,
                            actor=actor,
                        )
                    else:
                        stopped = self.repository.request_stop_active(
                            job_id,
                            owner_id=owner,
                            expected_attempt=attempt,
                            actor=actor,
                        )
                except Exception as exc:  # noqa: BLE001 - a failed stop is named
                    outcomes.append(
                        {
                            "strategy_id": strategy_id,
                            "job_id": job_id,
                            "attempt": attempt,
                            "status": status,
                            "outcome": "failed",
                            "error": type(exc).__name__,
                        }
                    )
                    continue
                outcomes.append(
                    {
                        "strategy_id": strategy_id,
                        "job_id": job_id,
                        "attempt": attempt,
                        "status": status,
                        "outcome": (
                            "stop_requested"
                            if status in ("starting", "running")
                            else "stopped"
                        )
                        if stopped
                        else "already_stopping",
                    }
                )
        return outcomes

    def _progress(self, target: Mapping[str, Any]) -> Dict[str, Any]:
        """One target's CURRENT progress from its own flatten operation."""
        base = {
            "strategy_id": str(target["strategy_id"]),
            "account_id": str(target.get("account_id") or ""),
            "execution_environment": str(target.get("execution_environment") or ""),
            "operation_id": None,
            "status": STATUS_BLOCKED,
            "missing": [],
            "refusal": None,
        }
        scope, refusal = self._scope(target)
        if scope is None:
            base["refusal"] = (refusal or {}).get("rejection_reason")
            base["detail"] = refusal or {}
            return base
        operation = self._flatten_status(scope)
        if operation is None:
            base["refusal"] = "FLATTEN_OPERATION_NONE"
            return base
        base["operation_id"] = str(operation.get("operation_id") or "")
        base["status"] = str(operation.get("status") or STATUS_BLOCKED)
        base["missing"] = [str(name) for name in (operation.get("missing") or [])]
        base["refusal"] = operation.get("refusal")
        return base

    async def _run(
        self, target: Mapping[str, Any], actor: str, reason: str
    ) -> Dict[str, Any]:
        """Run the governed flatten for ONE target, then report its progress."""
        scope, refusal = self._scope(target)
        if scope is None:
            return {
                "strategy_id": str(target["strategy_id"]),
                "account_id": str(target.get("account_id") or ""),
                "execution_environment": str(
                    target.get("execution_environment") or ""
                ),
                "operation_id": None,
                "status": STATUS_BLOCKED,
                "missing": [],
                "refusal": (refusal or {}).get("rejection_reason"),
                "detail": refusal or {},
            }
        try:
            await self._flatten(scope, reason=reason, actor=actor)
        except OwnerActionRefusal as exc:
            return {
                "strategy_id": str(target["strategy_id"]),
                "account_id": str(target.get("account_id") or ""),
                "execution_environment": str(
                    target.get("execution_environment") or ""
                ),
                "operation_id": None,
                "status": STATUS_BLOCKED,
                "missing": [],
                "refusal": str(exc.reason_code),
                "detail": exc.as_detail(),
            }
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, Mapping) else {}
            return {
                "strategy_id": str(target["strategy_id"]),
                "account_id": str(target.get("account_id") or ""),
                "execution_environment": str(
                    target.get("execution_environment") or ""
                ),
                "operation_id": None,
                "status": STATUS_BLOCKED,
                "missing": [],
                "refusal": str(
                    detail.get("rejection_reason") or "FLATTEN_REFUSED"
                ),
                "detail": dict(detail),
            }
        return self._progress(target)

    @staticmethod
    def _aggregate(progress: Sequence[Mapping[str, Any]]) -> str:
        if not progress:
            return STATUS_COMPLETE
        statuses = {str(row.get("status") or "") for row in progress}
        if statuses <= {STATUS_COMPLETE}:
            return STATUS_COMPLETE
        if STATUS_BLOCKED in statuses:
            return STATUS_BLOCKED
        return STATUS_IN_PROGRESS

    def _snapshot(
        self,
        operation: Mapping[str, Any],
        progress: Sequence[Mapping[str, Any]],
        *,
        idempotent: bool,
        jobs: Sequence[Mapping[str, Any]] = (),
        lanes_closed: Optional[Mapping[str, bool]] = None,
    ) -> Dict[str, Any]:
        header = dict(operation.get("header") or {})
        return {
            "operation_id": str(header.get("operation_id") or ""),
            "status": self._aggregate(progress),
            "idempotent": bool(idempotent),
            "actor_id": header.get("actor_id"),
            "reason": str(header.get("reason") or ""),
            "created_at": header.get("created_at"),
            "strategies": [dict(row) for row in progress],
            "jobs": [dict(row) for row in jobs],
            "lanes_closed": (
                {lane: bool(value) for lane, value in lanes_closed.items()}
                if lanes_closed is not None
                else None
            ),
        }

    # -------------------------------------------------------------- public

    def latest(self) -> Optional[Dict[str, Any]]:
        """The latest operation's snapshot, re-derived - or ``None``."""
        operation = self.store.latest()
        if operation is None:
            return None
        progress = [self._progress(target) for target in operation["targets"]]
        return self._snapshot(operation, progress, idempotent=False)

    async def start(
        self, *, owner: str, reason: str, confirm: str
    ) -> Dict[str, Any]:
        """Run (or resume) the kill switch, or refuse without the confirm word."""
        async with _start_lock():
            return await self._start(owner=owner, reason=reason, confirm=confirm)

    async def _start(
        self, *, owner: str, reason: str, confirm: str
    ) -> Dict[str, Any]:
        reason = str(reason or "")
        if str(confirm or "") != KILL_SWITCH_CONFIRM:
            raise KillSwitchRefusal(
                KILL_SWITCH_CONFIRM_REQUIRED,
                {
                    "expected": KILL_SWITCH_CONFIRM,
                    "message": (
                        "the kill switch requires the exact confirmation body "
                        f"\"confirm\": \"{KILL_SWITCH_CONFIRM}\""
                    ),
                },
                status_code=422,
            )
        existing = self.store.latest()
        if existing is not None:
            current = self._snapshot(
                existing,
                [self._progress(target) for target in existing["targets"]],
                idempotent=True,
            )
            if current["status"] != STATUS_COMPLETE:
                # Resume the SAME operation: the flattens are resumable, so a
                # second call advances what is left instead of starting a new one.
                progress = [
                    await self._run(target, owner, reason)
                    for target in existing["targets"]
                ]
                return self._snapshot(existing, progress, idempotent=True)

        strategies = self._all_strategies()
        owners = {
            str(getattr(row, "id", "") or ""): str(
                getattr(row, "owner_id", "") or ""
            )
            for row in strategies
        }
        targets = strategy_exposure_targets(
            self.session_factory, list(owners.keys())
        )
        for target in targets:
            target["owner_id"] = owners.get(str(target["strategy_id"]), "")
        operation_id = str(uuid.uuid4())
        self.store.record(
            operation_id=operation_id,
            actor_id=owner,
            reason=reason,
            confirm=str(confirm),
            targets=targets,
            created_at=self._clock(),
        )
        jobs = self._stop_jobs(strategies, actor=owner)
        lanes_closed = self._close_lanes(reason, owner, self.session_factory)
        progress = [await self._run(target, owner, reason) for target in targets]
        operation = {
            "header": {
                "operation_id": operation_id,
                "actor_id": owner,
                "reason": reason,
                "confirm": str(confirm),
                "created_at": self._clock().isoformat(),
            },
            "targets": targets,
        }
        return self._snapshot(
            operation,
            progress,
            idempotent=False,
            jobs=jobs,
            lanes_closed=lanes_closed,
        )
