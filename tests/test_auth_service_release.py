import importlib
import sys

import pytest


def _reload_auth_service(monkeypatch, **env):
    for key in (
        "APP_ENV",
        "APP_ALLOW_INSECURE_DEV_AUTH",
        "APP_JWT_SECRET",
        "JWT_SECRET",
        "APP_ADMIN_PASSWORD",
        "APP_ADMIN_PASSWORD_HASH",
        "APP_ADMIN_PASSWORD_HASH_B64",
        "APP_ADMIN_PASSWORD_HASH_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("auth_service", None)
    return importlib.import_module("auth_service")


def test_missing_jwt_secret_raises_outside_explicit_dev_mode(monkeypatch):
    auth_service = _reload_auth_service(monkeypatch)

    with pytest.raises(RuntimeError, match="APP_JWT_SECRET"):
        auth_service._jwt_secret()


def test_explicit_dev_gate_allows_insecure_jwt_secret(monkeypatch):
    auth_service = _reload_auth_service(
        monkeypatch,
        APP_ENV="development",
        APP_ALLOW_INSECURE_DEV_AUTH="true",
    )

    assert auth_service._jwt_secret() == "dev-insecure-change-me"


def test_missing_admin_credentials_raise_outside_explicit_dev_mode(monkeypatch):
    auth_service = _reload_auth_service(monkeypatch, APP_JWT_SECRET="release-secret")

    with pytest.raises(RuntimeError, match="APP_ADMIN_PASSWORD"):
        auth_service.verify_app_credentials("admin", "admin123")


def test_explicit_dev_gate_allows_admin123(monkeypatch):
    auth_service = _reload_auth_service(
        monkeypatch,
        APP_ENV="development",
        APP_ALLOW_INSECURE_DEV_AUTH="true",
        APP_JWT_SECRET="dev-secret",
    )

    assert auth_service.verify_app_credentials("admin", "admin123") is True
    assert auth_service.verify_app_credentials("admin", "wrong") is False
