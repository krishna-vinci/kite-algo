# Hosted Python strategies: user guide

This is the copy-and-paste guide for a hosted strategy: one Python file with
`def main(ctx)`, run by the platform's own supervisor. It covers exactly what is
supported today, what each field means, and what the platform will refuse.

## 1. Where the code goes

On **New hosted strategy** you paste the file into the source editor, or drop a
`.py` file on it. The source is stored as an immutable version of that strategy;
editing it later registers a *new* version, and a new version invalidates any
standing authorization that was bound to the old one.

The only supported entrypoint is:

```python
def main(ctx):
    ...
    return 0
```

`main` must be **synchronous**. The child bootstrap calls `main(ctx)` directly
and uses its return value as the exit code, so an `async def main` returns an
un-awaited coroutine, the process exits without running your code, and nothing
is traded. A file with no module-level `main(ctx)` is refused by the readiness
check before anything is created. The uploaded file is **never executed in the
API or in the browser** - only parsed (`ast`) for the entrypoint and its imports.

## 2. Supported packages

There is one documented runner profile, `hosted-python-dataframe-indicators`
(`python:3.14-slim`). Its packages are exactly:

* `pandas`, `numpy`, `numba`, `dateutil` (`python-dateutil`) - the numerical
  stack;
* `requests`, `httpx`, `websockets` - transport;
* the strategy SDK itself (`kite_algo_worker`).

Indicator computation runs in the **backend** image (`server_side_indicators`),
so `ctx.client.calculate_indicator(...)` needs none of that stack locally.

There is no runtime `pip install` from strategy source (`runtime_pip_install:
false`). The package list and the `main(ctx)` check come from the readiness
contract, `POST /api/strategies/readiness` (typed in the SDK as
`kite_algo_worker.readiness.SourceReadiness`); the composer shows the same
answer. A missing import is reported before launch where it can be determined
statically. Dynamic and guarded imports cannot be certified, so their status is
`unknown`, never `ready`.

## 3. What `ctx` gives you

| Attribute | Meaning |
| --- | --- |
| `ctx.params` | The validated parameter values for this run. |
| `ctx.client` | The SDK client (market data, indicators, options helpers). |
| `ctx.run` | This run: `progress`, `owned_work`, `submit_proposal`, `request_execution`, `submit_and_request_execution`, `execution_request(s)`. |
| `ctx.run_id` | The durable run id. |
| `ctx.execution_mode` | `paper`, `dry_run` or `live` - the environment of this attempt. |
| `ctx.scratch` | A per-job scratch directory (`KITE_ALGO_SCRATCH`), owned by the unprivileged child. |

The strategy and account are **derived from the persisted run**, never from a
parameter. Do not put `strategy_id` in your parameters and do not try to select
an account:

```python
identity = ctx.run.attribution()
if not identity["attributed"]:
    return 0                      # no persisted binding: refuse to guess
strategy_id = identity["strategy_id"]
account_scope = identity["account_id"]          # from the binding, not a param
environment = identity["execution_environment"] # paper | dry_run | live
```

`ctx.scratch` is a convenience workspace, not a sandbox: the child runs as its
own unprivileged user in its own workspace, and nothing about "only this
directory is writable" is promised by the platform.

Useful calls:

```python
ctx.progress("reading the index")                       # child liveness
ctx.client.get_candles("NSE:NIFTY 50", interval="5minute", lookback=60)
ctx.client.get_quotes(["NSE:RELIANCE"], mode="quote")
ctx.client.calculate_indicator({"name": "ema", "bars": bars, "period": 20})
ctx.client.resolve_ticker("NSE:RELIANCE")               # EXCHANGE:SYMBOL
ctx.client.options.get_chain("NIFTY", expiry="2026-10-29")
ctx.client.options.get_greeks("NIFTY", expiry="2026-10-29")
ctx.client.resolve_universe("phase5-equities")
ctx.client.universe_revisions("phase5-equities", limit=1)   # carries revision_id
ctx.run.owned_work()                                    # own book + pending work
```

