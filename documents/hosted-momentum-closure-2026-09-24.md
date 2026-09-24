# Nifty-500 momentum adapter: closure record

Root acceptance: reviewed as a **paper-experimental single-evaluation example**
on 2026-09-24. The current four-scenario evidence hashes match the source,
schema, and harness. The 94-test focused suite passed. Recurring portfolio
operation, deployed UI acceptance, and live trading are **not accepted** by this
record. The worker's no-commit statements below describe its execution boundary;
root integration commits the reviewed example and evidence separately from the
already deployed platform release.

Date: 2026-09-24. Baseline `e23be53` (deployed campaign) with this worktree's
uncommitted `b54360c`-onward work. This record corrects the momentum section of
`documents/hosted-usable-release-and-momentum-plan-2026-09-24.md`, which claimed
that no API/disposable-PostgreSQL harness existed for this adapter and that no
paper order had been placed anywhere.

Nothing here is self-accepted. Nothing was committed, staged, pushed, built,
migrated, deployed or reconfigured. No production database was read or written,
no notification was sent, no live gate was opened, and no real (broker) order
was placed. Every order in this record is a PAPER order inside a uniquely named
disposable PostgreSQL database on `127.0.0.1:15433` (production is `15432`),
dropped by the harness on exit. The adapter is a preview / paper-experimental
example; a paper fill does not prove live execution, and the deployment UI is
unchanged by this bundle.

## 1. What was wrong, traced to the source rather than inferred

Two independent defects made the review-first scenarios disagree with the
platform. Both were reproduced, root-caused and fixed in the example/harness;
neither required a backend, authorization or SDK change.

### 1.1 The child cancelled its own wait, so the owner's approval arrived too late

The adapter treated `awaiting_approval` as a settled state: its
`_TERMINAL_REQUEST_STATES` contained that status, so `_await_request(...)`
returned as soon as the request parked and `_dispatch(...)` reported the run as
"waiting for the owner's decision" and exited 0.

That is wrong in the platform's own terms. A child's authority is
attempt-scoped: `ExecutionRequestService._attempt_refusal` re-reads the job, its
`run_id`, `token_id`, `lease_epoch` and `attempt` on every claim
(`backend/strategies/execution_requests.py:1532-1571`), and refuses the work with
`HOSTED_ATTEMPT_FENCED` once the attempt is gone. Trace from run
`evidence/phase5-20260924T032817Z.json`: the request the child parked was later
approved, the claim refused it `HOSTED_ATTEMPT_FENCED`, and the scenario finished
with 0 orders while claiming a review-first flow. Run
`evidence/phase5-20260924T033033Z.json` shows the same race landing the other way:
there the approval and the claim narrowly beat the attempt's fence, so the same
code produced 4 orders AFTER the child had already stopped waiting. A green run
therefore depended on that timing rather than on the contract, which is why the
old runs did not prove anything about review-first.

Fix: `awaiting_approval` is not terminal. The child keeps the attempt alive,
publishes progress while it waits, and exits 0 only on a status the platform
resolved (`executed` / `refused` / `rejected`). If the attempt's own bound
expires while the owner has not decided, the run ends `unresolved` (exit 2) with
a named reason instead of reporting a parked request as a finished run.

### 1.2 The isolated instance's Redis side-channel, not the governed work, was the dispatch latency

The harness's isolated instance has no Redis and the inherited default URL
(`redis://redis:6379/0`) names a host that does not resolve here, so every
best-effort event publish blocked on connect. Measured directly: 5.03 s per
publish for that URL, 0.00 s for a closed loopback port.

Every publishing leg paid that cost: the run log shows the `paper.orders` /
`paper.trades` / `paper.positions` channels failing per attempt, and the recorded
fill timestamps from `evidence/phase5-20260924T034044Z.json` (kept database) are
`03:36:52.223Z`, `03:37:12.662Z`, `03:37:33.074Z`, `03:37:53.509Z` - one leg
every ~20.4 s. The manual request itself was created at `03:36:50.435Z` and
approved at `03:36:50.999Z`; the child's own bound (its `deadline_seconds`
parameter, 60) expired at ~`03:37:50.4Z`, by which point the request still had no
terminal status, so the child exited 2. The governed request was approved and
executed exactly as designed, just slower than the bound because of the stall.

