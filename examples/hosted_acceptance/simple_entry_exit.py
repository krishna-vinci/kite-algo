"""A deterministic single-instrument hosted paper strategy (Bundle 4 example).

The whole decision logic, so a reader can separate it from platform setup:

1. the job's ``params`` carry a short synthetic price series plus the entry and
   exit levels;
2. when the series first prints above ``entry_price`` the strategy proposes a
   BUY of ``quantity`` (``target_kind="single_instrument"``);
3. when the series then prints at or below ``exit_price`` it proposes the exit,
   an ABSOLUTE flat target (so a reducing order against an unfilled entry is a
   no-op, never a short);
4. the child then keeps its attempt ALIVE (progress reports) until the platform
   stops it, because the platform - not the child - sequences the executions:
   the operator reserves and executes the entry, publishes the attributed
   position, then executes the exit. Every exposure increase therefore happens
   while the attempt still holds authority; the supervisor terminates the child
   when the operator stops the job (or the observe bound elapses).

The child never observes fills: paper order-inspection endpoints are live-only,
and a child-side "wait for fill" would be an unsupported contract. The operator's
execution order is the ordering guarantee, and it is recorded in the platform's
own trails. Prices are synthetic and orders are paper. The child is handed a
worker token, a run id and a session nonce - no database, broker or supervisor
credential.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List


def _prices(params: Dict[str, Any]) -> List[float]:
    raw = params.get("prices") or []
    if not raw:
        raise RuntimeError("this example needs params['prices'] (a synthetic series)")
    return [float(value) for value in raw]


def main(ctx) -> int:  # noqa: ANN001 - the hosted contract is main(ctx)
    """Propose the entry, then the exit, and report progress.

    The platform sequences the EXECUTIONS, not the child: the child proposes the
    two decisions (the p3 evaluation), and the operator reserves/executes them in
    the recorded order (entry first). Each plan carries an ABSOLUTE target
    quantity, so the exit plan means "the book is flat" whenever it executes -
    a reducing order against an unfilled entry is a no-op, never a short.
    """
    params: Dict[str, Any] = dict(ctx.params or {})
    symbol = str(params.get("tradingsymbol") or "RELIANCE")
    quantity = int(params.get("quantity") or 1)
    entry_price = float(params.get("entry_price") or 0.0)
    exit_price = float(params.get("exit_price") or 0.0)
    reference_price = float(params.get("reference_price") or entry_price or 1.0)
    prices = _prices(params)

    strategy_id = str(params["strategy_id"])
    account_scope = str(ctx.run.run.get("account_scope") or ctx.run.config.account_scope)
    common = {
        "evaluation_kind": "run_now",
        "strategy_run_id": ctx.run_id,
        "strategy_id": strategy_id,
        "account_scope": account_scope,
        "target_kind": "single_instrument",
    }
    leg = {
        "instrument_token": int(params.get("instrument_token") or 738561),
        "exchange": str(params.get("exchange") or "NSE"),
        "tradingsymbol": symbol,
        "product": str(params.get("product") or "CNC"),
        "reference_price": reference_price,
    }

    ctx.progress("hosted example attached; replaying the synthetic series")
    proposed_entry = False
    proposed_exit = False
    for index, price in enumerate(prices, start=1):
        if not proposed_entry and price > entry_price:
            proposed_entry = True
            payload = dict(common, evaluation_id=f"{ctx.run_id}:entry:{index}")
            payload["payload"] = dict(leg, target_quantity=quantity)
            ctx.run.submit_proposal(payload)
            ctx.progress(f"entry proposed (target {quantity}) at synthetic price {price}")
        elif proposed_entry and not proposed_exit and price <= exit_price:
            proposed_exit = True
            payload = dict(common, evaluation_id=f"{ctx.run_id}:exit:{index}")
            payload["payload"] = dict(leg, target_quantity=0)
            ctx.run.submit_proposal(payload)
            ctx.progress(f"exit proposed (target flat) at synthetic price {price}")
    if not (proposed_entry and proposed_exit):
        raise RuntimeError("the synthetic series never produced the exit condition")

    # Stay alive while the operator executes (authority intact), reporting
    # progress. The supervisor terminates this process when the operator stops
    # the job; a bounded loop keeps a stuck platform from pinning the child
    # forever.
    import time as _time

    deadline = _time.monotonic() + float(params.get("alive_seconds") or 300)
    while _time.monotonic() < deadline:
        try:
            ctx.run.refresh()
        except Exception as exc:  # noqa: BLE001 - a released attempt is the stop signal
            ctx.progress(f"run no longer readable ({exc}); finishing")
            return 0
        ctx.progress("both proposals submitted; awaiting platform execution")
        _time.sleep(float(params.get("alive_poll_seconds") or 1.0))
    ctx.progress("alive bound reached; finishing")
    return 0
