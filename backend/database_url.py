"""Shared database URL resolution for API, migrations, and workers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import quote


def resolve_database_url(
    values: Mapping[str, str] | None = None,
    *,
    require_all: bool = False,
    driver: str = "postgresql+psycopg2",
) -> str:
    """Return an explicit DSN or assemble one from conventional ``DB_*`` vars.

    An empty ``DATABASE_URL`` is treated as unset. This matters for Compose
    env files, where an intentionally blank optional value is still exported.
    User, password, and database name are encoded so reserved characters do
    not change the connection target.
    """

    source = values if values is not None else os.environ
    direct = str(source.get("DATABASE_URL", "") or "").strip()
    if direct:
        return direct

    defaults = {
        "DB_HOST": "postgres",
        "DB_PORT": "5432",
        "DB_NAME": "postgres",
        "DB_USER": "postgres",
        "DB_PASSWORD": "postgres",
    }
    resolved = {
        key: str(source.get(key, "") or "").strip() or default
        for key, default in defaults.items()
    }
    if require_all:
        missing = [
            key for key in defaults
            if not str(source.get(key, "") or "").strip()
        ]
        if missing:
            raise ValueError(
                "DATABASE_URL is unset and DB_* configuration is incomplete; "
                f"missing {', '.join(missing)}"
            )

    user = quote(resolved["DB_USER"], safe="")
    password = quote(resolved["DB_PASSWORD"], safe="")
    name = quote(resolved["DB_NAME"], safe="")
    return (
        f"{driver}://{user}:{password}@{resolved['DB_HOST']}:{resolved['DB_PORT']}"
        f"/{name}"
    )
