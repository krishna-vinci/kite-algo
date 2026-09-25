"""Run-bound view of a strategy's OWN book and its pending execution work.

This is an interface onto records that already exist - the attributed position
projection, the plan execution trail, the live step claims and the governed
execution requests - never another ledger and never an account-net allocation
guess.

Three rules decide the shape of the answer:

* **One coordinate, one row.** A live step is ONE unit of work: its plan-trail
  event and its ``live_plan_submissions`` claim are merged on
  ``(plan_id, step_no)``, never concatenated, and a fill is never counted as
  remaining work twice.
* **Attributed, never relabelled.** Every row is filtered through the
  authoritative ``strategy_run_bindings`` of the requested
  ``(account, strategy, environment)``; each row reports the step's own
  instrument, product and side, so a caller can act on it without guessing.
* **Unknown is unknown.** An unpublished projection, a projection whose freshness
  cannot be proven, or a unit with no quantity evidence makes the OVERALL
  coverage ``unknown``. Work is read per STRATEGY rather than per attempt, so a
  disposable child's lifetime cannot make durable strategy exposure look flat.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import select, text

from backend.strategies.attribution_models import (
    LivePlanSubmission,
    StrategyPlan,
    StrategyPlanExecutionEvent,
    StrategyPlanOptionRun,
    StrategyPositionProjection,
    StrategyProposal,
    StrategyProjectionState,
    StrategyRunBinding,
)
from backend.strategies.models import HostedExecutionRequest

#: How long a published projection is trusted as fresh evidence. Past it the
#: snapshot still reports the rows it has, but says the coverage is unknown.
DEFAULT_POSITION_STALENESS_SECONDS = 900

#: Live step states from the ACTUAL model vocabulary (migrations 20260921_000038
#: / 20260922_000040 plus the ingestion vocabulary in ``live_ingestion``):
#: terminal is ``filled`` / ``rejected`` / ``no_op`` / ``residual_abandoned``;
#: everything else is work that is not settled.
TERMINAL_SUBMISSION_STATES = ("filled", "rejected", "no_op", "residual_abandoned")
PENDING_SUBMISSION_STATES = (
    "withheld",
    "releasing",
    "pending",
    "partial",
    "finalizing",
    "rejecting",
    "repair_required",
    "uncertain",
)

#: Plan-trail events that leave work outstanding for a step.
PENDING_EVENT_STATES = ("submitted", "partially_filled")

#: Events that close a step's work.
TERMINAL_EVENT_STATES = ("filled", "rejected", "failed", "no_op")

#: The order a step's events occur in; the tie-breaker for two rows written on
#: the same instant by one transaction.
EVENT_RANK = {
    "submitted": 0,
    "partially_filled": 1,
    "filled": 2,
    "rejected": 3,
    "failed": 4,
    "no_op": 5,
}

#: Request states that represent work a strategy has asked for but not finished.
#: ``executed`` is excluded on purpose: that work is represented by its plan's
#: own steps, and counting the request too would double it.
PENDING_REQUEST_STATUSES = (
    "requested",
    "awaiting_approval",
    "queued",
    "dispatching",
    "dispatch_unresolved",
)

#: How many option runs one snapshot reports before it declares truncation.
OPTION_RUN_LIMIT = 50


def _leg_forms(leg: Mapping[str, Any]) -> tuple:
    """One leg's two identity forms, tolerating both persisted shapes.

    A frozen plan leg carries ``instrument_id`` directly; a durable run leg keeps
    it under ``metadata``. Both are compared by identity, never by list position.
    """
    if not isinstance(leg, Mapping):
        return "", ""
    value = leg.get("instrument_id")
    if not value:
        metadata = leg.get("metadata")
        if isinstance(metadata, Mapping):
            value = metadata.get("instrument_id")
    symbol = str(leg.get("tradingsymbol") or leg.get("broker_symbol") or "").strip().upper()
    return (str(value) if value else ""), symbol


def _run_legs_contradict_binding(
    run_legs: Sequence[Mapping[str, Any]], frozen_legs: Sequence[Mapping[str, Any]]
) -> bool:
    """Whether a run's legs prove it is NOT the structure this edge froze.

    Only a positive contradiction counts: at least one run leg has to be
    comparable, and no run leg may match any frozen leg by instrument id or by
    symbol. Either side being unreadable (or carrying no identity at all) leaves
    nothing to contradict, and the caller keeps its normal coverage rules.
    """
    if not run_legs or not frozen_legs:
        return False
    frozen_forms = [_leg_forms(leg) for leg in frozen_legs]
    comparable = False
    for leg in run_legs:
        run_id, run_symbol = _leg_forms(leg)
        if not run_id and not run_symbol:
            continue
        comparable = True
        for frozen_id, frozen_symbol in frozen_forms:
            if run_id and frozen_id and run_id == frozen_id:
                return False
            if run_symbol and frozen_symbol and run_symbol == frozen_symbol:
                return False
    return comparable


def _protective_exit_unresolved(orders: Any) -> bool:
    """Whether a run's own durable records still own an unresolved exit stage.

    The rule lives with the staged-exit engine (``unresolved_stage_claim``); this
    asks it rather than re-deriving it, so the snapshot and the exit path can
    never disagree about what "unresolved" means. An unreadable rule is reported
    as unresolved: not knowing is never proof that a committed stage settled.
    """
    try:
        from backend.options.protection.staged_exit import unresolved_stage_claim
    except Exception:  # noqa: BLE001 - an unimportable rule cannot prove resolution
        return True
    try:
        return unresolved_stage_claim(orders) is not None
    except Exception:  # noqa: BLE001 - an unreadable payload keeps the claim
        return True


def _run_metadata(state: Any) -> Mapping[str, Any]:
    """A run's own metadata as a mapping, whichever shape the driver handed back.

    ``jsonb`` arrives decoded on PostgreSQL and may arrive as the JSON document
    itself elsewhere. An unreadable value is EMPTY rather than guessed: every
    caller here treats a missing key as "the first generation / no recorded
    shape", which is what a run the adjust engine has never touched holds.
    """
    metadata = (state or {}).get("metadata") if isinstance(state, Mapping) else None
    if isinstance(metadata, (str, bytes, bytearray)):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return {}
    if not isinstance(metadata, Mapping):
        return {}
    return metadata


def _structure_generation(state: Any) -> int:
    """The leg generation a run HOLDS, from its own metadata (1 when absent).

    Anything unreadable reads as generation 1 - the generation a run that never
    adjusted holds - because a fabricated high generation would refuse the next
    legitimate adjust, while a fabricated low one refuses it as a stale basis.
    Both refuse; neither guesses a structure this run does not hold.
    """
    metadata = _run_metadata(state)
    try:
        generation = int(metadata.get("structure_generation") or 1)
    except (TypeError, ValueError):
        return 1
    return generation if generation >= 1 else 1


def _structure_digest(state: Any, resolved: Mapping[str, Any]) -> str:
    """The SHAPE the run holds now: its own record first, its entry plan's second.

    A run that has been adjusted records the digest of the generation it holds,
    because an adjust that adds or removes a leg changes the shape while the plan
    that OPENED the run still names the old one. The duplicate gate compares this
    value against a new plan's frozen digest, so reading the originating plan's
    stale digest here would let an entry re-open the structure the run holds.
    """
    recorded = str(_run_metadata(state).get("structure_digest") or "")
    if recorded:
        return recorded
    return str((resolved or {}).get("structure_digest") or "")


def _json_list(value: Any) -> List[Any]:
    """A JSON list from a driver that may hand it back as text.

    PostgreSQL's ``jsonb`` arrives decoded; a text-shaped fixture (and any driver
    without a codec) arrives as the JSON document itself. Without this, a stored
    list would be read as a list of CHARACTERS, and "no outstanding leg" would
    quietly become "two legs outstanding".

    An unreadable value raises ``ValueError``: reading it as ``[]`` would turn
    "cannot tell" into "no outstanding leg", so the caller reports unknown
    coverage instead.
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    if isinstance(value, list):
        return list(value)
    raise ValueError(f"expected a JSON list, got {type(value).__name__}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


class OwnedWorkSnapshotService:
    """Read-only snapshot of one strategy's book plus its pending work."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        staleness_seconds: int = DEFAULT_POSITION_STALENESS_SECONDS,
    ) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        self._clock = clock or _utcnow
        self.staleness_seconds = max(1, int(staleness_seconds))

    # -- entry --------------------------------------------------------------

    def snapshot(
        self,
        *,
        strategy_id: str,
        account_id: str,
        execution_environment: str,
        strategy_run_id: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        moment = now or self._clock()
        strategy_id = str(strategy_id)
        account_id = str(account_id)
        environment = str(execution_environment)
        with self.session_factory() as session:
            publication = self._publication(
                session,
                account_id=account_id,
                strategy_id=strategy_id,
                environment=environment,
                moment=moment,
            )
            positions = (
                [
                    dict(row)
                    for row in self._positions(
                        session,
                        account_id=account_id,
                        strategy_id=strategy_id,
                        environment=environment,
                    )
                ]
                if publication["published"]
                else []
            )
            pending = self._pending(
                session,
                account_id=account_id,
                strategy_id=strategy_id,
                environment=environment,
            )
            option_runs, option_coverage = self._option_runs(
                session,
                account_id=account_id,
                strategy_id=strategy_id,
                environment=environment,
            )

        notes: List[str] = []
        if not publication["published"]:
            notes.append(
                "the strategy's position projection has not been published for this "
                "environment; coverage is unknown rather than flat"
            )
        elif not publication["fresh"]:
            notes.append(
                "the published projection's freshness cannot be proven for this "
                "environment; coverage is unknown rather than assumed current"
            )
        unknown_pending = [row for row in pending if row.get("coverage") == "unknown"]
        if unknown_pending:
            notes.append(
                f"{len(unknown_pending)} pending unit(s) have no quantity evidence; they "
                "are reported as unknown rather than as zero work"
            )
        if option_coverage["coverage"] == "unknown":
            notes.append(
                "this strategy's option runs could not be read completely for this "
                "account and environment; they are reported as unknown rather than "
                "as an empty set"
            )

        coverage_known = (
            bool(publication["published"])
            and bool(publication["fresh"])
            and not unknown_pending
            and option_coverage["coverage"] != "unknown"
        )
        return {
            "strategy_run_id": str(strategy_run_id),
            "strategy_id": strategy_id,
            "account_id": account_id,
            "execution_environment": environment,
            "projection": publication,
            "positions": positions,
            "pending": pending,
            "option_runs": option_runs,
            "option_runs_coverage": option_coverage,
            "coverage": "known" if coverage_known else "unknown",
            "observed_at": moment,
            "notes": notes,
        }

    # -- publication + positions -------------------------------------------

    def _publication(
        self,
        session: Any,
        *,
        account_id: str,
        strategy_id: str,
        environment: str,
        moment: datetime,
    ) -> Dict[str, Any]:
        state = session.execute(
            select(StrategyProjectionState).where(
                StrategyProjectionState.account_id == account_id,
                StrategyProjectionState.strategy_id == strategy_id,
                StrategyProjectionState.execution_environment == environment,
            )
        ).scalar_one_or_none()
        if state is None:
            return {
                "published": False,
                "coverage": "unpublished_unknown",
                "projection_version": 0,
                "content_sha256": None,
                "last_rebuild_at": None,
                "age_seconds": None,
                "fresh": False,
            }
        version = int(state.projection_version or 0)
        published = version >= 1 and state.content_sha256 is not None
        rebuilt = _as_utc(state.last_rebuild_at)
        age = None if rebuilt is None else max(0.0, (moment - rebuilt).total_seconds())
        fresh = bool(published and age is not None and age <= self.staleness_seconds)
        return {
            "published": published,
            "coverage": "published" if published else "unpublished_unknown",
            "projection_version": version,
            "content_sha256": state.content_sha256,
            "last_rebuild_at": state.last_rebuild_at,
            "age_seconds": age,
            "fresh": fresh,
        }

    @staticmethod
    def _positions(
        session: Any, *, account_id: str, strategy_id: str, environment: str
    ) -> List[Mapping[str, Any]]:
        rows = (
            session.execute(
                select(StrategyPositionProjection)
                .where(
                    StrategyPositionProjection.account_id == account_id,
                    StrategyPositionProjection.strategy_id == strategy_id,
                    StrategyPositionProjection.execution_environment == environment,
                    StrategyPositionProjection.net_quantity != 0,
                )
                .order_by(
                    StrategyPositionProjection.identity_kind,
                    StrategyPositionProjection.identity_key,
                    StrategyPositionProjection.product,
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "identity_kind": str(row.identity_kind),
                "identity_key": str(row.identity_key),
                "product": str(row.product),
                "instrument_token": int(row.instrument_token),
                "exchange": str(row.exchange),
                "tradingsymbol": str(row.tradingsymbol),
                "net_quantity": int(row.net_quantity),
                "unresolved_reason": row.unresolved_reason,
            }
            for row in rows
        ]

    # -- pending work -------------------------------------------------------

    def option_runs_for_scope(
        self,
        *,
        account_id: str,
        strategy_id: str,
        environment: str,
        session: Optional[Any] = None,
    ) -> tuple:
        """This strategy's OWN option runs for one (account, strategy, env).

        A thin public door onto the scope-derived read above, for callers that
        must decide something BEFORE acting rather than report a book - the
        plan/run binding edge asks it whether an equivalent structure is already
        open, and the continuation collector asks it whether the options lane
        still holds work. Both get the same answer, with the same coverage rule,
        because it is literally the same read.

        ``session`` lets a caller join an ALREADY-OPEN transaction (the entry
        admission path holds a lock while it asks); omitted, one is opened and
        closed here.
        """
        if session is not None:
            return self._option_runs(
                session,
                account_id=str(account_id),
                strategy_id=str(strategy_id),
                environment=str(environment),
            )
        with self.session_factory() as opened:
            return self._option_runs(
                opened,
                account_id=str(account_id),
                strategy_id=str(strategy_id),
                environment=str(environment),
            )

    def _bound_runs(
        self, session: Any, *, account_id: str, strategy_id: str, environment: str
    ) -> List[str]:
        """The authoritative run ids of this (account, strategy, environment)."""
        return [
            str(value)
            for value in session.execute(
                select(StrategyRunBinding.strategy_run_id).where(
                    StrategyRunBinding.account_id == account_id,
                    StrategyRunBinding.strategy_id == strategy_id,
                    StrategyRunBinding.execution_environment == environment,
                )
            )
            .scalars()
            .all()
        ]

    def _plans(
        self, session: Any, *, account_id: str, strategy_id: str, run_ids: List[str]
    ) -> List[StrategyPlan]:
        if not run_ids:
            return []
        proposal_ids = [
            str(value)
            for value in session.execute(
                select(StrategyProposal.proposal_id).where(
                    StrategyProposal.strategy_id == strategy_id,
                    StrategyProposal.account_id == account_id,
                    StrategyProposal.strategy_run_id.in_(run_ids),
                )
            )
            .scalars()
            .all()
        ]
        if not proposal_ids:
            return []
        return list(
            session.execute(
                select(StrategyPlan).where(StrategyPlan.proposal_id.in_(proposal_ids))
            )
            .scalars()
            .all()
        )

    def _pending(
        self, session: Any, *, account_id: str, strategy_id: str, environment: str
    ) -> List[Dict[str, Any]]:
        run_ids = self._bound_runs(
            session, account_id=account_id, strategy_id=strategy_id, environment=environment
        )
        plans = self._plans(
            session, account_id=account_id, strategy_id=strategy_id, run_ids=run_ids
        )
        plan_by_id = {str(plan.plan_id): plan for plan in plans}

        merged: Dict[Any, Dict[str, Any]] = {}
        self._merge_plan_trail(
            session, plans=plans, environment=environment, merged=merged
        )
        self._merge_live_submissions(
            session,
            strategy_id=strategy_id,
            account_id=account_id,
            environment=environment,
            merged=merged,
        )
        # Attribution last: it must see whatever state the sources produced.
        for key, row in merged.items():
            plan = plan_by_id.get(str(row.get("plan_id") or ""))
            if plan is not None:
                row.update(self._attribution(session, plan=plan, step_no=int(key[1])))
            row.setdefault("instrument_id", None)
            row.setdefault("exchange", None)
            row.setdefault("tradingsymbol", None)
            row.setdefault("product", None)
            row.setdefault("side", None)

        pending = sorted(
            merged.values(),
            key=lambda row: (str(row.get("plan_id") or ""), int(row.get("step_no") or 0)),
        )
        pending.extend(
            self._pending_requests(
                session,
                strategy_id=strategy_id,
                account_id=account_id,
                environment=environment,
            )
        )
        return pending

    def _option_runs(
        self, session: Any, *, account_id: str, strategy_id: str, environment: str
    ) -> tuple:
        """This strategy's option runs, discovered from its OWN bound attempts.

        The scope is derived, never supplied: the authoritative run ids of this
        ``(strategy, account, environment)`` pair bound the plan set, the plans
        bound the ``strategy_plan_option_runs`` edges, and those edges bind the
        durable option runs. A caller cannot name a canonical strategy, another
        account or another environment to widen the read.

        A mismatch in the persisted edge (a different strategy/account/env) is
        reported as UNKNOWN coverage rather than silently dropped, and a read
        failure never reports an empty set as if it were complete.
        """
        unknown = {"coverage": "unknown", "count": 0, "truncated": False, "reason": ""}
        try:
            run_ids = self._bound_runs(
                session,
                account_id=account_id,
                strategy_id=strategy_id,
                environment=environment,
            )
            plans = self._plans(
                session,
                account_id=account_id,
                strategy_id=strategy_id,
                run_ids=run_ids,
            )
            if not plans:
                return [], {**unknown, "coverage": "known", "reason": ""}
            plan_by_id = {str(plan.plan_id): plan for plan in plans}
            plan_ids = list(plan_by_id)
            edges = (
                session.execute(
                    select(StrategyPlanOptionRun)
                    .where(StrategyPlanOptionRun.plan_id.in_(plan_ids))
                    .order_by(StrategyPlanOptionRun.plan_id)
                )
                .scalars()
                .all()
            )
        except Exception as exc:  # noqa: BLE001 - a read failure is unknown coverage
            return [], {
                **unknown,
                # The client-facing reason names the failure, not the driver's
                # message: a raw exception here would leak SQL text and bound
                # parameters into a strategy's own log.
                "reason": "option_run_read_failed",
            }

        # Every edge must belong to the requested scope. A row that names another
        # strategy, account or environment is a data mismatch, not evidence.
        mismatch = False
        by_option_run: Dict[str, Dict[str, Any]] = {}
        for edge in edges:
            plan_id = str(edge.plan_id)
            if (
                str(edge.strategy_id) != str(strategy_id)
                or str(edge.account_id) != str(account_id)
                or str(edge.execution_environment) != str(environment)
            ):
                mismatch = True
                continue
            option_run_id = str(edge.option_run_id)
            phase = str(edge.phase)
            # The same run may be reached from more than one plan (an entry and
            # its exit); the run itself is reported once.
            if option_run_id in by_option_run:
                row = by_option_run[option_run_id]
                row.setdefault("plan_ids", []).append(plan_id)
                # The run's ORIGIN is the plan that opened it, never whichever
                # plan id happens to sort first: a close plan written earlier
                # lexically must not become the run's originating edge.
                if phase == "entry" and row.get("originating_phase") != "entry":
                    row["originating_plan_id"] = plan_id
                    row["originating_phase"] = phase
                continue
            by_option_run[option_run_id] = {
                "option_run_id": option_run_id,
                "plan_ids": [plan_id],
                "originating_plan_id": plan_id,
                "originating_phase": phase,
                "phase": phase,
                "worker_run_id": None if edge.worker_run_id is None else str(edge.worker_run_id),
            }

        if not by_option_run:
            coverage = {**unknown, "coverage": "known", "reason": ""}
            if mismatch:
                coverage = {**unknown, "reason": "option_run_scope_mismatch"}
            return [], coverage

        option_run_ids = sorted(by_option_run)
        truncated = len(option_run_ids) > OPTION_RUN_LIMIT
        option_run_ids = option_run_ids[:OPTION_RUN_LIMIT]
        try:
            placeholders = ", ".join(f":id{index}" for index in range(len(option_run_ids)))
            params = {f"id{index}": value for index, value in enumerate(option_run_ids)}
            state_rows = (
                session.execute(
                    text(
                        f"""
                        SELECT strategy_run_id, strategy_name, product, status,
                               legs, completed_legs, failed_legs, pending_legs,
                               orders, trades, metadata, updated_at
                        FROM public.option_run_states
                        WHERE strategy_run_id IN ({placeholders})
                        """
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        except Exception:  # noqa: BLE001
            return [], {**unknown, "reason": "option_run_state_read_failed"}
        # ``jsonb`` is decoded by the driver on PostgreSQL and arrives as text
        # elsewhere; normalise the list-shaped columns so "no outstanding leg"
        # cannot be read as a list of characters.
        try:
            state_by_id = {
                str(row["strategy_run_id"]): {
                    **dict(row),
                    "legs": _json_list(dict(row).get("legs")),
                    "completed_legs": _json_list(dict(row).get("completed_legs")),
                    "pending_legs": _json_list(dict(row).get("pending_legs")),
                    "failed_legs": _json_list(dict(row).get("failed_legs")),
                }
                for row in state_rows
            }
        except ValueError:
            return [], {**unknown, "reason": "option_run_state_unreadable"}

        rows: List[Dict[str, Any]] = []
        missing_state = False
        identity_mismatch = False
        for option_run_id in option_run_ids:
            edge = by_option_run[option_run_id]
            plan = plan_by_id.get(edge["originating_plan_id"])
            resolved = dict(getattr(plan, "resolved_plan", None) or {}) if plan is not None else {}
            state = state_by_id.get(option_run_id)
            if state is None:
                # The binding exists but its durable run is unreadable: that is
                # unknown, not "the run is finished".
                missing_state = True
            state_legs = list((state or {}).get("legs") or [])
            frozen_legs = list(resolved.get("legs") or [])
            if state is not None and _run_legs_contradict_binding(state_legs, frozen_legs):
                # The run row is reachable through this strategy's binding but its
                # legs are not the frozen legs of that edge. Trusting either side
                # would attribute a foreign structure to this strategy, so the
                # whole read is unknown rather than silently mis-scoped.
                identity_mismatch = True
            rows.append(
                {
                    "option_run_id": option_run_id,
                    "plan_ids": list(edge["plan_ids"]),
                    "originating_plan_id": edge["originating_plan_id"],
                    "originating_phase": edge.get("originating_phase") or edge["phase"],
                    "phase": edge["phase"],
                    "worker_run_id": edge["worker_run_id"],
                    "underlying": str(resolved.get("underlying") or ""),
                    "expiry": str(resolved.get("expiry") or ""),
                    "structure_id": str(resolved.get("structure_id") or ""),
                    # The structure identity the run HOLDS NOW: the shape this run
                    # records once an adjust has rewritten its legs, and otherwise
                    # the frozen identity of the edge that opened it. It is what
                    # makes "is this the same structure?" a comparison of
                    # identities rather than of list positions.
                    "structure_digest": _structure_digest(state, resolved),
                    # The leg generation the run HOLDS now (1 until an adjust
                    # lands). An adjust freezes the generation it observed as its
                    # basis, so this is what makes a stale basis detectable.
                    "structure_generation": _structure_generation(state),
                    "expiry_policy": str(resolved.get("expiry_policy") or ""),
                    "product": str(
                        (state or {}).get("product") or resolved.get("product") or ""
                    ),
                    "status": str((state or {}).get("status") or "unknown"),
                    "legs": state_legs or frozen_legs,
                    "completed_legs": list((state or {}).get("completed_legs") or []),
                    "pending_legs": list((state or {}).get("pending_legs") or []),
                    "failed_legs": list((state or {}).get("failed_legs") or []),
                    # An unresolved protective stage is in-flight work of the
                    # run itself: the platform committed a stage and does not
                    # know whether the broker took it.
                    "protective_exit_unresolved": _protective_exit_unresolved(
                        (state or {}).get("orders") or []
                    ),
                    "coverage": "unknown" if state is None else "known",
                }
            )

        # A TRUNCATED read is not a complete one: the hidden runs may include the
        # only open position, so coverage stays unknown and the caller must refuse
        # to guess rather than treat the returned set as everything it owns.
        incomplete = missing_state or mismatch or identity_mismatch or truncated
        coverage = {
            "coverage": "unknown" if incomplete else "known",
            "count": len(rows),
            "truncated": truncated,
            "reason": (
                "option_run_state_unavailable"
                if missing_state
                else "option_run_identity_mismatch"
                if identity_mismatch
                else "option_run_scope_mismatch"
                if mismatch
                else "option_run_limit_truncated"
                if truncated
                else ""
            ),
        }
        return rows, coverage

    def _merge_plan_trail(
        self,
        session: Any,
        *,
        plans: List[StrategyPlan],
        environment: str,
        merged: Dict[Any, Dict[str, Any]],
    ) -> None:
        """One row per step whose latest trail event is not terminal."""
        if not plans:
            return
        plan_ids = [str(plan.plan_id) for plan in plans]
        rows = (
            session.execute(
                select(StrategyPlanExecutionEvent)
                .where(StrategyPlanExecutionEvent.plan_id.in_(plan_ids))
                .order_by(
                    StrategyPlanExecutionEvent.created_at,
                    StrategyPlanExecutionEvent.id,
                )
            )
            .scalars()
            .all()
        )
        latest: Dict[Any, Any] = {}
        submitted: Dict[Any, Dict[str, Any]] = {}
        ordered = sorted(
            rows,
            key=lambda row: (
                _as_utc(row.created_at) or datetime.min.replace(tzinfo=timezone.utc),
                EVENT_RANK.get(str(row.event), 99),
                str(row.id),
            ),
        )
        for row in ordered:
            key = (str(row.plan_id), int(row.step_no))
            if str(row.event) == "submitted":
                submitted[key] = dict(row.detail or {})
            if key in latest and str(row.event) in TERMINAL_EVENT_STATES:
                latest[key] = row
                continue
            if key in latest and str(latest[key].event) in TERMINAL_EVENT_STATES:
                continue
            latest[key] = row

        for key, row in latest.items():
            if str(row.event) not in PENDING_EVENT_STATES:
                continue
            requested = _as_float((submitted.get(key) or {}).get("quantity"))
            if requested is None:
                requested = _as_float((submitted.get(key) or {}).get("signed_quantity"))
            filled = _as_float(row.filled_quantity)
            remaining = None
            if str(row.event) == "partially_filled":
                remaining = _as_float((row.detail or {}).get("pending_quantity"))
            else:
                remaining = abs(requested) if requested is not None else None
            merged[key] = {
                "source": "plan_execution",
                "sources": ["plan_execution"],
                "plan_id": key[0],
                "step_no": key[1],
                "state": str(row.event),
                "execution_environment": str(environment),
                "submitted_quantity": abs(requested) if requested is not None else None,
                "filled_quantity": filled,
                "remaining_quantity": remaining,
                "coverage": "known" if remaining is not None else "unknown",
                "reason": row.refusal_reason,
                "confidence": {},
                "detail": {
                    "paper_order_id": row.paper_order_id,
                    "broker_order_id": row.broker_order_id,
                    "trail_event": str(row.event),
                },
            }

    def _merge_live_submissions(
        self,
        session: Any,
        *,
        strategy_id: str,
        account_id: str,
        environment: str,
        merged: Dict[Any, Dict[str, Any]],
    ) -> None:
        """A live claim is authoritative for its step; the trail is merged in."""
        rows = (
            session.execute(
                select(LivePlanSubmission)
                .where(
                    LivePlanSubmission.strategy_id == strategy_id,
                    LivePlanSubmission.account_id == account_id,
                    LivePlanSubmission.execution_environment == environment,
                    LivePlanSubmission.state.not_in(list(TERMINAL_SUBMISSION_STATES)),
                )
                .order_by(LivePlanSubmission.plan_id, LivePlanSubmission.step_no)
            )
            .scalars()
            .all()
        )
        for row in rows:
            key = (str(row.plan_id), int(row.step_no))
            delta = dict(row.delta_snapshot or {})
            detail = dict(row.detail or {})
            requested = _as_float(delta.get("quantity"))
            if requested is None:
                requested = _as_float(delta.get("signed_quantity"))
            filled = _as_float(detail.get("filled_quantity"))
            if filled is None:
                filled = _as_float(detail.get("filled"))
            remaining = _as_float(detail.get("remaining_quantity"))
            if remaining is None and str(row.state) == "withheld" and requested is not None:
                remaining = abs(requested)
            if remaining is None and requested is not None and filled is not None:
                remaining = max(0.0, abs(requested) - abs(filled))

            prior = merged.get(key)
            confidence: Dict[str, Any] = {"live_claim_state": str(row.state)}
            if prior is not None:
                confidence["plan_trail_state"] = str(prior.get("state") or "")
                if (
                    prior.get("coverage") == "known"
                    and prior.get("remaining_quantity") is not None
                    and remaining is not None
                    and float(prior["remaining_quantity"]) != float(remaining)
                    and str(row.state) not in ("withheld", "releasing")
                ):
                    # The two durable records disagree about the outstanding
                    # quantity: report it as unknown rather than pick a winner.
                    confidence["disagreement"] = {
                        "plan_trail_remaining": prior.get("remaining_quantity"),
                        "live_claim_remaining": remaining,
                    }
                    remaining = None
                    filled = None
            merged[key] = {
                "source": "live_submission",
                "sources": sorted({"live_submission", *(prior or {}).get("sources", [])}),
                "plan_id": key[0],
                "step_no": key[1],
                "state": str(row.state),
                "execution_environment": str(row.execution_environment),
                "submitted_quantity": abs(requested) if requested is not None else None,
                "filled_quantity": filled,
                "remaining_quantity": remaining,
                "coverage": "known" if remaining is not None else "unknown",
                "reason": None,
                "confidence": confidence,
                "detail": {
                    **(prior or {}).get("detail", {}),
                    "broker_order_ids": list(row.broker_order_ids or []),
                    "step_ref": str(row.step_ref),
                    "delta_snapshot": delta,
                },
            }

    def _attribution(
        self, session: Any, *, plan: StrategyPlan, step_no: int
    ) -> Dict[str, Any]:
        """The step's own instrument/product/side, from the frozen plan."""
        spec = self._live_step_spec(session, plan_id=str(plan.plan_id)).get(int(step_no))
        if spec:
            return {
                "instrument_id": str(spec.get("instrument_id") or "") or None,
                "exchange": str(spec.get("exchange") or "") or None,
                "tradingsymbol": str(spec.get("tradingsymbol") or "") or None,
                "product": str(spec.get("product") or "") or None,
                "side": str(spec.get("side") or "") or None,
                "lane": str(spec.get("lane") or "") or None,
                "depends_on": [int(value) for value in (spec.get("depends_on") or [])],
                "release_rule": str(spec.get("release_rule") or "") or None,
                "attribution_source": "live_step_spec",
            }
        legs = list((plan.resolved_plan or {}).get("legs") or [])
        index = int(step_no) - 1
        if 0 <= index < len(legs):
            leg = dict(legs[index])
            signed = _as_float(leg.get("signed_quantity"))
            return {
                "instrument_id": str(leg.get("instrument_id") or "") or None,
                "exchange": str(leg.get("exchange") or "") or None,
                "tradingsymbol": str(leg.get("tradingsymbol") or "") or None,
                "product": str(leg.get("product") or "") or None,
                "side": self._leg_side(leg, signed),
                "lane": None,
                "depends_on": [],
                "release_rule": None,
                "attribution_source": "plan_leg",
            }
        return {"attribution_source": "unavailable"}

    @staticmethod
    def _leg_side(leg: Mapping[str, Any], signed: Optional[float]) -> Optional[str]:
        """The leg's own direction, from the signed quantity or the leg's field."""
        if signed is not None:
            return "BUY" if signed >= 0 else "SELL"
        declared = (
            str(leg.get("transaction_type") or leg.get("side") or leg.get("direction") or "")
            .strip()
            .upper()
        )
        return declared if declared in ("BUY", "SELL") else None

    @staticmethod
    def _live_step_spec(session: Any, *, plan_id: str) -> Dict[int, Dict[str, Any]]:
        """The frozen step specs of a live parent, when the parent exists.

        ``live_plan_executions`` has no ORM model (it is migration-owned), so it
        is read directly. A database without the table (a paper-only harness)
        simply has no live specs, and the plan's own legs are used instead.
        """
        import json

        from sqlalchemy import text

        from backend.strategies.live_sequence import StepSpec

        try:
            row = session.execute(
                text(
                    "SELECT step_spec FROM public.live_plan_executions "
                    "WHERE plan_id = :plan_id"
                ),
                {"plan_id": str(plan_id)},
            ).first()
        except Exception:  # noqa: BLE001 - a missing parent table is not an error
            return {}
        if row is None:
            return {}
        raw = row[0]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "[]")
            except ValueError:
                return {}
        specs = raw if isinstance(raw, list) else []
        out: Dict[int, Dict[str, Any]] = {}
        for item in specs:
            if not isinstance(item, dict):
                continue
            spec = StepSpec.from_dict(item)
            out[int(spec.step_no)] = spec.as_dict()
        return out

    def _pending_requests(
        self, session: Any, *, strategy_id: str, account_id: str, environment: str
    ) -> List[Dict[str, Any]]:
        rows = (
            session.execute(
                select(HostedExecutionRequest)
                .where(
                    HostedExecutionRequest.strategy_id == strategy_id,
                    HostedExecutionRequest.account_id == account_id,
                    HostedExecutionRequest.execution_environment == environment,
                    HostedExecutionRequest.status.in_(list(PENDING_REQUEST_STATUSES)),
                )
                .order_by(HostedExecutionRequest.created_at)
            )
            .scalars()
            .all()
        )
        return [
            {
                "source": "execution_request",
                "sources": ["execution_request"],
                "plan_id": str(row.plan_id),
                "step_no": 0,
                "state": str(row.status),
                "execution_environment": str(row.execution_environment),
                "submitted_quantity": None,
                "filled_quantity": None,
                "remaining_quantity": None,
                "coverage": "unknown",
                "reason": row.refusal_code,
                "confidence": {},
                "detail": {
                    "request_id": str(row.request_id),
                    "authorization_mode": str(row.authorization_mode),
                    "decision_kind": row.decision_kind,
                },
            }
            for row in rows
        ]