Fix: the harness points `REDIS_URL` at a closed loopback port for the isolated
instance, so the side-channel fails immediately and honestly instead of stalling
the governed path. The delivery semantics are unchanged (the publish is
best-effort and no notification is sent either way).

Two smaller corrections in the same pass: the momentum runner no longer services
the platform (approve/dispatch) after the child exits, so a green run cannot be
built on a fenced attempt; and an unused duplicate completeness helper
(`_session_is_complete`) was removed from the adapter, because the accepted
completeness rule is the verified calendar close plus the platform's finality
delay, which is already applied where the as-of session is chosen.

## 2. The four momentum scenarios, as actually run

### 2.1 Current: the four scenarios, re-based and guarded (Option A)

Command (disposable PostgreSQL on 15433, real routers over loopback, real
`backend.strategies.supervisor` child, real governed pipeline):

```
.venv/bin/python examples/hosted_platform/run_phase5_acceptance.py \
  --timeout 180 --only momentum_mid_month_deferral,momentum_manual_entry,momentum_autonomous_entry,momentum_breadth_exit
```

Evidence: `examples/hosted_platform/evidence/phase5-20260924T041640Z.json`
`ok: true`, `errors: []`, every `acceptance.ok` true, harness exit status 0, and
the disposable database dropped on exit. The run's own provenance block records
the exact inputs it drove:

| Input | SHA-256 |
| --- | --- |
| `examples/hosted_platform/nifty500_momentum.py` | `60ba7b40f94091b9d8d67556f3ba8aabe7af880d739c71ca98835eda483fd1e4` |
| `examples/hosted_platform/nifty500_momentum.schema.json` | `f089cc7dda38736653bda8630e0dfe45456b9db5c8ed3d46e81ce2f538a8da18` |
| `examples/hosted_platform/run_phase5_acceptance.py` | `f6c9515200fec7c573c18fb5d83063e02882f6f1e774a70cb0d5eac2a7a510ee` |

The fixture is judged against the real clock (section 6.4), so the run records
the sessions it decided on instead of depending on when it ran: every scenario
below shows `as_of_session 2026-09-23`, the current session `2026-09-24` returned
explicitly as an unfinished bar, and the schedule it was given.

| Scenario | Schedule | Child | Requests | Paper orders | Trace |
| --- | --- | --- | --- | --- | --- |
| `momentum_manual_entry` | `MONTHLY_CALENDAR_DAY` day 23 -> resolves to the as-of session (due) | exit 0, on its own | 1 `executed` | 4 BUY x 51 | parked `awaiting_approval`, ONE approval issued over HTTP while the child was alive, `queued`, `executed`, then the child logged `dispatch status=executed`; `orders_before_approval == 0` |
| `momentum_autonomous_entry` | `MONTHLY_CALENDAR_DAY` day 23 -> due | exit 0, on its own | 1 `executed` | 4 BUY x 51 | admitted `queued` under the version-bound grant with ZERO hand approvals; the child exited on the status it observed |
| `momentum_breadth_exit` | `MONTHLY_LAST_SESSION` (the gate is decided before the schedule) | exit 0, on its own | 1 `executed` | SELL 20 + SELL 10 | breadth 0/4 -> DEFENSIVE, exits its OWN seeded book only, approved while alive, `executed` |
| `momentum_mid_month_deferral` | `MONTHLY_CALENDAR_DAY` day 1 -> resolves to a DIFFERENT verified session of the month (not due) | exit 0, on its own | 0 | 0 | child logged `off the monthly rebalance session; no entry` with `regime RISK_ON`, as-of `2026-09-23`, and submitted nothing |

Each scenario also keeps a second strategy holding an unrelated instrument in
the same paper account; its projection was byte-identical before and after
(`bystander_untouched: true`), so "exit" never reaches another book.

Run artifacts are bounded: `request_timeline`, `orders_before_approval`,
`approvals_while_child_alive`, `requests_at_child_exit`, the child log tail and
the supervisor outcome are all recorded per job, so the claim "the approval
landed while the child was alive" is evidence in the file rather than a summary.

