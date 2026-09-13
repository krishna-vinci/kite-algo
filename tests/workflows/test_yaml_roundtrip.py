"""Canonical parity across the authoring paths (Phase 6 milestone 6C).

Spec §10.2 requires that the same workflow authored as YAML, as a REST/SDK
document dict, through the structured form, and through the canvas compile to
ONE canonical hash. The form and the canvas both emit a document dict, so the
assertions that matter are:

    YAML text  ─┐
                ├─►  parse  ──►  canonical hash   (must be equal)
    dict       ─┘

and that the readable YAML renderer is lossless in both directions.

These tests are deliberately hash-level rather than field-level: a field-level
comparison can pass while the stored identity still differs, which is exactly
the drift this milestone is meant to prevent.
"""

from __future__ import annotations

import pytest

from backend.workflows.compiler import compile_document
from backend.workflows.parser import (
    document_to_yaml,
    parse_workflow_dict,
    parse_workflow_yaml,
)

# Every shipped fixture that is expected to compile. `invalid-unknown-op.yaml`
# is intentionally excluded: it exists to prove a rejection.
FIXTURES = [
    "basic-price.yaml",
    "quality-momentum.yaml",
    "nifty-quality-momentum-screener.yaml",
    "phase4-consecutive-closes.yaml",
    "phase4-breakout-pullback.yaml",
    "phase4-breadth-window.yaml",
    "phase4-pair-ratio.yaml",
    "phase4-external-producer.yaml",
]


def _hash(document) -> str:
    return compile_document(document).canonical_hash


def _load_text(name: str) -> str:
    with open(f"tests/fixtures/workflows/{name}", encoding="utf-8") as handle:
        return handle.read()


@pytest.mark.parametrize("fixture", FIXTURES)
def test_yaml_and_dict_paths_agree(fixture: str) -> None:
    """The YAML text and the equivalent document dict are the same workflow.

    A divergence here would mean an SDK-authored workflow and a hand-written one
    could not be recognised as the same revision.
    """
    text = _load_text(fixture)
    from_yaml = parse_workflow_yaml(text)
    # The parsed document re-serialized to the canonical dict IS what the REST
    # and SDK paths send, so comparing them exercises the same normalisation.
    from_dict = parse_workflow_dict(from_yaml.to_document_dict())
    assert _hash(from_yaml) == _hash(from_dict)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_readable_yaml_round_trips_losslessly(fixture: str) -> None:
    document = parse_workflow_yaml(_load_text(fixture))
    rendered = document_to_yaml(document)
    assert _hash(parse_workflow_yaml(rendered)) == _hash(document)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_round_trip_is_idempotent(fixture: str) -> None:
    """A second pass changes nothing — otherwise the YAML tab would drift on
    every view, and a save from it would silently create a new revision."""
    first = parse_workflow_yaml(_load_text(fixture))
    rendered_once = document_to_yaml(first)
    second = parse_workflow_yaml(rendered_once)
    assert document_to_yaml(second) == rendered_once
    assert _hash(second) == _hash(first)


# ---------------------------------------------------------------------------
# form / canvas shaped documents
#
# These dicts are written EXACTLY as the frontend builders emit them
# (`features/alerts/lib/authoring.ts` and `screener-authoring.ts`): typed
# universe references, `freshness_limit_s` in seconds, `initial_match` always
# present, and an empty `instruments` list when a universe is used.
# ---------------------------------------------------------------------------

FORM_SHAPED_ALERT = {
    "version": 1,
    "name": "form-vs-yaml-alert",
    "session": "nse_equity",
    "instruments": [],
    "universe": {"union": [{"kind": "index", "name": "Nifty50"}], "deduplicate": True},
    "stages": [
        {
            "id": "px",
            "type": "signal",
            "clock": "candle_close",
            "timeframe": "day",
            "conditions": {"all": [{"left": {"field": "close"}, "op": "crosses_above", "right": 100}]},
        }
    ],
    "alerts": [
        {
            "id": "a1",
            "source": "px",
            "trigger": "on_transition",
            "channels": ["ops"],
            "cooldown_s": 300,
        }
    ],
}

