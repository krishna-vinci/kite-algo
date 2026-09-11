"""Safe YAML/JSON -> WorkflowDocument parser.

Safety rules (spec F1 / E-28):
- duplicate mapping keys are rejected, naming the key;
- custom YAML tags are rejected;
- documents larger than 256 KiB are rejected;
- alias expansion is bounded (node budget + depth cap).

Shape rules:
- unknown fields anywhere are rejected with the field path;
- shorthand is normalized: ``repeat: true/false`` -> ``on_transition``/``once``,
  ``"NSE:RELIANCE"`` -> ``InstrumentRef(exchange, symbol)``,
  ``evaluate_on`` -> ``clock``, ``on_signal`` -> ``on_transition``,
  ``cooldown: 30m`` -> ``cooldown_s: 1800``,
  ``rearm_above``/``rearm_below`` -> ``rearm_level`` + ``rearm_direction``;
- operand *contents* are parsed leniently (future capability syntax such as
  indicators, arithmetic expressions and dotted fundamental fields is captured
  into the typed operand model); validation, not parsing, flags them.

Dependency-free: stdlib + PyYAML only.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from .models import (
    AlertSpec,
    AttachmentSpec,
    BreadthSpec,
    Condition,
    ConditionGroup,
    DataPolicy,
    HysteresisSpec,
    InstrumentRef,
    Operand,
    RankSpec,
    ScheduleSpec,
    ScreenerSpec,
    SequenceSpec,
    Stage,
    UniverseRef,
    UniverseSpec,
    WorkflowDocument,
)

__all__ = ["WorkflowParseError", "parse_workflow_yaml", "parse_workflow_dict"]

MAX_DOCUMENT_BYTES = 256 * 1024
_MAX_NODES = 50_000
_MAX_DEPTH = 50

_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$")
_DURATION_UNITS_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Proposal-v1 names normalized into the v2 contract.
# "fundamentals_refresh" maps to candle_close: fundamentals conditions use
# the latest snapshot (with acquisition metadata) evaluated on candle events
# — a dedicated fundamentals stream is an explicit Phase 2 scope limitation.
_EVALUATE_ON_TO_CLOCK = {
    "ltp": "ltp",
    "candle_close": "candle_close",
    "fundamentals_refresh": "candle_close",
}

# Timeframe shorthand -> Kite interval names.
_TIMEFRAME_ALIASES = {
    "1m": "minute",
    "1minute": "minute",
    "3m": "3minute",
    "5m": "5minute",
    "10m": "10minute",
    "15m": "15minute",
    "30m": "30minute",
    "60m": "60minute",
    "1h": "60minute",
    "1d": "day",
    "daily": "day",
}
_ON_SIGNAL = "on_signal"


class WorkflowParseError(ValueError):
    """Raised when a document cannot be safely parsed into the model."""


# --------------------------------------------------------------------------
# Safe YAML loading
# --------------------------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader with duplicate-key detection and total tag rejection."""


def _construct_mapping(loader: _StrictLoader, node: yaml.Node, deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise WorkflowParseError(
                f"unhashable mapping key at line {key_node.start_mark.line + 1}: {key!r}"
            ) from exc
        if duplicate:
            raise WorkflowParseError(
                f"duplicate key '{key}' in YAML mapping (line {key_node.start_mark.line + 1})"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


def _reject_tag(loader: _StrictLoader, tag_suffix: Any, node: yaml.Node) -> Any:
    raise WorkflowParseError(
        f"custom YAML tags are not allowed (found tag '{node.tag}' "
        f"at line {node.start_mark.line + 1})"
    )


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)
_StrictLoader.add_multi_constructor("!", _reject_tag)
_StrictLoader.add_multi_constructor("tag:yaml.org,2002:python", _reject_tag)


def parse_workflow_yaml(text: str) -> WorkflowDocument:
    """Parse YAML text into a WorkflowDocument (safe, strict schema)."""
    if not isinstance(text, str):
        raise WorkflowParseError("document must be YAML text")
    size = len(text.encode("utf-8"))
    if size > MAX_DOCUMENT_BYTES:
        raise WorkflowParseError(
            f"document exceeds maximum size: {size} bytes (limit is 256 KiB / {MAX_DOCUMENT_BYTES} bytes)"
        )
    try:
        root = yaml.load(text, Loader=_StrictLoader)
    except WorkflowParseError:
        raise
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"invalid YAML: {exc}") from exc
    return parse_workflow_dict(root)


def parse_workflow_dict(obj: Any) -> WorkflowDocument:
    """Parse an already-loaded mapping (YAML or canonical JSON) into a
    WorkflowDocument."""
    if not isinstance(obj, dict):
        raise WorkflowParseError("document must be a YAML/JSON mapping")
    _check_budget(obj)
    return _parse_document(obj)


def _check_budget(root: Any) -> None:
    """Bound alias expansion: refuse to walk absurdly large logical trees."""
    counter = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal counter
        if depth > _MAX_DEPTH:
            raise WorkflowParseError(
                f"document too complex: nesting deeper than {_MAX_DEPTH} levels"
            )
        counter += 1
        if counter > _MAX_NODES:
            raise WorkflowParseError(
                "document too complex: alias expansion or size exceeds the parse budget "
                f"({_MAX_NODES} nodes)"
            )
        if isinstance(node, dict):
            for value in node.values():
                visit(value, depth + 1)
        elif isinstance(node, (list, tuple)):
            for value in node:
                visit(value, depth + 1)

    visit(root, 0)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _fail(path: str, message: str) -> WorkflowParseError:
    return WorkflowParseError(f"{path}: {message}")


def _unknown_field(path: str, key: Any) -> WorkflowParseError:
    return WorkflowParseError(f"unknown field '{path}.{key}' is not part of workflow schema v1")


