"""Canvas layout storage: positions for canvas nodes, and nothing else.

Separated from the operator router because the node-identity rules are the
interesting part and they deserve their own tests, without HTTP.

The contract this module enforces, and why each rule exists:

1. **A node id is `<namespace>:<local id>`.** Valid namespaces are ``stage``,
   ``alert`` and ``channel`` — the three id spaces a rendered workflow has.
   Unknown namespaces are rejected (422) rather than stored, because a stored
   unknown namespace is a layout row that can never be matched to a node: it
   would silently do nothing while looking like saved state.

2. **The namespace is load-bearing, not decoration.** Stage ids, alert ids and
   channel names can collide (a stage named ``telegram_primary`` beside a
   channel of the same name is legal). Keyed by the bare local id, those two
   nodes would share one row and a reorder could hand one node the other's
   position. The namespace is what makes the two distinct rows.

3. **Layout never touches the document.** Writing positions here creates no
   revision, computes no hash and cannot change one — so a cosmetic move is
   provably hash-neutral. That is why this is a table and not a `ui:` block
   inside the document (§3).

4. **Coordinates are finite and bounded.** NaN and infinity are refused (they
   would round-trip through JSON as ``null`` and break the canvas on reload),
   and absurd magnitudes are refused so one bad client cannot store values that
   make every later render compute nonsense.

5. **A write is a merge, not a replacement.** The canvas saves nodes
   individually; sending only the moved node must not delete positions for the
   rest. Deletion is therefore explicit (:func:`delete_layout`) rather than an
   implicit consequence of omission.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import delete as sql_delete, select
from sqlalchemy.orm import Session

from backend.workflows.repository import Base

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    String,
    Text,
)

__all__ = [
    "CANVAS_NAMESPACES",
    "CanvasLayoutEntry",
    "CanvasLayoutRepository",
    "CanvasNodeIdError",
    "WorkflowCanvasLayout",
    "parse_node_id",
    "validate_coordinate",
]

#: The id spaces a rendered workflow has. Each is a distinct namespace because
#: the three can legally collide by name.
CANVAS_NAMESPACES = ("stage", "alert", "channel")

#: Refuse coordinates beyond this magnitude. Real canvases do not approach it,
#: and a value this large makes a subsequent auto-fit compute nonsense, so it is
#: a bound on the nonsense rather than a legitimate limit.
MAX_ABS_COORDINATE = 1_000_000.0

#: Bound the local id the same way the document parser bounds stage/alert ids,
#: so a layout row can never be longer-lived or stranger than a document field.
MAX_NODE_ID_LENGTH = 256


class CanvasNodeIdError(ValueError):
    """A node id is not a valid namespaced identity."""


def parse_node_id(raw: Any) -> str:
    """Validate and normalize a namespaced canvas node id.

    Accepts exactly ``<namespace>:<local id>`` with a known namespace and a
    non-empty local id, and returns the normalized string. Whitespace around the
    whole value is trimmed; the local id is NOT trimmed, because a document id
    containing a space is legal and trimming it would silently point the layout
    row at a different node than the one on screen.
    """
    if not isinstance(raw, str):
        raise CanvasNodeIdError(
            f"node id must be a string, got {type(raw).__name__}"
        )
    candidate = raw.strip()
    if not candidate:
        raise CanvasNodeIdError("node id must not be empty")
    if len(candidate) > MAX_NODE_ID_LENGTH:
        raise CanvasNodeIdError(
            f"node id exceeds {MAX_NODE_ID_LENGTH} characters"
        )
    namespace, separator, local = candidate.partition(":")
    if not separator:
        raise CanvasNodeIdError(
            "node id must be namespaced as '<namespace>:<id>' "
            f"(one of {', '.join(CANVAS_NAMESPACES)}) — a bare id would let a "
            "stage, an alert and a channel of the same name share one position"
        )
    if namespace not in CANVAS_NAMESPACES:
        raise CanvasNodeIdError(
            f"unknown node namespace {namespace!r}; "
            f"expected one of {', '.join(CANVAS_NAMESPACES)}"
        )
    if not local:
        raise CanvasNodeIdError(
            f"node id {candidate!r} has an empty {namespace!r} identifier"
        )
    return f"{namespace}:{local}"


def validate_coordinate(value: Any, axis: str) -> float:
    """A finite, bounded float; rejects NaN/Infinity and absurd magnitudes."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanvasNodeIdError(f"{axis} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise CanvasNodeIdError(f"{axis} must be finite, got {value!r}")
    if abs(number) > MAX_ABS_COORDINATE:
        raise CanvasNodeIdError(
            f"{axis} must be within ±{MAX_ABS_COORDINATE:g}, got {number:g}"
        )
    return number


