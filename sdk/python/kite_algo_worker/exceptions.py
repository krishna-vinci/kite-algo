from __future__ import annotations

from typing import Any, Optional


def _rejection_reason_from_body(body: Any) -> Optional[str]:
    """Normalize the rejection reason from either response envelope shape."""
    if not isinstance(body, dict):
        return None
    for candidate in (body.get("rejection_reason"), (body.get("detail") or {}).get("rejection_reason") if isinstance(body.get("detail"), dict) else None):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


class KiteAlgoWorkerError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, response_body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.rejection_reason = _rejection_reason_from_body(response_body)


class AuthError(KiteAlgoWorkerError):
    pass


class PermissionDeniedError(KiteAlgoWorkerError):
    pass


class BrokerValidationError(KiteAlgoWorkerError):
    pass


class UnsupportedSchemaVersionError(BrokerValidationError):
    """The worker API rejected the requested schema version as unsupported."""


class WorkerDataUnavailableError(KiteAlgoWorkerError):
    """A worker read surface temporarily cannot serve its data (503)."""


class CalendarRangeUncoveredError(WorkerDataUnavailableError):
    """The requested date range is outside the active calendar version.

    Uncovered dates are never inferred to be holidays; the request fails closed.
    """


class StreamDisconnectedError(KiteAlgoWorkerError):
    pass


def _message_from_body(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("message")
        if detail:
            return str(detail)
    if body not in (None, ""):
        return str(body)
    return fallback


def error_for_status(status_code: int, body: Any, *, fallback: str) -> KiteAlgoWorkerError:
    message = _message_from_body(body, fallback)
    if status_code == 401:
        return AuthError(message, status_code=status_code, response_body=body)
    if status_code == 403:
        return PermissionDeniedError(message, status_code=status_code, response_body=body)
    if status_code == 422 and _rejection_reason_from_body(body) == "UNSUPPORTED_SCHEMA_VERSION":
        return UnsupportedSchemaVersionError(message, status_code=status_code, response_body=body)
    if status_code == 503:
        reason = _rejection_reason_from_body(body)
        if reason == "CALENDAR_RANGE_UNCOVERED":
            return CalendarRangeUncoveredError(message, status_code=status_code, response_body=body)
        if reason in {"CALENDAR_UNAVAILABLE", "PORTFOLIO_SNAPSHOT_UNAVAILABLE"}:
            return WorkerDataUnavailableError(message, status_code=status_code, response_body=body)
    if status_code in {400, 422}:
        return BrokerValidationError(message, status_code=status_code, response_body=body)
    return KiteAlgoWorkerError(message, status_code=status_code, response_body=body)


__all__ = [
    "AuthError",
    "BrokerValidationError",
    "CalendarRangeUncoveredError",
    "KiteAlgoWorkerError",
    "PermissionDeniedError",
    "StreamDisconnectedError",
    "UnsupportedSchemaVersionError",
    "WorkerDataUnavailableError",
    "error_for_status",
]
