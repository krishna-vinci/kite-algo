"""Durable protection ownership for option runs (B2.4 S1).

Protection is currently enumerated per OPEN worker run, so a run leaves
protection by *status change alone* and nothing outlives the closure. This module
is the record that does outlive it: ONE owner row per option run (primary key =
``option_run_states.strategy_run_id``) plus an append-only event log.

The rules this module enforces, and why:

- **One row makes two owners unrepresentable.** A partial unique index on a
  shared row would still allow a stale second row; a primary key does not.
- **``owner_epoch`` is a compare-and-swap ticket, and the transfer is ONE
  statement.** A two-phase ``transferring`` state would have a committed instant
  with no owner, so there is deliberately no such state: the predecessor stays
  authoritative until the successor's transfer commits, and its authority then
  ends only because a newer epoch exists.
- **Every mutation appends an event in the SAME transaction.** The row is the
  current truth, the log is how it got there.
- **A missing row and an unreadable row are different things.** :meth:`read`
  returns ``None`` only when the row genuinely does not exist; a database error
  propagates, because "unknown owner" must never be silently reported as "no
  owner" by the reader that decides whether exposure is admissible.
- **Release belongs to the terminal state.** A non-terminal run refuses by name
  (:data:`RELEASE_NOT_TERMINAL`), and release is idempotent, because the terminal
  status write that releases may itself be retried.

The store follows ``DurableOptionRunStore``'s dialect portability: PostgreSQL is
production and keeps ``jsonb``/``NOW()``, while SQLite (the established test
fixture) has neither and does not take ``pg_advisory_xact_lock``. The CAS itself
is what makes the transfer safe on both.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from sqlalchemy import text
from backend.app.database import SessionLocal

if TYPE_CHECKING:  # pragma: no cover - typing only. Importing this at runtime
    # would cycle: ``backend.options.execution.__init__`` imports the durable
    # store, which imports this module.
    from ..execution.models import OptionRunState

#: A run in one of these states is FINISHED: it no longer holds, and can no
#: longer move, the structure. Only this releases the owner row.
TERMINAL_RUN_STATUSES = frozenset({"exited", "settled"})

#: The keys a policy digest is computed over (design section 4): everything the
#: protection loop needs to decide, not just the structure digest. A digest, not
#: an integer, is the identity - two runs can both be at "version 3" with
#: different content.
POLICY_VERSION_KEYS = (
    "structure_digest",
    "structure_id",
    "underlying",
    "expiry",
    "expiry_policy",
    "rules",
    "precedence",
    "stale_exit_policy",
    "operations",
)

#: The snapshot the entry hook freezes from the plan's resolved inputs.
POLICY_SNAPSHOT_KEYS = (
    "structure_digest",
    "structure_id",
    "underlying",
    "expiry",
    "expiry_policy",
    "protection_policy",
    "max_loss",
)

ACTION_STATES = ("none", "claimed", "staging", "unresolved")

CONFLICT = "OPTION_PROTECTION_OWNER_CONFLICT"
OWNER_UNKNOWN = "OPTION_PROTECTION_OWNER_UNKNOWN"
OWNER_REQUIRED = "OPTION_PROTECTION_OWNER_REQUIRED"
RELEASE_NOT_TERMINAL = "OPTION_PROTECTION_RELEASE_NOT_TERMINAL"
#: The action vocabulary is the CHECK on the row; asking for a state outside it
#: is a caller bug, reported by name rather than as a raw constraint error.
ACTION_STATE_INVALID = "OPTION_PROTECTION_ACTION_STATE_INVALID"


class OptionProtectionOwnerRefusal(Exception):
    """A named refusal carrying the reason code and its evidence."""

    def __init__(self, reason_code: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})

    def as_detail(self) -> dict[str, Any]:
        payload = {"reason_code": self.reason_code}
        payload.update(self.detail)
        return payload


def option_protection_policy_version(policy: Mapping[str, Any] | None) -> str:
    """The identity of a protection policy: sha256 over its canonical JSON.

    Only the decision inputs are hashed (design section 4), so a snapshot that
    carries extra bookkeeping does not mint a new version. The canonical form is
    key-sorted and separator-tight, so the digest is stable across processes and
    does not depend on dict insertion order.
    """

    source = policy if isinstance(policy, Mapping) else {}
    canonical = {key: source.get(key) for key in POLICY_VERSION_KEYS}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def option_protection_policy_snapshot(
    resolved_plan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The frozen policy snapshot an entry claim stores from the plan inputs.

    The digest is NOT embedded here: ``policy_version`` is a column, and the one
    helper (:func:`option_protection_policy_version`) is what derives it.
    """

    resolved = resolved_plan if isinstance(resolved_plan, Mapping) else {}
    snapshot: dict[str, Any] = {}
    for key in POLICY_SNAPSHOT_KEYS:
        value = resolved.get(key)
        snapshot[key] = dict(value) if isinstance(value, Mapping) else value
    return snapshot