Catalog coordinates are `EXCHANGE:SYMBOL`. Whether a display name contains a
space is a **catalog** property: this platform's catalog stores the NSE indices
with a space - `NSE:NIFTY 50`, `NSE:NIFTY 500`, `NSE:NIFTY BANK` - while equities
are unspaced (`NSE:RELIANCE`). Do not blanket-strip or blanket-insert spaces in
user input. Resolve the instrument first and use what the catalog returns:

```python
resolved = ctx.client.resolve_ticker("NSE:NIFTY 50")
instrument = resolved.get("instrument") or resolved
coordinate = instrument["public_key"]   # the catalog's own key
```

## 4. Permissions

Choose permissions in the composer; importing or scanning your file grants
nothing.

| Permission | What it allows |
| --- | --- |
| **Read market data** (`data`) | Quotes, candles, indicator computation, option chain/Greeks reads, and resolving *existing* owner-owned universes. |
| **Propose trades** (`trade`) | Submitting proposals for this run and requesting their execution. Also account-scoped funds reads needed to author a trade. |
| **Send notifications** (`notify`) | Run-scoped notifications. A notification never authorizes trading. |

Without `data`, the market routes refuse; without `trade`, a proposal is refused
by name. Child credentials can never issue an authorization, approve a request,
or widen a risk policy.

## 5. Paper and live

Paper/live is a separate choice from authorization:

* **Paper** - simulated fills, no broker contact.
* **Dry run** - no orders at all.
* **Live** - real orders. Live is offered only when the server's options report
  it, and it is bound to the account you choose.

## 6. Review-first and automatic

| Mode | What happens when `ctx.run.request_execution(...)` runs |
| --- | --- |
| **Review trades first** (default) | The plan is frozen, the request waits as `awaiting_approval`, and only your approval releases it. |
| **Trade automatically within my limits** | The request is admitted under an owner-issued authorization bound to this exact version, account, environment and limit revision. |

Your own limits (allocation, per-instrument notional, gross notional, open
instruments, admission window, daily loss budget) are checked before any trade,
in **both** modes. An authorization is never inferred from "run now", and
changing the code, the account, the mode or the limits invalidates it.

Revocation stops *new* discretionary actions. It does not cancel an order that
already reached the broker, and it cannot un-send a request that already won its
dispatch claim.

## 7. Schedules, progress and logs

* Schedules have create/edit/disable, a next occurrence, last occurrence, and
  the platform's missed-run/overlap rules.
* `ctx.progress("...")` is the child's own liveness signal. It is the only thing
  that updates the attempt's progress; the supervisor heartbeat does not.
* The child writes to its own stdout/stderr, and the supervisor ships that log
  to the platform when the attempt reaches its terminal transition (bounded,
  redacted on ingest). So the run log is **post-attempt history, not a live
  stream**: `ctx.progress(...)` is what tells you now.
* A blocked strategy names the reason: an unreconciled attempt, a disabled
  strategy, an unavailable account, an unsafe dependency, etc.

## 8. Your own positions and pending orders

`ctx.run.owned_work()` returns *this strategy's* attributed book (not the
account net) plus its outstanding work:

```python
snapshot = ctx.run.owned_work()
snapshot["coverage"]            # "known" | "unknown"
snapshot["positions"]           # own net quantity per instrument
snapshot["pending"]             # submitted / withheld steps and remaining quantity
snapshot["projection"]          # publication + freshness metadata
snapshot["option_runs"]         # this strategy's own option runs (entry/exit)
snapshot["option_runs_coverage"]# known | unknown, plus truncation/reason
```

Rules that matter:

* an unpublished projection is **unknown**, not a flat book;
* a missing quote is not an unfilled order;
* if pending quantity already covers the change you were about to make, do not
  send it again - see `options_index_setup_adjustment.py` and
  `index_universe_equal_weight.py`.

## 9. Version updates and re-authorization

