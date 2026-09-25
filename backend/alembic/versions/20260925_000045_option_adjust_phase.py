"""option-run binding: admit the ``adjust`` phase on the plan->run edge.

Revision ID: 20260925_000045
Revises: 20260925_000044
Create Date: 2026-09-25

The adjust engine mutates an existing option run through a versioned leg set, so
a frozen ``adjust`` plan needs the same durable edge an entry and an exit already
have: one row in ``strategy_plan_option_runs`` naming the run it acts on and the
phase that produced it. ``ck_plan_option_run_phase`` admitted ``entry`` and
``exit`` only, so the phase vocabulary is widened by one value.

The row is the same shape the exit edge writes and the uniqueness rule is
unchanged: ``plan_id`` is the primary key (one plan resolves to one run, so a
retry returns the same edge) and only the ``entry`` phase is capped at one per
run by the partial unique index the previous revision created. Everything the
phase column can now say is a claim about a MUTATION of a run the platform
already owns; nothing is backfilled and no other object is altered.
"""

from alembic import op

revision = "20260925_000045"
down_revision = "20260925_000044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_plan_option_runs "
        "DROP CONSTRAINT IF EXISTS ck_plan_option_run_phase"
    )
    op.execute(
        "ALTER TABLE public.strategy_plan_option_runs "
        "ADD CONSTRAINT ck_plan_option_run_phase "
        "CHECK (phase IN ('entry', 'exit', 'adjust'))"
    )


def downgrade() -> None:
    # Narrowing the CHECK can only succeed while no ``adjust`` edge exists, and
    # the constraint itself is the guard: a row the adjust path wrote is never
    # silently dropped or relabelled as an exit. The operator archives
    # deliberately and re-runs the downgrade, which is the same fix-forward
    # posture 000043 and 000044 take for their own widenings.
    op.execute(
        "ALTER TABLE public.strategy_plan_option_runs "
        "DROP CONSTRAINT IF EXISTS ck_plan_option_run_phase"
    )
    op.execute(
        "ALTER TABLE public.strategy_plan_option_runs "
        "ADD CONSTRAINT ck_plan_option_run_phase "
        "CHECK (phase IN ('entry', 'exit'))"
    )
