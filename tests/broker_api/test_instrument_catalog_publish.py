"""C3/C4/C5 acceptance: publication generation coherence, completeness
rejection, and enrichment-independent identity.

The publisher runs against a scripted fake connection so the full
transaction (advisory lock, previous-publication read, staging, per-row
publish, retained bump, generation summary) is exercised without a live
PostgreSQL. A real-PostgreSQL concurrency check runs in the Phase 1.5 suite.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from backend.broker_api.instruments import catalog as catalog_module
from backend.broker_api.instruments.catalog import (
    CatalogRefreshPublisher,
    RefreshFailure,
    RefreshValidationError,
    identity_key,
    normalize_broker_record,
    normalize_exchange_rows,
)


G1 = "11111111-1111-1111-1111-111111111111"
G2 = "22222222-2222-2222-2222-222222222222"
IID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PUBLISHED_AT = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)


def _record(exchange="NSE", symbol="RELIANCE", token=738561, **overrides):
    value = {
        "instrument_token": token,
        "exchange_token": token // 10,
        "tradingsymbol": symbol,
        "name": "RELIANCE",
        "expiry": None,
        "strike": None,
        "tick_size": 0.05,
        "lot_size": 1,
        "instrument_type": "EQ",
        "segment": "NSE",
        "exchange": exchange,
    }
    value.update(overrides)
    return value


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list = []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.executed.append((text, params))
        result = self.conn.results.get(text, [])
        self._last = result
        return result

    def fetchone(self):
        rows = getattr(self, "_last", [])
        return rows[0] if rows else None

    def fetchall(self):
        return list(getattr(self, "_last", []))

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    """Scripted connection: canned rows keyed by normalized SQL text."""

    def __init__(self, results: dict):
        self.results = results
        self.cursors: list = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        cur = _FakeCursor(self)
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _cursor_sql(conn):
    return [entry for cur in conn.cursors for entry in cur.executed]


@pytest.fixture()
def stage_recorder(monkeypatch):
    calls = []

    def _record(cur, sql, values, *args, **kwargs):
        calls.append(len(values))

    monkeypatch.setattr(catalog_module.psycopg2.extras, "execute_values", _record)
    return calls


def _full_results(*, previous_published=True, prev_sources=(("NSE", 2, G1), ("MCX", 1, G1))):
    prev = [
        (G1, PUBLISHED_AT),
    ] if previous_published else []
    sources = [(ex, n, gen, PUBLISHED_AT) for (ex, n, gen) in prev_sources]
    return {
        "SELECT pg_advisory_xact_lock (%s)": [(True,)],
        "SELECT id, published_at FROM public.instrument_catalog_generations WHERE status IN ('published', 'degraded') AND published_at IS NOT NULL ORDER BY published_at DESC, created_at DESC LIMIT 1": prev,
        "SELECT r.exchange, COUNT(*) AS record_count, MAX(r.current_generation_id::text) AS source_generation, MAX(g.published_at) AS observed_at FROM public.instrument_catalog_records r JOIN public.instrument_catalog_generations g ON g.id = r.current_generation_id WHERE r.current_generation_id = %s AND r.lifecycle_status <> 'retired' GROUP BY r.exchange": sources,
        "INSERT INTO public.instrument_catalog_generations (status, requested_exchanges, validation_summary) VALUES ('staging', %s, %s) RETURNING id": [(G2,)],
        "INSERT INTO public.instrument_catalog_generations (status, requested_exchanges, retained_exchanges, validation_summary, completed_at) VALUES ('failed', %s, %s, %s, NOW()) RETURNING id": [(G2,)],
        "SELECT instrument_id, identity_key FROM public.instrument_catalog_records WHERE public_key = %s FOR UPDATE": [],
        "SELECT instrument_id FROM public.instrument_catalog_records WHERE identity_key = %s FOR UPDATE": [],
        "INSERT INTO public.instrument_catalog_records (identity_key, public_key, exchange, segment, tradingsymbol, name, instrument_type, underlying, option_type, expiry, strike, tick_size, lot_size, lifecycle_status, current_generation_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s IS NOT NULL AND %s < CURRENT_DATE THEN 'expired' ELSE 'active' END, %s) RETURNING instrument_id": [(IID,)],
        "UPDATE public.instrument_broker_mappings SET is_current = FALSE, valid_to_generation = %s, last_seen_at = NOW() WHERE broker = %s AND is_current = TRUE AND (instrument_id = %s OR broker_token = %s)": [],
        "INSERT INTO public.instrument_broker_mappings (instrument_id, broker, broker_exchange, broker_symbol, broker_token, broker_exchange_token, valid_from_generation, is_current) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)": [],
        "SELECT COUNT(*) FROM public.instrument_catalog_records WHERE current_generation_id = %s": [(3,)],
    }


def test_c3_scoped_refresh_bumps_untouched_exchanges_to_new_generation(stage_recorder):
    """After an NSE-only publish, MCX rows must point at the SAME generation
    so Go's single-generation view contract holds."""
    conn = _FakeConnection(_full_results())
    publisher = CatalogRefreshPublisher(connection_factory=lambda: conn)

    result = publisher.publish({"NSE": [_record(), _record(token=738562, symbol="TCS")]})

    assert result.status == "published"
    bump = [
        (sql, params)
        for sql, params in _cursor_sql(conn)
        if sql.startswith("UPDATE public.instrument_catalog_records SET current_generation_id")
    ]
    # the untouched-exchange bump must include MCX (previous publication member)
    assert any("MCX" in str(params) for sql, params in bump)
    # exchange_sources records MCX as retained from ITS original generation
    summary = [
        params for sql, params in _cursor_sql(conn)
        if sql.startswith("UPDATE public.instrument_catalog_generations SET status")
    ]
    payload = summary[-1]
    exchange_sources = json.loads(payload[5])
    assert exchange_sources["NSE"]["state"] == "accepted"
    assert exchange_sources["MCX"]["state"] == "retained"
    assert exchange_sources["MCX"]["source_generation"] == G1
    assert exchange_sources["MCX"]["observed_at"] is not None


