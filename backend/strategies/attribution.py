"""Durable strategy attribution: facts, resolution, fold, store and service.

Why this module exists
----------------------

One canonical, owner-controlled strategy identity must own every fill, and the
broker account's net position must never become strategy ownership: the broker
nets per ``(instrument, product)`` and knows nothing about which strategy opened
a position. This module materialises the platform's truth — a rebuildable
strategy position book per execution environment — from immutable fill facts
plus their durable attribution.

Three properties are structural, not conventional:

* **Books are separate.** ``execution_environment ∈ {live, paper, dry_run}`` is
  carried on every fact and every position key, so a paper fill can never alter
  a live projection and a simulated offset can never hide live exposure.
* **Canonical identity is resolved per fact, before folding.** A fill is mapped
  to an instrument using the mapping interval valid at *that fill's* effective
  time, never today's ``is_current`` mapping. A token re-mapped to a different
  instrument across generations therefore produces two positions rather than a
  false flat.
* **The projection is a full recompute**, inside one transaction whose advisory
  lock is taken *before* the fact snapshot, so an older snapshot can never
  overwrite a newer rebuild. ``content_sha256`` exists only for idempotence and
  is never chronology evidence.

Unresolved facts are never guessed at: they remain explicit ``identity_kind="raw"``
rows carrying a catalog-evidence era, so facts from the same era net across dates
while facts from distinct known eras never merge. Unresolved exposure is surfaced
as ``UNRESOLVED_INSTRUMENT_IDENTITY`` for later admission/reconciliation to
consume; this module does not build a freeze service.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.shared.serialization import _json_dumps, _row_mapping
from backend.strategies.attribution_models import (
    Strategy,
    StrategyAttributionAdjustment,
    StrategyAttributionAdjustmentLine,
    StrategyPositionProjection,
    StrategyProjectionState,
    StrategyRunBinding,
    WorkerTokenStrategyGrant,
)

#: The immutable book dimension. Equal to the run's persisted execution mode.
EXECUTION_ENVIRONMENTS = ("live", "paper", "dry_run")

#: Named condition for exposure that could not be mapped to a canonical instrument.
UNRESOLVED_INSTRUMENT_IDENTITY = "UNRESOLVED_INSTRUMENT_IDENTITY"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value: Any) -> datetime:
    """Parse a DB timestamp into an aware datetime (SQLite returns strings)."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if value is None:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class TradeFact:
    """One immutable fill fact, before canonical identity resolution.

    ``source_key`` is the durable fill identity (``trade:<trade_id>`` for live,
    the paper trade key for paper) and is the deduplication key: re-ingesting
    the same fill is idempotent. ``execution_environment`` comes from the run's
    binding, never from the fact's own payload, so a fill can never drift into
    another book.
    """

    source_key: str
    strategy_run_id: str
    execution_environment: str
    instrument_token: int
    exchange: str
    tradingsymbol: str
    product: str
    signed_quantity: int
    effective_at: datetime
    pinned_generation: Optional[str] = None


class PositionKey(NamedTuple):
    """The resolved position identity: one book line.

    ``identity_kind`` is ``canonical`` (``identity_key`` is the canonical
    instrument UUID) or ``raw`` (``identity_key`` is a catalog-evidence
    era-qualified raw identity). Raw and canonical identities never merge, and
    neither do distinct eras or distinct environments.
    """

    execution_environment: str
    identity_kind: str
    identity_key: str
    product: str


class AttributionFold:
    """Pure, deterministic aggregation of resolved facts into a position book."""

    @staticmethod
    def fold(facts: Iterable[Tuple[TradeFact, PositionKey]]) -> Dict[PositionKey, int]:
        """Sum signed quantities per resolved position key.

        Deduplicates by ``source_key`` (the fact sources already deduplicate at
        SQL level; this is belt-and-braces determinism), drops flat positions,
        and never consults broker aggregates. The result is ordered by position
        key so identical fact *sets* produce byte-identical content regardless
        of ingestion order — that ordering is what makes the published
        ``content_sha256`` meaningful for idempotence.
        """
        ordered = sorted(facts, key=lambda pair: (pair[0].source_key, pair[1]))
        totals: Dict[PositionKey, int] = {}
        seen: Set[str] = set()
        for fact, key in ordered:
            if fact.source_key in seen:
                continue
            seen.add(fact.source_key)
            if key.execution_environment != fact.execution_environment:
                raise ValueError(
                    f"position key environment {key.execution_environment!r} disagrees with "
                    f"fact environment {fact.execution_environment!r} for {fact.source_key}"
                )
            totals[key] = totals.get(key, 0) + int(fact.signed_quantity)
        return {key: quantity for key, quantity in sorted(totals.items()) if quantity != 0}


class RunBindingFailed(RuntimeError):
    """A run was inserted but its mandatory binding could not be written.

    Raised only from :meth:`SqlAttributionStore.create_run_with_binding`. The run
    insert happens first, so any failure after it is a binding failure, and the
    caller's transaction is rolled back — neither row survives.
    """


@dataclass(frozen=True)
class RunBindingInput:
    """The trusted run-to-strategy binding descriptor.

    Derived server-side — from the persisted strategy job for hosted runs, from
    the token's persisted grant for external runs — never from a run-create
    payload, which is untrusted input. ``execution_environment`` is the run's own
    server-validated execution mode; the database composite FKs make an
    owner/account/environment mismatch impossible even if a caller lies.
    """

    strategy_id: str
    owner_id: str
    account_id: str
    execution_environment: str
    bound_by: str
    binding_source: str


