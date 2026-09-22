"""Pure validation and snapshot helpers for hosted strategies.

Everything here is deterministic and side-effect free. **Strategy source is
never imported, compiled or executed** — it is only length-checked and hashed.
No worker run is created and no worker token is minted in this module.

The two security-relevant jobs are:

- **Parameter validation** with the ``jsonschema`` library: an invalid schema is
  rejected, and parameter values are validated against the schema. Remote
  ``$ref`` is refused so validation can never fetch a URL.
- **Child token composition**: the run token a child may hold is composed from a
  closed action set and validated to **exclude ``heartbeat``** (lifecycle rights
  belong to the supervisor via the internal lifecycle API, not the child).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

try:  # jsonschema 4.18+ uses the `referencing` library for $ref resolution.
    from referencing import Registry
    from referencing.exceptions import NoSuchResource

    def _refuse_retrieval(uri):
        # Any attempt to RETRIEVE a remote resource is refused, so parameter
        # validation can never reach the network even if a ref slips through.
        raise NoSuchResource(ref=uri)

    _NO_NETWORK_REGISTRY = Registry(retrieve=_refuse_retrieval)
except Exception:  # pragma: no cover - only if referencing is unavailable
    _NO_NETWORK_REGISTRY = None

from backend.algo_runtime.account_scope import parse_account_scope

__all__ = [
    "ALLOWED_EXECUTION_MODES",
    "ALLOWED_JOB_KINDS",
    "ALLOWED_SCHEDULE_KINDS",
    "ALLOWED_STALE_EXIT_POLICIES",
    "CHILD_FORBIDDEN_ACTIONS",
    "CHILD_ORDER_ACTIONS",
    "CHILD_NOTIFY_ACTIONS",
    "MAX_NAME_LENGTH",
    "MAX_PARAMS_BYTES",
    "MAX_SCHEMA_BYTES",
    "MAX_SOURCE_BYTES",
    "CAPABILITY_KEYS",
    "CAPABILITY_SCHEMA_VERSION",
    "StrategyValidationError",
    "build_capabilities_snapshot",
    "build_policy_snapshot",
    "capability_actions",
    "child_run_token_actions",
    "parse_capability_snapshot",
    "validate_capabilities",
    "new_job_id",
    "new_schedule_id",
    "new_strategy_id",
    "new_version_id",
    "template_id_for",
    "validate_account_scope",
    "validate_child_token_actions",
    "validate_name",
    "validate_parameters",
    "validate_parameters_schema",
    "validate_source",
]

# ---------------------------------------------------------------------------
# closed vocabularies
# ---------------------------------------------------------------------------

ALLOWED_EXECUTION_MODES = ("paper", "dry_run", "live")
ALLOWED_JOB_KINDS = ("continuous", "finite")
ALLOWED_STALE_EXIT_POLICIES = ("none", "exit_on_worker_stale")
#: Every kind the stored schedule vocabulary allows. ``weekly`` needs a weekday,
#: ``monthly`` a day of month, ``calendar`` explicit ISO dates; ``daily`` is a
#: plain clock time. ``session_close`` was deliberately deferred (ambiguous
#: completion window), so it is still absent.
ALLOWED_SCHEDULE_KINDS = ("daily", "weekly", "monthly", "calendar")

#: Actions a CHILD run token may hold. ``heartbeat`` is absent on purpose.
#: ``runs:progress`` is the child-authenticated liveness/progress marker.
#: ``proposals:submit`` is a *trading* right (G5): it rides with
#: ``order_capable`` so a data-only hosted child can never open proposal
#: authority, and it never appears in the base or notify sets.
CHILD_BASE_ACTIONS = frozenset({"runs:read", "runs:log", "runs:progress"})
CHILD_ORDER_ACTIONS = frozenset(
    {"intents:submit", "runs:exit", "risk:update", "proposals:submit"}
)
CHILD_NOTIFY_ACTIONS = frozenset({"notifications:publish"})
#: Lifecycle rights reserved to the supervisor (internal lifecycle API).
CHILD_FORBIDDEN_ACTIONS = frozenset({"heartbeat"})
_ALLOWED_CHILD_ACTIONS = (
    CHILD_BASE_ACTIONS | CHILD_ORDER_ACTIONS | CHILD_NOTIFY_ACTIONS
)

# ---------------------------------------------------------------------------
# size limits (defensive; the source is never executed)
# ---------------------------------------------------------------------------

MAX_SOURCE_BYTES = 256 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
MAX_PARAMS_BYTES = 64 * 1024
MAX_NAME_LENGTH = 120

#: Schema constructs that can change resolution or fetch a remote resource.
#: They are refused outright: local `#/...` `$ref` is the only supported form.
FORBIDDEN_SCHEMA_KEYS = frozenset(
    {"$id", "$dynamicRef", "$dynamicAnchor", "$recursiveRef", "$recursiveAnchor"}
)


class StrategyValidationError(ValueError):
    """A hosted-strategy input is invalid. Maps to HTTP 422 at the router."""


# ---------------------------------------------------------------------------
# identifiers
# ---------------------------------------------------------------------------


def new_strategy_id() -> str:
    return f"hs_{uuid.uuid4().hex}"


def new_version_id() -> str:
    return f"hsv_{uuid.uuid4().hex}"


def new_schedule_id() -> str:
    return f"hss_{uuid.uuid4().hex}"


def new_job_id() -> str:
    return f"hsj_{uuid.uuid4().hex}"


def new_reconciliation_id() -> str:
    return f"hsr_{uuid.uuid4().hex}"


def template_id_for(strategy_id: str) -> str:
    """The worker ``template_id`` for a hosted strategy.

    Hosted strategies get a namespaced template id rather than replacing the
    external worker's free-string template ids.
    """
    return f"hosted:{strategy_id}"


# ---------------------------------------------------------------------------
# source / name
# ---------------------------------------------------------------------------


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise StrategyValidationError("name is required")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise StrategyValidationError(f"name must be at most {MAX_NAME_LENGTH} characters")
    return cleaned


def validate_source(source: str) -> tuple[str, str]:
    """Return ``(source, sha256)``. The source is never imported or executed."""
    if not isinstance(source, str) or not source.strip():
        raise StrategyValidationError("source is required")
    if _utf8_len(source) > MAX_SOURCE_BYTES:
        raise StrategyValidationError(f"source exceeds {MAX_SOURCE_BYTES} bytes")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return source, digest


# ---------------------------------------------------------------------------
# JSON Schema parameter validation
# ---------------------------------------------------------------------------


def _scan_schema(node: Any, path: str = "") -> tuple[List[str], List[str]]:
    """Return ``(forbidden_key_paths, remote_refs)`` found anywhere in a schema.

    Remote ``$ref`` and any construct that can change resolution
    (``$id``/``$dynamicRef``/``$dynamicAnchor``/``$recursiveRef``/
    ``$recursiveAnchor``) are collected so validation can reject them before a
    validator ever tries to resolve anything.
    """
    forbidden: List[str] = []
    remote: List[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            key_path = f"{path}/{key}" if path else str(key)
            if key in FORBIDDEN_SCHEMA_KEYS:
                forbidden.append(key_path)
                continue
            if key == "$ref" and isinstance(value, str) and not value.startswith("#"):
                remote.append(value)
                continue
            child_forbidden, child_remote = _scan_schema(value, key_path)
            forbidden.extend(child_forbidden)
            remote.extend(child_remote)
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for index, item in enumerate(node):
            child_forbidden, child_remote = _scan_schema(item, f"{path}/{index}")
            forbidden.extend(child_forbidden)
            remote.extend(child_remote)
    return forbidden, remote


def validate_parameters_schema(schema: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Validate and normalize a parameter schema. Rejects invalid schemas.

    Remote ``$ref`` and resolution-changing constructs are refused: parameter
    validation must never fetch a URL or resolve through a remote base.
    """
    if schema is None:
        normalized: Dict[str, Any] = {}
    elif isinstance(schema, Mapping):
        normalized = dict(schema)
    else:
        raise StrategyValidationError("parameters_schema must be a JSON object")

    encoded = json.dumps(normalized, separators=(",", ":"), default=str)
    if _utf8_len(encoded) > MAX_SCHEMA_BYTES:
        raise StrategyValidationError(f"parameters_schema exceeds {MAX_SCHEMA_BYTES} bytes")

    forbidden, remote_refs = _scan_schema(normalized)
    if forbidden:
        raise StrategyValidationError(
            "parameters_schema may not use resolution-changing or remote constructs: "
            + ", ".join(sorted(set(forbidden)))
        )
    if remote_refs:
        raise StrategyValidationError(
            "parameters_schema may not use remote $ref: " + ", ".join(sorted(set(remote_refs)))
        )

    try:
        Draft202012Validator.check_schema(normalized)
    except SchemaError as exc:
        raise StrategyValidationError(f"invalid parameters_schema: {exc.message}") from exc
    except RecursionError as exc:
        raise StrategyValidationError("parameters_schema is recursive beyond the supported depth") from exc
    return normalized