This is a PAPER result in a disposable database. It says nothing about live
execution, and it does not make the strategy production-recurring-ready - the
gaps in section 4 are unchanged by this re-base.

### 2.2 Prior: the pre-guard revision, kept as a record

These runs are NOT reproducible under the freshness guard in section 6 and must
not be read as a current green claim. They were produced on the same day at
~03:48 and ~03:50 UTC by code that still allowed the as-of date to roll back to a
stale index bar (the fix is section 6.1), with the fixed-past fixture described
in section 6.4.

Command (four scenarios, same filter as above):

```
.venv/bin/python examples/hosted_platform/run_phase5_acceptance.py \
  --timeout 180 --only momentum_mid_month_deferral,momentum_manual_entry,momentum_autonomous_entry,momentum_breadth_exit
```

Evidence: `examples/hosted_platform/evidence/phase5-20260924T034828Z.json`
(`ok: true`, no errors, every `acceptance.ok` true).

| Scenario | Child | Requests | Paper orders | What that run's trace shows |
| --- | --- | --- | --- | --- |
| `momentum_manual_entry` | exit 0, on its own | 1 `executed` | 4 BUY x 51 | request parked `awaiting_approval` at `03:48:04.333Z`, ONE approval issued over HTTP while the child was alive, `queued` at `03:48:04.942Z`, `executed` at `03:48:07.500Z`, then the child logged `dispatch status=executed`. `orders_before_approval == 0` |
| `momentum_autonomous_entry` | exit 0, on its own | 1 `executed` | 4 BUY x 51 | admitted `queued` under the version-bound grant with ZERO hand approvals, `executed` at `03:48:15.786Z`, child exited on the observed status |
| `momentum_breadth_exit` | exit 0, on its own | 1 `executed` | SELL 20 + SELL 10 | breadth 0/4 -> DEFENSIVE, exits its OWN seeded book only, parked `awaiting_approval`, approved while alive, `executed` at `03:48:23.375Z` |
| `momentum_mid_month_deferral` | exit 0, on its own | 0 | 0 | as-of `2026-08-18` is not the month's last verified session; the child logged `off the monthly rebalance session; no entry` with `regime RISK_ON` and submitted nothing |

The whole harness (all TEN scenarios, including the three previously accepted
examples and the recovery scenario) was run with no `--only` filter on that same
pre-guard revision:
`examples/hosted_platform/evidence/phase5-20260924T035017Z.json`, `ok: true`,
`errors: []`, every scenario `acceptance.ok` true, disposable database
`kite_accept_07ac0533e5` dropped on exit.

Its nine other scenarios (the three accepted examples, `basis_mismatch`,
`options_adjustment`, `recovery`) are unaffected by this bundle's changes; only
the four momentum scenarios were re-run, deliberately, as section 6.4 records.

## 3. Test evidence

```
.venv/bin/python -m pytest tests/strategies/test_nifty500_momentum_source.py \
  tests/sdk/test_hosted_bootstrap.py tests/strategies/test_readiness.py \
  tests/strategies/test_hosted_harness_assertions.py -q
```

Result: `94 passed`, exit status 0 after the freshness guards (section 6). The
momentum file alone is 53 tests, exit 0 (`53 passed in 6.70s`). They load the real checked-in source through the real
hosted loader; no test stubs the adapter's own decisions.

Test changes in this bundle, all consequences of the two root causes:

* `test_a_parked_request_is_not_a_terminal_state` pins the invariant the wait
  rests on (`awaiting_approval` is NOT terminal).
* `test_the_child_holds_while_the_owner_decides` drives the durable row through
  `awaiting_approval -> awaiting_approval -> queued -> executed` while the child
  is alive and asserts it exits 0 on the observed decision, not on the park.
* `test_an_owner_wait_that_outlives_the_bound_is_unresolved` asserts the honest
  failure: exit 2, the reason naming the owner wait and the attempt's bound.
* `test_the_breadth_exit_waits_for_the_owner_like_every_other_request` asserts
  the exit path has no shortcut around the owner.
* `test_a_session_without_a_verified_close_is_never_the_as_of_session` replaces
  a stale expectation: an unverified month-end close falls back to the last
  provably finished session (`as_of 2026-08-28`) and the run defers by name
  instead of trading a session it cannot prove finished.
