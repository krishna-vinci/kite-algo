"""durable owner flatten: one resumable operation per strategy scope (B2.6b S3).

Revision ID: 20260925_000050
Revises: 20260925_000049
Create Date: 2026-09-25

Purely additive: one new table. Flatten (design section 3) is an orchestration -
stop the evaluator, cancel qualifying pending entry work, exit option runs one at
a time through their own staged exit, then close the non-option books with
target-zero reduction plans. Any step can WAIT on fills, so the work list and its
per-item outcome have to be durable: a second POST resumes the same operation
instead of starting a parallel flatten of the same book. ``manifest`` carries the
items with their outcomes, ``stop`` carries the evaluator-stop evidence the run
was gated on, and ``status`` is the operation's own verdict (``complete`` only
while every done condition holds).

``uq_sfo_open_scope`` is the resume rule: at most ONE open (non-complete)
operation exists per ``(account, strategy, environment)``, so "which operation am
I resuming" is never ambiguous. The downgrade drops exactly what the upgrade
created, so a prior-head database is restored verbatim.
"""

from alembic import op

revision = "20260925_000050"
down_revision = "20260925_000049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.strategy_flatten_operations (
            operation_id          TEXT PRIMARY KEY,
            strategy_id           TEXT NOT NULL,
            account_id            TEXT NOT NULL,
            execution_environment TEXT NOT NULL,
            status                TEXT NOT NULL
                CHECK (status IN ('complete','in_progress','blocked')),
            reason                TEXT NOT NULL DEFAULT '',
            actor_id              TEXT NOT NULL DEFAULT '',
            evidence_digest       TEXT NOT NULL DEFAULT '',
            stop                  JSONB NOT NULL DEFAULT '{}'::jsonb,
            manifest              JSONB NOT NULL DEFAULT '{}'::jsonb,
            refusal               TEXT,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_sfo_environment
                CHECK (execution_environment IN ('live','paper','dry_run')),
            CONSTRAINT fk_sfo_strategy FOREIGN KEY (strategy_id, account_id)
                REFERENCES public.strategies(id, account_scope) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sfo_scope ON public.strategy_flatten_operations "
        "(account_id, strategy_id, execution_environment)"
    )
    # One OPEN operation per scope: a second POST resumes this row.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_sfo_open_scope "
        "ON public.strategy_flatten_operations (account_id, strategy_id, execution_environment) "
        "WHERE status <> 'complete'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.strategy_flatten_operations")
