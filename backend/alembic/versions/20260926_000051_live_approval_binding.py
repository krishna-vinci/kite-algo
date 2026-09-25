"""live approval binding to version, generation and protection policy (C1.2 S3).

Revision ID: 20260926_000051
Revises: 20260925_000050
Create Date: 2026-09-26

Purely additive on ``public.strategy_approvals``. S3 (design section "Approval
binding and invalidation") extends what one owner authorisation is bound to, so
an approval that outlives its inputs is refused by NAME at release instead of
trading an artifact that no longer describes the strategy:

* ``strategy_version_id`` / ``version_number`` / ``source_sha256`` /
  ``policy_hash`` — the immutable version identity the originating
  ``HostedExecutionRequest`` pinned, so a version, source or policy change is a
  named refusal rather than a silent re-interpretation;
* ``option_run_id`` / ``based_on_generation`` / ``protection_policy_version`` —
  the frozen option target, its generation basis and the B2.4 owner policy the
  approval was granted against;
* ``reserved_option_generation`` — "this approval owns the right to move the run
  FROM this generation".

``uq_approvals_option_generation_active`` makes "two approvals own one option run
generation" a database fact, not a read-then-write race: it is a PARTIAL unique
index over ``(option_run_id, reserved_option_generation)`` restricted to ACTIVE
rows, and only plans that MOVE a generation (an adjust/roll) carry a reservation,
so several exit plans may still reference one run. The downgrade drops exactly
what the upgrade created, so a prior-head database is restored verbatim.
"""

from alembic import op

revision = "20260926_000051"
down_revision = "20260925_000050"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("strategy_version_id", "TEXT"),
    ("version_number", "INTEGER"),
    ("source_sha256", "TEXT"),
    ("policy_hash", "TEXT"),
    ("option_run_id", "TEXT"),
    ("based_on_generation", "BIGINT"),
    ("protection_policy_version", "TEXT"),
    ("reserved_option_generation", "BIGINT"),
)


def upgrade() -> None:
    for name, sql_type in _COLUMNS:
        op.execute(
            f"ALTER TABLE public.strategy_approvals "
            f"ADD COLUMN IF NOT EXISTS {name} {sql_type}"
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_approvals_option_run "
        "ON public.strategy_approvals (option_run_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_approvals_option_generation_active "
        "ON public.strategy_approvals (option_run_id, reserved_option_generation) "
        "WHERE status = 'active' AND option_run_id IS NOT NULL "
        "AND reserved_option_generation IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.uq_approvals_option_generation_active")
    op.execute("DROP INDEX IF EXISTS public.idx_approvals_option_run")
    for name, _sql_type in _COLUMNS:
        op.execute(
            f"ALTER TABLE public.strategy_approvals DROP COLUMN IF EXISTS {name}"
        )
