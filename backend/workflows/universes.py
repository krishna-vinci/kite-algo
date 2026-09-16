"""Universe membership subsystem for the alerts platform.

A *universe* is a named, owner-scoped, exchange-qualified instrument list that
alert workflows can reference. Three kinds exist:

- ``explicit``: a hand-picked ``members`` list of ``EXCHANGE:SYMBOL`` keys.
- ``index``: constituents of a supported index source list (``Nifty50`` ...)
  read from ``public.kite_ticker_tickers`` via an injectable loader.
- ``portfolio``: owner-scoped, READ-ONLY holdings-derived membership. The
  holdings fetch is injected as a ``portfolio_provider`` callable so this
  module never imports ``backend.broker_api`` (circular-import safety); the
  API layer wires the real provider, tests inject fakes.

Instrument identity is the exchange-qualified public key ``EXCHANGE:SYMBOL``
(e.g. ``NSE:RELIANCE``): the same text on NSE and BSE is a DIFFERENT
instrument, and every member is resolved through the published instrument
catalog (``public.instrument_catalog_published_v`` via
:class:`~backend.broker_api.instruments.catalog.InstrumentCatalog`, imported
lazily inside methods). Members whose catalog lifecycle is not ``active`` are
recorded in the revision coverage under ``rejected`` — never silently dropped
and never fatal. A whole-catalog failure (``CatalogUnavailableError``)
propagates and never persists an empty revision.

Persistence lives in ``public.universes`` / ``public.universe_revisions``
(migration ``backend/alembic/versions/20260909_000014_universe_membership.py``).
The SQLAlchemy models below register on the SAME declarative ``Base`` as
``backend.workflows.repository`` so tests can ``Base.metadata.create_all``
against SQLite; the column types adapt per dialect (``UUID``/``TEXT[]`` on
PostgreSQL, ``CHAR(36)``/``JSON`` on SQLite).

Every ``resolve_membership`` serializes per universe: on PostgreSQL an
advisory transaction lock (``pg_advisory_xact_lock(hashtext(...))``) plus
``FOR UPDATE`` on the universe row; the ``MAX(revision)+1`` read, the insert,
and the lock share one transaction, with the
``uq_universe_revisions_universe_revision`` unique constraint as backstop.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.types import CHAR, JSON as SA_JSON, TypeDecorator

from backend.workflows.repository import Base

__all__ = [
    "Universe",
    "UniverseRevision",
    "UniverseService",
    "UniverseError",
    "UniverseExistsError",
    "UniverseNotFoundError",
    "UniverseValidationError",
    "UniverseSourceUnavailable",
    "SUPPORTED_UNIVERSE_KINDS",
]

SUPPORTED_UNIVERSE_KINDS = ("explicit", "index", "portfolio", "screener")

# Index source lists whose constituents were ingested into
# public.kite_ticker_tickers. Validated against the ingestion module lazily
# (backend.broker_api is never imported at module import time).
_DEFAULT_INDEX_SOURCE_LISTS = ("Nifty50", "Nifty500", "NiftyBank")


def supported_index_source_lists() -> List[str]:
    """Index source lists this install can actually resolve.

    Public because two callers need the SAME answer: universe validation here,
    and the capabilities endpoint that tells the authoring UI which index lists
    to offer. A hard-coded copy in either place would drift the moment the
    ingestion registry gains a list, and the UI would then offer a value the
    validator rejects.
    """
    try:
        from backend.broker_api.instruments.index_ingestion import (
            list_supported_index_source_lists,
        )

        return list(list_supported_index_source_lists())
    except Exception:
        # The ingestion module pulls redis/kite clients; fall back to the
        # pinned list when it cannot even be imported in this process.
        return list(_DEFAULT_INDEX_SOURCE_LISTS)


_NAME_MAX_LENGTH = 255

# "EXCHANGE:SYMBOL" — exactly one colon, both parts non-empty. Uppercased by
# the normalizer; symbols may contain A-Z, digits, and -_.& (e.g. M&M,
# BAJAJ-AUTO).
_PUBLIC_KEY_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.&\-]*:[A-Z0-9][A-Z0-9_.&\-]*$")


# ---------------------------------------------------------------------------
# typed errors
# ---------------------------------------------------------------------------


class UniverseError(Exception):
    """Base class for universe subsystem failures."""


class UniverseExistsError(UniverseError):
    """A universe with the same (owner_id, name) already exists."""


class UniverseNotFoundError(UniverseError):
    """No universe with that name for this owner (owner isolation)."""


class UniverseValidationError(UniverseError):
    """The request violates a universe invariant (bad kind, bad keys...)."""


class UniverseSourceUnavailable(UniverseError):
    """A membership source (index ingestion, portfolio provider) is down or
    empty. An unavailable membership source is NEVER an empty successful
    universe."""


# ---------------------------------------------------------------------------
# dialect-adaptive column types
# ---------------------------------------------------------------------------


class GUID(TypeDecorator):
    """UUID on PostgreSQL, CHAR(36) elsewhere (SQLite tests)."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[override]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=False))
        return dialect.type_descriptor(CHAR(36))


