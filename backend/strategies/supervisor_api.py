"""HTTP client for the supervisor lifecycle API — no database credentials.

The supervisor holds **no** database access. Everything it knows about a job
comes from the credential-authenticated lifecycle API
(:mod:`backend.api.routers.hosted_lifecycle`). This module is deliberately
stdlib-only (``urllib``) so the supervisor has no dependency on the application's
DB stack, and it never logs the credential or the child token.

Errors are surfaced as :class:`SupervisorApiError` carrying the HTTP status and
the server's machine-readable ``rejection_reason`` so the caller can distinguish
a *conflict* (e.g. a preparation already completed by another caller) from a
transport failure or a server error.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

__all__ = ["LifecycleApiClient", "SupervisorApiError", "SupervisorTransportError"]

#: Response header names are contract; keep the supervisor credential out of logs.
CREDENTIAL_HEADER = "X-Hosted-Supervisor-Credential"


class SupervisorApiError(Exception):
    """The lifecycle API refused the request (HTTP status + machine reason)."""

    def __init__(self, status_code: int, reason: Optional[str], detail: Any = None) -> None:
        self.status_code = int(status_code)
        self.reason = reason
        self.detail = detail
        super().__init__(f"lifecycle API {self.status_code}: {reason or detail!r}")

    @property
    def is_conflict(self) -> bool:
        return self.status_code == 409


class SupervisorTransportError(Exception):
    """The lifecycle API could not be reached, or the response could not be read."""


class LifecycleApiClient:
    def __init__(
        self,
        base_url: str,
        credential: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not credential:
            raise ValueError("supervisor credential is required")
        self.base_url = base_url.rstrip("/")
        self._credential = credential
        self.timeout = float(timeout)

    # -- transport ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            url = f"{url}?{urllib.parse.urlencode(filtered)}"
        data = None
        headers = {
            CREDENTIAL_HEADER: self._credential,
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = self._safe_json(raw)
            reason = None
            if isinstance(payload, dict):
                detail = payload.get("detail")
                if isinstance(detail, dict):
                    reason = detail.get("rejection_reason")
                elif isinstance(detail, str):
                    reason = detail
            raise SupervisorApiError(exc.code, reason, payload) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SupervisorTransportError(str(exc)) from exc
        return self._safe_json(raw) or {}

    @staticmethod
    def _safe_json(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    # -- operations ---------------------------------------------------------

    def list_jobs(self, *, status: str = "queued", limit: int = 50) -> List[Dict[str, Any]]:
        payload = self._request("GET", "/hosted-supervisor/jobs", params={"status": status, "limit": limit})
        return list(payload.get("jobs") or [])

    def claim(
        self,
        job_id: str,
        *,
        lease_owner: str,
        expected_lease_epoch: int,
        expected_attempt: int,
        lease_until: str,
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/hosted-supervisor/jobs/{job_id}/claim",
            body={
                "lease_owner": lease_owner,
                "expected_lease_epoch": expected_lease_epoch,
                "expected_attempt": expected_attempt,
                "lease_until": lease_until,
            },
        )

    def job_state(
        self, job_id: str, *, lease_owner: str, lease_epoch: int, attempt: int
    ) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/hosted-supervisor/jobs/{job_id}",
            params={"lease_owner": lease_owner, "lease_epoch": lease_epoch, "attempt": attempt},
        )

    def prepare(self, job_id: str, *, lease_owner: str, lease_epoch: int, attempt: int) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/hosted-supervisor/jobs/{job_id}/prepare",
            body={"lease_owner": lease_owner, "lease_epoch": lease_epoch, "attempt": attempt},
        )

    def source(self, job_id: str, *, lease_owner: str, lease_epoch: int, attempt: int) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/hosted-supervisor/jobs/{job_id}/source",
            params={"lease_owner": lease_owner, "lease_epoch": lease_epoch, "attempt": attempt},
        )

    def heartbeat(
        self, job_id: str, *, lease_owner: str, lease_epoch: int, attempt: int, lease_until: str
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/hosted-supervisor/jobs/{job_id}/heartbeat",
            body={
                "lease_owner": lease_owner,
                "lease_epoch": lease_epoch,
                "attempt": attempt,
                "lease_until": lease_until,
            },
        )

    def release(self, job_id: str, *, lease_owner: str, lease_epoch: int, attempt: int) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/hosted-supervisor/jobs/{job_id}/release",
            body={"lease_owner": lease_owner, "lease_epoch": lease_epoch, "attempt": attempt},
        )

    def fence(
        self, job_id: str, *, lease_owner: str, lease_epoch: int, attempt: int, reason: str = "fenced_by_supervisor"
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/hosted-supervisor/jobs/{job_id}/fence",
            body={
                "lease_owner": lease_owner,
                "lease_epoch": lease_epoch,
                "attempt": attempt,
                "reason": reason,
            },
        )

    def recover(
        self, job_id: str, *, lease_owner: str, lease_epoch: int, attempt: int, reason: str = "lease_expired"
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/hosted-supervisor/jobs/{job_id}/recover",
            body={
                "lease_owner": lease_owner,
                "lease_epoch": lease_epoch,
                "attempt": attempt,
                "reason": reason,
            },
        )

    def process_cleanup(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        attempt: int,
        state: str,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "lease_owner": lease_owner,
            "lease_epoch": lease_epoch,
            "attempt": attempt,
            "state": state,
        }
        if note is not None:
            body["note"] = str(note)[:200]
        return self._request(
            "POST", f"/hosted-supervisor/jobs/{job_id}/process-cleanup", body=body
        )
