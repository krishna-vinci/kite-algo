from __future__ import annotations

import pytest

from backend.database_url import resolve_database_url
from backend.workflows.worker_entry import resolve_database_url as resolve_worker_database_url


def test_blank_database_url_falls_back_to_encoded_db_fields() -> None:
    values = {
        "DATABASE_URL": "",
        "DB_HOST": "postgres",
        "DB_PORT": "5432",
        "DB_NAME": "kite/db",
        "DB_USER": "user@example",
        "DB_PASSWORD": "p@ss/word",
    }

    expected = "postgresql+psycopg2://user%40example:p%40ss%2Fword@postgres:5432/kite%2Fdb"
    assert resolve_database_url(values) == expected
    assert resolve_worker_database_url(values) == expected


def test_worker_database_url_requires_complete_db_fields_when_no_direct_url() -> None:
    with pytest.raises(ValueError, match="DB_PASSWORD"):
        resolve_worker_database_url(
            {
                "DATABASE_URL": "",
                "DB_HOST": "postgres",
                "DB_PORT": "5432",
                "DB_NAME": "postgres",
                "DB_USER": "postgres",
            }
        )
