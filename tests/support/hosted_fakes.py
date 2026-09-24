"""Shared test doubles for the hosted supervisor lifecycle tests.

These fakes implement only the worker-repository surface the lifecycle service
and the worker routes use. They let the state machine and the HTTP boundary be
exercised against a *real* ``SqlAlchemyStrategyRepository`` (the authoritative
attempt ledger) without standing up the full PostgreSQL worker schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.api.repositories.algo_worker_repo import WorkerToken
from backend.shared.serialization import _hash_token

__all__ = ["FakeWorkerRepository", "StubJournalService", "make_request"]


def make_request(app: Any):
    """A Starlette request whose ``.app.state`` is ``app`` (for service calls)."""
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "app": app}
    return Request(scope)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakeWorkerRepository:
    def __init__(self) -> None:
        self.tokens: Dict[str, Dict[str, Any]] = {}
        self.hashes: Dict[str, str] = {}
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.created_runs: List[Dict[str, Any]] = []
        self.revoked: List[str] = []
        #: strategy_run_id -> RunBindingInput recorded by create_run_with_binding.
        self.bindings: Dict[str, Any] = {}

    # -- tokens -------------------------------------------------------------

    async def create_token(self, payload, *, raw_token: str, token_id: str) -> Dict[str, Any]:
        self.tokens[token_id] = {
            "token_id": token_id,
            "name": payload.name,
            "account_scope": payload.account_scope,
            "allowed_modes": list(payload.allowed_modes),
            "allowed_actions": list(payload.allowed_actions),
            "allowed_templates": list(payload.allowed_templates),
            "status": "active",
            "expires_at": payload.expires_at,
            "metadata": dict(payload.metadata or {}),
        }
        self.hashes[_hash_token(raw_token)] = token_id
        return dict(self.tokens[token_id])

    async def get_token_by_hash(self, token_hash: str) -> Optional[WorkerToken]:
        token_id = self.hashes.get(token_hash)
        if token_id is None:
            return None
        record = self.tokens[token_id]
        return WorkerToken(
            token_id=token_id,
            name=record["name"],
            account_scope=record["account_scope"],
            allowed_modes=list(record["allowed_modes"]),
            allowed_actions=list(record["allowed_actions"]),
            allowed_templates=list(record["allowed_templates"]),
            status=record["status"],
            expires_at=record["expires_at"],
        )

    async def touch_token(self, token_id: str) -> None:
        return None

    async def get_token_status(self, token_id: str) -> Optional[str]:
        """Persisted token status by id (mirrors the real repository method)."""
        record = self.tokens.get(token_id)
        return str(record["status"]) if record else None

    async def revoke_token(self, token_id: str) -> Optional[Dict[str, Any]]:
        record = self.tokens.get(token_id)
        if record is None:
            return None
        record["status"] = "revoked"
        self.revoked.append(token_id)
        return dict(record)

    async def record_heartbeat(self, token_id: str, payload) -> Dict[str, Any]:
        return {"status": "ok", "token_id": token_id}

    # -- runs ---------------------------------------------------------------

    async def create_run(self, token: WorkerToken, payload, *, strategy_run_id: str) -> Dict[str, Any]:
        if strategy_run_id in self.runs:
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("duplicate run", None, Exception("dup"))
        run = {
            "strategy_run_id": strategy_run_id,
            "token_id": token.token_id,
            "template_id": payload.template_id,
            "account_scope": payload.account_scope,
            "execution_mode": payload.execution_mode,
            "status": "open",
            "summary_fields": list(payload.summary_fields or []),
            "risk_schema": list(payload.risk_schema or []),
            "allowed_actions": list(payload.allowed_actions or []),
            "runtime_state": dict(payload.runtime_state or {}),
            "metadata": dict(payload.metadata or {}),
            "worker_session_nonce": None,
            "worker_session_claimed_at": None,
            "last_heartbeat_at": None,
            "created_at": _now().isoformat(),
            "updated_at": _now().isoformat(),
            "closed_at": None,
        }
        self.runs[strategy_run_id] = run
        self.created_runs.append(dict(run))
        return dict(run)

    async def create_run_with_binding(
        self, token: WorkerToken, payload, *, strategy_run_id: str, binding=None
    ) -> Dict[str, Any]:
        """Mirror of the real path: run + trusted binding in one call.

        The binding is recorded only after the run insert succeeds, so the fake
        models the store's single-transaction semantics.
        """
        run = await self.create_run(token, payload, strategy_run_id=strategy_run_id)
        if binding is not None:
            self.bindings[strategy_run_id] = binding
        return run

    async def active_grants(self, *, token_id: str, account_id: str) -> List[Dict[str, Any]]:
        return []

    async def get_run(self, strategy_run_id: str) -> Optional[Dict[str, Any]]:
        run = self.runs.get(strategy_run_id)
        return dict(run) if run else None

    async def list_runs(self, token, *, limit: int = 25, cursor: Optional[str] = None) -> Dict[str, Any]:
        items = [r for r in self.runs.values() if r["token_id"] == token.token_id][:limit]
        return {"items": items, "next_cursor": None}

    async def release_run_session(self, strategy_run_id: str, *, expected_nonce: str) -> Optional[Dict[str, Any]]:
        run = self.runs.get(strategy_run_id)
        if run is None or run["worker_session_nonce"] != expected_nonce:
            return None
        run["worker_session_nonce"] = None
        run["worker_session_claimed_at"] = None
        return dict(run)

    async def claim_run_session(
        self, strategy_run_id: str, *, freshness_seconds: int, claimed_without_heartbeat_seconds: int
    ) -> Optional[Dict[str, Any]]:
        run = self.runs.get(strategy_run_id)
        if run is None:
            return None
        if run["worker_session_nonce"] is None:
            run["worker_session_nonce"] = f"wsn_{uuid.uuid4().hex}"
            run["worker_session_claimed_at"] = _now().isoformat()
        return dict(run)

    async def record_run_heartbeat(self, strategy_run_id: str, *, expected_nonce: str) -> Optional[Dict[str, Any]]:
        run = self.runs.get(strategy_run_id)
        if run is None or run["worker_session_nonce"] != expected_nonce:
            return None
        run["last_heartbeat_at"] = _now().isoformat()
        return dict(run)


class StubJournalService:
    """Minimal journal service: run creation must not need v2 context tables."""

    def ensure_v2_worker_context(self, **kwargs: Any) -> Dict[str, Any]:
        return {}
