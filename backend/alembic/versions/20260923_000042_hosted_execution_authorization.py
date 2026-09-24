"""Governed hosted execution: authorization mode, grants, requests, audit.

Revision ID: 20260923_000042
Revises: 20260922_000041
Create Date: 2026-09-23

Purely additive. Four things become representable, and nothing that already
exists changes meaning:

1. ``hosted_strategies.authorization_mode`` — ``approval_based`` (default) or
   ``autonomous``. Selecting ``autonomous`` authorises nothing by itself; it only
   makes an owner-issued grant usable.
2. ``hosted_execution_grants`` — the immutable, owner-issued standing
   authorisation binding canonical/hosted strategy, immutable version id and
   source hash, account, environment and the canonical hash of the validated
   admission + mandatory run-protection policy. ``uq_hosted_execution_grant_active``
   is a PARTIAL unique index, so "one active grant per strategy/account/
   environment" is a database fact rather than a read-then-write race. Triggers
   forbid identity mutation, deletion, and reactivation after revocation.
3. ``hosted_execution_requests`` — the durable, idempotent request identity for
   one plan, with its decision evidence, reservation/approval links and durable
   dispatch claim.
4. ``hosted_execution_audit`` — append-only authorisation/dispatch audit.

Also additive on an existing table: ``strategy_approvals.actor_kind`` (default
``manual``) and ``authorization_evidence``. An autonomous decision is recorded
as ``automatic`` with grant-derived evidence, so it can never be read as a
forged manual click.

Downgrade drops exactly what the upgrade created and will fail while governed
rows exist, which is the correct fix-forward posture for an audit trail.
"""

from alembic import op

