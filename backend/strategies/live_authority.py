"""Persisted live execution authority.

The execution environment of a plan is DERIVED, never requested:

``plan -> proposal envelope.strategy_run_id -> strategy_run_bindings``
gives the canonical ``(strategy, owner, account, environment)``; the bound run,
its token and the hosting job then provide the live authority evidence. A
request parameter (or a reservation alone) can never switch a plan to live.

Authority is more than a token expiry. The reader refuses unless, at the moment
of the call:

* the plan's bound run is ``open``, its mode is ``live`` and it agrees with the
  binding's account/strategy/environment;
* the bound token is active, still unexpired, and actually allows ``live``;
* the hosting job is in an authority-granting status with a live lease owned by
  a supervisor (``lease_owner`` set, ``lease_epoch >= 1``, ``lease_until`` in
  the future) at the job's current ``attempt``.

The effective freshness bound returned to the submission layer is the EARLIER
of the token expiry and the lease expiry: a token that outlives its lease is not
authority to keep trading.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from sqlalchemy import select, text

from backend.app.database import SessionLocal
from backend.strategies.settlement import JOB_AUTHORITY_STATUSES

#: The environment this module authorises. Anything else is a refusal: the live
#: path never executes a paper/dry-run binding.
LIVE_ENVIRONMENT = "live"

#: Authority must outlive the dispatch by this much; a lease/token that expires
#: inside the margin cannot authorise a new order.
AUTHORITY_MIN_REMAINING_SECONDS = 1.0

#: Run statuses that may place NEW work. ``exiting``/``closed``/``failed`` may
#: not increase or reduce exposure through this path.
RUN_AUTHORITY_STATUSES = ("open",)

#: The token action a live submission needs. The hosted lifecycle mints this
#: from the pinned ``trade`` capability, so a data-only child never holds it; a
#: live book that could trade without it would mean the token's capabilities
#: were being ignored.
TOKEN_ORDER_ACTIONS = ("intents:submit",)


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


class LiveAuthorityRefusal(RuntimeError):
    """A named refusal from the persisted-authority reader."""

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(reason_code)
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})

    def as_detail(self) -> Dict[str, Any]:
        return {"reason_code": self.reason_code, **self.detail}


def _envelope_strategy_run_id(session_factory: Callable[[], Any], plan: Mapping[str, Any]) -> str:
    from backend.strategies.proposals import ProposalStore

    envelope = ProposalStore(session_factory=session_factory).get_proposal(
        str(plan.get("proposal_id") or "")
    )
    if envelope is None:
        raise LiveAuthorityRefusal(
            "PLAN_NOT_VALIDATED",
            {"plan_id": str(plan.get("plan_id") or ""), "message": "no proposal envelope for this plan"},
        )
    return str(envelope.get("strategy_run_id") or "")


def plan_binding(
    session_factory: Optional[Callable[[], Any]] = None,
    *,
    plan: Mapping[str, Any],
) -> Dict[str, Any]:
    """The canonical run binding frozen into this plan (environment included).

    The route uses this to DECIDE which executor serves the plan: a request
    parameter never selects the environment.
    """
    factory = session_factory or SessionLocal
    run_id = _envelope_strategy_run_id(factory, plan)
    if not run_id:
        raise LiveAuthorityRefusal(
            "STRATEGY_RUN_BINDING_MISSING",
            {"plan_id": str(plan.get("plan_id") or ""), "message": "the frozen plan carries no bound run"},
        )
    from backend.strategies.attribution_models import StrategyRunBinding

    with factory() as session:
        row = session.execute(
            select(StrategyRunBinding).where(StrategyRunBinding.strategy_run_id == run_id)
        ).scalar_one_or_none()
    if row is None:
        raise LiveAuthorityRefusal(
            "STRATEGY_RUN_BINDING_MISSING",
            {"plan_id": str(plan.get("plan_id") or ""), "strategy_run_id": run_id},
        )
    return {
        "strategy_run_id": str(row.strategy_run_id),
        "strategy_id": str(row.strategy_id),
        "owner_id": str(row.owner_id),
        "account_id": str(row.account_id),
        "execution_environment": str(row.execution_environment),
    }


def derive_live_authority(
    session_factory: Optional[Callable[[], Any]] = None,
    *,
    plan: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Derive the binding + live authority evidence for one frozen plan."""
    factory = session_factory or SessionLocal
    plan_id = str(plan.get("plan_id") or "")
    plan_strategy = str(plan.get("strategy_id") or "")
    plan_account = str(plan.get("account_id") or "")
    moment = now or datetime.now(timezone.utc)
    run_id = _envelope_strategy_run_id(factory, plan)
    if not run_id:
        raise LiveAuthorityRefusal(
            "STRATEGY_RUN_BINDING_MISSING",
            {"plan_id": plan_id, "message": "the frozen plan carries no bound run"},
        )

    try:
        binding = plan_binding(factory, plan=plan)
    except LiveAuthorityRefusal:
        raise
    with factory() as session:
        if (
            binding["strategy_id"] != plan_strategy
            or binding["account_id"] != plan_account
        ):
            raise LiveAuthorityRefusal(
                "ACCOUNT_SCOPE_MISMATCH",
                {
                    "plan_id": plan_id,
                    "binding_strategy_id": binding["strategy_id"],
                    "binding_account_id": binding["account_id"],
                    "plan_strategy_id": plan_strategy,
                    "plan_account_id": plan_account,
                },
            )
        if binding["execution_environment"] != LIVE_ENVIRONMENT:
            raise LiveAuthorityRefusal(
                "BINDING_ENVIRONMENT_MISMATCH",
                {
                    "plan_id": plan_id,
                    "binding_environment": binding["execution_environment"],
                    "expected": LIVE_ENVIRONMENT,
                },
            )

        run_row = (
            session.execute(
                text(
                    """
                    SELECT strategy_run_id, token_id, template_id, account_scope,
                           execution_mode, status
                    FROM public.algo_worker_runs
                    WHERE strategy_run_id = :run_id
                    """
                ),
                {"run_id": run_id},
            )
            .mappings()
            .first()
        )
        if run_row is None:
            raise LiveAuthorityRefusal(
                "STRATEGY_RUN_BINDING_MISSING",
                {"plan_id": plan_id, "strategy_run_id": run_id, "message": "bound run row missing"},
            )
        run_mode = str(run_row["execution_mode"] or "")
        run_status = str(run_row["status"] or "")
        if run_mode != LIVE_ENVIRONMENT:
            raise LiveAuthorityRefusal(
                "RUN_MODE_MISMATCH",
                {"plan_id": plan_id, "run_mode": run_mode, "expected": LIVE_ENVIRONMENT},
            )
        if run_status not in RUN_AUTHORITY_STATUSES:
            raise LiveAuthorityRefusal(
                "RUN_NOT_OPEN",
                {"plan_id": plan_id, "strategy_run_id": run_id, "run_status": run_status},
            )

        token_row = (
            session.execute(
                text(
                    """
                    SELECT token_id, status, expires_at, allowed_modes, metadata_json
                           , allowed_actions
                    FROM public.algo_worker_tokens
                    WHERE token_id = :token_id
                    """
                ),
                {"token_id": str(run_row["token_id"] or "")},
            )
            .mappings()
            .first()
        )
        if token_row is None:
            raise LiveAuthorityRefusal(
                "TOKEN_MISSING",
                {"plan_id": plan_id, "token_id": str(run_row["token_id"] or "")},
            )
        token_status = str(token_row["status"] or "")
        if token_status != "active":
            raise LiveAuthorityRefusal(
                "TOKEN_NOT_ACTIVE",
                {"plan_id": plan_id, "token_status": token_status},
            )
        modes = token_row["allowed_modes"]
        if isinstance(modes, str):
            import json

            try:
                modes = json.loads(modes)
            except ValueError:
                modes = []
        if LIVE_ENVIRONMENT not in {str(mode) for mode in (modes or [])}:
            raise LiveAuthorityRefusal(
                "TOKEN_MODE_NOT_ALLOWED",
                {"plan_id": plan_id, "allowed_modes": list(modes or [])},
            )
        actions = token_row["allowed_actions"]
        if isinstance(actions, str):
            import json as _json_actions

            try:
                actions = _json_actions.loads(actions or "[]")
            except ValueError:
                actions = []
        held = {str(action) for action in (actions or [])}
        missing_actions = [
            action for action in TOKEN_ORDER_ACTIONS if action not in held
        ]
        if missing_actions:
            # A token that cannot submit an intent is not trade authority,
            # whatever its mode list says.
            raise LiveAuthorityRefusal(
                "TOKEN_ACTION_NOT_ALLOWED",
                {
                    "plan_id": plan_id,
                    "missing_actions": missing_actions,
                    "allowed_actions": sorted(held),
                },
            )
        token_expires = _as_datetime(token_row["expires_at"])
        if token_expires is None or moment + timedelta(seconds=AUTHORITY_MIN_REMAINING_SECONDS) >= token_expires:
            raise LiveAuthorityRefusal(
                "TOKEN_EXPIRED",
                {
                    "plan_id": plan_id,
                    "token_expires_at": token_expires.isoformat() if token_expires else None,
                },
            )

        job_row = (
            session.execute(
                text(
                    """
                    SELECT id, status, execution_mode, owner_id, account_scope, desired_state,
                           token_id, lease_owner, lease_epoch, lease_until, attempt
                    FROM public.strategy_jobs
                    WHERE run_id = :run_id
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"run_id": run_id},
            )
            .mappings()
            .first()
        )
        if job_row is None:
            raise LiveAuthorityRefusal(
                "HOSTED_JOB_MISSING",
                {"plan_id": plan_id, "strategy_run_id": run_id},
            )
        job_status = str(job_row["status"] or "")
        if job_status not in JOB_AUTHORITY_STATUSES:
            raise LiveAuthorityRefusal(
                "HOSTED_JOB_NOT_AUTHORITY",
                {"plan_id": plan_id, "job_id": str(job_row["id"]), "job_status": job_status},
            )
        # An operator stop wins over any residual status: no new exposure once the
        # attempt has been asked to stop.
        if str(job_row["desired_state"] or "") == "stopped":
            raise LiveAuthorityRefusal(
                "HOSTED_STOP_REQUESTED",
                {"plan_id": plan_id, "job_id": str(job_row["id"]), "desired_state": "stopped"},
            )
        # The job's OWN immutable identity must agree with the binding and the run:
        # mode, owner and account are pinned at creation and never reinterpreted.
        job_mode = str(job_row["execution_mode"] or "")
        if job_mode != LIVE_ENVIRONMENT or job_mode != run_mode:
            raise LiveAuthorityRefusal(
                "HOSTED_JOB_MODE_MISMATCH",
                {"plan_id": plan_id, "job_id": str(job_row["id"]), "job_mode": job_mode, "run_mode": run_mode},
            )
        if str(job_row["owner_id"] or "") != binding["owner_id"]:
            raise LiveAuthorityRefusal(
                "HOSTED_JOB_OWNER_MISMATCH",
                {"plan_id": plan_id, "job_id": str(job_row["id"])},
            )
        if str(job_row["account_scope"] or "") != binding["account_id"]:
            raise LiveAuthorityRefusal(
                "HOSTED_JOB_ACCOUNT_MISMATCH",
                {"plan_id": plan_id, "job_id": str(job_row["id"])},
            )
        # The hosting job must be running the SAME child credential as the run.
        job_token_id = str(job_row["token_id"] or "")
        if job_token_id and job_token_id != str(token_row["token_id"]):
            raise LiveAuthorityRefusal(
                "HOSTED_JOB_TOKEN_MISMATCH",
                {"plan_id": plan_id, "job_id": str(job_row["id"])},
            )
        # The token's pinned attempt must be THIS job attempt: a later attempt's
        # credential cannot be reinterpreted as authority for an earlier one.
        token_metadata = token_row["metadata_json"]
        if isinstance(token_metadata, str):
            import json as _json

            try:
                token_metadata = _json.loads(token_metadata or "{}")
            except ValueError:
                token_metadata = {}
        pinned_attempt = int((token_metadata or {}).get("hosted_attempt") or 0)
        job_attempt = int(job_row["attempt"] or 1)
        if pinned_attempt and pinned_attempt != job_attempt:
            raise LiveAuthorityRefusal(
                "HOSTED_ATTEMPT_MISMATCH",
                {
                    "plan_id": plan_id,
                    "job_id": str(job_row["id"]),
                    "token_attempt": pinned_attempt,
                    "job_attempt": job_attempt,
                },
            )
        # A trade-authorising job ALWAYS needs a live, owned, unexpired lease -
        # queued/starting is not enough, and there is no lease-free path.
        lease_until = _as_datetime(job_row["lease_until"])
        lease_owner = str(job_row["lease_owner"] or "")
        lease_epoch = int(job_row["lease_epoch"] or 0)
        if not lease_owner or lease_epoch < 1 or lease_until is None:
            raise LiveAuthorityRefusal(
                "HOSTED_LEASE_MISSING",
                {
                    "plan_id": plan_id,
                    "job_id": str(job_row["id"]),
                    "job_status": job_status,
                    "lease_owner": lease_owner,
                    "lease_epoch": lease_epoch,
                },
            )
        if moment + timedelta(seconds=AUTHORITY_MIN_REMAINING_SECONDS) >= lease_until:
            raise LiveAuthorityRefusal(
                "HOSTED_LEASE_EXPIRED",
                {
                    "plan_id": plan_id,
                    "job_id": str(job_row["id"]),
                    "lease_until": lease_until.isoformat(),
                },
            )

    effective_expiry = token_expires
    if lease_until is not None and lease_until < effective_expiry:
        effective_expiry = lease_until

    authority = {
        "strategy_id": binding["strategy_id"],
        "account_id": binding["account_id"],
        "worker_run_id": run_id,
        "expires_at": effective_expiry.isoformat(),
        "token_expires_at": token_expires.isoformat(),
        "token_id": str(token_row["token_id"]),
        "token_status": token_status,
        "run_status": run_status,
        "run_mode": run_mode,
        "job_id": str(job_row["id"]),
        "job_status": job_status,
        "lease_owner": lease_owner,
        "lease_epoch": lease_epoch,
        "lease_until": lease_until.isoformat() if lease_until else None,
        "attempt": int(job_row["attempt"] or 1),
        "execution_environment": LIVE_ENVIRONMENT,
    }
    return {"plan_id": plan_id, "binding": binding, "authority": authority}


def live_authority_reader(session_factory: Optional[Callable[[], Any]] = None) -> Callable[..., Dict[str, Any]]:
    """A production ``authority_reader`` for :class:`LivePlanAdapter`.

    Re-derives the authority from persisted records on every call (validation and
    dispatch-time re-check), so a revoked token, a moved lease epoch or a closed
    run is observed even between the two checks.
    """
    factory = session_factory or SessionLocal

    def _reader(*, plan: Mapping[str, Any], binding: Mapping[str, Any] | None = None) -> Dict[str, Any]:
        derived = derive_live_authority(factory, plan=plan)
        expected_run = str((binding or {}).get("strategy_run_id") or "")
        if expected_run and expected_run != derived["binding"]["strategy_run_id"]:
            raise LiveAuthorityRefusal(
                "LIVE_AUTHORITY_RUN_MISMATCH",
                {"expected_run_id": expected_run, "derived_run_id": derived["binding"]["strategy_run_id"]},
            )
        return derived["authority"]

    return _reader
