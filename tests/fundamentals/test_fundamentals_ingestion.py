import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from fundamentals import ingestion
from fundamentals.ingestion import (
    DEFAULT_MIN_DELAY_SECONDS,
    MAX_ON_DEMAND_SYMBOLS,
    SyncConfig,
    SyncScope,
    _fingerprint,
    _should_fetch,
    resolve_scope_symbols,
    run_fundamentals_sync,
)


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


def test_symbols_scope_resolves_list():
    scope = SyncScope(scope_type="symbols", scope_value="reliance, tcs, , INFY")
    assert resolve_scope_symbols(scope) == ["RELIANCE", "TCS", "INFY"]


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args):
        return None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        self.closed = True


def test_index_scope_resolves_from_constituent_store(monkeypatch):
    from fundamentals import index_scopes

    members = [("INFY",), ("RELIANCE",), ("TCS",)]
    monkeypatch.setattr(index_scopes, "get_db_connection", lambda: _FakeConn(members))
    assert resolve_scope_symbols(SyncScope(scope_type="index", scope_value="Nifty50")) == ["INFY", "RELIANCE", "TCS"]


def test_unknown_index_scope_raises(monkeypatch):
    with pytest.raises(ValueError, match="index must be one of"):
        resolve_scope_symbols(SyncScope(scope_type="index", scope_value="nonexistent"))


def test_index_scope_without_constituents_raises(monkeypatch):
    from fundamentals import index_scopes

    monkeypatch.setattr(index_scopes, "get_db_connection", lambda: _FakeConn([]))
    with pytest.raises(ValueError, match="no constituents"):
        resolve_scope_symbols(SyncScope(scope_type="index", scope_value="Nifty50"))


def test_index_scope_keys_are_case_insensitive(monkeypatch):
    from fundamentals import index_scopes

    members = [("INFY",)]
    monkeypatch.setattr(index_scopes, "get_db_connection", lambda: _FakeConn(members))
    assert resolve_scope_symbols(SyncScope(scope_type="index", scope_value="nifty50")) == ["INFY"]


def test_index_scope_adapter_is_extensible(monkeypatch):
    """Adding a future index is one registry entry; nothing else changes."""
    from fundamentals import index_scopes

    adapters = dict(index_scopes._INDEX_SCOPE_ADAPTERS)
    adapters["NiftyMidcap150"] = index_scopes.IndexScopeAdapter(key="NiftyMidcap150", description="future")
    monkeypatch.setattr(index_scopes, "_INDEX_SCOPE_ADAPTERS", adapters)
    monkeypatch.setattr(index_scopes, "_CASE_FOLD", {key.casefold(): key for key in adapters})
    monkeypatch.setattr(index_scopes, "get_db_connection", lambda: _FakeConn([("ABC",)]))

    assert index_scopes.supported_index_scopes() == ["Nifty50", "Nifty500", "NiftyMidcap150"]
    assert resolve_scope_symbols(SyncScope(scope_type="index", scope_value="niftymidcap150")) == ["ABC"]


def test_unsupported_scope_type_raises():
    with pytest.raises(ValueError, match="unsupported scope type"):
        resolve_scope_symbols(SyncScope(scope_type="sector", scope_value="it"))


# ---------------------------------------------------------------------------
# Freshness / skip logic and fingerprinting
# ---------------------------------------------------------------------------


def _config(mode="incremental", freshness_hours=24.0):
    return SyncConfig(scope=SyncScope(scope_type="symbols", scope_value="X"), mode=mode, freshness_hours=freshness_hours)


def test_should_fetch_for_full_mode_missing_state_and_failures():
    assert _should_fetch({}, _config()) is True
    assert _should_fetch({"status": "failed"}, _config()) is True
    assert _should_fetch({"status": "success"}, _config(mode="full")) is True


def test_should_fetch_respects_freshness_window():
    now = pd.Timestamp.now(tz="UTC")
    fresh = {"status": "success", "last_checked_at": (now - timedelta(hours=2)).to_pydatetime()}
    stale = {"status": "success", "last_checked_at": (now - timedelta(hours=30)).to_pydatetime()}
    unparsable = {"status": "success", "last_checked_at": "not-a-date"}
    assert _should_fetch(fresh, _config(freshness_hours=24.0)) is False
    assert _should_fetch(stale, _config(freshness_hours=24.0)) is True
    assert _should_fetch(unparsable, _config()) is True


def test_fingerprint_ignores_scrape_metadata_and_is_stable():
    parsed_a = {"quarterly": pd.DataFrame([
        {"metric_key": "sales", "numeric_value": 120.0, "scraped_at": "t1", "source_url": "u", "page_html": "<x>"},
        {"metric_key": "net profit", "numeric_value": 15.0, "scraped_at": "t1", "source_url": "u", "page_html": "<x>"},
    ])}
    parsed_b = {"quarterly": pd.DataFrame([
        {"scraped_at": "t2", "page_html": "<y>", "numeric_value": 120.0, "metric_key": "sales", "source_url": "u"},
        {"scraped_at": "t2", "page_html": "<y>", "numeric_value": 15.0, "metric_key": "net profit", "source_url": "u"},
    ])}
    parsed_c = {"quarterly": pd.DataFrame([
        {"metric_key": "sales", "numeric_value": 999.0},
        {"metric_key": "net profit", "numeric_value": 15.0},
    ])}
    assert _fingerprint(parsed_a) == _fingerprint(parsed_b)
    assert _fingerprint(parsed_a) != _fingerprint(parsed_c)


