"""Lane-owned durable side effects for a CONFIRMED hosted live fill.

The parent protocol is lane-agnostic, and so is ingestion: it proves that a leg
filled from the broker's own trade records and attributes the book. What it must
NOT do is decide what a fill MEANS in a lane's own domain - that would put roll
semantics and option-run semantics inside the generic consumer.

This module is the seam. When a hosted live leg is confirmed filled, exactly one
lane-owned effect is recorded, from the parent's OWN frozen step specification:

* **futures/rolls.** A confirmed ``open_new`` fill is recorded against the roll as
  a REPLACEMENT execution (``RollStateMachine.record_replacement_fill``), and the
  roll is then asked to re-decide its proof (``prove_filled``). The roll's own
  proof rule - never the strategy's aggregate book - is what releases the
  old-contract close, so a pre-existing holding or an unrelated purchase can never
  stand in for the replacement. A partial fill stalls the roll; it never releases.

* **option structures.** A confirmed fill is recorded against the durable option
  run as a TRADE of the run's own leg id, so the run's own open quantities - the
  ONLY thing an exit is sized from - stay true once fills land asynchronously.

Both effects are idempotent by ``(plan, step, cumulative-filled quantity)``, because
ingestion retries a stage until its effect is re-readable: a replay of the same
confirmed total records nothing new.

Nothing here places, changes or cancels an order, and nothing here renews an
expired attempt's authority: it translates evidence the platform ALREADY proved
into the lane's own durable vocabulary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from backend.app.database import SessionLocal


#: Option-run statuses a CONFIRMED entry fill may complete from. ``partial_entry``
#: is deliberately absent: the lifecycle has no path from it back to ``entered``,
#: so taking it would strand a half-entered structure.
ENTRY_IN_FLIGHT_STATUSES = ("created", "entering")

#: Option-run statuses a fully-closed structure may settle from.
EXIT_IN_FLIGHT_STATUSES = ("exiting", "partial_exit")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


class LiveLaneLedger:
    """Record the OWN-domain consequence of one confirmed live leg fill."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        sequence: Any = None,
        roll_machine: Any = None,
        option_runs: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.session_factory = session_factory or SessionLocal
        if sequence is None:
            from backend.strategies.live_sequence import LivePlanSequence

            sequence = LivePlanSequence(session_factory=self.session_factory)
        self.sequence = sequence
        self._roll_machine = roll_machine
        self._option_runs = option_runs
        self._clock = clock or _utcnow

    # -- collaborators ------------------------------------------------------

    def _rolls(self) -> Any:
        if self._roll_machine is None:
            from backend.strategies.rolls import RollStateMachine

            self._roll_machine = RollStateMachine(session_factory=self.session_factory)
        return self._roll_machine

    def _runs(self) -> Any:
        if self._option_runs is None:
            from backend.options.execution.durable_store import DurableOptionRunStore

            self._option_runs = DurableOptionRunStore(session_factory=self.session_factory)
        return self._option_runs

    @staticmethod
    def _spec_for(parent: Mapping[str, Any], step_no: int) -> Optional[Any]:
        for spec in parent.get("step_spec") or []:
            if int(getattr(spec, "step_no", 0) or 0) == int(step_no):
                return spec
        return None

    # -- entry point --------------------------------------------------------

    def record_confirmed_fill(
        self, *, plan_id: str, step_no: int, filled_total: int
    ) -> Optional[Dict[str, Any]]:
        """Translate one confirmed fill into its lane's own durable record.

        Returns a small view of what was recorded (or ``None`` when this plan has
        no parent, or the lane has no own-domain ledger). A failure is RAISED: the
        caller keeps the step ``finalizing`` and retries, because a confirmed fill
        whose own-domain record is missing would leave the lane sizing against a
        book it no longer has.
        """
        parent = self.sequence.get_execution(str(plan_id))
        if parent is None:
            return None
        spec = self._spec_for(parent, int(step_no))
        if spec is None:
            return None
        lane = str(parent.get("lane") or "")
        if lane == "futures_roll":
            return self._record_roll_replacement(
                parent=parent, spec=spec, filled_total=int(filled_total)
            )
        if lane == "option_structure":
            return self._record_option_trade(
                parent=parent, spec=spec, filled_total=int(filled_total)
            )
        return None

    # -- futures/rolls ------------------------------------------------------

    def _record_roll_replacement(
        self, *, parent: Mapping[str, Any], spec: Any, filled_total: int
    ) -> Optional[Dict[str, Any]]:
        detail = dict(getattr(spec, "detail", {}) or {})
        roll_detail = _as_dict(detail.get("roll"))
        role = str(roll_detail.get("role") or "")
        if role != "open_new":
            # A plain futures plan (no roll) and a roll's CLOSE half have no
            # replacement to record: only the acquisition proves a roll.
            return None
        plan_id = str(parent.get("plan_id") or "")
        step_no = int(getattr(spec, "step_no", 0) or 0)
        machine = self._rolls()
        roll_id = roll_detail.get("roll_id")
        if not roll_id:
            # The acquisition plan can be frozen BEFORE its roll is opened (the
            # roll binds the plan), so the frozen block often carries no id. The
            # roll that NAMES this plan is the durable binding - the same lookup
            # ``_roll_preconditions`` uses - never a guess.
            roll = machine.for_plan(
                strategy_id=str(parent.get("strategy_id") or ""), plan_id=plan_id
            )
            if roll is None:
                raise LiveRefusalLike(
                    "LIVE_ROLL_UNKNOWN",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "roll_role": role,
                        "message": (
                            "a confirmed replacement fill cannot be attributed to a roll; "
                            "the step keeps finalizing rather than losing the proof"
                        ),
                    },
                )
            roll_id = roll["roll_id"]
        recorded = 0
        for entry in machine.replacement_fills(str(roll_id)):
            if str(entry.get("plan_id") or "") != plan_id:
                continue
            recorded += int(entry.get("quantity") or 0)
        increment = int(filled_total) - recorded
        if increment <= 0:
            return {
                "lane": "futures_roll",
                "roll_id": str(roll_id),
                "recorded_quantity": recorded,
                "increment": 0,
                "idempotent": True,
            }
        machine.record_replacement_fill(
            str(roll_id),
            # The idempotency key is the CUMULATIVE confirmed total of this step,
            # so a replayed stage records the same increment exactly once, while a
            # genuinely larger fill records only its own additional quantity.
            paper_order_id=f"live:{plan_id}:{step_no}:{int(filled_total)}",
            quantity=int(increment),
            instrument_id=str(getattr(spec, "instrument_id", "") or ""),
            plan_id=plan_id,
            actor_id="live-lane-ledger",
        )
        roll = machine.prove_filled(str(roll_id))
        return {
            "lane": "futures_roll",
            "roll_id": str(roll_id),
            "recorded_quantity": int(roll.get("proven_filled_quantity") or 0),
            "required_replacement_quantity": int(
                roll.get("required_replacement_quantity") or 0
            ),
            "roll_state": str(roll.get("state") or ""),
            "increment": int(increment),
            "idempotent": False,
        }

    # -- option structures --------------------------------------------------

    def _record_option_trade(
        self, *, parent: Mapping[str, Any], spec: Any, filled_total: int
    ) -> Optional[Dict[str, Any]]:
        detail = dict(getattr(spec, "detail", {}) or {})
        option = _as_dict(detail.get("option"))
        run_id = str(option.get("option_run_id") or "")
        leg_id = str(option.get("run_leg_id") or "")
        if not run_id or not leg_id:
            raise LiveRefusalLike(
                "LIVE_OPTION_RUN_LEG_UNRESOLVED",
                {
                    "plan_id": str(parent.get("plan_id") or ""),
                    "step_no": int(getattr(spec, "step_no", 0) or 0),
                    "message": "the frozen option step names no durable run leg",
                },
            )
        plan_id = str(parent.get("plan_id") or "")
        step_no = int(getattr(spec, "step_no", 0) or 0)
        store = self._runs()
        run = store.get_run(run_id)
        dedupe_key = f"{plan_id}:{step_no}:cumulative:{int(filled_total)}"
        recorded = sum(
            int(trade.get("quantity") or 0)
            for trade in (getattr(run, "trades", []) or [])
            if str((trade or {}).get("plan_id") or "") == plan_id
            and int((trade or {}).get("step_no") or 0) == step_no
        )
        increment = int(filled_total) - recorded
        if increment > 0:
            appended, _skipped = store.record_trades_once(
                run_id,
                [
                    {
                        "leg_id": leg_id,
                        "tradingsymbol": str(getattr(spec, "tradingsymbol", "") or ""),
                        "transaction_type": str(getattr(spec, "side", "") or "").upper(),
                        "quantity": int(increment),
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "filled_total": int(filled_total),
                        "dedupe_key": dedupe_key,
                        "source": "hosted_live_ingestion",
                        "recorded_at": self._clock().isoformat(),
                    }
                ],
                dedupe_key="dedupe_key",
            )
            run = store.get_run(run_id)
            if not appended:
                increment = 0
        status = self._advance_option_run(run)
        status = self._advance_option_adjust(parent, run, status=status)
        return {
            "lane": "option_structure",
            "option_run_id": run_id,
            "run_leg_id": leg_id,
            "recorded_quantity": int(recorded + max(increment, 0)),
            "increment": int(max(increment, 0)),
            "option_run_status": status,
            "idempotent": increment <= 0,
        }

    def _advance_option_adjust(
        self, parent: Mapping[str, Any], run: Any, *, status: str
    ) -> str:
        """Complete an adjust only from the run's own ledger proof.

        The generic helper leaves ``adjusting`` alone because it has no phase
        vocabulary. This is the option-specific completion: every frozen target
        quantity must equal the signed run trade, old removed legs must be flat,
        and only then does the generation bump and the desired protection freeze.
        The check is idempotent; replayed ingestion sees the newer status and
        returns without another bump.
        """
        if status != "adjusting":
            return status
        expected_by_leg: Dict[str, int] = {}
        desired_specs = []
        for spec in parent.get("step_spec") or []:
            detail = dict(getattr(spec, "detail", {}) or {})
            option = dict(detail.get("option") or {})
            if str(option.get("phase") or "") != "adjust":
                continue
            leg_id = str(option.get("run_leg_id") or "")
            target = int(detail.get("sizing", {}).get("target") or 0)
            expected_by_leg[leg_id] = target
            desired_specs.append(spec)
        if not desired_specs:
            return status

        open_by_leg: Dict[str, int] = {}
        for trade in getattr(run, "trades", []) or []:
            leg_id = str((trade or {}).get("leg_id") or "")
            side = str((trade or {}).get("transaction_type") or "").upper()
            open_by_leg[leg_id] = open_by_leg.get(leg_id, 0) + (
                int((trade or {}).get("quantity") or 0)
                if side == "BUY"
                else -int((trade or {}).get("quantity") or 0)
            )
        if any(open_by_leg.get(leg_id, 0) != target for leg_id, target in expected_by_leg.items()):
            return status

        old_legs = [dict(leg) for leg in getattr(run, "legs", []) or []]
        metadata = dict(getattr(run, "metadata", None) or {})
        generation = int(metadata.get("structure_generation") or 1)
        history = list(metadata.get("structure_generation_history") or [])
        history.append(
            {
                "generation": generation,
                "structure_digest": metadata.get("structure_digest") or "",
                "legs": old_legs,
            }
        )
        metadata["structure_generation"] = generation + 1
        metadata["structure_generation_history"] = history[-10:]
        desired_legs = []
        for spec in desired_specs:
            detail = dict(getattr(spec, "detail", {}) or {})
            option = dict(detail.get("option") or {})
            leg_id = str(option.get("run_leg_id") or "")
            target = int(detail.get("sizing", {}).get("target") or 0)
            if target == 0:
                continue
            existing = next(
                (dict(leg) for leg in old_legs if str(leg.get("leg_id") or "") == leg_id),
                None,
            )
            leg = existing or {
                "leg_id": leg_id,
                "tradingsymbol": str(getattr(spec, "tradingsymbol") or ""),
                "exchange": str(getattr(spec, "exchange") or ""),
                "product": str(getattr(spec, "product") or ""),
                "lot_size": int(getattr(spec, "lot_size") or 1),
                "metadata": {
                    "instrument_id": str(getattr(spec, "instrument_id") or ""),
                    "expiry": str(option.get("expiry") or ""),
                    "expiry_key": str(option.get("expiry") or ""),
                    "structure_id": str(option.get("structure_id") or ""),
                },
            }
            leg.update(
                {
                    "quantity": abs(target),
                    "lots": abs(target) // int(leg.get("lot_size") or 1),
                    "transaction_type": "BUY" if target > 0 else "SELL",
                }
            )
            desired_legs.append(leg)
        run.metadata = metadata
        if desired_legs:
            run.legs = desired_legs
        protection = next(
            (
                dict(option.get("desired_protection") or {})
                for spec in desired_specs
                for option in [dict((getattr(spec, "detail", {}) or {}).get("option") or {})]
                if option.get("desired_protection")
            ),
            None,
        )
        if protection is not None:
            run.protection = protection
        from backend.options.execution.lifecycle import mark_adjusted
        from backend.options.execution.plan_binding import read_option_protection_owner
        from backend.options.protection.ownership import (
            option_protection_policy_snapshot,
        )

        # B2.4 S4: a NEW generation freezes the adjust's protection policy on the
        # owner row, in the SAME transaction as the run's completion write and
        # CASed on the epoch observed here. A row that moved under us, a
        # superseded owner, or an unreadable row simply leaves the run write
        # unperformed - the owner row is never invented by a completion.
        owner_policy = None
        owner_run_id = None
        owner_observed_epoch = 0
        with self.session_factory() as session:
            owner_row, _owner_read_error = read_option_protection_owner(
                str(getattr(run, "strategy_run_id", "") or ""), session=session
            )
        if owner_row is not None and str(owner_row.get("state") or "") == "active":
            owner_policy = option_protection_policy_snapshot(protection or {})
            owner_run_id = str(owner_row.get("owner_run_id") or "") or None
            owner_observed_epoch = int(owner_row.get("owner_epoch") or 0)

        try:
            next_run = mark_adjusted(
                run, completed_legs=[leg_id for leg_id, target in expected_by_leg.items() if target]
            )
        except ValueError:
            return status
        if self._runs().save_run_if_status(
            next_run,
            allowed_from=("adjusting",),
            owner_policy=owner_policy,
            owner_run_id=owner_run_id,
            owner_observed_epoch=owner_observed_epoch,
        ):
            return str(next_run.status)
        return status

    def _advance_option_run(self, run: Any) -> str:
        """Derive the run's next status from its OWN recorded trades.

        Conservative on purpose. A run whose entry is only partially filled stays
        ``entering`` with the partial trades recorded (the state machine has no
        path back from ``partial_entry`` to ``entered``, so taking that status
        would strand a structure half-entered forever). The only transitions taken
        are the ones the trades PROVE: every entry leg at its intended quantity,
        or every leg flat at the end of an exit.
        """
        from backend.options.execution.lifecycle import mark_closed, mark_entered

        status = str(getattr(run, "status", "") or "")
        open_by_leg: Dict[str, int] = {}
        for trade in getattr(run, "trades", []) or []:
            leg_id = str((trade or {}).get("leg_id") or "")
            quantity = int((trade or {}).get("quantity") or 0)
            side = str((trade or {}).get("transaction_type") or "").upper()
            open_by_leg[leg_id] = open_by_leg.get(leg_id, 0) + (
                quantity if side == "BUY" else -quantity
            )
        legs = list(getattr(run, "legs", []) or [])
        if status in ENTRY_IN_FLIGHT_STATUSES:
            complete = bool(legs)
            for leg in legs:
                leg_id = str((leg or {}).get("leg_id") or "")
                intended = int((leg or {}).get("quantity") or 0)
                side = str((leg or {}).get("transaction_type") or "").upper()
                want = intended if side == "BUY" else -intended
                if open_by_leg.get(leg_id, 0) != want:
                    complete = False
                    break
            if complete:
                store = self._runs()
                try:
                    next_run = mark_entered(
                        run,
                        completed_legs=[
                            str((leg or {}).get("leg_id") or "") for leg in legs
                        ],
                    )
                except ValueError:
                    # The state machine does not allow this transition from here;
                    # the trades are recorded and the status stays as it was.
                    return status
                if store.save_run_if_status(next_run, allowed_from=(status,)):
                    return str(next_run.status)
            return status
        if status in EXIT_IN_FLIGHT_STATUSES:
            if all(open_by_leg.get(str((leg or {}).get("leg_id") or ""), 0) == 0 for leg in legs):
                store = self._runs()
                try:
                    next_run = mark_closed(run)
                except ValueError:
                    return status
                if store.save_run_if_status(next_run, allowed_from=(status,)):
                    return str(next_run.status)
            return status
        return status


class LiveRefusalLike(RuntimeError):
    """A named refusal that the live consumer treats as a retryable stage block."""

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(str(reason_code))
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})
