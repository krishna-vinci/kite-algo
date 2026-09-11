"""baseline schema bootstrap

Revision ID: 20260330_000001
Revises:
Create Date: 2026-03-30 00:00:01

Executes the FROZEN baseline snapshot, NOT the evolving ``backend/schema.sql``.

This migration used to run ``schema.sql`` verbatim. Because that file keeps
growing, it eventually contained statements owned by LATER migrations in this
same chain (the alert/universe/screener/Phase 4 blocks), so a from-zero
``alembic upgrade head`` executed them before their tables existed and aborted
at the first one. Keeping the baseline frozen and the evolving DDL separate is
what makes an empty-database upgrade work again; an already-migrated database
is unaffected because this revision is already applied there.

See ``backend/alembic/baseline_schema.sql`` for the frozen snapshot and the
list of blocks it deliberately excludes.
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "20260330_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen snapshot: adding new schema here would recreate the very problem
    # this split fixes. New changes go in a new migration (and schema.sql).
    baseline_path = Path(__file__).resolve().parents[1] / "baseline_schema.sql"
    baseline_sql = baseline_path.read_text(encoding="utf-8")
    op.execute(baseline_sql)


def downgrade() -> None:
    pass
