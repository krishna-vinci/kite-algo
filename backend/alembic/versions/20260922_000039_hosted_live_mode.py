"""Admit the live mode for hosted strategies and link live trail rows.

Revision ID: 20260922_000039
Revises: 20260921_000038
Create Date: 2026-09-22

Three additive changes:

1. The hosted registries may now carry ``live`` (strategy default mode, schedule
   mode, job mode). This only makes the mode *representable*; execution is still
   gated by the deployment setting ``HOSTED_LIVE_ENABLED`` (default false) and by
   the persisted-authority checks in the live service.
2. ``strategy_plan_execution_events`` gains the BROKER order id bound to a live
   step (``paper_order_id`` stays the paper link). Purely additive column.
3. ``live_plan_submissions`` gains the single-writer consumer lease
   (``consumer_token`` / ``consumer_until``) and admits the resolved/repair
   vocabulary the outcome consumer writes. The lease is what makes exactly ONE
   consumer the writer of a step: the claim is taken by a conditional UPDATE on
   the row, so a second instance (or a restart) updates zero rows instead of
   duplicating the publish/consume/barrier effects. ``consumer_until`` is the
   crash repair: an abandoned lease expires by plain time comparison and a later
   pass resumes the recorded cursor.

Downgrade restores the paper/dry-run-only CHECKs (it will fail if live rows
exist, which is the correct fix-forward posture) and drops the new columns.
"""

from alembic import op

revision = "20260922_000039"
down_revision = "20260921_000038"
branch_labels = None
depends_on = None

_PAPER_ONLY = "'paper','dry_run'"
_WITH_LIVE = "'paper','dry_run','live'"


def _swap_execution_mode_constraint(table: str, constraint: str, allowed: str) -> None:
    op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT IF EXISTS {constraint}")
    op.execute(
        f"ALTER TABLE public.{table} ADD CONSTRAINT {constraint} "
        f"CHECK (execution_mode IN ({allowed}))"
    )


def upgrade() -> None:
    for table, constraint in (
        ("hosted_strategy_schedules", "ck_hosted_strategy_schedules_execution_mode"),
        ("strategy_jobs", "ck_strategy_jobs_execution_mode"),
    ):
        _swap_execution_mode_constraint(table, constraint, _WITH_LIVE)

    op.execute(
        "ALTER TABLE public.hosted_strategies "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategies_execution_mode"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategies "
        "ADD CONSTRAINT ck_hosted_strategies_execution_mode "
        f"CHECK (default_execution_mode IN ({_WITH_LIVE}))"
    )

    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "ADD COLUMN IF NOT EXISTS broker_order_id TEXT"
    )

    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "ADD COLUMN IF NOT EXISTS consumer_token TEXT"
    )
    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "ADD COLUMN IF NOT EXISTS consumer_until TIMESTAMPTZ"
    )

    # Durable live submissions progress: an ingested fill moves a claim from
    # ``pending`` to ``partial``/``finalizing``/``filled``; a terminal broker
    # refusal to ``rejecting``/``rejected``; a terminal cancel with a residual
    # (or an effect the consumer cannot confirm) to the explicit
    # ``repair_required``. Widening the vocabulary is additive (existing rows
    # satisfy it).
    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "DROP CONSTRAINT IF EXISTS ck_live_plan_submission_state"
    )
    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "ADD CONSTRAINT ck_live_plan_submission_state "
        "CHECK (state IN ('pending','partial','finalizing','rejecting','repair_required',"
        "'residual_abandoned','filled','uncertain','rejected','no_op'))"
    )

    # The database-level backstop for the live outcome consumer's
    # ``work_resolved`` de-duplication. The consumer decides inside one
    # transaction holding the book lock; this index makes a duplicate literally
    # impossible, including for a caller that bypassed that path.
    #
    # Scoped to ``execution_environment = 'live'``: the live vocabulary is new
    # (this migration), so the constraint cannot collide with existing paper or
    # dry-run history, and it still dedupes per book because the environment is
    # part of the key.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_barrier_work_resolved_live_step "
        "ON public.strategy_execution_barrier_events "
        "(account_id, strategy_id, execution_environment, event, ref, "
        "(detail ->> 'plan_id')) "
        "WHERE event = 'work_resolved' AND ref IS NOT NULL "
        "AND execution_environment = 'live'"
    )

    # The operator's bounded disposition of a live residual is an append-only
    # TRAIL event, so the trail vocabulary admits it. Additive: existing rows
    # satisfy the widened constraint.
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "DROP CONSTRAINT IF EXISTS strategy_plan_execution_events_event_check"
    )
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "DROP CONSTRAINT IF EXISTS ck_spee_event"
    )
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "ADD CONSTRAINT ck_spee_event CHECK (event IN "
        "('submitted','filled','partially_filled','rejected','failed','no_op',"
        "'residual_abandoned'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "DROP CONSTRAINT IF EXISTS ck_spee_event"
    )
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "ADD CONSTRAINT ck_spee_event CHECK (event IN "
        "('submitted','filled','partially_filled','rejected','failed','no_op'))"
    )
    op.execute("DROP INDEX IF EXISTS public.uq_barrier_work_resolved_live_step")
    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "DROP COLUMN IF EXISTS consumer_until"
    )
    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "DROP COLUMN IF EXISTS consumer_token"
    )
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "DROP COLUMN IF EXISTS broker_order_id"
    )
    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "DROP CONSTRAINT IF EXISTS ck_live_plan_submission_state"
    )
    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "ADD CONSTRAINT ck_live_plan_submission_state "
        "CHECK (state IN ('pending','uncertain','rejected','no_op'))"
    )
    for table, constraint in (
        ("hosted_strategy_schedules", "ck_hosted_strategy_schedules_execution_mode"),
        ("strategy_jobs", "ck_strategy_jobs_execution_mode"),
    ):
        _swap_execution_mode_constraint(table, constraint, _PAPER_ONLY)

    op.execute(
        "ALTER TABLE public.hosted_strategies "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategies_execution_mode"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategies "
        "ADD CONSTRAINT ck_hosted_strategies_execution_mode "
        f"CHECK (default_execution_mode IN ({_PAPER_ONLY}))"
    )
