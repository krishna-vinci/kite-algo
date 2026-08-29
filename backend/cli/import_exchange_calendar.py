"""Import an operator-supplied, official NSE canonical session CSV.

The command never downloads or scrapes NSE.  It prints a validation preview by
default and writes only when --apply is explicitly supplied.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from backend.app.database import get_db_connection
from backend.broker_api.market.exchange_calendar import CSV_SCHEMA_VERSION, CalendarSchemaMigrationRequired, import_calendar_csv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--source-reference", required=True)
    parser.add_argument("--official-source-document-sha256", required=True, help="SHA-256 of the supplied official NSE source document, not of this canonical CSV")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument("--segment", default="CM")
    parser.add_argument("--parser-version", default=CSV_SCHEMA_VERSION)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    text = args.csv_path.read_text(encoding="utf-8")
    conn = get_db_connection() if args.apply else None
    try:
        result = import_calendar_csv(conn, text, exchange=args.exchange.upper(), segment=args.segment.upper(), source_reference=args.source_reference, official_source_document_sha256=args.official_source_document_sha256, parser_version=args.parser_version, actor=args.actor, reason=args.reason, apply=args.apply)
    except CalendarSchemaMigrationRequired as exc:
        parser.error(str(exc))
    finally:
        if conn is not None:
            conn.close()
    print({key: value for key, value in result.items() if key != "sessions"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
