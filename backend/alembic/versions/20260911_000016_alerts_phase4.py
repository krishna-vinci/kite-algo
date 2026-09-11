"""alerts Phase 4 F10 — advanced-condition state, breadth, session caps,
external signal producers, and the universes.kind CHECK correction.

Revision ID: 20260911_000016
Revises: 20260910_000015
Create Date: 2026-09-11 00:00:16

Two classes of object live here and the downgrade treats them differently:

- **Computed state** (breadth threshold/contributions, session counters,
  suppression counters, external *values*) is disposable: it is derived from
  evaluation and rebuilds on the next pass.
- **User-authored configuration** (external producer definitions, their value
  schemas, their credentials, and ``universes.kind='screener'`` rows) is NOT
  disposable. The downgrade refuses while any of it is present unless the
  operator passes ``-x drop_phase4_config=true``, and every refusal check runs
  BEFORE any DDL so a refused downgrade leaves the schema untouched.
"""
import os

from alembic import op
from sqlalchemy import text

revision = "20260911_000016"
down_revision = "20260910_000015"
branch_labels = None
depends_on = None


def _drop_config_requested() -> bool:
    """True when the operator explicitly opted into destroying configuration.

    Three channels are accepted, because Alembic only hands ``-x key=value``
    to the migration through ``cmd_opts`` (it is normally an env.py concern)
    and operators may drive migrations programmatically:

    - ``-x drop_phase4_config=true`` (the documented CLI form);
    - ``config.attributes["drop_phase4_config"]`` (programmatic callers and
      env.py scripts that forward the flag);
    - ``ALERTS_PHASE4_ALLOW_CONFIG_DROP=1`` (scripts, CI, and any Alembic
      version whose internals differ).
    """
    config = op.get_context().config
    candidates = [
        config.attributes.get("drop_phase4_config"),
        config.get_main_option("drop_phase4_config"),
        os.environ.get("ALERTS_PHASE4_ALLOW_CONFIG_DROP"),
    ]
    cmd_opts = getattr(config, "cmd_opts", None)
    for raw in list(getattr(cmd_opts, "x", None) or []):
        if isinstance(raw, str) and "=" in raw:
            key, _, value = raw.partition("=")
            if key.strip() == "drop_phase4_config":
                candidates.append(value)
    for candidate in candidates:
        if str(candidate).strip().lower() in ("true", "1", "yes"):
            return True
    return False