def _validator(schema: Mapping[str, Any]) -> Draft202012Validator:
    if _NO_NETWORK_REGISTRY is not None:
        return Draft202012Validator(schema, registry=_NO_NETWORK_REGISTRY)
    return Draft202012Validator(schema)


def validate_parameters(
    schema: Optional[Mapping[str, Any]], params: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Validate parameter values against a schema. Never executes anything.

    A recursive/self-referential schema fails boundedly as a validation error
    rather than crashing the caller, and an unresolvable reference is a
    validation error (the no-network registry refuses to retrieve anything).
    """
    values: Dict[str, Any] = dict(params or {})
    encoded = json.dumps(values, separators=(",", ":"), default=str)
    if _utf8_len(encoded) > MAX_PARAMS_BYTES:
        raise StrategyValidationError(f"parameters exceed {MAX_PARAMS_BYTES} bytes")

    normalized_schema = validate_parameters_schema(schema)
    if not normalized_schema:
        return values

    validator = _validator(normalized_schema)
    try:
        errors = sorted(validator.iter_errors(values), key=lambda error: list(error.path))
    except RecursionError as exc:
        raise StrategyValidationError("parameters_schema is recursive beyond the supported depth") from exc
    except Exception as exc:  # referencing/_WrappedReferencingError and friends
        name = type(exc).__name__
        if "Referenc" in name or "Unresolv" in name or "NoSuch" in name:
            raise StrategyValidationError(
                f"parameters_schema reference could not be resolved locally: {exc}"
            ) from exc
        raise
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.path) or "<root>"
        raise StrategyValidationError(f"invalid parameters at {location}: {first.message}")
    return values


# ---------------------------------------------------------------------------
# child token composition (no minting here)
# ---------------------------------------------------------------------------


def child_run_token_actions(
    *,
    order_capable: bool = False,
    notify: bool = False,
    extra: Iterable[str] = (),
) -> List[str]:
    """Compose the action set a child run token may hold.

    The child never receives ``heartbeat``: lifecycle rights belong to the
    supervisor. Composition is validated before it could ever be minted.
    """
    actions = set(CHILD_BASE_ACTIONS)
    if order_capable:
        actions |= CHILD_ORDER_ACTIONS
    if notify:
        actions |= CHILD_NOTIFY_ACTIONS
    actions |= {str(action) for action in extra}
    return validate_child_token_actions(actions)


def validate_child_token_actions(actions: Iterable[str]) -> List[str]:
    """Reject unknown actions and any lifecycle action (e.g. ``heartbeat``)."""
    requested = {str(action).strip() for action in actions if str(action).strip()}
    forbidden = requested & CHILD_FORBIDDEN_ACTIONS
    if forbidden:
        raise StrategyValidationError(
            "a child run token may not hold lifecycle actions: "
            + ", ".join(sorted(forbidden))
            + " (heartbeat/claim/release belong to the supervisor)"
        )
    unknown = requested - _ALLOWED_CHILD_ACTIONS
    if unknown:
        raise StrategyValidationError(
            "unsupported child token actions: " + ", ".join(sorted(unknown))
        )
    return sorted(requested)


# ---------------------------------------------------------------------------
# account-scope configuration (parsing is NOT authorization)
# ---------------------------------------------------------------------------


def validate_account_scope(account_scope: str, execution_mode: str) -> str:
    """Parse and shape-check an account scope for the given mode.

    Parsing alone is not authorization — the caller must have already checked
    the account against the operator's allowlist. This only enforces the
    mode/scope consistency the worker run path also enforces (a paper run needs
    a paper scope).
    """
    candidate = (account_scope or "").strip()
    if not candidate:
        raise StrategyValidationError("account_scope is required")
    try:
        parsed = parse_account_scope(candidate)
    except ValueError as exc:
        raise StrategyValidationError(str(exc)) from exc
    if execution_mode not in ALLOWED_EXECUTION_MODES:
        raise StrategyValidationError(
            f"execution_mode must be one of {', '.join(ALLOWED_EXECUTION_MODES)}"
        )
    if execution_mode == "paper" and parsed.mode != "paper":
        raise StrategyValidationError("paper execution requires a paper account_scope")
    if execution_mode == "live" and parsed.mode != "live":
        raise StrategyValidationError(
            "live execution requires a live account_scope (kite:<broker_user_id>)"
        )
    return candidate


# ---------------------------------------------------------------------------
# bounded schedule validation (stored only in this slice)
# ---------------------------------------------------------------------------

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_clock(label: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text == "session_close":
        raise StrategyValidationError(
            f"{label} 'session_close' is not supported; use an explicit HH:MM clock time"
        )
    if not _HHMM.match(text):
        raise StrategyValidationError(f"{label} must be an explicit HH:MM (24h) clock time")
    return text


def validate_schedule(
    *,
    schedule_kind: str,
    at_time: str,
    weekday: Optional[int] = None,
    day_of_month: Optional[int] = None,
    calendar_dates: Optional[Iterable[str]] = None,
    timezone: str = "Asia/Kolkata",
    window_end: Optional[str] = None,
    squareoff_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate the bounded schedule shape for every supported kind.

    ``daily`` is a clock time, ``weekly`` adds a weekday, ``monthly`` a day of
    month (clamped to the month's length at materialisation), and ``calendar``
    the explicit ISO dates. ``session_close`` remains rejected (ambiguous
    completion window). A field that belongs to another kind is rejected rather
    than stored, so a schedule can never carry a silent second meaning.
    """
    if schedule_kind not in ALLOWED_SCHEDULE_KINDS:
        raise StrategyValidationError(
            f"schedule_kind must be one of {', '.join(ALLOWED_SCHEDULE_KINDS)}"
        )
    at = _validate_clock("at_time", at_time)
    if at is None:
        raise StrategyValidationError("at_time is required")

    if schedule_kind == "daily":
        if weekday is not None or day_of_month is not None or calendar_dates is not None:
            raise StrategyValidationError("a daily schedule takes no weekday/day_of_month/dates")
        weekday = None
        day_of_month = None
        calendar_dates = None
    elif schedule_kind == "weekly":
        if (
            weekday is None
            or not isinstance(weekday, int)
            or isinstance(weekday, bool)
            or not (0 <= weekday <= 6)
        ):
            raise StrategyValidationError("a weekly schedule requires weekday 0..6")
        if day_of_month is not None or calendar_dates is not None:
            raise StrategyValidationError("a weekly schedule takes no day_of_month/calendar dates")
        day_of_month = None
        calendar_dates = None
    elif schedule_kind == "monthly":
        if (
            day_of_month is None
            or not isinstance(day_of_month, int)
            or isinstance(day_of_month, bool)
            or not (1 <= day_of_month <= 31)
        ):
            raise StrategyValidationError("a monthly schedule requires day_of_month 1..31")
        if weekday is not None or calendar_dates is not None:
            raise StrategyValidationError("a monthly schedule takes no weekday/calendar dates")
        weekday = None
        calendar_dates = None
    else:  # calendar
        dates = _validate_calendar_dates(calendar_dates)
        if not dates:
            raise StrategyValidationError(
                "a calendar schedule requires at least one ISO date in calendar_dates"
            )
        if weekday is not None or day_of_month is not None:
            raise StrategyValidationError("a calendar schedule takes no weekday/day_of_month")
        weekday = None
        day_of_month = None
        calendar_dates = dates

    tz = str(timezone or "").strip() or "Asia/Kolkata"
    return {
        "schedule_kind": schedule_kind,
        "at_time": at,
        "weekday": weekday,
        "day_of_month": day_of_month,
        "calendar_dates": calendar_dates,
        "timezone": tz,
        "window_end": _validate_clock("window_end", window_end),
        "squareoff_at": _validate_clock("squareoff_at", squareoff_at),
    }


def _validate_calendar_dates(values: Optional[Iterable[str]]) -> Optional[List[str]]:
    """Normalize explicit ISO dates: sorted, unique, and actually valid dates."""
    from datetime import date as _date

    if values is None:
        return None
    if isinstance(values, str):
        candidates = [item.strip() for item in values.split(",") if item.strip()]
    else:
        candidates = [str(item).strip() for item in values if str(item).strip()]
    parsed: List[str] = []
    for item in candidates:
        try:
            parsed.append(_date.fromisoformat(item).isoformat())
        except ValueError as exc:
            raise StrategyValidationError(f"calendar_dates entry is not an ISO date: {item!r}") from exc
    return sorted(dict.fromkeys(parsed))


# ---------------------------------------------------------------------------
# immutable configuration snapshots
# ---------------------------------------------------------------------------


#: Capabilities a hosted version may declare. ``trade`` grants the paper order
#: actions, ``notify`` grants run-scoped notification publish, ``data`` is the
#: baseline read/log capability. A snapshot that does not clearly declare these
#: is *ambiguous* and confers NO trading rights.
CAPABILITY_KEYS = ("data", "trade", "notify")
#: Current capability snapshot schema. Earlier marker-only snapshots (schema 1,
#: which recorded no capability entries) are treated as ambiguous and fail closed.
CAPABILITY_SCHEMA_VERSION = 2


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_capabilities_snapshot(
    *,
    trade: bool = False,
    notify: bool = False,
    data: bool = True,
) -> Dict[str, Any]:
    """Build a canonical capability snapshot.

    The default is deliberately **data-only** (no trading): a version that does
    not explicitly ask for trading rights cannot silently receive them.
    """
    for label, value in (("trade", trade), ("notify", notify), ("data", data)):
        if not isinstance(value, bool):
            raise StrategyValidationError(f"capability '{label}' must be a boolean")
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "capabilities": {"trade": trade, "notify": notify, "data": data},
        "captured_at": _utcnow_iso(),
    }


