"""market-session schedule kind and many-decision session identity.

Revision ID: 20260926_000054
Revises: 20260926_000053
Create Date: 2026-09-26

A market-session schedule is one job for one NSE trading day, with offsets
pinned beside the other schedule configuration. Session children may submit
many proposals under validated ``session_occurrence`` identities, so the
proposal envelope CHECK gains that kind. No existing schedule or proposal row
changes.
"""

from alembic import op

revision = "20260926_000054"
down_revision = "20260926_000053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules ADD COLUMN IF NOT EXISTS "
        "start_offset_min INTEGER"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules ADD COLUMN IF NOT EXISTS "
        "stop_offset_min INTEGER"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_kind"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "ADD CONSTRAINT ck_hosted_strategy_schedules_kind CHECK (schedule_kind IN "
        "('daily', 'weekly', 'monthly', 'calendar', 'market_session'))"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_start_offset"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "ADD CONSTRAINT ck_hosted_strategy_schedules_start_offset CHECK "
        "(start_offset_min IS NULL OR (start_offset_min >= 0 AND start_offset_min < 1440))"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_stop_offset"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "ADD CONSTRAINT ck_hosted_strategy_schedules_stop_offset CHECK "
        "(stop_offset_min IS NULL OR (stop_offset_min >= 0 AND stop_offset_min < 1440))"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_session_offsets"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "ADD CONSTRAINT ck_hosted_strategy_schedules_session_offsets CHECK "
        "(schedule_kind <> 'market_session' OR (start_offset_min IS NOT NULL "
        "AND stop_offset_min IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_non_session_offsets"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "ADD CONSTRAINT ck_hosted_strategy_schedules_non_session_offsets CHECK "
        "(schedule_kind = 'market_session' OR "
        "(start_offset_min IS NULL AND stop_offset_min IS NULL))"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals "
        "DROP CONSTRAINT IF EXISTS ck_proposals_evaluation_kind"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals "
        "ADD CONSTRAINT ck_proposals_evaluation_kind CHECK (evaluation_kind IN "
        "('scheduled_occurrence', 'session_occurrence', 'run_now'))"
    )
    # A session occurrence names its job the same way a scheduled occurrence
    # does; widen the job-required check to match instead of leaving it
    # unenforced for the new kind.
    op.execute(
        "ALTER TABLE public.strategy_proposals "
        "DROP CONSTRAINT IF EXISTS ck_proposals_scheduled_requires_job"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals "
        "ADD CONSTRAINT ck_proposals_scheduled_requires_job CHECK "
        "(evaluation_kind NOT IN ('scheduled_occurrence', 'session_occurrence') "
        "OR job_id IS NOT NULL)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_proposals "
        "DROP CONSTRAINT IF EXISTS ck_proposals_evaluation_kind"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals "
        "ADD CONSTRAINT ck_proposals_evaluation_kind CHECK (evaluation_kind IN "
        "('scheduled_occurrence', 'run_now'))"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals "
        "DROP CONSTRAINT IF EXISTS ck_proposals_scheduled_requires_job"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals "
        "ADD CONSTRAINT ck_proposals_scheduled_requires_job CHECK "
        "(evaluation_kind <> 'scheduled_occurrence' OR job_id IS NOT NULL)"
    )
    for constraint in (
        "ck_hosted_strategy_schedules_non_session_offsets",
        "ck_hosted_strategy_schedules_session_offsets",
        "ck_hosted_strategy_schedules_stop_offset",
        "ck_hosted_strategy_schedules_start_offset",
    ):
        op.execute(
            f"ALTER TABLE public.hosted_strategy_schedules "
            f"DROP CONSTRAINT IF EXISTS {constraint}"
        )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_kind"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "ADD CONSTRAINT ck_hosted_strategy_schedules_kind CHECK (schedule_kind IN "
        "('daily', 'weekly', 'monthly', 'calendar'))"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP COLUMN IF EXISTS stop_offset_min"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP COLUMN IF EXISTS start_offset_min"
    )