* `test_every_schema_parameter_is_one_the_adapter_actually_reads` pins the
  parameter contract in both directions: no schema property is ignored by the
  adapter, every documented default equals the adapter's own default, and the
  one parameter the adapter keeps internal (`product`, always CNC) has a named
  refusal rather than a second accepted product.

The freshness guards added later the same day carry their own four tests; they are
listed in section 6.3.

## 4. Recurrence gaps (documented, deliberately not worked around)

These are platform facts this bundle does not fix, does not bypass and does not
claim away. They are the reason this adapter is NOT production-recurring-ready.

1. **A run that leaves exposure cannot be reconciled, and an unreconciled
   attempt blocks the next job.** The harness records the platform's own refusal
   on every momentum run that ends with a filled book: `POST .../reconciliation`
   returns 409 `OPEN_EXPOSURE`. `POST .../jobs` then raises `StrategyFenceError`,
   surfaced as 409 `STRATEGY_BLOCKED`
   (`backend/api/routers/strategies.py:2987`, recovery gate at
   `:347`). This bundle does not reconcile open holdings and does not force the
   attempt terminal to make a recurrence look green.
2. **An invested book can be refused `ALLOCATION_EXCEEDED` on an otherwise flat
   trade.** Admission adds the strategy's attributed consumption, its active
   reservations and the plan's requirement before comparing with the recorded
   allocation (`backend/strategies/admission.py:408-422`). A full-target
   `intent_bundle` carries the whole book as its requirement, so a monthly
   rebalance of a book already near the allocation can be refused even when the
   cash delta is small. The adapter reports that refusal by name and places
   nothing.
3. **The owner allocation is not child-verifiable on this path.** The
   `CAPITAL_BASIS_MISMATCH` check applies to `target_kind="target_weights"`; this
   adapter submits exact-quantity `intent_bundle` legs to preserve whole shares.
   It states `budget_inr` and enforces its own affordability, but the binding
   platform control is the admission ceiling, so no equality claim is made.
4. **Fan-out cost and membership bias.** One daily-history call per constituent
   (~500 per run, bounded concurrency, sequential fallback when the child's
   rlimits refuse a thread pool) and a current-only constituent list, so the
   replay carries survivorship bias. Both are stated limitations, not defects.
5. **Paper, not live.** Every fill here is a paper fill in a disposable
   database. Nothing in this record demonstrates live broker execution,
   notification delivery or a production schedule.

## 5. Facts corrected in the plan document