def test_c3_scoped_refresh_single_generation_with_zero_previous(stage_recorder):
    conn = _FakeConnection(_full_results(previous_published=False, prev_sources=()))
    publisher = CatalogRefreshPublisher(connection_factory=lambda: conn)
    result = publisher.publish({"NSE": [_record()]})
    assert result.status == "published"
    assert result.exchange_sources["NSE"]["state"] == "accepted"


def test_c4_truncated_payload_is_rejected_and_retained(stage_recorder):
    results = _full_results(prev_sources=(("NSE", 1000, G1),))
    conn = _FakeConnection(results)
    publisher = CatalogRefreshPublisher(connection_factory=lambda: conn, minimum_count=1)

    result = publisher.publish({"NSE": [_record()]})  # 1 row vs 1000 previously

    assert result.status == "failed"
    assert "suspiciously truncated" in result.validation_errors["NSE"]
    assert result.retained_exchanges == ["NSE"]


def test_c4_force_exchanges_waives_truncation_check(stage_recorder):
    results = _full_results(prev_sources=(("NSE", 1000, G1),))
    conn = _FakeConnection(results)
    publisher = CatalogRefreshPublisher(connection_factory=lambda: conn, minimum_count=1)

    result = publisher.publish({"NSE": [_record()]}, force_exchanges=["NSE"])

    assert result.status == "published"
    assert result.accepted_exchanges == ["NSE"]


def test_c4_wrong_scope_row_rejects_exchange(stage_recorder):
    conn = _FakeConnection(_full_results(previous_published=False, prev_sources=()))
    publisher = CatalogRefreshPublisher(connection_factory=lambda: conn)
    result = publisher.publish({"NSE": [_record(exchange="BSE")]})
    assert result.status == "failed"
    assert "wrong-scope" in result.validation_errors["NSE"]


def test_c4_non_finite_numeric_field_is_typed_rejection():
    with pytest.raises(RefreshValidationError, match="invalid tick_size"):
        normalize_broker_record(_record(tick_size=float("nan")), source_exchange="NSE")


def test_c4_unparseable_expiry_is_typed_rejection():
    with pytest.raises(RefreshValidationError, match="expiry"):
        normalize_broker_record(
            _record(expiry="not-a-date", instrument_type="FUT"), source_exchange="NSE"
        )


def test_c5_identity_ignores_enrichment_corrections():
    base = normalize_broker_record(_record(), source_exchange="NSE")
    corrected = normalize_broker_record(
        _record(segment="NSE-EQ", name="RELIANCE INDUSTRIES"), source_exchange="NSE"
    )
    assert base["identity_key"] == corrected["identity_key"]


def test_c5_identity_ignores_inferred_underlying_changes():
    with_underlying = normalize_broker_record(
        _record(instrument_type="FUT", expiry=date(2026, 10, 30), underlying="GOLD"),
        source_exchange="MCX",
    )
    inferred = normalize_broker_record(
        _record(instrument_type="FUT", expiry=date(2026, 10, 30)),
        source_exchange="MCX",
    )
    assert with_underlying["identity_key"] == inferred["identity_key"]


def test_c5_strike_zero_and_none_are_the_same_identity():
    zero = normalize_broker_record(_record(strike=0), source_exchange="NSE")
    blank = normalize_broker_record(_record(strike=None), source_exchange="NSE")
    assert zero["identity_key"] == blank["identity_key"]
    assert zero["strike"] is None


def test_c5_real_contract_change_changes_identity():
    october = identity_key(
        normalize_broker_record(
            _record(instrument_type="FUT", expiry=date(2026, 10, 30)), source_exchange="MCX"
        )
    )
    november = identity_key(
        normalize_broker_record(
            _record(instrument_type="FUT", expiry=date(2026, 11, 30)), source_exchange="MCX"
        )
    )
    assert october != november


def test_c5_identity_numeric_rounding_is_stable():
    a = identity_key({"exchange": "NFO", "tradingsymbol": "X", "strike": 100.12345678})
    b = identity_key({"exchange": "NFO", "tradingsymbol": "X", "strike": 100.12345679})
    assert a == b
