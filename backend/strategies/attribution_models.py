"""ORM tables for durable strategy attribution (G1).

Defined on the shared ``Base`` from :mod:`backend.workflows.repository` so a
single ``Base.metadata.create_all`` (used throughout the test suite) registers
every platform table, and so the Postgres migration
``20260917_000025_durable_strategy_attribution`` mirrors exactly these tables.

The ORM uses ``JSON`` and ``Text`` for the UUID-bearing columns (portable to
the SQLite test database). The Postgres migration and ``schema.sql`` use
``JSONB`` and native ``UUID`` for the same columns — the established pattern in
this repo (see ``backend/strategies/models.py``). Readers treat the identifier
as an opaque string, so a native ``UUID`` returned by PostgreSQL is handled by
the same code path as the SQLite text form.

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
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Text,
    UniqueConstraint,
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
    journal_template_id = Column(Text, nullable=True)
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
    canonical_instrument_id = Column(Text, nullable=True)
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


# ---------------------------------------------------------------------------
# Account truth (Phase 2 / G2+G3+G4)
# ---------------------------------------------------------------------------


class BrokerTradeFact(Base):
    """Account-wide ingested fill fact, keyed by durable broker identity.

    Insert-only (a BEFORE UPDATE OR DELETE trigger in the migration raises):
    a bad ingest generation is corrected by ingesting the missing truth, never
    by editing or deleting history. Deduplicated by ``(account_id, trade_id)``,
    so re-ingesting a page of trades is a no-op.

    A fact is *tracked* when an execution link claims its order, and *manual*
    (unattributed) otherwise — that distinction is derived, never stored here.
    """

    __tablename__ = "broker_trade_facts"

    fact_id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    trade_id = Column(Text, nullable=False)
    broker_order_id = Column(Text, nullable=False)
    instrument_token = Column(BigInteger, nullable=False)
    exchange = Column(Text, nullable=False)
    tradingsymbol = Column(Text, nullable=False)
    product = Column(Text, nullable=False)
    transaction_type = Column(Text, nullable=False)
    quantity = Column(Integer, nullable=False)
    fill_price = Column(Float, nullable=True)
    trade_timestamp = Column(DateTime(timezone=True), nullable=True)
    ingest_generation = Column(BigInteger, nullable=False)
    ingested_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("account_id", "trade_id", name="uq_broker_trade_facts_account_trade"),
        CheckConstraint("transaction_type IN ('BUY', 'SELL')", name="ck_btf_transaction_type"),
        CheckConstraint("quantity > 0", name="ck_btf_quantity"),
        Index(
            "idx_btf_account_coord",
            "account_id",
            "instrument_token",
            "exchange",
            "tradingsymbol",
            "product",
        ),
    )


class AccountIngestState(Base):
    """Per-account ingest cursor/generation.

    Distinguishes "we have not looked yet" from "we looked and the mismatch is
    real", which is what makes ``pending_ingest`` honest rather than a guess.
    """

    __tablename__ = "account_ingest_state"

    account_id = Column(Text, primary_key=True)
    last_orders_fetch_at = Column(DateTime(timezone=True), nullable=True)
    last_complete_ingest_at = Column(DateTime(timezone=True), nullable=True)
    ingest_generation = Column(BigInteger, nullable=False, server_default="0")
    status = Column(Text, nullable=False, server_default="idle")
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('idle', 'refreshing', 'stale')", name="ck_ais_status"),
    )


class StrategyReconciliationState(Base):
    """Persisted per-coordinate divergence classification.

    ``residual_quantity`` is ``broker - attributed - manual``: zero when
    aligned. Both ``pending_ingest`` and ``unexplained`` freeze new exposure on
    the coordinate; only persistent ``unexplained`` escalates to the owner, and
    ``owner_notified_at`` is written at most once.
    """

    __tablename__ = "strategy_reconciliation_state"

    account_id = Column(Text, primary_key=True)
    instrument_token = Column(BigInteger, primary_key=True)
    exchange = Column(Text, primary_key=True)
    tradingsymbol = Column(Text, primary_key=True)
    product = Column(Text, primary_key=True)
    divergence_class = Column(Text, nullable=False)
    broker_quantity = Column(BigInteger, nullable=False)
    attributed_quantity = Column(BigInteger, nullable=False)
    manual_quantity = Column(BigInteger, nullable=False)
    residual_quantity = Column(BigInteger, nullable=False)
    refresh_attempts = Column(Integer, nullable=False, server_default="0")
    last_checked_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    owner_notified_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "divergence_class IN ('aligned', 'pending_ingest', 'unexplained')",
            name="ck_srs_divergence_class",
        ),
    )


class StrategyAttributionAdjustment(Base):
    """Append-only owner reclassification header. Trigger-immutable."""

    __tablename__ = "strategy_attribution_adjustments"

    adjustment_id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    adjustment_kind = Column(Text, nullable=False)
    reason_code = Column(Text, nullable=False)
    created_by = Column(Text, nullable=False)
    evidence = Column(JSON, nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "adjustment_kind IN ('owner_reclassification')", name="ck_saa_adjustment_kind"
        ),
        Index("idx_saa_account", "account_id", "created_at"),
    )


class StrategyAttributionAdjustmentLine(Base):
    """One signed quantity move between the manual residual and a strategy book.

    ``quantity_delta`` is the signed quantity **credited to the strategy**: a
    claimed ``-10`` fill is ``delta = -10``, which lowers the strategy's book by
    10 and raises the manual residual by 10 toward zero. The manual book is the
    implicit counterparty, so a line is a balanced transfer by construction; a
    correction is a new opposite-sign line referencing the original in
    ``evidence``.

    Lines are **live-book facts in V1** — there is deliberately no
    ``execution_environment`` column, and the fold assigns ``'live'``.
    """

    __tablename__ = "strategy_attribution_adjustment_lines"

    adjustment_id = Column(Text, primary_key=True)
    line_no = Column(Integer, primary_key=True)
    strategy_id = Column(Text, nullable=False)
    owner_id = Column(Text, nullable=False)
    account_id = Column(Text, nullable=False)
    instrument_token = Column(BigInteger, nullable=False)
    exchange = Column(Text, nullable=False)
    tradingsymbol = Column(Text, nullable=False)
    product = Column(Text, nullable=False)
    quantity_delta = Column(Integer, nullable=False)
    effective_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("quantity_delta <> 0", name="ck_saal_quantity_delta"),
        # Owner/account integrity mirrors strategy_run_bindings exactly.
        ForeignKeyConstraint(
            ["strategy_id", "owner_id", "account_id"],
            ["strategies.id", "strategies.owner_id", "strategies.account_scope"],
            ondelete="RESTRICT",
        ),
        Index(
            "idx_saal_strategy_coord",
            "strategy_id",
            "instrument_token",
            "exchange",
            "tradingsymbol",
            "product",
        ),
    )


class StrategyProposal(Base):
    """The durable envelope of one market decision (R3 §6).

    ``UNIQUE (strategy_id, evaluation_id)`` is the whole cardinality contract:
    one evaluation identity creates at most one envelope, whose payload is stored
    verbatim beside its ``payload_sha256`` so "same evaluation, different
    decision" stays decidable after the fact. Trigger-immutable.
    """

    __tablename__ = "strategy_proposals"

    proposal_id = Column(Text, primary_key=True)
    strategy_id = Column(Text, nullable=False)
    account_id = Column(Text, nullable=False)
    evaluation_id = Column(Text, nullable=False)
    evaluation_kind = Column(Text, nullable=False)
    job_id = Column(Text, nullable=True)
    strategy_run_id = Column(Text, nullable=False)
    target_kind = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False)
    payload_sha256 = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("strategy_id", "evaluation_id", name="uq_proposals_strategy_evaluation"),
        CheckConstraint(
            "evaluation_kind IN ('scheduled_occurrence', 'run_now')",
            name="ck_proposals_evaluation_kind",
        ),
        CheckConstraint(
            # Every kind the compiler registry ships. ``target_futures`` and
            # ``option_structure`` were missing, so a valid futures or
            # option-structure submission could not be stored at all.
            "target_kind IN ('single_instrument', 'target_weights', 'intent_bundle', "
            "'target_futures', 'option_structure')",
            name="ck_proposals_target_kind",
        ),
        CheckConstraint("status IN ('received', 'validated', 'refused')", name="ck_proposals_status"),
        CheckConstraint(
            "evaluation_kind <> 'scheduled_occurrence' OR job_id IS NOT NULL",
            name="ck_proposals_scheduled_requires_job",
        ),
        ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            name="fk_proposals_strategy_canonical",
            ondelete="RESTRICT",
        ),
        Index("idx_proposals_strategy", "strategy_id", "created_at"),
    )


class StrategyPlan(Base):
    """The immutable resolved artifact: logical + resolved + the pin (R3 §7).

    Resolution happens once, against ``pinned_catalog_generation``. Invalidation
    is derived at read time (a newer generation that re-maps a pinned instrument
    or retires its record), never stored, so a plan is never rewritten when the
    catalog moves — and an unrelated generation change leaves it valid.

    ``pinned_catalog_generation`` is ``Text`` here per the portability precedent
    (the migration and ``schema.sql`` keep the native ``UUID`` and the FK: a plan
    can never point at a generation that does not exist).
    """

    __tablename__ = "strategy_plans"

    plan_id = Column(Text, primary_key=True)
    proposal_id = Column(Text, nullable=False)
    strategy_id = Column(Text, nullable=False)
    account_id = Column(Text, nullable=False)
    plan_kind = Column(Text, nullable=False)
    plan_hash = Column(Text, nullable=False)
    logical_plan = Column(JSON, nullable=False)
    resolved_plan = Column(JSON, nullable=False)
    pinned_universe_revision_id = Column(Text, nullable=True)
    pinned_member_hash = Column(Text, nullable=True)
    pinned_catalog_generation = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_plans_proposal"),
        CheckConstraint(
            "plan_kind IN ('single_instrument', 'target_weights', 'intent_bundle', "
            "'target_futures', 'option_structure')",
            name="ck_plans_plan_kind",
        ),
        CheckConstraint(
            "plan_kind <> 'target_weights' OR "
            "(pinned_universe_revision_id IS NOT NULL AND pinned_member_hash IS NOT NULL)",
            name="ck_plans_target_weights_scope",
        ),
        ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            name="fk_plans_strategy_canonical",
            ondelete="RESTRICT",
        ),
        # Declared because ``strategy_proposals`` IS in this metadata: it orders
        # the unit of work so the envelope is written before the plan that
        # references it. The FK to ``instrument_catalog_generations`` stays
        # undeclared for the same reason the platform FKs do — that table has no
        # ORM model here, so the database (migration and schema.sql) is the
        # enforcement point.
        ForeignKeyConstraint(
            ["proposal_id"],
            ["strategy_proposals.proposal_id"],
            name="fk_plans_proposal",
            ondelete="RESTRICT",
        ),
        Index("idx_plans_strategy", "strategy_id", "created_at"),
    )


class StrategyPlanOptionRun(Base):
    """The durable edge binding a frozen ``option_structure`` plan to its run.

    R3's options lane executes through the existing ``option_run_states`` engine,
    whose primary key is the option-run id. That id is deliberately **not** the
    hosted worker-run id: one hosted worker run can carry several option runs,
    and an option run outlives the plan that created it. This relation is the
    only place the two identities meet, so neither has to be overloaded.

    ``plan_id`` is unique: a plan resolves to exactly one binding, which is what
    makes a retry return the same run instead of manufacturing a second one. An
    entry plan creates at most one run (``uq_plan_option_run_entry``), while
    several exit plans may reference the same run — closing a structure is not
    one plan.

    The FK to ``strategy_plans`` IS declared here (that table is in this
    metadata). The FK to ``strategies`` is composite so owner/account mismatch is
    impossible. The FKs to ``option_run_states`` and ``algo_worker_runs`` are
    **not** declared: neither has an ORM model in this codebase, so the database
    (migration and ``schema.sql``) stays the enforcement point, per the module
    precedent.
    """

    __tablename__ = "strategy_plan_option_runs"

    plan_id = Column(Text, primary_key=True)
    option_run_id = Column(Text, nullable=False)
    worker_run_id = Column(Text, nullable=True)
    strategy_id = Column(Text, nullable=False)
    account_id = Column(Text, nullable=False)
    execution_environment = Column(Text, nullable=False)
    phase = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "phase IN ('entry', 'exit')",
            name="ck_plan_option_run_phase",
        ),
        ForeignKeyConstraint(
            ["plan_id"],
            ["strategy_plans.plan_id"],
            name="fk_plan_option_run_plan",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            name="fk_plan_option_run_strategy",
            ondelete="RESTRICT",
        ),
        Index("idx_plan_option_run_run", "option_run_id"),
        Index("idx_plan_option_run_worker", "worker_run_id"),
        Index(
            "uq_plan_option_run_entry",
            "option_run_id",
            unique=True,
            postgresql_where=text("phase = 'entry'"),
            sqlite_where=text("phase = 'entry'"),
        ),
    )


class LivePlanSubmission(Base):
    """The DURABLE claim for one live plan step (preparatory live adapter).

    A live submission cannot live in process memory: two adapter instances (or a
    restart) must not be able to dispatch the same step twice, and an outcome that
    was never confirmed must survive the process that produced it. This row is the
    claim and the outcome record:

    * ``UNIQUE (plan_id, step_no)`` makes the claim atomic - the loser of the
      insert reads the winner's row instead of dispatching;
    * ``state`` distinguishes ``pending`` (accepted, awaiting fills),
      ``uncertain`` (transport/response unknown - NEVER auto-repeated, work and
      reservation retained) and ``rejected`` (an authoritative refusal, which may
      resolve the known-unfilled residual work);
    * ``broker_order_ids``/``delta_snapshot``/``detail`` are the evidence: the
      resolved delta this step was authorised to trade and what the broker said.

    The FKs to ``strategy_plans`` and ``strategies`` are declared here (both are in
    this metadata); no live ORDER is ever inferred from this table.
    """

    __tablename__ = "live_plan_submissions"

    submission_id = Column(Text, primary_key=True)
    plan_id = Column(Text, nullable=False)
    step_no = Column(Integer, nullable=False)
    step_ref = Column(Text, nullable=False)
    strategy_id = Column(Text, nullable=False)
    account_id = Column(Text, nullable=False)
    execution_environment = Column(Text, nullable=False)
    state = Column(Text, nullable=False)
    broker_order_ids = Column(JSON, nullable=False, server_default=text("'[]'"))
    delta_snapshot = Column(JSON, nullable=False, server_default=text("'{}'"))
    detail = Column(JSON, nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("plan_id", "step_no", name="uq_live_plan_step"),
        CheckConstraint(
            "state IN ('pending', 'uncertain', 'rejected', 'no_op')",
            name="ck_live_plan_submission_state",
        ),
        ForeignKeyConstraint(
            ["plan_id"],
            ["strategy_plans.plan_id"],
            name="fk_live_plan_submission_plan",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            name="fk_live_plan_submission_strategy",
            ondelete="RESTRICT",
        ),
        Index("idx_live_plan_submission_state", "state"),
    )


class StrategyProposalJournal(Base):
    """Append-only trail of what happened to an evaluation (R3 §19).

    It duplicates no state: the envelope and the plan are the truth, the journal
    is the sequence. A correction is a NEW event; a refusal is terminal.
    """

    __tablename__ = "strategy_proposal_journal"

    id = Column(Text, primary_key=True)
    strategy_id = Column(Text, nullable=False)
    evaluation_id = Column(Text, nullable=True)
    proposal_id = Column(Text, nullable=True)
    event = Column(Text, nullable=False)
    reason_code = Column(Text, nullable=True)
    detail = Column(JSON, nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "event IN ('received', 'idempotent_retry', 'conflict', "
            "'validation_refused', 'plan_created')",
            name="ck_proposal_journal_event",
        ),
        Index("idx_proposal_journal_strategy", "strategy_id", "created_at"),
    )


class StrategyAdmissionPolicy(Base):
    """The recorded basis for every admission decision (G9).

    ``allocation_inr`` is REQUIRED for a live strategy and enforced by the
    service, not the database: a paper-only strategy may legitimately have none,
    and 0 is a real limit that must stay distinguishable from NULL ("not
    enforced"). Without a recorded allocation, capital enforcement would be
    unenforceable the moment someone asked what the limit was.
    """

    __tablename__ = "strategy_admission_policies"

    strategy_id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    allocation_inr = Column(Float, nullable=True)
    per_instrument_notional_inr = Column(Float, nullable=True)
    gross_notional_inr = Column(Float, nullable=True)
    max_open_instruments = Column(Integer, nullable=True)
    admissions_per_window = Column(Integer, nullable=True)
    admission_window_seconds = Column(Integer, nullable=True)
    daily_loss_budget_inr = Column(Float, nullable=True)
    updated_by = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "allocation_inr IS NULL OR allocation_inr >= 0", name="ck_sap_allocation_non_negative"
        ),
        CheckConstraint(
            "admissions_per_window IS NULL OR admissions_per_window > 0",
            name="ck_sap_admissions_per_window",
        ),
        ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            name="fk_sap_strategy_canonical",
            ondelete="RESTRICT",
        ),
    )


class StrategyReservation(Base):
    """A durable capacity claim against one plan (G10).

    Mutable by design — a reservation has a lifecycle — so unlike every other
    table in this campaign it carries no insert-only trigger. The append-only
    record is :class:`StrategyReservationEvent`. ``UNIQUE (plan_id)`` makes one
    plan claim capacity once, ever.
    """

    __tablename__ = "strategy_reservations"

    reservation_id = Column(Text, primary_key=True)
    plan_id = Column(Text, nullable=False)
    strategy_id = Column(Text, nullable=False)
    account_id = Column(Text, nullable=False)
    evaluation_id = Column(Text, nullable=False)
    execution_environment = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default="active")
    reserved_notional_inr = Column(Float, nullable=False)
    margin_evidence = Column(JSON, nullable=True)
    margin_as_of = Column(DateTime(timezone=True), nullable=True)
    valid_until = Column(DateTime(timezone=True), nullable=False)
    renewed_at = Column(DateTime(timezone=True), nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)
    release_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_reservations_plan"),
        CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')",
            name="ck_res_execution_environment",
        ),
        CheckConstraint(
            "status IN ('active', 'renewed', 'consumed', 'released', 'expired', "
            "'action_required')",
            name="ck_res_status",
        ),
        CheckConstraint("reserved_notional_inr >= 0", name="ck_res_notional_non_negative"),
        # Declared because both tables are in this metadata: it orders the unit of
        # work. The composite FK to ``strategies`` is declared too, matching the
        # G1 precedent; the FK to ``strategy_plans`` is what the migration and
        # schema.sql carry for the database's own enforcement.
        ForeignKeyConstraint(
            ["plan_id"],
            ["strategy_plans.plan_id"],
            name="fk_res_plan",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            name="fk_res_strategy_canonical",
            ondelete="RESTRICT",
        ),
        Index("idx_reservations_account_status", "account_id", "status"),
    )


class StrategyReservationEvent(Base):
    """Append-only record of every reservation transition. Trigger-immutable."""

    __tablename__ = "strategy_reservation_events"

    id = Column(Text, primary_key=True)
    reservation_id = Column(Text, nullable=False)
    event = Column(Text, nullable=False)
    actor_id = Column(Text, nullable=True)
    detail = Column(JSON, nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "event IN ('created', 'renewed', 'advanced', 'consumed', 'released', "
            "'expired', 'action_required', 'disposition_confirmed')",
            name="ck_res_event",
        ),
        ForeignKeyConstraint(
            ["reservation_id"],
            ["strategy_reservations.reservation_id"],
            name="fk_res_event_reservation",
            ondelete="RESTRICT",
        ),
        Index("idx_reservation_events", "reservation_id", "created_at"),
    )


class StrategyApproval(Base):
    """The owner's authorisation, bound to every structural pin (G6).

    At most one ``active`` row per plan — the partial unique index
    ``uq_approvals_plan_active`` is the real contract, so a concurrent
    double-approval cannot produce two live approvals. Superseding inserts a NEW
    row and marks the old one superseded; approvals are never rewritten.
    """

    __tablename__ = "strategy_approvals"

    approval_id = Column(Text, primary_key=True)
    plan_id = Column(Text, nullable=False)
    strategy_id = Column(Text, nullable=False)
    account_id = Column(Text, nullable=False)
    reservation_id = Column(Text, nullable=False)
    plan_hash = Column(Text, nullable=False)
    exposure_snapshot_version = Column(BigInteger, nullable=False)
    exposure_snapshot_hash = Column(Text, nullable=True)
    reconciliation_version = Column(BigInteger, nullable=False)
    catalog_generation = Column(Text, nullable=False)
    session_product_snapshot = Column(JSON, nullable=False, server_default=text("'{}'"))
    actor_id = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default="active")
    valid_from = Column(DateTime(timezone=True), nullable=False)
    valid_until = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'expired', 'superseded', 'revoked')", name="ck_appr_status"
        ),
        ForeignKeyConstraint(
            ["plan_id"], ["strategy_plans.plan_id"], name="fk_appr_plan", ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["reservation_id"],
            ["strategy_reservations.reservation_id"],
            name="fk_appr_reservation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            name="fk_appr_strategy_canonical",
            ondelete="RESTRICT",
        ),
        Index("idx_approvals_strategy", "strategy_id", "created_at"),
    )


class AccountReconciliationVersion(Base):
    """Monotonic counter of an account's reconciliation state (D-8).

    An approval pins the value so divergence discovered *after* approval
    invalidates it — which is the whole point: the owner approved a book that was
    aligned, and if it stops being aligned the approval must say so.
    """

    __tablename__ = "account_reconciliation_versions"

    account_id = Column(Text, primary_key=True)
    version = Column(BigInteger, nullable=False, server_default="0")
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# Settlement barrier and four-axis evidence (Phase 5 / G7)
# ---------------------------------------------------------------------------


#: Barrier event vocabulary (mirrors ``ck_sebe_event``).
BARRIER_EVENTS = ("work_created", "work_resolved", "proof_recorded")

#: Settlement overall rollup vocabulary (mirrors ``ck_ssa_overall``).
SETTLEMENT_OVERALL = ("settled", "unsettled", "unknown")


class StrategyExecutionBarrier(Base):
    """The durable execution version per book ``(account, strategy, env)`` (D-1).

    ``barrier_version`` is bumped ONLY by work transitions, in the same
    transaction as their event row. A quiescence proof stamps
    ``quiet_since_version = barrier_version`` without bumping the version, so
    the invariant "any later work event invalidates every prior proof" is the
    plain inequality ``quiet_since_version <> barrier_version`` — never a
    quiet window and never two identical reads.
    """

    __tablename__ = "strategy_execution_barriers"

    account_id = Column(Text, primary_key=True)
    strategy_id = Column(Text, primary_key=True)
    execution_environment = Column(Text, primary_key=True)
    barrier_version = Column(BigInteger, nullable=False, server_default="0")
    quiet_since_version = Column(BigInteger, nullable=True)
    last_proof_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')",
            name="ck_seb_environment",
        ),
    )


class StrategyExecutionBarrierEvent(Base):
    """Append-only barrier event log. **Insert-only, enforced by a trigger.**

    Each row records the barrier version at the moment of the event. Work
    events carry the NEW (bumped) version; a ``proof_recorded`` row carries the
    CURRENT version — proofs do not change the version, work does.
    """

    __tablename__ = "strategy_execution_barrier_events"

    id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    strategy_id = Column(Text, nullable=False)
    execution_environment = Column(Text, nullable=False)
    version = Column(BigInteger, nullable=False)
    event = Column(Text, nullable=False)
    ref = Column(Text, nullable=True)
    detail = Column(JSON, nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "event IN ('work_created', 'work_resolved', 'proof_recorded')",
            name="ck_sebe_event",
        ),
        CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')",
            name="ck_sebe_environment",
        ),
        Index(
            "idx_barrier_events_key",
            "account_id",
            "strategy_id",
            "execution_environment",
            "created_at",
        ),
        Index(
            "idx_barrier_events_version",
            "account_id",
            "strategy_id",
            "execution_environment",
            "version",
        ),
    )


class StrategySettlementAssessment(Base):
    """Append-only snapshot of one four-axis settlement assessment (D-3, D-5).

    An assessment is a **snapshot, not a state**: it records the
    ``barrier_version`` it was taken at plus per-axis evidence digests, so a
    later barrier bump (late fill, new work) makes its staleness detectable —
    the platform re-assesses before acting. Trigger-immutable like every other
    evidence surface in this campaign.
    """

    __tablename__ = "strategy_settlement_assessments"

    id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    strategy_id = Column(Text, nullable=False)
    execution_environment = Column(Text, nullable=False)
    overall = Column(Text, nullable=False)
    barrier_version = Column(BigInteger, nullable=False)
    axes = Column(JSON, nullable=False)
    evidence_digest = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "overall IN ('settled', 'unsettled', 'unknown')", name="ck_ssa_overall"
        ),
        CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')",
            name="ck_ssa_environment",
        ),
        Index(
            "idx_settlement_assessments_key",
            "account_id",
            "strategy_id",
            "execution_environment",
            "created_at",
        ),
    )


# ---------------------------------------------------------------------------
# Plan execution event trail (Phase 6 / Project 6, D-3)
# ---------------------------------------------------------------------------


#: Execution event vocabulary (mirrors ``ck_spee_event``).
PLAN_EXECUTION_EVENTS = (
    "submitted",
    "filled",
    "partially_filled",
    "rejected",
    "failed",
    "no_op",
)

#: Plan kinds the paper executor can act on: every kind the compiler registry
#: ships. ``single_instrument``/``intent_bundle`` carry explicit signed
#: quantities per leg, ``target_futures``/``option_structure`` carry compiled
#: per-leg quantities from the pinned contracts, and ``target_weights`` is sized
#: from its pinned weights and the strategy's allocation (see the executor).
EXECUTABLE_PLAN_KINDS = (
    "single_instrument",
    "intent_bundle",
    "target_weights",
    "target_futures",
    "option_structure",
)


class StrategyPlanExecutionEvent(Base):
    """Append-only trail of one plan's execution (D-3). **Insert-only by trigger.**

    The trail is the ONLY execution state the schema carries: the current state
    of a step is derived from its rows (``submitted`` → ``filled`` | ``rejected``
    | ``failed``; ``no_op`` is terminal in itself), so execution history can
    never be rewritten into something that did not happen. Every refusal is an
    event with its named reason, and every paper order the executor submits is
    linked by ``paper_order_id`` — the fill-to-attribution chain end to end.
    """

    __tablename__ = "strategy_plan_execution_events"

    id = Column(Text, primary_key=True)
    plan_id = Column(Text, nullable=False)
    step_no = Column(Integer, nullable=False)
    event = Column(Text, nullable=False)
    paper_order_id = Column(Text, nullable=True)
    filled_quantity = Column(Integer, nullable=True)
    refusal_reason = Column(Text, nullable=True)
    actor_id = Column(Text, nullable=False)
    detail = Column(JSON, nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "event IN ('submitted', 'filled', 'partially_filled', 'rejected', 'failed', "
            "'no_op')",
            name="ck_spee_event",
        ),
        # Declared because ``strategy_plans`` IS in this metadata: it orders the
        # unit of work so a plan exists before its first execution event. The
        # ON DELETE RESTRICT keeps the trail attached to its plan forever.
        ForeignKeyConstraint(
            ["plan_id"],
            ["strategy_plans.plan_id"],
            name="fk_plan_exec_plan",
            ondelete="RESTRICT",
        ),
        Index("idx_plan_exec_events", "plan_id", "step_no", "created_at"),
    )


class StrategyScheduleOccurrence(Base):
    """One materialised occurrence of a schedule (G11).

    The schedule itself is the pre-existing ``hosted_strategy_schedules`` row; this
    table records *which* occurrences existed and what happened to each.
    ``UNIQUE (schedule_id, occurrence_key)`` is the fencing contract: two
    schedulers racing the same tick collide on the index instead of double-firing,
    and a missed occurrence is a row that says ``skipped`` with its reason — never
    a silent gap in the record.
    """

    __tablename__ = "strategy_schedule_occurrences"

    id = Column(Text, primary_key=True)
    schedule_id = Column(Text, nullable=False)
    strategy_id = Column(Text, nullable=False)
    occurrence_key = Column(Text, nullable=False)
    due_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(Text, nullable=False, server_default="pending")
    fired_at = Column(DateTime(timezone=True), nullable=True)
    evaluation_id = Column(Text, nullable=True)
    skip_reason = Column(Text, nullable=True)
    detail = Column(JSON, nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'fired', 'skipped', 'expired')",
            name="ck_sched_occurrence_status",
        ),
        UniqueConstraint("schedule_id", "occurrence_key", name="uq_schedule_occurrences_key"),
        ForeignKeyConstraint(
            ["schedule_id"],
            ["hosted_strategy_schedules.id"],
            name="fk_occurrence_schedule",
            ondelete="CASCADE",
        ),
        Index("idx_schedule_occurrences_status", "status", "due_at"),
    )


class PaperOrderFillProgress(Base):
    """A paper order's fill state (G12).

    Until this table existed every paper fill was instant and full. Keeping the
    progress in a side table rather than altering ``paper_orders`` means the
    existing runtime is untouched: an order with no progress row behaves exactly
    as it did before, and a rollback that drops this table leaves instant-full
    fills rather than a broken runtime.
    """

    __tablename__ = "paper_order_fill_progress"

    account_scope = Column(Text, primary_key=True)
    paper_order_id = Column(Text, primary_key=True)
    filled_quantity = Column(Integer, nullable=False, server_default="0")
    remaining_quantity = Column(Integer, nullable=False, server_default="0")
    status = Column(Text, nullable=False, server_default="open")
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'partially_filled', 'filled', 'cancelled')",
            name="ck_pofp_status",
        ),
        CheckConstraint("filled_quantity >= 0", name="ck_pofp_filled_non_negative"),
        CheckConstraint("remaining_quantity >= 0", name="ck_pofp_remaining_non_negative"),
    )


class StrategyCorporateActionEvent(Base):
    """A detected suspected corporate action (G13, first delivery).

    The row is MUTABLE because a detection has a lifecycle (detected → escalated
    → resolved); the append-only record is
    :class:`StrategyCorporateActionEventLog`. This mirrors
    ``strategy_reservations`` + ``strategy_reservation_events`` exactly — one
    pattern, documented, not half of two.
    """

    __tablename__ = "strategy_corporate_action_events"

    id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    instrument_token = Column(BigInteger, nullable=False)
    exchange = Column(Text, nullable=False)
    tradingsymbol = Column(Text, nullable=False)
    product = Column(Text, nullable=False)
    action_kind = Column(Text, nullable=False)
    evidence = Column(JSON, nullable=False, server_default=text("'{}'"))
    status = Column(Text, nullable=False, server_default="detected")
    resolved_adjustment_id = Column(Text, nullable=True)
    detected_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    escalated_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "action_kind IN ('suspected_split', 'suspected_bonus', 'suspected_merger', "
            "'unclassified')",
            name="ck_scae_action_kind",
        ),
        CheckConstraint(
            "status IN ('detected', 'escalated', 'resolved')", name="ck_scae_status"
        ),
        Index("idx_corporate_action_account", "account_id", "detected_at"),
    )


class StrategyCorporateActionEventLog(Base):
    """Append-only log of a corporate-action detection's transitions."""

    __tablename__ = "strategy_corporate_action_event_log"

    id = Column(Text, primary_key=True)
    event_id = Column(Text, nullable=False)
    event = Column(Text, nullable=False)
    actor_id = Column(Text, nullable=True)
    detail = Column(JSON, nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "event IN ('detected', 'escalated', 'freeze_confirmed', 'resolved')",
            name="ck_scael_event",
        ),
        ForeignKeyConstraint(
            ["event_id"],
            ["strategy_corporate_action_events.id"],
            name="fk_corporate_action_log_event",
            ondelete="RESTRICT",
        ),
        Index("idx_corporate_action_log_event", "event_id", "created_at"),
    )


