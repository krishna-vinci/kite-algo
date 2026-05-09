import importlib
import sys


def _reload_runtime_public_config(monkeypatch, **env):
    for key in ("APP_ALLOWED_ORIGINS", "SCHEDULER_NTFY_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("backend.app.runtime_public_config", None)
    return importlib.import_module("backend.app.runtime_public_config")


def test_default_allowed_origins_are_generic_local_hosts(monkeypatch):
    config = _reload_runtime_public_config(monkeypatch)

    assert config.get_allowed_cors_origins() == [
        "http://localhost:13000",
        "http://127.0.0.1:13000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


def test_allowed_origins_respect_csv_env(monkeypatch):
    config = _reload_runtime_public_config(
        monkeypatch,
        APP_ALLOWED_ORIGINS="https://app.example.com, https://ops.example.com ",
    )

    assert config.get_allowed_cors_origins() == [
        "https://app.example.com",
        "https://ops.example.com",
    ]


def test_scheduler_ntfy_url_is_optional(monkeypatch):
    config = _reload_runtime_public_config(monkeypatch)

    assert config.get_scheduler_ntfy_url() is None


def test_scheduler_ntfy_url_returns_stripped_value(monkeypatch):
    config = _reload_runtime_public_config(
        monkeypatch,
        SCHEDULER_NTFY_URL=" https://ntfy.example.com/scheduler-alerts ",
    )

    assert config.get_scheduler_ntfy_url() == "https://ntfy.example.com/scheduler-alerts"