| The plan said | Measured |
| --- | --- |
| the adapter "refuses when the last bar is not final" (7.3) | completion is proven by the verified calendar session close plus the platform's 900 s finality delay, or by the platform's own last-bar verdict for the newest expected session; a missing close is never inferred from the date |
| "29 tests" (7.7.4) | 53 tests in the momentum file (94 across the plan's command) after the freshness guards, all loading the real source through the real loader |
| "An API/disposable-PostgreSQL paper harness for this adapter is not in this bundle ... no such run is claimed" (7.8.4) | the phase-5 harness runs four momentum scenarios end to end (real routers, real supervisor child, disposable PostgreSQL) and all four are green; see `evidence/phase5-20260924T034828Z.json` |
| "no paper or live order was placed anywhere" (closing) | paper orders were placed inside the disposable database and are recorded in the evidence files; no real order and no notification were sent |

## 6. Addendum, 2026-09-24 (freshness guards after review)

Root's review found one remaining freshness hole and one over-permissive finality
rule. Both are fixed in the adapter, with focused tests; neither needed a backend,
SDK, authorization or harness change.

### 6.1 The as-of date may no longer roll back

`_run` derived `latest_completed` from the verified calendar and then set
`as_of = max(index_bars <= latest_completed)` without ever asserting the two
agree. A provider that simply omits the newest index bar therefore re-dated an
older bar as "now" AND shrank `calendar_window` with it, so a stale signal could
be traded under a fresh attempt. The adapter now refuses by name:

```
INDEX_HISTORY_STALE   as_of=... latest_completed=... newest_index_bar=...
```

* `as_of == latest_completed` is required: an old bar is not this session's
  evidence.
* Members are unchanged here: they are already matched against the whole replay
  window, and a member hole is a named refusal, never an exclusion.

### 6.2 An explicit ``False`` finality verdict is now scoped to its bar

`last_candle_final` was accepted anywhere as long as it was present, so a bar the
platform explicitly called unfinished could still be the as-of bar. The rule is
now:

* the verdict must exist (`SESSION_FINALITY_UNKNOWN` otherwise);
* an explicit ``False`` is allowed ONLY when it is about something later than the
  as-of session, proven by a RETURNED bar after the as-of session
  (`max(bars) > as_of`). That is the real "today is still open, yesterday is the
  signal" shape. Note what the platform actually computes: the flag comes from
  the last EXPECTED session of the requested range
  (`backend/broker_api/market/exchange_calendar.py:231-260`), which is why the
  "expected but absent" list is discussed below;
* otherwise it is refused: `INDEX_SESSION_NOT_FINAL` for the index,
  `CONSTITUENT_SESSION_NOT_FINAL` for a member. One unfinished member makes the
  breadth denominator unknown, so the run places nothing at all.

Operational consequence, stated because it is the intended shape rather than a
side effect: while the current session is still open, the platform's verdict is
``False`` for the range's last expected session and no later bar exists yet, so a
pre-close run refuses by name instead of deciding on a range whose tail it cannot
see. That matches the accepted operational rule that the daily evaluation runs
after the session close plus the 900 s finality delay. If root prefers a narrower
reading - an explicit ``False`` plus a session AFTER the as-of named in
`missing_sessions` (the platform's own "expected but absent" list) also counts as
"the flag refers later" - that is a one-line change in
`_false_finality_leaves_a_later_bar`; it is NOT implemented here because the
instruction was explicit that ``False`` needs a later returned bar.

### 6.3 Focused tests

`tests/strategies/test_nifty500_momentum_source.py` gained four tests and one
fixture shape for the not-yet-closed tail:

* `test_a_missing_newest_index_bar_is_a_named_refusal` - the index's newest bar
  removed while every member stays fresh: zero proposals, zero requests.
* `test_index_finality_false_on_the_as_of_bar_is_a_refusal`.
* `test_member_finality_false_on_the_as_of_bar_is_a_refusal`.
* `test_a_not_yet_closed_tail_session_does_not_poison_the_as_of_bar` - a later,
  still-open session with ``False`` keeps the previous as-of date usable.

Measured: `.venv/bin/python -m pytest tests/strategies/test_nifty500_momentum_source.py -q`
-> `53 passed`, exit 0; the four-file command -> `94 passed`, exit 0. The fixture
now also publishes every calendar session AFTER its own as-of session without a
reported close, which is the honest shape for a session that has not finished and
is what keeps the fixed-date fixture consistent with the guard.

### 6.4 Resolution: the momentum harness fixtures are re-based (Option A, implemented)

The guard refuses in the existing phase-5 momentum fixtures, and that is the
guard working: those fixtures synthesized index/member history that stops at a
fixed past month end (`2026-08-31`, or `2026-08-18` for the deferral scenario)
while `seed_momentum_calendar` seeds verified calendar rows (with closes) through
the real today. The newest session the calendar proves finished is a real,
current session, so such a fixture is genuinely a stale feed.

Recorded evidence of that refusal (two scenarios only):
`examples/hosted_platform/evidence/phase5-20260924T040853Z.json` (`ok: false`),
child log:

```
no action: INDEX_HISTORY_STALE {"as_of": "2026-08-31", "latest_completed": "2026-09-23",
 "message": "the newest session the verified calendar reports as finished has no index bar; ...",
 "newest_index_bar": "2026-08-31"}
```

Root chose Option A and authorised the fixture work. What was implemented:

* The fixture's as-of session is the newest session the platform's own completion
  rule allows - a weekday closed at 15:30 IST plus the platform's 900 s finality
  delay, computed from the real clock (`momentum_latest_completed_session`) - and
  the synthetic history is synthesized THROUGH it. Nothing about the seeded
  calendar is faked: every weekday carries its 15:30 close, no HOLIDAY is
  invented, and no close is left empty.
* While the current session is still open, the synthetic provider returns that
  session's UNFINISHED bar explicitly, after the as-of bar
  (`MomentumFixture.open_session`). That is the honest shape of a live range, the
  platform's own verdict for it is ``False``, and it is exactly what lets the
  guard accept the previous completed bar instead of refusing.
* The four scenarios are scheduled explicitly instead of depending on the date the
  suite happens to run: `MONTHLY_CALENDAR_DAY` with `day_of_month` equal to the
  as-of day for the two due scenarios (which `resolve` maps onto the as-of session
  itself), and a day whose resolved session is provably a DIFFERENT verified
  session of the month for the deferral scenario (`1` once the as-of day is past
  the 15th, otherwise the as-of day plus one). Checked over ten representative
  instants, including a month end, the last weekend of a month, the first of a
  month, an early-month day and a late-December evening.
* `MONTHLY_LAST_SESSION` coverage is untouched: the focused unit suite still
  exercises the last-session path (month-end due, the day-31 fallback, mid-month
  not due), and the breadth-exit scenario keeps that parameter because a failed
  breadth gate is decided before the schedule.
* Every evidence file now records the exact inputs it drove (SHA-256 of the
  adapter, the schema and the harness) plus `as_of_session`, `open_session` and
  the schedule parameter it was given, so a run is self-describing.

Result: the four scenarios are green again on the guarded code - section 2.1 and
`examples/hosted_platform/evidence/phase5-20260924T041640Z.json` (`ok: true`,
harness exit status 0). The prior four-scenario and ten-scenario evidence is kept
as a record of the pre-guard revision (section 2.2) and is NOT a current claim.

A one-line fixture tweak was not available, which is why the re-base looks like
this: publishing the sessions after the fixture's as-of as "known but not yet
closed" (``closes_at = NULL``) is the other honest shape, and the production
completeness assessment rejects it today: `assess_daily_completeness` calls
`time.fromisoformat(str(last["closes_at"]))`
(`backend/broker_api/market/exchange_calendar.py:250`) and raises
`ValueError: Invalid isoformat string: 'None'` for such a row. Marking those
sessions as HOLIDAY instead avoids the crash but removes them from the month's
verified session list, which makes the deferral undecidable - the defect the
accepted design rejected. (That NULL-close experiment was run once, crashed
exactly as above, and was reverted; its artifact is not kept.)

### 6.5 Exit status: `ok: false` is provably a non-zero exit

`main` already ended with `return 0 if RESULT["ok"] else 1`; the "exit 0" in the
earlier report was the reporting pipeline (a trailing `grep`/`tail`) masking the
status, not the harness. Both paths are now recorded with the real status:

* `phase5-20260924T041640Z.json` - `ok: true`, harness exit status 0.
* `phase5-20260924T041715Z.json` - a deliberately bounded run
  (`--timeout 2 --only momentum_mid_month_deferral`) that cannot finish in time:
  `ok: false`, harness exit status **1**. Its `child_timeout` failure is the
  forced condition, not a product defect.

The other six scenarios of the previous ten-scenario pass were not re-run. They
do not touch the momentum fixture or the adapter, so their evidence stands; only
the four momentum scenarios are claimed current, and only from the run in section
2.1.

**Not production-recurring-ready.** The re-base changes test fixtures only. The
recurrence gaps in section 4 are unchanged: a filled book still cannot be
reconciled (`OPEN_EXPOSURE`), an unreconciled attempt still blocks the next job
(`STRATEGY_BLOCKED`), an invested book can still be refused `ALLOCATION_EXCEEDED`,
and owner-allocation equality is still not verifiable on the exact-quantity
`intent_bundle` path. Every order in this record is a PAPER order in a disposable
database; no live execution, notification or production schedule is claimed.

## 7. Cleanup

The harness drops its own disposable database, and this bundle additionally
dropped the 46 leftover `kite_accept_*` databases from earlier runs on 15433
(verified: 0 remain; the unrelated pre-existing `kite_test*` databases were left
alone). No supervised child process was left running (`ps` for
`kite_algo_worker.hosted` is empty), `.phase5-workspace/` is removed, and no
production database, migration, credential or environment file was touched.
