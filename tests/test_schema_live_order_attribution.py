from pathlib import Path


def test_schema_has_live_order_intents_and_broker_import_source():
    schema = Path("schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS public.live_order_intents" in schema
    assert "cost_contract_json JSONB" in schema
    assert "client_order_ref TEXT NOT NULL" in schema
    assert "broker_import" in schema
    assert "live_fill" in schema


def test_schema_has_basket_execution_tables_and_links():
    schema = Path("schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS public.basket_executions" in schema
    assert "CREATE TABLE IF NOT EXISTS public.basket_execution_legs" in schema
    assert "CREATE TABLE IF NOT EXISTS public.worker_execution_events" in schema
    assert "ADD COLUMN IF NOT EXISTS basket_execution_id TEXT" in schema
    assert "ADD COLUMN IF NOT EXISTS basket_leg_index INTEGER" in schema


def test_schema_has_worker_execution_links_and_bracket_tables():
    schema = Path("schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS public.worker_live_execution_links" in schema
    assert "CREATE TABLE IF NOT EXISTS public.bracket_intents" in schema
    assert "CREATE TABLE IF NOT EXISTS public.bracket_actions" in schema
    assert "ADD COLUMN IF NOT EXISTS bracket_intent_id TEXT" in schema
