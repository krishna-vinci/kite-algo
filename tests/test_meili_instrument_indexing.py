from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "broker_api" / "broker_api.py"


def _source_text() -> str:
    return SOURCE.read_text()


def test_meili_source_query_keeps_unexpired_and_no_expiry_instruments():
    source = _source_text()

    assert "AT TIME ZONE 'Asia/Kolkata'" in source
    assert "WHERE expiry IS NULL OR expiry >= {MEILI_INSTRUMENT_MARKET_DATE_SQL}" in source
    assert "WHERE expiry < {MEILI_INSTRUMENT_MARKET_DATE_SQL}" in source


def test_meili_reindex_does_not_clear_all_documents():
    source = _source_text()

    assert "delete_all_documents" not in source
    assert "delete_documents(batch)" in source


def test_meili_documents_do_not_store_redundant_payload_fields():
    source = _source_text()

    assert '"last_updated":' not in source
    assert '"underlying_symbol":' not in source
    assert '"expiry_label":' not in source


def test_search_exchange_parser_covers_imported_exchange_codes():
    source = _source_text()

    assert 'KITE_INSTRUMENT_IMPORT_EXCHANGES = ["NSE", "NFO", "BSE", "BFO", "CDS", "BCD", "MCX"]' in source
    assert "exchange_map = {exchange: exchange for exchange in KITE_INSTRUMENT_IMPORT_EXCHANGES}" in source