class OptionProtectionOwnerStore:
    """DB-backed owner rows plus their append-only event log.

    ``db`` is the caller's session when the mutation must join the caller's
    transaction (the entry hook and the terminal status write). Without it the
    store owns the transaction, exactly like ``DurableOptionRunStore``.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] = SessionLocal,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    # -- dialect portability -------------------------------------------------

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
    def _uuid_value(cls, session: Any, name: str) -> str:
        if cls._dialect_name(session) == "sqlite":
            return f":{name}"
        return f"CAST(:{name} AS uuid)"

    @classmethod
    def _now_expression(cls, session: Any) -> str:
        return "CURRENT_TIMESTAMP" if cls._dialect_name(session) == "sqlite" else "NOW()"

    @classmethod
    def _lock_clause(cls, session: Any) -> str:
        return "" if cls._dialect_name(session) == "sqlite" else "FOR UPDATE"

    @classmethod
    def _advisory_lock(cls, session: Any, option_run_id: str) -> None:
        """Serialize transfers of ONE run on PostgreSQL (no-op on SQLite)."""

        if cls._dialect_name(session) == "sqlite":
            return
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"option-run:{option_run_id}"},
        )

    # -- reads ---------------------------------------------------------------

    def read(self, option_run_id: str, db: Any = None) -> Optional[dict[str, Any]]:
        """The owner row, or ``None`` when it genuinely does not exist.

        An unreadable row is NOT ``None``: a database error propagates, so the
        caller never mistakes "unknown" for "no owner".
        """

        option_run_id = self._require_id(option_run_id)
        owns_session = db is None
        session = db or self._session_factory()
        try:
            row = (
                session.execute(
                    text(
                        """
                        SELECT
                            option_run_id, strategy_id, account_id,
                            execution_environment, owner_run_id, owner_epoch,
                            policy_version, policy, action_state, stage_digest,
                            state, released_at
                        FROM public.option_protection_owners
                        WHERE option_run_id = :option_run_id
                        """
                    ),
                    {"option_run_id": option_run_id},
                )
                .mappings()
                .first()
            )
        finally:
            if owns_session:
                session.close()
        return None if row is None else self._view(dict(row))

    def list_protection_owners(
        self, db: Any = None, *, owner_run_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Every ACTIVE owner row, with its option run's live evidence.

        This is the protection loop's enumeration (design section 2 step 3): the
        row - not the owning worker run's status - is what keeps a structure
        protected, so the option run's own live evidence is joined in and NO
        predicate is placed on the worker run being ``open``. A structure whose
        worker run has closed stays protected until the option run itself reaches
        a terminal status and releases the row.

        ``owner_run_id`` narrows the read to the rows ONE worker run is
        authoritative for, which is how the safety gate resolves a hosted,
        plan-created run (``opt_run_<uuid>``) whose option-run id shares nothing
        with the worker run id.
        """

        conditions = ["o.state = 'active'"]
        params: dict[str, Any] = {}
        if owner_run_id is not None:
            conditions.append("o.owner_run_id = :owner_run_id")
            params["owner_run_id"] = str(owner_run_id)
        owns_session = db is None
        session = db or self._session_factory()
        try:
            rows = (
                session.execute(
                    text(
                        f"""
                        SELECT
                            o.option_run_id, o.strategy_id, o.account_id,
                            o.execution_environment, o.owner_run_id, o.owner_epoch,
                            o.policy_version, o.policy, o.action_state, o.stage_digest,
                            o.state, o.released_at,
                            s.status AS option_run_status,
                            s.orders AS option_run_orders
                        FROM public.option_protection_owners o
                        LEFT JOIN public.option_run_states s
                          ON s.strategy_run_id = o.option_run_id
                        WHERE {' AND '.join(conditions)}
                        ORDER BY o.option_run_id
                        """
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        finally:
            if owns_session:
                session.close()
        return [self._view(dict(row)) for row in rows]

    # -- mutations -----------------------------------------------------------

    def claim(
        self,
        run: OptionRunState,
        owner_run_id: Optional[str],
        policy: Mapping[str, Any] | None,
        policy_version: Optional[str] = None,
        db: Any = None,
    ) -> dict[str, Any]:
        """Claim ownership of a freshly created option run (epoch 1).

        The owner is REQUIRED: ``ck_opo_owner_present`` ties an ``active`` row to
        a named owner run, so a run with no worker run cannot be claimed at all
        and refuses by name rather than writing an ownerless "active" row.
        """

        option_run_id = self._require_id(run.strategy_run_id)
        owner = str(owner_run_id or "").strip()
        if not owner:
            raise OptionProtectionOwnerRefusal(
                OWNER_REQUIRED,
                {
                    "option_run_id": option_run_id,
                    "message": (
                        "an active protection owner must name the worker run that "
                        "is authoritative; an ownerless active row is not "
                        "representable"
                    ),
                },
            )
        scope = self._run_scope(run)
        snapshot = dict(policy or {})
        version = str(policy_version or "") or option_protection_policy_version(snapshot)
        owns_session = db is None
        session = db or self._session_factory()
        try:
            self._insert_owner(
                session,
                option_run_id=option_run_id,
                scope=scope,
                owner_run_id=owner,
                owner_epoch=1,
                policy=snapshot,
                policy_version=version,
            )
            self._append_event(
                session,
                option_run_id=option_run_id,
                owner_epoch=1,
                event="claimed",
                owner_run_id=owner,
                actor_id=owner,
                detail={"policy_version": version, "scope": scope},
            )
            row = self._select_for_update(session, option_run_id)
            if owns_session:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()
        if row is None:
            raise OptionProtectionOwnerRefusal(
                OWNER_UNKNOWN,
                {"option_run_id": option_run_id, "reason": "claim_not_persisted"},
            )
        return self._view(dict(row))

    def transfer(
        self,
        option_run_id: str,
        successor_run_id: str,
        observed_epoch: int,
        policy: Mapping[str, Any] | None,
        policy_version: Optional[str] = None,
        db: Any = None,
    ) -> int:
        """Move ownership to ``successor_run_id`` in ONE compare-and-swap.

        Zero rows updated means somebody else already moved the run: that is the
        named refusal :data:`CONFLICT`, never a blind retry. On PostgreSQL the
        whole read/CAS/event sequence runs under the run's own advisory lock, so
        two successors cannot interleave; on SQLite the CAS alone is atomic.
        """

        option_run_id = self._require_id(option_run_id)
        successor = str(successor_run_id or "").strip()
        if not successor:
            raise OptionProtectionOwnerRefusal(
                OWNER_REQUIRED,
                {
                    "option_run_id": option_run_id,
                    "message": "a transfer must name the successor worker run",
                },
            )
        observed = int(observed_epoch)
        snapshot = dict(policy or {})
        version = str(policy_version or "") or option_protection_policy_version(snapshot)
        owns_session = db is None
        session = db or self._session_factory()
        try:
            self._advisory_lock(session, option_run_id)
            current = self._select_for_update(session, option_run_id)
            if current is None:
                raise OptionProtectionOwnerRefusal(
                    OWNER_UNKNOWN,
                    {
                        "option_run_id": option_run_id,
                        "reason": "owner_row_absent",
                        "message": (
                            "there is no protection owner row for this run; a "
                            "transfer never invents one"
                        ),
                    },
                )
            json_args = self._json_value(session, "policy")
            result = session.execute(
                text(
                    f"""
                    UPDATE public.option_protection_owners
                    SET
                        owner_run_id = :successor_run_id,
                        owner_epoch = owner_epoch + 1,
                        policy = {json_args},
                        policy_version = :policy_version,
                        updated_at = {self._now_expression(session)}
                    WHERE option_run_id = :option_run_id
                      AND state = 'active'
                      AND owner_epoch = :observed_epoch
                    """
                ),
                {
                    "option_run_id": option_run_id,
                    "successor_run_id": successor,
                    "policy": json.dumps(snapshot),
                    "policy_version": version,
                    "observed_epoch": observed,
                },
            )
            if int(getattr(result, "rowcount", 0) or 0) == 0:
                raise OptionProtectionOwnerRefusal(
                    CONFLICT,
                    {
                        "option_run_id": option_run_id,
                        "successor_run_id": successor,
                        "observed_epoch": observed,
                        "current_epoch": int(current.get("owner_epoch") or 0),
                        "current_owner_run_id": current.get("owner_run_id"),
                        "current_state": str(current.get("state") or ""),
                        "message": (
                            "another owner moved this run first; the caller must "
                            "re-read, never retry blindly"
                        ),
                    },
                )
            new_epoch = observed + 1
            self._append_event(
                session,
                option_run_id=option_run_id,
                owner_epoch=new_epoch,
                event="transferred",
                owner_run_id=successor,
                actor_id=successor,
                detail={
                    "previous_owner_run_id": current.get("owner_run_id"),
                    "previous_policy_version": current.get("policy_version"),
                    "policy_version": version,
                    "observed_epoch": observed,
                },
            )
            if str(current.get("policy_version") or "") != version:
                # A transfer that carries a changed policy is still ONE CAS:
                # there is no window in which two policies are both active.
                self._append_event(
                    session,
                    option_run_id=option_run_id,
                    owner_epoch=new_epoch,
                    event="policy_changed",
                    owner_run_id=successor,
                    actor_id=successor,
                    detail={
                        "previous_policy_version": current.get("policy_version"),
                        "policy_version": version,
                    },
                )
            if owns_session:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()
        return new_epoch

    def record_action(
        self,
        option_run_id: str,
        action_state: str,
        stage_digest: Optional[str],
        observed_epoch: int,
        db: Any = None,
    ) -> dict[str, Any]:
        """Record the run's protection action state under the same CAS ticket.

        The owner row is the single place ``action_state`` lives; the run's own
        ``orders`` keep the stage claim that is the evidence for it.
        """

        option_run_id = self._require_id(option_run_id)
        state = str(action_state or "").strip()
        if state not in ACTION_STATES:
            raise OptionProtectionOwnerRefusal(
                ACTION_STATE_INVALID,
                {
                    "option_run_id": option_run_id,
                    "action_state": state,
                    "allowed": list(ACTION_STATES),
                    "message": "unknown protection action state",
                },
            )
        observed = int(observed_epoch)
        owns_session = db is None
        session = db or self._session_factory()
        try:
            self._advisory_lock(session, option_run_id)
            result = session.execute(
                text(
                    f"""
                    UPDATE public.option_protection_owners
                    SET
                        action_state = :action_state,
                        stage_digest = :stage_digest,
                        updated_at = {self._now_expression(session)}
                    WHERE option_run_id = :option_run_id
                      AND state = 'active'
                      AND owner_epoch = :observed_epoch
                    """
                ),
                {
                    "option_run_id": option_run_id,
                    "action_state": state,
                    "stage_digest": None if stage_digest is None else str(stage_digest),
                    "observed_epoch": observed,
                },
            )
            if int(getattr(result, "rowcount", 0) or 0) == 0:
                raise OptionProtectionOwnerRefusal(
                    CONFLICT,
                    {
                        "option_run_id": option_run_id,
                        "observed_epoch": observed,
                        "action_state": state,
                        "message": "the caller is not the owner at this epoch",
                    },
                )
            row = self._select_for_update(session, option_run_id)
            self._append_event(
                session,
                option_run_id=option_run_id,
                owner_epoch=observed,
                event="action_resolved" if state == "none" else "action_claimed",
                owner_run_id=(row or {}).get("owner_run_id"),
                actor_id=(row or {}).get("owner_run_id"),
                detail={
                    "action_state": state,
                    "stage_digest": None if stage_digest is None else str(stage_digest),
                },
            )
            if owns_session:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()
        return self._view(dict(row or {"option_run_id": option_run_id}))

    def release(self, option_run_id: str, db: Any = None) -> Optional[dict[str, Any]]:
        """Release an owner row for a TERMINAL run. Idempotent.

        A non-terminal run refuses by name: an operator "stop evaluator" leaves
        the row ``active``, because risk-reducing authority has to survive a
        stopped loop. A run with no owner row has nothing to release, so this is
        a no-op rather than an error - the terminal status write releases for
        every run, including the direct-API runs that never had an owner.
        """

        option_run_id = self._require_id(option_run_id)
        owns_session = db is None
        session = db or self._session_factory()
        try:
            current = self._select_for_update(session, option_run_id)
            if current is None:
                if owns_session:
                    session.rollback()
                return None
            if str(current.get("state")) == "released":
                if owns_session:
                    session.rollback()
                return self._view(dict(current))
            run_status_row = (
                session.execute(
                    text(
                        """
                        SELECT status
                        FROM public.option_run_states
                        WHERE strategy_run_id = :option_run_id
                        """
                    ),
                    {"option_run_id": option_run_id},
                )
                .mappings()
                .first()
            )
            run_status = "" if run_status_row is None else str(run_status_row["status"] or "")
            if run_status not in TERMINAL_RUN_STATUSES:
                raise OptionProtectionOwnerRefusal(
                    RELEASE_NOT_TERMINAL,
                    {
                        "option_run_id": option_run_id,
                        "run_status": run_status or None,
                        "terminal": sorted(TERMINAL_RUN_STATUSES),
                        "message": (
                            "an owner row is released only by a terminal run "
                            "status; a stopped evaluator keeps it active"
                        ),
                    },
                )
            result = session.execute(
                text(
                    f"""
                    UPDATE public.option_protection_owners
                    SET
                        state = 'released',
                        owner_run_id = NULL,
                        owner_epoch = owner_epoch + 1,
                        released_at = {self._now_expression(session)},
                        updated_at = {self._now_expression(session)}
                    WHERE option_run_id = :option_run_id
                      AND state = 'active'
                    """
                ),
                {"option_run_id": option_run_id},
            )
            if int(getattr(result, "rowcount", 0) or 0) == 0:
                # Another writer released it between the read and the CAS. The
                # outcome the caller asked for is already true.
                released = self._select_for_update(session, option_run_id)
                if owns_session:
                    session.rollback()
                return None if released is None else self._view(dict(released))
            row = self._select_for_update(session, option_run_id)
            self._append_event(
                session,
                option_run_id=option_run_id,
                owner_epoch=int((row or {}).get("owner_epoch") or 0),
                event="released",
                owner_run_id=None,
                actor_id=None,
                detail={
                    "previous_owner_run_id": current.get("owner_run_id"),
                    "run_status": run_status,
                },
            )
            if owns_session:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()
        return self._view(dict(row or {"option_run_id": option_run_id}))

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _require_id(option_run_id: str) -> str:
        value = str(option_run_id or "").strip()
        if not value:
            raise ValueError("option_run_id is required")
        return value

    @staticmethod
    def _run_scope(run: OptionRunState) -> dict[str, str]:
        metadata = getattr(run, "metadata", None) or {}
        scope = {
            "strategy_id": str(metadata.get("strategy_id") or ""),
            "account_id": str(metadata.get("account_id") or ""),
            "execution_environment": str(metadata.get("execution_environment") or ""),
        }
        missing = [key for key, value in scope.items() if not value]
        if missing:
            raise OptionProtectionOwnerRefusal(
                OWNER_UNKNOWN,
                {
                    "option_run_id": str(getattr(run, "strategy_run_id", "") or ""),
                    "missing_scope": sorted(missing),
                    "message": (
                        "the run does not carry the scope the owner row is "
                        "required to agree with"
                    ),
                },
            )
        return scope

    @classmethod
    def _view(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        view = dict(row)
        policy = view.get("policy")
        if isinstance(policy, str):
            try:
                policy = json.loads(policy or "{}")
            except json.JSONDecodeError:
                policy = {}
        view["policy"] = policy if isinstance(policy, dict) else {}
        # The option run's own stage records ride along on
        # :meth:`list_protection_owners`; SQLite hands JSON back as text.
        if isinstance(view.get("option_run_orders"), str):
            try:
                view["option_run_orders"] = json.loads(view["option_run_orders"] or "[]")
            except json.JSONDecodeError:
                view["option_run_orders"] = []
        if view.get("owner_epoch") is not None:
            view["owner_epoch"] = int(view["owner_epoch"])
        return view

    def _select_for_update(self, session: Any, option_run_id: str) -> Optional[dict[str, Any]]:
        row = (
            session.execute(
                text(
                    f"""
                    SELECT
                        option_run_id, strategy_id, account_id,
                        execution_environment, owner_run_id, owner_epoch,
                        policy_version, policy, action_state, stage_digest,
                        state, released_at
                    FROM public.option_protection_owners
                    WHERE option_run_id = :option_run_id
                    {self._lock_clause(session)}
                    """
                ),
                {"option_run_id": option_run_id},
            )
            .mappings()
            .first()
        )
        return None if row is None else dict(row)

    def _insert_owner(
        self,
        session: Any,
        *,
        option_run_id: str,
        scope: Mapping[str, str],
        owner_run_id: str,
        owner_epoch: int,
        policy: Mapping[str, Any],
        policy_version: str,
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        json_args = self._json_value(session, "policy")
        try:
            session.execute(
                text(
                    f"""
                    INSERT INTO public.option_protection_owners (
                        option_run_id, strategy_id, account_id,
                        execution_environment, owner_run_id, owner_epoch,
                        policy_version, policy, action_state, state,
                        created_at, updated_at
                    ) VALUES (
                        :option_run_id, :strategy_id, :account_id,
                        :execution_environment, :owner_run_id, :owner_epoch,
                        :policy_version, {json_args}, 'none', 'active',
                        {self._now_expression(session)}, {self._now_expression(session)}
                    )
                    """
                ),
                {
                    "option_run_id": option_run_id,
                    "strategy_id": scope["strategy_id"],
                    "account_id": scope["account_id"],
                    "execution_environment": scope["execution_environment"],
                    "owner_run_id": owner_run_id,
                    "owner_epoch": int(owner_epoch),
                    "policy_version": policy_version,
                    "policy": json.dumps(dict(policy)),
                },
            )
        except IntegrityError as exc:
            # The primary key IS the rule: this run already has an owner row and
            # a second one is impossible. Reported by name, not as a raw error.
            raise OptionProtectionOwnerRefusal(
                CONFLICT,
                {
                    "option_run_id": option_run_id,
                    "owner_run_id": owner_run_id,
                    "message": "this option run already has a protection owner row",
                },
            ) from exc

    def _append_event(
        self,
        session: Any,
        *,
        option_run_id: str,
        owner_epoch: int,
        event: str,
        owner_run_id: Optional[str],
        actor_id: Optional[str],
        detail: Mapping[str, Any],
    ) -> None:
        id_arg = self._uuid_value(session, "id")
        detail_arg = self._json_value(session, "detail")
        session.execute(
            text(
                f"""
                INSERT INTO public.option_protection_owner_events (
                    id, option_run_id, owner_epoch, event, owner_run_id,
                    actor_id, detail, created_at
                ) VALUES (
                    {id_arg}, :option_run_id, :owner_epoch, :event, :owner_run_id,
                    :actor_id, {detail_arg}, {self._now_expression(session)}
                )
                """
            ),
            {
                "id": self._id_factory(),
                "option_run_id": option_run_id,
                "owner_epoch": int(owner_epoch),
                "event": str(event),
                "owner_run_id": owner_run_id,
                "actor_id": actor_id,
                "detail": json.dumps(dict(detail or {})),
            },
        )


#: The production default, mirroring ``get_option_run_store``.
_DEFAULT_OWNER_STORE: Any | None = None


def get_option_protection_owner_store() -> OptionProtectionOwnerStore:
    global _DEFAULT_OWNER_STORE
    if _DEFAULT_OWNER_STORE is None:
        _DEFAULT_OWNER_STORE = OptionProtectionOwnerStore()
    return _DEFAULT_OWNER_STORE


def reset_option_protection_owner_store(store: Any = None) -> Any:
    global _DEFAULT_OWNER_STORE
    _DEFAULT_OWNER_STORE = store
    return _DEFAULT_OWNER_STORE
