from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.algo_runtime.account_scope import parse_account_scope
from backend.broker_api.core.redis_events import publish_event
from backend.broker_api.timeline.worker_timeline import worker_timeline_store
from backend.app.database import SessionLocal
from backend.shared.serialization import _hash_token, _json_dumps, _json_loads, _row_mapping, _to_float, _to_int, _utcnow

if TYPE_CHECKING:  # pragma: no cover
    from backend.api.schemas.worker import WorkerHeartbeatRequest, WorkerIntentRequest, WorkerRunCreateRequest, WorkerTokenCreateRequest

WORKER_SESSION_FRESHNESS_SECONDS = int(os.getenv("WORKER_SESSION_FRESHNESS_SECONDS", "60"))
WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS = int(
    os.getenv("WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS", "120")
)
WORKER_RUN_STALE_ACTION_SECONDS = int(
    os.getenv("WORKER_RUN_STALE_ACTION_SECONDS", "180")
)
WORKER_RUN_LIST_DEFAULT_LIMIT = 25
WORKER_RUN_LIST_MAX_LIMIT = 100
_WORKER_RUN_CURSOR_VERSION = 1
_WORKER_RUN_CURSOR_SECRET = os.getenv(
    "ALGO_WORKER_RUN_CURSOR_SECRET", "kite-algo-worker-run-cursor"
).encode("utf-8")


def _worker_token_allows_account_scope(token_scope: Optional[str], requested_scope: Any) -> bool:
    """Mirror worker_shared account-scope semantics without importing the router."""

    normalized_token_scope = str(token_scope or "").strip()
    if not normalized_token_scope:
        return True
    normalized_requested_scope = str(requested_scope or "").strip()
    if not normalized_requested_scope:
        return False
    try:
        token_parsed = parse_account_scope(normalized_token_scope)
        requested_parsed = parse_account_scope(normalized_requested_scope)
    except ValueError:
        return normalized_token_scope == normalized_requested_scope

    if token_parsed.mode == "live":
        if requested_parsed.mode == "paper":
            return True
        return requested_parsed.normalized == token_parsed.normalized
    return requested_parsed.normalized == token_parsed.normalized


def _worker_run_is_visible_to_token(token: WorkerToken, row: Any) -> bool:
    payload = _row_mapping(row)
    mode = str(payload.get("execution_mode") or "").strip().lower()
    allowed_modes = {str(value).strip().lower() for value in token.allowed_modes}
    if mode not in allowed_modes:
        return False
    template_id = str(payload.get("template_id") or "")
    if token.allowed_templates and template_id not in set(token.allowed_templates):
        return False
    return _worker_token_allows_account_scope(token.account_scope, payload.get("account_scope"))


def _cursor_text(value: Any) -> str:
    if value is None:
        raise ValueError("Run cursor is missing its creation timestamp")
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    text_value = str(value).strip()
    if not text_value or len(text_value) > 128:
        raise ValueError("Run cursor contains an invalid creation timestamp")
    return text_value


def _cursor_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _cursor_b64decode(value: str) -> bytes:
    if not value or len(value) > 512:
        raise ValueError("Invalid run cursor")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid run cursor") from exc


def _encode_worker_run_cursor(created_at: Any, strategy_run_id: str) -> str:
    run_id = str(strategy_run_id or "").strip()
    if not run_id or len(run_id) > 256:
        raise ValueError("Cannot create a cursor for a run without an ID")
    body = _json_dumps(
        {
            "v": _WORKER_RUN_CURSOR_VERSION,
            "created_at": _cursor_text(created_at),
            "strategy_run_id": run_id,
        }
    ).encode("utf-8")
    signature = hmac.new(_WORKER_RUN_CURSOR_SECRET, body, hashlib.sha256).digest()
    return f"{_cursor_b64encode(body)}.{_cursor_b64encode(signature)}"


