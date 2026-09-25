from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import text
from backend.app.database import SessionLocal
from backend.options.protection.ownership import (
    TERMINAL_RUN_STATUSES,
    OptionProtectionOwnerStore,
)

from .models import OptionRunCreateRequest, OptionRunState


class DurableOptionRunStore:
    """DB-backed canonical option run store for durable execution/protection state."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] = SessionLocal,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._id_factory = id_factory or self._next_run_id

    def _next_run_id(self) -> str:
        return f"opt_run_{uuid.uuid4().hex}"

    # -- dialect portability -------------------------------------------------
    #
    # PostgreSQL is the production database and keeps its native ``jsonb`` cast
    # and ``NOW()``. SQLite (the established test fixture) has neither: ``CAST(x
    # AS jsonb)`` would apply NUMERIC affinity and silently store ``0`` for
    # ``'[]'``, and ``FOR UPDATE`` is not valid syntax at all. The fake sessions
    # used by the unit tests expose no dialect and keep the PostgreSQL text, so
    # this stays a no-op for them.

    @staticmethod
    def _dialect_name(session: Any) -> str:
        bind = None
        getter = getattr(session, "get_bind", None)
        if callable(getter):
            try:
                bind = getter()
            except Exception:  # noqa: BLE001 - unknown session shape
                bind = None
        dialect = getattr(getattr(bind, "dialect", None), "name", None)
        return str(dialect or "postgresql")

    @classmethod
    def _json_value(cls, session: Any, name: str) -> str:
        if cls._dialect_name(session) == "sqlite":
            return f":{name}"
        return f"CAST(:{name} AS jsonb)"

    @classmethod
    def _now_expression(cls, session: Any) -> str:
        return "CURRENT_TIMESTAMP" if cls._dialect_name(session) == "sqlite" else "NOW()"

    @classmethod
    def _lock_clause(cls, session: Any, *, for_update: bool) -> str:
        if not for_update or cls._dialect_name(session) == "sqlite":
            return ""
        return "FOR UPDATE"

    @staticmethod
    def _to_json(value: Any) -> str:
        return json.dumps(value)

    @staticmethod
    def _normalize_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return []
            return parsed if isinstance(parsed, list) else []
        return []

    @staticmethod
    def _normalize_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _normalize_optional_dict(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None
            if parsed is None:
                return None
            return parsed if isinstance(parsed, dict) else None
        if isinstance(value, dict):
            return value
        return None

    @classmethod
    def _row_to_state(cls, row: dict[str, Any]) -> OptionRunState:
        return OptionRunState(
            strategy_run_id=str(row.get("strategy_run_id") or ""),
            strategy_name=str(row.get("strategy_name") or ""),
            product=row.get("product"),
            status=str(row.get("status") or "created"),
            legs=cls._normalize_list(row.get("legs")),
            protection=cls._normalize_optional_dict(row.get("protection")),
            metadata=cls._normalize_dict(row.get("metadata")),
            orders=cls._normalize_list(row.get("orders")),
            trades=cls._normalize_list(row.get("trades")),
            completed_legs=cls._normalize_list(row.get("completed_legs")),
            failed_legs=cls._normalize_list(row.get("failed_legs")),
            pending_legs=cls._normalize_list(row.get("pending_legs")),
        )

    @staticmethod
    def _require_id(strategy_run_id: str) -> None:
        if not strategy_run_id:
            raise ValueError("strategy_run_id is required")

    def create_run(
        self, request: OptionRunCreateRequest, *, db: Any = None
    ) -> OptionRunState:
        """Insert one run. ``db`` joins the CALLER's transaction (no commit).

        The plan-binding edge needs the run row and the binding row to be one
        atomic unit: created together and rolled back together, so a losing
        concurrent resolution can never leave an orphan run behind.
        """
        strategy_run_id = str(request.strategy_run_id or self._id_factory())
        if not strategy_run_id.startswith("opt_run_") and not request.strategy_run_id:
            strategy_run_id = f"opt_run_{strategy_run_id}"

        run = OptionRunState.from_create_request(request, strategy_run_id=strategy_run_id)
        owns_session = db is None
        session = db or self._session_factory()
        try:
            json_args = {
                name: self._json_value(session, name)
                for name in (
                    "legs",
                    "protection",
                    "metadata",
                    "orders",
                    "trades",
                    "completed_legs",
                    "failed_legs",
                    "pending_legs",
                )
            }
            session.execute(
                text(
                    f"""
                    INSERT INTO public.option_run_states (
                        strategy_run_id,
                        strategy_name,
                        product,
                        status,
                        legs,
                        protection,
                        metadata,
                        orders,
                        trades,
                        completed_legs,
                        failed_legs,
                        pending_legs
                    ) VALUES (
                        :strategy_run_id,
                        :strategy_name,
                        :product,
                        :status,
                        {json_args["legs"]},
                        {json_args["protection"]},
                        {json_args["metadata"]},
                        {json_args["orders"]},
                        {json_args["trades"]},
                        {json_args["completed_legs"]},
                        {json_args["failed_legs"]},
                        {json_args["pending_legs"]}
                    )
                    """
                ),
                {
                    "strategy_run_id": run.strategy_run_id,
                    "strategy_name": run.strategy_name,
                    "product": run.product,
                    "status": run.status,
                    "legs": self._to_json(run.legs),
                    "protection": self._to_json(run.protection),
                    "metadata": self._to_json(run.metadata),
                    "orders": self._to_json(run.orders),
                    "trades": self._to_json(run.trades),
                    "completed_legs": self._to_json(run.completed_legs),
                    "failed_legs": self._to_json(run.failed_legs),
                    "pending_legs": self._to_json(run.pending_legs),
                },
            )
            if owns_session:
                session.commit()
            return run
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()

    def list_runs(self) -> list[OptionRunState]:
        session = self._session_factory()
        try:
            rows = (
                session.execute(
                    text(
                        """
                        SELECT
                            strategy_run_id,
                            strategy_name,
                            product,
                            status,
                            legs,
                            protection,
                            metadata,
                            orders,
                            trades,
                            completed_legs,
                            failed_legs,
                            pending_legs
                        FROM public.option_run_states
                        ORDER BY updated_at DESC, strategy_run_id
                        """
                    )
                )
                .mappings()
                .all()
            )
            return [self._row_to_state(dict(row)) for row in rows]
        finally:
            session.close()

    def get_run(self, strategy_run_id: str) -> OptionRunState:
        self._require_id(strategy_run_id)
        session = self._session_factory()
        try:
            return self._get_run_in_session(session, strategy_run_id)
        finally:
            session.close()

    def get_run_in_session(self, session: Any, strategy_run_id: str) -> OptionRunState:
        self._require_id(strategy_run_id)
        return self._get_run_in_session(session, strategy_run_id)

    def _get_run_in_session(self, session: Any, strategy_run_id: str, *, for_update: bool = False) -> OptionRunState:
        lock_clause = self._lock_clause(session, for_update=for_update)
        row = (
            session.execute(
                text(
                    f"""
                    SELECT
                        strategy_run_id,
                        strategy_name,
                        product,
                        status,
                        legs,
                        protection,
                        metadata,
                        orders,
                        trades,
                        completed_legs,
                        failed_legs,
                        pending_legs
                    FROM public.option_run_states
                    WHERE strategy_run_id = :strategy_run_id
                    {lock_clause}
                    """
                ),
                {"strategy_run_id": strategy_run_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise KeyError(f"Option run not found: {strategy_run_id}")
        return self._row_to_state(dict(row))

    def save_run(
        self,
        run: OptionRunState,
        *,
        owner_policy: Any = None,
        owner_run_id: Optional[str] = None,
        owner_observed_epoch: Optional[int] = None,
    ) -> OptionRunState:
        """Write the durable run, and - when asked - its owner row's new policy.

        B2.4 S4: an adjust completion freezes a NEW protection policy on the
        owner row in the SAME transaction as the run's completion write. Passing
        ``owner_policy`` therefore hands the CAS its observed epoch, and the two
        writes commit together or not at all: a run that reads as ``adjusted``
        under a policy the owner row never saw (or the reverse) is not a state
        this platform can be left in.
        """
        self._require_id(run.strategy_run_id)
        session = self._session_factory()
        try:
            self._update_run_in_session(session, run)
            self._release_protection_owner_if_terminal(session, run)
            if owner_policy is not None:
                OptionProtectionOwnerStore(
                    session_factory=self._session_factory
                ).update_policy(
                    run.strategy_run_id,
                    owner_policy,
                    int(owner_observed_epoch or 0),
                    owner_run_id=owner_run_id,
                    db=session,
                )
            session.commit()
            return run
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _release_protection_owner_if_terminal(self, session: Any, run: OptionRunState) -> None:
        """Release the protection owner in the SAME transaction as the status write.

        The owner row outlives the worker run, so it ends with the OPTION run's
        terminal state and not with anything else. Doing it inside this
        transaction - rather than after it - is what keeps "terminal but still
        owned" and "released but still running" from being observable.
        """
        if str(getattr(run, "status", "") or "") not in TERMINAL_RUN_STATUSES:
            return
        OptionProtectionOwnerStore(session_factory=self._session_factory).release(
            run.strategy_run_id, db=session
        )

    def _update_run_in_session(self, session: Any, run: OptionRunState) -> None:
        json_args = {
            name: self._json_value(session, name)
            for name in (
                "legs",
                "protection",
                "metadata",
                "orders",
                "trades",
                "completed_legs",
                "failed_legs",
                "pending_legs",
            )
        }
        result = session.execute(
            text(
                f"""
                UPDATE public.option_run_states
                SET
                    strategy_name = :strategy_name,
                    product = :product,
                    status = :status,
                    legs = {json_args["legs"]},
                    protection = {json_args["protection"]},
                    metadata = {json_args["metadata"]},
                    orders = {json_args["orders"]},
                    trades = {json_args["trades"]},
                    completed_legs = {json_args["completed_legs"]},
                    failed_legs = {json_args["failed_legs"]},
                    pending_legs = {json_args["pending_legs"]},
                    updated_at = {self._now_expression(session)}
                WHERE strategy_run_id = :strategy_run_id
                """
            ),
            {
                "strategy_run_id": run.strategy_run_id,
                "strategy_name": run.strategy_name,
                "product": run.product,
                "status": run.status,
                "legs": self._to_json(run.legs),
                "protection": self._to_json(run.protection),
                "metadata": self._to_json(run.metadata),
                "orders": self._to_json(run.orders),
                "trades": self._to_json(run.trades),
                "completed_legs": self._to_json(run.completed_legs),
                "failed_legs": self._to_json(run.failed_legs),
                "pending_legs": self._to_json(run.pending_legs),
            },
        )
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            raise KeyError(f"Option run not found: {run.strategy_run_id}")

    def save_run_if_status(
        self,
        run: OptionRunState,
        *,
        allowed_from: "list[str] | tuple[str, ...]",
        expected_generation: int | None = None,
        db: Any = None,
    ) -> bool:
        """Compare-and-set the run's status: the per-RUN execution ownership.

        Two plans may target the same option run (an entry and any number of
        exits), so a blind status write would let both exits win the same
        transition and overclose the structure. This CAS makes the transition
        itself the ownership token: exactly one caller moves the run out of the
        status it observed, and the loser refuses instead of trading. An adjust
        also pins the frozen leg generation, so a plan cannot win a status race
        and then trade against a newer structure.
        """
        self._require_id(run.strategy_run_id)
        allowed = [str(status) for status in allowed_from]
        if not allowed:
            return False
        owns_session = db is None
        session = db or self._session_factory()
        json_args = {
            name: self._json_value(session, name)
            for name in (
                "legs",
                "protection",
                "metadata",
                "orders",
                "trades",
                "completed_legs",
                "failed_legs",
                "pending_legs",
            )
        }
        try:
            placeholders = ", ".join(f":status_{index}" for index in range(len(allowed)))
            params: dict[str, Any] = {
                "strategy_run_id": run.strategy_run_id,
                "strategy_name": run.strategy_name,
                "product": run.product,
                "status": run.status,
                "legs": self._to_json(run.legs),
                "protection": self._to_json(run.protection),
                "metadata": self._to_json(run.metadata),
                "orders": self._to_json(run.orders),
                "trades": self._to_json(run.trades),
                "completed_legs": self._to_json(run.completed_legs),
                "failed_legs": self._to_json(run.failed_legs),
                "pending_legs": self._to_json(run.pending_legs),
            }
            for index, status in enumerate(allowed):
                params[f"status_{index}"] = status
            params["expected_generation"] = (
                None if expected_generation is None else str(int(expected_generation))
            )
            if expected_generation is None:
                generation_predicate = "1 = 1"
            elif self._dialect_name(session) == "sqlite":
                generation_predicate = (
                    "COALESCE(json_extract(metadata, '$.structure_generation'), '1') "
                    "= :expected_generation"
                )
            else:
                generation_predicate = (
                    "COALESCE(metadata->>'structure_generation', '1') "
                    "= :expected_generation"
                )
            result = session.execute(
                text(
                    f"""
                    UPDATE public.option_run_states
                    SET
                        strategy_name = :strategy_name,
                        product = :product,
                        status = :status,
                        legs = {json_args["legs"]},
                        protection = {json_args["protection"]},
                        metadata = {json_args["metadata"]},
                        orders = {json_args["orders"]},
                        trades = {json_args["trades"]},
                        completed_legs = {json_args["completed_legs"]},
                        failed_legs = {json_args["failed_legs"]},
                        pending_legs = {json_args["pending_legs"]},
                        updated_at = {self._now_expression(session)}
                    WHERE strategy_run_id = :strategy_run_id
                      AND status IN ({placeholders})
                      AND {generation_predicate}
                    """
                ),
                params,
            )
            won = int(getattr(result, "rowcount", 0) or 0) > 0
            if won:
                self._release_protection_owner_if_terminal(session, run)
            if owns_session:
                session.commit()
            return won
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()

    def record_orders(self, strategy_run_id: str, orders: list[dict]) -> OptionRunState:
        self._require_id(strategy_run_id)
        session = self._session_factory()
        try:
            run = self._get_run_in_session(session, strategy_run_id, for_update=True)
            run.orders.extend(list(orders))
            self._update_run_in_session(session, run)
            session.commit()
            return run
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_trades(self, strategy_run_id: str, trades: list[dict]) -> OptionRunState:
        self._require_id(strategy_run_id)
        session = self._session_factory()
        try:
            run = self._get_run_in_session(session, strategy_run_id, for_update=True)
            run.trades.extend(list(trades))
            self._update_run_in_session(session, run)
            session.commit()
            return run
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_trades_once(
        self, strategy_run_id: str, trades: list[dict], *, dedupe_key: str
    ) -> tuple[list[dict], list[dict]]:
        """Append only the trades whose ``dedupe_key`` is not already recorded.

        The read of the existing keys and the append happen inside ONE row-locked
        transaction on the run, so two consumers that both observed the key as
        absent cannot both write it: the second one re-reads under the lock and
        appends nothing. ``extend``-under-lock is not enough on its own, which is
        exactly the double-booked fill this exists to prevent.

        Returns ``(appended, skipped)``.
        """
        self._require_id(strategy_run_id)
        self._require_id(dedupe_key)
        session = self._session_factory()
        try:
            run = self._get_run_in_session(session, strategy_run_id, for_update=True)
            seen = {
                str((row or {}).get(dedupe_key) or "")
                for row in (run.trades or [])
                if isinstance(row, dict)
            }
            appended: list[dict] = []
            skipped: list[dict] = []
            for trade in list(trades or []):
                key = str((trade or {}).get(dedupe_key) or "")
                if not key or key in seen:
                    skipped.append(dict(trade or {}))
                    continue
                seen.add(key)
                appended.append(dict(trade or {}))
            if appended:
                run.trades.extend(appended)
                self._update_run_in_session(session, run)
            session.commit()
            return appended, skipped
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_leg_evidence_once(
        self,
        strategy_run_id: str,
        *,
        orders: list[dict] | None = None,
        trades: list[dict] | None = None,
    ) -> OptionRunState:
        """Append order/trade evidence once, keyed by its paper order id.

        The read and append happen under the run row lock. This is the one
        ledger path used both immediately after a leg outcome and again during
        settlement, so a crash between those points cannot double-book a fill.
        """
        self._require_id(strategy_run_id)
        session = self._session_factory()
        try:
            run = self._get_run_in_session(session, strategy_run_id, for_update=True)
            order_keys = {
                str((row or {}).get("order_id") or "")
                for row in (run.orders or [])
                if isinstance(row, dict)
            }
            trade_keys = {
                str((row or {}).get("order_id") or "")
                for row in (run.trades or [])
                if isinstance(row, dict)
            }
            appended_orders = [
                dict(order or {})
                for order in (orders or [])
                if str((order or {}).get("order_id") or "") not in order_keys
            ]
            appended_trades = []
            for trade in (trades or []):
                key = str((trade or {}).get("order_id") or "")
                if key and key not in trade_keys:
                    trade_keys.add(key)
                    appended_trades.append(dict(trade or {}))
            if appended_orders:
                run.orders.extend(appended_orders)
            if appended_trades:
                run.trades.extend(appended_trades)
            if appended_orders or appended_trades:
                self._update_run_in_session(session, run)
            session.commit()
            return run
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def claim_stage(
        self,
        strategy_run_id: str,
        claim: dict,
        *,
        now: Any = None,
    ) -> tuple[bool, Optional[dict]]:
        """Atomically claim the run's single in-flight exit stage.

        Under the run's row lock, this checks whether a PREVIOUS claim is still
        unresolved (``state`` is ``sending`` OR ``unknown``) and, if so, refuses:
        exactly one sender owns the stage. The loser gets the holder's claim back
        so it can report who is in flight instead of sending a second order.

        The lease is NOT a way out. A sender paused past its lease can still resume
        and call the broker (there is no fence at the broker write itself), so an
        expired lease is an OBSERVATION only; the stage is released solely by
        durable evidence that resolves EVERY leg (the broker's own references, or
        an acknowledged zero-send/rejection recorded by the sender).

        Returns ``(won, existing_claim)``.
        """
        self._require_id(strategy_run_id)
        session = self._session_factory()
        try:
            run = self._get_run_in_session(session, strategy_run_id, for_update=True)
            holder = self._live_stage_claim(run, now=now)
            if holder is not None:
                session.rollback()
                return False, holder
            run.orders.extend([dict(claim)])
            self._update_run_in_session(session, run)
            session.commit()
            return True, dict(claim)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _live_stage_claim(run: OptionRunState, *, now: Any = None) -> Optional[dict]:
        """The claim that currently owns the run's stage sending, if any.

        A stage's LATEST record decides its state: a ``sending`` claim followed by
        its outcome is resolved, and must not keep blocking later stages.

        ``sending`` and ``unknown`` BOTH own the send, and the lease does NOT
        release that ownership: a sender paused past its lease can still resume and
        call the broker (there is no fence at the broker write itself), so elapsed
        time is an observation, never proof that an old process is gone. Only
        durable evidence that resolves EVERY leg - the broker's own references, or
        an acknowledged zero-send/rejection recorded by the sender - settles the
        stage and frees the claim. ``now`` is therefore only reported in the
        holder's view, not used to release anything.
        """
        from datetime import datetime, timezone

        moment = now or datetime.now(timezone.utc)
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        if getattr(moment, "tzinfo", None) is None:
            moment = moment.replace(tzinfo=timezone.utc)
        latest: Dict[tuple, dict] = {}
        order: list = []
        for row in list(run.orders or []):
            if not isinstance(row, dict) or not row.get("stage_digest"):
                continue
            key = (str(row.get("stage_digest")), int(row.get("attempt") or 1))
            if key not in latest:
                order.append(key)
            latest[key] = row
        for key in reversed(order):
            row = latest[key]
            if str(row.get("state")) not in ("sending", "unknown"):
                continue
            lease_until = row.get("lease_until")
            view = dict(row)
            if lease_until:
                try:
                    parsed = datetime.fromisoformat(str(lease_until).replace("Z", "+00:00"))
                except ValueError:
                    parsed = None
                if parsed is not None:
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    # Reported for observability ONLY. An expired lease is not a
                    # reason to hand the stage to anyone else.
                    view["lease_expired"] = bool(parsed <= moment)
            return view
        return None
