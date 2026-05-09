"""baseline schema bootstrap

Revision ID: 20260330_000001
Revises:
Create Date: 2026-03-30 00:00:01
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "20260330_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema_path = Path(__file__).resolve().parents[2] / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")
    op.execute(schema_sql)


def downgrade() -> None:
    pass