class StrategySquareoffEvidence(Base):
    """Append-only record of a MIS square-off and what it did (Project 8).

    A failed square-off is recorded ``action_required`` and keeps reconciling —
    explicitly NOT settlement. A broker auto-square-off observed afterwards is
    ``missed_by_broker``: a fallback that happened, never the control that
    decided. Trigger-immutable.
    """

    __tablename__ = "strategy_squareoff_evidence"

    id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    strategy_id = Column(Text, nullable=False)
    strategy_run_id = Column(Text, nullable=False)
    product = Column(Text, nullable=False)
    session_date = Column(Date, nullable=False)
    exchange = Column(Text, nullable=False)
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    exit_claim_id = Column(Text, nullable=True)
    outcome = Column(Text, nullable=False)
    detail = Column(JSON, nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('squared_off', 'action_required', 'missed_by_broker', "
            "'stale_worker_exit')",
            name="ck_sse_outcome",
        ),
        Index("idx_sse_run", "strategy_run_id", "session_date"),
    )


class StrategyRoll(Base):
    """A durable ordered roll: acquire → prove-filled → release-close (R3 §13).

    The full-required-fill gate lives in the state machine, not in an order flag:
    a roll releases the old contract's close step only once the replacement is
    PROVEN filled on the strategy's attributed book. A partial or stalled
    replacement marks the roll ``action_required`` with the old attribution intact
    and never auto-reverses.
    """

    __tablename__ = "strategy_rolls"

    roll_id = Column(Text, primary_key=True)
    strategy_id = Column(Text, nullable=False)
    account_id = Column(Text, nullable=False)
    #: Both identities, retained through the whole transition.
    old_instrument_id = Column(Text, nullable=False)
    new_instrument_id = Column(Text, nullable=False)
    old_coordinate = Column(JSON, nullable=False)
    new_coordinate = Column(JSON, nullable=False)
    required_replacement_quantity = Column(Integer, nullable=False)
    proven_filled_quantity = Column(Integer, nullable=False, server_default="0")
    state = Column(Text, nullable=False, server_default="acquiring")
    action_reason = Column(Text, nullable=True)
    peak_margin_evidence = Column(JSON, nullable=True)
    plan_id = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "state IN ('acquiring', 'proving_filled', 'releasing_old', 'completed', "
            "'action_required')",
            name="ck_roll_state",
        ),
        CheckConstraint(
            "required_replacement_quantity > 0", name="ck_roll_required_positive"
        ),
        CheckConstraint("proven_filled_quantity >= 0", name="ck_roll_proven_non_negative"),
        ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            name="fk_rolls_strategy_canonical",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["plan_id"], ["strategy_plans.plan_id"], name="fk_rolls_plan", ondelete="RESTRICT"
        ),
        Index("idx_rolls_strategy_state", "strategy_id", "state"),
    )


