"""owner actions: a terminal cancel event and an owner-action audit vocabulary.

Revision ID: 20260925_000049
Revises: 20260925_000048
Create Date: 2026-09-25

Purely additive; three CHECKs are widened and nothing is backfilled.

* ``strategy_plan_execution_events.event`` gains ``cancelled``. The plan trail's
  terminal vocabulary was missing the one word a cancelled order needs:
  ``plan_binding.option_plan_execution_state`` already reads ``cancelled`` as a
  terminal outcome (``_PLAN_TRAIL_TERMINAL_OUTCOMES``), so a row the platform
  could not insert was a hole in an otherwise complete rule. B2.6b's owner
  dispositions (cancel-pending and dead-submission) are what need it.
* ``strategy_proposal_journal.event`` gains ``owner_action``. An owner's
  cancel/disposition is a strategy-scoped audit event on the same append-only
  journal the proposal lifecycle already writes, distinct from ``plan_created``
  (nothing was planned) and from ``validation_refused`` (nothing was refused).
* ``strategy_job_reconciliations.outcome`` gains ``owner_action``. The hosted-job
  audit is the append-only table a repair already writes to; distinct from
  ``reconciled`` (a human cleared the job's block - an owner action never does)
  and from ``option_run_repair`` (that path moves a run, this one settles a step).

Every widening accepts all existing values, so no row is invalidated.
"""

from alembic import op

revision = "20260925_000049"
down_revision = "20260925_000048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "DROP CONSTRAINT IF EXISTS ck_spee_event"
    )
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "ADD CONSTRAINT ck_spee_event "
        "CHECK (event IN ('submitted', 'filled', 'partially_filled', 'rejected', "
        "'failed', 'no_op', 'residual_abandoned', 'release_recovered', 'cancelled'))"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposal_journal "
        "DROP CONSTRAINT IF EXISTS ck_proposal_journal_event"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposal_journal "
        "ADD CONSTRAINT ck_proposal_journal_event "
        "CHECK (event IN ('received', 'idempotent_retry', 'conflict', "
        "'validation_refused', 'plan_created', 'owner_action'))"
    )
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "DROP CONSTRAINT IF EXISTS ck_strategy_job_reconciliations_outcome"
    )
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "ADD CONSTRAINT ck_strategy_job_reconciliations_outcome "
        "CHECK (outcome IN ('reconciled', 'blocked', 'continuation', "
        "'option_run_repair', 'owner_action'))"
    )


def downgrade() -> None:
    # Narrowing a CHECK can only succeed while no row carries the new value, and
    # the constraint itself is the guard: an owner action is never silently
    # relabelled. The operator archives deliberately and re-runs the downgrade,
    # the same fix-forward posture 000043/000044 take.
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "DROP CONSTRAINT IF EXISTS ck_spee_event"
    )
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "ADD CONSTRAINT ck_spee_event "
        "CHECK (event IN ('submitted', 'filled', 'partially_filled', 'rejected', "
        "'failed', 'no_op', 'residual_abandoned', 'release_recovered'))"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposal_journal "
        "DROP CONSTRAINT IF EXISTS ck_proposal_journal_event"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposal_journal "
        "ADD CONSTRAINT ck_proposal_journal_event "
        "CHECK (event IN ('received', 'idempotent_retry', 'conflict', "
        "'validation_refused', 'plan_created'))"
    )
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "DROP CONSTRAINT IF EXISTS ck_strategy_job_reconciliations_outcome"
    )
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "ADD CONSTRAINT ck_strategy_job_reconciliations_outcome "
        "CHECK (outcome IN ('reconciled', 'blocked', 'continuation', 'option_run_repair'))"
    )