def _decode_worker_run_cursor(cursor: str) -> Dict[str, str]:
    if not isinstance(cursor, str) or len(cursor) > 1024:
        raise ValueError("Invalid run cursor")
    parts = cursor.split(".")
    if len(parts) != 2:
        raise ValueError("Invalid run cursor")
    body = _cursor_b64decode(parts[0])
    signature = _cursor_b64decode(parts[1])
    expected = hmac.new(_WORKER_RUN_CURSOR_SECRET, body, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid run cursor")
    try:
        payload = _json_loads(body.decode("utf-8"), None)
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise ValueError("Invalid run cursor") from exc
    if not isinstance(payload, dict) or set(payload) != {"v", "created_at", "strategy_run_id"}:
        raise ValueError("Invalid run cursor")
    if payload.get("v") != _WORKER_RUN_CURSOR_VERSION:
        raise ValueError("Invalid run cursor")
    run_id = payload.get("strategy_run_id")
    created_at = payload.get("created_at")
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 256:
        raise ValueError("Invalid run cursor")
    if not isinstance(created_at, str) or not created_at.strip() or len(created_at) > 128:
        raise ValueError("Invalid run cursor")
    return {"created_at": created_at, "strategy_run_id": run_id}


@dataclass
class WorkerToken:
    token_id: str
    name: str
    account_scope: Optional[str]
    allowed_modes: List[str]
    allowed_actions: List[str]
    allowed_templates: List[str]
    status: str = "active"
    expires_at: Optional[datetime] = None

class SqlAlchemyAlgoWorkerRepository:
    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    async def create_token(self, payload: WorkerTokenCreateRequest, *, raw_token: str, token_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._create_token_sync, payload, raw_token, token_id)

    async def list_tokens(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_tokens_sync)

    async def revoke_token(self, token_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._revoke_token_sync, token_id)

    async def get_token_by_hash(self, token_hash: str) -> Optional[WorkerToken]:
        return await asyncio.to_thread(self._get_token_by_hash_sync, token_hash)

    async def touch_token(self, token_id: str) -> None:
        await asyncio.to_thread(self._touch_token_sync, token_id)

    async def record_heartbeat(self, token_id: str, payload: WorkerHeartbeatRequest) -> Dict[str, Any]:
        return await asyncio.to_thread(self._record_heartbeat_sync, token_id, payload)

    async def create_run(self, token: WorkerToken, payload: WorkerRunCreateRequest, *, strategy_run_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._create_run_sync, token, payload, strategy_run_id)

    async def get_run(self, strategy_run_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._get_run_sync, strategy_run_id)

    async def list_runs(
        self,
        token: WorkerToken,
        *,
        limit: int = WORKER_RUN_LIST_DEFAULT_LIMIT,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self._list_runs_sync, token, limit, cursor)

    async def list_runs_for_control_plane(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_runs_for_control_plane_sync)

    async def list_protection_enabled_runs(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_protection_enabled_runs_sync)

    async def update_run_risk(self, strategy_run_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._update_run_risk_sync, strategy_run_id, patch)

    async def update_run_runtime_state(self, strategy_run_id: str, runtime_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._update_run_runtime_state_sync, strategy_run_id, runtime_state)

    async def update_run_backend_protection(
        self,
        strategy_run_id: str,
        protection: Dict[str, Any],
        protection_state: Dict[str, Any],
        *,
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(
            self._update_run_backend_protection_sync,
            strategy_run_id,
            protection,
            protection_state,
            expected_generation,
            expected_triggered_rule,
            expected_exit_claim_id,
        )

    async def update_run_backend_protection_with_events(
        self,
        strategy_run_id: str,
        protection: Dict[str, Any],
        protection_state: Dict[str, Any],
        *,
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
        timeline_events: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(
            self._update_run_backend_protection_with_events_sync,
            strategy_run_id,
            protection,
            protection_state,
            expected_generation,
            expected_triggered_rule,
            expected_exit_claim_id,
            timeline_events,
        )

    async def update_run_backend_protection_state(
        self,
        strategy_run_id: str,
        protection_state: Dict[str, Any],
        *,
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(
            self._update_run_backend_protection_state_sync,
            strategy_run_id,
            protection_state,
            expected_generation,
            expected_triggered_rule,
            expected_exit_claim_id,
        )

    async def update_run_backend_protection_state_with_events(
        self,
        strategy_run_id: str,
        protection_state: Dict[str, Any],
        *,
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
        timeline_events: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(
            self._update_run_backend_protection_state_with_events_sync,
            strategy_run_id,
            protection_state,
            expected_generation,
            expected_triggered_rule,
            expected_exit_claim_id,
            timeline_events,
        )

    async def update_run_status(self, strategy_run_id: str, status: str, *, state_patch: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._update_run_status_sync, strategy_run_id, status, state_patch)

    async def claim_run_session(
        self,
        strategy_run_id: str,
        *,
        freshness_seconds: int,
        claimed_without_heartbeat_seconds: int,
    ) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(
            self._claim_run_session_sync,
            strategy_run_id,
            freshness_seconds,
            claimed_without_heartbeat_seconds,
        )

    async def release_run_session(self, strategy_run_id: str, *, expected_nonce: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._release_run_session_sync, strategy_run_id, expected_nonce)

    async def record_run_heartbeat(self, strategy_run_id: str, *, expected_nonce: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._record_run_heartbeat_sync, strategy_run_id, expected_nonce)

    async def list_stale_recovery_runs(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_stale_recovery_runs_sync)

    async def list_exiting_recovery_runs(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_exiting_recovery_runs_sync)

    async def list_live_strategy_open_legs(self, *, strategy_run_id: str, account_id: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_live_strategy_open_legs_sync, strategy_run_id, account_id)

    async def get_live_order_attribution_refs(self, *, strategy_run_id: str, account_id: str) -> Dict[str, List[str]]:
        return await asyncio.to_thread(self._get_live_order_attribution_refs_sync, strategy_run_id, account_id)

    async def list_live_strategy_broker_positions(self, *, strategy_run_id: str, account_id: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_live_strategy_broker_positions_sync, strategy_run_id, account_id)

    async def has_worker_execution_links(self, *, strategy_run_id: str, account_id: str) -> bool:
        return await asyncio.to_thread(self._has_worker_execution_links_sync, strategy_run_id, account_id)

    async def has_unresolved_worker_execution(self, *, strategy_run_id: str, account_id: str) -> bool:
        return await asyncio.to_thread(self._has_unresolved_worker_execution_sync, strategy_run_id, account_id)

    async def has_active_bracket_intent(self, *, strategy_run_id: str) -> bool:
        return await asyncio.to_thread(self._has_active_bracket_intent_sync, strategy_run_id)

    async def has_pending_bracket_actions(self, *, strategy_run_id: str) -> bool:
        return await asyncio.to_thread(self._has_pending_bracket_actions_sync, strategy_run_id)

    async def get_intent_result(self, strategy_run_id: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._get_intent_result_sync, strategy_run_id, idempotency_key)

    async def save_intent_result(self, *, token_id: str, strategy_run_id: str, request: WorkerIntentRequest, status: str, result: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._save_intent_result_sync,
            token_id=token_id,
            strategy_run_id=strategy_run_id,
            request=request,
            status=status,
            result=result,
        )

    async def begin_intent(
        self,
        *,
        token_id: str,
        strategy_run_id: str,
        request: WorkerIntentRequest,
        initial_result: Dict[str, Any],
        status: str = "pending",
        db: Optional[Session] = None,
    ) -> Dict[str, Any]:
        if db is not None:
            return self._begin_intent_sync(token_id, strategy_run_id, request, initial_result, status, db)
        return await asyncio.to_thread(
            self._begin_intent_sync,
            token_id,
            strategy_run_id,
            request,
            initial_result,
            status,
            db,
        )

    async def finalize_intent_result(
        self,
        *,
        strategy_run_id: str,
        idempotency_key: str,
        status: str,
        result: Dict[str, Any],
        db: Optional[Session] = None,
    ) -> Dict[str, Any]:
        if db is not None:
            return self._finalize_intent_result_sync(strategy_run_id, idempotency_key, status, result, db)
        return await asyncio.to_thread(
            self._finalize_intent_result_sync,
            strategy_run_id,
            idempotency_key,
            status,
            result,
            db,
        )

    def _create_token_sync(self, payload: WorkerTokenCreateRequest, raw_token: str, token_id: str) -> Dict[str, Any]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_tokens (
                        token_id, name, token_hash, account_scope, allowed_modes,
                        allowed_actions, allowed_templates, status, expires_at, metadata_json
                    ) VALUES (
                        :token_id, :name, :token_hash, :account_scope, CAST(:allowed_modes AS JSONB),
                        CAST(:allowed_actions AS JSONB), CAST(:allowed_templates AS JSONB),
                        'active', :expires_at, CAST(:metadata_json AS JSONB)
                    )
                    RETURNING token_id, name, account_scope, allowed_modes, allowed_actions,
                              allowed_templates, status, created_at, expires_at, last_used_at
                    """
                ),
                {
                    "token_id": token_id,
                    "name": payload.name,
                    "token_hash": _hash_token(raw_token),
                    "account_scope": payload.account_scope,
                    "allowed_modes": _json_dumps(payload.allowed_modes),
                    "allowed_actions": _json_dumps(payload.allowed_actions),
                    "allowed_templates": _json_dumps(payload.allowed_templates),
                    "expires_at": payload.expires_at,
                    "metadata_json": _json_dumps(payload.metadata),
                },
            ).fetchone()
            db.commit()
            return self._token_view(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _list_tokens_sync(self) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT token_id, name, account_scope, allowed_modes, allowed_actions,
                           allowed_templates, status, created_at, expires_at, last_used_at
                    FROM public.algo_worker_tokens
                    ORDER BY created_at DESC
                    """
                )
            ).fetchall()
            return [self._token_view(row) for row in rows]
        finally:
            db.close()

    def _revoke_token_sync(self, token_id: str) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_tokens
                    SET status = 'revoked', updated_at = NOW()
                    WHERE token_id = :token_id
                    RETURNING token_id, name, account_scope, allowed_modes, allowed_actions,
                              allowed_templates, status, created_at, expires_at, last_used_at
                    """
                ),
                {"token_id": token_id},
            ).fetchone()
            db.commit()
            return self._token_view(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _get_token_by_hash_sync(self, token_hash: str) -> Optional[WorkerToken]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT token_id, name, account_scope, allowed_modes, allowed_actions,
                           allowed_templates, status, expires_at
                    FROM public.algo_worker_tokens
                    WHERE token_hash = :token_hash
                    """
                ),
                {"token_hash": token_hash},
            ).fetchone()
            if row is None:
                return None
            payload = _row_mapping(row)
            return WorkerToken(
                token_id=str(payload["token_id"]),
                name=str(payload["name"]),
                account_scope=payload.get("account_scope"),
                allowed_modes=_json_loads(payload.get("allowed_modes"), []),
                allowed_actions=_json_loads(payload.get("allowed_actions"), []),
                allowed_templates=_json_loads(payload.get("allowed_templates"), []),
                status=str(payload.get("status") or "active"),
                expires_at=payload.get("expires_at"),
            )
        finally:
            db.close()

    def _touch_token_sync(self, token_id: str) -> None:
        db = self.session_factory()
        try:
            db.execute(
                text("UPDATE public.algo_worker_tokens SET last_used_at = NOW(), updated_at = NOW() WHERE token_id = :token_id"),
                {"token_id": token_id},
            )
            db.commit()
        finally:
            db.close()

    def _record_heartbeat_sync(self, token_id: str, payload: WorkerHeartbeatRequest) -> Dict[str, Any]:
        db = self.session_factory()
        try:
            db.execute(
                text(
                    """
                    UPDATE public.algo_worker_tokens
                    SET last_heartbeat_at = NOW(),
                        heartbeat_json = CAST(:heartbeat_json AS JSONB),
                        updated_at = NOW()
                    WHERE token_id = :token_id
                    """
                ),
                {
                    "token_id": token_id,
                    "heartbeat_json": _json_dumps(payload.model_dump(mode="json")),
                },
            )
            db.commit()
            return {"status": "ok", "token_id": token_id, "received_at": _utcnow().isoformat()}
        finally:
            db.close()

    def _create_run_sync(self, token: WorkerToken, payload: WorkerRunCreateRequest, strategy_run_id: str) -> Dict[str, Any]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_runs (
                        strategy_run_id, token_id, template_id, account_scope, execution_mode,
                        status, summary_fields_json, risk_schema_json, allowed_actions_json,
                        runtime_state_json, metadata_json
                    ) VALUES (
                        :strategy_run_id, :token_id, :template_id, :account_scope, :execution_mode,
                        'open', CAST(:summary_fields_json AS JSONB), CAST(:risk_schema_json AS JSONB),
                        CAST(:allowed_actions_json AS JSONB), CAST(:runtime_state_json AS JSONB),
                        CAST(:metadata_json AS JSONB)
                    )
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "token_id": token.token_id,
                    "template_id": payload.template_id,
                    "account_scope": payload.account_scope,
                    "execution_mode": payload.execution_mode,
                    "summary_fields_json": _json_dumps(payload.summary_fields),
                    "risk_schema_json": _json_dumps(payload.risk_schema),
                    "allowed_actions_json": _json_dumps(payload.allowed_actions),
                    "runtime_state_json": _json_dumps(payload.runtime_state),
                    "metadata_json": _json_dumps(payload.metadata),
                },
            ).fetchone()
            db.commit()
            return self._run_view(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _get_run_sync(self, strategy_run_id: str) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(text("SELECT * FROM public.algo_worker_runs WHERE strategy_run_id = :strategy_run_id"), {"strategy_run_id": strategy_run_id}).fetchone()
            return self._run_view(row) if row else None
        finally:
            db.close()

    def _list_runs_sync(
        self,
        token: WorkerToken,
        limit: int,
        cursor: Optional[str],
    ) -> Dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= WORKER_RUN_LIST_MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {WORKER_RUN_LIST_MAX_LIMIT}")

        cursor_values = _decode_worker_run_cursor(cursor) if cursor is not None else None
        db = self.session_factory()
        try:
            conditions = ["r.token_id = :token_id"]
            params: Dict[str, Any] = {"token_id": token.token_id}
            if cursor_values is not None:
                conditions.append(
                    "(r.created_at < :cursor_created_at "
                    "OR (r.created_at = :cursor_created_at "
                    "AND r.strategy_run_id < :cursor_strategy_run_id))"
                )
                params.update(
                    {
                        "cursor_created_at": cursor_values["created_at"],
                        "cursor_strategy_run_id": cursor_values["strategy_run_id"],
                    }
                )

            rows = [
                dict(row)
                for row in db.execute(
                    text(
                        """
                        SELECT r.strategy_run_id, r.token_id, r.template_id,
                               r.account_scope, r.execution_mode, r.status,
                               r.metadata_json, r.created_at, r.updated_at, r.closed_at
                        FROM public.algo_worker_runs r
                        WHERE __WHERE__
                        ORDER BY r.created_at DESC, r.strategy_run_id DESC
                        """.replace("__WHERE__", " AND ".join(conditions))
                    ),
                    params,
                ).mappings().all()
            ]
        finally:
            db.close()

        # Filtering is intentionally applied to the token-owned rows before
        # selecting a page.  In particular, the query must never reuse the
        # unrestricted control-plane listing and then redact its output.
        visible_rows = [
            row
            for row in rows
            if _worker_run_is_visible_to_token(token, row)
        ]
        page = visible_rows[: limit + 1]
        has_more = len(page) > limit
        items = [self._run_summary(row) for row in page[:limit]]
        next_cursor = None
        if has_more and items:
            last = page[limit - 1]
            next_cursor = _encode_worker_run_cursor(
                last.get("created_at"), str(last.get("strategy_run_id") or "")
            )
        return {"items": items, "next_cursor": next_cursor}

    def _list_runs_for_control_plane_sync(self) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                        r.*,
                        t.name AS worker_name,
                        t.last_heartbeat_at AS token_last_heartbeat_at,
                        t.heartbeat_json
                    FROM public.algo_worker_runs r
                    LEFT JOIN public.algo_worker_tokens t ON t.token_id = r.token_id
                    ORDER BY COALESCE(r.updated_at, r.created_at) DESC
                    """
                )
            ).fetchall()
            return [self._run_view_with_worker(row) for row in rows]
        finally:
            db.close()

    def _list_protection_enabled_runs_sync(self) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                        r.*,
                        t.name AS worker_name,
                        t.last_heartbeat_at AS token_last_heartbeat_at,
                        t.heartbeat_json
                    FROM public.algo_worker_runs r
                    LEFT JOIN public.algo_worker_tokens t ON t.token_id = r.token_id
                    WHERE r.status IN ('open', 'exiting')
                      AND COALESCE((r.runtime_state_json -> 'backend_protection' ->> 'enabled')::BOOLEAN, FALSE) = TRUE
                    ORDER BY COALESCE(r.updated_at, r.created_at) ASC
                    """
                )
            ).fetchall()
            return [self._run_view_with_worker(row) for row in rows]
        finally:
            db.close()

    def _update_run_risk_sync(self, strategy_run_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        run = self._get_run_sync(strategy_run_id)
        if run is None:
            raise KeyError(strategy_run_id)
        state = dict(run.get("runtime_state") or {})
        risk = dict(state.get("risk") or {})
        risk.update(patch)
        state["risk"] = risk

        risk_schema = []
        for field in list(run.get("risk_schema") or []):
            item = dict(field)
            if item.get("key") in patch:
                item["value"] = patch[item["key"]]
            risk_schema.append(item)

        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET runtime_state_json = CAST(:runtime_state_json AS JSONB),
                        risk_schema_json = CAST(:risk_schema_json AS JSONB),
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "runtime_state_json": _json_dumps(state),
                    "risk_schema_json": _json_dumps(risk_schema),
                },
            ).fetchone()
            db.commit()
            return self._run_view(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _update_run_status_sync(self, strategy_run_id: str, status: str, state_patch: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        run = self._get_run_sync(strategy_run_id)
        if run is None:
            return None
        state = dict(run.get("runtime_state") or {})
        if state_patch:
            state.update(state_patch)
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET status = :status,
                        runtime_state_json = CAST(:runtime_state_json AS JSONB),
                        updated_at = NOW(),
                        closed_at = CASE WHEN :status = 'closed' THEN NOW() ELSE closed_at END
                    WHERE strategy_run_id = :strategy_run_id
                    RETURNING *
                    """
                ),
                {"strategy_run_id": strategy_run_id, "status": status, "runtime_state_json": _json_dumps(state)},
            ).fetchone()
            db.commit()
            return self._run_view(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _claim_run_session_sync(
        self,
        strategy_run_id: str,
        freshness_seconds: int,
        claimed_without_heartbeat_seconds: int,
    ) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            now = _utcnow()
            heartbeat_stale_before = now - timedelta(seconds=max(1, int(freshness_seconds)))
            claim_stale_before = now - timedelta(seconds=max(1, int(claimed_without_heartbeat_seconds)))
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET worker_session_nonce = :nonce,
                        worker_session_claimed_at = NOW(),
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                      AND (
                        worker_session_nonce IS NULL
                        OR (
                          last_heartbeat_at IS NOT NULL
                          AND last_heartbeat_at < :heartbeat_stale_before
                        )
                        OR (
                          last_heartbeat_at IS NULL
                          AND worker_session_claimed_at IS NOT NULL
                          AND worker_session_claimed_at < :claim_stale_before
                        )
                      )
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "nonce": f"wsn_{uuid.uuid4().hex}",
                    "heartbeat_stale_before": heartbeat_stale_before,
                    "claim_stale_before": claim_stale_before,
                },
            ).fetchone()
            db.commit()
            return self._run_view(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _release_run_session_sync(self, strategy_run_id: str, expected_nonce: str) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET worker_session_nonce = NULL,
                        worker_session_claimed_at = NULL,
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                      AND worker_session_nonce = :expected_nonce
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "expected_nonce": expected_nonce,
                },
            ).fetchone()
            db.commit()
            return self._run_view(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _record_run_heartbeat_sync(self, strategy_run_id: str, expected_nonce: str) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET last_heartbeat_at = NOW(),
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                      AND worker_session_nonce = :expected_nonce
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "expected_nonce": expected_nonce,
                },
            ).fetchone()
            db.commit()
            return self._run_view(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _list_stale_recovery_runs_sync(self) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT r.*
                    FROM public.algo_worker_runs r
                    WHERE r.status IN ('open', 'paused')
                      AND NOT (
                        r.worker_session_nonce IS NULL
                        AND r.last_heartbeat_at IS NULL
                      )
                      AND NOT (
                        COALESCE((r.runtime_state_json -> 'backend_protection' ->> 'enabled')::BOOLEAN, FALSE) = TRUE
                        AND COALESCE((r.runtime_state_json -> 'backend_protection' -> 'operations' ->> 'exit_on_worker_stale')::BOOLEAN, FALSE) = TRUE
                      )
                      AND (
                        (
                          r.last_heartbeat_at IS NOT NULL
                          AND r.last_heartbeat_at < NOW() - (CAST(:stale_seconds AS TEXT) || ' seconds')::INTERVAL
                        )
                        OR (
                          r.last_heartbeat_at IS NULL
                          AND r.worker_session_claimed_at IS NOT NULL
                          AND r.worker_session_claimed_at < NOW() - (CAST(:claimed_without_heartbeat_seconds AS TEXT) || ' seconds')::INTERVAL
                        )
                      )
                    ORDER BY COALESCE(r.updated_at, r.created_at) ASC
                    """
                ),
                {
                    "stale_seconds": max(1, int(WORKER_RUN_STALE_ACTION_SECONDS)),
                    "claimed_without_heartbeat_seconds": max(1, int(WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS)),
                },
            ).fetchall()
            return [self._run_view(row) for row in rows]
        finally:
            db.close()

    def _list_exiting_recovery_runs_sync(self) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.algo_worker_runs
                    WHERE status = 'exiting'
                    ORDER BY COALESCE(updated_at, created_at) ASC
                    """
                )
            ).fetchall()
            return [self._run_view(row) for row in rows]
        finally:
            db.close()

    def _update_run_runtime_state_sync(self, strategy_run_id: str, runtime_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET runtime_state_json = CAST(:runtime_state_json AS JSONB),
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                    RETURNING *
                    """
                ),
                {"strategy_run_id": strategy_run_id, "runtime_state_json": _json_dumps(runtime_state)},
            ).fetchone()
            db.commit()
            return self._run_view(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _persist_backend_protection_change_sync(
        self,
        *,
        strategy_run_id: str,
        protection: Optional[Dict[str, Any]],
        protection_state: Dict[str, Any],
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
        timeline_events: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.algo_worker_runs
                    WHERE strategy_run_id = :strategy_run_id
                    FOR UPDATE
                    """
                ),
                {"strategy_run_id": strategy_run_id},
            ).fetchone()
            if row is None:
                db.rollback()
                return None

            run_payload = self._run_view(row)
            runtime_state = dict(run_payload.get("runtime_state") or {})
            current_state = dict(runtime_state.get("backend_protection_state") or {})

            if expected_generation is not None and _to_int(current_state.get("generation"), default=0) != _to_int(expected_generation, default=0):
                db.rollback()
                return None
            if expected_triggered_rule is not None and str(current_state.get("triggered_rule") or "") != str(expected_triggered_rule):
                db.rollback()
                return None
            if expected_exit_claim_id is not None and str(current_state.get("exit_claim_id") or "") != str(expected_exit_claim_id):
                db.rollback()
                return None

            if protection is not None:
                runtime_state["backend_protection"] = dict(protection)
            runtime_state["backend_protection_state"] = dict(protection_state)

            updated = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET runtime_state_json = CAST(:runtime_state_json AS JSONB),
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "runtime_state_json": _json_dumps(runtime_state),
                },
            ).fetchone()
            if updated is None:
                db.rollback()
                return None

            committed_events: List[Dict[str, Any]] = []
            for timeline_event in list(timeline_events or []):
                payload = dict(timeline_event)
                committed = worker_timeline_store.append_event(
                    db=db,
                    strategy_run_id=strategy_run_id,
                    account_id=str(run_payload.get("account_scope") or ""),
                    basket_execution_id=payload.get("basket_execution_id"),
                    event_kind=str(payload.get("event_kind") or "protection"),
                    event_source=str(payload.get("event_source") or "backend_protection"),
                    event_type=str(payload.get("event_type") or "protection.state_changed"),
                    related_resource_type=payload.get("related_resource_type"),
                    related_resource_id=payload.get("related_resource_id"),
                    summary=payload.get("summary"),
                    payload=dict(payload.get("payload") or {}),
                )
                committed_events.append(committed)

            db.commit()
            return {
                "run": self._run_view(updated),
                "timeline_events": committed_events,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _update_run_backend_protection_with_events_sync(
        self,
        strategy_run_id: str,
        protection: Dict[str, Any],
        protection_state: Dict[str, Any],
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
        timeline_events: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._persist_backend_protection_change_sync(
            strategy_run_id=strategy_run_id,
            protection=protection,
            protection_state=protection_state,
            expected_generation=expected_generation,
            expected_triggered_rule=expected_triggered_rule,
            expected_exit_claim_id=expected_exit_claim_id,
            timeline_events=timeline_events,
        )

    def _update_run_backend_protection_state_with_events_sync(
        self,
        strategy_run_id: str,
        protection_state: Dict[str, Any],
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
        timeline_events: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._persist_backend_protection_change_sync(
            strategy_run_id=strategy_run_id,
            protection=None,
            protection_state=protection_state,
            expected_generation=expected_generation,
            expected_triggered_rule=expected_triggered_rule,
            expected_exit_claim_id=expected_exit_claim_id,
            timeline_events=timeline_events,
        )

    def _update_run_backend_protection_sync(
        self,
        strategy_run_id: str,
        protection: Dict[str, Any],
        protection_state: Dict[str, Any],
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET runtime_state_json = jsonb_set(
                            jsonb_set(
                                COALESCE(runtime_state_json, '{}'::jsonb),
                                '{backend_protection}',
                                CAST(:protection_json AS JSONB),
                                true
                            ),
                            '{backend_protection_state}',
                            CAST(:protection_state_json AS JSONB),
                            true
                        ),
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                      AND (
                        :expected_generation IS NULL
                        OR COALESCE((runtime_state_json -> 'backend_protection_state' ->> 'generation')::INTEGER, 0) = :expected_generation
                      )
                      AND (
                        :check_triggered_rule = FALSE
                        OR COALESCE(runtime_state_json -> 'backend_protection_state' ->> 'triggered_rule', '') = :expected_triggered_rule
                      )
                      AND (
                        :check_exit_claim_id = FALSE
                        OR COALESCE(runtime_state_json -> 'backend_protection_state' ->> 'exit_claim_id', '') = :expected_exit_claim_id
                      )
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "protection_json": _json_dumps(protection),
                    "protection_state_json": _json_dumps(protection_state),
                    "expected_generation": expected_generation,
                    "check_triggered_rule": expected_triggered_rule is not None,
                    "expected_triggered_rule": expected_triggered_rule or "",
                    "check_exit_claim_id": expected_exit_claim_id is not None,
                    "expected_exit_claim_id": expected_exit_claim_id or "",
                },
            ).fetchone()
            db.commit()
            return self._run_view(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _update_run_backend_protection_state_sync(
        self,
        strategy_run_id: str,
        protection_state: Dict[str, Any],
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET runtime_state_json = jsonb_set(
                            COALESCE(runtime_state_json, '{}'::jsonb),
                            '{backend_protection_state}',
                            CAST(:protection_state_json AS JSONB),
                            true
                        ),
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                      AND (
                        :expected_generation IS NULL
                        OR COALESCE((runtime_state_json -> 'backend_protection_state' ->> 'generation')::INTEGER, 0) = :expected_generation
                      )
                      AND (
                        :check_triggered_rule = FALSE
                        OR COALESCE(runtime_state_json -> 'backend_protection_state' ->> 'triggered_rule', '') = :expected_triggered_rule
                      )
                      AND (
                        :check_exit_claim_id = FALSE
                        OR COALESCE(runtime_state_json -> 'backend_protection_state' ->> 'exit_claim_id', '') = :expected_exit_claim_id
                      )
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "protection_state_json": _json_dumps(protection_state),
                    "expected_generation": expected_generation,
                    "check_triggered_rule": expected_triggered_rule is not None,
                    "expected_triggered_rule": expected_triggered_rule or "",
                    "check_exit_claim_id": expected_exit_claim_id is not None,
                    "expected_exit_claim_id": expected_exit_claim_id or "",
                },
            ).fetchone()
            db.commit()
            return self._run_view(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _list_live_strategy_open_legs_sync(self, strategy_run_id: str, account_id: str) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            has_exact_links = self._has_worker_execution_links_sync(strategy_run_id, account_id, db=db)
            if has_exact_links:
                rows = db.execute(
                    text(
                        """
                        WITH attributed_orders AS (
                            SELECT DISTINCT
                                wl.account_id,
                                wl.broker_order_id
                            FROM public.worker_live_execution_links wl
                            WHERE wl.strategy_run_id = :strategy_run_id
                              AND wl.account_id = :account_id
                              AND COALESCE(NULLIF(wl.broker_order_id, ''), '') <> ''
                        ),
                        leg_facts AS (
                            SELECT
                                CAST(NULL AS UUID) AS journal_run_id,
                                otf.account_id,
                                otf.instrument_token,
                                COALESCE(NULLIF(otf.exchange, ''), COALESCE(otf.payload_json ->> 'exchange', '')) AS exchange,
                                COALESCE(NULLIF(otf.tradingsymbol, ''), COALESCE(otf.payload_json ->> 'tradingsymbol', '')) AS tradingsymbol,
                                COALESCE(NULLIF(otf.product, ''), COALESCE(otf.payload_json ->> 'product', '')) AS product,
                                SUM(
                                    CASE
                                        WHEN UPPER(COALESCE(NULLIF(otf.transaction_type, ''), COALESCE(otf.payload_json ->> 'transaction_type', ''))) = 'BUY'
                                            THEN otf.quantity
                                        ELSE -otf.quantity
                                    END
                                ) AS net_quantity
                            FROM public.order_trade_fills otf
                            INNER JOIN attributed_orders ao
                              ON ao.account_id = otf.account_id
                             AND ao.broker_order_id = otf.order_id
                            GROUP BY
                                otf.account_id,
                                otf.instrument_token,
                                COALESCE(NULLIF(otf.exchange, ''), COALESCE(otf.payload_json ->> 'exchange', '')),
                                COALESCE(NULLIF(otf.tradingsymbol, ''), COALESCE(otf.payload_json ->> 'tradingsymbol', '')),
                                COALESCE(NULLIF(otf.product, ''), COALESCE(otf.payload_json ->> 'product', ''))
                        )
                        SELECT
                            lf.journal_run_id,
                            lf.account_id,
                            lf.instrument_token,
                            lf.exchange,
                            lf.tradingsymbol,
                            lf.product,
                            lf.net_quantity,
                            ap.net_quantity AS broker_net_quantity
                        FROM leg_facts lf
                        LEFT JOIN public.account_positions ap
                          ON ap.account_id = lf.account_id
                          AND ap.instrument_token = lf.instrument_token
                          AND ap.product = lf.product
                          AND ap.exchange = lf.exchange
                          AND ap.tradingsymbol = lf.tradingsymbol
                        WHERE lf.net_quantity <> 0
                        ORDER BY lf.exchange, lf.tradingsymbol, lf.product
                        """
                    ),
                    {"strategy_run_id": strategy_run_id, "account_id": account_id},
                ).mappings().all()
                return [dict(row) for row in rows]

            rows = db.execute(
                text(
                    """
                    WITH attributed_orders AS (
                        SELECT DISTINCT
                            resolved.account_id,
                            resolved.broker_order_id
                        FROM (
                            SELECT
                                loi.account_id,
                                loi.broker_order_id
                            FROM public.live_order_intents loi
                            WHERE loi.strategy_run_id = :strategy_run_id
                              AND loi.account_id = :account_id
                              AND COALESCE(NULLIF(loi.broker_order_id, ''), '') <> ''
                            UNION ALL
                            SELECT
                                loi.account_id,
                                coe.order_id AS broker_order_id
                            FROM public.live_order_intents loi
                            INNER JOIN public.canonical_order_events coe
                              ON coe.account_id = loi.account_id
                             AND CAST(coe.payload_json AS TEXT) LIKE '%"tag"%'
                             AND CAST(coe.payload_json AS TEXT) LIKE '%' || '"' || loi.client_order_ref || '"' || '%'
                            WHERE loi.strategy_run_id = :strategy_run_id
                              AND loi.account_id = :account_id
                              AND COALESCE(NULLIF(loi.client_order_ref, ''), '') <> ''
                        ) resolved
                        WHERE COALESCE(NULLIF(resolved.broker_order_id, ''), '') <> ''
                    ),
                    leg_facts AS (
                        SELECT
                            CAST(NULL AS UUID) AS journal_run_id,
                            otf.account_id,
                            otf.instrument_token,
                            COALESCE(NULLIF(otf.exchange, ''), COALESCE(otf.payload_json ->> 'exchange', '')) AS exchange,
                            COALESCE(NULLIF(otf.tradingsymbol, ''), COALESCE(otf.payload_json ->> 'tradingsymbol', '')) AS tradingsymbol,
                            COALESCE(NULLIF(otf.product, ''), COALESCE(otf.payload_json ->> 'product', '')) AS product,
                            SUM(
                                CASE
                                    WHEN UPPER(COALESCE(NULLIF(otf.transaction_type, ''), COALESCE(otf.payload_json ->> 'transaction_type', ''))) = 'BUY'
                                        THEN otf.quantity
                                    ELSE -otf.quantity
                                END
                            ) AS net_quantity
                        FROM public.order_trade_fills otf
                        INNER JOIN attributed_orders ao
                          ON ao.account_id = otf.account_id
                         AND ao.broker_order_id = otf.order_id
                        GROUP BY
                            otf.account_id,
                            otf.instrument_token,
                            COALESCE(NULLIF(otf.exchange, ''), COALESCE(otf.payload_json ->> 'exchange', '')),
                            COALESCE(NULLIF(otf.tradingsymbol, ''), COALESCE(otf.payload_json ->> 'tradingsymbol', '')),
                            COALESCE(NULLIF(otf.product, ''), COALESCE(otf.payload_json ->> 'product', ''))
                    )
                    SELECT
                        lf.journal_run_id,
                        lf.account_id,
                        lf.instrument_token,
                        lf.exchange,
                        lf.tradingsymbol,
                        lf.product,
                        lf.net_quantity,
                        ap.net_quantity AS broker_net_quantity
                    FROM leg_facts lf
                    LEFT JOIN public.account_positions ap
                      ON ap.account_id = lf.account_id
                      AND ap.instrument_token = lf.instrument_token
                      AND ap.product = lf.product
                      AND ap.exchange = lf.exchange
                      AND ap.tradingsymbol = lf.tradingsymbol
                    WHERE lf.net_quantity <> 0
                    ORDER BY lf.exchange, lf.tradingsymbol, lf.product
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).mappings().all()
            return [dict(row) for row in rows]
        finally:
            db.close()

    def _get_live_order_attribution_refs_sync(self, strategy_run_id: str, account_id: str) -> Dict[str, List[str]]:
        db = self.session_factory()
        try:
            has_exact_links = self._has_worker_execution_links_sync(strategy_run_id, account_id, db=db)
            if has_exact_links:
                rows = db.execute(
                    text(
                        """
                        SELECT broker_order_id, client_order_ref
                        FROM public.worker_live_execution_links
                        WHERE strategy_run_id = :strategy_run_id
                          AND account_id = :account_id
                        """
                    ),
                    {"strategy_run_id": strategy_run_id, "account_id": account_id},
                ).mappings().all()
                broker_order_ids = sorted(
                    {
                        str(row.get("broker_order_id") or "").strip()
                        for row in rows
                        if str(row.get("broker_order_id") or "").strip()
                    }
                )
                client_order_refs = sorted(
                    {
                        str(row.get("client_order_ref") or "").strip()
                        for row in rows
                        if str(row.get("client_order_ref") or "").strip()
                    }
                )
                return {
                    "broker_order_ids": broker_order_ids,
                    "client_order_refs": client_order_refs,
                }

            rows = db.execute(
                text(
                    """
                    SELECT client_order_ref, broker_order_id
                    FROM public.live_order_intents
                    WHERE strategy_run_id = :strategy_run_id
                      AND account_id = :account_id
                    ORDER BY created_at DESC
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).mappings().all()
            recovered_rows = db.execute(
                text(
                    """
                    SELECT DISTINCT
                        loi.client_order_ref,
                        coe.order_id AS broker_order_id
                    FROM public.live_order_intents loi
                    INNER JOIN public.canonical_order_events coe
                      ON coe.account_id = loi.account_id
                     AND CAST(coe.payload_json AS TEXT) LIKE '%"tag"%'
                     AND CAST(coe.payload_json AS TEXT) LIKE '%' || '"' || loi.client_order_ref || '"' || '%'
                    WHERE loi.strategy_run_id = :strategy_run_id
                      AND loi.account_id = :account_id
                      AND COALESCE(NULLIF(loi.client_order_ref, ''), '') <> ''
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).mappings().all()
            combined_rows = [dict(row) for row in rows] + [dict(row) for row in recovered_rows]
            broker_order_ids = sorted(
                {
                    str(row.get("broker_order_id") or "").strip()
                    for row in combined_rows
                    if str(row.get("broker_order_id") or "").strip()
                }
            )
            client_order_refs = sorted(
                {
                    str(row.get("client_order_ref") or "").strip()
                    for row in combined_rows
                    if str(row.get("client_order_ref") or "").strip()
                }
            )
            return {
                "broker_order_ids": broker_order_ids,
                "client_order_refs": client_order_refs,
            }
        finally:
            db.close()

    def _list_live_strategy_broker_positions_sync(self, strategy_run_id: str, account_id: str) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            has_exact_links = self._has_worker_execution_links_sync(strategy_run_id, account_id, db=db)
            if has_exact_links:
                rows = db.execute(
                    text(
                        """
                        WITH attributed_orders AS (
                            SELECT DISTINCT
                                wl.account_id,
                                wl.broker_order_id
                            FROM public.worker_live_execution_links wl
                            WHERE wl.strategy_run_id = :strategy_run_id
                              AND wl.account_id = :account_id
                              AND COALESCE(NULLIF(wl.broker_order_id, ''), '') <> ''
                        ),
                        attributed_instruments AS (
                            SELECT DISTINCT
                                osp.account_id,
                                osp.instrument_token,
                                osp.exchange,
                                osp.tradingsymbol,
                                osp.product
                            FROM public.order_state_projection osp
                            INNER JOIN attributed_orders ao
                              ON ao.account_id = osp.account_id
                             AND ao.broker_order_id = osp.order_id
                            WHERE COALESCE(osp.instrument_token, 0) <> 0
                              AND COALESCE(NULLIF(osp.exchange, ''), '') <> ''
                              AND COALESCE(NULLIF(osp.tradingsymbol, ''), '') <> ''
                              AND COALESCE(NULLIF(osp.product, ''), '') <> ''
                        )
                        SELECT
                            ap.account_id,
                            ap.instrument_token,
                            ap.exchange,
                            ap.tradingsymbol,
                            ap.product,
                            ap.net_quantity,
                            ap.average_price,
                            ap.last_price,
                            ap.updated_at,
                            ap.last_reconciled_at
                        FROM public.account_positions ap
                        INNER JOIN attributed_instruments ai
                          ON ai.account_id = ap.account_id
                         AND ai.instrument_token = ap.instrument_token
                         AND ai.exchange = ap.exchange
                         AND ai.tradingsymbol = ap.tradingsymbol
                         AND ai.product = ap.product
                        WHERE ap.net_quantity <> 0
                        ORDER BY ap.exchange, ap.tradingsymbol, ap.product
                        """
                    ),
                    {"strategy_run_id": strategy_run_id, "account_id": account_id},
                ).mappings().all()
                return [dict(row) for row in rows]

            rows = db.execute(
                text(
                    """
                    WITH attributed_orders AS (
                        SELECT DISTINCT
                            resolved.account_id,
                            resolved.broker_order_id
                        FROM (
                            SELECT
                                loi.account_id,
                                loi.broker_order_id
                            FROM public.live_order_intents loi
                            WHERE loi.strategy_run_id = :strategy_run_id
                              AND loi.account_id = :account_id
                              AND COALESCE(NULLIF(loi.broker_order_id, ''), '') <> ''
                            UNION ALL
                            SELECT
                                loi.account_id,
                                coe.order_id AS broker_order_id
                            FROM public.live_order_intents loi
                            INNER JOIN public.canonical_order_events coe
                              ON coe.account_id = loi.account_id
                             AND CAST(coe.payload_json AS TEXT) LIKE '%"tag"%'
                             AND CAST(coe.payload_json AS TEXT) LIKE '%' || '"' || loi.client_order_ref || '"' || '%'
                            WHERE loi.strategy_run_id = :strategy_run_id
                              AND loi.account_id = :account_id
                              AND COALESCE(NULLIF(loi.client_order_ref, ''), '') <> ''
                        ) resolved
                        WHERE COALESCE(NULLIF(resolved.broker_order_id, ''), '') <> ''
                    ),
                    attributed_instruments AS (
                        SELECT DISTINCT
                            osp.account_id,
                            osp.instrument_token,
                            osp.exchange,
                            osp.tradingsymbol,
                            osp.product
                        FROM public.order_state_projection osp
                        INNER JOIN attributed_orders ao
                          ON ao.account_id = osp.account_id
                         AND ao.broker_order_id = osp.order_id
                        WHERE COALESCE(osp.instrument_token, 0) <> 0
                          AND COALESCE(NULLIF(osp.exchange, ''), '') <> ''
                          AND COALESCE(NULLIF(osp.tradingsymbol, ''), '') <> ''
                          AND COALESCE(NULLIF(osp.product, ''), '') <> ''
                    )
                    SELECT
                        ap.account_id,
                        ap.instrument_token,
                        ap.exchange,
                        ap.tradingsymbol,
                        ap.product,
                        ap.net_quantity,
                        ap.average_price,
                        ap.last_price,
                        ap.updated_at,
                        ap.last_reconciled_at
                    FROM public.account_positions ap
                    INNER JOIN attributed_instruments ai
                      ON ai.account_id = ap.account_id
                     AND ai.instrument_token = ap.instrument_token
                     AND ai.exchange = ap.exchange
                     AND ai.tradingsymbol = ap.tradingsymbol
                     AND ai.product = ap.product
                    WHERE ap.net_quantity <> 0
                    ORDER BY ap.exchange, ap.tradingsymbol, ap.product
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).mappings().all()
            return [dict(row) for row in rows]
        finally:
            db.close()

    def _has_worker_execution_links_sync(self, strategy_run_id: str, account_id: str, db: Optional[Session] = None) -> bool:
        owns_db = db is None
        session = db or self.session_factory()
        try:
            row = session.execute(
                text(
                    """
                    SELECT 1
                    FROM public.worker_live_execution_links
                    WHERE strategy_run_id = :strategy_run_id
                      AND account_id = :account_id
                    LIMIT 1
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).fetchone()
            return bool(row)
        finally:
            if owns_db:
                session.close()

    def _has_unresolved_worker_execution_sync(self, strategy_run_id: str, account_id: str) -> bool:
        db = self.session_factory()
        try:
            net_row = db.execute(
                text(
                    """
                    SELECT COALESCE(SUM(
                        CASE
                            WHEN UPPER(COALESCE(otf.transaction_type, '')) = 'BUY' THEN otf.quantity
                            WHEN UPPER(COALESCE(otf.transaction_type, '')) = 'SELL' THEN -otf.quantity
                            ELSE 0
                        END
                    ), 0) AS net_quantity
                    FROM public.worker_live_execution_links wl
                    INNER JOIN public.order_trade_fills otf
                      ON otf.account_id = wl.account_id
                     AND otf.trade_id = wl.trade_id
                    WHERE wl.strategy_run_id = :strategy_run_id
                      AND wl.account_id = :account_id
                      AND wl.trade_id IS NOT NULL
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).fetchone()
            if _to_int((net_row[0] if net_row else 0)) != 0:
                return True

            pending_row = db.execute(
                text(
                    """
                    SELECT 1
                    FROM public.worker_live_execution_links wl
                    LEFT JOIN public.order_state_projection osp
                      ON osp.account_id = wl.account_id
                     AND osp.order_id = wl.broker_order_id
                    WHERE wl.strategy_run_id = :strategy_run_id
                      AND wl.account_id = :account_id
                      AND wl.trade_id IS NULL
                      AND (
                        osp.order_id IS NULL
                        OR UPPER(COALESCE(osp.latest_status, '')) NOT IN ('COMPLETE', 'CANCELLED', 'REJECTED', 'LAPSED')
                      )
                    LIMIT 1
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).fetchone()
            return bool(pending_row)
        finally:
            db.close()

    def _has_active_bracket_intent_sync(self, strategy_run_id: str) -> bool:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT 1
                    FROM public.bracket_intents
                    WHERE strategy_run_id = :strategy_run_id
                      AND status IN ('entry_submitting', 'entry_working', 'arming_exits', 'armed', 'cancelling')
                    LIMIT 1
                    """
                ),
                {"strategy_run_id": strategy_run_id},
            ).fetchone()
            return bool(row)
        finally:
            db.close()

    def _has_pending_bracket_actions_sync(self, strategy_run_id: str) -> bool:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT 1
                    FROM public.bracket_actions
                    WHERE strategy_run_id = :strategy_run_id
                      AND status IN ('pending', 'claimed')
                    LIMIT 1
                    """
                ),
                {"strategy_run_id": strategy_run_id},
            ).fetchone()
            return bool(row)
        finally:
            db.close()

    def _get_intent_result_sync(self, strategy_run_id: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT result_json
                    FROM public.algo_worker_intents
                    WHERE strategy_run_id = :strategy_run_id AND idempotency_key = :idempotency_key
                    """
                ),
                {"strategy_run_id": strategy_run_id, "idempotency_key": idempotency_key},
            ).fetchone()
            if row is None:
                return None
            return _json_loads(_row_mapping(row).get("result_json"), {})
        finally:
            db.close()

    def _save_intent_result_sync(self, *, token_id: str, strategy_run_id: str, request: WorkerIntentRequest, status: str, result: Dict[str, Any]) -> Dict[str, Any]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_intents (
                        token_id, strategy_run_id, idempotency_key, intent_type,
                        request_json, status, result_json
                    ) VALUES (
                        :token_id, :strategy_run_id, :idempotency_key, :intent_type,
                        CAST(:request_json AS JSONB), :status, CAST(:result_json AS JSONB)
                    )
                    ON CONFLICT (strategy_run_id, idempotency_key) DO UPDATE SET
                        result_json = public.algo_worker_intents.result_json
                    RETURNING result_json
                    """
                ),
                {
                    "token_id": token_id,
                    "strategy_run_id": strategy_run_id,
                    "idempotency_key": request.idempotency_key,
                    "intent_type": request.intent_type,
                    "request_json": request.model_dump_json(),
                    "status": status,
                    "result_json": _json_dumps(result),
                },
            ).fetchone()
            db.commit()
            return _json_loads(_row_mapping(row).get("result_json"), result)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _begin_intent_sync(
        self,
        token_id: str,
        strategy_run_id: str,
        request: WorkerIntentRequest,
        initial_result: Dict[str, Any],
        status: str,
        db: Optional[Session] = None,
    ) -> Dict[str, Any]:
        owns_db = db is None
        session = db or self.session_factory()
        try:
            inserted = session.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_intents (
                        token_id, strategy_run_id, idempotency_key, intent_type,
                        request_json, status, result_json
                    ) VALUES (
                        :token_id, :strategy_run_id, :idempotency_key, :intent_type,
                        CAST(:request_json AS JSONB), :status, CAST(:result_json AS JSONB)
                    )
                    ON CONFLICT (strategy_run_id, idempotency_key) DO NOTHING
                    RETURNING status, result_json
                    """
                ),
                {
                    "token_id": token_id,
                    "strategy_run_id": strategy_run_id,
                    "idempotency_key": request.idempotency_key,
                    "intent_type": request.intent_type,
                    "request_json": request.model_dump_json(),
                    "status": status,
                    "result_json": _json_dumps(initial_result),
                },
            ).fetchone()
            if inserted is not None:
                if owns_db:
                    session.commit()
                mapped = _row_mapping(inserted)
                return {
                    "status": str(mapped.get("status") or status),
                    "result": _json_loads(mapped.get("result_json"), initial_result),
                    "claimed": True,
                }

            row = session.execute(
                text(
                    """
                    SELECT status, result_json
                    FROM public.algo_worker_intents
                    WHERE strategy_run_id = :strategy_run_id
                      AND idempotency_key = :idempotency_key
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "idempotency_key": request.idempotency_key,
                },
            ).fetchone()
            if owns_db:
                session.commit()
            mapped = _row_mapping(row)
            return {
                "status": str(mapped.get("status") or status),
                "result": _json_loads(mapped.get("result_json"), initial_result),
                "claimed": False,
            }
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    def _finalize_intent_result_sync(
        self,
        strategy_run_id: str,
        idempotency_key: str,
        status: str,
        result: Dict[str, Any],
        db: Optional[Session] = None,
    ) -> Dict[str, Any]:
        owns_db = db is None
        session = db or self.session_factory()
        try:
            row = session.execute(
                text(
                    """
                    UPDATE public.algo_worker_intents
                    SET status = :status,
                        result_json = CAST(:result_json AS JSONB)
                    WHERE strategy_run_id = :strategy_run_id
                      AND idempotency_key = :idempotency_key
                    RETURNING status, result_json
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "idempotency_key": idempotency_key,
                    "status": status,
                    "result_json": _json_dumps(result),
                },
            ).fetchone()
            if row is None:
                raise KeyError(f"Intent not found for finalize: {strategy_run_id}:{idempotency_key}")
            if owns_db:
                session.commit()
            mapped = _row_mapping(row)
            return {
                "status": str(mapped.get("status") or status),
                "result": _json_loads(mapped.get("result_json"), result),
            }
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    def _token_view(self, row: Any) -> Dict[str, Any]:
        payload = _row_mapping(row)
        return {
            "token_id": str(payload["token_id"]),
            "name": str(payload["name"]),
            "account_scope": payload.get("account_scope"),
            "allowed_modes": _json_loads(payload.get("allowed_modes"), []),
            "allowed_actions": _json_loads(payload.get("allowed_actions"), []),
            "allowed_templates": _json_loads(payload.get("allowed_templates"), []),
            "status": str(payload.get("status") or "active"),
            "created_at": payload.get("created_at"),
            "expires_at": payload.get("expires_at"),
            "last_used_at": payload.get("last_used_at"),
        }

    def _run_view(self, row: Any) -> Dict[str, Any]:
        payload = _row_mapping(row)
        return {
            "strategy_run_id": str(payload["strategy_run_id"]),
            "token_id": str(payload["token_id"]),
            "template_id": str(payload["template_id"]),
            "account_scope": str(payload["account_scope"]),
            "execution_mode": str(payload["execution_mode"]),
            "status": str(payload.get("status") or "open"),
            "summary_fields": _json_loads(payload.get("summary_fields_json"), []),
            "risk_schema": _json_loads(payload.get("risk_schema_json"), []),
            "allowed_actions": _json_loads(payload.get("allowed_actions_json"), []),
            "runtime_state": _json_loads(payload.get("runtime_state_json"), {}),
            "metadata": _json_loads(payload.get("metadata_json"), {}),
            "worker_session_nonce": payload.get("worker_session_nonce"),
            "worker_session_claimed_at": payload.get("worker_session_claimed_at"),
            "last_heartbeat_at": payload.get("last_heartbeat_at"),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "closed_at": payload.get("closed_at"),
        }

    def _run_summary(self, row: Any) -> Dict[str, Any]:
        payload = _row_mapping(row)
        metadata = _json_loads(payload.get("metadata_json"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        strategy_run_id = str(payload.get("strategy_run_id") or "")
        template_id = str(payload.get("template_id") or "")
        name = str(
            metadata.get("strategy_name")
            or metadata.get("name")
            or template_id
            or strategy_run_id
        ).strip()
        return {
            "strategy_run_id": strategy_run_id,
            "name": name or strategy_run_id,
            "template_id": template_id,
            "account_scope": str(payload.get("account_scope") or ""),
            "execution_mode": str(payload.get("execution_mode") or ""),
            "status": str(payload.get("status") or "open"),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "closed_at": payload.get("closed_at"),
        }

    def _run_view_with_worker(self, row: Any) -> Dict[str, Any]:
        payload = self._run_view(row)
        raw = _row_mapping(row)
        payload["worker_name"] = raw.get("worker_name")
        payload["token_last_heartbeat_at"] = raw.get("token_last_heartbeat_at")
        if payload.get("last_heartbeat_at") is None:
            payload["last_heartbeat_at"] = raw.get("token_last_heartbeat_at")
        payload["heartbeat_json"] = _json_loads(raw.get("heartbeat_json"), {})
        return payload