class StrategyRollEvent(Base):
    """Append-only trail of a roll's transitions. Trigger-immutable."""

    __tablename__ = "strategy_roll_events"

    id = Column(Text, primary_key=True)
    roll_id = Column(Text, nullable=False)
    event = Column(Text, nullable=False)
    detail = Column(JSON, nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "event IN ('created', 'acquired', 'replacement_filled', 'fill_proven', "
            "'close_released', 'old_flat', 'completed', 'stalled', 'escalated')",
            name="ck_roll_event",
        ),
        ForeignKeyConstraint(
            ["roll_id"], ["strategy_rolls.roll_id"], name="fk_roll_events_roll",
            ondelete="RESTRICT",
        ),
        Index("idx_roll_events", "roll_id", "created_at"),
    )


class OptionSettlementEvidence(Base):
    """Append-only evidence that an option structure settled (Project 10 / G8).

    Cash settlement applies an external adjustment ONLY with a row here, from an
    authoritative source. Expiry time alone adjusts nothing, and neither does a
    position disappearing from view: both are consistent with the position still
    existing and simply not being visible.
    """

    __tablename__ = "option_settlement_evidence"

    id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    option_run_id = Column(Text, nullable=False)
    structure_digest = Column(Text, nullable=False)
    settlement_kind = Column(Text, nullable=False)
    evidence_source = Column(Text, nullable=False)
    evidence_ref = Column(JSON, nullable=False)
    recorded_by = Column(Text, nullable=False)
    adjustment_id = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "settlement_kind IN ('cash', 'physical')", name="ck_ose_settlement_kind"
        ),
        CheckConstraint(
            "evidence_source IN ('broker_ledger', 'contract_note', 'exchange_file')",
            name="ck_ose_evidence_source",
        ),
        Index("idx_ose_run", "option_run_id"),
    )