# ---------------------------------------------------------------------------
# Full engine run with injected fakes (no network, no real sleeps)
# ---------------------------------------------------------------------------


class _FakeFetchResult:
    def __init__(self, symbol, *, not_modified=False, etag='"e1"'):
        self.requested_symbol = symbol
        self.company_slug = symbol
        self.statement_scope = "consolidated"
        self.source_url = f"https://www.screener.in/company/{symbol}/consolidated/"
        self.fetched_at = "2026-09-05T00:00:00+00:00"
        self.etag = etag
        self.last_modified = None
        self.not_modified = not_modified
        self.html = "" if not_modified else f"<html>{symbol}</html>"


def _install_engine_fakes(
    monkeypatch,
    *,
    states,
    not_modified_symbols=(),
    unchanged_by_fingerprint=(),
    failing_symbols=(),
    empty_parsed_symbols=(),
):
    """Wire run_fundamentals_sync to in-memory fakes; returns captured calls."""
    sleeps = []
    state_writes = []
    upserts = []
    started = {}

    def fake_resolve(scope):
        if scope.scope_type == "symbols":
            return [s.strip().upper() for s in scope.scope_value.split(",") if s.strip()]
        return []

    def fake_state(symbol, statement_scope):
        return states.get(symbol, {})

    class FakeScreener:
        async def fetch_company_page(self, symbol, *, statement_scope="consolidated", if_none_match=None, if_modified_since=None):
            if symbol in failing_symbols:
                raise RuntimeError(f"boom for {symbol}")
            return _FakeFetchResult(symbol, not_modified=symbol in not_modified_symbols)

    def fake_parse(result):
        if result.requested_symbol in empty_parsed_symbols:
            return {name: pd.DataFrame() for name in ingestion.DATASETS}
        if result.requested_symbol in unchanged_by_fingerprint:
            return {"quarterly": pd.DataFrame([{"metric_key": "sales", "numeric_value": 1.0}])}
        return {"quarterly": pd.DataFrame([{"metric_key": "sales", "numeric_value": 2.0}])}

    def fake_fingerprint(parsed):
        return str(parsed["quarterly"]["numeric_value"].iloc[0])

    def fake_upsert(symbol, statement_scope, parsed, scraped_at):
        upserts.append(symbol)

    def fake_write_state(symbol, statement_scope, **kwargs):
        state_writes.append((symbol, kwargs["status"]))

    def fake_start(config, requested):
        run_id = ingestion.uuid.uuid4()
        started["run_id"] = run_id
        started["requested"] = requested
        return run_id

    finished = {}

    def fake_finish(run_id, **kwargs):
        finished["status"] = kwargs["status"]
        finished["counts"] = (kwargs["changed"], kwargs["unchanged"], kwargs["failed"], kwargs["skipped"])

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ingestion, "resolve_scope_symbols", fake_resolve)
    monkeypatch.setattr(ingestion, "_load_symbol_state", fake_state)
    monkeypatch.setattr(ingestion, "ScreenerClient", FakeScreener)
    monkeypatch.setattr(ingestion, "parse_screener_company_page", fake_parse)
    monkeypatch.setattr(ingestion, "_fingerprint", fake_fingerprint)
    monkeypatch.setattr(ingestion, "_upsert_symbol", fake_upsert)
    monkeypatch.setattr(ingestion, "_write_symbol_state", fake_write_state)
    monkeypatch.setattr(ingestion, "_start_run", fake_start)
    monkeypatch.setattr(ingestion, "_finish_run", fake_finish)
    monkeypatch.setattr(ingestion.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ingestion, "ensure_screener_parser_ready", lambda: None)
    return sleeps, state_writes, upserts, started, finished


def _config_for(symbols, *, on_demand=True, mode="incremental", delay=DEFAULT_MIN_DELAY_SECONDS):
    return SyncConfig(
        scope=SyncScope(scope_type="symbols", scope_value=",".join(symbols)),
        mode=mode,
        min_delay_seconds=delay,
        on_demand=on_demand,
    )