class WorkflowCanvasLayout(Base):
    """One node's position. Position only — never semantics."""

    __tablename__ = "workflow_canvas_layout"

    id = Column(String, primary_key=True)
    owner_id = Column(Text, nullable=False)
    workflow_id = Column(String, nullable=False)
    node_id = Column(String(MAX_NODE_ID_LENGTH), nullable=False)
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    collapsed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class CanvasLayoutEntry:
    node_id: str
    x: float
    y: float
    collapsed: bool
    updated_at: Optional[str]


class CanvasLayoutRepository:
    """Owner-scoped layout reads and writes.

    ``owner_id`` is always the server-authorized scope, never a client value —
    a layout row is per owner, so passing a client-supplied owner here would let
    one operator reposition another's canvas.
    """

    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory

    def _session(self) -> Session:
        return self.session_factory()

    def list_layout(
        self, owner_id: str, workflow_id: str, *, db: Optional[Session] = None
    ) -> List[CanvasLayoutEntry]:
        if db is not None:
            return self._list(db, owner_id, workflow_id)
        session = self._session()
        try:
            return self._list(session, owner_id, workflow_id)
        finally:
            session.close()

    def _list(self, session: Session, owner_id: str, workflow_id: str) -> List[CanvasLayoutEntry]:
        rows = session.execute(
            select(WorkflowCanvasLayout)
            .where(
                WorkflowCanvasLayout.owner_id == owner_id,
                WorkflowCanvasLayout.workflow_id == workflow_id,
            )
            .order_by(WorkflowCanvasLayout.node_id.asc())
        ).scalars().all()
        return [
            CanvasLayoutEntry(
                node_id=row.node_id,
                x=float(row.x),
                y=float(row.y),
                collapsed=bool(row.collapsed),
                updated_at=row.updated_at.isoformat() if row.updated_at else None,
            )
            for row in rows
        ]

    def upsert_layout(
        self,
        owner_id: str,
        workflow_id: str,
        entries: Iterable[Dict[str, Any]],
        *,
        now: Optional[datetime] = None,
    ) -> List[CanvasLayoutEntry]:
        """Merge positions for the given nodes; other nodes are left alone.

        A merge rather than a replacement: the canvas saves the node the user
        just moved, and treating that payload as the whole layout would delete
        every other position. Deletion is explicit — see :meth:`delete_layout`.

        Validation happens for EVERY entry before ANY row is written, so a
        payload with one bad node cannot leave a half-applied layout.
        """
        timestamp = now or datetime.now(timezone.utc)
        prepared = []
        for entry in entries:
            node_id = parse_node_id(entry.get("node_id"))
            prepared.append(
                (
                    node_id,
                    validate_coordinate(entry.get("x"), "x"),
                    validate_coordinate(entry.get("y"), "y"),
                    bool(entry.get("collapsed", False)),
                )
            )
        # Last write wins within one payload, without a duplicate-key error.
        deduped = {item[0]: item for item in prepared}

        session = self._session()
        try:
            existing = {
                row.node_id: row
                for row in session.execute(
                    select(WorkflowCanvasLayout).where(
                        WorkflowCanvasLayout.owner_id == owner_id,
                        WorkflowCanvasLayout.workflow_id == workflow_id,
                    )
                ).scalars().all()
            }
            for node_id, x, y, collapsed in deduped.values():
                row = existing.get(node_id)
                if row is None:
                    session.add(
                        WorkflowCanvasLayout(
                            id=uuid.uuid4().hex,
                            owner_id=owner_id,
                            workflow_id=workflow_id,
                            node_id=node_id,
                            x=x,
                            y=y,
                            collapsed=collapsed,
                            created_at=timestamp,
                            updated_at=timestamp,
                        )
                    )
                else:
                    row.x = x
                    row.y = y
                    row.collapsed = collapsed
                    row.updated_at = timestamp
            session.commit()
            return self._list(session, owner_id, workflow_id)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_layout(
        self,
        owner_id: str,
        workflow_id: str,
        node_ids: Iterable[str],
        *,
        db: Optional[Session] = None,
    ) -> int:
        """Explicitly forget positions for the given nodes; returns the count.

        Called when a node genuinely leaves the document (a deleted stage), so
        the layout table does not accumulate rows for nodes that no longer
        exist.
        """
        normalized = [parse_node_id(node_id) for node_id in node_ids]
        if not normalized:
            return 0

        def _apply(session: Session) -> int:
            result = session.execute(
                sql_delete(WorkflowCanvasLayout).where(
                    WorkflowCanvasLayout.owner_id == owner_id,
                    WorkflowCanvasLayout.workflow_id == workflow_id,
                    WorkflowCanvasLayout.node_id.in_(normalized),
                )
            )
            return int(result.rowcount or 0)

        if db is not None:
            return _apply(db)
        session = self._session()
        try:
            removed = _apply(session)
            session.commit()
            return removed
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
