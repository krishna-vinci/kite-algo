"""Widen the plan-kind vocabularies to the kinds the compilers already ship.

Revision ID: 20260921_000035
Revises: 20260917_000034
Create Date: 2026-09-21

Why this exists:

- ``FuturesCompiler.target_kind`` is ``target_futures`` and
  ``OptionStructureCompiler.target_kind`` is ``option_structure``, and the
  proposal route writes ``plan_kind = compiled.target_kind``. Both kinds were
  therefore impossible to persist: the CHECKs allowed only
  ``single_instrument``/``target_weights``/``intent_bundle``, so a valid
  futures or option-structure submission failed as a raw integrity error.
  Project 9/10 shipped compilers and ledger entries claiming those lanes were
  complete while the storage vocabulary refused them.

- The change is purely additive: the allowed sets only grow, so every existing
  row stays valid and a downgrade restores the previous vocabulary (any row of
  a new kind is rejected, which is the truthful pre-change state).
"""

from alembic import op

revision = "20260921_000035"
down_revision = "20260917_000034"
branch_labels = None
depends_on = None

_WIDENED = "'single_instrument','target_weights','intent_bundle','target_futures','option_structure'"
_NARROW = "'single_instrument','target_weights','intent_bundle'"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_proposals DROP CONSTRAINT ck_proposals_target_kind"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals ADD CONSTRAINT ck_proposals_target_kind "
        f"CHECK (target_kind IN ({_WIDENED}))"
    )
    op.execute("ALTER TABLE public.strategy_plans DROP CONSTRAINT ck_plans_plan_kind")
    op.execute(
        "ALTER TABLE public.strategy_plans ADD CONSTRAINT ck_plans_plan_kind "
        f"CHECK (plan_kind IN ({_WIDENED}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.strategy_plans DROP CONSTRAINT ck_plans_plan_kind")
    op.execute(
        "ALTER TABLE public.strategy_plans ADD CONSTRAINT ck_plans_plan_kind "
        f"CHECK (plan_kind IN ({_NARROW}))"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals DROP CONSTRAINT ck_proposals_target_kind"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals ADD CONSTRAINT ck_proposals_target_kind "
        f"CHECK (target_kind IN ({_NARROW}))"
    )
