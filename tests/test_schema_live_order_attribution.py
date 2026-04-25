from pathlib import Path


def test_schema_has_live_order_intents_and_broker_import_source():
    schema = Path("schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS public.live_order_intents" in schema
    assert "cost_contract_json JSONB" in schema
    assert "client_order_ref TEXT NOT NULL" in schema
    assert "broker_import" in schema
    assert "live_fill" in schema