def validate_capabilities(payload: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Validate an author-supplied capability request into a canonical snapshot.

    ``None``/``{}`` means data-only. Unknown keys and non-boolean values are
    rejected (422 at the store).
    """
    if payload in (None, {}):
        return build_capabilities_snapshot()
    if not isinstance(payload, Mapping):
        raise StrategyValidationError("capabilities must be a JSON object")
    unknown = sorted(set(payload) - set(CAPABILITY_KEYS))
    if unknown:
        raise StrategyValidationError(
            "unsupported capabilities: " + ", ".join(unknown)
        )
    for key in CAPABILITY_KEYS:
        if key in payload and not isinstance(payload[key], bool):
            raise StrategyValidationError(f"capability '{key}' must be a boolean")
    return build_capabilities_snapshot(
        trade=bool(payload.get("trade", False)),
        notify=bool(payload.get("notify", False)),
        data=bool(payload.get("data", True)),
    )


def parse_capability_snapshot(snapshot: Optional[Mapping[str, Any]]) -> Dict[str, bool]:
    """Parse a stored capability snapshot into ``{data, trade, notify}``.

    **Fails closed** for anything ambiguous: a missing snapshot, a marker-only
    legacy snapshot (``schema_version`` 1, no ``capabilities`` map), an unknown
    schema version, extra keys, or non-boolean values all raise. A caller that
    cannot positively prove trading rights is not granted any.
    """
    if not isinstance(snapshot, Mapping):
        raise StrategyValidationError("capability snapshot is missing or malformed")
    if snapshot.get("schema_version") != CAPABILITY_SCHEMA_VERSION:
        raise StrategyValidationError(
            "capability snapshot is unsupported/ambiguous "
            f"(schema_version={snapshot.get('schema_version')!r}); re-save the version "
            "with explicit capabilities"
        )
    capabilities = snapshot.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise StrategyValidationError("capability snapshot has no capability map")
    unknown = sorted(set(capabilities) - set(CAPABILITY_KEYS))
    if unknown:
        raise StrategyValidationError(
            "capability snapshot has unknown capabilities: " + ", ".join(unknown)
        )
    parsed: Dict[str, bool] = {}
    for key in CAPABILITY_KEYS:
        value = capabilities.get(key)
        if not isinstance(value, bool):
            raise StrategyValidationError(
                f"capability snapshot must declare boolean '{key}'"
            )
        parsed[key] = value
    return parsed


def capability_actions(capabilities: Mapping[str, bool]) -> List[str]:
    """Compose the child token actions for a capability set.

    ``trade`` → paper order actions; ``notify`` → ``notifications:publish``;
    ``data`` → the baseline read/log actions (always present). ``heartbeat`` is
    never included.
    """
    return child_run_token_actions(
        order_capable=bool(capabilities.get("trade")),
        notify=bool(capabilities.get("notify")),
    )


def build_policy_snapshot(
    *,
    stale_exit_policy: str,
    max_duration_s: int,
    progress_deadline_s: int,
) -> Dict[str, Any]:
    """Immutable effective policy stored on a job/schedule.

    The effective max duration and progress deadline are captured here so a
    queued job is never re-derived from mutable strategy defaults.
    """
    if stale_exit_policy not in ALLOWED_STALE_EXIT_POLICIES:
        raise StrategyValidationError(
            f"stale_exit_policy must be one of {', '.join(ALLOWED_STALE_EXIT_POLICIES)}"
        )
    for label, value in (
        ("max_duration_s", max_duration_s),
        ("progress_deadline_s", progress_deadline_s),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise StrategyValidationError(f"{label} must be a positive integer")
    return {
        "stale_exit_policy": stale_exit_policy,
        "max_duration_s": int(max_duration_s),
        "progress_deadline_s": int(progress_deadline_s),
        "captured_at": _utcnow_iso(),
    }