revision = "20260923_000042"
down_revision = "20260922_000041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- 1. authorization mode on the hosted strategy -----------------------
    op.execute(
        "ALTER TABLE public.hosted_strategies "
        "ADD COLUMN IF NOT EXISTS authorization_mode TEXT NOT NULL DEFAULT 'approval_based'"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategies "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategies_authorization_mode"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategies "
        "ADD CONSTRAINT ck_hosted_strategies_authorization_mode "
        "CHECK (authorization_mode IN ('approval_based', 'autonomous'))"
    )

    # --- 2. approval provenance -------------------------------------------
    op.execute(
        "ALTER TABLE public.strategy_approvals "
        "ADD COLUMN IF NOT EXISTS actor_kind TEXT NOT NULL DEFAULT 'manual'"
    )
    op.execute(
        "ALTER TABLE public.strategy_approvals "
        "ADD COLUMN IF NOT EXISTS authorization_evidence JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE public.strategy_approvals "
        "DROP CONSTRAINT IF EXISTS ck_appr_actor_kind"
    )
    op.execute(
        "ALTER TABLE public.strategy_approvals "
        "ADD CONSTRAINT ck_appr_actor_kind CHECK (actor_kind IN ('manual', 'automatic'))"
    )

    # --- 3. the immutable grant -------------------------------------------
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.hosted_execution_grants ("
        " grant_id TEXT PRIMARY KEY,"
        " owner_id TEXT NOT NULL,"
        " strategy_id TEXT NOT NULL,"
        " canonical_strategy_id TEXT NOT NULL,"
        " version_id TEXT NOT NULL,"
        " version_number INTEGER NOT NULL,"
        " source_sha256 TEXT NOT NULL,"
        " account_id TEXT NOT NULL,"
        " execution_environment TEXT NOT NULL,"
        " policy_hash TEXT NOT NULL,"
        " policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " issued_by TEXT NOT NULL,"
        " issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " expires_at TIMESTAMPTZ,"
        " status TEXT NOT NULL DEFAULT 'active',"
        " revoked_by TEXT,"
        " revoked_at TIMESTAMPTZ,"
        " revocation_reason TEXT,"
        " superseded_by TEXT,"
        " superseded_at TIMESTAMPTZ,"
        " supersession_reason TEXT,"
        " request_key TEXT NOT NULL,"
        " content_sha256 TEXT NOT NULL,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " CONSTRAINT uq_hosted_execution_grant_request"
        "   UNIQUE (owner_id, strategy_id, request_key),"
        " CONSTRAINT ck_hosted_execution_grant_status"
        "   CHECK (status IN ('active', 'revoked', 'superseded')),"
        " CONSTRAINT ck_hosted_execution_grant_environment"
        "   CHECK (execution_environment IN ('paper', 'dry_run', 'live')),"
        " CONSTRAINT ck_hosted_execution_grant_version CHECK (version_number > 0),"
        " CONSTRAINT ck_hosted_execution_grant_revocation CHECK ("
        "   (status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL)"
        "   OR (status <> 'revoked' AND revoked_at IS NULL AND revoked_by IS NULL)),"
        " CONSTRAINT ck_hosted_execution_grant_supersession CHECK ("
        "   (status = 'superseded' AND superseded_by IS NOT NULL"
        "     AND superseded_at IS NOT NULL)"
        "   OR (status <> 'superseded' AND superseded_by IS NULL"
        "     AND superseded_at IS NULL)),"
        " CONSTRAINT fk_hosted_execution_grant_strategy_owner"
        "   FOREIGN KEY (strategy_id, owner_id)"
        "   REFERENCES public.hosted_strategies (id, owner_id) ON DELETE CASCADE,"
        " CONSTRAINT fk_hosted_execution_grant_version_strategy"
        "   FOREIGN KEY (version_id, strategy_id)"
        "   REFERENCES public.hosted_strategy_versions (id, strategy_id)"
        "   ON DELETE RESTRICT,"
        " CONSTRAINT fk_hosted_execution_grant_canonical"
        "   FOREIGN KEY (canonical_strategy_id, account_id)"
        "   REFERENCES public.strategies (id, account_scope) ON DELETE RESTRICT"
        ")"
    )
    # The partial index is the second guard behind the row lock: even a caller
    # that bypassed the service cannot hold two active grants for one book.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_hosted_execution_grant_active "
        "ON public.hosted_execution_grants "
        "(strategy_id, account_id, execution_environment) WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_hosted_execution_grant_strategy "
        "ON public.hosted_execution_grants (strategy_id, created_at DESC)"
    )

    op.execute(
        "CREATE OR REPLACE FUNCTION forbid_hosted_execution_grant_identity_mutation() "
        "RETURNS trigger AS $$"
        "BEGIN"
        "    IF NEW.grant_id <> OLD.grant_id"
        "       OR NEW.owner_id <> OLD.owner_id"
        "       OR NEW.strategy_id <> OLD.strategy_id"
        "       OR NEW.canonical_strategy_id <> OLD.canonical_strategy_id"
        "       OR NEW.version_id <> OLD.version_id"
        "       OR NEW.version_number <> OLD.version_number"
        "       OR NEW.source_sha256 <> OLD.source_sha256"
        "       OR NEW.account_id <> OLD.account_id"
        "       OR NEW.execution_environment <> OLD.execution_environment"
        "       OR NEW.policy_hash <> OLD.policy_hash"
        "       OR NEW.issued_by <> OLD.issued_by"
        "       OR NEW.request_key <> OLD.request_key"
        "       OR NEW.content_sha256 <> OLD.content_sha256 THEN"
        "        RAISE EXCEPTION "
        "'hosted_execution_grants identity is immutable (issue a new grant)';"
        "    END IF;"
        "    IF OLD.status <> 'active' AND NEW.status = 'active' THEN"
        "        RAISE EXCEPTION "
        "'a revoked or superseded hosted execution grant cannot be reactivated';"
        "    END IF;"
        "    RETURN NEW;"
        "END;"
        "$$ LANGUAGE plpgsql"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_hosted_execution_grant_identity "
        "ON public.hosted_execution_grants"
    )
    op.execute(
        "CREATE TRIGGER trg_hosted_execution_grant_identity "
        "BEFORE UPDATE ON public.hosted_execution_grants "
        "FOR EACH ROW EXECUTE FUNCTION forbid_hosted_execution_grant_identity_mutation()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION forbid_hosted_execution_grant_delete() "
        "RETURNS trigger AS $$"
        "BEGIN"
        "    RAISE EXCEPTION "
        "'hosted_execution_grants are never deleted (revoke or supersede instead)';"
        "END;"
        "$$ LANGUAGE plpgsql"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_hosted_execution_grant_no_delete "
        "ON public.hosted_execution_grants"
    )
    op.execute(
        "CREATE TRIGGER trg_hosted_execution_grant_no_delete "
        "BEFORE DELETE ON public.hosted_execution_grants "
        "FOR EACH ROW EXECUTE FUNCTION forbid_hosted_execution_grant_delete()"
    )

    # --- 4. the durable execution request ---------------------------------
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.hosted_execution_requests ("
        " request_id TEXT PRIMARY KEY,"
        " owner_id TEXT NOT NULL,"
        " strategy_id TEXT NOT NULL,"
        " canonical_strategy_id TEXT NOT NULL,"
        " account_id TEXT NOT NULL,"
        " execution_environment TEXT NOT NULL,"
        " strategy_run_id TEXT NOT NULL,"
        " job_id TEXT,"
        " token_id TEXT,"
        " attempt INTEGER,"
        " lease_epoch BIGINT,"
        " version_id TEXT NOT NULL,"
        " version_number INTEGER,"
        " source_sha256 TEXT NOT NULL,"
        " policy_hash TEXT NOT NULL,"
        " evaluation_id TEXT,"
        " plan_id UUID NOT NULL,"
        " plan_hash TEXT NOT NULL,"
        " authorization_mode TEXT NOT NULL,"
        " grant_id TEXT,"
        " status TEXT NOT NULL DEFAULT 'requested',"
        " refusal_code TEXT,"
        " refusal_detail JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " decision_kind TEXT,"
        " decision_actor TEXT,"
        " decision_at TIMESTAMPTZ,"
        " decision_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " approval_id UUID,"
        " reservation_id UUID,"
        " execution_detail JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " dispatch_claim_id TEXT,"
        " dispatch_claimed_at TIMESTAMPTZ,"
        " dispatch_started_at TIMESTAMPTZ,"
        " dispatch_finished_at TIMESTAMPTZ,"
        " idempotency_key TEXT NOT NULL,"
        " request_hash TEXT NOT NULL,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " CONSTRAINT uq_hosted_execution_requests_key"
        "   UNIQUE (owner_id, plan_id, idempotency_key),"
        " CONSTRAINT ck_hosted_execution_request_status CHECK (status IN ("
        "   'requested', 'awaiting_approval', 'queued', 'dispatching',"
        "   'executed', 'refused', 'rejected', 'dispatch_unresolved')),"
        " CONSTRAINT ck_hosted_execution_request_mode"
        "   CHECK (authorization_mode IN ('approval_based', 'autonomous')),"
        " CONSTRAINT ck_hosted_execution_request_environment"
        "   CHECK (execution_environment IN ('paper', 'dry_run', 'live')),"
        " CONSTRAINT fk_hosted_execution_request_strategy_owner"
        "   FOREIGN KEY (strategy_id, owner_id)"
        "   REFERENCES public.hosted_strategies (id, owner_id) ON DELETE CASCADE,"
        " CONSTRAINT fk_hosted_execution_request_canonical"
        "   FOREIGN KEY (canonical_strategy_id, account_id)"
        "   REFERENCES public.strategies (id, account_scope) ON DELETE RESTRICT,"
        " CONSTRAINT fk_hosted_execution_request_plan"
        "   FOREIGN KEY (plan_id) REFERENCES public.strategy_plans (plan_id)"
        "   ON DELETE RESTRICT,"
        " CONSTRAINT fk_hosted_execution_request_grant"
        "   FOREIGN KEY (grant_id) REFERENCES public.hosted_execution_grants (grant_id)"
        "   ON DELETE RESTRICT,"
        " CONSTRAINT fk_hosted_execution_request_approval"
        "   FOREIGN KEY (approval_id) REFERENCES public.strategy_approvals (approval_id)"
        "   ON DELETE RESTRICT,"
        " CONSTRAINT fk_hosted_execution_request_reservation"
        "   FOREIGN KEY (reservation_id)"
        "   REFERENCES public.strategy_reservations (reservation_id) ON DELETE RESTRICT"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_hosted_execution_request_strategy "
        "ON public.hosted_execution_requests (strategy_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_hosted_execution_request_state "
        "ON public.hosted_execution_requests (status, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_hosted_execution_request_run "
        "ON public.hosted_execution_requests (strategy_run_id, created_at DESC)"
    )

    # --- 5. append-only audit ---------------------------------------------
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.hosted_execution_audit ("
        " audit_id BIGSERIAL PRIMARY KEY,"
        " owner_id TEXT NOT NULL,"
        " strategy_id TEXT NOT NULL,"
        " subject_kind TEXT NOT NULL,"
        " subject_id TEXT NOT NULL,"
        " event TEXT NOT NULL,"
        " actor_id TEXT NOT NULL,"
        " actor_kind TEXT NOT NULL,"
        " detail JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " CONSTRAINT ck_hosted_execution_audit_subject"
        "   CHECK (subject_kind IN ('grant', 'mode', 'request', 'dispatch')),"
        " CONSTRAINT ck_hosted_execution_audit_actor_kind"
        "   CHECK (actor_kind IN ('owner', 'system', 'automatic_grant'))"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_hosted_execution_audit_strategy "
        "ON public.hosted_execution_audit (strategy_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_hosted_execution_audit_subject "
        "ON public.hosted_execution_audit (subject_kind, subject_id)"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION forbid_hosted_execution_audit_mutation() "
        "RETURNS trigger AS $$"
        "BEGIN"
        "    RAISE EXCEPTION 'hosted_execution_audit is append-only (insert-only)';"
        "END;"
        "$$ LANGUAGE plpgsql"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_hosted_execution_audit_immutable "
        "ON public.hosted_execution_audit"
    )
    op.execute(
        "CREATE TRIGGER trg_hosted_execution_audit_immutable "
        "BEFORE UPDATE OR DELETE ON public.hosted_execution_audit "
        "FOR EACH ROW EXECUTE FUNCTION forbid_hosted_execution_audit_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_hosted_execution_audit_immutable "
        "ON public.hosted_execution_audit"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_hosted_execution_audit_mutation()")
    op.execute("DROP TABLE IF EXISTS public.hosted_execution_audit")
    op.execute("DROP TABLE IF EXISTS public.hosted_execution_requests")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_hosted_execution_grant_no_delete "
        "ON public.hosted_execution_grants"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_hosted_execution_grant_identity "
        "ON public.hosted_execution_grants"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_hosted_execution_grant_delete()")
    op.execute("DROP FUNCTION IF EXISTS forbid_hosted_execution_grant_identity_mutation()")
    op.execute("DROP TABLE IF EXISTS public.hosted_execution_grants")
    op.execute(
        "ALTER TABLE public.strategy_approvals "
        "DROP CONSTRAINT IF EXISTS ck_appr_actor_kind"
    )
    op.execute(
        "ALTER TABLE public.strategy_approvals DROP COLUMN IF EXISTS authorization_evidence"
    )
    op.execute("ALTER TABLE public.strategy_approvals DROP COLUMN IF EXISTS actor_kind")
    op.execute(
        "ALTER TABLE public.hosted_strategies "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategies_authorization_mode"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategies DROP COLUMN IF EXISTS authorization_mode"
    )
