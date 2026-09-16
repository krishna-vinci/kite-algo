"""delivery attempt provider acknowledgement id.

Revision ID: 20260912_000017
Revises: 20260911_000016
Create Date: 2026-09-12 00:00:17

Adds ONE nullable column: ``delivery_attempts.provider_id``.

Why it exists: the adapters already return a provider acknowledgement
(Telegram's ``message_id``, ntfy's ``X-Ntfy-Id``) but ``record_attempt``
discarded it, so the data needed to answer "did the provider actually accept
this message, and under which id" was unavailable to the operator — the
Phase 6 6A requirement to surface provider outcomes. It is the one place a
schema change was genuinely required rather than a new endpoint over existing
data.

Purely additive and nullable, so:

- existing rows keep working (``provider_id`` is simply NULL for history
  recorded before this migration — never backfilled, because the value was
  never captured and inventing one would be worse than an honest gap);
- no reader requires it;
- the downgrade is a plain column drop that loses only this captured metadata.
"""

from alembic import op

revision = "20260912_000017"
down_revision = "20260911_000016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.delivery_attempts "
        "ADD COLUMN IF NOT EXISTS provider_id VARCHAR(128)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.delivery_attempts DROP COLUMN IF EXISTS provider_id"
    )
