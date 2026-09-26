from __future__ import annotations

import hashlib
import json
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from backend.api.services.protection import evaluate_backend_protection, validate_backend_protection_payload
from backend.broker_api.core.redis_events import publish_event
from backend.broker_api.orders.autoslice import should_autoslice
from backend.options.protection.live_metrics import (
    derive_live_option_protection_metrics,
    open_option_positions,
)
from backend.options.protection.runtime import (
    evaluate_option_protection_state,
    normalize_protection_config as normalize_option_protection_config,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _heartbeat_age(last_heartbeat_at: Any, now: datetime) -> Optional[int]:
    if last_heartbeat_at is None:
        return None
    if isinstance(last_heartbeat_at, str):
        parsed = datetime.fromisoformat(last_heartbeat_at.replace("Z", "+00:00"))
    else:
        parsed = last_heartbeat_at
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))


class WorkerProtectionRuntime:
    def __init__(
        self,
        repo: Any,
        pnl_loader: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
       exit_submitter: Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]],
        now_fn: Callable[[], datetime] = _utcnow,
        squareoff_schedule: Optional[Dict[str, Any]] = None,
        structure_exit_submitter: Optional[
            Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]
        ] = None,
        owner_store: Any = None,
        option_run_store: Any = None,
        index_tick_loader: Optional[Callable[[int], Awaitable[Dict[str, Any] | None]]] = None,
        option_tick_loader: Optional[Callable[[int], Awaitable[Dict[str, Any] | None]]] = None,
        index_token_resolver: Optional[Callable[[str, str], Awaitable[int | None]]] = None,
        option_token_resolver: Optional[Callable[[str, str], Awaitable[int | None]]] = None,
    ) -> None:
        self.repo = repo
        self.pnl_loader = pnl_loader
        self.exit_submitter = exit_submitter
        self.now_fn = now_fn
        self.squareoff_schedule = squareoff_schedule or {}
        #: The STRUCTURE-aware exit. A hedged structure may not be liquidated as a
        #: whole book: its short's liability is bounded by its long, so the
        #: platform derives bounded stages from the run's own evidence instead
        #: (shorts first, hedges only against PROVEN short closure). ``None`` keeps
        #: the generic path, which is what every non-structure run uses.
        self.structure_exit_submitter = structure_exit_submitter
        #: Where a protective action is MIRRORED, so the owner row and this loop
        #: cannot disagree about an exit that is in flight (B2.4 S2a). Injected in
        #: tests; the production default is the durable owner store.
        self.owner_store = owner_store
        #: Durable option-run evidence and market reads. They are lazy in
        #: production so tests and non-option deployments never construct the
        #: options machinery merely to evaluate a flat equity run.
        self.option_run_store = option_run_store
        self.index_tick_loader = index_tick_loader
        self.option_tick_loader = option_tick_loader
        self.index_token_resolver = index_token_resolver
        self.option_token_resolver = option_token_resolver

    async def evaluate_once(self) -> Dict[str, int]:
        # OPTION STRUCTURES are enumerated by OWNER ROW, not by the worker run's
        # status: a structure whose worker run has closed is still protected until
        # the option run itself reaches a terminal status and releases the row.
        owner_runs = await self._protection_owner_runs()
        owned_worker_run_ids = {
            str(run.get("strategy_run_id") or "")
            for run in owner_runs
            if str(run.get("strategy_run_id") or "")
        }
        runs = await self.repo.list_protection_enabled_runs()
        pending: list[Dict[str, Any]] = []
        for run in owner_runs:
            # The owner row survives the worker run's STATUS; it does not turn
            # protection on. A run whose own protection config is off has no rule
            # to fire and no structure identity to exit, so evaluating it would
            # only write state every pass. Those runs stay on exactly the path
            # they had before this slice.
            if not self._protection_enabled(run) and not self._has_option_metric_rules(run):
                continue
            pending.append(dict(run))
        for run in list(runs or []):
            # ONE evaluation per structure: the OWNER ROW WINS for a worker run
            # that has one, so the generic per-run list cannot add a second pass
            # that would claim - and could submit - the same exit twice.
            if str(run.get("strategy_run_id") or "") in owned_worker_run_ids:
                continue
            pending.append(dict(run))
        evaluated = 0
        triggered = 0
        errors = 0
        for run in pending:
            evaluated += 1
            try:
                if await self._evaluate_run(dict(run)):
                    triggered += 1
            except Exception as exc:
                errors += 1
                await self._persist_run_error(run, exc)
        return {"evaluated": evaluated, "triggered": triggered, "errors": errors}

    async def _protection_owner_runs(self) -> list[Dict[str, Any]]:
        """The runs an ACTIVE protection owner row names (B2.4 S2a).

        A repository that cannot enumerate owners - an older test double, or a
        deployment with no option structures - contributes nothing, which is what
        keeps the generic per-run loop byte-for-byte the behaviour it had.
        """

        lister = getattr(self.repo, "list_protection_owners", None)
        if lister is None:
            return []
        rows = await lister()
        return [dict(row) for row in (rows or [])]

    @staticmethod
    def _protection_enabled(run: Dict[str, Any]) -> bool:
        """Whether the run's own protection config is ON.

        An unreadable config is evaluated rather than skipped, so its failure is
        recorded like any other instead of disappearing.
        """

        try:
            config = validate_backend_protection_payload(
                (run.get("runtime_state") or {}).get("backend_protection")
            )
        except Exception:  # noqa: BLE001 - let the evaluation report the error
            return True
        return bool(config.enabled)

    @staticmethod
    def _has_option_metric_rules(run: Dict[str, Any]) -> bool:
        owner = run.get("protection_owner")
        if not isinstance(owner, dict):
            return False
        policies: list[Any] = []
        policy = owner.get("policy")
        if isinstance(policy, dict):
            nested = policy.get("protection_policy")
            if isinstance(nested, dict):
                policies.append(nested)
            policies.append(policy)
        option_metrics = {
            "index_ltp",
            "combined_premium",
            "combined_premium_change_pct",
            "strategy_mtm",
            "open_quantity",
        }
        for source in policies:
            rules = source.get("rules")
            if isinstance(rules, list) and any(
                isinstance(rule, dict) and str(rule.get("metric") or "") in option_metrics
                for rule in rules
            ):
                return True
        return False

    def _protection_owner_store(self) -> Any:
        if self.owner_store is None:
            from backend.options.protection.ownership import (
                OptionProtectionOwnerStore,
                get_option_protection_owner_store,
            )

            # The mirror travels the SAME database as the loop's own reads: the
            # owner row and the worker run's protection state are two records of
            # one decision, so they must not be written through two connections
            # that only happen to agree in production.
            session_factory = getattr(self.repo, "session_factory", None)
            self.owner_store = (
                OptionProtectionOwnerStore(session_factory=session_factory)
                if session_factory is not None
                else get_option_protection_owner_store()
            )
        return self.owner_store

    @staticmethod
    def _protection_owner_context(run: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The owner row this evaluation is acting for, when there is one.

        Only owner-row-driven evaluations carry it. A structure reached through a
        worker run with no owner row has no row to mirror onto, and the run's own
        ``orders`` remain its only evidence.
        """

        owner = run.get("protection_owner")
        if not isinstance(owner, dict):
            return None
        if not str(owner.get("option_run_id") or ""):
            return None
        if owner.get("owner_epoch") is None:
            return None
        return owner

    @staticmethod
    def _owner_action_state(structure_exit: Any) -> str:
        """The owner row's action state for one stage verdict (design section 3)."""

        exit_result = structure_exit if isinstance(structure_exit, dict) else {}
        if str(exit_result.get("reason") or "") == "stage_send_unknown":
            # The platform committed to a stage and cannot say whether the broker
            # took it: UNRESOLVED work, never a settled outcome.
            return "unresolved"
        if bool(exit_result.get("submitted")):
            return "none" if bool(exit_result.get("complete", True)) else "staging"
        # Not submitted: the exit claim this pass took is still held, so the row
        # stays claimed and the next pass continues the SAME staged exit.
        return "claimed"

    def _mirror_protection_owner_action(
        self,
        owner: Optional[Dict[str, Any]],
        action_state: str,
        *,
        stage_digest: Any = None,
    ) -> None:
        """Mirror a protective action onto the owner row.

        The run's own ``orders`` stay the EVIDENCE; the row is where the action
        state lives, so the gates and this loop read one answer. Bookkeeping never
        stops a risk-reducing exit: a refusal - this caller is no longer the owner
        at that epoch - leaves the existing row untouched rather than turning a
        submitted exit into a failed evaluation.
        """

        if owner is None:
            return
        try:
            self._protection_owner_store().record_action(
                str(owner.get("option_run_id") or ""),
                str(action_state),
                None if stage_digest is None else str(stage_digest),
                int(owner.get("owner_epoch") or 0),
            )
        except Exception:  # noqa: BLE001 - mirroring is bookkeeping, never the exit
            return

    async def _evaluate_run(self, run: Dict[str, Any]) -> bool:
        runtime_state = dict(run.get("runtime_state") or {})
        config = validate_backend_protection_payload(runtime_state.get("backend_protection"), live=str(run.get("execution_mode") or "").lower() == "live")
        state = dict(runtime_state.get("backend_protection_state") or {})
        now = self.now_fn()
        if self._has_recent_exit_claim(state, now):
            return False
        owner = self._protection_owner_context(run)
        if owner is not None and self._has_option_metric_rules(run):
            option_result = await self._evaluate_option_owner_run(
                run, runtime_state, state, owner, now=now
            )
            if option_result is not None:
                return option_result
        pnl = await self.pnl_loader(run)
        positions = list(pnl.get("legs") or pnl.get("positions") or [])
        next_state = evaluate_backend_protection(
            config,
            state=state,
            positions=positions,
            heartbeat_age_sec=_heartbeat_age(run.get("last_heartbeat_at"), now),
            now=now,
            squareoff_schedule=self.squareoff_schedule,
        )
        did_trigger = bool(next_state.get("status") == "triggered" and not state.get("exit_submitted"))
        if did_trigger:
            owner = self._protection_owner_context(run)
            structure = self._structure_identity(config)
            if structure is None and owner is not None:
                policy = owner.get("policy")
                digest = str((policy or {}).get("structure_digest") or "")
                if digest:
                    structure = {"structure_digest": digest}
            if owner is not None and structure is None:
                # Resolve before the durable claim: an ACTIVE option owner with
                # no structure identity is refused by name, never sent to the
                # generic whole-book submitter and never left claim-only.
                raise RuntimeError(
                    "OPTION_PROTECTION_STRUCTURE_UNKNOWN: active owner policy "
                    "does not name a structure"
                )
            claim_id = str(uuid.uuid4())
            claimed_state = {
                **next_state,
                "exit_claim_id": claim_id,
                "exit_claimed_at": now.isoformat(),
                "exit_idempotency_key": self._idempotency_key(run, next_state),
            }
            claimed_result = await self._persist_state(
                run,
                runtime_state,
                state,
                claimed_state,
                expected_generation=state.get("generation"),
                expected_triggered_rule=state.get("triggered_rule") or "",
            )
            if claimed_result is None:
                return False
            await self._publish_timeline_rows(claimed_result.get("timeline_events") or [])

            if structure is not None:
                # Structure-aware: the exit is derived SERVER-SIDE from the
                # durable option run's own legs and own confirmed fills, staged
                # short-first, and submitted through the platform's own
                # risk-reducing authority. An evaluator's recommended orders are
                # never trusted, and a whole-book liquidation is never used here.
                # The CLAIM is mirrored before the stage: the owner row is where
                # the action state lives, so a gate that reads it while the stage
                # is in flight must already see the claim, not "none".
                self._mirror_protection_owner_action(owner, "claimed")
                structure_exit = await self._submit_structure_exit(
                    run, claimed_state, structure, claim_id=claim_id
                )
                self._mirror_protection_owner_action(
                    owner,
                    self._owner_action_state(structure_exit),
                    stage_digest=(structure_exit or {}).get("stage_digest"),
                )
                structure_state = {
                    **claimed_state,
                    # "submitted" is not "done": when the staged protocol still
                    # owes a hedge release (it is waiting for the short's own
                    # confirmed fill), the run stays unprotected-but-in-sequence
                    # and the next evaluation continues the SAME staged exit.
                    "exit_submitted": bool(
                        structure_exit.get("submitted")
                        and structure_exit.get("complete", True)
                    ),
                    "structure_exit_complete": bool(structure_exit.get("complete", True)),
                    "exit_submission_status": (
                        "submitted" if structure_exit.get("submitted")
                        else str(structure_exit.get("reason") or "not_submitted")
                    ),
                    "structure_exit": structure_exit,
                }
                persisted_structure = await self._persist_state(
                    run,
                    runtime_state,
                    claimed_state,
                    structure_state,
                    expected_generation=structure_state.get("generation"),
                    expected_exit_claim_id=claim_id,
                )
                if persisted_structure is None:
                    persisted_structure = await self._persist_state(
                        run, runtime_state, claimed_state, structure_state
                    )
                if persisted_structure is not None:
                    await self._publish_timeline_rows(
                        persisted_structure.get("timeline_events") or []
                    )
                return bool(structure_exit.get("submitted"))

            try:
                exit_result = await self.exit_submitter(run, claimed_state)
            except Exception as exc:
                unknown_state = {
                    **claimed_state,
                    "status": "error",
                    "exit_submitted": True,
                    "exit_submission_status": "unknown",
                    "exit_error": str(exc),
                }
                persisted_unknown = await self._persist_state(
                    run,
                    runtime_state,
                    claimed_state,
                    unknown_state,
                    expected_generation=unknown_state.get("generation"),
                    expected_exit_claim_id=claim_id,
                )
                if persisted_unknown is None:
                    persisted_unknown = await self._persist_state(run, runtime_state, claimed_state, unknown_state)
                if persisted_unknown is not None:
                    await self._publish_timeline_rows(persisted_unknown.get("timeline_events") or [])
                return False
            if self._is_deferred_exit_result(exit_result):
                deferred_state = {
                    **claimed_state,
                    "exit_submitted": False,
                    "exit_submission_status": "deferred",
                    "exit_result": exit_result,
                }
                persisted_deferred = await self._persist_state(
                    run,
                    runtime_state,
                    claimed_state,
                    deferred_state,
                    expected_generation=deferred_state.get("generation"),
                    expected_exit_claim_id=claim_id,
                )
                if persisted_deferred is None:
                    persisted_deferred = await self._persist_state(run, runtime_state, claimed_state, deferred_state)
                if persisted_deferred is not None:
                    await self._publish_timeline_rows(persisted_deferred.get("timeline_events") or [])
                return False
            next_state = {
                **claimed_state,
                "exit_submitted": True,
                "exit_result": exit_result,
            }
            persisted_terminal = await self._persist_state(
                run,
                runtime_state,
                claimed_state,
                next_state,
                expected_generation=next_state.get("generation"),
                expected_exit_claim_id=claim_id,
            )
            if persisted_terminal is None:
                persisted_terminal = await self._persist_state(run, runtime_state, claimed_state, next_state)
            if persisted_terminal is not None:
                await self._publish_timeline_rows(persisted_terminal.get("timeline_events") or [])
            return True
        persisted_non_trigger = await self._persist_state(
            run,
            runtime_state,
            state,
            next_state,
            expected_generation=state.get("generation"),
            expected_triggered_rule=state.get("triggered_rule") or "",
            expected_exit_claim_id=state.get("exit_claim_id") or "",
        )
        if persisted_non_trigger is not None:
            await self._publish_timeline_rows(persisted_non_trigger.get("timeline_events") or [])
        return did_trigger

    async def _evaluate_option_owner_run(
        self,
        run: Dict[str, Any],
        runtime_state: Dict[str, Any],
        state: Dict[str, Any],
        owner: Dict[str, Any],
        *,
        now: datetime,
    ) -> Optional[bool]:
        """Evaluate fresh option metrics, then reuse the generic trigger path.

        ``None`` means the owner row has no option-metric rules and belongs to
        the generic evaluation it always used. A claimed option trigger never
        bypasses the staged-exit adapter or owner-row CAS.
        """

        option_run = await asyncio.to_thread(self._option_run_store().get_run, str(owner.get("option_run_id") or ""))
        protection = self._option_rule_config(option_run, owner)
        if protection is None:
            return None
        if state.get("exit_submitted"):
            return False
        metrics, metric_errors = await derive_live_option_protection_metrics(
            option_run,
            index_token_resolver=self._index_token_resolver(),
            index_tick_loader=self._index_tick_loader(),
            option_tick_loader=self._option_tick_loader(),
            option_token_resolver=self._option_token_resolver(),
            now=now,
        )
        persisted_metrics = {**metrics, "as_of": now.isoformat()}
        await asyncio.to_thread(
            self._option_run_store().update_protection_metrics,
            str(option_run.strategy_run_id),
            persisted_metrics,
            errors=metric_errors,
        )
        await self._record_option_metric_availability(
            run,
            runtime_state,
            state,
            metric_errors,
            now=now,
        )
        verdict = evaluate_option_protection_state(
            run=option_run,
            protection=protection,
            metric_snapshot=metrics,
        )
        try:
            has_open_quantity = float(metrics.get("open_quantity") or 0) > 0
        except (TypeError, ValueError):
            has_open_quantity = False
        if not has_open_quantity:
            return False
        if not verdict.get("triggered"):
            return False

        rule = dict(verdict.get("matched_rule") or {})
        next_state = {
            **state,
            "status": "triggered",
            "action": "exit_strategy",
            "triggered_rule": f"option:{rule.get('key') or rule.get('metric') or 'unknown'}",
            "details": {
                "option_rule": rule,
                "option_metrics": dict(verdict.get("metrics") or {}),
                "option_run_id": str(owner.get("option_run_id") or ""),
            },
            "generation": int(state.get("generation") or 0) + 1,
        }
        claim_id = str(uuid.uuid4())
        claimed_state = {
            **next_state,
            "exit_claim_id": claim_id,
            "exit_claimed_at": now.isoformat(),
            "exit_idempotency_key": self._idempotency_key(run, next_state),
        }
        claimed_result = await self._persist_state(
            run,
            runtime_state,
            state,
            claimed_state,
            expected_generation=state.get("generation"),
            expected_triggered_rule=state.get("triggered_rule") or "",
        )
        if claimed_result is None:
            return False
        await self._publish_timeline_rows(claimed_result.get("timeline_events") or [])

        structure = self._option_structure_identity(option_run, owner)
        if structure is None:
            raise RuntimeError(
                "OPTION_PROTECTION_STRUCTURE_UNKNOWN: active option-metric owner "
                "does not name a structure"
            )
        self._mirror_protection_owner_action(owner, "claimed")
        structure_exit = await self._submit_structure_exit(
            run, claimed_state, structure, claim_id=claim_id
        )
        self._mirror_protection_owner_action(
            owner,
            self._owner_action_state(structure_exit),
            stage_digest=(structure_exit or {}).get("stage_digest"),
        )
        structure_state = {
            **claimed_state,
            "exit_submitted": bool(
                structure_exit.get("submitted")
                and structure_exit.get("complete", True)
            ),
            "structure_exit_complete": bool(structure_exit.get("complete", True)),
            "exit_submission_status": (
                "submitted" if structure_exit.get("submitted")
                else str(structure_exit.get("reason") or "not_submitted")
            ),
            "structure_exit": structure_exit,
        }
        persisted_structure = await self._persist_state(
            run,
            runtime_state,
            claimed_state,
            structure_state,
            expected_generation=structure_state.get("generation"),
            expected_exit_claim_id=claim_id,
        )
        if persisted_structure is None:
            persisted_structure = await self._persist_state(
                run, runtime_state, claimed_state, structure_state
            )
        if persisted_structure is not None:
            await self._publish_timeline_rows(
                persisted_structure.get("timeline_events") or []
            )
        return bool(structure_exit.get("submitted"))

    async def _record_option_metric_availability(
        self,
        run: Dict[str, Any],
        runtime_state: Dict[str, Any],
        state: Dict[str, Any],
        errors: Dict[str, str],
        *,
        now: datetime,
    ) -> None:
        """Persist one metric-unavailable event per continuous outage.

        The first stale tick anchors the outage. Timeline visibility is delayed
        until ~30 seconds so a one-tick market-data gap does not create alert
        noise; the event is then written once and the marker prevents repeats.
        """

        unavailable = state.get("option_metric_unavailable")
        unavailable = dict(unavailable) if isinstance(unavailable, dict) else {}
        if not errors:
            if not unavailable:
                return
            next_state = {
                **state,
                "option_metric_unavailable": None,
            }
            await self._persist_state(
                run,
                runtime_state,
                state,
                next_state,
                expected_generation=state.get("generation"),
                expected_triggered_rule=state.get("triggered_rule") or "",
                expected_exit_claim_id=state.get("exit_claim_id") or "",
            )
            return

        missing_metrics = [
            {"metric": str(metric), "reason": str(reason)}
            for metric, reason in sorted(errors.items())
        ]
        first_seen_at = str(unavailable.get("first_seen_at") or "")
        if not first_seen_at:
            next_state = {
                **state,
                "option_metric_unavailable": {
                    "first_seen_at": now.isoformat(),
                    "metrics": missing_metrics,
                },
            }
            await self._persist_state(
                run,
                runtime_state,
                state,
                next_state,
                expected_generation=state.get("generation"),
                expected_triggered_rule=state.get("triggered_rule") or "",
                expected_exit_claim_id=state.get("exit_claim_id") or "",
            )
            return

        try:
            first_seen = datetime.fromisoformat(first_seen_at.replace("Z", "+00:00"))
        except ValueError:
            first_seen = now
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        elapsed = (now.astimezone(timezone.utc) - first_seen.astimezone(timezone.utc)).total_seconds()
        event_emitted_at = str(unavailable.get("event_emitted_at") or "")
        if elapsed < 30 or event_emitted_at:
            return

        strategy_run_id = str(run.get("strategy_run_id") or "")
        event = {
            "event_kind": "protection",
            "event_source": "backend_protection",
            "event_type": "OPTION_PROTECTION_METRIC_UNAVAILABLE",
            "related_resource_type": "strategy_run",
            "related_resource_id": strategy_run_id,
            "summary": "Option protection metric unavailable",
            "payload": {
                "emission_mode": "mutation_driven",
                "first_seen_at": first_seen_at,
                "missing_metrics": missing_metrics,
            },
        }
        next_state = {
            **state,
            "option_metric_unavailable": {
                **unavailable,
                "metrics": missing_metrics,
                "event_emitted_at": now.isoformat(),
            },
        }
        await self._persist_state(
            run,
            runtime_state,
            state,
            next_state,
            expected_generation=state.get("generation"),
            expected_triggered_rule=state.get("triggered_rule") or "",
            expected_exit_claim_id=state.get("exit_claim_id") or "",
            timeline_events=[event],
        )

    async def collect_option_metric_subscription_tokens(self) -> set[int]:
        """Tokens needed by every active owner run with option-metric rules.

        This is enumeration only: it reuses the same resolvers as metric reads
        and never contacts Redis or creates a market-runtime owner.
        """

        tokens: set[int] = set()
        for candidate in await self._protection_owner_runs():
            run = dict(candidate)
            owner = self._protection_owner_context(run)
            if owner is None or not self._has_option_metric_rules(run):
                continue
            try:
                option_run = await asyncio.to_thread(
                    self._option_run_store().get_run,
                    str(owner.get("option_run_id") or ""),
                )
                if self._option_rule_config(option_run, owner) is None:
                    continue
                protection = getattr(option_run, "protection", None)
                underlying = str(protection.get("underlying") or "") if isinstance(protection, dict) else ""
                if underlying:
                    index_token = await self._index_token_resolver()(underlying, "NSE")
                    if index_token is not None:
                        tokens.add(int(index_token))
                for position in open_option_positions(option_run):
                    try:
                        token = int(position.get("instrument_token"))
                    except (TypeError, ValueError):
                        token = await self._option_token_resolver()(
                            str(position.get("exchange") or "NFO"),
                            str(position.get("symbol") or ""),
                        )
                    if token is not None:
                        tokens.add(int(token))
            except Exception:  # noqa: BLE001 - one unreadable run cannot starve others
                continue
        return tokens

    @staticmethod
    def _option_rule_config(option_run: Any, owner: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize owner policy rules first, with the run's own rules as fallback."""

        sources: list[Any] = []
        policy = owner.get("policy")
        if isinstance(policy, dict):
            nested = policy.get("protection_policy")
            if isinstance(nested, dict):
                sources.append(nested)
            sources.append(policy)
        if getattr(option_run, "protection", None):
            sources.append(option_run.protection)

        rules: list[dict[str, Any]] = []
        precedence: list[str] = []
        option_metrics = {"index_ltp", "combined_premium", "combined_premium_change_pct", "strategy_mtm", "open_quantity"}
        for source in sources:
            raw_rules = source.get("rules") if isinstance(source, dict) else None
            if not isinstance(raw_rules, list):
                continue
            relevant = [
                rule
                for rule in raw_rules
                if isinstance(rule, dict) and str(rule.get("metric") or "") in option_metrics
            ]
            if not relevant:
                continue
            normalized = normalize_option_protection_config({"rules": relevant})
            for rule in normalized["rules"]:
                if rule not in rules:
                    rules.append(rule)
            for role in normalized["precedence"]:
                if role not in precedence:
                    precedence.append(role)
        if not rules:
            return None
        return {"rules": rules, "precedence": precedence}

    @staticmethod
    def _option_structure_identity(option_run: Any, owner: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        policy = owner.get("policy")
        digest = str((policy or {}).get("structure_digest") or "")
        if not digest:
            protection = getattr(option_run, "protection", None)
            digest = str((protection or {}).get("structure_digest") or "")
        return {"structure_digest": digest} if digest else None

    def _option_run_store(self) -> Any:
        if self.option_run_store is None:
            from backend.options.execution.durable_store import DurableOptionRunStore

            session_factory = getattr(self.repo, "session_factory", None)
            self.option_run_store = (
                DurableOptionRunStore(session_factory=session_factory)
                if session_factory is not None
                else DurableOptionRunStore()
            )
        return self.option_run_store

    def _index_tick_loader(self) -> Callable[[int], Awaitable[Dict[str, Any] | None]]:
        if self.index_tick_loader is not None:
            return self.index_tick_loader

        async def read_tick(token: int) -> Dict[str, Any] | None:
            from backend.broker_api.core.redis_events import get_redis

            redis = get_redis()
            raw = await redis.get(f"market:tick:{int(token)}")
            return json.loads(raw) if raw else None

        self.index_tick_loader = read_tick
        return self.index_tick_loader

    def _option_tick_loader(self) -> Callable[[int], Awaitable[Dict[str, Any] | None]]:
        if self.option_tick_loader is not None:
            return self.option_tick_loader
        self.option_tick_loader = self._index_tick_loader()
        return self.option_tick_loader

    def _index_token_resolver(self) -> Callable[[str, str], Awaitable[int | None]]:
        if self.index_token_resolver is not None:
            return self.index_token_resolver

        async def resolve(underlying: str, exchange: str) -> int | None:
            _ = exchange
            from backend.broker_api.instruments.instruments_repository import InstrumentsRepository

            def lookup() -> int | None:
                return InstrumentsRepository().get_spot_token(underlying)

            return await asyncio.to_thread(lookup)

        self.index_token_resolver = resolve
        return self.index_token_resolver

    def _option_token_resolver(self) -> Callable[[str, str], Awaitable[int | None]]:
        if self.option_token_resolver is not None:
            return self.option_token_resolver

        async def resolve(exchange: str, tradingsymbol: str) -> int | None:
            from backend.broker_api.instruments.instruments_repository import InstrumentsRepository

            def lookup() -> int | None:
                instrument = InstrumentsRepository().get_instrument_by_exchange_symbol(
                    exchange, tradingsymbol
                )
                return int(instrument.get("instrument_token")) if instrument else None

            return await asyncio.to_thread(lookup)

        self.option_token_resolver = resolve
        return self.option_token_resolver

    def _has_recent_exit_claim(self, state: Dict[str, Any], now: datetime) -> bool:
        if state.get("exit_submitted") or not state.get("exit_claim_id"):
            return False
        claimed_at = state.get("exit_claimed_at")
        if not claimed_at:
            return True
        try:
            parsed = datetime.fromisoformat(str(claimed_at).replace("Z", "+00:00"))
        except Exception:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() < 60

    @staticmethod
    def _is_deferred_exit_result(result: Any) -> bool:
        return bool(isinstance(result, dict) and (result.get("deferred") or str(result.get("status") or "").lower() == "deferred"))

    async def _persist_state(
        self,
        run: Dict[str, Any],
        runtime_state: Dict[str, Any],
        previous_protection_state: Dict[str, Any],
        protection_state: Dict[str, Any],
        *,
        expected_generation: Any = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
        timeline_events: Optional[list[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        strategy_run_id = str(run["strategy_run_id"])
        expected = int(expected_generation) if expected_generation is not None else None
        if timeline_events is None:
            timeline_events = self._build_timeline_events(
                run,
                previous_state=previous_protection_state,
                next_state=protection_state,
            )
        if hasattr(self.repo, "update_run_backend_protection_state_with_events"):
            return await self.repo.update_run_backend_protection_state_with_events(
                strategy_run_id,
                protection_state,
                expected_generation=expected,
                expected_triggered_rule=expected_triggered_rule,
                expected_exit_claim_id=expected_exit_claim_id,
                timeline_events=timeline_events,
            )
        if hasattr(self.repo, "update_run_backend_protection_state"):
            updated = await self.repo.update_run_backend_protection_state(
                strategy_run_id,
                protection_state,
                expected_generation=expected,
                expected_triggered_rule=expected_triggered_rule,
                expected_exit_claim_id=expected_exit_claim_id,
            )
            if updated is None:
                return None
            return {"run": updated, "timeline_events": []}
        runtime_state["backend_protection_state"] = protection_state
        updated = await self.repo.update_run_runtime_state(strategy_run_id, runtime_state)
        if updated is None:
            return None
        return {"run": updated, "timeline_events": []}

    async def _persist_run_error(self, run: Dict[str, Any], exc: Exception) -> None:
        try:
            runtime_state = dict(run.get("runtime_state") or {})
            previous = dict(runtime_state.get("backend_protection_state") or {})
            if previous.get("exit_submitted"):
                return
            runtime_state["backend_protection_state"] = {
                **previous,
                "status": "error",
                "last_checked_at": self.now_fn().isoformat(),
                "error": str(exc),
            }
            persisted = await self._persist_state(
                run,
                runtime_state,
                previous,
                runtime_state["backend_protection_state"],
                expected_generation=previous.get("generation"),
                expected_triggered_rule=previous.get("triggered_rule") or "",
                expected_exit_claim_id=previous.get("exit_claim_id") or "",
            )
            if persisted is not None:
                await self._publish_timeline_rows(persisted.get("timeline_events") or [])
        except Exception:
            return

    def _build_timeline_events(
        self,
        run: Dict[str, Any],
        *,
        previous_state: Dict[str, Any],
        next_state: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        events: list[Dict[str, Any]] = []
        strategy_run_id = str(run.get("strategy_run_id") or "")
        previous_status = str(previous_state.get("status") or "")
        next_status = str(next_state.get("status") or "")
        previous_exit_submitted = bool(previous_state.get("exit_submitted"))
        next_exit_submitted = bool(next_state.get("exit_submitted"))

        if next_status == "triggered" and previous_status != "triggered":
            events.append(
                {
                    "event_kind": "protection",
                    "event_source": "backend_protection",
                    "event_type": "protection.triggered",
                    "related_resource_type": "strategy_run",
                    "related_resource_id": strategy_run_id,
                    "summary": f"Backend protection triggered: {next_state.get('triggered_rule') or 'unknown_rule'}",
                    "payload": {
                        "emission_mode": "mutation_driven",
                        "status": next_status,
                        "triggered_rule": next_state.get("triggered_rule"),
                        "action": next_state.get("action"),
                        "generation": next_state.get("generation"),
                    },
                }
            )

        if next_exit_submitted and not previous_exit_submitted:
            events.append(
                {
                    "event_kind": "protection",
                    "event_source": "backend_protection",
                    "event_type": "protection.exit_submitted",
                    "related_resource_type": "strategy_run",
                    "related_resource_id": strategy_run_id,
                    "summary": f"Backend protection exit submitted: {next_state.get('triggered_rule') or 'unknown_rule'}",
                    "payload": {
                        "emission_mode": "mutation_driven",
                        "status": next_status,
                        "triggered_rule": next_state.get("triggered_rule"),
                        "exit_submission_status": next_state.get("exit_submission_status"),
                        "exit_result": next_state.get("exit_result"),
                        "generation": next_state.get("generation"),
                    },
                }
            )

        if previous_status == "error" and next_status == "active":
            events.append(
                {
                    "event_kind": "protection",
                    "event_source": "backend_protection",
                    "event_type": "protection.blocking_changed",
                    "related_resource_type": "strategy_run",
                    "related_resource_id": strategy_run_id,
                    "summary": "Backend protection recovered from error to active",
                    "payload": {
                        "emission_mode": "mutation_driven",
                        "previous_status": previous_status,
                        "status": next_status,
                        "generation": next_state.get("generation"),
                    },
                }
            )
        return events

    async def _publish_timeline_rows(self, rows: list[Dict[str, Any]]) -> None:
        for row in list(rows or []):
            strategy_run_id = str(row.get("strategy_run_id") or "").strip()
            if not strategy_run_id:
                continue
            await publish_event(f"worker.execution.events:{strategy_run_id}", dict(row))

    @staticmethod
    def _structure_identity(config: Any) -> Optional[Dict[str, Any]]:
        """The structure this run belongs to, or ``None``.

        ``None`` on every run that is not part of an option structure, which is what
        keeps their behaviour identical: the structure-aware branch is entered only
        when a structure was actually declared.
        """
        structure = getattr(config, "structure", None)
        if structure is None:
            return None
        payload = (
            structure.model_dump() if hasattr(structure, "model_dump") else dict(structure)
        )
        if not str(payload.get("structure_digest") or ""):
            return None
        return payload

    async def _submit_structure_exit(
        self,
        run: Dict[str, Any],
        state: Dict[str, Any],
        structure: Dict[str, Any],
        *,
        claim_id: str,
    ) -> Dict[str, Any]:
        """Submit a structure's staged exit, derived server-side.

        The engine adapter (``StagedStructureExit``) is the ONLY path: it resolves
        the durable option run bound to this worker run, reads the run's OWN
        confirmed trades, and asks the existing ``build_structure_exit_orders``
        rule which bounded actions are permitted now. The short closes first; the
        hedge is released only in a LATER stage, and only for the quantity its
        short is proven to have closed.

        An evaluator's ``recommended_exit_orders`` is deliberately NOT part of this
        call: a caller's order list is not evidence, and a protective rule that
        submitted one would be trusting the layer it exists to bound.
        """
        if self.structure_exit_submitter is not None:
            try:
                return await self.structure_exit_submitter(run, state)
            except Exception as exc:  # noqa: BLE001 - a failed stage is evidence
                return {
                    "submitted": False,
                    "complete": False,
                    "reason": "staged_exit_failed",
                    "error": str(exc),
                    "claim_id": claim_id,
                    "orders": [],
                }
        # No staged-exit adapter is wired, so there is NO safe structure exit: the
        # generic whole-book path would sell the long with the short (the naked
        # window the structure exists to prevent) and the evaluator's recommended
        # orders are not evidence. Refuse, and record why.
        return {
            "submitted": False,
            "complete": False,
            "reason": "staged_exit_not_wired",
            "claim_id": claim_id,
            "structure_digest": str((structure or {}).get("structure_digest") or ""),
            "orders": [],
        }

    def _idempotency_key(self, run: Dict[str, Any], state: Dict[str, Any]) -> str:
        generation = state.get("generation") or 1
        rule = str(state.get("triggered_rule") or "unknown")
        digest = hashlib.sha1(json.dumps(state.get("details") or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:10]
        return f"backend-protection:{run['strategy_run_id']}:g{generation}:{rule}:{digest}"


async def collect_worker_option_protection_subscription_tokens(
    repo: Any = None,
) -> set[int]:
    """Read the option-metric token set for the positions subscription worker."""

    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository

    if repo is None:
        repo = SqlAlchemyAlgoWorkerRepository()
    runtime = WorkerProtectionRuntime(
        repo=repo,
        pnl_loader=lambda _run: _empty_pnl(),
        exit_submitter=lambda _run, _state: _unused_exit(),
    )
    return await runtime.collect_option_metric_subscription_tokens()


async def _empty_pnl() -> Dict[str, Any]:
    return {"legs": []}


async def _unused_exit() -> Dict[str, Any]:
    raise RuntimeError("option subscription enumeration must never submit an exit")


async def load_worker_run_pnl_for_protection(request: Any, run: Dict[str, Any]) -> Dict[str, Any]:
    from backend.api.routers.worker_protection import _build_worker_run_pnl_snapshot

    return await _build_worker_run_pnl_snapshot(request, run)


async def submit_worker_protection_exit(request: Any, run: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    from backend.api.services.control_plane import exit_control_strategy

    return await exit_control_strategy(
        request,
        str(run["strategy_run_id"]),
        account_scope=str(run.get("account_scope") or "default"),
        reason=f"backend_protection:{state.get('triggered_rule') or 'unknown'}",
        dry_run=False,
        idempotency_key=str(state.get("exit_idempotency_key") or "") or None,
    )


async def submit_worker_protection_structure_exit(
    request: Any, run: Dict[str, Any], state: Dict[str, Any]
) -> Dict[str, Any]:
    """The production stage submitter for an option STRUCTURE's protection exit.

    The staged protocol itself lives in the options domain
    (``backend.options.protection.staged_exit``): it resolves the durable option
    run bound to this worker run, reads the run's OWN confirmed fills, and lets the
    existing ``build_structure_exit_orders`` rule decide which bounded actions are
    permitted now. This function is only the broker boundary it needs, and it
    builds that boundary the way the control plane does for any platform-initiated
    exit: the account's own broker session and a SERVER-SIDE attribution, so a
    risk-REDUCING action stays available after the child's credential is gone.

    Nothing here reads the child's token, and nothing here can INCREASE exposure:
    every order is a close of a leg the run's own evidence says it holds.
    """
    import asyncio
    from uuid import uuid4

    from fastapi import Response

    from backend.api.routers.worker_shared import _load_live_kite_for_account
    from backend.app.database import SessionLocal
    from backend.broker_api.orders import BasketOrderRequest, OrdersService
    from backend.options.protection.staged_exit import StagedStructureExit

    account_id = str(run.get("account_scope") or "")
    metadata = dict(run.get("metadata") or {})
    session_factory = getattr(
        getattr(request.app.state, "algo_worker_repository", None),
        "session_factory",
        None,
    ) or SessionLocal

    async def place_orders(
        *,
        account_id: str,
        worker_run_id: str,
        option_run_id: str,
        structure_digest: str,
        legs: list,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        from backend.algo_runtime.execution_attribution import build_execution_attribution

        kite = await asyncio.to_thread(_load_live_kite_for_account, account_id)
        stage = idempotency_key.rsplit(":", 1)[-1][:8].upper()
        payload_orders = []
        for index, leg in enumerate(legs):
            leg = dict(leg or {})
            # The claim already froze a DETERMINISTIC client order reference per
            # leg: the same stage computed twice can never become two different
            # broker orders, and the platform keeps an authoritative correlation
            # for each one (which is what the pre-send fence reads).
            client_order_ref = str(leg.get("client_order_ref") or f"KA{stage}{index + 1:02d}")
            attribution = build_execution_attribution(
                execution_mode="live",
                strategy_run_id=worker_run_id,
                strategy_family=str(metadata.get("strategy_family") or "options_strategy"),
                strategy_name=str(metadata.get("strategy_name") or "option_structure_protection"),
                account_ref=account_id,
                entry_surface="hosted_option_protection",
                source="backend_protection",
                idempotency_key=idempotency_key,
                metadata={
                    "option_run_id": option_run_id,
                    "structure_digest": structure_digest,
                    "stage_digest": idempotency_key.rsplit(":", 1)[-1],
                    "stage_leg_index": index,
                },
            )
            attribution["client_order_ref"] = client_order_ref
            payload_orders.append(
                {
                    "exchange": str(leg.get("exchange") or "NFO"),
                    "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                    "transaction_type": str(leg.get("transaction_type") or ""),
                    "quantity": int(leg.get("quantity") or 0),
                    "variety": str(leg.get("variety") or "regular"),
                    "product": str(leg.get("product") or "NRML"),
                    "order_type": str(leg.get("order_type") or "MARKET"),
                    "autoslice": should_autoslice(str(leg.get("exchange") or "")),
                    "attribution": attribution,
                }
            )
        basket = BasketOrderRequest.model_validate(
            {"orders": payload_orders, "all_or_none": False, "dry_run": False}
        )
        service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
        try:
            result = await service.place_basket(
                kite,
                basket,
                f"option-protection-{uuid4()}",
                session_id=f"backend:option-protection:{option_run_id}",
                idempotency_key=idempotency_key,
                response=Response(),
            )
        except Exception as exc:  # noqa: BLE001 - the stage stays unresolved, never re-sent
            return {
                "legs": [
                    {"index": index, "order_id": None, "error": str(exc)}
                    for index in range(len(payload_orders))
                ]
            }
        payload = result.model_dump(mode="json")
        # The orders service answers with a ``BasketOrderResponse``: one entry per
        # requested leg under ``results``, each carrying its own ``index`` and the
        # broker's ``order_id`` ONLY when the broker accepted that leg. Matching on
        # the reported index (not the list position) keeps a partial basket aligned.
        answers: dict = {}
        for position, row in enumerate(list(payload.get("results") or [])):
            if not isinstance(row, dict):
                continue
            answers[int(row.get("index", position))] = row
        return {
            "legs": [
                {
                    "index": index,
                    "order_id": (
                        str(answers[index].get("order_id"))
                        if answers.get(index, {}).get("order_id")
                        else None
                    ),
                    "error": (
                        None
                        if answers.get(index, {}).get("order_id")
                        else "no order reference returned for this leg"
                    ),
                }
                for index in range(len(payload_orders))
            ]
        }

    staged = StagedStructureExit(
        session_factory=session_factory,
        place_orders=place_orders,
    )
    return await staged.submit(worker_run=run, trigger=state)
