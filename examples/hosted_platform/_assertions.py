"""Hard acceptance assertions for the Phase 5 harness.

The harness is evidence, so a run that produced no request, no execution event,
no paper order, no attributed position or no proof of settlement FAILS rather
than printing a row count. Two rules keep the assertions honest:

* **A dispatch is not a fill and a terminal request is not a settled book.**
  ``settled`` is decided by the platform's own four-axis assessment
  (``SettlementService`` via ``POST /settlement/assess``), never by "there are
  positions and nothing is dispatching".
* **The options lane keeps its own book.** Its acceptance axis is the durable
  option run (closed, with no outstanding legs) plus the four axes, not an equity
  position projection that an option structure never writes to.

Imported by ``run_phase5_acceptance`` only; strategies never import it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

#: The four core settlement axes, named exactly as ``backend.strategies.settlement``
#: names them (``AXIS_*``) and as the settlement API returns them. Domain-adapter
#: axes join the rollup but are not required here.
CORE_SETTLEMENT_AXES = (
    "quiescence",
    "attribution_scoped_flatness",
    "terminal_domain_state",
    "no_live_evaluation_authority",
)


def _orders_by_symbol(paper_orders: List[Mapping[str, Any]]) -> Dict[str, int]:
    totals: Dict[str, int] = {}
    for row in paper_orders:
        symbol = str(row.get("tradingsymbol") or "").strip().upper()
        if not symbol:
            continue
        totals[symbol] = totals.get(symbol, 0) + int(row.get("quantity") or 0)
    return totals


def _position_totals(positions: List[Mapping[str, Any]]) -> Dict[str, int]:
    totals: Dict[str, int] = {}
    for row in positions:
        symbol = str(row.get("tradingsymbol") or "").strip().upper()
        if not symbol:
            continue
        totals[symbol] = totals.get(symbol, 0) + int(row.get("net_quantity") or 0)
    return totals


def _settlement_axes(evidence: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    settlement = evidence.get("settlement")
    if not isinstance(settlement, Mapping):
        return {}
    axes = settlement.get("axes")
    return dict(axes) if isinstance(axes, Mapping) else {}


def assert_scenario(
    label: str,
    spec: Mapping[str, Any],
    evidence: Mapping[str, Any],
    supervisor: Mapping[str, Any],
) -> Dict[str, Any]:
    """The acceptance axes for one example scenario."""
    failures: List[str] = []
    requests = list(evidence.get("requests") or [])
    events = list(evidence.get("execution_events") or [])
    projections = list(evidence.get("attributed_positions") or [])
    paper_orders = list(evidence.get("paper_orders") or [])
    option_runs = list(evidence.get("option_runs") or [])
    expected_requests = int(spec.get("expected_requests") or 1)
    expected_orders = spec.get("expected_orders")
    allowed_statuses = set(spec.get("allowed_request_statuses") or ["executed"])
    child_log = str(evidence.get("child_log") or "")

    # -- the child is the decision maker ------------------------------------
    outcome = str(supervisor.get("outcome") or "")
    exit_code = supervisor.get("exit_code")
    wanted_exit = int(spec.get("expected_exit_code") or 0)
    if outcome != "exited":
        failures.append(
            f"the child never exited on its own (supervisor outcome={outcome!r}, "
            f"stop={supervisor.get('stop')!r})"
        )
    elif int(exit_code if exit_code is not None else -1) != wanted_exit:
        failures.append(f"the child exited {exit_code} instead of {wanted_exit}")
    marker = str(spec.get("final_marker") or "")
    if marker and marker not in child_log:
        failures.append(f"the child log does not contain its final marker {marker!r}")
    if not child_log.strip():
        failures.append("the child produced no log, so nothing was decided")

    if spec.get("expects_deferral"):
        # An example that must NOT submit anything is verified by the ABSENCE of a
        # request plus the child's own named reason. Claiming a green "executed"
        # run here would be the false result.
        if requests:
            failures.append(
                f"expected no execution request, saw {len(requests)} "
                "(a deferred example must not submit anything)"
            )
        if paper_orders:
            failures.append(
                f"a deferred example placed {len(paper_orders)} paper order(s); "
                "a refusal must not reach the order boundary"
            )
        for deferral_marker in spec.get("deferral_markers") or []:
            if str(deferral_marker) not in child_log:
                failures.append(f"the child log does not name {deferral_marker!r}")
        return {
            "label": label,
            "ok": not failures,
            "failures": failures,
            "axes": {
                "deferred": True,
                "requests_created": len(requests),
                "paper_orders": len(paper_orders),
                "child_named_reason": bool(child_log),
                "child_exit_code": exit_code,
            },
        }

    # -- the governed request ledger ---------------------------------------
    if len(requests) != expected_requests:
        failures.append(f"expected exactly {expected_requests} requests, saw {len(requests)}")
    statuses = [str(row.get("status")) for row in requests]
    unexpected = sorted({status for status in statuses if status not in allowed_statuses})
    if unexpected:
        failures.append(f"request statuses outside {sorted(allowed_statuses)}: {unexpected}")
    sequence = list(spec.get("expected_status_sequence") or [])
    if sequence and statuses != sequence:
        failures.append(f"request status sequence {statuses} != {sequence}")

    # -- the execution trail ------------------------------------------------
    if not events:
        failures.append("no strategy_plan_execution_events row was written")
    submitted_events = [row for row in events if str(row.get("event")) == "submitted"]
    executed = [row for row in requests if str(row.get("status")) == "executed"]
    if executed and not submitted_events:
        failures.append("a request executed but no 'submitted' trail event exists")

    # -- paper orders: exact counts, quantities and shapes ------------------
    if not paper_orders:
        failures.append("no paper order exists")
    if isinstance(expected_orders, int) and len(paper_orders) != expected_orders:
        failures.append(f"expected exactly {expected_orders} paper orders, saw {len(paper_orders)}")
    for row in spec.get("expected_order_rows") or []:
        symbol = str(row.get("tradingsymbol") or "").upper()
        match = [
            order
            for order in paper_orders
            if str(order.get("tradingsymbol") or "").upper() == symbol
            and str(order.get("transaction_type") or "").upper()
            == str(row.get("transaction_type") or "").upper()
            and int(order.get("quantity") or 0) == int(row.get("quantity") or 0)
        ]
        if not match:
            failures.append(
                f"no paper order matches {row} (saw "
                f"{[(o.get('tradingsymbol'), o.get('transaction_type'), o.get('quantity')) for o in paper_orders]})"
            )

    # The money ceiling: the frozen plan's own reference prices times the quantities
    # that were actually ordered must stay inside the declared budget. A plan that
    # spends past its budget is a real finding, not a rounding detail.
    max_notional = spec.get("max_notional_inr")
    if max_notional:
        prices: Dict[str, float] = {}
        for plan in evidence.get("plans") or []:
            resolved = dict((plan or {}).get("resolved_plan") or {})
            for leg in resolved.get("legs") or []:
                if not isinstance(leg, dict):
                    continue
                price = leg.get("reference_price")
                if price is None:
                    continue
                prices[str(leg.get("tradingsymbol") or "").upper()] = float(price)
        notional = 0.0
        for order in paper_orders:
            symbol = str(order.get("tradingsymbol") or "").upper()
            if symbol in prices:
                notional += int(order.get("quantity") or 0) * prices[symbol]
        if notional > float(max_notional) + 1.0:
            failures.append(
                f"the ordered notional {round(notional, 2)} exceeds the declared budget "
                f"{max_notional}"
            )

    # -- open exposure, or a closed option structure ------------------------
    if bool(spec.get("requires_option_close")):
        option_statuses = {str(row.get("run_status") or "").lower() for row in option_runs}
        # EVERY run this strategy's bindings point at must be closed: a single
        # closed run next to an open one is not a closed structure.
        option_closed = bool(option_runs) and option_statuses.issubset(
            {"closed", "settled", "exited"}
        )
        if not option_runs:
            failures.append("no option run was created for this strategy")
        else:
            if not option_closed:
                failures.append(
                    "an option run was never closed "
                    f"(statuses={sorted(option_statuses)})"
                )
            entry_edges = [r for r in option_runs if str(r.get("phase")) == "entry"]
            exit_edges = [r for r in option_runs if str(r.get("phase")) == "exit"]
            if not entry_edges:
                failures.append("no option-run binding exists for the entry plan")
            if not exit_edges:
                failures.append("no option-run binding exists for the close plan")
            if len(exit_edges) > 1:
                failures.append(
                    f"{len(exit_edges)} close plans were submitted; a repeated observation "
                    "must not duplicate the adjustment"
                )
    else:
        expected_positions = {
            str(symbol).upper(): int(quantity)
            for symbol, quantity in dict(spec.get("expected_positions") or {}).items()
        }
        if expected_positions:
            totals = _position_totals(projections)
            for symbol, quantity in expected_positions.items():
                if totals.get(symbol) != quantity:
                    failures.append(
                        f"attributed position {symbol} is {totals.get(symbol)} instead of {quantity}"
                    )
            if projections and not all(
                int(row.get("projection_version") or 0) >= 1 for row in projections
            ):
                failures.append("an attributed position row carries no published projection version")
        elif not projections:
            failures.append("a request executed but no attributed position was published")

        # The paper orders and the attributed book must agree on the NET traded
        # quantity per symbol: an order that never reached the book (or a book that
        # shows a fill no order explains) is a real discrepancy, not a row count.
        traded: Dict[str, int] = {}
        for order in paper_orders:
            symbol = str(order.get("tradingsymbol") or "").strip().upper()
            if not symbol:
                continue
            sign = 1 if str(order.get("transaction_type") or "").upper() == "BUY" else -1
            traded[symbol] = traded.get(symbol, 0) + sign * int(order.get("quantity") or 0)
        totals = _position_totals(projections)
        for symbol, net in sorted(traded.items()):
            if totals.get(symbol, 0) != net:
                failures.append(
                    f"the attributed book for {symbol} is {totals.get(symbol, 0)} while its "
                    f"paper orders net to {net}"
                )

    # -- settlement: the platform's own four axes ---------------------------
    axes = _settlement_axes(evidence)
    if not axes:
        failures.append("the four-axis settlement assessment was not collected")
    else:
        missing_axes = [name for name in CORE_SETTLEMENT_AXES if name not in axes]
        if missing_axes:
            failures.append(f"the settlement assessment is missing axes {missing_axes}")
        for name, want in dict(spec.get("expected_settlement_axes") or {}).items():
            got = str((axes.get(name) or {}).get("state") or "missing")
            if got != want:
                failures.append(f"settlement axis {name} is {got!r} instead of {want!r}")

    # -- an owner decision may not precede any order ------------------------
    if spec.get("expects_manual"):
        before = evidence.get("orders_before_approval")
        if before is None:
            failures.append("the manual request was never observed waiting for its owner decision")
        elif int(before) != 0:
            failures.append(f"{before} paper order(s) existed before the owner decision")

    settlement_overall = None
    if isinstance(evidence.get("settlement"), Mapping):
        settlement_overall = evidence["settlement"].get("overall")

    axes_out = {
        "requests_created": len(requests),
        "requests_executed": len(executed),
        "request_statuses": statuses,
        "execution_events": len(events),
        "submitted_events": len(submitted_events),
        "attributed_positions": len(projections),
        "paper_orders": len(paper_orders),
        "paper_order_quantities": _orders_by_symbol(paper_orders),
        "option_runs": len(option_runs),
        "option_runs_closed": int(
            bool(option_runs)
            and {str(row.get("run_status") or "").lower() for row in option_runs}.issubset(
                {"closed", "settled", "exited"}
            )
        ),
        "settlement_overall": settlement_overall,
        "settlement_axes": {
            name: str((axes.get(name) or {}).get("state") or "missing")
            for name in CORE_SETTLEMENT_AXES
        },
        "child_outcome": outcome,
        "child_exit_code": exit_code,
        "orders_before_approval": evidence.get("orders_before_approval"),
    }
    return {"label": label, "ok": not failures, "failures": failures, "axes": axes_out}


def assert_recovery(scenario: Mapping[str, Any]) -> Dict[str, Any]:
    """The recovery matrix must produce the exact evidence-backed outcome map."""
    failures: List[str] = []
    statuses = dict(scenario.get("statuses") or {})
    outcomes = dict(scenario.get("outcome_states") or {})
    counts = dict(scenario.get("counts") or {})
    expected_status = {
        "withheld": "dispatch_unresolved",
        "accepted": "executed",
        "rejected": "refused",
        "uncertain": "dispatch_unresolved",
    }
    expected_outcome = {
        "withheld": "not_submitted",
        "accepted": "submitted",
        "rejected": "rejected",
        "uncertain": "unknown",
    }
    for name, want in expected_status.items():
        got = str(statuses.get(name) or "")
        if got != want:
            failures.append(f"{name}: status {got!r} != {want!r}")
    for name, want in expected_outcome.items():
        got = str(outcomes.get(name) or "")
        if got != want:
            failures.append(f"{name}: outcome {got!r} != {want!r}")
    if int(counts.get("proved_submitted") or 0) != 1:
        failures.append("exactly one claim should be proven submitted")
    if int(counts.get("proved_rejected") or 0) != 1:
        failures.append("exactly one claim should be proven authoritatively rejected")
    if int(counts.get("unresolved") or 0) != 2:
        failures.append("two claims should stay unresolved")
    if int(scenario.get("still_dispatching") or 0) != 0:
        failures.append("an abandoned claim is still in 'dispatching'")
    return {"ok": not failures, "failures": failures}