EQUIVALENT_ALERT_YAML = """
version: 1
name: form-vs-yaml-alert
session: nse_equity
instruments: []
universe:
  union:
    - kind: index
      name: Nifty50
  deduplicate: true
stages:
  - id: px
    type: signal
    clock: candle_close
    timeframe: day
    conditions:
      all:
        - left: {field: close}
          op: crosses_above
          right: 100
alerts:
  - id: a1
    source: px
    trigger: on_transition
    channels: [ops]
    cooldown_s: 300
"""

FORM_SHAPED_SCREENER = {
    "version": 1,
    "name": "form-vs-yaml-screener",
    "session": "nse_equity",
    "instruments": [],
    "universe": {"union": [{"kind": "index", "name": "Nifty50"}], "deduplicate": True},
    "stages": [
        {
            "id": "scan",
            "type": "signal",
            "clock": "candle_close",
            "timeframe": "day",
            "conditions": {"all": [{"left": {"field": "change_pct"}, "op": "gt", "right": 2}]},
        }
    ],
    "screener": {
        "schedule": {"every": "1d", "calendar": "nse_equity", "at": "session_close"},
        "rank": {"by": {"field": "change_pct"}, "direction": "desc"},
        "top_n": 20,
        "freshness_limit_s": 259200,
        "attachments": [
            {
                "id": "top10",
                "trigger": "top_n",
                "channels": ["ops"],
                "initial_match": False,
                "top_n": 10,
                "entry_rank": 10,
                "exit_rank": 15,
            }
        ],
    },
}

EQUIVALENT_SCREENER_YAML = """
version: 1
name: form-vs-yaml-screener
session: nse_equity
instruments: []
universe:
  union:
    - kind: index
      name: Nifty50
  deduplicate: true
stages:
  - id: scan
    type: signal
    clock: candle_close
    timeframe: day
    conditions:
      all:
        - left: {field: change_pct}
          op: gt
          right: 2
screener:
  schedule:
    every: 1d
    calendar: nse_equity
    at: session_close
  rank:
    by: {field: change_pct}
    direction: desc
  top_n: 20
  freshness_limit_s: 259200
  attachments:
    - id: top10
      trigger: top_n
      channels: [ops]
      initial_match: false
      top_n: 10
      entry_rank: 10
      exit_rank: 15
"""


@pytest.mark.parametrize(
    ("document", "yaml_text", "label"),
    [
        (FORM_SHAPED_ALERT, EQUIVALENT_ALERT_YAML, "universe-targeted alert"),
        (FORM_SHAPED_SCREENER, EQUIVALENT_SCREENER_YAML, "screener with attachments"),
    ],
)
def test_form_shaped_document_matches_hand_written_yaml(
    document: dict, yaml_text: str, label: str
) -> None:
    """The builder's output and a hand-written document are one workflow."""
    from_form = parse_workflow_dict(document)
    from_yaml = parse_workflow_yaml(yaml_text)
    assert _hash(from_form) == _hash(from_yaml), f"{label} drifted between authoring paths"


def test_form_shaped_documents_render_and_round_trip() -> None:
    """The readable renderer also preserves the form/canvas constructs."""
    for document in (FORM_SHAPED_ALERT, FORM_SHAPED_SCREENER):
        parsed = parse_workflow_dict(document)
        rendered = document_to_yaml(parsed)
        assert _hash(parse_workflow_yaml(rendered)) == _hash(parsed)


def test_cosmetic_only_change_is_not_in_the_document() -> None:
    """Canvas layout cannot move a hash because it is not in the document.

    Spec §3 requires canonical hashes to exclude canvas/cosmetic metadata. The
    layout lives in its own table, so this holds by construction — asserted here
    because it is the property that would silently break if someone later moved
    positions into a `ui:` block.
    """
    parsed = parse_workflow_dict(FORM_SHAPED_ALERT)
    canonical = parsed.to_document_dict()
    assert "ui" not in canonical
    assert "layout" not in canonical
    assert "canvas" not in canonical