def test_run_sync_counts_changed_unchanged_skipped_and_failed(monkeypatch):
    states = {
        "FRESH": {"status": "success", "last_checked_at": pd.Timestamp.now(tz="UTC").to_pydatetime()},
        "GONE": {"status": "success", "last_checked_at": pd.Timestamp.now(tz="UTC").to_pydatetime()},
        # Stale check time so B is fetched; stored fingerprint matches the fake parse
        # output, exercising the fingerprint-unchanged path.
        "B": {
            "status": "success",
            "content_fingerprint": "1.0",
            "last_checked_at": (pd.Timestamp.now(tz="UTC") - timedelta(hours=30)).to_pydatetime(),
        },
    }
    sleeps, state_writes, upserts, started, finished = _install_engine_fakes(
        monkeypatch,
        states=states,
        not_modified_symbols={"A"},
        unchanged_by_fingerprint={"B"},
        failing_symbols={"C"},
    )
    result = asyncio.run(run_fundamentals_sync(_config_for(["A", "B", "C", "FRESH", "GONE", "D"])))

    assert result["symbols_requested"] == 6
    assert result["symbols_changed"] == 1  # D
    assert result["symbols_unchanged"] == 2  # A (304), B (fingerprint)
    assert result["symbols_skipped"] == 2  # FRESH, GONE (fresh window)
    assert result["symbols_failed"] == 1  # C
    assert result["failed_symbols"] == [{"symbol": "C", "error": "RuntimeError: boom for C"}]
    assert upserts == ["D"]
    assert ("C", "failed") in state_writes
    assert finished["status"] == "completed"
    assert finished["counts"] == (1, 2, 1, 2)
    # Politeness delay runs between fetched symbols only: fetch order is
    # A, B, C, FRESH, GONE, D -> sleeps after A, B, C (D is last), never
    # after skipped symbols.
    assert len(sleeps) == 3
    assert all(s == DEFAULT_MIN_DELAY_SECONDS for s in sleeps)


def test_run_sync_full_mode_refetches_everything_and_skips_nothing(monkeypatch):
    states = {"A": {"status": "success", "last_checked_at": pd.Timestamp.now(tz="UTC").to_pydatetime()}}
    sleeps, state_writes, upserts, started, finished = _install_engine_fakes(monkeypatch, states=states)
    result = asyncio.run(run_fundamentals_sync(_config_for(["A", "B"], mode="full", delay=0)))

    assert result["symbols_skipped"] == 0
    assert result["symbols_changed"] == 2
    assert sleeps == []  # zero-delay config honored


def test_run_sync_on_demand_enforces_symbol_cap(monkeypatch):
    sleeps, state_writes, upserts, started, finished = _install_engine_fakes(monkeypatch, states={})
    symbols = [f"S{i:03d}" for i in range(MAX_ON_DEMAND_SYMBOLS + 1)]
    with pytest.raises(ValueError, match="on-demand sync limited"):
        asyncio.run(run_fundamentals_sync(_config_for(symbols, on_demand=True)))


def test_run_sync_scheduler_not_capped(monkeypatch):
    sleeps, state_writes, upserts, started, finished = _install_engine_fakes(monkeypatch, states={})
    symbols = [f"S{i:03d}" for i in range(MAX_ON_DEMAND_SYMBOLS + 5)]
    result = asyncio.run(run_fundamentals_sync(_config_for(symbols, on_demand=False, delay=0)))
    assert result["symbols_requested"] == MAX_ON_DEMAND_SYMBOLS + 5


def test_run_sync_single_flight_guard(monkeypatch):
    sleeps, state_writes, upserts, started, finished = _install_engine_fakes(monkeypatch, states={})
    # Simulate a concurrent sync holding the lock.
    asyncio.run(_contended_run())


async def _contended_run():
    async with ingestion._sync_lock:
        with pytest.raises(RuntimeError, match="already in progress"):
            await run_fundamentals_sync(_config_for(["A"], delay=0))


def test_run_sync_fatal_parser_dependency_aborts_run(monkeypatch):
    finished = []

    def boom():
        raise ModuleNotFoundError("No module named 'lxml'")

    monkeypatch.setattr(ingestion, "ensure_screener_parser_ready", boom)
    monkeypatch.setattr(ingestion, "_start_run", lambda config, requested: ingestion.uuid.uuid4())
    monkeypatch.setattr(ingestion, "_finish_run", lambda run_id, **kwargs: finished.append(kwargs))
    with pytest.raises(ModuleNotFoundError):
        asyncio.run(run_fundamentals_sync(_config_for(["A"], delay=0)))
    assert finished == [
        {
            "changed": 0,
            "unchanged": 0,
            "failed": 0,
            "skipped": 0,
            "status": "failed",
            "error": "ModuleNotFoundError: No module named 'lxml'",
        }
    ]


def test_run_sync_rejects_page_without_recognized_datasets(monkeypatch):
    sleeps, state_writes, upserts, started, finished = _install_engine_fakes(
        monkeypatch,
        states={},
        empty_parsed_symbols={"BLOCKED"},
    )

    result = asyncio.run(run_fundamentals_sync(_config_for(["BLOCKED"], delay=0)))

    assert result["symbols_changed"] == 0
    assert result["symbols_failed"] == 1
    assert upserts == []
    assert state_writes == [("BLOCKED", "failed")]
    assert "no recognized fundamentals datasets" in result["failed_symbols"][0]["error"]