Editing the source registers a new immutable version. Because an authorization
binds the version id and source hash, the new version needs a fresh explicit
authorization before automatic trading resumes. Review-first strategies keep
working without re-authorization.

## 10. Examples in this directory

| File | Schema | What it shows |
| --- | --- | --- |
| `index_indicator_strategy.py` | `index_indicator.schema.json` | An index ticker plus indicator signal, and a *separately configured* traded instrument. |
| `options_index_setup_adjustment.py` | `options_index_setup.schema.json` | Index-ticker setup, live premiums/Greeks, a frozen defined structure, and one bounded adjustment that never duplicates itself. |
| `index_universe_equal_weight.py` | `index_universe_equal_weight.schema.json` | An owner universe, complete-coverage validation, and an equal-weight full-snapshot target against a budget. |

### The budget is the owner's allocation

A weights target is sized against the **owner-issued admission allocation**
(`StrategyAdmissionPolicy.allocation_inr`), not against a number the strategy
sends: an approved size cannot be narrated into existence.

If your proposal **states** `capital_basis_inr`, the server checks it against
that allocation and refuses by name when they differ - in either direction:

* `CAPITAL_BASIS_MISMATCH` - the stated budget is not the recorded allocation;
* `CAPITAL_BASIS_INVALID` - the stated value is missing a number, zero, negative,
  `NaN` or infinite.

The refusal happens before any plan, reservation or order exists, so a stated
budget is never silently replaced by a bigger allocation. Omitting the field
uses the recorded allocation, which is the normal path. `budget_inr` in
`index_universe_equal_weight.py` is therefore the strategy's budget *and* must be
the number the operator recorded for that strategy; the example names the
refusal (with both amounts) instead of crashing, and reports the settled notional
after a successful rebalance.

## 11. Limitations

* Only `main(ctx)` files are supported. This is not arbitrary-Python hosting.
* No runtime package installation from strategy source.
* No direct broker calls, no raw order or basket mutation, no option/protection
  mutation outside the governed request path.
* Dynamic option-leg replacement is not an engine capability. The supported
  adjustment is an `option_structure` plan in the `exit` phase, which closes the
  run's own frozen legs (short liabilities first) and carries the `option_run`
  reference the entry created. That identity comes from
  `ctx.run.owned_work()["option_runs"]` (with `option_runs_coverage` saying
  whether the set is complete), so no id is ever passed in as a parameter and an
  unreadable set is a named no-action rather than a guess.
* Physical settlement needs separate capability evidence; the examples use
  `exit_before_cutoff`.
* Closing a structure by exit is not cash/physical settlement. A closed option
  run stays `exited` (flat) and the option domain reports it as settled only when
  authoritative settlement evidence exists; the example stops at the closed,
  flat state and does not claim collateral release or settlement.
* Simulated execution is not live certification.

## 12. For the operator (separate from launching a strategy)

Strategy authors need no new environment variable. The deployment provides the
runner image, the supervisor credential, the hosted account allowlist and the
dispatcher. Those are operator concerns and are not part of a user's launch.

## 13. Running the integration harness

```bash
timeout 900 .venv/bin/python examples/hosted_platform/run_phase5_acceptance.py
```

It creates and drops its own uniquely named database on `127.0.0.1:15433`
(never production 15432), serves the production routers over loopback, runs the
real supervisor child for the examples, performs the production operator
reconciliation/terminal transition and a real four-axis settlement assessment
(`POST /api/strategies/{id}/settlement/assess`), and runs the recovery matrix.
Evidence lands under `examples/hosted_platform/evidence/`.

The harness refuses to report a green scenario on a weak signal. It requires
that the child exit **by itself** with exit code 0 and its own final marker (a
terminal request count does not end the scenario), the exact number of
dispatched requests/paper orders and their quantities and statuses, the
attributed book to agree with those orders, and - for the closed option
structure - the option run's own status plus all four settlement axes. A holding
scenario is asserted to show its expected open exposure and a **unsettled**
attribution-flatness axis, because an open position is not settlement.
