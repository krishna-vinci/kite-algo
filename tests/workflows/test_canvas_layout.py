"""Phase 6 6B backend: canvas layout persistence.

The canvas is another editor of the same canonical document, so the layout
store must be provably incapable of affecting the definition. These tests pin
that, plus the namespaced node identity that makes positions trustworthy when
stage ids, alert ids and channel names collide.

Nothing here exercises a browser or a rendering library.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test"
)

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.workflows.canvas_layout import (  # noqa: E402
    CANVAS_NAMESPACES,
    CanvasLayoutRepository,
    CanvasNodeIdError,
    WorkflowCanvasLayout,
    parse_node_id,
    validate_coordinate,
)
from backend.workflows.compiler import compile_document  # noqa: E402
from backend.workflows.parser import parse_workflow_dict  # noqa: E402
from backend.workflows.repository import (  # noqa: E402
    Base,
    SqlAlchemyWorkflowRepository,
)

OWNER = "app:admin"
OTHER = "kite:paper-a"
T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)

# A document in which a STAGE and a CHANNEL deliberately share the name
# `telegram_primary`. This is legal (separate id spaces) and it is exactly the
# case where a bare, non-namespaced key would cross-assign positions.
COLLIDING_DOCUMENT = {
    "version": 1,
    "name": "collision",
    "session": "nse_equity",
    "instruments": ["NSE:RELIANCE"],
    "stages": [
        {
            "id": "telegram_primary",
            "type": "signal",
            "clock": "ltp",
            "conditions": {
                "all": [
                    {"left": {"field": "ltp"}, "op": "crosses_above",
                     "right": {"value": 3000}}
                ]
            },
        }
    ],
    "alerts": [
        {"id": "telegram_primary", "source": "telegram_primary",
         "trigger": "on_transition", "channels": ["telegram_primary"]},
    ],
}


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture()
def repo(session_factory):
    return CanvasLayoutRepository(session_factory)


@pytest.fixture()
def workflow_id(session_factory):
    repository = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(COLLIDING_DOCUMENT))
    workflow, _revision = repository.create_workflow(
        OWNER, "collision", compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    return workflow.id


# ---------------------------------------------------------------------------
# node identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("stage:px", "stage:px"),
        ("alert:a1", "alert:a1"),
        ("channel:telegram_primary", "channel:telegram_primary"),
        ("  stage:px  ", "stage:px"),
        ("stage:a b", "stage:a b"),
    ],
)
def test_valid_node_ids_are_accepted(raw, expected):
    assert parse_node_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "px",                      # bare id: the collision hazard
        "",
        "   ",
        "stage:",
        ":px",
        "unknown:px",              # namespace that can never match a node
        "stage",
        "STAGE:px",                # case-sensitive: namespaces are exact
        "stage:a:b",               # extra colon stays part of the local id — legal
    ],
)
def test_invalid_node_ids_are_refused(raw):
    if raw == "stage:a:b":
        # Documented behavior: only the FIRST colon separates, so a local id may
        # itself contain a colon. Asserted rather than left ambiguous.
        assert parse_node_id(raw) == "stage:a:b"
        return
    with pytest.raises(CanvasNodeIdError):
        parse_node_id(raw)


def test_a_bare_node_id_error_explains_the_collision_hazard():
    """The message must teach the rule, because the client writes these ids."""
    with pytest.raises(CanvasNodeIdError) as excinfo:
        parse_node_id("telegram_primary")
    message = str(excinfo.value)
    assert "namespaced" in message
    assert "stage, an alert and a channel" in message


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, "1", None])
def test_non_finite_or_non_numeric_coordinates_are_refused(value):
    with pytest.raises(CanvasNodeIdError):
        validate_coordinate(value, "x")


def test_absurd_coordinates_are_refused():
    with pytest.raises(CanvasNodeIdError):
        validate_coordinate(1e12, "y")


# ---------------------------------------------------------------------------
# the collision that motivates namespacing
# ---------------------------------------------------------------------------


def test_colliding_stage_alert_and_channel_names_keep_separate_positions(repo, workflow_id):
    """The whole reason node ids are namespaced.

    A stage, an alert and a channel can all be named `telegram_primary`. Each
    must persist its OWN position; a bare-key design would give them one shared
    row and a reorder could hand one node another's coordinates.
    """
    repo.upsert_layout(OWNER, workflow_id, [
        {"node_id": "stage:telegram_primary", "x": 10.0, "y": 20.0},
        {"node_id": "alert:telegram_primary", "x": 30.0, "y": 40.0},
        {"node_id": "channel:telegram_primary", "x": 50.0, "y": 60.0},
    ])
    positions = {
        entry.node_id: (entry.x, entry.y)
        for entry in repo.list_layout(OWNER, workflow_id)
    }
    assert positions == {
        "stage:telegram_primary": (10.0, 20.0),
        "alert:telegram_primary": (30.0, 40.0),
        "channel:telegram_primary": (50.0, 60.0),
    }
    assert len(positions) == 3, "three distinct nodes must not share one row"


def test_every_namespace_can_hold_its_own_position(repo, workflow_id):
    for index, namespace in enumerate(CANVAS_NAMESPACES):
        repo.upsert_layout(
            OWNER, workflow_id,
            [{"node_id": f"{namespace}:n{index}", "x": float(index), "y": 0.0}],
        )
    stored = {entry.node_id for entry in repo.list_layout(OWNER, workflow_id)}
    assert stored == {f"{namespace}:n{i}" for i, namespace in enumerate(CANVAS_NAMESPACES)}


# ---------------------------------------------------------------------------
# hash neutrality — the core 6B guarantee
# ---------------------------------------------------------------------------


def test_writing_layout_does_not_touch_the_document_or_its_hash(repo, workflow_id, session_factory):
    """A cosmetic move must not create a revision or change the hash."""
    from backend.workflows.repository import WorkflowRevision

    def _revisions():
        with session_factory() as session:
            return (
                session.query(WorkflowRevision)
                .filter_by(workflow_id=workflow_id)
                .order_by(WorkflowRevision.revision.asc())
                .all()
            )

    before = _revisions()
    assert len(before) == 1
    before_hash = before[0].canonical_hash
    before_document = dict(before[0].document)

    repo.upsert_layout(OWNER, workflow_id, [
        {"node_id": "stage:telegram_primary", "x": 123.5, "y": -400.25, "collapsed": True},
    ])

    after = _revisions()
    assert len(after) == 1, "a layout-only change must not create a revision"
    assert after[0].canonical_hash == before_hash
    assert dict(after[0].document) == before_document, "the definition must be untouched"
    assert after[0].status == before[0].status


def test_a_bare_node_id_never_reaches_storage(repo, workflow_id, session_factory):
    """Rejected ids must not be silently stored under a made-up namespace."""
    with pytest.raises(CanvasNodeIdError):
        repo.upsert_layout(OWNER, workflow_id, [{"node_id": "px", "x": 1.0, "y": 2.0}])
    with session_factory() as session:
        assert session.query(WorkflowCanvasLayout).count() == 0


# ---------------------------------------------------------------------------
# merge vs replace
# ---------------------------------------------------------------------------


def test_a_write_merges_and_leaves_other_nodes_alone(repo, workflow_id):
    """Saving the moved node must not delete every other position."""
    repo.upsert_layout(OWNER, workflow_id, [
        {"node_id": "stage:a", "x": 1.0, "y": 1.0},
        {"node_id": "stage:b", "x": 2.0, "y": 2.0},
    ])
    repo.upsert_layout(OWNER, workflow_id, [
        {"node_id": "stage:a", "x": 99.0, "y": 99.0},
    ])
    positions = {
        entry.node_id: (entry.x, entry.y)
        for entry in repo.list_layout(OWNER, workflow_id)
    }
    assert positions["stage:a"] == (99.0, 99.0)
    assert positions["stage:b"] == (2.0, 2.0), "an omitted node must be preserved"


def test_a_failed_entry_leaves_the_layout_untouched(repo, workflow_id, session_factory):
    """Validation is all-or-nothing, so no half-applied layout is possible."""
    repo.upsert_layout(OWNER, workflow_id, [{"node_id": "stage:a", "x": 1.0, "y": 1.0}])
    with pytest.raises(CanvasNodeIdError):
        repo.upsert_layout(OWNER, workflow_id, [
            {"node_id": "stage:b", "x": 5.0, "y": 5.0},
            {"node_id": "bogus", "x": 6.0, "y": 6.0},
        ])
    stored = {entry.node_id for entry in repo.list_layout(OWNER, workflow_id)}
    assert stored == {"stage:a"}, "the valid entry must not be persisted either"


def test_duplicate_nodes_in_one_payload_do_not_error(repo, workflow_id):
    """Last write wins rather than raising a unique-constraint error."""
    entries = repo.upsert_layout(OWNER, workflow_id, [
        {"node_id": "stage:a", "x": 1.0, "y": 1.0},
        {"node_id": "stage:a", "x": 7.0, "y": 8.0},
    ])
    assert len(entries) == 1
    assert (entries[0].x, entries[0].y) == (7.0, 8.0)


def test_collapsed_round_trips_and_defaults_to_false(repo, workflow_id):
    repo.upsert_layout(OWNER, workflow_id, [
        {"node_id": "stage:a", "x": 0.0, "y": 0.0, "collapsed": True},
        {"node_id": "stage:b", "x": 0.0, "y": 0.0},
    ])
    by_id = {entry.node_id: entry for entry in repo.list_layout(OWNER, workflow_id)}
    assert by_id["stage:a"].collapsed is True
    assert by_id["stage:b"].collapsed is False


# ---------------------------------------------------------------------------
# deletion is explicit
# ---------------------------------------------------------------------------


def test_delete_removes_only_the_named_nodes(repo, workflow_id):
    repo.upsert_layout(OWNER, workflow_id, [
        {"node_id": "stage:a", "x": 1.0, "y": 1.0},
        {"node_id": "stage:b", "x": 2.0, "y": 2.0},
    ])
    assert repo.delete_layout(OWNER, workflow_id, ["stage:a"]) == 1
    assert [entry.node_id for entry in repo.list_layout(OWNER, workflow_id)] == ["stage:b"]


def test_delete_of_an_absent_node_is_a_no_op(repo, workflow_id):
    assert repo.delete_layout(OWNER, workflow_id, ["stage:never-existed"]) == 0


# ---------------------------------------------------------------------------
# owner isolation
# ---------------------------------------------------------------------------


def test_layout_is_scoped_to_the_owner(repo, workflow_id):
    """A layout row belongs to one owner; a foreign scope sees nothing."""
    repo.upsert_layout(OWNER, workflow_id, [{"node_id": "stage:a", "x": 1.0, "y": 1.0}])
    assert repo.list_layout(OTHER, workflow_id) == []
    assert repo.delete_layout(OTHER, workflow_id, ["stage:a"]) == 0
    assert len(repo.list_layout(OWNER, workflow_id)) == 1


def test_the_same_node_id_can_differ_per_owner(repo, workflow_id):
    repo.upsert_layout(OWNER, workflow_id, [{"node_id": "stage:a", "x": 1.0, "y": 1.0}])
    repo.upsert_layout(OTHER, workflow_id, [{"node_id": "stage:a", "x": 9.0, "y": 9.0}])
    owner_entry = repo.list_layout(OWNER, workflow_id)[0]
    other_entry = repo.list_layout(OTHER, workflow_id)[0]
    assert (owner_entry.x, owner_entry.y) == (1.0, 1.0)
    assert (other_entry.x, other_entry.y) == (9.0, 9.0)


# ---------------------------------------------------------------------------
# independence from the document
# ---------------------------------------------------------------------------


def test_layout_survives_a_document_change(repo, workflow_id, session_factory):
    """Positions are not tied to a revision, so a new revision keeps them."""
    repo.upsert_layout(OWNER, workflow_id, [{"node_id": "stage:a", "x": 3.0, "y": 4.0}])
    repository = SqlAlchemyWorkflowRepository(session_factory)
    changed = {**COLLIDING_DOCUMENT, "name": "collision-v2"}
    compiled = compile_document(parse_workflow_dict(changed))
    repository.add_draft_revision(
        workflow_id, compiled.document.to_document_dict(), compiled.canonical_hash
    )
    assert [entry.node_id for entry in repo.list_layout(OWNER, workflow_id)] == ["stage:a"]
