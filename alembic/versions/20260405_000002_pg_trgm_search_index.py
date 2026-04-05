"""compatibility placeholder for reverted pg_trgm search migration

Revision ID: 20260405_000002
Revises: 20260330_000001
Create Date: 2026-04-05 00:00:02

This revision is intentionally kept as a no-op so environments that already
recorded this revision in alembic_version can continue to upgrade cleanly after
the pg_trgm instrument-search experiment was reverted.
"""

from __future__ import annotations


revision = "20260405_000002"
down_revision = "20260330_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
