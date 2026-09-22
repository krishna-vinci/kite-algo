"""Durable multi-step parent for hosted live plan execution.

Revision ID: 20260922_000040
Revises: 20260922_000039
Create Date: 2026-09-22

Why a parent row exists at all:

- ``live_plan_submissions`` is a PER-STEP claim (``UNIQUE (plan_id, step_no)``).
  A one-leg plan can be described by that alone, but a portfolio/CNC basket, a
  MIS square-off or a futures roll is an ORDERED SET of steps whose release is
  governed by dependencies between them. That protocol does not belong in an
  ad-hoc ``detail`` blob: it has to be immutable, queryable and enforceable.

- ``live_plan_executions`` is the durable parent: exactly ONE row per frozen
  plan (``UNIQUE (plan_id)``), carrying the immutable ordered step/dependency
  specification frozen at first admission. Per-leg claims stay in
  ``live_plan_submissions``; this table never duplicates them, and it is not a
  second execution ledger.

- Parent creation, every step claim and the barrier work are materialized in ONE
  transaction under the canonical book lock, so a crash can never leave a
  half-materialized parent. Withheld dependents are written as ``withheld``
  claims (in flight, never dispatched) and are released only by the shared
  sequence pass.

Also additive here:

* ``live_plan_submissions`` admits ``withheld`` (a dependent step whose
  prerequisites have not filled) and ``releasing`` (a step the sequence pass has
  committed to dispatch but has not yet returned an order id for).
* the lane vocabulary reserves the futures-roll and option-structure lanes the
  next bundle wires, so that bundle adds NO new constraint migration.

Downgrade drops exactly what the upgrade created (and will fail while live rows
exist, which is the correct fix-forward posture).
"""

from alembic import op

revision = "20260922_000040"
down_revision = "20260922_000039"
branch_labels = None
depends_on = None

#: Lanes the parent vocabulary admits. ``target_weights``/``mis`` are produced by
#: this bundle; ``futures_roll``/``option_structure`` are RESERVED for the next
#: bundle, which then needs no constraint change to record them.
_LANES = (
    "'single_instrument','target_weights','mis','futures_roll','option_structure'"
)

_STEP_STATES_WITH_SEQUENCE = (
    "'pending','withheld','releasing','partial','finalizing','rejecting',"
    "'repair_required','residual_abandoned','filled','uncertain','rejected','no_op'"
)

_STEP_STATES_WITHOUT_SEQUENCE = (
    "'pending','partial','finalizing','rejecting','repair_required',"
    "'residual_abandoned','filled','uncertain','rejected','no_op'"
)


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.live_plan_executions ("
        " execution_id TEXT PRIMARY KEY,"
        " plan_id UUID NOT NULL,"
        " strategy_id TEXT NOT NULL,"
        " account_id TEXT NOT NULL,"
        " execution_environment TEXT NOT NULL,"
        " lane TEXT NOT NULL,"
        " state TEXT NOT NULL,"
        " step_spec JSONB NOT NULL DEFAULT '[]'::jsonb,"
        " detail JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " CONSTRAINT uq_live_plan_execution_plan UNIQUE (plan_id),"
        " CONSTRAINT ck_live_plan_execution_state"
        "   CHECK (state IN ('planned', 'executing', 'settled', 'blocked')),"
        " CONSTRAINT ck_live_plan_execution_lane CHECK (lane IN (" + _LANES + ")),"
        " CONSTRAINT fk_live_plan_execution_plan FOREIGN KEY (plan_id)"
        "   REFERENCES public.strategy_plans (plan_id) ON DELETE RESTRICT,"
        " CONSTRAINT fk_live_plan_execution_strategy"
        "   FOREIGN KEY (strategy_id, account_id)"
        "   REFERENCES public.strategies (id, account_scope) ON DELETE RESTRICT"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_plan_execution_state "
        "ON public.live_plan_executions (state)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_plan_execution_book "
        "ON public.live_plan_executions (account_id, strategy_id, execution_environment, state)"
    )

    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "DROP CONSTRAINT IF EXISTS ck_live_plan_submission_state"
    )
    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "ADD CONSTRAINT ck_live_plan_submission_state "
        "CHECK (state IN (" + _STEP_STATES_WITH_SEQUENCE + "))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "DROP CONSTRAINT IF EXISTS ck_live_plan_submission_state"
    )
    op.execute(
        "ALTER TABLE public.live_plan_submissions "
        "ADD CONSTRAINT ck_live_plan_submission_state "
        "CHECK (state IN (" + _STEP_STATES_WITHOUT_SEQUENCE + "))"
    )
    op.execute("DROP INDEX IF EXISTS public.idx_live_plan_execution_book")
    op.execute("DROP INDEX IF EXISTS public.idx_live_plan_execution_state")
    op.execute("DROP TABLE IF EXISTS public.live_plan_executions")
