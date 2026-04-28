from __future__ import annotations

from typing import Any


class KiteAlgoWorkerError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, response_body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class AuthError(KiteAlgoWorkerError):
    pass


class PermissionDeniedError(KiteAlgoWorkerError):
    pass


class BrokerValidationError(KiteAlgoWorkerError):
    pass


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
    if status_code in {400, 422}:
        return BrokerValidationError(message, status_code=status_code, response_body=body)
    return KiteAlgoWorkerError(message, status_code=status_code, response_body=body)


__all__ = [
    "AuthError",
    "BrokerValidationError",
    "KiteAlgoWorkerError",
    "PermissionDeniedError",
    "StreamDisconnectedError",
    "error_for_status",
]
