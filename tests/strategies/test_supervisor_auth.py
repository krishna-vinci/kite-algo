"""Unit tests for the supervisor lifecycle credential."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from backend.strategies import supervisor_auth  # noqa: E402


def _request(headers=None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


def test_unconfigured_is_default_deny(monkeypatch):
    monkeypatch.delenv(supervisor_auth.HOSTED_SUPERVISOR_CREDENTIALS_ENV, raising=False)
    monkeypatch.delenv(supervisor_auth.HOSTED_SUPERVISOR_CREDENTIAL_ENV, raising=False)
    assert supervisor_auth.supervisor_is_configured() is False
    with pytest.raises(HTTPException) as exc:
        supervisor_auth.require_supervisor(_request({supervisor_auth.HEADER_NAME: "anything"}))
    assert exc.value.status_code == 401


def test_missing_header_is_rejected(monkeypatch):
    monkeypatch.setenv(supervisor_auth.HOSTED_SUPERVISOR_CREDENTIAL_ENV, "s3cret")
    with pytest.raises(HTTPException) as exc:
        supervisor_auth.require_supervisor(_request({}))
    assert exc.value.status_code == 401


def test_wrong_credential_is_rejected(monkeypatch):
    monkeypatch.setenv(supervisor_auth.HOSTED_SUPERVISOR_CREDENTIAL_ENV, "s3cret")
    with pytest.raises(HTTPException) as exc:
        supervisor_auth.require_supervisor(_request({supervisor_auth.HEADER_NAME: "nope"}))
    assert exc.value.status_code == 401


def test_correct_credential_passes(monkeypatch):
    monkeypatch.setenv(supervisor_auth.HOSTED_SUPERVISOR_CREDENTIAL_ENV, "s3cret")
    supervisor_auth.require_supervisor(_request({supervisor_auth.HEADER_NAME: "s3cret"}))


def test_rotation_accepts_either_credential(monkeypatch):
    monkeypatch.setenv(
        supervisor_auth.HOSTED_SUPERVISOR_CREDENTIALS_ENV, "old-cred,new-cred"
    )
    monkeypatch.delenv(supervisor_auth.HOSTED_SUPERVISOR_CREDENTIAL_ENV, raising=False)
    supervisor_auth.require_supervisor(_request({supervisor_auth.HEADER_NAME: "old-cred"}))
    supervisor_auth.require_supervisor(_request({supervisor_auth.HEADER_NAME: "new-cred"}))
    with pytest.raises(HTTPException):
        supervisor_auth.require_supervisor(_request({supervisor_auth.HEADER_NAME: "other"}))
