"""Screener candle reads resolve universe members through the catalog."""

from unittest import mock

from backend.workflows.catalog_token_map import CatalogTokenMap


def test_resolves_any_member_and_caches_hits_only():
    calls = []

    def fake_resolve(keys, sessions, fallback_tokens):
        calls.append(set(keys))
        key = next(iter(keys))
        return ({"NSE:INFY": 408065} if key == "NSE:INFY" else {}), {}

    with mock.patch(
        "backend.workflows.worker_entry.resolve_catalog_instrument_tokens", side_effect=fake_resolve
    ):
        tokens = CatalogTokenMap(lambda: None)
        assert tokens.get("NSE:INFY") == 408065
        assert tokens.get("NSE:INFY") == 408065
        assert tokens.get("NSE:UNKNOWN") is None
        assert tokens.get("NSE:UNKNOWN") is None
    # a hit is cached; a miss is retried (never cached)
    assert calls == [{"NSE:INFY"}, {"NSE:UNKNOWN"}, {"NSE:UNKNOWN"}]


def test_a_resolution_error_is_not_cached():
    with mock.patch(
        "backend.workflows.worker_entry.resolve_catalog_instrument_tokens",
        side_effect=[RuntimeError("db down"), ({"NSE:TCS": 2953217}, {})],
    ):
        tokens = CatalogTokenMap(lambda: None)
        assert tokens.get("NSE:TCS") is None
        assert tokens.get("NSE:TCS") == 2953217


def test_scheduled_screener_history_uses_the_catalog_map():
    import inspect

    from backend.workflows import worker_entry

    source = inspect.getsource(worker_entry)
    assert "screener_history = PgCandleHistory(engine, CatalogTokenMap(session_factory))" in source
