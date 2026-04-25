from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import SessionLocal


def _json_loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return value


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    text_value = str(value).strip()
    return text_value or None


def _none_state(summary: str = "No protection runtime attached") -> Dict[str, Any]:
    return {
        "source": "none",
        "status": "unknown",
        "summary": summary,
        "last_checked_at": None,
        "details": {},
    }


class InvestingProtectionRepository:
    def __init__(self, session_factory=SessionLocal) -> None:
        self.session_factory = session_factory

    async def summarize_strategy(self, strategy_name: str) -> Optional[Dict[str, Any]]:
        import asyncio

        return await asyncio.to_thread(self._summarize_strategy_sync, strategy_name)

    def _summarize_strategy_sync(self, strategy_name: str) -> Optional[Dict[str, Any]]:
        db: Session = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT
                        strategy_name,
                        COUNT(*) FILTER (WHERE status = 'ACTIVE') AS active_holding_count,
                        COUNT(*) FILTER (WHERE status = 'PENDING_EXIT') AS pending_exit_count,
                        COALESCE(SUM(pnl), 0) AS total_pnl,
                        MIN(pnl_percent) AS worst_pnl_percent,
                        MAX(updated_at) AS last_checked_at
                    FROM public.investing_strategies
                    WHERE strategy_name = :strategy_name
                      AND status IN ('ACTIVE', 'PENDING_EXIT')
                    GROUP BY strategy_name
                    """
                ),
                {"strategy_name": strategy_name},
            ).mappings().first()
            return dict(row) if row else None
        finally:
            db.close()


@dataclass
class ControlPlaneProtectionService:
    option_strategy_store: Any = None
    algo_runtime_service: Any = None
    investing_repository: Any = None

    async def for_strategy(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        metadata = dict(strategy.get("metadata") or {})
        family = str(metadata.get("strategy_family") or strategy.get("strategy_family") or "").strip().lower()
        if family == "options_strategy" or self._looks_like_option_run(strategy):
            option_state = await self._option_protection(strategy)
            if option_state["source"] != "none":
                return option_state
        if family == "investment_strategy":
            investing_state = await self._investing_protection(strategy)
            if investing_state["source"] != "none":
                return investing_state
        return self._metadata_protection(strategy)

    def _looks_like_option_run(self, strategy: Dict[str, Any]) -> bool:
        strategy_id = str(strategy.get("strategy_run_id") or "")
        metadata = dict(strategy.get("metadata") or {})
        return bool(
            metadata.get("option_strategy_id")
            or metadata.get("underlying")
            or strategy_id.startswith("option-")
        )

    async def _option_protection(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        if self.option_strategy_store is None:
            return _none_state("Option strategy store is not available")
        strategy_run_id = str(strategy.get("strategy_run_id") or "")
        if not strategy_run_id:
            return _none_state("Option strategy run id is unavailable")
        run = self.option_strategy_store.get_strategy_run(strategy_run_id)
        if not run:
            return _none_state("No option strategy runtime record found")
        canonical = _json_loads(run.get("canonical_strategy"), {})
        rules = list(canonical.get("rules") or [])
        prefs = dict(canonical.get("protection_preferences") or {})
        algo_instance_id = run.get("algo_instance_id") or strategy.get("algo_instance_id")
        runtime = await self._algo_runtime_instance(algo_instance_id)
        lifecycle = str(runtime.get("lifecycle_state") or "unknown") if runtime else "unknown"
        status = "active" if lifecycle in {"enabled", "running"} else lifecycle
        if runtime and runtime.get("last_error"):
            status = "error"
        summary = f"Option protection {status}; {len(rules)} rule(s) configured"
        if prefs:
            summary += f"; preferences: {', '.join(sorted(prefs.keys()))}"
        return {
            "source": "option_runtime",
            "status": status,
            "summary": summary,
            "last_checked_at": runtime.get("last_evaluated_at") if runtime else _iso(run.get("updated_at")),
            "details": {
                "strategy_run_id": strategy_run_id,
                "algo_instance_id": algo_instance_id,
                "lifecycle_state": lifecycle,
                "rule_count": len(rules),
                "protection_preferences": prefs,
                "last_action": runtime.get("last_action") if runtime else None,
                "last_error": runtime.get("last_error") if runtime else None,
            },
        }

    async def _algo_runtime_instance(self, algo_instance_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not algo_instance_id or self.algo_runtime_service is None:
            return None
        status = await self.algo_runtime_service.status()
        for instance in list(status.get("instances") or []):
            if str(instance.get("instance_id")) == str(algo_instance_id):
                return dict(instance)
        return None

    async def _investing_protection(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        repository = self.investing_repository or InvestingProtectionRepository()
        metadata = dict(strategy.get("metadata") or {})
        strategy_name = str(metadata.get("strategy_name") or strategy.get("display_name") or "").strip()
        if not strategy_name:
            return _none_state("Investment strategy name is unavailable")
        summary = await repository.summarize_strategy(strategy_name)
        if not summary:
            return _none_state("No active investing holdings found")
        active_count = int(summary.get("active_holding_count") or 0)
        pending_exit_count = int(summary.get("pending_exit_count") or 0)
        status = "pending_exit" if pending_exit_count > 0 else "active"
        return {
            "source": "investing_runtime",
            "status": status,
            "summary": f"{active_count} active holdings; {pending_exit_count} pending exits; P&L {float(summary.get('total_pnl') or 0):.2f}",
            "last_checked_at": _iso(summary.get("last_checked_at")),
            "details": {
                "strategy_name": strategy_name,
                "active_holding_count": active_count,
                "pending_exit_count": pending_exit_count,
                "total_pnl": float(summary.get("total_pnl") or 0),
                "worst_pnl_percent": float(summary.get("worst_pnl_percent") or 0),
            },
        }

    def _metadata_protection(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        protection = strategy.get("protection") if isinstance(strategy.get("protection"), dict) else None
        if protection is None:
            metadata = dict(strategy.get("metadata") or {})
            protection = metadata.get("protection") if isinstance(metadata.get("protection"), dict) else None
        if not protection:
            return _none_state()
        return {
            "source": str(protection.get("source") or "metadata"),
            "status": str(protection.get("status") or "unknown"),
            "summary": str(protection.get("summary") or "Protection state supplied by metadata"),
            "last_checked_at": protection.get("last_checked_at"),
            "details": dict(protection.get("details") or {}),
        }