class StringArray(TypeDecorator):
    """TEXT[] on PostgreSQL, JSON-encoded list elsewhere (SQLite tests)."""

    impl = SA_JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[override]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_ARRAY(String()))
        return dialect.type_descriptor(SA_JSON())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_postgres(session: Any) -> bool:
    bind = getattr(session, "bind", None)
    return bind is not None and getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"


def _qualified_table(session: Any, name: str) -> str:
    return f"public.{name}" if _is_postgres(session) else name


def _iso(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        # Raw freshness reads (best-effort SQL) can hand back driver strings;
        # normalize parseable ones to UTC ISO-8601.
        try:
            return _iso(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _parse_dt(value: Optional[Any]) -> Optional[datetime]:
    """Best-effort datetime coercion (raw SQL freshness reads return strings)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_public_key(value: str) -> str:
    """Validate and canonicalize one ``EXCHANGE:SYMBOL`` public key.

    Uppercases and strips whitespace; raises :class:`UniverseValidationError`
    unless the value is well-formed (exactly one colon, non-empty parts).
    """
    raw = str(value or "").strip().upper()
    if not _PUBLIC_KEY_RE.match(raw):
        raise UniverseValidationError(
            f"invalid instrument key {value!r}: expected well-formed EXCHANGE:SYMBOL "
            "(uppercase, exactly one ':', non-empty parts)"
        )
    return raw


def _qualify_symbol(value: str) -> str:
    """Exchange-qualify a loader-provided symbol (bare symbols default NSE)."""
    raw = str(value or "").strip().upper()
    if ":" in raw:
        return normalize_public_key(raw)
    if not raw:
        raise UniverseSourceUnavailable("index source produced an empty symbol")
    return f"NSE:{raw}"


# ---------------------------------------------------------------------------
# tables (same Base as backend.workflows.repository)
# ---------------------------------------------------------------------------


class Universe(Base):
    __tablename__ = "universes"

    id = Column(GUID, primary_key=True, default=_uuid)
    owner_id = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    kind = Column(String(16), nullable=False)  # explicit | index | portfolio
    source_config = Column(JSON, nullable=False, default=dict)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_universes_owner_name"),
    )


class UniverseRevision(Base):
    __tablename__ = "universe_revisions"

    id = Column(GUID, primary_key=True, default=_uuid)
    universe_id = Column(GUID, ForeignKey("universes.id"), nullable=False)
    revision = Column(Integer, nullable=False)
    expression = Column(JSON, nullable=False, default=dict)
    members = Column(StringArray, nullable=False, default=list)
    member_count = Column(Integer, nullable=False, default=0)
    # Catalog generation the membership was resolved against (NULL when the
    # catalog exposed no generation).
    source_generation = Column(GUID, nullable=True)
    coverage = Column(JSON, nullable=False, default=dict)
    resolved_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "universe_id", "revision", name="uq_universe_revisions_universe_revision"
        ),
        Index("idx_universe_revisions_universe_resolved", "universe_id", "resolved_at"),
    )


# ---------------------------------------------------------------------------
# portfolio provider protocol
# ---------------------------------------------------------------------------

# PortfolioMembershipProvider protocol: callable(owner_id: str,
# source_config: dict) -> list[str] of exchange-qualified public keys.
# Failures MUST raise UniverseSourceUnavailable. Read-only and owner-scoped by
# contract: the provider only ever sees the owner_id derived from the worker
# token identity.
PortfolioMembershipProvider = Callable[[str, Dict[str, Any]], List[str]]

# IndexConstituentsLoader protocol: callable(source_list: str) -> list[str]
# returning tradingsymbols (bare symbols are exchange-qualified to NSE by the
# service; fully qualified keys pass through).
IndexConstituentsLoader = Callable[[str], List[str]]


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class UniverseService:
    """Owner-scoped universe CRUD + catalog-resolved membership revisions."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        catalog: Optional[Any] = None,
        portfolio_provider: Optional[PortfolioMembershipProvider] = None,
        index_constituents_loader: Optional[IndexConstituentsLoader] = None,
    ) -> None:
        self.session_factory = session_factory
        # None means lazily constructed InstrumentCatalog (import inside the
        # method so this module never imports backend.broker_api at import
        # time).
        self._catalog = catalog
        self._portfolio_provider = portfolio_provider
        self._index_constituents_loader = index_constituents_loader

    # -- catalog / source plumbing ------------------------------------------

    def _catalog_for(self, catalog_session_factory: Optional[Any] = None):
        if self._catalog is not None:
            return self._catalog
        from backend.broker_api.instruments.catalog import InstrumentCatalog

        if catalog_session_factory is not None:
            return InstrumentCatalog(catalog_session_factory)
        return InstrumentCatalog()

    def _default_index_loader(self, source_list: str) -> List[str]:
        """Read DISTINCT constituents from public.kite_ticker_tickers.

        Members are exchange-qualified using each row's ``exchange`` column
        (rows without one default to NSE).
        """
        session = self.session_factory()
        try:
            table = _qualified_table(session, "kite_ticker_tickers")
            rows = session.execute(
                text(
                    f"SELECT tradingsymbol, exchange FROM {table} "
                    "WHERE source_list = :source"
                ),
                {"source": source_list},
            ).all()
        finally:
            session.close()
        keys: List[str] = []
        for tradingsymbol, exchange in rows:
            symbol = str(tradingsymbol or "").strip().upper()
            if not symbol:
                continue
            keys.append(f"{str(exchange or '').strip().upper() or 'NSE'}:{symbol}")
        if not keys:
            raise UniverseSourceUnavailable(
                f"index source list {source_list!r} has no ingested constituents"
            )
        return keys

    def _supported_index_source_lists(self) -> List[str]:
        return supported_index_source_lists()

    def _normalize_index_source_list(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            raise UniverseValidationError("index universes require source_config.source_list")
        supported = self._supported_index_source_lists()
        for name in supported:
            if raw.lower().replace("-", "").replace("_", "") == name.lower().replace("-", "").replace("_", ""):
                return name
        raise UniverseValidationError(
            f"unsupported index source_list {raw!r}; supported: {', '.join(sorted(supported))}"
        )

    def _normalize_explicit_members(self, value: Any) -> List[str]:
        if not isinstance(value, (list, tuple)) or not value:
            raise UniverseValidationError(
                "explicit universes require source_config.members: a non-empty list of EXCHANGE:SYMBOL keys"
            )
        members: List[str] = []
        for item in value:
            if not isinstance(item, str):
                raise UniverseValidationError(
                    f"explicit members must be strings, got {item!r}"
                )
            members.append(normalize_public_key(item))
        return sorted(set(members))

    def _validated_source_config(self, kind: str, source_config: Any) -> Dict[str, Any]:
        """Validate (kind, source_config) WITHOUT touching the database.

        Shared by create_universe and preview_membership so preview exercises
        the exact same validation create does.
        """
        if kind not in SUPPORTED_UNIVERSE_KINDS:
            raise UniverseValidationError(
                f"kind must be one of {', '.join(SUPPORTED_UNIVERSE_KINDS)}, got {kind!r}"
            )
        if source_config is None:
            source_config = {}
        if not isinstance(source_config, dict):
            raise UniverseValidationError("source_config must be an object")
        config = dict(source_config)

        if kind == "explicit":
            config["members"] = self._normalize_explicit_members(config.get("members"))
        elif kind == "index":
            config["source_list"] = self._normalize_index_source_list(config.get("source_list"))
        elif kind == "screener":
            workflow_ref = str(config.get("workflow") or "").strip()
            if not workflow_ref:
                raise UniverseValidationError(
                    "screener universes require source_config.workflow: the "
                    "owning screener workflow's name (owner-scoped)"
                )
            top_n = config.get("top_n")
            if top_n is not None and (
                isinstance(top_n, bool) or not isinstance(top_n, int) or not (1 <= top_n <= 1000)
            ):
                raise UniverseValidationError("source_config.top_n must be an integer in [1, 1000]")
            limit = config.get("freshness_limit_s")
            if limit is not None and (
                isinstance(limit, bool) or not isinstance(limit, int) or not (300 <= limit <= 30 * 24 * 3600)
            ):
                raise UniverseValidationError(
                    "source_config.freshness_limit_s must be an integer in [300, 2592000]"
                )
        else:  # portfolio
            if self._portfolio_provider is None:
                raise UniverseValidationError(
                    "portfolio universes require a portfolio provider to be configured"
                )
        return config

    # -- serialization -------------------------------------------------------

    @staticmethod
    def _universe_dict(universe: Universe) -> Dict[str, Any]:
        return {
            "universe_id": str(universe.id),
            "owner_id": str(universe.owner_id),
            "name": str(universe.name),
            "kind": str(universe.kind),
            "source_config": dict(universe.source_config or {}),
            "enabled": bool(universe.enabled),
            "created_at": _iso(universe.created_at),
            "updated_at": _iso(universe.updated_at),
        }

    @staticmethod
    def _revision_dict(revision: UniverseRevision) -> Dict[str, Any]:
        return {
            "revision_id": str(revision.id),
            "universe_id": str(revision.universe_id),
            "revision": int(revision.revision),
            "members": list(revision.members or []),
            "member_count": int(revision.member_count or 0),
            "source_generation": (
                str(revision.source_generation) if revision.source_generation else None
            ),
            "coverage": dict(revision.coverage or {}),
            "resolved_at": _iso(revision.resolved_at),
            "created_at": _iso(revision.created_at),
        }

    @staticmethod
    def _owned_universe(session: Session, owner_id: str, name: str) -> Universe:
        universe = session.execute(
            select(Universe).where(Universe.owner_id == owner_id, Universe.name == name)
        ).scalar_one_or_none()
        if universe is None:
            raise UniverseNotFoundError(
                f"universe {name!r} not found for owner {owner_id!r}"
            )
        return universe

    # -- CRUD ----------------------------------------------------------------

    def create_universe(
        self,
        owner_id: str,
        name: str,
        kind: str,
        source_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create a universe; duplicate (owner_id, name) raises
        :class:`UniverseExistsError` — creation is deliberately NOT
        idempotent."""
        owner = str(owner_id or "").strip()
        if not owner:
            raise UniverseValidationError("owner_id is required")
        universe_name = str(name or "").strip()
        if not universe_name:
            raise UniverseValidationError("name is required")
        if len(universe_name) > _NAME_MAX_LENGTH:
            raise UniverseValidationError("name must be at most 255 characters")
        if len(owner) > _NAME_MAX_LENGTH:
            raise UniverseValidationError("owner_id must be at most 255 characters")
        universe_kind = str(kind or "").strip().lower()
        validated_config = self._validated_source_config(universe_kind, source_config)

        session = self.session_factory()
        try:
            existing = session.execute(
                select(Universe.id).where(
                    Universe.owner_id == owner, Universe.name == universe_name
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise UniverseExistsError(
                    f"universe {universe_name!r} already exists for owner {owner!r}"
                )
            if universe_kind == "screener":
                # Fail fast at authoring: reject a screener source whose
                # dependency chain loops back to this universe (including a
                # direct reference to the universe being created).
                from sqlalchemy import or_ as _or

                from backend.workflows.repository import Workflow

                source_ref = str(validated_config.get("workflow") or "").strip()
                source_workflow = session.execute(
                    select(Workflow).where(
                        Workflow.owner_id == owner,
                        _or(Workflow.name == source_ref, Workflow.id == source_ref),
                    )
                ).scalar_one_or_none()
                if source_workflow is not None:
                    self._assert_no_screener_universe_cycle(
                        session, owner, source_workflow, origin_name=universe_name
                    )
            row = Universe(
                owner_id=owner,
                name=universe_name,
                kind=universe_kind,
                source_config=validated_config,
            )
            session.add(row)
            session.commit()
            return self._universe_dict(row)
        except IntegrityError as exc:
            session.rollback()
            raise UniverseExistsError(
                f"universe {universe_name!r} already exists for owner {owner!r}"
            ) from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_universe(self, owner_id: str, name: str) -> Dict[str, Any]:
        session = self.session_factory()
        try:
            return self._universe_dict(self._owned_universe(session, owner_id, name))
        finally:
            session.close()

    def list_universes(self, owner_id: str) -> List[Dict[str, Any]]:
        session = self.session_factory()
        try:
            rows = session.execute(
                select(Universe)
                .where(Universe.owner_id == owner_id)
                .order_by(Universe.name.asc(), Universe.id.asc())
            ).scalars().all()
            return [self._universe_dict(row) for row in rows]
        finally:
            session.close()

    # -- membership resolution -----------------------------------------------

    def _candidate_members(
        self,
        session: Session,
        owner_id: str,
        kind: str,
        config: Dict[str, Any],
        now: datetime,
        *,
        origin_name: Optional[str] = None,
    ) -> Tuple[List[str], Optional[Dict[str, Any]]]:
        """Raw (pre-catalog) member candidates plus source freshness info.

        Returns ``(candidates, source_freshness)``; candidates are sorted and
        deduplicated. Unavailable sources raise UniverseSourceUnavailable.
        """
        if kind == "explicit":
            return list(config.get("members") or []), None

        if kind == "index":
            source_list = str(config.get("source_list") or "")
            loader = self._index_constituents_loader or self._default_index_loader
            try:
                raw = loader(source_list)
            except UniverseSourceUnavailable:
                raise
            except Exception as exc:
                raise UniverseSourceUnavailable(
                    f"index source list {source_list!r} unavailable: {exc}"
                ) from exc
            if not raw:
                raise UniverseSourceUnavailable(
                    f"index source list {source_list!r} has no constituents"
                )
            try:
                candidates = sorted({_qualify_symbol(item) for item in raw})
            except UniverseValidationError as exc:
                raise UniverseSourceUnavailable(
                    f"index source list {source_list!r} produced unusable data: {exc}"
                ) from exc
            return candidates, self._index_source_freshness(session, source_list)

        if kind == "screener":
            return self._screener_candidates(
                session, owner_id, config, now, origin_name=origin_name
            )

        # portfolio: owner-scoped, read-only, provider injected.
        provider = self._portfolio_provider
        if provider is None:  # defensive; creation already rejects this
            raise UniverseSourceUnavailable("no portfolio provider configured")
        try:
            raw = provider(owner_id, dict(config or {}))
        except UniverseSourceUnavailable:
            raise
        except Exception as exc:
            raise UniverseSourceUnavailable(
                f"portfolio source unavailable for owner {owner_id!r}: {exc}"
            ) from exc
        try:
            candidates = sorted({normalize_public_key(item) for item in (raw or [])})
        except UniverseValidationError as exc:
            raise UniverseSourceUnavailable(
                f"portfolio source produced unusable data: {exc}"
            ) from exc
        return candidates, {
            "owner_id": owner_id,
            "last_refreshed_at": _iso(now),
        }

    def _screener_candidates(
        self,
        session: Session,
        owner_id: str,
        config: Dict[str, Any],
        now: datetime,
        *,
        origin_name: Optional[str] = None,
    ) -> Tuple[List[str], Optional[Dict[str, Any]]]:
        """Members from the owning screener workflow's latest COMPLETE run.

        Ownership: the source workflow must belong to the same owner.
        Freshness: the run's scheduled_for must be within
        ``freshness_limit_s`` (default 3d) or the source is unavailable —
        dependent alerts then see degraded universes instead of stale
        membership silently going stale (E-18).
        Cycle prevention: the source workflow's document must not reference
        a screener-sourced universe that resolves back to it (bounded walk).
        """
        from sqlalchemy import or_ as _or

        from backend.workflows.repository import Workflow, WorkflowRevision
        from backend.workflows.screener_repository import ScreenerRun, ScreenerRunMember

        workflow_ref = str(config.get("workflow") or "").strip()
        top_n = config.get("top_n")
        limit_s = int(config.get("freshness_limit_s") or 3 * 24 * 3600)
        workflow = session.execute(
            select(Workflow).where(
                Workflow.owner_id == owner_id,
                _or(Workflow.name == workflow_ref, Workflow.id == workflow_ref),
            )
        ).scalar_one_or_none()
        if workflow is None:
            raise UniverseSourceUnavailable(
                f"screener workflow {workflow_ref!r} not found for this owner"
            )
        self._assert_no_screener_universe_cycle(
            session, owner_id, workflow, origin_name=origin_name
        )
        run = session.execute(
            select(ScreenerRun)
            .where(
                ScreenerRun.owner_id == owner_id,
                ScreenerRun.workflow_id == workflow.id,
                ScreenerRun.status == "complete",
            )
            .order_by(ScreenerRun.scheduled_for.desc())
            .limit(1)
        ).scalar_one_or_none()
        if run is None:
            raise UniverseSourceUnavailable(
                f"screener workflow {workflow_ref!r} has no complete run yet"
            )
        scheduled = run.scheduled_for
        if scheduled is not None and scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        if scheduled is not None and (now - scheduled) > timedelta(seconds=limit_s):
            raise UniverseSourceUnavailable(
                f"screener workflow {workflow_ref!r} result is stale: last "
                f"complete run {scheduled.isoformat()} exceeds the "
                f"freshness limit ({limit_s}s); dependent alerts stay silent "
                "until a fresh complete run"
            )
        rows = session.execute(
            select(ScreenerRunMember).where(
                ScreenerRunMember.run_id == run.id,
                ScreenerRunMember.passed.is_(True),
            )
        ).scalars().all()
        members = [
            row.instrument_key
            for row in rows
            if top_n is None or (row.rank is not None and int(row.rank) <= int(top_n))
        ]
        freshness = {
            "source": "screener",
            "source_workflow_id": str(workflow.id),
            "source_run_id": str(run.id),
            "source_run_scheduled_for": _iso(scheduled),
            "source_run_universe_revision": run.universe_revision,
        }
        return sorted(set(members)), freshness

    def _assert_no_screener_universe_cycle(
        self,
        session: Session,
        owner_id: str,
        source_workflow,
        depth: int = 0,
        origin_name: Optional[str] = None,
    ) -> None:
        """Reject U(source=W) when W's document references a screener
        universe that (transitively) sources W — bounded, owner-scoped.

        ``origin_name`` is the universe currently being created/resolved: a
        document referencing it directly closes a cycle even though its row
        may not exist yet at creation time."""
        from backend.workflows.repository import Workflow, WorkflowRevision
        from sqlalchemy import or_ as _or

        if depth > 8:
            raise UniverseValidationError(
                "screener universe dependency chain exceeds the maximum depth"
            )
        revision = session.execute(
            select(WorkflowRevision)
            .where(
                WorkflowRevision.workflow_id == source_workflow.id,
                WorkflowRevision.status == "active",
            )
            .limit(1)
        ).scalar_one_or_none()
        document = (revision.document or {}) if revision is not None else {}
        universe_expr = document.get("universe") or {}

        def _universe_ref_name(ref: Any) -> Optional[str]:
            # refs are stored either typed ({kind, name}) or as shorthand
            # ({universe: name}); index refs cannot close a universe cycle.
            if not isinstance(ref, dict):
                return None
            kind = str(ref.get("kind") or "")
            name = str(ref.get("name") or "")
            if kind in ("universe", "watchlist") and name:
                return name
            for key in ("universe", "watchlist"):
                if key in ref and isinstance(ref[key], str) and ref[key]:
                    return ref[key]
            return None

        ref_names = [
            name
            for name in (
                _universe_ref_name(ref)
                for ref in (universe_expr.get("union") or [])
                + (universe_expr.get("intersect") or [])
                + (universe_expr.get("exclude") or [])
            )
            if name
        ]
        for name in ref_names:
            if origin_name is not None and name == origin_name:
                raise UniverseValidationError(
                    f"dependency cycle: screener workflow "
                    f"{source_workflow.name!r} references universe {name!r}, "
                    "which (transitively) consumes this workflow's own results"
                )
            universe = session.execute(
                select(Universe).where(Universe.owner_id == owner_id, Universe.name == name)
            ).scalar_one_or_none()
            if universe is None or str(universe.kind) != "screener":
                continue
            next_ref = str((universe.source_config or {}).get("workflow") or "").strip()
            if not next_ref:
                continue
            next_workflow = session.execute(
                select(Workflow).where(
                    Workflow.owner_id == owner_id,
                    _or(Workflow.name == next_ref, Workflow.id == next_ref),
                )
            ).scalar_one_or_none()
            if next_workflow is None:
                continue
            if str(next_workflow.id) == str(source_workflow.id):
                raise UniverseValidationError(
                    f"dependency cycle: screener workflow "
                    f"{source_workflow.name!r} consumes its own screener results "
                    "through universe "
                    f"{name!r}"
                )
            self._assert_no_screener_universe_cycle(
                session, owner_id, next_workflow, depth + 1, origin_name=origin_name
            )

    def _index_source_freshness(
        self, session: Session, source_list: str
    ) -> Dict[str, Any]:
        """Best-effort freshness from index_refresh_state / ticker rows.

        Postgres column names verified against migration
        20260829_000007 (``index_refresh_state.last_constituent_refresh_at``,
        ``kite_ticker_tickers.last_refreshed_at``). Missing tables degrade to
        None rather than failing an otherwise-valid resolve.
        """
        state_at: Optional[Any] = None
        rows_at: Optional[Any] = None
        try:
            state_table = _qualified_table(session, "index_refresh_state")
            state_at = session.execute(
                text(
                    f"SELECT MAX(last_constituent_refresh_at) FROM {state_table} "
                    "WHERE source_list = :source"
                ),
                {"source": source_list},
            ).scalar()
        except Exception:
            state_at = None
        try:
            rows_table = _qualified_table(session, "kite_ticker_tickers")
            rows_at = session.execute(
                text(
                    f"SELECT MAX(last_refreshed_at) FROM {rows_table} "
                    "WHERE source_list = :source"
                ),
                {"source": source_list},
            ).scalar()
        except Exception:
            rows_at = None

        stamps = [
            _as_utc(parsed)
            for parsed in (_parse_dt(state_at), _parse_dt(rows_at))
            if parsed is not None
        ]
        return {
            "source_list": source_list,
            "last_constituent_refresh_at": _iso(state_at),
            "constituents_last_refreshed_at": _iso(rows_at),
            "last_refreshed_at": _iso(max(stamps)) if stamps else None,
        }

    def _resolve_through_catalog(
        self, candidates: Sequence[str], catalog: Any
    ) -> Tuple[List[str], List[Dict[str, str]], Optional[str]]:
        """Resolve candidates through the catalog.

        Members whose lifecycle is not ``active`` land in ``rejected`` with a
        reason (never silently dropped, never fatal). Catalog-wide failures
        (``CatalogUnavailableError``) propagate — the caller must not persist
        a revision in that case.
        """
        from backend.broker_api.instruments.catalog import (
            AmbiguousInstrumentError,
            InstrumentNotFoundError,
        )

        members: List[str] = []
        rejected: List[Dict[str, str]] = []
        source_generation: Optional[str] = None
        for key in candidates:
            try:
                descriptor = catalog.resolve_public_key(key)
            except InstrumentNotFoundError:
                rejected.append({"key": key, "reason": "not_found"})
                continue
            except AmbiguousInstrumentError:
                rejected.append({"key": key, "reason": "ambiguous"})
                continue
            lifecycle = str(getattr(descriptor, "lifecycle_status", "") or "").lower()
            if lifecycle != "active":
                rejected.append({"key": key, "reason": lifecycle or "inactive"})
                continue
            members.append(str(getattr(descriptor, "public_key", "") or key).upper())
            generation = getattr(descriptor, "catalog_generation", None)
            if generation:
                source_generation = str(generation)
        return sorted(set(members)), rejected, (source_generation or None)

    def preview_membership(
        self,
        owner_id: str,
        kind: str,
        source_config: Dict[str, Any],
        *,
        catalog_session_factory: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Resolve would-be membership WITHOUT any persistent side effect."""
        owner = str(owner_id or "").strip()
        if not owner:
            raise UniverseValidationError("owner_id is required")
        universe_kind = str(kind or "").strip().lower()
        config = self._validated_source_config(universe_kind, source_config)

        now = _utcnow()
        session = self.session_factory()
        try:
            candidates, freshness = self._candidate_members(
                session, owner, universe_kind, config, now
            )
        finally:
            session.close()
        catalog = self._catalog_for(catalog_session_factory)
        members, rejected, source_generation = self._resolve_through_catalog(
            candidates, catalog
        )
        coverage = self._coverage(
            universe_kind, len(members), rejected, candidates, now, freshness
        )
        return {
            "kind": universe_kind,
            "members": members,
            "rejected": rejected,
            "source_generation": source_generation,
            "coverage": coverage,
        }

    def resolve_membership(
        self,
        owner_id: str,
        name: str,
        *,
        catalog_session_factory: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Resolve current membership, persist one revision, return payload."""
        now = _utcnow()
        session = self.session_factory()
        try:
            universe = self._owned_universe(session, owner_id, name)
            kind = str(universe.kind)
            config = dict(universe.source_config or {})
            candidates, freshness = self._candidate_members(
                session, owner_id, kind, config, now, origin_name=str(name)
            )
            catalog = self._catalog_for(catalog_session_factory)
            members, rejected, source_generation = self._resolve_through_catalog(
                candidates, catalog
            )

            # Serialize per universe inside the write transaction: advisory
            # xact lock keyed on 'universe:owner/name' + FOR UPDATE on the
            # universe row (Postgres). The MAX(revision)+1 read, the insert,
            # and the lock share this one transaction; the unique constraint
            # is the final backstop. SQLite (tests) relies on the
            # single-writer engine plus the constraint.
            if _is_postgres(session):
                session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtext('universe:' || :lock_key))"
                    ),
                    {"lock_key": f"{owner_id}/{name}"},
                )
                session.execute(
                    select(Universe.id).where(Universe.id == universe.id).with_for_update()
                )
            latest = session.execute(
                select(func.max(UniverseRevision.revision)).where(
                    UniverseRevision.universe_id == universe.id
                )
            ).scalar()
            next_revision = int(latest or 0) + 1

            coverage = self._coverage(
                kind, len(members), rejected, candidates, now, freshness
            )
            row = UniverseRevision(
                universe_id=universe.id,
                revision=next_revision,
                expression=config,
                members=list(members),
                member_count=len(members),
                source_generation=source_generation,
                coverage=coverage,
                resolved_at=now,
                created_at=now,
            )
            session.add(row)
            session.commit()
            return {
                "universe_id": str(universe.id),
                "owner_id": str(universe.owner_id),
                "name": str(universe.name),
                "kind": kind,
                "revision": int(next_revision),
                "members": list(members),
                "rejected": list(rejected),
                "source_generation": source_generation,
                "coverage": coverage,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _coverage(
        kind: str,
        resolved_count: int,
        rejected: List[Dict[str, str]],
        candidates: Sequence[str],
        now: datetime,
        freshness: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "resolved": int(resolved_count),
            "rejected": len(rejected),
            "candidates": len(candidates),
            "source": kind,
            "resolved_at": _iso(now),
            # explicit: null. index: max last_refreshed_at from
            # index_refresh_state / constituent rows.
            "source_freshness": freshness,
        }

    # -- revision history ------------------------------------------------------

    def latest_revision(self, owner_id: str, name: str) -> Optional[Dict[str, Any]]:
        session = self.session_factory()
        try:
            universe = self._owned_universe(session, owner_id, name)
            row = session.execute(
                select(UniverseRevision)
                .where(UniverseRevision.universe_id == universe.id)
                .order_by(
                    UniverseRevision.resolved_at.desc(), UniverseRevision.revision.desc()
                )
                .limit(1)
            ).scalar_one_or_none()
            return self._revision_dict(row) if row is not None else None
        finally:
            session.close()

    def revision_at(
        self, owner_id: str, name: str, effective_before: datetime
    ) -> Optional[Dict[str, Any]]:
        """Latest revision whose resolved_at <= effective_before (naive
        datetimes are treated as UTC)."""
        session = self.session_factory()
        try:
            universe = self._owned_universe(session, owner_id, name)
            cutoff = _as_utc(effective_before)
            rows = session.execute(
                select(UniverseRevision)
                .where(UniverseRevision.universe_id == universe.id)
                .order_by(
                    UniverseRevision.resolved_at.desc(), UniverseRevision.revision.desc()
                )
            ).scalars().all()
            for row in rows:
                resolved = row.resolved_at
                if resolved is not None and _as_utc(resolved) <= cutoff:
                    return self._revision_dict(row)
            return None
        finally:
            session.close()

    def list_revisions(
        self, owner_id: str, name: str, *, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Revision history, newest first, bounded by ``limit``."""
        safe_limit = max(1, min(int(limit or 50), 500))
        session = self.session_factory()
        try:
            universe = self._owned_universe(session, owner_id, name)
            rows = session.execute(
                select(UniverseRevision)
                .where(UniverseRevision.universe_id == universe.id)
                .order_by(
                    UniverseRevision.resolved_at.desc(), UniverseRevision.revision.desc()
                )
                .limit(safe_limit)
            ).scalars().all()
            return [self._revision_dict(row) for row in rows]
        finally:
            session.close()
