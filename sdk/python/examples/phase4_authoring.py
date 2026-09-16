"""Phase 4 example: author, activate, and observe a workflow over the SDK.

Exercises the alerts-platform surface end to end: capability discovery,
validation, create with an idempotency key, activation, event history, health,
a screener run, a universe, and an external producer whose values expire.

Run against a live worker (never against production data):

    KITE_ALGO_API_BASE=http://localhost:18777 \\
    KITE_ALGO_WORKER_TOKEN=kwa_... \\
    python sdk/python/examples/phase4_authoring.py

The producer credential is printed ONCE by the server and is never retrievable
again; this example keeps it in memory only and never logs it.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kite_algo_worker.client import AlgoWorkerConfig, KiteAlgoWorkerClient  # noqa: E402
from kite_algo_worker.exceptions import KiteAlgoWorkerError  # noqa: E402

DOCUMENT = {
    "version": 1,
    "name": "phase4-advanced-conditions",
    "session": "nse_equity",
    "instruments": ["NSE:INFY", "NSE:TCS"],
    "stages": [
        {
            # Three consecutive closes above a constant threshold, with
            # hysteresis so a boundary oscillation does not re-fire.
            "id": "momentum3",
            "type": "signal",
            "clock": "candle_close",
            "timeframe": "day",
            "conditions": {
                "all": [
                    {"left": {"field": "close"}, "op": "gt", "right": {"value": 1500},
                     "hysteresis": {"release": 1485}},
                ]
            },
            "consecutive_bars": 3,
        },
        {
            # Breakout, then a bounded pullback measured in ELAPSED TIME.
            "id": "pullback",
            "type": "signal",
            "clock": "candle_close",
            "timeframe": "15minute",
            "sequence": {
                "first": {"all": [{"field": "close", "op": "crosses_above",
                                   "value": 1500}]},
                "then": {"all": [{"field": "close", "op": "lt", "value": 1490}]},
                "within": "2h",
            },
        },
        {
            # Windowed distinct-symbol participation: at least 2 different
            # instruments triggering within 30 minutes (NOT simultaneous).
            "id": "breadth2",
            "type": "breadth",
            "clock": "candle_close",
            "timeframe": "5minute",
            "breadth": {
                "condition": {"all": [{"field": "close", "op": "gt", "value": 1400}]},
                "distinct_instruments": 2,
                "window": "30m",
            },
        },
    ],
    "alerts": [
        {"id": "momentum", "source": "momentum3", "trigger": "on_transition",
         "max_per_session": 5, "session_cap_reset": "session"},
        {"id": "pullback-alert", "source": "pullback", "trigger": "once"},
    ],
}


def main() -> int:
    base_url = os.environ.get("KITE_ALGO_API_BASE", "http://localhost:18777")
    token = os.environ.get("KITE_ALGO_WORKER_TOKEN", "")
    if not token:
        print("KITE_ALGO_WORKER_TOKEN is required", file=sys.stderr)
        return 2
    client = KiteAlgoWorkerClient(AlgoWorkerConfig(base_url=base_url, token=token))

    # 1. Capability discovery — exactly what the server can evaluate.
    caps = client.workflow_capabilities()["capabilities"]
    print("stage types:", caps["stage_types"])
    print("breadth modes:", caps["breadth_modes"])
    print("pair formulas:", {name: spec["formula"] for name, spec in caps["pairs"].items()})

    # 2. Validate before creating anything.
    result = client.validate_workflow(document=DOCUMENT)
    print("valid:", result.get("ok"))
    if not result.get("ok"):
        for issue in result.get("issues", []):
            print(f"  {issue['where']}: {issue['message']}")
        return 1

    # 3. Preview: same semantics, no persistence, no deliveries.
    print("preview:", client.preview_workflow(document=DOCUMENT).get("evaluation"))

    # 4. Create once. The idempotency key makes a retry return the SAME
    #    workflow rather than creating a duplicate.
    created = client.create_workflow(
        document=DOCUMENT, idempotency_key="phase4-example-create-0001"
    )
    workflow_id = created["workflow"]["id"]
    print("workflow:", workflow_id, "created:", created.get("created"))

    # 5. Activate, then inspect history and health.
    print("activate:", client.activate_workflow(workflow_id).get("ok"))
    print("events:", client.workflow_events(workflow_id, limit=5).get("total"))
    health = client.workflow_health(workflow_id)
    print("health:", health.get("active_revision"), health.get("delivery_counts"))

    # 6. An external producer with expiring values. The secret is shown ONCE.
    producer = client.create_signal_producer(
        name="phase4-example",
        value_schema={"fields": {"score": "number"}},
        default_ttl_s=900,
    )
    print("producer:", producer["producer"]["name"])
    credential = client.issue_signal_credential("phase4-example")
    secret = credential["secret"]
    print("credential issued (secret kept in memory only):", credential["token_id"])

    now = datetime.now(timezone.utc)
    for offset in (0, 1):
        client.submit_signal_value(
            secret,
            value={"score": 80.0 + offset},
            event_time=(now - timedelta(minutes=1)).isoformat(),
            idempotency_key=f"phase4-example-{offset}",
        )
    values = client.list_signal_values("phase4-example", limit=5)
    print("accepted values:", values["total"])
    print("signals:", client.signals_health()["producers"][0])

    # A consumer reads `external.phase4-example.score` in a condition; the
    # value is SAMPLED at the stage's candle clock, never pushed, so it can
    # expire between evaluations — that is why `expires_at` matters.
    print("done; revoke the producer when finished:",
          f"client.revoke_signal_producer('phase4-example')")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KiteAlgoWorkerError as exc:
        print(f"worker API error: {exc}", file=sys.stderr)
        raise SystemExit(1)
