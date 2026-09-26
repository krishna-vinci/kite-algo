"""Shared facts about Kite's exchange-level order autoslicing."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import text

AUTOSLICE_EXCHANGES = frozenset({"NFO", "BFO", "MCX", "CDS"})
AUTOSLICE_TAG_PREFIX = "autoslice:"
EVENT_PROCESSOR_LOCK_ID = 87234101


def should_autoslice(exchange: Any) -> bool:
    """Autoslice applies only to derivatives and commodity orders."""
    return str(exchange or "").strip().upper() in AUTOSLICE_EXCHANGES


def autoslice_parent_id(payload: Mapping[str, Any]) -> Optional[str]:
    """Return the submitted parent id from a Kite autoslice child's tags."""
    tags: Iterable[Any]
    raw_tags = payload.get("tags")
    if isinstance(raw_tags, str):
        try:
            decoded = json.loads(raw_tags)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = raw_tags
        tags = decoded if isinstance(decoded, list) else [decoded]
    elif isinstance(raw_tags, (list, tuple, set)):
        tags = raw_tags
    else:
        return None
    for tag in tags:
        value = str(tag or "")
        if value.startswith(AUTOSLICE_TAG_PREFIX):
            parent_id = value[len(AUTOSLICE_TAG_PREFIX) :].strip()
            if parent_id:
                return parent_id
    return None


def bound_run_for_plan(session_factory, plan_id: str) -> Optional[str]:
    """The live run binding used to scope autoslice child discovery."""
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT sp.strategy_run_id
                FROM public.strategy_plans pl
                JOIN public.strategy_proposals sp ON sp.proposal_id = pl.proposal_id
                JOIN public.strategy_run_bindings b ON b.strategy_run_id = sp.strategy_run_id
                WHERE pl.plan_id = :plan_id
                  AND b.execution_environment = 'live'
                """
            ),
            {"plan_id": str(plan_id)},
        ).first()
    return str(row[0]) if row else None


def autoslice_child_order_ids(
    session_factory,
    *,
    account_id: str,
    run_id: str,
    parent_order_ids: Sequence[str],
) -> list[str]:
    """Discover linked children whose Kite tags name these submitted parents."""
    parents = sorted({str(value or "").strip() for value in parent_order_ids if str(value or "").strip()})
    if not account_id or not run_id or not parents:
        return []
    needles = [f"%{AUTOSLICE_TAG_PREFIX}{parent}%" for parent in parents]
    with session_factory() as session:
        rows = session.execute(
            text(
                """
                SELECT DISTINCT coe.order_id, coe.payload_json
                FROM public.canonical_order_events coe
                WHERE coe.account_id = :account_id
                  AND coe.order_id <> ALL(:parents)
                  AND (coe.payload_json ->> 'tags')::text LIKE ANY(:needles)
                  AND (
                      EXISTS (
                          SELECT 1
                          FROM public.live_order_intents loi
                          WHERE loi.account_id = coe.account_id
                            AND loi.broker_order_id = coe.order_id
                            AND loi.strategy_run_id = :run_id
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM public.worker_live_execution_links wl
                          WHERE wl.account_id = coe.account_id
                            AND wl.broker_order_id = coe.order_id
                            AND wl.strategy_run_id = :run_id
                      )
                  )
                """
            ),
            {
                "account_id": str(account_id),
                "run_id": str(run_id),
                "parents": parents,
                "needles": needles,
            },
        ).fetchall()

    children: set[str] = set()
    for order_id, payload_json in rows:
        if isinstance(payload_json, Mapping):
            payload = dict(payload_json)
        elif isinstance(payload_json, str):
            try:
                decoded = json.loads(payload_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            payload = decoded if isinstance(decoded, Mapping) else {}
        else:
            continue
        parent_id = autoslice_parent_id(payload)
        if parent_id in set(parents):
            children.add(str(order_id or ""))
    return sorted(value for value in children if value)


def recover_autoslice_children(
    session_factory,
    *,
    account_id: str,
    run_id: str,
    parent_order_id: str,
) -> int:
    """Link and requeue tagged children observed before the parent was marked.

    The broker response and its database bookkeeping are separate operations, so
    Kite may deliver a child event in between. This closes that window with the
    parent's now-authoritative ownership: insert the missing child links and put
    already-processed child events back in the processor queue in one transaction.
    """
    parent = str(parent_order_id or "").strip()
    run = str(run_id or "").strip()
    account = str(account_id or "").strip()
    if not parent or not run or not account:
        return 0
    needle = f"%{AUTOSLICE_TAG_PREFIX}{parent}%"
    session = session_factory()
    try:
        # Serialize with canonical-event processing: a child claimed just before
        # parent marking may be in flight, and its final write must not overwrite
        # this recovery's pending state.
        session.execute(
            text("SELECT pg_advisory_xact_lock(:event_processor_lock_id)"),
            {"event_processor_lock_id": EVENT_PROCESSOR_LOCK_ID},
        )
        session.execute(
            text(
                """
                INSERT INTO public.worker_live_execution_links (
                    strategy_run_id, account_id, broker_order_id, trade_id
                )
                SELECT DISTINCT :run_id, coe.account_id, coe.order_id, NULL
                FROM public.canonical_order_events coe
                WHERE coe.account_id = :account_id
                  AND coe.order_id <> :parent_order_id
                  AND (coe.payload_json ->> 'tags')::text LIKE :needle
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.worker_live_execution_links owner
                      WHERE owner.account_id = coe.account_id
                        AND owner.broker_order_id = coe.order_id
                        AND owner.trade_id IS NULL
                  )
                ON CONFLICT (account_id, broker_order_id) WHERE trade_id IS NULL
                DO NOTHING
                """
            ),
            {
                "run_id": run,
                "account_id": account,
                "parent_order_id": parent,
                "needle": needle,
            },
        )
        result = session.execute(
            text(
                """
                UPDATE public.canonical_order_events coe
                SET processing_state = 'pending',
                    processing_started_at = NULL,
                    last_error = NULL
                WHERE coe.account_id = :account_id
                  AND coe.order_id <> :parent_order_id
                  AND (coe.payload_json ->> 'tags')::text LIKE :needle
                  AND coe.processing_state IN ('processed', 'failed')
                  AND EXISTS (
                      SELECT 1
                      FROM public.worker_live_execution_links owner
                      WHERE owner.account_id = coe.account_id
                        AND owner.broker_order_id = coe.order_id
                        AND owner.trade_id IS NULL
                        AND owner.strategy_run_id = :run_id
                  )
                """
            ),
            {
                "run_id": run,
                "account_id": account,
                "parent_order_id": parent,
                "needle": needle,
            },
        )
        requeued = int(getattr(result, "rowcount", 0) or 0)
        session.commit()
        return requeued
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def merge_order_ids(*groups: Sequence[str]) -> list[str]:
    """Keep the step's evidence order stable while adding discovered children."""
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for value in group:
            order_id = str(value or "").strip()
            if order_id and order_id not in seen:
                seen.add(order_id)
                merged.append(order_id)
    return merged