def _check_keys(raw: dict, known: set[str], path: str) -> None:
    for key in raw:
        if not isinstance(key, str):
            raise _fail(path, f"mapping keys must be strings, got {key!r}")
        if key not in known:
            raise _unknown_field(path, key)


def _require_str(raw: dict, key: str, path: str, *, allow_empty: bool = False) -> Any:
    if key not in raw:
        raise _fail(f"{path}.{key}", f"requires '{key}' (missing); must be a non-empty string")
    value = raw[key]
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _fail(f"{path}.{key}", f"must be a non-empty string, got {value!r}")
    return value


def _optional_str(raw: dict, key: str, path: str) -> Any:
    if key not in raw or raw[key] is None:
        return None
    return _require_str(raw, key, path)


def _optional_int(raw: dict, key: str, path: str) -> Any:
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(f"{path}.{key}", f"must be an integer, got {value!r}")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(path, f"must be a number, got {value!r}")
    return float(value)


def _positive_int(value: Any, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _fail(path, f"must be an integer >= {minimum}, got {value!r}")
    return value


def _duration_seconds(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise _fail(path, f"must be a duration or seconds integer, got {value!r}")
    if isinstance(value, int):
        return _positive_int(value, path)
    if isinstance(value, str):
        match = _DURATION_RE.match(value.strip())
        if match:
            return int(match.group(1)) * _DURATION_UNITS_S[match.group(2)]
        if value.strip().isdigit():
            return _positive_int(int(value.strip()), path)
    raise _fail(path, f"must be a duration like '30m'/'1h' or seconds, got {value!r}")


# --------------------------------------------------------------------------
# document
# --------------------------------------------------------------------------


def _parse_document(obj: dict) -> WorkflowDocument:
    _check_keys(
        obj,
        {"version", "name", "instruments", "stages", "alerts", "data_policy",
         "session", "timezone", "universe", "screener"},  # timezone reserved;
        "document",                                        # universe/screener typed below
    )

    version = obj.get("version")
    if isinstance(version, bool) or version != 1:
        raise _fail(
            "document.version",
            f"unsupported schema version {version!r}; only version 1 is supported",
        )

    name = obj.get("name")
    if not isinstance(name, str) or not name:
        raise _fail("document.name", f"must be a non-empty string, got {name!r}")

    session = obj.get("session", "nse_equity")
    if not isinstance(session, str) or not session:
        raise _fail("document.session", f"must be a non-empty string, got {session!r}")

    instruments_raw = obj.get("instruments", [])
    if not isinstance(instruments_raw, list):
        raise _fail("document.instruments", "must be a list")
    instruments = tuple(
        _instrument(item, f"document.instruments[{i}]")
        for i, item in enumerate(instruments_raw)
    )

    stages_raw = obj.get("stages")
    if not isinstance(stages_raw, list):
        raise _fail("document.stages", "must be a list")
    stages = tuple(_stage(item, i) for i, item in enumerate(stages_raw))

    alerts_raw = obj.get("alerts", [])
    if not isinstance(alerts_raw, list):
        raise _fail("document.alerts", "must be a list")
    alerts = tuple(_alert(item, i) for i, item in enumerate(alerts_raw))

    data_policy = _data_policy(obj.get("data_policy", {}))

    universe = (
        _universe_spec(obj["universe"])
        if "universe" in obj and obj["universe"] not in (None, {}, [])
        else None
    )

    screener = (
        _screener_spec(obj["screener"])
        if "screener" in obj and obj["screener"] not in (None, {})
        else None
    )

    document = WorkflowDocument(
        version=1,
        name=name,
        session=session,
        instruments=instruments,
        stages=stages,
        alerts=alerts,
        data_policy=data_policy,
        universe=universe,
        screener=screener,
    )
    # Reserved top-level keys are outside the frozen dataclass model but must
    # still reach the compiler so unsupported values fail *validation* (issue
    # list) instead of being silently ignored. Parser-only channel; equality,
    # canonical JSON and the canonical hash are unaffected.
    reserved = {key: obj[key] for key in ("timezone",) if key in obj}
    if reserved:
        object.__setattr__(document, "_reserved", reserved)
    return document


def _universe_ref(raw: Any, path: str) -> UniverseRef:
    if not isinstance(raw, dict):
        raise _fail(path, f"universe reference must be a mapping, got {raw!r}")
    _check_keys(raw, {"kind", "name", "universe", "index", "watchlist"}, path)
    if "kind" in raw:
        kind = _require_str(raw, "kind", path)
        if kind not in ("universe", "index", "watchlist"):
            raise _fail(f"{path}.kind", f"unknown universe reference kind '{kind}'")
        return UniverseRef(kind=kind, name=_require_str(raw, "name", path))
    for kind in ("universe", "index", "watchlist"):
        if kind in raw:
            return UniverseRef(kind=kind, name=_require_str(raw, kind, path))
    raise _fail(path, "universe reference requires 'universe', 'index', or 'watchlist'")


def _universe_spec(raw: Any) -> UniverseSpec:
    if not isinstance(raw, dict):
        raise _fail("document.universe", "must be a mapping")
    _check_keys(
        raw, {"union", "exclude", "intersect", "deduplicate", "refs", "name"},
        "document.universe",
    )

    refs_raw = None
    if "union" in raw:
        refs_raw = raw["union"]
    elif "refs" in raw:
        refs_raw = raw["refs"]
    if refs_raw is None:
        raise _fail("document.universe.union", "requires a 'union' list of references")
    if not isinstance(refs_raw, list) or not refs_raw:
        raise _fail("document.universe.union", "must be a non-empty list of references")
    refs = tuple(
        _universe_ref(item, f"document.universe.union[{i}]")
        for i, item in enumerate(refs_raw)
    )

    exclude: tuple[UniverseRef, ...] = ()
    if "exclude" in raw and raw["exclude"] is not None:
        exclude_raw = raw["exclude"]
        if not isinstance(exclude_raw, list):
            raise _fail("document.universe.exclude", "must be a list of references")
        exclude = tuple(
            _universe_ref(item, f"document.universe.exclude[{i}]")
            for i, item in enumerate(exclude_raw)
        )

    intersect: tuple[UniverseRef, ...] = ()
    if "intersect" in raw and raw["intersect"] is not None:
        intersect_raw = raw["intersect"]
        if not isinstance(intersect_raw, list) or not intersect_raw:
            raise _fail("document.universe.intersect", "must be a non-empty list of references")
        intersect = tuple(
            _universe_ref(item, f"document.universe.intersect[{i}]")
            for i, item in enumerate(intersect_raw)
        )

    deduplicate = raw.get("deduplicate", True)
    if not isinstance(deduplicate, bool):
        raise _fail("document.universe.deduplicate", "must be a boolean")

    return UniverseSpec(refs=refs, exclude=exclude, intersect=intersect, deduplicate=deduplicate)


_SCREENER_TRIGGER_TYPES = ("entry", "exit", "top_n", "rank_delta")


def _screener_spec(raw: Any) -> ScreenerSpec:
    """Parse the ``screener`` block (Phase 3 F9): schedule, optional ranking,
    optional top-N cut, and result attachments."""
    if not isinstance(raw, dict):
        raise _fail("document.screener", "must be a mapping")
    _check_keys(
        raw,
        {"schedule", "rank", "top_n", "attachments", "freshness_limit",
         "freshness_limit_s"},  # _s is the canonical-JSON (seconds) form
        "document.screener",
    )
    if "schedule" not in raw:
        raise _fail("document.screener.schedule", "is required")
    schedule = _schedule_spec(raw["schedule"])

    rank: Optional[RankSpec] = None
    if raw.get("rank") is not None:
        rank_raw = raw["rank"]
        if not isinstance(rank_raw, dict):
            raise _fail("document.screener.rank", "must be a mapping")
        _check_keys(rank_raw, {"by", "direction"}, "document.screener.rank")
        direction = rank_raw.get("direction", "desc")
        if direction not in ("desc", "asc"):
            raise _fail(
                "document.screener.rank.direction",
                f"must be 'desc' or 'asc', got {direction!r}",
            )
        rank = RankSpec(by=_operand(rank_raw["by"], "document.screener.rank.by"), direction=direction)

    top_n = raw.get("top_n")
    if top_n is not None:
        if isinstance(top_n, bool) or not isinstance(top_n, int) or not (1 <= top_n <= 1000):
            raise _fail("document.screener.top_n", "must be an integer in [1, 1000]")

    if "freshness_limit_s" in raw and raw.get("freshness_limit_s") is not None:
        freshness_limit_s = raw["freshness_limit_s"]
        if isinstance(freshness_limit_s, bool) or not isinstance(freshness_limit_s, int):
            raise _fail("document.screener.freshness_limit_s", "must be an integer number of seconds")
    else:
        freshness_limit = raw.get("freshness_limit", "3d")
        freshness_limit_s = _duration_seconds(freshness_limit, "document.screener.freshness_limit")
    if freshness_limit_s < 300 or freshness_limit_s > 30 * 24 * 3600:
        raise _fail(
            "document.screener.freshness_limit",
            "must be between 5m and 30d",
        )

    attachments_raw = raw.get("attachments", [])
    if not isinstance(attachments_raw, list):
        raise _fail("document.screener.attachments", "must be a list")
    attachments = tuple(
        _attachment_spec(item, f"document.screener.attachments[{i}]")
        for i, item in enumerate(attachments_raw)
    )

    return ScreenerSpec(
        schedule=schedule,
        rank=rank,
        top_n=top_n,
        attachments=attachments,
        freshness_limit_s=freshness_limit_s,
    )


def _schedule_spec(raw: Any) -> ScheduleSpec:
    if not isinstance(raw, dict):
        raise _fail("document.screener.schedule", "must be a mapping")
    _check_keys(raw, {"every", "calendar", "at"}, "document.screener.schedule")
    every = _duration_seconds(_require_str(raw, "every", "document.screener.schedule"), "document.screener.schedule.every")
    if every < 300 or every > 31 * 24 * 3600:
        raise _fail("document.screener.schedule.every", "must be between 5m and 31d")
    calendar = raw.get("calendar", "nse_equity")
    if calendar != "nse_equity":
        # MCX/currency eligibility is feed-driven: no session calendar exists
        # for scheduled scans. Reject instead of applying NSE hours.
        raise _fail(
            "document.screener.schedule.calendar",
            f"unsupported schedule calendar {calendar!r}: only 'nse_equity' is "
            "calendar-backed; MCX/currency have no session calendar for "
            "scheduled scans",
        )
    at = raw.get("at")
    if at is not None:
        at = str(at)
        if at != "session_close":
            try:
                hours, minutes = at.split(":")
                if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
                    raise ValueError
            except (ValueError, AttributeError):
                raise _fail(
                    "document.screener.schedule.at",
                    "must be 'HH:MM' (IST) or 'session_close'",
                )
    return ScheduleSpec(every=f"{every}s", calendar=calendar, at=at)


def _attachment_spec(raw: Any, path: str) -> AttachmentSpec:
    if not isinstance(raw, dict):
        raise _fail(path, "attachment must be a mapping")
    _check_keys(
        raw,
        {"id", "trigger", "channels", "top_n", "rank_delta", "entry_rank",
         "exit_rank", "exit_after", "initial_match", "message"},
        path,
    )
    att_id = _require_str(raw, "id", path)
    trigger = _require_str(raw, "trigger", path)
    if trigger not in _SCREENER_TRIGGER_TYPES:
        raise _fail(f"{path}.trigger", f"unknown attachment trigger '{trigger}' (supported: {list(_SCREENER_TRIGGER_TYPES)})")
    channels_raw = raw.get("channels", [])
    if not isinstance(channels_raw, list) or not channels_raw:
        raise _fail(f"{path}.channels", "must be a non-empty list of channel names")
    channels = tuple(_require_str(raw, "channels", path) if False else str(c) for c in channels_raw)

    def _int_field(key: str) -> Optional[int]:
        value = raw.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 1000:
            raise _fail(f"{path}.{key}", "must be an integer in [1, 1000]")
        return value

    top_n = _int_field("top_n")
    rank_delta = _int_field("rank_delta")
    entry_rank = _int_field("entry_rank")
    exit_rank = _int_field("exit_rank")
    exit_after = _int_field("exit_after")
    initial_match = raw.get("initial_match", False)
    if not isinstance(initial_match, bool):
        raise _fail(f"{path}.initial_match", "must be a boolean")
    message = _optional_str(raw, "message", path)
    if trigger == "top_n" and top_n is None:
        raise _fail(f"{path}.top_n", "is required for trigger 'top_n'")
    if trigger == "rank_delta" and rank_delta is None:
        raise _fail(f"{path}.rank_delta", "is required for trigger 'rank_delta'")
    if (entry_rank is not None or exit_rank is not None) and trigger not in ("top_n", "entry", "exit"):
        raise _fail(f"{path}.entry_rank", f"hysteresis ranks only apply to top_n/entry/exit triggers, not '{trigger}'")
    if entry_rank is not None and exit_rank is not None and exit_rank <= entry_rank:
        raise _fail(
            f"{path}.exit_rank",
            f"exit_rank ({exit_rank}) must be greater than entry_rank ({entry_rank}) "
            "so a boundary symbol cannot oscillate between states (E-17)",
        )
    if trigger == "top_n" and top_n is not None:
        if entry_rank is None:
            entry_rank = top_n
        if exit_rank is None:
            # default exit buffer: 1.5x the entry band (at least entry + 1)
            exit_rank = top_n + max(1, top_n // 2)
        if exit_rank <= entry_rank:
            raise _fail(
                f"{path}.exit_rank",
                f"exit_rank ({exit_rank}) must be greater than entry_rank ({entry_rank})",
            )
    return AttachmentSpec(
        id=att_id,
        trigger=trigger,
        channels=channels,
        top_n=top_n,
        rank_delta=rank_delta,
        entry_rank=entry_rank,
        exit_rank=exit_rank,
        exit_after=exit_after,
        initial_match=initial_match,
        message=message,
    )


def _instrument(item: Any, path: str) -> InstrumentRef:
    if isinstance(item, str):
        if ":" not in item:
            raise _fail(path, "instrument shorthand must be 'EXCHANGE:SYMBOL', got " + repr(item))
        exchange, _, symbol = item.partition(":")
        exchange = exchange.strip()
        symbol = symbol.strip()
        if not exchange or not symbol:
            raise _fail(path, "instrument shorthand must be 'EXCHANGE:SYMBOL', got " + repr(item))
        return InstrumentRef(symbol=symbol, exchange=exchange)
    if isinstance(item, dict):
        _check_keys(item, {"symbol", "exchange"}, path)
        if "symbol" not in item or "exchange" not in item:
            raise _fail(path, "instrument mapping requires 'symbol' and 'exchange'")
        return InstrumentRef(
            symbol=_require_str(item, "symbol", path),
            exchange=_require_str(item, "exchange", path),
        )
    raise _fail(path, f"instrument must be 'EXCHANGE:SYMBOL' or a mapping, got {item!r}")


def _data_policy(raw: Any) -> DataPolicy:
    if not isinstance(raw, dict):
        raise _fail("document.data_policy", "must be a mapping")
    _check_keys(raw, {"missing", "insufficient_history", "require_closed_candles"}, "document.data_policy")
    missing = raw.get("missing", DataPolicy.missing)
    insufficient = raw.get("insufficient_history", DataPolicy.insufficient_history)
    require_closed = raw.get("require_closed_candles", DataPolicy.require_closed_candles)
    if not isinstance(missing, str) or not missing:
        raise _fail("document.data_policy.missing", "must be a non-empty string")
    if not isinstance(insufficient, str) or not insufficient:
        raise _fail("document.data_policy.insufficient_history", "must be a non-empty string")
    if not isinstance(require_closed, bool):
        raise _fail("document.data_policy.require_closed_candles", "must be a boolean")
    return DataPolicy(
        missing=missing,
        insufficient_history=insufficient,
        require_closed_candles=require_closed,
    )


# --------------------------------------------------------------------------
# stages and conditions
# --------------------------------------------------------------------------


def _stage(raw: Any, index: int) -> Stage:
    if not isinstance(raw, dict):
        raise _fail(f"document.stages[{index}]", "stage must be a mapping")
    if "id" not in raw:
        raise _fail(f"document.stages[{index}]", "stage requires an 'id'")
    sid = _require_str(raw, "id", f"document.stages[{index}]")
    path = f"document.stages.{sid}"
    _check_keys(
        raw,
        {
            "id", "type", "clock", "timeframe", "conditions", "input", "evaluate_on",
            "any", "not", "any_conditions", "not_conditions",
            "function", "params", "stage_params", "source", "source_field",
            # Phase 4 F10
            "consecutive_bars", "sequence", "breadth",
        },
        path,
    )

    stage_type = raw.get("type", "signal")
    if not isinstance(stage_type, str) or not stage_type:
        raise _fail(f"{path}.type", f"must be a non-empty string, got {stage_type!r}")

    clock: Any = None
    if "clock" in raw:
        clock = _require_str(raw, "clock", path)
    if "evaluate_on" in raw:
        evaluate_on = _require_str(raw, "evaluate_on", path)
        mapped = _EVALUATE_ON_TO_CLOCK.get(evaluate_on, evaluate_on)
        if clock is not None and clock != mapped:
            raise _fail(f"{path}.clock", f"conflicts with evaluate_on='{evaluate_on}'")
        clock = mapped
    if clock is None:
        raise _fail(path, "stage requires 'clock' (or 'evaluate_on')")

    timeframe = _optional_str(raw, "timeframe", path)
    if timeframe is not None:
        timeframe = _TIMEFRAME_ALIASES.get(timeframe.strip().lower(), timeframe)
    stage_input = _optional_str(raw, "input", path)

    function = _optional_str(raw, "function", path)
    stage_params_raw = raw.get("params", raw.get("stage_params"))
    if stage_params_raw is not None and not isinstance(stage_params_raw, dict):
        raise _fail(f"{path}.params", "must be a mapping")
    stage_params = dict(stage_params_raw or {})
    if "stage_params" in raw and "params" in raw and raw["stage_params"] != raw["params"]:
        raise _fail(f"{path}.stage_params", "conflicts with params")
    source_field = _optional_str(raw, "source", path) or _optional_str(raw, "source_field", path)
    if "source" in raw and "source_field" in raw and raw["source"] != raw["source_field"]:
        raise _fail(f"{path}.source_field", "conflicts with source")

    has_conditions = "conditions" in raw
    has_groups = any(key in raw for key in ("any", "not", "any_conditions", "not_conditions"))
    consecutive_bars = _optional_int(raw, "consecutive_bars", path)
    sequence = _sequence(raw.get("sequence"), f"{path}.sequence") if "sequence" in raw else None
    breadth = _breadth(raw.get("breadth"), f"{path}.breadth") if "breadth" in raw else None
    if (
        not has_conditions
        and not has_groups
        and stage_type not in ("feature", "breadth")
        and sequence is None
    ):
        raise _fail(path, "stage requires 'conditions'")

    conditions: tuple[Condition, ...] = ()
    any_conditions: tuple[Condition, ...] = ()
    not_conditions: tuple[Condition, ...] = ()
    if has_conditions:
        conditions = _conditions(raw["conditions"], f"{path}.conditions", group="all")
    # Groups declared INSIDE `conditions` are merged with the top-level
    # aliases. They used to be silently dropped, which reduced an OR/NOT rule
    # to AND-only without any error.
    inline_groups = (
        _condition_groups_from_block(raw["conditions"], f"{path}.conditions")
        if has_conditions
        else {}
    )
    if "any" in raw or "any_conditions" in raw:
        items = raw.get("any", raw.get("any_conditions"))
        any_conditions = _condition_list(items, f"{path}.any")
    elif inline_groups.get("any"):
        any_conditions = inline_groups["any"]
    if "not" in raw or "not_conditions" in raw:
        items = raw.get("not", raw.get("not_conditions"))
        if isinstance(items, dict) or isinstance(items, Condition):
            items = [items]
        not_conditions = _condition_list(items, f"{path}.not")
    elif inline_groups.get("not"):
        not_conditions = inline_groups["not"]

    return Stage(
        id=sid,
        type=stage_type,
        clock=clock,  # type: ignore[arg-type]  # unknown clocks are flagged by the compiler
        timeframe=timeframe,
        conditions=conditions,
        input=stage_input,
        any_conditions=any_conditions,
        not_conditions=not_conditions,
        function=function,
        stage_params=stage_params,
        source_field=source_field,
        consecutive_bars=consecutive_bars,
        sequence=sequence,
        breadth=breadth,
    )


_SEQUENCE_KEYS = {"first", "then", "within_bars", "within"}
_BREADTH_KEYS = {"condition", "distinct_instruments", "window", "mode"}


def _sequence(raw: Any, path: str) -> SequenceSpec:
    """Parse a bounded A-then-B sequence (Phase 4 F10)."""
    if not isinstance(raw, dict):
        raise _fail(path, "sequence must be a mapping")
    _check_keys(raw, _SEQUENCE_KEYS, path)
    for required in ("first", "then"):
        if required not in raw:
            raise _fail(path, f"sequence requires '{required}'")
    within_bars = _optional_int(raw, "within_bars", path)
    within_s = None
    if "within" in raw:
        within_s = _duration_seconds(raw["within"], f"{path}.within")
    if within_bars is None and within_s is None:
        raise _fail(
            path,
            "sequence requires at least one bound: 'within_bars' (completed bars) "
            "or 'within' (elapsed time)",
        )
    return SequenceSpec(
        first=_condition_groups(raw["first"], f"{path}.first"),
        then=_condition_groups(raw["then"], f"{path}.then"),
        within_bars=within_bars,
        within_s=within_s,
    )


def _breadth(raw: Any, path: str) -> BreadthSpec:
    """Parse windowed distinct-symbol participation (Phase 4 F10)."""
    if not isinstance(raw, dict):
        raise _fail(path, "breadth must be a mapping")
    _check_keys(raw, _BREADTH_KEYS, path)
    if "condition" not in raw:
        raise _fail(path, "breadth requires 'condition'")
    if "distinct_instruments" not in raw:
        raise _fail(path, "breadth requires 'distinct_instruments'")
    distinct = raw["distinct_instruments"]
    if isinstance(distinct, bool) or not isinstance(distinct, int) or distinct < 2:
        raise _fail(f"{path}.distinct_instruments", "must be an integer >= 2")
    if "window" not in raw:
        raise _fail(path, "breadth requires 'window'")
    window_s = _duration_seconds(raw["window"], f"{path}.window")
    mode = _optional_str(raw, "mode", path) or "triggers_within"
    return BreadthSpec(
        condition=_condition_groups(raw["condition"], f"{path}.condition"),
        distinct_instruments=distinct,
        window_s=window_s,
        mode=mode,
    )


def _condition_groups(raw: Any, path: str) -> tuple[ConditionGroup, ...]:
    """Parse a condition block into named 3VL groups (all/any/not).

    Three accepted forms, in order of preference:

    - **mapping** — ``{all: [...], any: [...]}`` (authoring shorthand);
    - **list of single-group mappings** — ``[{any: [...]}, {all: [...]}]``,
      which is the canonical serialized form (``to_document_dict``) and is
      what makes parse → serialize → parse an identity;
    - **bare condition list** — ``[...]``, shorthand for one ``all`` group.
    """
    if isinstance(raw, list):
        if _is_group_list(raw):
            groups = []
            for item in raw:
                (key, items), = item.items()
                if key == "not" and isinstance(items, dict):
                    items = [items]
                groups.append(
                    ConditionGroup(kind=key, conditions=_condition_list(items, f"{path}.{key}"))
                )
            return tuple(groups)
        return (ConditionGroup(kind="all", conditions=_condition_list(raw, path)),)
    if not isinstance(raw, dict):
        raise _fail(path, "must be a mapping with 'all'/'any'/'not' or a list of conditions")
    groups = []
    for key, items in raw.items():
        if not isinstance(key, str) or key not in ("all", "any", "not"):
            raise _unknown_field(path, key)
        if key == "not" and isinstance(items, dict):
            items = [items]
        groups.append(
            ConditionGroup(kind=key, conditions=_condition_list(items, f"{path}.{key}"))
        )
    if not groups:
        raise _fail(path, "must name at least one of 'all'/'any'/'not'")
    return tuple(groups)


def _is_group_list(items: list) -> bool:
    """True when every element names exactly one 3VL group (canonical form)."""
    if not items:
        return False
    return all(
        isinstance(item, dict)
        and len(item) == 1
        and next(iter(item)) in ("all", "any", "not")
        for item in items
    )


def _conditions(raw: Any, path: str, *, group: str = "all") -> tuple[Condition, ...]:
    """Parse one named group from a ``conditions`` block.

    The block may name ANY non-empty subset of the three groups:
    ``{all: [...]}``, the fully layered ``{all: [...], any: [...], not: [...]}``
    (the documented form), or a group-only rule such as ``{any: [...]}``.

    An absent ``all`` group is simply empty, and an empty AND group is True, so
    an ``any``-only rule evaluates to exactly the OR the author wrote. Requiring
    ``all`` was an accident of calling this with ``group="all"`` unconditionally:
    it made a legitimate OR-only rule unauthorable with no semantic reason, even
    though the sibling group parser (:func:`_condition_groups`) has always
    accepted any subset. The ``any``/``not`` groups themselves are parsed by
    :func:`_condition_groups_from_block`, so a layered rule is never silently
    reduced to its AND group.
    """
    if isinstance(raw, dict):
        for key in raw:
            if not isinstance(key, str) or key not in ("all", "any", "not"):
                raise _unknown_field(path, key)
        if not raw:
            raise _fail(path, "must name at least one of 'all'/'any'/'not'")
        if group not in raw:
            if group == "all" and ("any" in raw or "not" in raw):
                # OR/NOT-only rule: the AND group is empty (and therefore true).
                return ()
            raise _fail(path, f"must be a mapping with an '{group}' list of conditions")
        items = raw[group]
    elif isinstance(raw, list):
        items = raw
    else:
        raise _fail(path, "must be a mapping with 'all'/'any'/'not' or a list of conditions")
    return _condition_list(items, path)


def _condition_groups_from_block(raw: Any, path: str) -> dict:
    """The optional ``any``/``not`` groups declared INSIDE ``conditions``.

    These used to be validated and then dropped, which silently turned an
    OR/NOT rule into an AND-only rule. They are returned here so the stage can
    merge them with the top-level aliases instead of ignoring them.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key in ("any", "not"):
        if key in raw:
            items = raw[key]
            if key == "not" and isinstance(items, dict):
                items = [items]
            out[key] = _condition_list(items, f"{path}.{key}")
    return out


def _condition_list(items: Any, path: str) -> tuple[Condition, ...]:
    if not isinstance(items, list):
        raise _fail(path, "must be a list of conditions")
    if len(items) > 1 and path.endswith(".not"):
        raise _fail(path, "a 'not' group takes exactly one condition")
    return tuple(_condition(item, f"{path}[{i}]") for i, item in enumerate(items))


def _condition(raw: Any, path: str) -> Condition:
    if not isinstance(raw, dict):
        raise _fail(path, f"condition must be a mapping, got {raw!r}")
    _check_keys(raw, {"left", "op", "right", "field", "value", "hysteresis"}, path)

    if "op" not in raw:
        raise _fail(f"{path}.op", "condition requires an operator 'op'")
    op = _require_str(raw, "op", path)
    hysteresis = (
        _hysteresis(raw["hysteresis"], f"{path}.hysteresis")
        if "hysteresis" in raw
        else None
    )

    if "left" in raw or "right" in raw:
        if "left" not in raw or "right" not in raw:
            raise _fail(path, "full condition form requires both 'left' and 'right'")
        return Condition(
            left=_operand(raw["left"], f"{path}.left"),
            op=op,
            right=_operand(raw["right"], f"{path}.right"),
            hysteresis=hysteresis,
        )

    if "field" in raw:
        # shorthand: {field: X, op: gt, value: Y}
        field_name = _require_str(raw, "field", path)
        value_operand = Operand(kind="value")
        if "value" in raw and raw["value"] is not None:
            value_operand = Operand(kind="value", value=_number(raw["value"], f"{path}.value"))
        return Condition(
            left=Operand(kind="field", name=field_name),
            op=op,
            right=value_operand,
            hysteresis=hysteresis,
        )

    raise _fail(path, "condition must define either left/op/right or field/op/value")


def _hysteresis(raw: Any, path: str) -> HysteresisSpec:
    """Parse a hysteresis block: ``{release: <level>}``."""
    if not isinstance(raw, dict):
        raise _fail(path, "hysteresis must be a mapping with a 'release' level")
    _check_keys(raw, {"release"}, path)
    if "release" not in raw or raw["release"] is None:
        raise _fail(path, "hysteresis requires a 'release' level")
    return HysteresisSpec(release=_number(raw["release"], f"{path}.release"))


_PAIR_KINDS = ("pair_ratio", "relative_strength")
_PAIR_KEYS = {"instrument", "reference", "field", "lookback", "max_skew_bars"}


def _pair_operand(raw: Any, path: str) -> Optional[Operand]:
    """Recognize a cross-instrument pair operand (Phase 4 F10).

    ``{pair_ratio: {...}}`` / ``{relative_strength: {...}}`` are captured here
    rather than falling through to the unnamed-indicator branch, so the
    compiler sees a typed ``kind="pair"`` operand with named parameters.
    """
    present = [key for key in _PAIR_KINDS if key in raw]
    if not present:
        return None
    if len(present) > 1:
        raise _fail(path, f"operand mixes pair kinds: {', '.join(sorted(present))}")
    if len(raw) != 1:
        extra = sorted(set(raw) - {present[0]})
        raise _fail(
            f"{path}.{present[0]}",
            f"pair operand cannot be combined with other keys: {', '.join(extra)}",
        )
    kind = present[0]
    spec = raw[kind]
    if not isinstance(spec, dict):
        raise _fail(f"{path}.{kind}", "pair operand must be a mapping")
    _check_keys(spec, _PAIR_KEYS, f"{path}.{kind}")
    ignored = sorted(set(spec) - _PAIR_KEYS)
    if ignored:  # _check_keys already raised; defensive
        raise _fail(f"{path}.{kind}", f"unknown keys: {', '.join(ignored)}")
    return Operand(kind="pair", name=kind, params=dict(spec))


def _operand(raw: Any, path: str) -> Operand:
    if raw is None:
        return Operand(kind="value")
    if isinstance(raw, bool):
        raise _fail(path, f"boolean is not a valid operand, got {raw!r}")
    if isinstance(raw, (int, float)):
        return Operand(kind="value", value=float(raw))
    if isinstance(raw, str):
        raise _fail(path, f"operand must be a number or a mapping, got {raw!r}")
    if not isinstance(raw, dict):
        raise _fail(path, f"operand must be a number or a mapping, got {raw!r}")

    if "kind" in raw:
        # explicit (serialized) form: {kind, name, value, params, source, offset}
        _check_keys(raw, {"kind", "name", "value", "params", "source", "offset"}, path)
        kind = _require_str(raw, "kind", path)
        if kind not in ("field", "value", "indicator", "pair"):
            raise _fail(f"{path}.kind", f"unknown operand kind '{kind}'")
        name = _optional_str(raw, "name", path)
        value: Any = None
        if raw.get("value") is not None:
            value = _number(raw["value"], f"{path}.value")
        params_raw = raw.get("params", {})
        if not isinstance(params_raw, dict):
            raise _fail(f"{path}.params", "must be a mapping")
        offset_raw = raw.get("offset")
        offset = offset_raw if isinstance(offset_raw, int) and not isinstance(offset_raw, bool) else None
        return Operand(
            kind=kind,
            name=name,
            value=value,
            params=dict(params_raw),
            source=_optional_str(raw, "source", path),
            offset=offset,
        )

    # lenient shorthand form: {field}|{indicator}|{value} plus extra keys that
    # become indicator params (e.g. {indicator: ema, period: 200}) or capture
    # future expression syntax (e.g. {multiply: [...]}) as an unnamed
    # indicator operand. Contents are validated later, not here.
    field_name = raw.get("field")
    indicator = raw.get("indicator")
    value = raw.get("value")
    pair = _pair_operand(raw, path)
    if pair is not None:
        return pair
    extras = {k: v for k, v in raw.items() if k not in ("field", "indicator", "value")}
    params: dict = {}
    if "params" in extras:
        params_value = extras.pop("params")
        if not isinstance(params_value, dict):
            raise _fail(f"{path}.params", "must be a mapping")
        params.update(params_value)
    params.update(extras)

    if field_name is not None:
        if not isinstance(field_name, str) or not field_name:
            raise _fail(f"{path}.field", f"must be a non-empty string, got {field_name!r}")
        return Operand(kind="field", name=field_name, params=params)
    if indicator is not None:
        if not isinstance(indicator, str) or not indicator:
            raise _fail(f"{path}.indicator", f"must be a non-empty string, got {indicator!r}")
        source = params.pop("source", None)
        offset = params.pop("offset", None)
        offset_val = offset if isinstance(offset, int) and not isinstance(offset, bool) else None
        return Operand(
            kind="indicator",
            name=indicator,
            params=params,
            source=source if isinstance(source, str) else None,
            offset=offset_val,
        )
    if value is not None:
        return Operand(kind="value", value=_number(value, f"{path}.value"), params=params)
    # future expression syntax: capture whole mapping as an unnamed indicator
    return Operand(kind="indicator", name=None, params=dict(raw))


# --------------------------------------------------------------------------
# alerts
# --------------------------------------------------------------------------


def _alert(raw: Any, index: int) -> AlertSpec:
    if not isinstance(raw, dict):
        raise _fail(f"document.alerts[{index}]", "alert must be a mapping")
    if "id" not in raw:
        raise _fail(f"document.alerts[{index}]", "alert requires an 'id'")
    aid = _require_str(raw, "id", f"document.alerts[{index}]")
    path = f"document.alerts.{aid}"
    _check_keys(
        raw,
        {
            "id", "source", "trigger", "repeat", "reminder_interval", "reminder_interval_s",
            "cooldown", "cooldown_s", "rearm_above", "rearm_below", "rearm_level",
            "rearm_direction", "notify_if_already_true",
            "expires", "expires_at", "channels", "message", "scope",  # scope: reserved
            # Phase 4 F10
            "max_per_session", "session_cap_reset",
        },
        path,
    )

    source = _require_str(raw, "source", path)

    trigger: Any = raw.get("trigger")
    if trigger is not None:
        if not isinstance(trigger, str) or not trigger:
            raise _fail(f"{path}.trigger", f"must be a non-empty string, got {trigger!r}")
        if trigger == _ON_SIGNAL:  # proposal-v1 alias
            trigger = "on_transition"
    repeat = raw.get("repeat")
    if repeat is not None:
        if not isinstance(repeat, bool):
            raise _fail(f"{path}.repeat", f"must be a boolean, got {repeat!r}")
        repeat_trigger = "on_transition" if repeat else "once"
        if trigger is not None and trigger != repeat_trigger:
            raise _fail(
                f"{path}.repeat",
                f"conflicts with trigger='{trigger}' (repeat={repeat} implies '{repeat_trigger}')",
            )
        trigger = repeat_trigger

    cooldown_s: Any = None
    if "cooldown_s" in raw and raw["cooldown_s"] is not None:
        cooldown_s = _positive_int(raw["cooldown_s"], f"{path}.cooldown_s")
    if "cooldown" in raw and raw["cooldown"] is not None:
        seconds = _duration_seconds(raw["cooldown"], f"{path}.cooldown")
        if cooldown_s is not None and cooldown_s != seconds:
            raise _fail(f"{path}.cooldown", f"conflicts with cooldown_s={cooldown_s}")
        cooldown_s = seconds

    reminder_interval_s: Any = None
    if "reminder_interval_s" in raw and raw["reminder_interval_s"] is not None:
        reminder_interval_s = _positive_int(raw["reminder_interval_s"], f"{path}.reminder_interval_s", minimum=1)
    if "reminder_interval" in raw and raw["reminder_interval"] is not None:
        seconds = _duration_seconds(raw["reminder_interval"], f"{path}.reminder_interval")
        if reminder_interval_s is not None and reminder_interval_s != seconds:
            raise _fail(f"{path}.reminder_interval", f"conflicts with reminder_interval_s={reminder_interval_s}")
        reminder_interval_s = seconds

    rearm_level: Any = None
    rearm_direction: Any = None
    if "rearm_above" in raw and raw["rearm_above"] is not None:
        rearm_level = _number(raw["rearm_above"], f"{path}.rearm_above")
        rearm_direction = "above"
    if "rearm_below" in raw and raw["rearm_below"] is not None:
        if rearm_level is not None:
            raise _fail(path, "cannot specify both rearm_above and rearm_below")
        rearm_level = _number(raw["rearm_below"], f"{path}.rearm_below")
        rearm_direction = "below"
    if "rearm_level" in raw and raw["rearm_level"] is not None:
        # canonical (serialized) form: level + direction together
        level = _number(raw["rearm_level"], f"{path}.rearm_level")
        direction = _optional_str(raw, "rearm_direction", path)
        if direction not in ("above", "below"):
            raise _fail(f"{path}.rearm_direction", "must be 'above' or 'below'")
        if rearm_level is not None and (rearm_level != level or rearm_direction != direction):
            raise _fail(path, "cannot combine rearm_above/rearm_below with rearm_level/rearm_direction")
        rearm_level, rearm_direction = level, direction

    notify_if_already_true = raw.get("notify_if_already_true", False)
    if not isinstance(notify_if_already_true, bool):
        raise _fail(
            f"{path}.notify_if_already_true",
            f"must be a boolean, got {notify_if_already_true!r}",
        )

    expires_at = _optional_str(raw, "expires_at", path)
    if "expires" in raw and raw["expires"] is not None:
        expires_alias = _require_str(raw, "expires", path)
        if expires_at is not None and expires_at != expires_alias:
            raise _fail(f"{path}.expires", f"conflicts with expires_at='{expires_at}'")
        expires_at = expires_alias

    channels_raw = raw.get("channels", [])
    if not isinstance(channels_raw, list):
        raise _fail(f"{path}.channels", "must be a list of channel names")
    channels = []
    for i, ch in enumerate(channels_raw):
        if not isinstance(ch, str) or not ch:
            raise _fail(f"{path}.channels[{i}]", f"must be a non-empty string, got {ch!r}")
        channels.append(ch)

    message = _optional_str(raw, "message", path)

    max_per_session = _optional_int(raw, "max_per_session", path)
    session_cap_reset = _optional_str(raw, "session_cap_reset", path)

    return AlertSpec(
        id=aid,
        source=source,
        trigger=trigger if trigger is not None else "on_transition",
        reminder_interval_s=reminder_interval_s,
        cooldown_s=cooldown_s,
        rearm_level=rearm_level,
        rearm_direction=rearm_direction,
        notify_if_already_true=notify_if_already_true,
        expires_at=expires_at,
        channels=tuple(channels),
        message=message,
        max_per_session=max_per_session,
        session_cap_reset=session_cap_reset,
    )
