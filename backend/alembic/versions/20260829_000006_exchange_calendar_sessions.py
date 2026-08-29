"""audited exchange calendar sessions

Revision ID: 20260829_000006
Revises: 20260501_000005
Create Date: 2026-08-29 00:00:06
"""
from alembic import op

revision = "20260829_000006"
down_revision = "20260501_000005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE public.exchange_calendar_source_documents (
        source_document_id BIGSERIAL PRIMARY KEY, exchange TEXT NOT NULL, segment TEXT NOT NULL,
        official_source_reference TEXT NOT NULL,
        official_source_document_sha256 CHAR(64) NOT NULL,
        canonical_csv_sha256 CHAR(64) NOT NULL,
        parser_version TEXT NOT NULL,
        calendar_version BIGINT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
        imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), supersedes_calendar_version BIGINT,
        UNIQUE(exchange, segment, calendar_version),
        UNIQUE(exchange, segment, official_source_document_sha256, canonical_csv_sha256))""")
    op.execute("""CREATE TABLE public.exchange_calendar_sessions (
        exchange TEXT NOT NULL, segment TEXT NOT NULL, session_date DATE NOT NULL, calendar_version BIGINT NOT NULL,
        session_type TEXT NOT NULL CHECK (session_type IN ('REGULAR','HOLIDAY','SPECIAL')), opens_at TIME, closes_at TIME,
        verified BOOLEAN NOT NULL, source_document_id BIGINT NOT NULL REFERENCES public.exchange_calendar_source_documents(source_document_id),
        PRIMARY KEY(exchange, segment,session_date,calendar_version))""")


def downgrade() -> None:
    op.execute("DROP TABLE public.exchange_calendar_sessions")
    op.execute("DROP TABLE public.exchange_calendar_source_documents")
