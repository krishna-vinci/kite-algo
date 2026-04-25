from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

import requests


JsonDict = Dict[str, Any]


@dataclass(frozen=True)
class AlgoWorkerConfig:
    """Connection settings for the Kite Algo worker API."""

    base_url: str
    token: str
    timeout: float = 10.0
    api_prefix: str = "/api/algo-workers"


class KiteAlgoWorkerError(RuntimeError):
    """Raised when the worker API returns a non-2xx response."""

    def __init__(self, message: str, *, status_code: int, response_body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class KiteAlgoWorkerClient:
    """Small, boring HTTP client for external algo workers.

    The client only calls public `/api/algo-workers/worker/*` endpoints. It never
    calls broker, database, paper-runtime, or market-runtime internals.
    """

    def __init__(self, config: AlgoWorkerConfig) -> None:
        if not config.base_url:
            raise ValueError("base_url is required")
        if not config.token:
            raise ValueError("token is required")
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def health(self) -> JsonDict:
        return self._request("GET", "/worker/health")

    def heartbeat(
        self,
        worker_id: Optional[str] = None,
        status: str = "healthy",
        metrics: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        payload: JsonDict = {"status": status, "metrics": dict(metrics or {})}
        if worker_id is not None:
            payload["worker_id"] = worker_id
        return self._request("POST", "/worker/heartbeat", json=payload)

    def create_run(
        self,
        *,
        template_id: str,
        account_scope: str,
        strategy_run_id: Optional[str] = None,
        execution_mode: str = "paper",
        summary_fields: Optional[Iterable[Mapping[str, Any]]] = None,
        risk_schema: Optional[Iterable[Mapping[str, Any]]] = None,
        allowed_actions: Optional[Iterable[str]] = None,
        runtime_state: Optional[Mapping[str, Any]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        payload: JsonDict = {
            "template_id": template_id,
            "account_scope": account_scope,
            "execution_mode": execution_mode,
            "summary_fields": [dict(item) for item in (summary_fields or [])],
            "risk_schema": [dict(item) for item in (risk_schema or [])],
            "allowed_actions": list(allowed_actions or ["edit_risk", "exit_strategy"]),
            "runtime_state": dict(runtime_state or {}),
            "metadata": dict(metadata or {}),
        }
        if strategy_run_id is not None:
            payload["strategy_run_id"] = strategy_run_id
        return self._request("POST", "/worker/runs", json=payload)

    def get_run(self, strategy_run_id: str) -> JsonDict:
        return self._request("GET", f"/worker/runs/{strategy_run_id}")

    def get_run_pnl(self, strategy_run_id: str) -> JsonDict:
        return self._request("GET", f"/worker/runs/{strategy_run_id}/pnl")

    def stream_run_pnl(self, strategy_run_id: str, *, interval_seconds: float = 1.0) -> Iterator[JsonDict]:
        response = self.session.request(
            "GET",
            self._url(f"/worker/runs/{strategy_run_id}/pnl/stream"),
            timeout=(self.config.timeout, None),
            stream=True,
            params={"interval_seconds": interval_seconds},
        )
        if not 200 <= response.status_code < 300:
            try:
                self._raise_response_error(response, "GET", f"/worker/runs/{strategy_run_id}/pnl/stream")
            finally:
                response.close()

        def _events() -> Iterator[JsonDict]:
            current_event = "message"
            try:
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip() or "message"
                        continue
                    if line.startswith("data:"):
                        payload = line.split(":", 1)[1].strip()
                        if payload:
                            decoded = json.loads(payload)
                            if current_event == "error":
                                raise KiteAlgoWorkerError(
                                    f"Worker API stream error for run {strategy_run_id}: {decoded.get('detail') if isinstance(decoded, dict) else decoded}",
                                    status_code=0,
                                    response_body=decoded,
                                )
                            if current_event == "end":
                                break
                            yield decoded
                        current_event = "message"
            finally:
                response.close()

        return _events()

    def place_order(
        self,
        strategy_run_id: str,
        order: Mapping[str, Any],
        idempotency_key: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        key = self._require_idempotency_key(idempotency_key)
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/intents",
            json={
                "intent_type": "place_order",
                "payload": {"order": dict(order)},
                "idempotency_key": key,
                "metadata": dict(metadata or {}),
            },
        )

    def place_basket(
        self,
        strategy_run_id: str,
        orders: Iterable[Mapping[str, Any]],
        idempotency_key: str,
        metadata: Optional[Mapping[str, Any]] = None,
        *,
        all_or_none: bool = False,
        dry_run: bool = False,
    ) -> JsonDict:
        key = self._require_idempotency_key(idempotency_key)
        order_list: List[JsonDict] = [dict(order) for order in orders]
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/intents",
            json={
                "intent_type": "place_basket",
                "payload": {"basket": {"orders": order_list, "all_or_none": all_or_none, "dry_run": dry_run}},
                "idempotency_key": key,
                "metadata": dict(metadata or {}),
            },
        )

    def patch_risk(
        self,
        strategy_run_id: str,
        patch: Mapping[str, Any],
        reason: Optional[str] = None,
    ) -> JsonDict:
        return self._request(
            "PATCH",
            f"/worker/runs/{strategy_run_id}/risk",
            json={"patch": dict(patch), "reason": reason},
        )

    def exit_run(
        self,
        strategy_run_id: str,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        dry_run: bool = False,
    ) -> JsonDict:
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/exit",
            json={"reason": reason, "idempotency_key": idempotency_key, "dry_run": dry_run},
        )

    @staticmethod
    def _require_idempotency_key(idempotency_key: str) -> str:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required for order intents")
        if not 8 <= len(key) <= 160:
            raise ValueError("idempotency_key must be between 8 and 160 characters")
        return key

    def _url(self, path: str) -> str:
        base = self.config.base_url.rstrip("/")
        prefix = "/" + self.config.api_prefix.strip("/")
        suffix = "/" + path.strip("/")
        return f"{base}{prefix}{suffix}"

    def _request(self, method: str, path: str, **kwargs: Any) -> JsonDict:
        response = self.session.request(method, self._url(path), timeout=self.config.timeout, **kwargs)
        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw": response.text}

        self._raise_response_error(response, method, path)
        raise AssertionError("unreachable")

    @staticmethod
    def _raise_response_error(response: requests.Response, method: str, path: str) -> None:
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text
        detail = body.get("detail") if isinstance(body, dict) else body
        message = f"Worker API {method} {path} failed with HTTP {response.status_code}: {detail}"
        raise KiteAlgoWorkerError(message, status_code=response.status_code, response_body=body)
