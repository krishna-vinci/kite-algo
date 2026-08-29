from backend.broker_api.market.exchange_calendar import CSV_SCHEMA_VERSION, dry_run_import, parse_canonical_csv, sha256_text


CSV = """session_date,session_type,opens_at,closes_at,verified
2026-08-28,REGULAR,09:15:00,15:30:00,true
2026-08-29,HOLIDAY,,,true
2026-08-30,SPECIAL,18:15:00,19:15:00,true
"""


def test_canonical_import_requires_audited_source_metadata_and_dry_run():
    preview = dry_run_import(CSV, source_reference="https://www.nseindia.com/example-circular", sha256=sha256_text(CSV), parser_version=CSV_SCHEMA_VERSION, actor="operator@example", reason="official circular import")
    assert preview["dry_run"] is True
    assert preview["session_count"] == 3
    assert preview["sessions"][2]["session_type"] == "SPECIAL"


def test_unverified_or_ambiguous_rows_are_rejected():
    try:
        parse_canonical_csv("session_date,session_type,opens_at,closes_at,verified\n2026-08-29,REGULAR,,,true\n", schema_version=CSV_SCHEMA_VERSION)
    except ValueError as exc:
        assert "tradable" in str(exc)
    else:
        raise AssertionError("invalid regular session was accepted")
