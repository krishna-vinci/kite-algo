"""The platform's OWN staged structure exit, derived server-side (D-6/D-8).

The worker-protection runtime has to be able to protect a hedged option structure
whose child is dead. The generic exit is a whole-book liquidation: it is the right
answer for a MIS position, and the WRONG answer for a structure, because a
structure's short carries unlimited liability and its long is what bounds it. A
whole-book basket that sells the long with (or before) the short opens exactly the
naked window the structure was built to avoid.

This module is the missing engine adapter. It derives the exit actions from
PERSISTED, SERVER-SIDE evidence only:

* the durable option run bound to the worker run (its own legs and its own
  confirmed trades - never a caller's position list and never the strategy's
  aggregate projection, which mixes structures that share a contract);
* the existing ``build_structure_exit_orders`` rule (shorts close first; a hedge
  is released only once EVERY short the run holds is PROVEN fully closed);
* the run's own durable stage records and the ordinary ingestion facts
  (``live_order_intents`` / ``order_trade_fills`` / ``order_state_projection``)
  for what has already been sent and what has actually filled.

Four properties matter, and each is asserted by tests:

1. **No caller order list is trusted.** ``recommended_exit_orders`` from an
   evaluator is never submitted; only server-derived orders can reach a broker.
2. **A stage is durable BEFORE the send.** The stage claim - its digest, its
   deterministic client order references and the EXACT per-leg quantities - is
   written to the run's own durable record (under the run store's row lock) before
   the broker call. A crash between acceptance and recording therefore leaves a
   ``sending`` claim that is reconciled from the platform's pre-send records; the
   stage is never re-sent, and a changed evidence digest can never turn the same
   stage into a second, larger order.
3. **Only OUTSTANDING quantity is netted.** A partially filled order counts only
   its unfilled remainder, and a rejected one counts nothing, because the run's
   own position already reflects what filled. This is what prevents an oversized
   close on a restart.
4. **Own fills come from ordinary ingestion.** Confirmed trades for the platform's
   protection orders are read from ``order_trade_fills`` and recorded against the
   run's own leg ids, idempotently per trade. No second ledger, and no test-only
   shortcut: a short is proven closed by the same rows production writes.

Nothing here needs the child's credential: the platform's own broker session and
its own attribution carry the exit, which is what keeps a risk-REDUCING action
available after the child's authority is gone. It never re-grants that authority.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import text

from backend.app.database import SessionLocal

#: Stage states. ``sending`` is the durable pre-send claim; the others are the
#: outcomes it resolves to. An unresolved ``sending`` stage NEVER re-sends.
STAGE_SENDING = "sending"
STAGE_SUBMITTED = "submitted"
STAGE_PARTIAL = "partial"
STAGE_REJECTED = "rejected"
STAGE_UNKNOWN = "unknown"

RESOLVED_STAGE_STATES = (STAGE_SUBMITTED, STAGE_PARTIAL, STAGE_REJECTED, STAGE_UNKNOWN)

#: Owner-row resolution refusals (B2.4). The owner row is the worker-run ->
#: option-run relation that MOVES at a handover, so an unreadable owner table is
#: a refusal: falling back to the creation-time metadata binding would resolve a
#: structure to a superseded owner's run.
OWNER_UNREADABLE = "protection_owner_unreadable"
#: The creation-time binding and the structure's CURRENT owner disagree.
BINDING_CONFLICT = "option_run_binding_conflict"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:20]


def _stage_records(run: Any) -> List[Dict[str, Any]]:
    """Every stage record one run carries, oldest first."""
    return [
        dict(row)
        for row in (getattr(run, "orders", []) or [])
        if isinstance(row, Mapping) and row.get("stage_digest")
    ]


def unresolved_stage_claim(orders: Any) -> Optional[Dict[str, Any]]:
    """The newest stage claim in ``orders`` that has NOT resolved, if any.

    A ``sending`` claim means the platform committed to a stage and does not
    know whether the broker accepted it. It is reconciled from the platform's
    own pre-send records; until then NOTHING new is sent, because the retry key
    of a stage whose evidence moved is not the key of the stage that may be
    live.

    This is a MODULE-level rule on purpose: the staged-exit engine, the governed
    exit path and the owned-work snapshot all have to agree on what "an
    unresolved protective stage" means, and two implementations would eventually
    disagree.
    """
    if isinstance(orders, (str, bytes, bytearray)):
        # A raw database row may hand back the JSON text rather than a list.
        try:
            orders = json.loads(orders)
        except ValueError:
            # Unreadable stage records are NOT "no unresolved stage": the
            # platform cannot prove the stage settled, so it stays claimed.
            return {"state": STAGE_UNKNOWN, "reason": "stage_records_unreadable"}
    if orders is None:
        orders = []
    if not isinstance(orders, (list, tuple)):
        return {"state": STAGE_UNKNOWN, "reason": "stage_records_unreadable"}
    # Attempt-FENCED: a claim and its resolution are keyed by
    # ``(stage digest, attempt)``, so an old attempt's outcome can never
    # resolve a newer attempt's claim. ``unknown`` is UNRESOLVED work, not a
    # settled outcome: it keeps the claim (and every later attempt) blocked
    # until durable evidence covers every leg.
    claims: Dict[tuple, Dict[str, Any]] = {}
    order: List[tuple] = []
    for row in list(orders or []):
        if not isinstance(row, Mapping) or not row.get("stage_digest"):
            continue
        record = dict(row)
        digest = str(record.get("stage_digest") or "")
        if not digest:
            continue
        key = (digest, int(record.get("attempt") or 1))
        if str(record.get("state")) in (STAGE_SENDING, STAGE_UNKNOWN) and key not in claims:
            order.append(key)
        claims[key] = record
    for key in reversed(order):
        if str(claims[key].get("state")) in (STAGE_SENDING, STAGE_UNKNOWN):
            return claims[key]
    return None


class StagedStructureExit:
    """Derive and submit ONE stage of a bounded structure exit."""

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        run_store: Any = None,
        place_orders: Optional[Callable[..., Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        lease_seconds: float = 300.0,
    ) -> None:
        self.session_factory = session_factory or SessionLocal
        self._run_store = run_store
        #: The broker boundary, called as
        #: ``place_orders(account_id=..., worker_run_id=..., option_run_id=...,
        #: structure_digest=..., legs=[{index,tradingsymbol,transaction_type,
        #: quantity,client_order_ref}], idempotency_key=...)`` and answering
        #: ``{"legs": [{"index": int, "order_id": str|None, "error": str|None}]}``.
        #: ``None`` means the platform cannot place anything, so the stage is
        #: refused (never simulated).
        self._place_orders = place_orders
        self._clock = clock or _utcnow
        #: This sender's identity and how long its stage claim is stamped as
        #: exclusive. The claim is taken under the run's row lock, so two senders can
        #: never both own one stage; the lease is an OBSERVATION a later pass may
        #: report, NOT proof that the owner is gone. There is no fence at the broker
        #: write, so an owner paused past its lease can still send: missing pre-send
        #: records are never read as absence on the strength of elapsed time alone.
        self.owner = f"staged-exit-{uuid.uuid4().hex[:12]}"
        self.lease_seconds = float(lease_seconds)

    # -- collaborators ------------------------------------------------------

    def _runs(self) -> Any:
        if self._run_store is None:
            from backend.options.execution.durable_store import DurableOptionRunStore

            self._run_store = DurableOptionRunStore(session_factory=self.session_factory)
        return self._run_store

    # -- run resolution -----------------------------------------------------

    def resolve_run_for_worker_run(
        self, *, worker_run_id: str, account_id: str = ""
    ) -> tuple[Optional[Any], Dict[str, Any]]:
        """The ONE durable option run bound to a hosted worker run.

        Resolution is OWNER ROW FIRST (B2.4): the ACTIVE protection owner row whose
        ``owner_run_id`` is this worker run IS the relation between a hosted run
        and the structure it protects. The row MOVES when a handover transfers the
        structure to a successor, while the creation-time
        ``metadata.worker_run_id`` binding does not - so resolving from metadata
        after a handover would find nothing and leave the structure unprotected.

        The creation-time binding remains the FALLBACK, for the direct options API
        (where the caller chooses the worker run id as the option run id) and for
        runs that predate B2.4. It may only be used when NO active owner row names
        this worker run AND the run it resolves has no active owner of its own: a
        stale binding whose structure has moved is refused by name.

        More than one binding is AMBIGUOUS, not "pick the newest": a worker run
        whose structures cannot be told apart must not be declared complete on the
        strength of whichever row happened to be updated last. An owner row that
        CONTRADICTS the creation binding is a data error, not a preference; both
        refuse by name. An unreadable owner row refuses too (fail closed), never
        falling back silently to the stale metadata. The run's recorded account
        must also match the worker run's account in either shape - a run belonging
        to another account is never this run's.
        """
        run_id = str(worker_run_id or "")
        if not run_id:
            return None, {"reason": "no_bound_option_run", "worker_run_id": run_id}
        owner_candidates, owner_refusal = self._owner_bound_runs(
            run_id, account_id=account_id
        )
        if owner_refusal is not None:
            return None, owner_refusal
        if owner_candidates:
            return self._resolve_from_owner_row(
                run_id, owner_candidates, account_id=account_id
            )
        return self._resolve_from_metadata_binding(run_id, account_id=account_id)

    def _resolve_from_owner_row(
        self,
        run_id: str,
        owner_candidates: List[Dict[str, Any]],
        *,
        account_id: str,
    ) -> tuple[Optional[Any], Dict[str, Any]]:
        """Resolve through the ACTIVE owner row(s) naming this worker run."""

        if len(owner_candidates) > 1:
            return None, {
                "reason": "option_run_ambiguous",
                "source": "protection_owner",
                "worker_run_id": run_id,
                "candidates": sorted(
                    str(row["option_run_id"]) for row in owner_candidates
                ),
            }
        chosen = str(owner_candidates[0]["option_run_id"])
        metadata_candidates, metadata_refusal, metadata_mismatch = (
            self._metadata_bound_runs(run_id, account_id=account_id)
        )
        if metadata_refusal is not None or metadata_mismatch:
            # The creation-time binding does not agree with the row that owns the
            # structure NOW: refuse rather than pick, exactly as ambiguity does.
            return None, {
                "reason": BINDING_CONFLICT,
                "worker_run_id": run_id,
                "owner_run_candidates": [chosen],
                "metadata_candidates": list(metadata_candidates),
                "metadata_reason": (
                    None if metadata_refusal is None else metadata_refusal["reason"]
                ),
            }
        if metadata_candidates and metadata_candidates != [chosen]:
            return None, {
                "reason": BINDING_CONFLICT,
                "worker_run_id": run_id,
                "owner_run_candidates": [chosen],
                "metadata_candidates": list(metadata_candidates),
            }
        return self._runs().get_run(chosen), {
            "reason": "ok",
            "option_run_id": chosen,
            "candidates": 1,
            "source": "protection_owner",
        }

    def _resolve_from_metadata_binding(
        self, run_id: str, *, account_id: str
    ) -> tuple[Optional[Any], Dict[str, Any]]:
        """The pre-B2.4 / direct-options-API fallback."""

        candidates, refusal, _mismatch = self._metadata_bound_runs(
            run_id, account_id=account_id
        )
        if refusal is not None:
            return None, refusal
        if not candidates:
            return None, {"reason": "no_bound_option_run", "worker_run_id": run_id}
        if len(candidates) > 1:
            return None, {
                "reason": "option_run_ambiguous",
                "source": "metadata_binding",
                "worker_run_id": run_id,
                "candidates": list(candidates),
            }
        # The binding must not resolve a structure that now has a DIFFERENT active
        # owner: that run is superseded, and a superseded run must not act.
        stale, stale_refusal = self._active_owners_for_option_runs(candidates)
        if stale_refusal is not None:
            return None, stale_refusal
        if stale:
            return None, {
                "reason": BINDING_CONFLICT,
                "worker_run_id": run_id,
                "metadata_candidates": list(candidates),
                "current_owner_run_id": str(
                    (stale[0] or {}).get("owner_run_id") or ""
                ),
            }
        chosen = str(candidates[0])
        return self._runs().get_run(chosen), {
            "reason": "ok",
            "option_run_id": chosen,
            "candidates": 1,
            "source": "metadata_binding",
        }

    def _owner_bound_runs(
        self, run_id: str, *, account_id: str
    ) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """The ACTIVE owner rows naming this worker run, or a named refusal.

        The creation-time binding is a SNAPSHOT; the owner row is the live record.
        An unreadable owner read therefore refuses by name - it never falls back to
        the snapshot, which after a handover names the predecessor.

        The row's account is checked the same way the creation binding's is: a row
        whose account is missing or different is a mismatch, and a mismatch refuses
        rather than being skipped.
        """

        rows, refusal = self._owner_rows(
            "owner_run_id = :run_id",
            {"run_id": run_id},
            refusal_extra={"worker_run_id": run_id},
        )
        if refusal is not None:
            return [], refusal
        candidates: List[Dict[str, Any]] = []
        mismatched: List[Dict[str, Any]] = []
        for row in rows:
            bound_account = str(row.get("account_id") or "")
            if not bound_account or (account_id and bound_account != str(account_id)):
                mismatched.append(
                    {
                        "option_run_id": str(row.get("option_run_id") or ""),
                        "bound_account_id": bound_account or None,
                    }
                )
                continue
            candidates.append(row)
        if mismatched:
            return [], {
                "reason": "option_run_account_mismatch",
                "source": "protection_owner",
                "worker_run_id": run_id,
                "account_id": str(account_id),
                "mismatched": mismatched,
            }
        return candidates, None

    def _active_owners_for_option_runs(
        self, option_run_ids: Sequence[str]
    ) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """The ACTIVE owner rows of these option runs, or a named refusal."""

        ids = [str(value) for value in option_run_ids if str(value)]
        if not ids:
            return [], None
        placeholders = ", ".join(f":id{index}" for index in range(len(ids)))
        params = {f"id{index}": value for index, value in enumerate(ids)}
        return self._owner_rows(
            f"option_run_id IN ({placeholders})",
            params,
            refusal_extra={"option_run_ids": ids},
        )

    def _owner_rows(
        self,
        predicate: str,
        params: Dict[str, Any],
        *,
        refusal_extra: Dict[str, Any],
    ) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """ACTIVE owner rows matching one predicate; unreadable refuses by name."""

        try:
            with self.session_factory() as session:
                rows = (
                    session.execute(
                        text(
                            "SELECT option_run_id, owner_run_id, account_id "
                            "FROM public.option_protection_owners "
                            f"WHERE state = 'active' AND {predicate} "
                            "ORDER BY option_run_id"
                        ),
                        params,
                    )
                    .mappings()
                    .all()
                )
        except Exception as exc:  # noqa: BLE001 - fail closed, never fall back
            return [], {
                "reason": OWNER_UNREADABLE,
                "error": f"{type(exc).__name__}: {exc}",
                **refusal_extra,
            }
        return [dict(row) for row in rows], None

    def _metadata_bound_runs(
        self, run_id: str, *, account_id: str
    ) -> tuple[List[str], Optional[Dict[str, Any]], bool]:
        """The option runs whose CREATION binding names this worker run.

        Returns ``(candidates, refusal, account_mismatch)``: the same rule this
        method has always applied (a row whose recorded account is missing or
        different is a mismatch, and any mismatch refuses rather than being
        skipped).
        """

        with self.session_factory() as session:
            rows = (
                session.execute(
                    text(
                        "SELECT strategy_run_id, metadata FROM public.option_run_states "
                        "WHERE metadata ->> 'worker_run_id' = :run_id "
                        "ORDER BY updated_at DESC"
                    ),
                    {"run_id": run_id},
                )
                .mappings()
                .all()
            )
        if not rows:
            return [], None, False
        candidates: List[str] = []
        mismatched: List[Dict[str, Any]] = []
        for row in rows:
            metadata = row["metadata"]
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata or "{}")
                except ValueError:
                    metadata = {}
            metadata = dict(metadata or {})
            bound_account = str(metadata.get("account_id") or "")
            if not bound_account or (account_id and bound_account != str(account_id)):
                mismatched.append(
                    {
                        "option_run_id": str(row["strategy_run_id"]),
                        "bound_account_id": bound_account or None,
                    }
                )
                continue
            candidates.append(str(row["strategy_run_id"]))
        if mismatched:
            return [], {
                "reason": "option_run_account_mismatch",
                "worker_run_id": run_id,
                "account_id": str(account_id),
                "mismatched": mismatched,
            }, True
        return candidates, None, False

    # -- ordinary ingestion reads -------------------------------------------

    def order_fills(
        self, order_ids: Sequence[str], *, account_id: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Confirmed fills per broker order, for ONE bound account.

        Broker order ids are account-scoped: the same id can exist on another
        account, so a read that ignored the bound account could attribute a
        stranger's fill to this run and falsely prove a short closed.
        """
        ids = [str(value) for value in order_ids if str(value)]
        if not ids or not str(account_id or ""):
            return {}
        with self.session_factory() as session:
            rows = (
                session.execute(
                    text(
                        "SELECT trade_id, order_id, quantity, price, transaction_type, "
                        " tradingsymbol, instrument_token, product "
                        "FROM public.order_trade_fills "
                        "WHERE account_id = :account AND order_id = ANY(:ids) "
                        "ORDER BY fill_timestamp, trade_id"
                    ),
                    {"ids": ids, "account": str(account_id)},
                )
                .mappings()
                .all()
            )
        out: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(str(row["order_id"]), []).append(dict(row))
        return out

    def order_terminal_status(
        self, order_ids: Sequence[str], *, account_id: str
    ) -> Dict[str, str]:
        """The broker's terminal status per order, for ONE bound account."""
        ids = [str(value) for value in order_ids if str(value)]
        if not ids or not str(account_id or ""):
            return {}
        with self.session_factory() as session:
            rows = (
                session.execute(
                    text(
                        "SELECT order_id, latest_status, terminal "
                        "FROM public.order_state_projection "
                        "WHERE account_id = :account AND order_id = ANY(:ids)"
                    ),
                    {"ids": ids, "account": str(account_id)},
                )
                .mappings()
                .all()
            )
        return {
            str(row["order_id"]): (
                str(row["latest_status"] or "").upper() + ("|TERMINAL" if row["terminal"] else "")
            )
            for row in rows
        }

    # -- durable stage records ----------------------------------------------

    @staticmethod
    def stage_records(run: Any) -> List[Dict[str, Any]]:
        """Every stage record this run carries, oldest first."""
        return _stage_records(run)

    def unresolved_stage(self, run: Any) -> Optional[Dict[str, Any]]:
        """The newest stage claim that has not resolved, if any."""
        return unresolved_stage_claim(getattr(run, "orders", []) or [])

    def _record_stage(self, run_id: str, record: Mapping[str, Any]) -> None:
        self._runs().record_orders(str(run_id), [dict(record)])

    # -- reconciliation -----------------------------------------------------

    def reconcile_own_fills(self, run: Any) -> Dict[str, Any]:
        """Record confirmed fills of OUR OWN protection orders on the run.

        The fills themselves are written by the ORDINARY ingestion path
        (``order_trade_fills``); this only translates the ones whose attribution is
        the platform's structure protection into the run's own trade records, keyed
        by ``(order_id, trade_id)`` so a replay records nothing twice. That is what
        makes a short's closure provable in a real deployment rather than only in a
        test that seeded the trades by hand.
        """
        recorded: List[Dict[str, Any]] = []
        mismatched: List[Dict[str, Any]] = []
        legs: Dict[str, Dict[str, Any]] = {}
        order_ids: List[str] = []
        for record in self.stage_records(run):
            account_id = str(record.get("account_id") or "")
            for leg in record.get("legs") or []:
                leg = dict(leg or {})
                order_id = str(leg.get("order_id") or "")
                if not order_id:
                    continue
                order_ids.append(order_id)
                legs[order_id] = {**leg, "account_id": account_id}
        # Account-scoped: another account's order with the same id is NOT ours.
        fills: Dict[str, List[Dict[str, Any]]] = {}
        for order_id, leg in legs.items():
            account_id = str(leg.get("account_id") or "")
            for row in self.order_fills([order_id], account_id=account_id).get(order_id, []):
                fills.setdefault(order_id, []).append(row)
        for order_id, rows in fills.items():
            leg = legs.get(order_id)
            if leg is None:
                continue
            for row in rows:
                # The fill must describe the LEG this run actually sent: a symbol,
                # side, instrument or product that disagrees is not evidence, and
                # recording it could falsely close a short.
                if not self._fill_matches_leg(row, leg):
                    mismatched.append(
                        {
                            "order_id": order_id,
                            "trade_id": str(row.get("trade_id") or ""),
                            "expected": {
                                "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                                "transaction_type": str(leg.get("transaction_type") or ""),
                                "instrument_token": leg.get("instrument_token"),
                                "product": str(leg.get("product") or ""),
                            },
                            "actual": {
                                "tradingsymbol": str(row.get("tradingsymbol") or ""),
                                "transaction_type": str(row.get("transaction_type") or ""),
                                "instrument_token": row.get("instrument_token"),
                                "product": str(row.get("product") or ""),
                            },
                        }
                    )
                    continue
                marker = f"{order_id}:{row.get('trade_id')}"
                raw_price = row.get("price")
                try:
                    price = None if raw_price is None else float(raw_price)
                except (TypeError, ValueError):
                    price = None
                recorded.append(
                    {
                        "leg_id": str(leg.get("leg_id") or ""),
                        "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                        "transaction_type": str(leg.get("transaction_type") or "").upper(),
                        "quantity": abs(int(row.get("quantity") or 0)),
                        "price": price,
                        "order_id": order_id,
                        "trade_id": str(row.get("trade_id") or ""),
                        "stage_fill_id": marker,
                        "source": "hosted_option_protection_ingestion",
                        "recorded_at": self._clock().isoformat(),
                    }
                )
        if recorded:
            # The dedup happens INSIDE the run's row-locked transaction, so two
            # reconcilers that both observed the marker as absent cannot both write
            # the same fill (which would double the book and could falsely prove a
            # short closed).
            appended, skipped = self._runs().record_trades_once(
                str(run.strategy_run_id), recorded, dedupe_key="stage_fill_id"
            )
            return {
                "recorded": len(appended),
                "skipped": len(skipped),
                "trades": appended,
                "mismatched_fills": mismatched,
                "option_run_id": str(run.strategy_run_id),
            }
        return {
            "recorded": 0,
            "skipped": 0,
            "mismatched_fills": mismatched,
            "option_run_id": str(run.strategy_run_id),
        }

    @staticmethod
    def _fill_matches_leg(row: Mapping[str, Any], leg: Mapping[str, Any]) -> bool:
        """Whether an ingestion fill really describes the leg this run sent."""
        symbol = str(row.get("tradingsymbol") or "")
        expected_symbol = str(leg.get("tradingsymbol") or "")
        if symbol and expected_symbol and symbol != expected_symbol:
            return False
        side = str(row.get("transaction_type") or "").upper()
        expected_side = str(leg.get("transaction_type") or "").upper()
        if side and expected_side and side != expected_side:
            return False
        product = str(row.get("product") or "").upper()
        expected_product = str(leg.get("product") or "").upper()
        if product and expected_product and product != expected_product:
            return False
        expected_token = leg.get("instrument_token")
        token = row.get("instrument_token")
        if expected_token is not None and token is not None and int(expected_token) != int(token):
            return False
        return True

    def _resolve_sending_stage(self, run: Any, claim: Mapping[str, Any]) -> Dict[str, Any]:
        """Classify a ``sending`` claim from the platform's own pre-send records.

        Every planned leg has a deterministic client order reference (written
        BEFORE the broker call), so the platform can say, per leg, whether the send
        reached the order path and whether the broker acknowledged it. A leg that
        is genuinely UNKNOWN keeps the stage unresolved - nothing is re-sent on a
        guess.

        The trap is a sender paused BETWEEN claiming the stage and creating its
        pre-send records: "no pre-send record yet" looks exactly like "nothing was
        sent", and the claim's LEASE cannot tell them apart.

        The lease is an OBSERVATION, never proof of cessation: a sender paused past
        its lease can still resume and call the broker, because there is no fence at
        the broker write itself. So an expired lease does NOT let the platform read
        missing pre-send rows as non-submission, and it is reported for
        observability only.

        The only way out of this state is durable evidence that covers EVERY leg:
        the broker's OWN order reference in the pre-send records. An IDLESS row is
        never non-submission and never earns a retry - not even when it is marked
        ``failed`` - because the order path records an explicit broker refusal and a
        post-acceptance ambiguity (a timeout, a socket close, a process death after
        the broker took the order) under the same ``failed`` status, told apart only
        by free text in ``error_json``. Anything unresolvable stays ``unknown`` and
        keeps blocking.
        """
        lease_until = claim.get("lease_until")
        if not lease_until:
            return {
                "state": STAGE_UNKNOWN,
                "reason": "SENDER_RELINQUISH_NOT_PROVEN",
                "message": (
                    "the claim carries no lease, so the platform cannot prove the "
                    "sender is gone and must not treat missing pre-send records as "
                    "non-submission"
                ),
            }
        lease_expired = False
        try:
            parsed = datetime.fromisoformat(str(lease_until).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            lease_expired = bool(parsed <= self._clock())
        except ValueError:
            parsed = None
        observation = {
            "lease_until": None if parsed is None else parsed.isoformat(),
            "lease_expired": lease_expired,
        }
        legs = [dict(leg or {}) for leg in (claim.get("legs") or [])]
        refs = [str(leg.get("client_order_ref") or "") for leg in legs]
        if any(not ref for ref in refs):
            return {
                "state": STAGE_UNKNOWN,
                "reason": "CLIENT_ORDER_REF_MISSING",
                **observation,
            }
        try:
            with self.session_factory() as session:
                rows = (
                    session.execute(
                        text(
                            "SELECT client_order_ref, broker_order_id, status "
                            "FROM public.live_order_intents "
                            "WHERE account_id = :account AND client_order_ref = ANY(:refs)"
                        ),
                        {"account": str(claim.get("account_id") or ""), "refs": refs},
                    )
                    .mappings()
                    .all()
                )
        except Exception as exc:  # noqa: BLE001 - an unreadable source is UNKNOWN
            return {
                "state": STAGE_UNKNOWN,
                "reason": "FENCE_READ_FAILED",
                "error": str(exc),
                **observation,
            }
        by_ref = {str(row["client_order_ref"]): dict(row) for row in rows}
        outcomes: List[Dict[str, Any]] = []
        unknown = 0
        accepted = 0
        for index, leg in enumerate(legs):
            ref = str(leg.get("client_order_ref") or "")
            row = by_ref.get(ref)
            if row is None:
                # NO pre-send record for this leg. That is UNKNOWN, never "not
                # sent": a paused sender may not have written its row yet.
                unknown += 1
                outcomes.append({"index": index, "client_order_ref": ref, "state": "unknown"})
                continue
            order_id = str(row.get("broker_order_id") or "")
            if order_id:
                accepted += 1
                outcomes.append(
                    {
                        "index": index,
                        "client_order_ref": ref,
                        "order_id": order_id,
                        "state": "accepted",
                    }
                )
                continue
            # An IDLESS pre-send row is NOT proof of non-submission, whatever its
            # status says. The order path writes ``failed`` both for an explicit
            # broker refusal and for an ambiguous failure AFTER the broker may have
            # accepted (``OrdersService.place_order``'s generic ``except Exception``
            # calls ``mark_live_order_intent_failed``). Retrying on that would
            # duplicate an exit that a live order already performs, so the leg stays
            # UNKNOWN, its status is carried as an observation, and the stage keeps
            # owning the send.
            unknown += 1
            outcomes.append(
                {
                    "index": index,
                    "client_order_ref": ref,
                    "state": "unknown",
                    "status": str(row.get("status") or ""),
                }
            )
        if unknown:
            # At least one leg is unresolvable: the stage stays UNKNOWN and keeps
            # blocking. Any references that WERE found are still folded onto their
            # legs, so their fills are tracked while the rest stays blocked.
            return {
                "state": STAGE_UNKNOWN,
                "reason": "STAGE_OUTCOME_UNKNOWN",
                "leg_outcomes": outcomes,
                "legs": [
                    {**leg, "order_id": outcome.get("order_id")}
                    for leg, outcome in zip(legs, outcomes)
                ],
                **observation,
            }
        if accepted:
            state = STAGE_SUBMITTED if accepted == len(legs) else STAGE_PARTIAL
            return {
                "state": state,
                "reason": "RECONCILED_FROM_PRE_SEND_RECORDS",
                "leg_outcomes": outcomes,
                # The discovered references are folded back into the legs, because
                # they are what a later pass reads the ORDINARY fill facts by.
                "legs": [
                    {**leg, "order_id": outcome.get("order_id")}
                    for leg, outcome in zip(legs, outcomes)
                ],
                **observation,
            }
        # Nothing was accepted AND nothing was proven not-submitted: there is no
        # evidence to retry on, so the stage stays UNKNOWN.
        return {
            "state": STAGE_UNKNOWN,
            "reason": "STAGE_OUTCOME_UNKNOWN",
            "leg_outcomes": outcomes,
            **observation,
        }

    # -- evidence -----------------------------------------------------------

    @staticmethod
    def own_open_by_leg(run: Any) -> Dict[str, int]:
        """The run's OWN open quantity per leg, from its recorded trades."""
        open_by_leg: Dict[str, int] = {}
        for trade in getattr(run, "trades", []) or []:
            leg_id = str((trade or {}).get("leg_id") or "")
            if not leg_id:
                continue
            quantity = int((trade or {}).get("quantity") or 0)
            side = str((trade or {}).get("transaction_type") or "").upper()
            open_by_leg[leg_id] = open_by_leg.get(leg_id, 0) + (
                quantity if side == "BUY" else -quantity
            )
        return open_by_leg

    @staticmethod
    def own_peak_by_leg(run: Any) -> Dict[str, int]:
        """The LARGEST quantity the run ever held per leg, from its own trades."""
        peak: Dict[str, int] = {}
        running: Dict[str, int] = {}
        for trade in getattr(run, "trades", []) or []:
            leg_id = str((trade or {}).get("leg_id") or "")
            if not leg_id:
                continue
            quantity = int((trade or {}).get("quantity") or 0)
            side = str((trade or {}).get("transaction_type") or "").upper()
            running[leg_id] = running.get(leg_id, 0) + (
                quantity if side == "BUY" else -quantity
            )
            peak[leg_id] = max(peak.get(leg_id, 0), abs(running[leg_id]))
        return peak

    def outstanding_by_symbol(self, run: Any) -> tuple[Dict[str, int], Dict[str, int]]:
        """``(buy, sell)`` quantities that are still WORKING per symbol.

        Only the OUTSTANDING part of an order counts: the run's own position
        already reflects what filled, so subtracting a filled quantity again would
        double-count it and could hide a genuine remainder. A rejected or cancelled
        order with nothing filled is outstanding for nothing, which is what lets a
        later stage pick the exit back up instead of treating it as submitted.
        """
        buy: Dict[str, int] = {}
        sell: Dict[str, int] = {}
        order_ids: List[str] = []
        legs: List[Dict[str, Any]] = []
        accounts: Dict[str, str] = {}
        seen_orders: set = set()
        for record in self.stage_records(run):
            if str(record.get("state")) == STAGE_SENDING:
                # A claim that has not resolved names no order yet: nothing is
                # KNOWN to be working, and the unresolved-stage guard (not this
                # netting) is what stops a second send.
                continue
            for leg in record.get("legs") or []:
                leg = dict(leg or {})
                order_id = str(leg.get("order_id") or "")
                if not order_id or order_id in seen_orders:
                    # A leg the broker never acknowledged is outstanding for
                    # NOTHING: treating it as submitted would silently suppress a
                    # necessary exit. A leg already counted once is not counted
                    # again by a later record of the same stage.
                    continue
                seen_orders.add(order_id)
                order_ids.append(order_id)
                accounts[order_id] = str(record.get("account_id") or "")
                legs.append(leg)
        # Account-scoped reads: an order id that belongs to another account is not
        # a working order of this run.
        fills: Dict[str, List[Dict[str, Any]]] = {}
        for order_id in order_ids:
            fills.update(
                self.order_fills([order_id], account_id=accounts.get(order_id, ""))
            )
        for leg in legs:
            symbol = str(leg.get("tradingsymbol") or "")
            side = str(leg.get("transaction_type") or "").upper()
            requested = abs(int(leg.get("quantity") or 0))
            if not symbol or side not in ("BUY", "SELL") or requested <= 0:
                continue
            order_id = str(leg.get("order_id") or "")
            filled = sum(
                abs(int(row.get("quantity") or 0))
                for row in fills.get(order_id, [])
                if str(row.get("transaction_type") or "").upper() in ("", side)
            )
            outstanding = max(0, requested - filled)
            if outstanding <= 0:
                continue
            target = buy if side == "BUY" else sell
            target[symbol] = target.get(symbol, 0) + outstanding
        return buy, sell

    def short_closure_state(self, run: Any) -> tuple[bool, Dict[str, int]]:
        """``(every_short_fully_closed, proven_closure_per_symbol)``.

        The SELECTED release contract is conservative: a hedge is released only
        once every short the run holds is proven fully closed by the run's own
        confirmed fills. One place decides that, so the position rows and the proof
        can never disagree.
        """
        peak_by_leg = self.own_peak_by_leg(run)
        open_by_leg = self.own_open_by_leg(run)
        all_closed = True
        proven: Dict[str, int] = {}
        for leg in getattr(run, "legs", []) or []:
            leg = dict(leg or {})
            if str(leg.get("transaction_type") or "").upper() != "SELL":
                continue
            leg_id = str(leg.get("leg_id") or "")
            peak = int(peak_by_leg.get(leg_id, 0))
            if peak == 0:
                continue
            remaining = max(0, -int(open_by_leg.get(leg_id, 0)))
            if remaining > 0:
                all_closed = False
                continue
            proven[str(leg.get("tradingsymbol") or "")] = peak
        return all_closed, proven

    def own_positions(self, run: Any) -> List[Dict[str, Any]]:
        """The run's own positions as the exit builder's row shape (SIGNED)."""
        peak_by_leg = self.own_peak_by_leg(run)
        open_by_leg = self.own_open_by_leg(run)
        outstanding_buy, outstanding_sell = self.outstanding_by_symbol(run)
        shorts_closed, proven_by_symbol = self.short_closure_state(run)
        rows: List[Dict[str, Any]] = []
        for leg in getattr(run, "legs", []) or []:
            leg = dict(leg or {})
            leg_id = str(leg.get("leg_id") or "")
            peak = int(peak_by_leg.get(leg_id, 0))
            if peak == 0:
                continue
            side = str(leg.get("transaction_type") or "").upper()
            symbol = str(leg.get("tradingsymbol") or "")
            net = int(open_by_leg.get(leg_id, 0))
            if side == "SELL":
                open_short = max(0, -net)
                proven = int(proven_by_symbol.get(symbol, 0))
                to_close = max(0, open_short - outstanding_buy.get(symbol, 0))
                liability = -(to_close + proven)
            else:
                if not shorts_closed:
                    # The hedge stays exactly where it is until every short it can
                    # answer for is PROVEN gone.
                    continue
                open_long = max(0, net)
                liability = max(0, open_long - outstanding_sell.get(symbol, 0))
            if liability == 0:
                continue
            metadata = dict(leg.get("metadata") or {})
            rows.append(
                {
                    "tradingsymbol": symbol,
                    # NOT ``structure_leg_id``/``hedge_for``: those are the builder's
                    # "this long answers for that short" mapping, and a run that
                    # declares none must not point a hedge at its own id.
                    "run_leg_id": leg_id,
                    "side": "SELL" if liability < 0 else "BUY",
                    "quantity": int(liability),
                    "exchange": str(leg.get("exchange") or "NFO"),
                    "product": str(run.product or leg.get("product") or ""),
                    "underlying": str(metadata.get("underlying") or ""),
                    "expiry": str(leg.get("expiry_key") or metadata.get("expiry") or ""),
                    "exit_order_type": leg.get("exit_order_type"),
                    "exit_price": leg.get("exit_price"),
                    "limit_price": leg.get("limit_price"),
                }
            )
        return rows

    def plan_exit(self, run: Any) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """The bounded exit actions for this run's CURRENT state, or none."""
        from backend.options.protection.exit_builder import build_structure_exit_orders

        positions = self.own_positions(run)
        if not positions:
            return [], {"positions": 0, "reason": "run_flat"}
        _closed, proven = self.short_closure_state(run)
        orders, detail = build_structure_exit_orders(
            positions, closed_short_quantities=proven
        )
        return list(orders), {"positions": len(positions), **dict(detail or {})}

    def run_is_flat(self, run: Any) -> bool:
        """Whether the run's OWN confirmed fills say it holds nothing."""
        open_by_leg = self.own_open_by_leg(run)
        for leg in getattr(run, "legs", []) or []:
            leg_id = str((leg or {}).get("leg_id") or "")
            if int(open_by_leg.get(leg_id, 0)) != 0:
                return False
        return True

    # -- submission ---------------------------------------------------------

    def _stage_legs(
        self,
        *,
        orders: Sequence[Mapping[str, Any]],
        digest: str,
        attempt: int = 1,
        leg_ids_by_symbol: Optional[Mapping[str, str]] = None,
        legs_by_symbol: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """The claim's exact per-leg intent, with deterministic client references.

        The reference is ``(evidence digest, attempt, leg index)``: stable for one
        attempt - so a crash can never turn it into a second order, and the pre-send
        fence can find it - and DIFFERENT for a deliberate retry after evidence
        proves nothing was placed. The platform's client reference is unique, so a
        retry that reused it would be refused outright.
        """
        stage = f"{digest[:6].upper()}{int(attempt)}"
        by_symbol = dict(leg_ids_by_symbol or {})
        run_legs = dict(legs_by_symbol or {})
        legs: List[Dict[str, Any]] = []
        for index, order in enumerate(orders):
            order = dict(order or {})
            symbol = str(order.get("tradingsymbol") or "")
            run_leg = dict(run_legs.get(symbol) or {})
            legs.append(
                {
                    "index": index,
                    "client_order_ref": f"KA{stage}{index + 1:02d}",
                    "tradingsymbol": symbol,
                    "transaction_type": str(order.get("transaction_type") or "").upper(),
                    "quantity": abs(int(order.get("quantity") or 0)),
                    # The exit builder's order rows carry the CONTRACT, not the run's
                    # leg id, so the claim resolves it by symbol: the reconciled
                    # fills then land on the run's OWN leg, which is what the proof
                    # is read from.
                    "leg_id": str(
                        order.get("run_leg_id") or by_symbol.get(symbol) or run_leg.get("leg_id") or ""
                    ),
                    # The expectations a later ingestion read is checked against:
                    # a fill that disagrees with these is NOT this run's evidence.
                    "instrument_token": run_leg.get("instrument_token"),
                    "exchange": str(order.get("exchange") or "NFO"),
                    "product": str(order.get("product") or ""),
                    "variety": str(order.get("variety") or "regular"),
                    "order_type": str(order.get("order_type") or "MARKET"),
                }
            )
        return legs

    async def submit(
        self, *, worker_run: Mapping[str, Any], trigger: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Submit ONE stage of the structure exit, or refuse by name."""
        if str((trigger or {}).get("status") or "") != "triggered":
            return {"submitted": False, "complete": False, "reason": "not_triggered", "orders": []}
        worker_run_id = str((worker_run or {}).get("strategy_run_id") or "")
        account_id = str((worker_run or {}).get("account_scope") or "")
        config = dict(
            (((worker_run or {}).get("runtime_state") or {}).get("backend_protection")) or {}
        )
        structure_digest = str((config.get("structure") or {}).get("structure_digest") or "")
        try:
            run, resolution = self.resolve_run_for_worker_run(
                worker_run_id=worker_run_id, account_id=account_id
            )
        except Exception as exc:  # noqa: BLE001 - unreadable attribution is a refusal
            return {
                "submitted": False,
                "complete": False,
                "reason": "attribution_unavailable",
                "error": str(exc),
                "orders": [],
            }
        if run is None:
            # Unknown or AMBIGUOUS attribution: the platform will NOT fall back to
            # a whole-book exit, and will not declare a worker complete on the
            # strength of one arbitrarily chosen run.
            return {
                "submitted": False,
                "complete": False,
                "reason": str(resolution.get("reason") or "no_bound_option_run"),
                "resolution": dict(resolution),
                "worker_run_id": worker_run_id,
                "orders": [],
            }
        # 1. Translate the fills ordinary ingestion already confirmed for OUR
        #    protection orders onto this run, so the proof below is real evidence.
        try:
            ingested = self.reconcile_own_fills(run)
        except Exception as exc:  # noqa: BLE001 - unreadable ingestion is a refusal
            return {
                "submitted": False,
                "complete": False,
                "reason": "own_fill_reconcile_failed",
                "error": str(exc),
                "option_run_id": str(run.strategy_run_id),
                "orders": [],
            }
        run = self._runs().get_run(str(run.strategy_run_id))
        # 2. A stage the platform already committed to and has not resolved is
        #    NEVER re-sent: reconcile it from the platform's own pre-send records.
        unresolved = self.unresolved_stage(run)
        if unresolved is not None:
            verdict = self._resolve_sending_stage(run, unresolved)
            if verdict.get("state") == STAGE_UNKNOWN:
                return {
                    "submitted": False,
                    "complete": False,
                    "reason": "stage_send_unknown",
                    "stage_digest": str(unresolved.get("stage_digest") or ""),
                    "option_run_id": str(run.strategy_run_id),
                    "leg_outcomes": list(verdict.get("leg_outcomes") or []),
                    "orders": [],
                }
            resolution_record = {
                **dict(unresolved),
                "state": str(verdict.get("state")),
                "reason": str(verdict.get("reason") or ""),
                "leg_outcomes": list(verdict.get("leg_outcomes") or []),
                "legs": list(verdict.get("legs") or unresolved.get("legs") or []),
                "resolved_at": self._clock().isoformat(),
            }
            self._record_stage(str(run.strategy_run_id), resolution_record)
            run = self._runs().get_run(str(run.strategy_run_id))
        # 3. Derive the bounded actions for the current evidence.
        orders, detail = self.plan_exit(run)
        flat = self.run_is_flat(run)
        if not orders:
            return {
                "submitted": True,
                "complete": flat,
                "reason": (
                    str(detail.get("reason") or "structure_flat")
                    if flat
                    else "no_permitted_action"
                ),
                "option_run_id": str(run.strategy_run_id),
                "orders": [],
                "detail": detail,
                "ingested_fills": int(ingested.get("recorded") or 0),
            }
        evidence = {
            "option_run_id": str(run.strategy_run_id),
            "orders": orders,
            "detail": detail,
        }
        digest = _digest(evidence)
        # The ATTEMPT number is derived from the run's own durable records for this
        # evidence: an unresolved claim (or the first attempt) keeps it stable, and
        # only a settled "nothing was placed" outcome advances it, which is what
        # lets a refused leg be tried again without ever colliding with the
        # platform's unique client reference of the attempt that already happened.
        attempt = 1 + sum(
            1
            for row in self.stage_records(run)
            if str(row.get("stage_digest")) == digest
            and str(row.get("state")) != STAGE_SENDING
        )
        run_legs = {
            str((leg or {}).get("tradingsymbol") or ""): {
                "leg_id": str((leg or {}).get("leg_id") or ""),
                "instrument_token": (leg or {}).get("instrument_token"),
                "product": str((leg or {}).get("product") or ""),
            }
            for leg in (getattr(run, "legs", []) or [])
        }
        legs = self._stage_legs(
            orders=orders,
            digest=digest,
            attempt=attempt,
            legs_by_symbol=run_legs,
            leg_ids_by_symbol={
                symbol: str(meta.get("leg_id") or "") for symbol, meta in run_legs.items()
            },
        )
        recorded = {
            str(row.get("stage_digest")): dict(row) for row in self.stage_records(run)
        }
        previous = recorded.get(digest)
        if previous is not None:
            previous_state = str(previous.get("state") or "")
            if previous_state in (STAGE_SUBMITTED, STAGE_PARTIAL):
                return {
                    "submitted": True,
                    "complete": flat,
                    "reason": "already_submitted",
                    "stage_digest": digest,
                    "option_run_id": str(run.strategy_run_id),
                    "orders": orders,
                    "order_ids": [
                        str(leg.get("order_id"))
                        for leg in (previous.get("legs") or [])
                        if leg.get("order_id")
                    ],
                    "detail": detail,
                }
            if previous_state == STAGE_UNKNOWN:
                # An attempted send whose outcome is unknown is NEVER re-sent.
                return {
                    "submitted": False,
                    "complete": False,
                    "reason": "stage_send_unknown",
                    "stage_digest": digest,
                    "option_run_id": str(run.strategy_run_id),
                    "orders": orders,
                    "leg_outcomes": list(previous.get("leg_outcomes") or []),
                    "detail": detail,
                }
            # Anything else - a REJECTED stage, or one the platform proved never
            # reached the order path - placed NOTHING, so a later pass may try
            # again under fresh evidence instead of treating it as submitted.
            detail = {**detail, "retry_after": previous_state}
        if self._place_orders is None:
            return {
                "submitted": False,
                "complete": False,
                "reason": "no_order_boundary",
                "stage_digest": digest,
                "orders": orders,
                "detail": detail,
            }
        idempotency_key = f"staged-structure-exit:{run.strategy_run_id}:{digest}:a{attempt}"
        # 4. DURABLE PRE-SEND CLAIM: the digest, the deterministic client order
        #    references and the EXACT per-leg quantities are written under the run
        #    store's row lock BEFORE the broker call, so a crash can never turn the
        #    same stage into a second (or larger) order.
        claim = {
            "stage_digest": digest,
            "attempt": int(attempt),
            "state": STAGE_SENDING,
            "idempotency_key": idempotency_key,
            "legs": legs,
            "worker_run_id": worker_run_id,
            "account_id": account_id,
            "structure_digest": structure_digest,
            "owner": self.owner,
            "lease_until": (
                self._clock() + timedelta(seconds=self.lease_seconds)
            ).isoformat(),
            "claim_digest": _digest(legs),
            "recorded_at": self._clock().isoformat(),
            "source": "hosted_option_protection",
        }
        try:
            # ATOMIC: one sender owns the stage. The run's row lock makes the
            # "is another claim live?" check and the insert one decision, so two
            # submitters that derived the same stage cannot both reach the broker.
            won, holder = self._runs().claim_stage(
                str(run.strategy_run_id), claim, now=self._clock()
            )
            if not won:
                return {
                    "submitted": False,
                    "complete": False,
                    "reason": "stage_claimed_by_other",
                    "stage_digest": str((holder or {}).get("stage_digest") or digest),
                    "holder": {
                        "owner": str((holder or {}).get("owner") or ""),
                        "lease_until": str((holder or {}).get("lease_until") or ""),
                        "attempt": int((holder or {}).get("attempt") or 1),
                    },
                    "orders": orders,
                    "detail": detail,
                }
        except Exception as exc:  # noqa: BLE001 - no claim, no send
            return {
                "submitted": False,
                "complete": False,
                "reason": "stage_claim_failed",
                "error": str(exc),
                "stage_digest": digest,
                "orders": orders,
                "detail": detail,
            }
        try:
            result = self._place_orders(
                account_id=account_id,
                worker_run_id=worker_run_id,
                option_run_id=str(run.strategy_run_id),
                structure_digest=structure_digest,
                legs=[dict(leg) for leg in legs],
                idempotency_key=idempotency_key,
            )
            if hasattr(result, "__await__"):
                result = await result
        except Exception as exc:  # noqa: BLE001 - an unknown send stays unresolved
            self._record_stage(
                str(run.strategy_run_id),
                {
                    **claim,
                    "state": STAGE_UNKNOWN,
                    "reason": "ORDER_BOUNDARY_FAILED",
                    "error": str(exc),
                    "resolved_at": self._clock().isoformat(),
                },
            )
            return {
                "submitted": False,
                "complete": False,
                "reason": "stage_send_unknown",
                "error": str(exc),
                "stage_digest": digest,
                "orders": orders,
                "detail": detail,
            }
        payload = dict(result or {}) if isinstance(result, Mapping) else {}
        outcomes = self._leg_outcomes(legs=legs, payload=payload)
        accepted = sum(1 for row in outcomes if row.get("order_id"))
        if accepted == len(legs):
            state = STAGE_SUBMITTED
        elif accepted:
            state = STAGE_PARTIAL
        else:
            # NOT ONE leg came back with an order reference. That is NOT proof the
            # broker placed nothing: the immediate response is idless both for an
            # explicit refusal and for an ambiguous failure AFTER acceptance (the
            # order path collapses them). So the stage is resolved to UNKNOWN - it
            # keeps owning the send - and the next pass reconciles it from the
            # platform's own durable pre-send records instead of re-sending on a
            # guess. A rejected basket must never be assumed from an idless answer.
            state = STAGE_UNKNOWN
        blockers = [
            {
                "tradingsymbol": row.get("tradingsymbol"),
                "quantity": row.get("quantity"),
                "blocker": "ORDER_NOT_ACCEPTED",
                "error": row.get("error"),
            }
            for row in outcomes
            if not row.get("order_id")
        ]
        record = {
            **claim,
            "state": state,
            "legs": [
                {**leg, "order_id": outcome.get("order_id"), "error": outcome.get("error")}
                for leg, outcome in zip(legs, outcomes)
            ],
            "reason": "submitted" if state != STAGE_UNKNOWN else "ORDER_BOUNDARY_UNRESOLVED",
            "blockers": blockers,
            "ingested_fills": int(ingested.get("recorded") or 0),
            "resolved_at": self._clock().isoformat(),
        }
        try:
            self._record_stage(str(run.strategy_run_id), record)
        except Exception as exc:  # noqa: BLE001 - the order exists; the record must say so
            return {
                "submitted": True,
                "complete": False,
                "reason": "stage_record_failed",
                "error": str(exc),
                "stage_digest": digest,
                "orders": orders,
                "detail": detail,
            }
        if state == STAGE_UNKNOWN:
            # No leg was acknowledged, so NOTHING is claimed as submitted and the
            # stage stays in flight. The blockers name every leg without a reference.
            return {
                "submitted": False,
                "complete": False,
                "reason": "stage_send_unknown",
                "stage_digest": digest,
                "option_run_id": str(run.strategy_run_id),
                "orders": orders,
                "leg_outcomes": outcomes,
                "blockers": blockers,
                "detail": detail,
            }
        return {
            "submitted": True,
            "complete": flat,
            "reason": "submitted",
            "stage_digest": digest,
            "option_run_id": str(run.strategy_run_id),
            "orders": orders,
            "order_ids": [str(row["order_id"]) for row in outcomes if row.get("order_id")],
            "leg_outcomes": outcomes,
            "blockers": blockers,
            "detail": detail,
            "withheld_hedges": list(detail.get("withheld_hedges") or []),
        }

    @staticmethod
    def _leg_outcomes(
        *, legs: Sequence[Mapping[str, Any]], payload: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        """Per-leg acceptance, aligned to the claim's legs by index.

        The broker boundary answers per leg; a leg it did not answer for is NOT
        silently dropped, because a structure that quietly loses an exit leg is a
        structure left half-hedged. It is recorded with no order id and a named
        blocker, and the outstanding-quantity netting lets a later stage pick it up.

        An idless answer is only ever an ABSENCE of an acceptance, never a proof of
        refusal: the boundary cannot tell an explicit rejection from a failure that
        happened AFTER the broker took the order. Callers must treat a fully idless
        answer as UNKNOWN, not as "nothing was placed".
        """
        answers = list(payload.get("legs") or [])
        by_index: Dict[int, Dict[str, Any]] = {}
        for position, answer in enumerate(answers):
            answer = dict(answer or {}) if isinstance(answer, Mapping) else {}
            index = int(answer.get("index", position))
            by_index[index] = answer
        ids = [str(value) for value in (payload.get("order_ids") or []) if value]
        outcomes: List[Dict[str, Any]] = []
        for index, leg in enumerate(legs):
            answer = by_index.get(index, {})
            order_id = str(answer.get("order_id") or "")
            if not order_id and index < len(ids):
                order_id = ids[index]
            outcomes.append(
                {
                    "index": index,
                    "client_order_ref": str(leg.get("client_order_ref") or ""),
                    "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                    "transaction_type": str(leg.get("transaction_type") or ""),
                    "quantity": int(leg.get("quantity") or 0),
                    "order_id": order_id or None,
                    "error": (
                        None
                        if order_id
                        else str(answer.get("error") or "no order reference returned")
                    ),
                }
            )
        return outcomes
