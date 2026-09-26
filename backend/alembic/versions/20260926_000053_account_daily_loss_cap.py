"""account-wide daily loss cap on the platform live settings (P2 UX).

Revision ID: 20260926_000053
Revises: 20260926_000052
Create Date: 2026-09-26

Purely additive. The account-wide day-loss cap is another OPTIONAL field on the
same single ``platform_live_settings`` row the per-lane map lives on, so one
operator surface owns both "which lanes may take new exposure" and "how much may
the whole account lose today". The append-only audit table grows the matching
before/after columns, so a cap change is legible with its author.

Nothing here arms live trading or flattens anything: admission reads the cap and
refuses an exposure-INCREASING live plan once the broker's own day P&L is at or
below it (``backend.strategies.daily_loss``). The downgrade drops exactly the
columns the upgrade added, so a prior-head database is restored verbatim.
"""

from alembic import op

revision = "20260926_000053"
down_revision = "20260926_000052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.platform_live_settings "
        "ADD COLUMN IF NOT EXISTS account_daily_loss_cap_inr NUMERIC(18,2)"
    )
    op.execute(
        "ALTER TABLE public.platform_live_settings_audit "
        "ADD COLUMN IF NOT EXISTS previous_account_daily_loss_cap_inr NUMERIC(18,2)"
    )
    op.execute(
        "ALTER TABLE public.platform_live_settings_audit "
        "ADD COLUMN IF NOT EXISTS account_daily_loss_cap_inr NUMERIC(18,2)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.platform_live_settings_audit "
        "DROP COLUMN IF EXISTS account_daily_loss_cap_inr"
    )
    op.execute(
        "ALTER TABLE public.platform_live_settings_audit "
        "DROP COLUMN IF EXISTS previous_account_daily_loss_cap_inr"
    )
    op.execute(
        "ALTER TABLE public.platform_live_settings "
        "DROP COLUMN IF EXISTS account_daily_loss_cap_inr"
    )
