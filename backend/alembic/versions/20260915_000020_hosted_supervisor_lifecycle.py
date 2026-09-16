"""hosted supervisor lifecycle: durable credential-handoff markers.

Revision ID: 20260915_000020
Revises: 20260915_000019
Create Date: 2026-09-15 00:00:20

Purely additive: two nullable columns on ``strategy_jobs``. No existing table is
altered, no backfill is needed, and every pre-existing row gets ``NULL``.

- ``handoff_at``  — set exactly once, when a supervised launch has delivered the
  child configuration (run id + session nonce + one-time worker token) to the
  supervisor. It is the durable evidence that a credential was handed off, so a
  repeated preparation fails closed (fence/revoke) instead of minting a second
  live credential for the same attempt.
- ``last_error``  — a short, non-secret diagnostic recorded when preparation
  fails. It never contains credentials.

The token plaintext is never stored (only its hash lives in
``algo_worker_tokens``), so this column records *that* a handoff happened, never
the handed-off value.
"""

from alembic import op

revision = "20260915_000020"
down_revision = "20260915_000019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_jobs "
        "ADD COLUMN IF NOT EXISTS handoff_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE public.strategy_jobs "
        "ADD COLUMN IF NOT EXISTS last_error TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.strategy_jobs DROP COLUMN IF EXISTS last_error")
    op.execute("ALTER TABLE public.strategy_jobs DROP COLUMN IF EXISTS handoff_at")
