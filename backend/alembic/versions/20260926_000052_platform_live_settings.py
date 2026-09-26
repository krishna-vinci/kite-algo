"""platform live-lane settings and their append-only audit (P2 UX).

Revision ID: 20260926_000052
Revises: 20260926_000051
Create Date: 2026-09-26

Purely additive. Until now the per-lane live allowlist lived only in the
deployment environment (``HOSTED_LIVE_LANES``), so a lane change needed a
redeploy. Two tables move the operator's answer into the database:

1. ``platform_live_settings`` - a SINGLE row (``settings_id = 1``, enforced by a
   CHECK) holding ``lanes`` as ``JSONB``. An absent row is not "no policy": the
   reader then keeps the deployment env allowlist, which is default-deny.
2. ``platform_live_settings_audit`` - append-only (insert-only trigger, the same
   posture as ``hosted_execution_audit``) with the actor, the reason and the lane
   map before and after, so a lane change can never be rewritten or erased.

Nothing here arms live trading. ``HOSTED_LIVE_ENABLED`` still gates ALL live
execution, this row only decides which lanes may take NEW exposure, and
reductions, exits, the MIS square-off, repair and flatten are never gated by it.
The downgrade drops exactly what the upgrade created, so a prior-head database
is restored verbatim.
"""

from alembic import op

revision = "20260926_000052"
down_revision = "20260926_000051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.platform_live_settings ("
        " settings_id INTEGER PRIMARY KEY,"
        " lanes JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " updated_by TEXT NOT NULL,"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " CONSTRAINT ck_platform_live_settings_singleton CHECK (settings_id = 1)"
        ")"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.platform_live_settings_audit ("
        " audit_id BIGSERIAL PRIMARY KEY,"
        " actor_id TEXT NOT NULL,"
        " reason TEXT,"
        " previous_lanes JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " lanes JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_live_settings_audit_created "
        "ON public.platform_live_settings_audit (created_at DESC)"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION forbid_platform_live_settings_audit_mutation() "
        "RETURNS trigger AS $$"
        "BEGIN"
        "    RAISE EXCEPTION "
        "'platform_live_settings_audit is append-only (insert-only)';"
        "END;"
        "$$ LANGUAGE plpgsql"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_live_settings_audit_immutable "
        "ON public.platform_live_settings_audit"
    )
    op.execute(
        "CREATE TRIGGER trg_platform_live_settings_audit_immutable "
        "BEFORE UPDATE OR DELETE ON public.platform_live_settings_audit "
        "FOR EACH ROW EXECUTE FUNCTION forbid_platform_live_settings_audit_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_live_settings_audit_immutable "
        "ON public.platform_live_settings_audit"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_platform_live_settings_audit_mutation()")
    op.execute("DROP TABLE IF EXISTS public.platform_live_settings_audit")
    op.execute("DROP TABLE IF EXISTS public.platform_live_settings")
