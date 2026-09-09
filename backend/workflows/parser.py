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
    Condition,
    DataPolicy,
    InstrumentRef,
    Operand,
    Stage,
    WorkflowDocument,
)

__all__ = ["WorkflowParseError", "parse_workflow_yaml", "parse_workflow_dict"]

MAX_DOCUMENT_BYTES = 256 * 1024
_MAX_NODES = 50_000
_MAX_DEPTH = 50

_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$")
_DURATION_UNITS_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Proposal-v1 names normalized into the v2 contract.
_EVALUATE_ON_TO_CLOCK = {"ltp": "ltp", "candle_close": "candle_close"}
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
    value = raw[key]
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _fail(f"{path}.{key}", f"must be a non-empty string, got {value!r}")
    return value


def _optional_str(raw: dict, key: str, path: str) -> Any:
    if key not in raw or raw[key] is None:
        return None
    return _require_str(raw, key, path)


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
         "session", "timezone", "universe"},  # timezone/universe: reserved, ignored
        "document",
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

    return WorkflowDocument(
        version=1,
        name=name,
        session=session,
        instruments=instruments,
        stages=stages,
        alerts=alerts,
        data_policy=data_policy,
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
        {"id", "type", "clock", "timeframe", "conditions", "input", "evaluate_on"},
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
    stage_input = _optional_str(raw, "input", path)

    if "conditions" not in raw:
        raise _fail(path, "stage requires 'conditions'")
    conditions = _conditions(raw["conditions"], f"{path}.conditions")

    return Stage(
        id=sid,
        type=stage_type,
        clock=clock,  # type: ignore[arg-type]  # unknown clocks are flagged by the compiler
        timeframe=timeframe,
        conditions=conditions,
        input=stage_input,
    )


def _conditions(raw: Any, path: str) -> tuple[Condition, ...]:
    if isinstance(raw, dict):
        for key in raw:
            if not isinstance(key, str) or key != "all":
                raise _unknown_field(path, key)
        items = raw["all"]
    elif isinstance(raw, list):
        items = raw
    else:
        raise _fail(path, "must be a mapping with 'all' or a list of conditions")
    if not isinstance(items, list):
        raise _fail(f"{path}.all", "must be a list of conditions")
    return tuple(_condition(item, f"{path}[{i}]") for i, item in enumerate(items))


def _condition(raw: Any, path: str) -> Condition:
    if not isinstance(raw, dict):
        raise _fail(path, f"condition must be a mapping, got {raw!r}")
    _check_keys(raw, {"left", "op", "right", "field", "value"}, path)

    if "op" not in raw:
        raise _fail(f"{path}.op", "condition requires an operator 'op'")
    op = _require_str(raw, "op", path)

    if "left" in raw or "right" in raw:
        if "left" not in raw or "right" not in raw:
            raise _fail(path, "full condition form requires both 'left' and 'right'")
        return Condition(
            left=_operand(raw["left"], f"{path}.left"),
            op=op,
            right=_operand(raw["right"], f"{path}.right"),
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
        )

    raise _fail(path, "condition must define either left/op/right or field/op/value")


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
        # explicit (serialized) form: {kind, name, value, params}
        _check_keys(raw, {"kind", "name", "value", "params"}, path)
        kind = _require_str(raw, "kind", path)
        if kind not in ("field", "value", "indicator"):
            raise _fail(f"{path}.kind", f"unknown operand kind '{kind}'")
        name = _optional_str(raw, "name", path)
        value: Any = None
        if raw.get("value") is not None:
            value = _number(raw["value"], f"{path}.value")
        params_raw = raw.get("params", {})
        if not isinstance(params_raw, dict):
            raise _fail(f"{path}.params", "must be a mapping")
        return Operand(kind=kind, name=name, value=value, params=dict(params_raw))

    # lenient shorthand form: {field}|{indicator}|{value} plus extra keys that
    # become indicator params (e.g. {indicator: ema, period: 200}) or capture
    # future expression syntax (e.g. {multiply: [...]}) as an unnamed
    # indicator operand. Contents are validated later, not here.
    field_name = raw.get("field")
    indicator = raw.get("indicator")
    value = raw.get("value")
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
        return Operand(kind="indicator", name=indicator, params=params)
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
    )