class SqlAttributionStore:
    """Durable binding + fact-source + publication store.

    New-table writes go through the ORM on the shared ``Base`` (portable to the
    SQLite test database); reads of platform fact tables use the codebase's
    ``public.``-qualified Core SQL. All reads a recompute performs happen on the
    session ``recompute_publish`` opened, strictly after its advisory lock.
    """

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory

    # ------------------------------------------------------------------ binding

    def bind_run(
        self,
        *,
        strategy_run_id: str,
        strategy_id: str,
        owner_id: str,
        account_id: str,
        execution_environment: str,
        bound_by: str,
        binding_source: str,
        db: Optional[Any] = None,
    ) -> None:
        """INSERT one immutable binding.

        There is no update path: a second binding for the same run collides on
        the primary key, and a PostgreSQL trigger refuses UPDATE/DELETE outright.
        Account/owner/environment disagreement is refused by the composite FKs,
        not by this method.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            session.add(
                StrategyRunBinding(
                    strategy_run_id=strategy_run_id,
                    strategy_id=strategy_id,
                    owner_id=owner_id,
                    account_id=account_id,
                    execution_environment=execution_environment,
                    bound_by=bound_by,
                    binding_source=binding_source,
                )
            )
            session.flush()
            if owns_db:
                session.commit()
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    def bound_run_ids(
        self,
        *,
        account_id: str,
        strategy_id: str,
        execution_environment: str,
        db: Optional[Any] = None,
    ) -> Set[str]:
        """Run ids bound to this strategy **in this environment only**."""
        owns_db = db is None
        session = db or self.session_factory()
        try:
            rows = session.execute(
                select(StrategyRunBinding.strategy_run_id).where(
                    StrategyRunBinding.account_id == account_id,
                    StrategyRunBinding.strategy_id == strategy_id,
                    StrategyRunBinding.execution_environment == execution_environment,
                )
            ).scalars().all()
            return {str(row) for row in rows}
        finally:
            if owns_db:
                session.close()

    # ------------------------------------------------------------------ grants

    def active_grants(
        self, *, token_id: str, account_id: str, db: Optional[Any] = None
    ) -> List[Dict[str, str]]:
        """Active token→strategy grants for one account.

        A grant authorizes a token for a canonical strategy only while it is
        unrevoked **and** the strategy's canonical account matches the requested
        account exactly. A grant for another account is treated as absent — the
        account check is a join condition, not a caller convention.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            rows = session.execute(
                select(
                    Strategy.id,
                    Strategy.owner_id,
                    Strategy.account_scope,
                )
                .join(WorkerTokenStrategyGrant, WorkerTokenStrategyGrant.strategy_id == Strategy.id)
                .where(
                    WorkerTokenStrategyGrant.token_id == token_id,
                    WorkerTokenStrategyGrant.revoked_at.is_(None),
                    Strategy.account_scope == account_id,
                )
                .order_by(Strategy.id)
            ).all()
            return [
                {
                    "strategy_id": str(row[0]),
                    "owner_id": str(row[1]),
                    "account_scope": str(row[2]),
                }
                for row in rows
            ]
        finally:
            if owns_db:
                session.close()

    def grant_strategy(
        self,
        *,
        token_id: str,
        strategy_id: str,
        granted_by: str,
        db: Optional[Any] = None,
    ) -> None:
        """Issue (or re-issue) a token→strategy grant.

        Re-issuing clears a previous revocation; the row is never deleted, so
        the grant history survives revocation.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            existing = session.execute(
                select(WorkerTokenStrategyGrant).where(
                    WorkerTokenStrategyGrant.token_id == token_id,
                    WorkerTokenStrategyGrant.strategy_id == strategy_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    WorkerTokenStrategyGrant(
                        token_id=token_id,
                        strategy_id=strategy_id,
                        granted_by=granted_by,
                    )
                )
            else:
                existing.revoked_at = None
                existing.granted_by = granted_by
                existing.granted_at = _utcnow()
            session.flush()
            if owns_db:
                session.commit()
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    def revoke_grant(
        self, *, token_id: str, strategy_id: str, db: Optional[Any] = None
    ) -> bool:
        """Revoke a grant by stamping ``revoked_at``. History is never deleted."""
        owns_db = db is None
        session = db or self.session_factory()
        try:
            grant = session.execute(
                select(WorkerTokenStrategyGrant).where(
                    WorkerTokenStrategyGrant.token_id == token_id,
                    WorkerTokenStrategyGrant.strategy_id == strategy_id,
                )
            ).scalar_one_or_none()
            if grant is None:
                return False
            grant.revoked_at = _utcnow()
            session.flush()
            if owns_db:
                session.commit()
            return True
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    # ------------------------------------------------------- adjustments (G4+)

    def create_reclassification(
        self,
        *,
        account_id: str,
        strategy_id: str,
        owner_id: str,
        reason_code: str,
        created_by: str,
        lines: Sequence[Dict[str, Any]],
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Insert one append-only reclassification adjustment and its lines.

        Header and lines commit in ONE transaction. There is no update or delete
        path: a correction is a new opposite-sign adjustment referencing the
        original in ``evidence``. ``quantity_delta`` is the signed quantity
        credited to the strategy, so the manual residual moves the opposite way
        by construction — the manual book is the implicit counterparty.

        An omitted ``effective_at`` defaults to the adjustment's creation time,
        never the original fill's timestamp: the fold order is "when the owner
        decided", not "when the human traded".
        """
        adjustment_id = str(uuid.uuid4())
        created_at = _utcnow()
        session = self.session_factory()
        try:
            session.add(
                StrategyAttributionAdjustment(
                    adjustment_id=adjustment_id,
                    account_id=account_id,
                    adjustment_kind="owner_reclassification",
                    reason_code=reason_code,
                    created_by=created_by,
                    evidence=dict(evidence or {}),
                )
            )
            stored_lines = []
            for index, line in enumerate(lines, start=1):
                effective_at = line.get("effective_at") or created_at
                session.add(
                    StrategyAttributionAdjustmentLine(
                        adjustment_id=adjustment_id,
                        line_no=index,
                        strategy_id=strategy_id,
                        owner_id=owner_id,
                        account_id=account_id,
                        instrument_token=int(line["instrument_token"]),
                        exchange=str(line["exchange"]),
                        tradingsymbol=str(line["tradingsymbol"]),
                        product=str(line["product"]),
                        quantity_delta=int(line["quantity_delta"]),
                        effective_at=effective_at,
                    )
                )
                stored_lines.append(
                    {
                        "line_no": index,
                        "instrument_token": int(line["instrument_token"]),
                        "exchange": str(line["exchange"]),
                        "tradingsymbol": str(line["tradingsymbol"]),
                        "product": str(line["product"]),
                        "quantity_delta": int(line["quantity_delta"]),
                        "effective_at": effective_at,
                    }
                )
            session.commit()
            return {
                "adjustment_id": adjustment_id,
                "strategy_id": strategy_id,
                "account_id": account_id,
                "adjustment_kind": "owner_reclassification",
                "reason_code": reason_code,
                "created_by": created_by,
                "evidence": dict(evidence or {}),
                "created_at": created_at,
                "lines": stored_lines,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def iter_adjustment_facts(
        self, *, account_id: str, strategy_id: str, db: Optional[Any] = None
    ) -> List[TradeFact]:
        """Adjustment lines as fold facts for one strategy's **live** book.

        Stable source identity ``adjustment:<adjustment_id>:<line_no>`` is what
        makes double application across rebuilds structurally impossible: every
        full recompute folds each line exactly once. Lines are live-book facts in
        V1, which is why the environment is assigned rather than stored.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            rows = session.execute(
                select(StrategyAttributionAdjustmentLine)
                .where(
                    StrategyAttributionAdjustmentLine.account_id == account_id,
                    StrategyAttributionAdjustmentLine.strategy_id == strategy_id,
                )
                .order_by(
                    StrategyAttributionAdjustmentLine.adjustment_id,
                    StrategyAttributionAdjustmentLine.line_no,
                )
            ).scalars().all()
        except SQLAlchemyError:
            return []
        finally:
            if owns_db:
                session.close()
        return [
            TradeFact(
                source_key=f"adjustment:{row.adjustment_id}:{row.line_no}",
                strategy_run_id=f"adjustment:{row.adjustment_id}",
                execution_environment="live",
                instrument_token=int(row.instrument_token),
                exchange=str(row.exchange),
                tradingsymbol=str(row.tradingsymbol),
                product=str(row.product),
                signed_quantity=int(row.quantity_delta),
                effective_at=row.effective_at,
                pinned_generation=None,
            )
            for row in rows
        ]

    # ------------------------------------------------------- strategy closure

    def run_binding(self, *, strategy_run_id: str, db: Optional[Any] = None) -> Optional[Dict[str, str]]:
        """The run's trusted binding, or ``None`` when the run is unbound.

        ``None`` is the legacy signal: an unbound run keeps run-scoped behavior
        and is never silently re-scoped to a strategy.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            row = session.execute(
                select(StrategyRunBinding).where(
                    StrategyRunBinding.strategy_run_id == strategy_run_id
                )
            ).scalar_one_or_none()
        except SQLAlchemyError:
            # A database predating the binding table behaves as unbound.
            return None
        finally:
            if owns_db:
                session.close()
        if row is None:
            return None
        return {
            "strategy_id": str(row.strategy_id),
            "owner_id": str(row.owner_id),
            "account_id": str(row.account_id),
            "execution_environment": str(row.execution_environment),
        }

    def open_positions_for_run(self, *, strategy_run_id: str, db: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        """The run's **strategy book**, or ``None`` when the run is unbound.

        This is the single source flatness, exposure display and exit sizing read
        for a strategy-bound run (R3 §10, D-5): account flatness never substitutes
        for strategy flatness, so a strategy is flat even while the account still
        holds another strategy's quantity on the same line.

        ``broker_net_quantity`` is carried per leg for the one-sided exit guard
        only — never as a flatness gate here. The read is lock-free: the
        projection is only ever mutated by ``recompute_publish``.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            binding = self.run_binding(strategy_run_id=strategy_run_id, db=session)
            if binding is None:
                return None
            rows = session.execute(
                select(StrategyPositionProjection)
                .where(
                    StrategyPositionProjection.account_id == binding["account_id"],
                    StrategyPositionProjection.strategy_id == binding["strategy_id"],
                    StrategyPositionProjection.execution_environment
                    == binding["execution_environment"],
                    StrategyPositionProjection.net_quantity != 0,
                )
                .order_by(
                    StrategyPositionProjection.identity_kind,
                    StrategyPositionProjection.identity_key,
                    StrategyPositionProjection.product,
                )
            ).scalars().all()
            broker_nets = self._broker_nets_by_coordinate(session, account_id=binding["account_id"])
            legs = [
                {
                    "journal_run_id": None,
                    "account_id": binding["account_id"],
                    "instrument_token": int(row.instrument_token),
                    "exchange": str(row.exchange),
                    "tradingsymbol": str(row.tradingsymbol),
                    "product": str(row.product),
                    "net_quantity": int(row.net_quantity),
                    "identity_kind": str(row.identity_kind),
                    "identity_key": str(row.identity_key),
                    "broker_net_quantity": broker_nets.get(
                        (int(row.instrument_token), str(row.product))
                    ),
                }
                for row in rows
            ]
            return {
                "strategy_id": binding["strategy_id"],
                "account_id": binding["account_id"],
                "execution_environment": binding["execution_environment"],
                "legs": legs,
            }
        finally:
            if owns_db:
                session.close()

    @staticmethod
    def _broker_nets_by_coordinate(session: Any, *, account_id: str) -> Dict[Tuple[int, str], int]:
        """Broker net per ``(instrument_token, product)`` for the exit guard."""
        try:
            rows = session.execute(
                text(
                    """
                    SELECT instrument_token, product, net_quantity
                    FROM public.account_positions
                    WHERE account_id = :account_id
                    """
                ),
                {"account_id": account_id},
            ).fetchall()
        except SQLAlchemyError:
            # Broker positions are the secondary constraint; their absence must
            # not block a strategy-book read.
            return {}
        return {(int(row[0]), str(row[1])): int(row[2] or 0) for row in rows}

    def canonical_strategy(
        self, *, strategy_id: str, owner_id: Optional[str] = None, db: Optional[Any] = None
    ) -> Optional[Dict[str, str]]:
        """Read one canonical strategy row (optionally owner-scoped)."""
        owns_db = db is None
        session = db or self.session_factory()
        try:
            query = select(Strategy).where(Strategy.id == strategy_id)
            if owner_id is not None:
                query = query.where(Strategy.owner_id == owner_id)
            row = session.execute(query).scalar_one_or_none()
            if row is None:
                return None
            return {
                "strategy_id": str(row.id),
                "owner_id": str(row.owner_id),
                "name": str(row.name),
                "account_scope": str(row.account_scope),
                "status": str(row.status),
            }
        finally:
            if owns_db:
                session.close()

    def create_run_with_binding(
        self,
        *,
        token: Any,
        payload: Any,
        strategy_run_id: str,
        binding: Optional[RunBindingInput],
    ) -> Dict[str, Any]:
        """Insert the run and its mandatory binding in ONE session/transaction.

        A crash or failure leaves **neither** row. ``binding=None`` is the
        explicit legacy-compatibility path: the run is created unattributed.
        """
        session = self.session_factory()
        try:
            # The run table uses JSONB on PostgreSQL; plain text parameters on
            # SQLite keep the JSON intact there (CAST(x AS JSONB) would coerce
            # to NUMERIC and silently corrupt the payload in the test database).
            postgres = session.bind.dialect.name == "postgresql"

            def _json_param(name: str) -> str:
                return f"CAST(:{name} AS JSONB)" if postgres else f":{name}"

            row = session.execute(
                text(
                    f"""
                    INSERT INTO public.algo_worker_runs (
                        strategy_run_id, token_id, template_id, account_scope, execution_mode,
                        status, summary_fields_json, risk_schema_json, allowed_actions_json,
                        runtime_state_json, metadata_json
                    ) VALUES (
                        :strategy_run_id, :token_id, :template_id, :account_scope, :execution_mode,
                        'open', {_json_param("summary_fields_json")}, {_json_param("risk_schema_json")},
                        {_json_param("allowed_actions_json")}, {_json_param("runtime_state_json")},
                        {_json_param("metadata_json")}
                    )
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "token_id": token.token_id,
                    "template_id": payload.template_id,
                    "account_scope": payload.account_scope,
                    "execution_mode": payload.execution_mode,
                    "summary_fields_json": _json_dumps(payload.summary_fields),
                    "risk_schema_json": _json_dumps(payload.risk_schema),
                    "allowed_actions_json": _json_dumps(payload.allowed_actions),
                    "runtime_state_json": _json_dumps(payload.runtime_state),
                    "metadata_json": _json_dumps(payload.metadata),
                },
            ).fetchone()
            if binding is not None:
                try:
                    self.bind_run(
                        strategy_run_id=strategy_run_id,
                        strategy_id=binding.strategy_id,
                        owner_id=binding.owner_id,
                        account_id=binding.account_id,
                        execution_environment=binding.execution_environment,
                        bound_by=binding.bound_by,
                        binding_source=binding.binding_source,
                        db=session,
                    )
                except Exception as exc:
                    # The run insert already ran, so any failure here is a
                    # binding failure; the caller's rollback removes both rows.
                    raise RunBindingFailed(str(exc)) from exc
            session.commit()
            result = _row_mapping(row) if row is not None else {}
            return {
                "strategy_run_id": str(result.get("strategy_run_id") or strategy_run_id),
                "token_id": str(result.get("token_id") or token.token_id),
                "template_id": str(result.get("template_id") or payload.template_id),
                "account_scope": str(result.get("account_scope") or payload.account_scope),
                "execution_mode": str(result.get("execution_mode") or payload.execution_mode),
                "status": str(result.get("status") or "open"),
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -------------------------------------------------------------- ownership

    def resolve_owned_orders(
        self, *, account_id: str, db: Optional[Any] = None
    ) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
        """Map broker order id -> owning strategy run, surfacing corruption.

        Execution links are authoritative; a placed live intent is a fallback
        only when no execution link exists. Multiple distinct owners within
        either source, or a link-versus-intent disagreement, are corruption: the
        order is **excluded** from the owned map and reported as an anomaly.
        Never picks a row silently.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            link_rows = session.execute(
                text(
                    """
                    SELECT broker_order_id, strategy_run_id
                    FROM public.worker_live_execution_links
                    WHERE account_id = :account_id
                      AND broker_order_id IS NOT NULL
                    """
                ),
                {"account_id": account_id},
            ).fetchall()
            intent_rows = session.execute(
                text(
                    """
                    SELECT broker_order_id, strategy_run_id
                    FROM public.live_order_intents
                    WHERE account_id = :account_id
                      AND broker_order_id IS NOT NULL
                    """
                ),
                {"account_id": account_id},
            ).fetchall()
        finally:
            if owns_db:
                session.close()

        def _owners(rows: Sequence[Any]) -> Dict[str, Set[str]]:
            grouped: Dict[str, Set[str]] = {}
            for row in rows:
                values = list(row)
                order_id = str(values[0] or "").strip()
                run_id = str(values[1] or "").strip()
                if not order_id or not run_id:
                    continue
                grouped.setdefault(order_id, set()).add(run_id)
            return grouped

        link_owners = _owners(link_rows)
        intent_owners = _owners(intent_rows)

        owned: Dict[str, str] = {}
        anomalies: List[Dict[str, Any]] = []
        for order_id in sorted(set(link_owners) | set(intent_owners)):
            links = link_owners.get(order_id, set())
            intents = intent_owners.get(order_id, set())
            if len(links) > 1 or len(intents) > 1:
                anomalies.append(
                    {
                        "broker_order_id": order_id,
                        "kind": "multi_owner",
                        "runs": sorted(links | intents),
                    }
                )
                continue
            link_owner = next(iter(links), None)
            intent_owner = next(iter(intents), None)
            if link_owner is not None and intent_owner is not None and link_owner != intent_owner:
                anomalies.append(
                    {
                        "broker_order_id": order_id,
                        "kind": "link_intent_disagreement",
                        "runs": sorted({link_owner, intent_owner}),
                    }
                )
                continue
            owner = link_owner or intent_owner
            if owner is not None:
                owned[order_id] = owner
        return owned, anomalies

    # ------------------------------------------------------------ fact sources

    def iter_live_trade_facts(
        self,
        *,
        account_id: str,
        owned_orders: Dict[str, str],
        bound_run_ids: Set[str],
        db: Optional[Any] = None,
    ) -> List[TradeFact]:
        """All fills of owned orders whose run is bound **in the live book**.

        Trade links never gate inclusion: a failed link upsert must not hide a
        real fill. Facts are deduplicated by the durable broker fill identity
        ``(account_id, trade_id)``.
        """
        if not owned_orders:
            return []
        owns_db = db is None
        session = db or self.session_factory()
        try:
            # ``= ANY(:array)`` is PostgreSQL; other dialects (the SQLite test
            # database) need an expanding IN list. Both read the same rows.
            if session.bind.dialect.name == "postgresql":
                ownership_predicate = "otf.order_id = ANY(:owned_order_ids)"
                params: Dict[str, Any] = {
                    "account_id": account_id,
                    "owned_order_ids": list(owned_orders),
                }
                statement = text(
                    f"""
                    SELECT otf.order_id, otf.trade_id,
                           otf.instrument_token,
                           COALESCE(NULLIF(otf.exchange, ''), otf.payload_json ->> 'exchange')       AS exchange,
                           COALESCE(NULLIF(otf.tradingsymbol, ''), otf.payload_json ->> 'tradingsymbol') AS tradingsymbol,
                           COALESCE(NULLIF(otf.product, ''), otf.payload_json ->> 'product')         AS product,
                           CASE WHEN UPPER(COALESCE(NULLIF(otf.transaction_type, ''), otf.payload_json ->> 'transaction_type')) = 'BUY'
                                THEN otf.quantity ELSE -otf.quantity END                              AS signed_quantity,
                           otf.fill_timestamp
                    FROM public.order_trade_fills otf
                    WHERE otf.account_id = :account_id
                      AND {ownership_predicate}
                    ORDER BY otf.fill_timestamp ASC, otf.trade_id ASC
                    """
                )
            else:
                ownership_predicate = "otf.order_id IN :owned_order_ids"
                params = {"account_id": account_id}
                statement = text(
                    f"""
                    SELECT otf.order_id, otf.trade_id,
                           otf.instrument_token,
                           COALESCE(NULLIF(otf.exchange, ''), json_extract(otf.payload_json, '$.exchange'))       AS exchange,
                           COALESCE(NULLIF(otf.tradingsymbol, ''), json_extract(otf.payload_json, '$.tradingsymbol')) AS tradingsymbol,
                           COALESCE(NULLIF(otf.product, ''), json_extract(otf.payload_json, '$.product'))         AS product,
                           CASE WHEN UPPER(COALESCE(NULLIF(otf.transaction_type, ''), json_extract(otf.payload_json, '$.transaction_type'))) = 'BUY'
                                THEN otf.quantity ELSE -otf.quantity END                              AS signed_quantity,
                           otf.fill_timestamp
                    FROM public.order_trade_fills otf
                    WHERE otf.account_id = :account_id
                      AND {ownership_predicate}
                    ORDER BY otf.fill_timestamp ASC, otf.trade_id ASC
                    """
                ).bindparams(bindparam("owned_order_ids", value=list(owned_orders), expanding=True))
            rows = session.execute(statement, params).fetchall()
        finally:
            if owns_db:
                session.close()

        facts: List[TradeFact] = []
        seen: Set[str] = set()
        for row in rows:
            values = _row_mapping(row)
            trade_id = str(values.get("trade_id") or "").strip()
            order_id = str(values.get("order_id") or "").strip()
            if not trade_id or trade_id in seen:
                continue
            seen.add(trade_id)
            run_id = owned_orders.get(order_id)
            if not run_id or run_id not in bound_run_ids:
                continue
            facts.append(
                TradeFact(
                    source_key=f"trade:{trade_id}",
                    strategy_run_id=run_id,
                    execution_environment="live",
                    instrument_token=int(values.get("instrument_token") or 0),
                    exchange=str(values.get("exchange") or ""),
                    tradingsymbol=str(values.get("tradingsymbol") or ""),
                    product=str(values.get("product") or ""),
                    signed_quantity=int(values.get("signed_quantity") or 0),
                    effective_at=_as_datetime(values.get("fill_timestamp")),
                    pinned_generation=None,
                )
            )
        return facts

    def iter_paper_trade_facts(
        self,
        *,
        account_scope: str,
        bound_run_ids: Set[str],
        db: Optional[Any] = None,
    ) -> List[TradeFact]:
        """Paper fills of runs bound in the paper book, deduplicated by trade id.

        Paper rebuilds consume only paper bindings and paper trades; a paper
        trade can never appear in a live fold.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            rows = session.execute(
                text(
                    """
                    SELECT pt.trade_id, po.instrument_token, po.exchange, po.tradingsymbol, po.product,
                           CASE WHEN UPPER(pt.transaction_type) = 'BUY' THEN pt.quantity ELSE -pt.quantity END AS signed_quantity,
                           pt.trade_timestamp,
                           COALESCE(po.metadata_json -> 'attribution' ->> 'strategy_run_id',
                                    po.metadata_json ->> 'strategy_run_id') AS strategy_run_id
                    FROM public.paper_trades pt
                    INNER JOIN public.paper_orders po
                      ON po.account_scope = pt.account_scope AND po.order_id = pt.order_id
                    WHERE pt.account_scope = :account_scope
                    ORDER BY pt.trade_timestamp ASC, pt.trade_id ASC
                    """
                ),
                {"account_scope": account_scope},
            ).fetchall()
        finally:
            if owns_db:
                session.close()

        facts: List[TradeFact] = []
        seen: Set[str] = set()
        for row in rows:
            values = _row_mapping(row)
            trade_id = str(values.get("trade_id") or "").strip()
            run_id = str(values.get("strategy_run_id") or "").strip()
            if not trade_id or trade_id in seen:
                continue
            seen.add(trade_id)
            if not run_id or run_id not in bound_run_ids:
                continue
            facts.append(
                TradeFact(
                    source_key=f"paper:{trade_id}",
                    strategy_run_id=run_id,
                    execution_environment="paper",
                    instrument_token=int(values.get("instrument_token") or 0),
                    exchange=str(values.get("exchange") or ""),
                    tradingsymbol=str(values.get("tradingsymbol") or ""),
                    product=str(values.get("product") or ""),
                    signed_quantity=int(values.get("signed_quantity") or 0),
                    effective_at=_as_datetime(values.get("trade_timestamp")),
                    pinned_generation=None,
                )
            )
        return facts

    # ----------------------------------------------------- identity resolution

    def resolve_fact_identity(self, fact: TradeFact, *, db: Optional[Any] = None) -> PositionKey:
        """Resolve ONE fact to its position key. Never grouped.

        The mapping interval is selected by **this fact's own** ``effective_at``
        (or its pinned generation), never by today's ``is_current`` mapping, so a
        token re-mapped to a different instrument across generations yields two
        genuinely distinct positions. The matched mapping must also agree with
        the fill's exchange/symbol evidence; a contradiction is unresolved
        (``evidence_mismatch``), never silently accepted.

        Unresolved facts keep an explicit catalog-evidence era identity, so facts
        from the same era net across dates while facts from distinct known eras
        never merge.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            params: Dict[str, Any] = {
                "broker_token": str(fact.instrument_token),
                "effective_at": fact.effective_at,
            }
            pinned_clause = ""
            if fact.pinned_generation:
                params["pinned_generation"] = fact.pinned_generation
                pinned_clause = "AND m.valid_from_generation = :pinned_generation"
            rows = session.execute(
                text(
                    f"""
                    SELECT m.instrument_id,
                           m.broker_exchange,
                           m.broker_symbol,
                           m.valid_from_generation,
                           m.valid_to_generation
                    FROM public.instrument_broker_mappings m
                    JOIN public.instrument_catalog_generations g_from ON g_from.id = m.valid_from_generation
                    LEFT JOIN public.instrument_catalog_generations g_to ON g_to.id = m.valid_to_generation
                    WHERE m.broker = 'kite'
                      AND m.broker_token = :broker_token
                      {pinned_clause}
                      AND g_from.published_at <= :effective_at
                      AND (g_to.published_at IS NULL OR g_to.published_at > :effective_at)
                    """
                ),
                params,
            ).fetchall()

            active_generation = None
            if not rows:
                active_row = session.execute(
                    text(
                        """
                        SELECT id
                        FROM public.instrument_catalog_generations
                        WHERE published_at IS NOT NULL
                          AND published_at <= :effective_at
                        ORDER BY published_at DESC
                        LIMIT 1
                        """
                    ),
                    {"effective_at": fact.effective_at},
                ).fetchone()
                active_generation = str(list(active_row)[0]) if active_row is not None else None
        finally:
            if owns_db:
                session.close()

        raw_tuple = (
            f"kite:{fact.instrument_token}|{fact.exchange}|{fact.tradingsymbol}|{fact.product}"
        )

        if rows:
            candidates = {str(_row_mapping(row).get("instrument_id") or "") for row in rows}
            candidates.discard("")
            evidence_ok = any(
                str(_row_mapping(row).get("broker_exchange") or "").strip() == str(fact.exchange).strip()
                and str(_row_mapping(row).get("broker_symbol") or "").strip() == str(fact.tradingsymbol).strip()
                for row in rows
            )
            if len(candidates) == 1 and evidence_ok:
                return PositionKey(
                    fact.execution_environment, "canonical", next(iter(candidates)), fact.product
                )
            if not evidence_ok and len(candidates) == 1:
                reason = "evidence_mismatch"
            else:
                reason = "mapping_ambiguous"
            era = self._era_from_rows(rows, fact)
            if reason == "evidence_mismatch":
                # Marked in the era so the reason stays recoverable from the
                # identity alone; facts sharing this era still net together.
                era = f"evidence-mismatch:{era}"
        else:
            reason = "mapping_missing"
            era = f"gen:{active_generation}" if active_generation else "pre-catalog"

        return PositionKey(
            fact.execution_environment, "raw", f"raw:{raw_tuple}|era={era}", fact.product
        )

    @staticmethod
    def _era_from_rows(rows: Sequence[Any], fact: TradeFact) -> str:
        """Catalog-evidence era for an unresolved fact (never date-derived)."""
        if fact.pinned_generation:
            return f"gen:{fact.pinned_generation}"
        eras = {
            f"interval:{_row_mapping(row).get('valid_from_generation') or 'pre-catalog'}.."
            f"{_row_mapping(row).get('valid_to_generation') or ''}"
            for row in rows
        }
        return sorted(eras)[0] if eras else "pre-catalog"

    # ------------------------------------------------------------ publication

    def recompute_publish(
        self,
        *,
        account_id: str,
        strategy_id: str,
        execution_environment: str,
        resolve_and_fold: Callable[[Any, Set[str]], Tuple[List[Dict[str, Any]], str]],
        on_before_commit: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """The single supported publication path.

        One transaction: begin -> advisory lock for
        ``(account, strategy, environment)`` -> read bound runs -> run the
        caller's pipeline **on that same session** -> replace rows -> advance
        ``projection_version`` -> optional hook -> commit. Because the lock is
        taken before the snapshot by construction, an older snapshot cannot
        overwrite a newer rebuild. Any exception rolls back and fully retains the
        previous projection and version.

        There is deliberately no out-of-transaction snapshot path: publication
        outside this transaction is unsupported and fails closed.
        """
        session = self.session_factory()
        try:
            self._lock_projection(session, account_id, strategy_id, execution_environment)
            bound = self.bound_run_ids(
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=execution_environment,
                db=session,
            )
            rows, content_sha256 = resolve_and_fold(session, bound)

            state = session.execute(
                select(StrategyProjectionState).where(
                    StrategyProjectionState.account_id == account_id,
                    StrategyProjectionState.strategy_id == strategy_id,
                    StrategyProjectionState.execution_environment == execution_environment,
                )
            ).scalar_one_or_none()

            if state is not None and state.content_sha256 == content_sha256:
                if on_before_commit is not None:
                    on_before_commit()
                session.commit()
                return {
                    "projection_version": int(state.projection_version or 0),
                    "content_sha256": content_sha256,
                    "unchanged": True,
                }

            projection_version = int(state.projection_version or 0) + 1 if state is not None else 1

            session.execute(
                delete(StrategyPositionProjection).where(
                    StrategyPositionProjection.account_id == account_id,
                    StrategyPositionProjection.strategy_id == strategy_id,
                    StrategyPositionProjection.execution_environment == execution_environment,
                )
            )
            for row in rows:
                session.add(
                    StrategyPositionProjection(
                        account_id=account_id,
                        strategy_id=strategy_id,
                        execution_environment=execution_environment,
                        identity_kind=str(row["identity_kind"]),
                        identity_key=str(row["identity_key"]),
                        product=str(row["product"]),
                        canonical_instrument_id=row.get("canonical_instrument_id"),
                        instrument_token=int(row["instrument_token"]),
                        exchange=str(row["exchange"]),
                        tradingsymbol=str(row["tradingsymbol"]),
                        net_quantity=int(row["net_quantity"]),
                        unresolved_reason=row.get("unresolved_reason"),
                        projection_version=projection_version,
                    )
                )

            if state is None:
                session.add(
                    StrategyProjectionState(
                        account_id=account_id,
                        strategy_id=strategy_id,
                        execution_environment=execution_environment,
                        projection_version=projection_version,
                        content_sha256=content_sha256,
                        last_rebuild_at=_utcnow(),
                    )
                )
            else:
                state.projection_version = projection_version
                state.content_sha256 = content_sha256
                state.last_rebuild_at = _utcnow()

            session.flush()
            if on_before_commit is not None:
                on_before_commit()
            session.commit()
            return {
                "projection_version": projection_version,
                "content_sha256": content_sha256,
                "unchanged": False,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _lock_projection(session: Any, account_id: str, strategy_id: str, execution_environment: str) -> None:
        """Serialize recomputes for one book. PostgreSQL only; SQLite no-op.

        ``pg_advisory_xact_lock`` is transaction-scoped, so the lock is held from
        here until commit — strictly before the fact snapshot is taken.
        """
        if session.bind.dialect.name != "postgresql":
            return
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{account_id}:{strategy_id}:{execution_environment}"},
        )

    # -------------------------------------------------------- unresolved facts

    @staticmethod
    def unresolved_reason_for(key: PositionKey) -> Optional[str]:
        """Why a raw identity is unresolved, derived from its catalog-evidence era.

        The store owns era semantics, so the reason lives beside them rather than
        being encoded in the immutability key: an ``evidence_mismatch`` era is
        explicit, a bare ``gen:``/``pre-catalog`` era means no mapping covered the
        fact, and an ``interval:`` era means the window was found but ambiguous.
        """
        if key.identity_kind != "raw":
            return None
        era = str(key.identity_key).rsplit("|era=", 1)[-1]
        if era.startswith("evidence-mismatch:"):
            return "evidence_mismatch"
        if era.startswith("gen:") or era == "pre-catalog":
            return "mapping_missing"
        return "mapping_ambiguous"


class StrategyAttributionService:
    """Full recompute, rebuild, open positions and unresolved surfacing.

    Publication is on demand: G1 adds no scheduler and no background loop. The
    projection is always a **full recompute** of the environment's book, so
    ``rebuild`` and ``publish`` are the same operation and there is no
    incremental cursor that a late fill could slip behind.
    """

    def __init__(self, store: SqlAttributionStore, *, run_async: Callable[..., Any] = None) -> None:
        if run_async is None:
            import asyncio

            run_async = asyncio.to_thread
        self.store = store
        self._run_async = run_async

    async def publish(self, *, account_id: str, strategy_id: str, execution_environment: str) -> Dict[str, Any]:
        """Recompute and publish the book for one ``(account, strategy, env)``."""
        return await self._run_async(
            self._publish_sync,
            account_id=account_id,
            strategy_id=strategy_id,
            execution_environment=execution_environment,
        )

    async def rebuild(self, *, account_id: str, strategy_id: str, execution_environment: str) -> Dict[str, Any]:
        """Identical to :meth:`publish` — full recompute IS the V1 model."""
        return await self.publish(
            account_id=account_id,
            strategy_id=strategy_id,
            execution_environment=execution_environment,
        )

    async def open_positions(
        self, *, account_id: str, strategy_id: str, execution_environment: str
    ) -> List[Dict[str, Any]]:
        """Non-zero rows for **that environment only**, ordered by identity.

        Settlement and admission consumers call this with
        ``execution_environment="live"``; the paper book is never visible to them.
        """
        return await self._run_async(
            self._open_positions_sync,
            account_id=account_id,
            strategy_id=strategy_id,
            execution_environment=execution_environment,
        )

    # ------------------------------------------------------------------ syncing

    def _publish_sync(self, *, account_id: str, strategy_id: str, execution_environment: str) -> Dict[str, Any]:
        captured: Dict[str, Any] = {"unresolved": [], "anomalies": [], "folded_facts": 0}

        def _resolve_and_fold(db: Any, bound_run_ids: Set[str]) -> Tuple[List[Dict[str, Any]], str]:
            anomalies: List[Dict[str, Any]] = []
            if execution_environment == "live":
                owned_orders, conflicts = self.store.resolve_owned_orders(account_id=account_id, db=db)
                anomalies.extend(conflicts)
                facts = self.store.iter_live_trade_facts(
                    account_id=account_id,
                    owned_orders=owned_orders,
                    bound_run_ids=bound_run_ids,
                    db=db,
                )
                # Append-only owner reclassifications enter the live book as
                # immutable facts with stable source identity, so every rebuild
                # folds each line exactly once (D-3). They are not runs, so they
                # are deliberately not subject to the bound-run filter.
                facts = list(facts) + self.store.iter_adjustment_facts(
                    account_id=account_id, strategy_id=strategy_id, db=db
                )
            else:
                # Paper and dry_run share the paper-shaped sources but are never
                # mixed: the binding filter keeps the books apart, and a dry-run
                # fact is relabelled so it can never land in the paper book.
                # The current dry-run path persists no fills, so this is normally
                # empty; no simulated fills are manufactured here.
                facts = self.store.iter_paper_trade_facts(
                    account_scope=account_id,
                    bound_run_ids=bound_run_ids,
                    db=db,
                )
                if execution_environment == "dry_run":
                    facts = [replace(fact, execution_environment="dry_run") for fact in facts]

            captured["folded_facts"] = len(facts)

            resolved: List[Tuple[TradeFact, PositionKey]] = []
            representative: Dict[PositionKey, TradeFact] = {}
            for fact in facts:
                key = self.store.resolve_fact_identity(fact, db=db)
                resolved.append((fact, key))
                representative.setdefault(key, fact)

            positions = AttributionFold.fold(resolved)

            rows: List[Dict[str, Any]] = []
            unresolved: List[Dict[str, Any]] = []
            for key, quantity in positions.items():
                fact = representative[key]
                reason = SqlAttributionStore.unresolved_reason_for(key)
                rows.append(
                    {
                        "identity_kind": key.identity_kind,
                        "identity_key": key.identity_key,
                        "canonical_instrument_id": (
                            key.identity_key if key.identity_kind == "canonical" else None
                        ),
                        "instrument_token": fact.instrument_token,
                        "exchange": fact.exchange,
                        "tradingsymbol": fact.tradingsymbol,
                        "product": key.product,
                        "net_quantity": int(quantity),
                        "unresolved_reason": reason,
                    }
                )
                if reason is not None:
                    unresolved.append(
                        {
                            "identity_kind": key.identity_kind,
                            "identity_key": key.identity_key,
                            "product": key.product,
                            "net_quantity": int(quantity),
                            "unresolved_reason": reason,
                            "condition": UNRESOLVED_INSTRUMENT_IDENTITY,
                        }
                    )

            rows.sort(key=lambda row: (row["identity_kind"], row["identity_key"], row["product"]))
            unresolved.sort(key=lambda entry: (entry["identity_key"], entry["product"]))
            captured["unresolved"] = unresolved
            captured["anomalies"] = anomalies
            return rows, _content_sha256(rows)

        result = self.store.recompute_publish(
            account_id=account_id,
            strategy_id=strategy_id,
            execution_environment=execution_environment,
            resolve_and_fold=_resolve_and_fold,
        )
        return {
            "strategy_id": strategy_id,
            "execution_environment": execution_environment,
            "folded_facts": captured["folded_facts"],
            "projection_version": result["projection_version"],
            "content_sha256": result["content_sha256"],
            "unresolved": captured["unresolved"],
            "anomalies": captured["anomalies"],
            "unchanged": result["unchanged"],
        }

    def _open_positions_sync(
        self, *, account_id: str, strategy_id: str, execution_environment: str
    ) -> List[Dict[str, Any]]:
        session = self.store.session_factory()
        try:
            rows = session.execute(
                select(StrategyPositionProjection)
                .where(
                    StrategyPositionProjection.account_id == account_id,
                    StrategyPositionProjection.strategy_id == strategy_id,
                    StrategyPositionProjection.execution_environment == execution_environment,
                    StrategyPositionProjection.net_quantity != 0,
                )
                .order_by(
                    StrategyPositionProjection.identity_kind,
                    StrategyPositionProjection.identity_key,
                    StrategyPositionProjection.product,
                )
            ).scalars().all()
            return [
                {
                    "account_id": str(row.account_id),
                    "strategy_id": str(row.strategy_id),
                    "execution_environment": str(row.execution_environment),
                    "identity_kind": str(row.identity_kind),
                    "identity_key": str(row.identity_key),
                    "canonical_instrument_id": (
                        str(row.canonical_instrument_id) if row.canonical_instrument_id else None
                    ),
                    "instrument_token": int(row.instrument_token),
                    "exchange": str(row.exchange),
                    "tradingsymbol": str(row.tradingsymbol),
                    "product": str(row.product),
                    "net_quantity": int(row.net_quantity),
                    "unresolved_reason": row.unresolved_reason,
                    "projection_version": int(row.projection_version),
                }
                for row in rows
            ]
        finally:
            session.close()


def _content_sha256(rows: Sequence[Dict[str, Any]]) -> str:
    """Content hash over the row serialization.

    Used **only** for idempotence (recognising an unchanged recompute). It is
    never chronology evidence: a matching hash says "the facts produced the same
    book", not "this snapshot is newer than another".
    """
    payload = json.dumps(list(rows), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
