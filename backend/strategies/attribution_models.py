"""ORM tables for durable strategy attribution (G1).

Defined on the shared ``Base`` from :mod:`backend.workflows.repository` so a
single ``Base.metadata.create_all`` (used throughout the test suite) registers
every platform table, and so the Postgres migration
``20260917_000025_durable_strategy_attribution`` mirrors exactly these tables.

The ORM uses ``JSON`` and ``Uuid(as_uuid=False)`` (portable to the SQLite test
database). The Postgres migration and ``schema.sql`` use ``JSONB`` and native
``UUID`` for the same columns — the established pattern in this repo (see
``backend/strategies/models.py``).

Identity relations are **composite foreign keys**, so integrity is enforced by
the database rather than by repository discipline:

- a binding's ``(strategy_id, owner_id, account_id)`` must equal the canonical
  strategy's ``(id, owner_id, account_scope)`` — owner/account mismatch is
  impossible, and the strategy cannot be deleted while bindings exist;
- the projection and its state row must agree with the canonical strategy's
  account via ``(strategy_id, account_id)``.

Two foreign keys that exist in the migration and ``schema.sql`` are deliberately
**not** declared here: ``strategy_run_bindings.(strategy_run_id,
execution_environment) -> algo_worker_runs (strategy_run_id, execution_mode)``
and ``worker_token_strategy_grants.token_id -> algo_worker_tokens.token_id``.
Those platform tables have no ORM model anywhere in this codebase (they are Core
``text()`` SQL, always ``public.``-qualified), so declaring the FKs on the shared
metadata would make ``Base.metadata.create_all`` raise
``NoReferencedTableError`` in the SQLite test path. The database — not the ORM
mirror — remains the enforcement point for both, and the migration and
``schema.sql`` carry them verbatim.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)

from backend.workflows.repository import Base

#: Binding provenance vocabulary (mirrors ``ck_binding_source``).
BINDING_SOURCES = ("hosted_job", "external_run_create", "audited_mapping", "legacy_compat")

#: Execution book vocabulary (mirrors ``ck_binding_environment`` / ``ck_spp_environment``).
EXECUTION_ENVIRONMENTS = ("live", "paper", "dry_run")

#: Canonical product status vocabulary (mirrors ``ck_strategies_status``).
STRATEGY_STATUSES = ("active", "disabled", "archived")


class Strategy(Base):
    """The single canonical, owner-controlled strategy identity.

    One user-facing Strategy product; hosted Python and external workers are
    compute adapters over this row, never separate strategies. Job, attempt, run
    and token ids are never strategy identity.

    ``journal_template_id`` links the analytics-only journal template identity
    (which has no owner/account lifecycle) and is never owner authority.
    """

    __tablename__ = "strategies"

    id = Column(Text, primary_key=True)
    #: The app owner. Server-derived; never actor-supplied.
    owner_id = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    #: The broker account this strategy is bound to (e.g. ``kite:AB1234``).
    account_scope = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default="active")
    journal_template_id = Column(Uuid(as_uuid=False), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_strategies_owner_name"),
        # Composite targets for database-enforced integrity: bindings reference
        # (id, owner_id, account_scope); projection and state reference
        # (id, account_scope) for account agreement.
        UniqueConstraint("id", "owner_id", "account_scope", name="uq_strategies_id_owner_account"),
        UniqueConstraint("id", "account_scope", name="uq_strategies_id_account"),
        CheckConstraint("status IN ('active', 'disabled', 'archived')", name="ck_strategies_status"),
    )


class ExternalStrategyAdapter(Base):
    """The external compute adapter: configuration for a worker-hosted strategy.

    Created and configured only by the cookie-authenticated account-owner API.
    ``strategy_id`` is ``ON DELETE RESTRICT`` so an adapter cannot outlive the
    canonical strategy it computes for.
    """

    __tablename__ = "external_strategy_adapters"

    id = Column(Text, primary_key=True)
    strategy_id = Column(
        Text, ForeignKey("strategies.id", ondelete="RESTRICT"), nullable=False
    )
    status = Column(Text, nullable=False, server_default="active")
    config_json = Column(JSON, nullable=False, server_default=text("'{}'"))
    created_by = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_external_adapters_status"),
        Index("idx_external_adapters_strategy", "strategy_id"),
    )


class WorkerTokenStrategyGrant(Base):
    """Owner-issued authorization for a worker token to act for a strategy.

    A worker token is a credential, never a strategy owner: this table is the
    only thing that lets a token's runs bind to a canonical strategy. Revocation
    removes authority, not ownership (``revoked_at`` is set; history is kept).
    """

    __tablename__ = "worker_token_strategy_grants"

    token_id = Column(Text, primary_key=True)
    strategy_id = Column(Text, primary_key=True)
    granted_by = Column(Text, nullable=False)
    granted_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("idx_grants_strategy", "strategy_id"),)


class StrategyRunBinding(Base):
    """Immutable run-to-strategy binding. **Insert-only, enforced by a trigger.**

    A ``BEFORE UPDATE OR DELETE`` trigger on ``public.strategy_run_bindings``
    raises for every mutation, and the row is ``ON DELETE RESTRICT`` to both the
    run and the canonical strategy: attribution history outlives operational run
    rows, and deleting a strategy with attributed history is refused (archive
    instead). Audited legacy mapping appends NEW rows; a mistaken binding
    requires the dedicated ownership-transfer protocol (outside G1).

    ``execution_environment`` is the immutable book dimension
    (``live``/``paper``/``dry_run``) and must equal the run's persisted
    ``execution_mode`` — enforced by a composite FK in the database.
    """

    __tablename__ = "strategy_run_bindings"

    strategy_run_id = Column(Text, primary_key=True)
    strategy_id = Column(Text, nullable=False)
    owner_id = Column(Text, nullable=False)
    account_id = Column(Text, nullable=False)
    execution_environment = Column(Text, nullable=False)
    bound_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    bound_by = Column(Text, nullable=False)
    binding_source = Column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "binding_source IN ('hosted_job', 'external_run_create', 'audited_mapping', 'legacy_compat')",
            name="ck_binding_source",
        ),
        CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')",
            name="ck_binding_environment",
        ),
        # Owner/account integrity: the binding's owner and account must equal
        # the canonical strategy's (composite FK — mismatch is impossible).
        ForeignKeyConstraint(
            ["strategy_id", "owner_id", "account_id"],
            ["strategies.id", "strategies.owner_id", "strategies.account_scope"],
            ondelete="RESTRICT",
        ),
        Index("idx_strategy_run_bindings_strategy", "strategy_id"),
        Index(
            "idx_strategy_run_bindings_account_env",
            "account_id",
            "strategy_id",
            "execution_environment",
        ),
    )


class StrategyPositionProjection(Base):
    """The rebuildable strategy position book for one execution environment.

    Keyed by ``(account_id, strategy_id, execution_environment, identity_kind,
    identity_key, product)``. ``identity_kind='canonical'`` rows carry a
    canonical instrument id; ``identity_kind='raw'`` rows are explicit
    unresolved facts whose ``identity_key`` carries a catalog-evidence era, so
    facts from distinct known eras never merge and unresolved exposure is never
    silently attributed.
    """

    __tablename__ = "strategy_position_projection"

    account_id = Column(Text, primary_key=True)
    strategy_id = Column(Text, primary_key=True)
    execution_environment = Column(Text, primary_key=True)
    identity_kind = Column(Text, primary_key=True)
    identity_key = Column(Text, primary_key=True)
    product = Column(Text, primary_key=True)
    canonical_instrument_id = Column(Uuid(as_uuid=False), nullable=True)
    instrument_token = Column(BigInteger, nullable=False)
    exchange = Column(Text, nullable=False)
    tradingsymbol = Column(Text, nullable=False)
    net_quantity = Column(Integer, nullable=False)
    unresolved_reason = Column(Text, nullable=True)
    projection_version = Column(BigInteger, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')", name="ck_spp_environment"
        ),
        CheckConstraint("identity_kind IN ('canonical', 'raw')", name="ck_spp_identity_kind"),
        CheckConstraint(
            "(identity_kind = 'canonical' AND canonical_instrument_id IS NOT NULL) "
            "OR (identity_kind = 'raw' AND canonical_instrument_id IS NULL)",
            name="ck_spp_identity_consistency",
        ),
        # Account agreement is database-enforced: a projection row's account
        # must equal the canonical strategy's account.
        ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            ondelete="RESTRICT",
        ),
        Index("idx_spp_strategy", "account_id", "strategy_id", "execution_environment"),
    )


class StrategyProjectionState(Base):
    """Publication state per ``(account, strategy, execution_environment)``.

    ``projection_version`` advances on every non-idempotent publication and
    ``content_sha256`` is kept **only for idempotence, never chronology** — a
    content hash is not evidence that one snapshot is newer than another.
    """

    __tablename__ = "strategy_projection_state"

    account_id = Column(Text, primary_key=True)
    strategy_id = Column(Text, primary_key=True)
    execution_environment = Column(Text, primary_key=True)
    projection_version = Column(BigInteger, nullable=False, server_default="0")
    content_sha256 = Column(Text, nullable=True)
    last_rebuild_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')", name="ck_sps_environment"
        ),
        # Account agreement is database-enforced, not merely a strategy_id FK.
        ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            ondelete="RESTRICT",
        ),
    )
