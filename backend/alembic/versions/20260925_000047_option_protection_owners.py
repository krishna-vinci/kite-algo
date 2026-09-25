"""Durable protection ownership for option runs (B2.4 S1).

Revision ID: 20260925_000047
Revises: 20260925_000046
Create Date: 2026-09-25

Protection is enumerated per OPEN WORKER RUN today, so a run leaves protection by
*status change alone* and no record outlives the closure. This revision adds the
missing ownership record: ONE row per option run plus an append-only event log,
written and transferred in the SAME transaction as the run, the plan edge and the
terminal status.

The primary key on ``option_run_id`` is the whole point: two owners are
unrepresentable, which a partial unique index on an otherwise-shared row would not
be. ``owner_epoch`` is the compare-and-swap ticket every claim/transfer/policy
change increments, and ``state`` has exactly two values - there is deliberately no
``transferring``: a two-phase state has a committed instant with no owner.

``ck_opo_owner_present`` ties the state to the owner column, so an ACTIVE row
always names the run that is authoritative, and a released row never does.

Purely additive: two new tables. The downgrade drops exactly what the upgrade
created, so a prior-head database is restored verbatim.
"""

from alembic import op

revision = "20260925_000047"
down_revision = "20260925_000046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.option_protection_owners (
            option_run_id        TEXT PRIMARY KEY
                REFERENCES public.option_run_states(strategy_run_id) ON DELETE RESTRICT,
            strategy_id          TEXT NOT NULL,
            account_id           TEXT NOT NULL,
            execution_environment TEXT NOT NULL,
            owner_run_id         TEXT,
            owner_epoch          BIGINT NOT NULL DEFAULT 1,
            policy_version       TEXT NOT NULL,
            policy               JSONB NOT NULL,
            action_state         TEXT NOT NULL DEFAULT 'none'
                CHECK (action_state IN ('none','claimed','staging','unresolved')),
            stage_digest         TEXT,
            state                TEXT NOT NULL DEFAULT 'active'
                CHECK (state IN ('active','released')),
            released_at          TIMESTAMPTZ,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_opo_owner_present
                CHECK ((state = 'active') = (owner_run_id IS NOT NULL)),
            CONSTRAINT ck_opo_environment
                CHECK (execution_environment IN ('live','paper','dry_run')),
            CONSTRAINT fk_opo_strategy FOREIGN KEY (strategy_id, account_id)
                REFERENCES public.strategies(id, account_scope) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.option_protection_owner_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            option_run_id TEXT NOT NULL,
            owner_epoch BIGINT NOT NULL,
            event TEXT NOT NULL CHECK (event IN
                ('claimed','transferred','policy_changed','action_claimed',
                 'action_resolved','released')),
            owner_run_id TEXT,
            actor_id TEXT,
            detail JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.option_protection_owner_events")
    op.execute("DROP TABLE IF EXISTS public.option_protection_owners")
