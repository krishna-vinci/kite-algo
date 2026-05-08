from __future__ import annotations

import os


DEFAULT_ALLOWED_CORS_ORIGINS = (
    "http://localhost:13000",
    "http://127.0.0.1:13000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def _split_csv_env(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def get_allowed_cors_origins() -> list[str]:
    raw = (os.getenv("APP_ALLOWED_ORIGINS") or "").strip()
    if raw:
        return _split_csv_env(raw)
    return list(DEFAULT_ALLOWED_CORS_ORIGINS)


def get_scheduler_ntfy_url() -> str | None:
    raw = (os.getenv("SCHEDULER_NTFY_URL") or "").strip()
    return raw or None