def _refuse_if_configuration_present() -> None:
    """Refuse a destructive downgrade while user-authored rows exist.

    Runs first, before any DROP: a refused downgrade must leave the database
    exactly as it was. The operator is told precisely what exists and how to
    proceed deliberately.
    """
    if _drop_config_requested():
        return
    bind = op.get_bind()
    problems = []
    for table, label in (
        ("external_signal_producers", "external signal producers"),
        ("external_signal_producer_credentials", "external producer credentials"),
    ):
        try:
            count = bind.execute(text(f"SELECT count(*) FROM public.{table}")).scalar()
        except Exception:
            continue  # table already absent: nothing to protect
        if count:
            problems.append(f"{count} {label}")
    try:
        screener_universes = bind.execute(
            text("SELECT count(*) FROM public.universes WHERE kind = 'screener'")
        ).scalar()
    except Exception:
        screener_universes = 0
    if screener_universes:
        problems.append(f"{screener_universes} screener-backed universes")

    if problems:
        raise RuntimeError(
            "Refusing to downgrade 20260911_000016: user-authored configuration "
            "is present (" + "; ".join(problems) + "). Export it first "
            "(GET /api/worker/signals/producers and a database backup), then re-run "
            "with 'alembic -c backend/alembic.ini downgrade 20260910_000015 "
            "-x drop_phase4_config=true' to destroy it deliberately."
        )


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Breadth: one threshold row per (owner, workflow, revision, stage) plus
    # one contribution row per participating instrument. State is NEVER kept
    # in per-subscription checkpoints: a workflow-level event cannot be
    # governed by N per-instrument copies of `satisfied`.
    # ------------------------------------------------------------------
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.alert_breadth_state (
        id UUID PRIMARY KEY,
        owner_id TEXT NOT NULL,
        workflow_id UUID NOT NULL,
        revision_id UUID NOT NULL,
        stage_id TEXT NOT NULL,
        satisfied BOOLEAN NOT NULL DEFAULT false,
        crossing_seq BIGINT NOT NULL DEFAULT 0,
        satisfied_since_ts TIMESTAMPTZ,
        last_fired_ts TIMESTAMPTZ,
        last_count INTEGER,
        member_count INTEGER,
        aggregation_watermark TIMESTAMPTZ,
        membership_resolved_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (owner_id, workflow_id, revision_id, stage_id)
    );
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.alert_breadth_triggers (
        id BIGSERIAL PRIMARY KEY,
        owner_id TEXT NOT NULL,
        workflow_id UUID NOT NULL,
        revision_id UUID NOT NULL,
        stage_id TEXT NOT NULL,
        instrument_key TEXT NOT NULL,
        last_trigger_ts TIMESTAMPTZ NOT NULL,
        last_bar_ts TIMESTAMPTZ,
        universe_revision INTEGER,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (owner_id, workflow_id, revision_id, stage_id, instrument_key)
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_breadth_triggers_window
        ON public.alert_breadth_triggers
        (workflow_id, revision_id, stage_id, last_trigger_ts);
    """)

    # ------------------------------------------------------------------
    # Session caps: one counter per (owner, workflow, revision, alert,
    # session) shared atomically across every instrument, and a durable
    # record of what the cap suppressed so a skipped notification is
    # inspectable rather than silent.
    # ------------------------------------------------------------------
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.alert_session_counters (
        id UUID PRIMARY KEY,
        owner_id TEXT NOT NULL,
        workflow_id UUID NOT NULL,
        revision_id UUID NOT NULL,
        alert_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        first_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (owner_id, workflow_id, revision_id, alert_id, session_id)
    );
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.alert_suppression_counters (
        id UUID PRIMARY KEY,
        owner_id TEXT NOT NULL,
        workflow_id UUID NOT NULL,
        revision_id UUID NOT NULL,
        alert_id TEXT NOT NULL,
        session_id TEXT,
        reason TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        first_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_instrument_key TEXT,
        last_stage_id TEXT,
        UNIQUE (owner_id, workflow_id, revision_id, alert_id, session_id, reason)
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_suppression_counters_workflow
        ON public.alert_suppression_counters (workflow_id, reason);
    """)

    # ------------------------------------------------------------------
    # External signal producers. Producer definitions, value schemas and
    # credentials are USER-AUTHORED configuration; values are expiring data.
    # Credentials store a hash only: the raw secret is returned exactly once
    # at issuance and is never retrievable, echoed or logged afterwards.
    # ------------------------------------------------------------------
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.external_signal_producers (
        id UUID PRIMARY KEY,
        owner_id TEXT NOT NULL,
        name TEXT NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT true,
        value_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
        default_ttl_s INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        revoked_at TIMESTAMPTZ,
        UNIQUE (owner_id, name)
    );
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.external_signal_producer_credentials (
        id UUID PRIMARY KEY,
        producer_id UUID NOT NULL REFERENCES public.external_signal_producers(id)
            ON DELETE CASCADE,
        token_id TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_used_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_producer_credentials_producer
        ON public.external_signal_producer_credentials (producer_id, status);
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.external_signal_values (
        id UUID PRIMARY KEY,
        producer_id UUID NOT NULL REFERENCES public.external_signal_producers(id)
            ON DELETE CASCADE,
        owner_id TEXT NOT NULL,
        instrument_key TEXT,
        event_time TIMESTAMPTZ NOT NULL,
        received_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('accepted', 'late')),
        value JSONB NOT NULL,
        content_hash TEXT NOT NULL,
        idempotency_key TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (producer_id, idempotency_key)
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_external_values_lookup
        ON public.external_signal_values (producer_id, instrument_key, event_time DESC);
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_external_values_expiry
        ON public.external_signal_values (expires_at);
    """)

    # ------------------------------------------------------------------
    # Phase 3 defect repair: the code supports kind='screener' (dynamic
    # universes fed by screener results) but the CHECK added in
    # 20260909_000014 admits only explicit/index/portfolio, so creating one
    # failed on real PostgreSQL. An ALTER (not a rebuild) so it fixes an
    # existing database; the constraint name is the auto-generated one.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE public.universes DROP CONSTRAINT IF EXISTS universes_kind_check;")
    op.execute("""
    ALTER TABLE public.universes ADD CONSTRAINT universes_kind_check
        CHECK (kind IN ('explicit', 'index', 'portfolio', 'screener'));
    """)


def downgrade() -> None:
    _refuse_if_configuration_present()

    # Values first (dependent on producers), then credentials, then producers.
    op.execute("DROP TABLE IF EXISTS public.external_signal_values")
    op.execute("DROP TABLE IF EXISTS public.external_signal_producer_credentials")
    op.execute("DROP TABLE IF EXISTS public.external_signal_producers")
    # Computed state.
    op.execute("DROP TABLE IF EXISTS public.alert_suppression_counters")
    op.execute("DROP TABLE IF EXISTS public.alert_session_counters")
    op.execute("DROP TABLE IF EXISTS public.alert_breadth_triggers")
    op.execute("DROP TABLE IF EXISTS public.alert_breadth_state")
    # Revert the CHECK only after the configuration pre-checks passed, so a
    # refused downgrade never leaves a constraint that rejects existing rows.
    op.execute("ALTER TABLE public.universes DROP CONSTRAINT IF EXISTS universes_kind_check;")
    op.execute("""
    ALTER TABLE public.universes ADD CONSTRAINT universes_kind_check
        CHECK (kind IN ('explicit', 'index', 'portfolio'));
    """)
