from datetime import date

import pytest

from backend.broker_api.instruments.catalog import (
    AmbiguousInstrumentError,
    InstrumentCatalog,
    InstrumentDescriptor,
    InstrumentNotFoundError,
    MissingExchangeError,
    descriptor_from_row,
    identity_key,
    normalize_public_key,
)


def _row(**overrides):
    row = {
        "instrument_id": "11111111-1111-1111-1111-111111111111",
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
        "tick_size": 1.0,
        "lot_size": 100,
        "broker": "kite",
        "broker_token": 123,
        "catalog_generation": "22222222-2222-2222-2222-222222222222",
        "lifecycle_status": "active",
    }
    row.update(overrides)
    return row


def test_normalize_public_key_requires_exchange_and_normalizes_case():
    assert normalize_public_key(" mcx:gold26octfut ") == "MCX:GOLD26OCTFUT"
    with pytest.raises(MissingExchangeError):
        normalize_public_key("GOLD26OCTFUT")


def test_identity_key_changes_for_different_exact_contracts():
    october = _row(expiry=date(2026, 10, 30))
    november = _row(expiry=date(2026, 11, 30))
    assert identity_key(october) != identity_key(november)


def test_descriptor_serialization_has_stable_and_compatibility_fields():
    descriptor = descriptor_from_row(_row())
    payload = descriptor.to_dict()
    assert payload["instrument_id"]
    assert payload["catalog_generation"]
    assert payload["segment"] == "MCX-FUT"
    assert payload["expiry"] == "2026-10-30"
    assert payload["instrument_token"] == 123
    assert payload["symbol"] == "MCX:GOLD26OCTFUT"


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _sql, _params):
        return _Result(self.rows)

    def close(self):
        pass


def test_catalog_exact_resolution_and_ambiguity():
    descriptor = InstrumentCatalog(db=lambda: _Session([_row()])).resolve_public_key("MCX:gold26octfut")
    assert descriptor.broker_token == 123

    with pytest.raises(AmbiguousInstrumentError):
        InstrumentCatalog(db=lambda: _Session([_row(), _row(instrument_id="different")])).resolve_public_key("MCX:GOLD26OCTFUT")


def test_catalog_missing_token_is_typed():
    with pytest.raises(InstrumentNotFoundError):
        InstrumentCatalog(db=lambda: _Session([])).resolve_broker_token(999)


def test_catalog_search_and_health():
    rows = [_row()]
    catalog = InstrumentCatalog(db=lambda: _Session(rows))
    assert catalog.search("gold")[0].public_key == "MCX:GOLD26OCTFUT"
    health = catalog.health()
    assert health["status"] == "uninitialized" or "status" in health
