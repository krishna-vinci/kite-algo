"""canvas layout persistence, keyed by namespaced node identity.

Revision ID: 20260912_000018
Revises: 20260912_000017
Create Date: 2026-09-12 00:00:18

Stores WHERE nodes sit on the visual canvas. It deliberately stores nothing
about what a node MEANS:

- the canvas is another editor of the same canonical document, not a second
  format (spec §3), so no node type, condition, or setting is persisted here —
  a layout row that could carry semantics would be a second source of truth for
  the definition, which is exactly what the plan forbids;
- consequently a cosmetic move cannot change the canonical hash, because the
  hash is computed from the document and nothing here reaches it. Spec §3
  ("canonical hashes exclude canvas/cosmetic metadata") holds by construction
  rather than by the hasher special-casing a `ui:` block — the alternative that
  Phase 4's hash-stability incident argues against.

**Why `node_id` is a namespaced identity, not a bare stage id.** Stage ids,
alert ids and channel names are three SEPARATE id spaces and may legally
collide: a stage `telegram_primary` and a channel `telegram_primary` can both
exist in one document. Keyed by a bare id, those two nodes would share a row, so
reordering or renaming the document could hand one node the other's saved
position — a silent visual corruption, and it would make "layout is independent
of the document" untrue. The namespace also lets alert and channel nodes hold
their own positions, which a stage-only key cannot express at all.

Purely additive: one new table, no change to any existing table, so it is safe
to apply before the code that reads it and needs no backfill. Existing workflows
simply have no layout rows and the canvas falls back to computed positions.
"""

from alembic import op

revision = "20260912_000018"
down_revision = "20260912_000017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.workflow_canvas_layout (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        workflow_id TEXT NOT NULL REFERENCES public.workflows(id) ON DELETE CASCADE,
        node_id TEXT NOT NULL,
        x DOUBLE PRECISION NOT NULL,
        y DOUBLE PRECISION NOT NULL,
        collapsed BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_canvas_layout_owner_workflow_node
            UNIQUE (owner_id, workflow_id, node_id)
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_canvas_layout_workflow
        ON public.workflow_canvas_layout (owner_id, workflow_id);
    """)


def downgrade() -> None:
    # Layout is purely cosmetic state: dropping it costs a redraw, never data
    # that cannot be recreated by moving a node again. The documents themselves
    # are untouched by both directions of this migration.
    op.execute("DROP TABLE IF EXISTS public.workflow_canvas_layout")
