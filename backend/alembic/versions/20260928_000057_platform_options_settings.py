"""platform option-chain settings and append-only audit.

Revision ID: 20260928_000057
Revises: 20260927_000056
Create Date: 2026-09-28
"""

from alembic import op

revision = "20260928_000057"
down_revision = "20260927_000056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.platform_options_settings ("
        " settings_id INTEGER PRIMARY KEY,"
        " always_on JSONB NOT NULL DEFAULT '[]'::jsonb,"
        " cadence_sec INTEGER NOT NULL,"
        " tick_driven BOOLEAN NOT NULL,"
        " min_interval_sec NUMERIC(6,2) NOT NULL,"
        " idle_stop_minutes INTEGER NOT NULL,"
        " updated_by TEXT NOT NULL,"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " CONSTRAINT ck_platform_options_settings_singleton CHECK (settings_id = 1)"
        ")"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.platform_options_settings_audit ("
        " audit_id BIGSERIAL PRIMARY KEY,"
        " actor_id TEXT NOT NULL,"
        " reason TEXT,"
        " previous_always_on JSONB NOT NULL DEFAULT '[]'::jsonb,"
        " always_on JSONB NOT NULL DEFAULT '[]'::jsonb,"
        " previous_cadence_sec INTEGER NOT NULL,"
        " cadence_sec INTEGER NOT NULL,"
        " previous_tick_driven BOOLEAN NOT NULL,"
        " tick_driven BOOLEAN NOT NULL,"
        " previous_min_interval_sec NUMERIC(6,2) NOT NULL,"
        " min_interval_sec NUMERIC(6,2) NOT NULL,"
        " previous_idle_stop_minutes INTEGER NOT NULL,"
        " idle_stop_minutes INTEGER NOT NULL,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_options_settings_audit_created "
        "ON public.platform_options_settings_audit (created_at DESC)"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION forbid_platform_options_settings_audit_mutation() "
        "RETURNS trigger AS $$"
        "BEGIN "
        "RAISE EXCEPTION 'platform_options_settings_audit is append-only (insert-only)'; "
        "END;"
        "$$ LANGUAGE plpgsql"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_options_settings_audit_immutable "
        "ON public.platform_options_settings_audit"
    )
    op.execute(
        "CREATE TRIGGER trg_platform_options_settings_audit_immutable "
        "BEFORE UPDATE OR DELETE ON public.platform_options_settings_audit "
        "FOR EACH ROW EXECUTE FUNCTION forbid_platform_options_settings_audit_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_options_settings_audit_immutable "
        "ON public.platform_options_settings_audit"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS forbid_platform_options_settings_audit_mutation()"
    )
    op.execute("DROP TABLE IF EXISTS public.platform_options_settings_audit")
    op.execute("DROP TABLE IF EXISTS public.platform_options_settings")
