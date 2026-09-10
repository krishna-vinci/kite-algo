"""Alert message building: default explainable template + user override.

`build_message` renders the human-facing (subject, body) for one alert
occurrence. Default formatting follows spec F11/F3: a stable, explainable
summary carrying rule, symbol, evidence, event time (IST and UTC) and the
event id (E-22 dedup-by-eye). A user template may override the body with a
fixed placeholder set — ``${symbol}``, ``${level}``, ``${ltp}``, ``${time}``,
``${rule}``, ``${event_id}`` — unknown/missing values render as ``-``.

Both parts are hard-capped (E-14): subject <= 120 chars, body <= 3800 chars
with a ``…[truncated]`` marker, so provider limits are never the failure mode.

Stdlib only — no redis/sqlalchemy/httpx needed here.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional, Tuple

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")
SUBJECT_MAX = 120
BODY_MAX = 3800
TRUNCATION_MARKER = "…[truncated]"

_PLACEHOLDER_RE = re.compile(r"\$\{(\w+)\}")

# Placeholders resolvable from the alert context; anything else renders as "-".
_CONTEXT_KEYS = ("symbol", "rule", "event_id")


def _as_utc(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def format_event_time(moment: dt.datetime) -> Tuple[str, str]:
    """Return (IST string, UTC string) in the fixed display formats."""
    utc = _as_utc(moment)
    ist = utc.astimezone(IST)
    ist_str = ist.strftime("%Y-%m-%d %H:%M:%S IST")
    utc_str = utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    return ist_str, utc_str


def build_message(
    *,
    rule_name: str,
    instrument_key: str,
    evidence: dict,
    fired_at: dt.datetime,
    template: Optional[str] = None,
    event_id: Optional[str] = None,
) -> Tuple[str, str]:
    """Build (subject, body) for a fired alert occurrence."""
    subject = _cap_subject(f"[Alert] {rule_name}: {instrument_key}")
    ist_str, utc_str = format_event_time(fired_at)

    if template:
        body = _render_template(
            template,
            rule_name=rule_name,
            instrument_key=instrument_key,
            evidence=evidence,
            event_id=event_id,
            ist_str=ist_str,
        )
    else:
        body = _default_body(
            rule_name=rule_name,
            instrument_key=instrument_key,
            evidence=evidence,
            event_id=event_id,
            ist_str=ist_str,
            utc_str=utc_str,
        )
    if event_id is not None and str(event_id) not in body:
        body = f"{body}\nevent_id: {event_id}"
    return subject, _cap_body(body, event_id=event_id)


def _default_body(
    *,
    rule_name: str,
    instrument_key: str,
    evidence: dict,
    event_id: Optional[str],
    ist_str: str,
    utc_str: str,
) -> str:
    values = " ".join(f"{key}={evidence[key]}" for key in sorted(evidence))
    lines = [
        f"rule: {rule_name}",
        f"symbol: {instrument_key}",
        f"values: {values}",
    ]
    if "condition" in evidence:
        lines.append(f"condition: {evidence['condition']}")
    if "timeframe" in evidence:
        lines.append(f"timeframe: {evidence['timeframe']}")
    lines.append(f"time: {ist_str} ({utc_str})")
    lines.append(f"event_id: {event_id if event_id is not None else '-'}")
    return "\n".join(lines)


def build_screener_message(
    *,
    screener_name: str,
    evidence: dict,
    event_id: Optional[str],
    ist_str: str,
    utc_str: str,
    instrument_key: str,
) -> Tuple[str, str]:
    """Screener attachment notification: explains the screener, the trigger
    and action, the symbol's ranks/values, the run time and data freshness."""
    subject = _cap_subject(f"[Screener] {screener_name}: {instrument_key}")
    lines = [
        f"screener: {screener_name}",
        f"trigger: {evidence.get('trigger', '-')} ({evidence.get('action', '-')})",
        f"symbol: {instrument_key}",
    ]
    rank = evidence.get("rank")
    prev_rank = evidence.get("prev_rank")
    if rank is not None:
        rank_line = f"rank: {rank}"
        if prev_rank is not None:
            rank_line += f" (prev {prev_rank}"
            if evidence.get("rank_delta") is not None:
                rank_line += f", delta {evidence['rank_delta']}"
            rank_line += ")"
        lines.append(rank_line)
    values = evidence.get("values") or {}
    if isinstance(values, dict) and values:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(values.items()))
        lines.append(f"values: {rendered}")
    scheduled_for = evidence.get("scheduled_for")
    if scheduled_for:
        lines.append(f"run_as_of: {scheduled_for}")
    lines.append(f"time: {ist_str} ({utc_str})")
    lines.append(f"event_id: {event_id if event_id is not None else '-'}")
    return subject, _cap_body("\n".join(lines), event_id=event_id)


def _render_template(
    template: str,
    *,
    rule_name: str,
    instrument_key: str,
    evidence: dict,
    event_id: Optional[str],
    ist_str: str,
) -> str:
    flattened = _flatten_condition_evidence(evidence)
    values: dict[str, str] = {
        "symbol": instrument_key,
        "rule": rule_name,
        "time": ist_str,
        "event_id": event_id if event_id is not None else "-",
        "level": flattened.get("level", "-"),
        "ltp": flattened.get("ltp", "-"),
    }

    def _replace(match: re.Match[str]) -> str:
        return values.get(match.group(1), "-")

    return _PLACEHOLDER_RE.sub(_replace, template)


def _evidence_str(evidence: dict, key: str) -> str:
    if key not in evidence or evidence[key] is None:
        return "-"
    return str(evidence[key])


def _flatten_condition_evidence(evidence: dict) -> dict[str, str]:
    """Expose unambiguous aliases while retaining nested evidence in output.

    Predicate evidence is keyed by condition identity. A single condition can
    safely provide ``${ltp}``/``${level}``; with multiple conflicting
    conditions the placeholder becomes ``-`` instead of silently selecting a
    condition. Equal values remain usable (for example two conditions sharing
    one threshold).
    """
    values: dict[str, list[str]] = {}
    for key, value in evidence.items():
        if not isinstance(value, dict):
            if key in {"ltp", "level"} and value is not None:
                values.setdefault(key, []).append(str(value))
            continue
        for nested_key in ("ltp", "level"):
            nested_value = value.get(nested_key)
            if nested_value is not None:
                values.setdefault(nested_key, []).append(str(nested_value))
    flattened: dict[str, str] = {}
    for key, candidates in values.items():
        unique = list(dict.fromkeys(candidates))
        if len(unique) == 1:
            flattened[key] = unique[0]
    return flattened


def _cap_subject(subject: str) -> str:
    return subject[:SUBJECT_MAX]


def _cap_body(body: str, *, event_id: Optional[str] = None) -> str:
    if len(body) <= BODY_MAX:
        return body
    suffix = ""
    if event_id is not None:
        suffix = f"\nevent_id: {event_id}"
    keep = BODY_MAX - len(TRUNCATION_MARKER) - len(suffix)
    if keep < 0:
        suffix = suffix[-(BODY_MAX - len(TRUNCATION_MARKER)):]
        keep = 0
    return body[:keep] + TRUNCATION_MARKER + suffix
