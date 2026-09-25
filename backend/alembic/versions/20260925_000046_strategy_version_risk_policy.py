"""strategy versions: declare a per-version risk policy (B2.5).

Revision ID: 20260925_000046
Revises: 20260925_000045
Create Date: 2026-09-25

A hosted strategy VERSION now declares its own risk policy - max loss, notional
and margin limits, protection, an expiry-policy allow-list, the structure
families it may trade and whether a naked structure is permitted. The policy is
frozen with the immutable version and enforced at admission as
``effective = min(declared, operator policy, platform ceiling)``; ceilings only
tighten.

The column is NULLABLE and carries no default: a version written before this
revision (or one that simply does not declare a policy) has no policy at all,
which is exactly the state the options lane refuses by name
(``STRATEGY_RISK_POLICY_MISSING``). Nothing is backfilled - inventing a policy
for an existing version would widen the exposure that version was reviewed with.
"""

from alembic import op

revision = "20260925_000046"
down_revision = "20260925_000045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.hosted_strategy_versions "
        "ADD COLUMN IF NOT EXISTS risk_policy JSONB"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.hosted_strategy_versions "
        "DROP COLUMN IF EXISTS risk_policy"
    )
