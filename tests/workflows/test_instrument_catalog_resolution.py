from datetime import date

import pytest

from backend.workflows.worker_entry import resolve_catalog_instrument_tokens


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    """Fake session dispatching on the SQL text (view vs records vs health)."""

    def __init__(self, view_rows=(), record_rows=(), health_rows=(), error=None):
        self._view_rows = list(view_rows)
        self._record_rows = list(record_rows)
        self._health_rows = list(health_rows)
        self._error = error

    def execute(self, statement, _params):
        if self._error is not None:
            raise self._error
        sql = str(statement)
        if "instrument_catalog_published_v" in sql:
            return _Result(self._view_rows)
        if "instrument_catalog_generations" in sql:
            return _Result(self._health_rows)
        if "instrument_catalog_records" in sql:
            return _Result(self._record_rows)
        return _Result([])

    def close(self):
        pass


def _descriptor_row(**overrides):
    row = {
        "instrument_id": "instrument-1",
        "public_key": "MCX:GOLD26OCTFUT",
        "exchange": "MCX",
        "segment": "MCX-FUT",
        "tradingsymbol": "GOLD26OCTFUT",
        "name": "GOLD",
        "instrument_type": "FUT",
        "underlying": "GOLD",
        "option_type": None,
        "expiry": date(2026, 10, 30),
        "strike": None,
        "tick_size": 1,
        "lot_size": 100,
        "broker": "kite",
        "broker_token": 123668231,
        "catalog_generation": "generation-1",
        "lifecycle_status": "active",
    }
    row.update(overrides)
    return row


def _health_row(status):
    return {
        "id": "generation-1" if status != "uninitialized" else None,
        "status": status,
        "record_count": 10,
        "requested_exchanges": [],
        "accepted_exchanges": [],
        "retained_exchanges": [],
        "validation_summary": {},
        "exchange_sources": {},
        "published_at": None,
    }


def test_catalog_resolution_wins_over_legacy_token_fallback():
    resolved, rejected = resolve_catalog_instrument_tokens(
        {"MCX:GOLD26OCTFUT"},
        lambda: _Session(view_rows=[_descriptor_row()]),
        fallback_tokens={"MCX:GOLD26OCTFUT": 999},
    )
    assert resolved == {"MCX:GOLD26OCTFUT": 123668231}
    assert rejected == {}


def test_catalog_resolution_uses_explicit_fallback_when_catalog_is_empty():
    resolved, rejected = resolve_catalog_instrument_tokens(
        {"MCX:GOLD26OCTFUT"},
        lambda: _Session(),
        fallback_tokens={"MCX:GOLD26OCTFUT": 999},
    )
    assert resolved == {"MCX:GOLD26OCTFUT": 999}
    assert rejected == {}


def test_expired_catalog_instrument_is_not_resolved_from_fallback():
    resolved, rejected = resolve_catalog_instrument_tokens(
        {"MCX:GOLD26OCTFUT"},
        lambda: _Session(view_rows=[_descriptor_row(lifecycle_status="expired")]),
        fallback_tokens={"MCX:GOLD26OCTFUT": 999},
    )
    assert resolved == {}
    assert rejected == {"MCX:GOLD26OCTFUT": "expired"}


def test_not_found_in_initialized_catalog_refuses_fallback(monkeypatch):
    monkeypatch.delenv("ALERTS_INSTRUMENT_TOKEN_FALLBACK", raising=False)
    resolved, rejected = resolve_catalog_instrument_tokens(
        {"MCX:GOLD26OCTFUT"},
        lambda: _Session(health_rows=[_health_row("published")]),
        fallback_tokens={"MCX:GOLD26OCTFUT": 999},
    )
    assert resolved == {}
    assert rejected == {"MCX:GOLD26OCTFUT": "not_found"}


def test_retired_record_hidden_from_view_is_rejected_not_fallback(monkeypatch):
    monkeypatch.delenv("ALERTS_INSTRUMENT_TOKEN_FALLBACK", raising=False)
    resolved, rejected = resolve_catalog_instrument_tokens(
        {"MCX:GOLD26OCTFUT"},
        lambda: _Session(
            health_rows=[_health_row("published")],
            record_rows=[{"lifecycle_status": "retired"}],
        ),
        fallback_tokens={"MCX:GOLD26OCTFUT": 999},
    )
    assert resolved == {}
    assert rejected == {"MCX:GOLD26OCTFUT": "retired"}


def test_always_policy_applies_fallback_for_genuinely_missing_record(monkeypatch):
    monkeypatch.setenv("ALERTS_INSTRUMENT_TOKEN_FALLBACK", "always")
    resolved, rejected = resolve_catalog_instrument_tokens(
        {"MCX:GOLD26OCTFUT"},
        lambda: _Session(health_rows=[_health_row("published")]),
        fallback_tokens={"MCX:GOLD26OCTFUT": 999},
    )
    assert resolved == {"MCX:GOLD26OCTFUT": 999}
    assert rejected == {}


def test_always_policy_still_refuses_retired_record(monkeypatch):
    monkeypatch.setenv("ALERTS_INSTRUMENT_TOKEN_FALLBACK", "always")
    resolved, rejected = resolve_catalog_instrument_tokens(
        {"MCX:GOLD26OCTFUT"},
        lambda: _Session(
            health_rows=[_health_row("published")],
            record_rows=[{"lifecycle_status": "retired"}],
        ),
        fallback_tokens={"MCX:GOLD26OCTFUT": 999},
    )
    assert resolved == {}
    assert rejected == {"MCX:GOLD26OCTFUT": "retired"}


def test_unavailable_catalog_raises_instead_of_silent_fallback(monkeypatch):
    from backend.broker_api.instruments.catalog import CatalogUnavailableError

    monkeypatch.delenv("ALERTS_INSTRUMENT_TOKEN_FALLBACK", raising=False)
    with pytest.raises(CatalogUnavailableError):
        resolve_catalog_instrument_tokens(
            {"MCX:GOLD26OCTFUT"},
            lambda: _Session(error=RuntimeError("database down")),
            fallback_tokens={"MCX:GOLD26OCTFUT": 999},
        )


def test_ambiguous_instrument_is_rejected():
    resolved, rejected = resolve_catalog_instrument_tokens(
        {"MCX:GOLD26OCTFUT"},
        lambda: _Session(view_rows=[_descriptor_row(), _descriptor_row(instrument_id="instrument-2")]),
        fallback_tokens={"MCX:GOLD26OCTFUT": 999},
    )
    assert resolved == {}
    assert rejected == {"MCX:GOLD26OCTFUT": "ambiguous"}
