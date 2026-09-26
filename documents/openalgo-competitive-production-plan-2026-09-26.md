# Production-ready and competitive with OpenAlgo — locked plan

**Date:** 2026-09-26
**Inputs:** three read-only research passes over the local OpenAlgo checkout (`openalgo/`, head `0a99c14ef`,
2026-09-18), the 2026-09-23 usability review, and code checks on our side in this session. OpenAlgo was used for
workflow and UX ideas only. No code copied, no architecture replaced.

> **Correction (later the same day).** Our own capabilities are now audited with file:line evidence in
> `documents/kite-algo-platform-reference.md`. That document wins wherever the two disagree. Corrections to this
> plan:
> - **Relative legs (P1.4).** ATM±N, ITM/OTM and `delta_target` resolution already exist through the resolve
>   endpoint and SDK helpers. What is missing is only a relative leg *inside* the frozen proposal.
> - **Expiry selection and rolling.** Weekly and monthly expiry selectors and a governed option roll already exist.
>   What is missing is a platform-driven roll scheduler.
> - **Freeze slicing (P0.1).** Still missing, but the cheap route is the unused `autoslice` field on
>   `PlaceOrderRequest`.
> - **This plan is on hold** until the owner has reviewed the reference.

---

## 1. Verdict

**Where we win: execution safety.** This is where OpenAlgo is weakest, and it is the part that loses money when
it goes wrong.

| Safety property | OpenAlgo | kite-algo |
| --- | --- | --- |
| Hedge before short | BUY legs sent first, but it never waits for the fill. A rejected hedge still lets the shorts go out. Baskets of 10 or fewer are sent concurrently. | Hedge fill gate: the short is released only after the hedge fills. |
| Exit order | Broker close-all, in whatever order the book lists positions | Staged exit buys back shorts first |
| Pre-trade margin check | None on any order path | Basket margin at admission (`required_margin_inr`) |
| Partial and failed legs | Multi-order and basket always return 200 "success" | Named refusals and a repair flow |
| Per-strategy attribution | Strategy name tag. A contract shared by two strategies cannot be split between them. | Per-run confirmed fills, ownership, settlement |
| Paper realism for short options | Margin blocks only the premium. Expiry settles at the last LTP. | Paper uses the same governed pipeline |
| Secrets | Hosted scripts inherit the whole `.env` (broker secret, app key, pepper) | Sandboxed child, capability-scoped tokens |
| Semi-auto approvals | Exits and cancels are blocked. Queued orders never expire. Approval fires a MARKET order at the price at that moment. | Reduce-only work is never blocked. Approval is bound to the plan version, catalog and price. |

**Where OpenAlgo wins: ease of use, reach, and features traders see.**
- One Live/Analyze toggle in the navbar, with the whole UI re-themed in paper mode.
- Trading behaviour lives in the UI and the database; env holds only infrastructure.
- A four-question installer and self-healing upgrades.
- Telegram and WhatsApp remote kill (`/closeall`, `/stoppython`, `/mode`).
- Relative option legs (expiry rank plus ATM offset) resolved by the platform.
- A run-level risk panel: combined MTM stop and target, lock-profit ratchet, trail-to-entry, session daily loss
  limit, `exit_time` square-off.
- Tick-driven stops, with a 2-second REST fallback.
- Freeze-quantity-safe exits in scalping.
- A wide analytics surface: IV smile and surface, GEX, OI, straddle charts, and a Strategy Builder with payoff and
  what-if sliders.
- An "armed" start that says in plain words when the strategy will start, and plain pause reasons (holiday,
  weekend).

**Gaps that neither product closes, which we can own:**
- Platform-computed **position Greeks**. OpenAlgo computes them only in the browser, on the Strategy Builder page.
- **Greek-based triggers** such as a net-delta limit.
- **Stops on the underlying for option positions**.
- **Delta-driven adjustment** that runs live. OpenAlgo has only a backtest of a re-centred straddle.
- **Live SIP and portfolio rebalancing**. OpenAlgo's are backtest-only.
- **One automation model.** OpenAlgo has six surfaces that don't share risk logic.

**Where we are today:** the engine is strong, but we are not production-ready for live option trading. We lack
intraday decision cadence, several safety gaps are open (§2 P0), and no real order has been placed yet.

---

## 2. Locked work list

Ranked by value for going live safely, then by value for competing. File references were checked in this session.

### P0 — required before real money (safety gaps found in our code)

| # | Item | Why (evidence) |
| --- | --- | --- |
| P0.1 | **Freeze-quantity slicing on every live order, including protective and staged exits** | Options carry `freeze_quantity` (`live_sequence.py:678`) but nothing slices orders. Futures only refuse above it (`compiler/futures.py:193`). An exit larger than the exchange freeze limit is rejected by the exchange, so the stop fails exactly when it matters. |
| P0.2 | **Underlying/index stop that actually fires** | `index_ltp` rules read `metadata["protection_metrics"]`, which no production code writes. When the value is missing the rule is silently skipped (`options/protection/evaluator.py`). The rule looks set but never fires. |
| P0.3 | **Daily loss budget that works live**, plus an **account-wide daily loss cap and a kill switch** (flatten all and stop all) | A live strategy with `daily_loss_budget_inr` is refused at admission because there is no attributed live realized-loss source (`admission.py:1228`). There is no account-wide cap and no single kill switch. |
| P0.4 | **Fix the failing live resize test** | `test_live_option_resize_rejected_hedge_releases_nothing`: the adjust proposal returns 201 with no plan. It fails on `development` too. |
| P0.5 | **One small real trade per lane** (C2), owner-approved | Nothing has been sent to the real broker yet. |

### P1 — options and real time (our lead)

| # | Item | What it means |
| --- | --- | --- |
| P1.1 | **Market-hours session** run style | A schedule starts the strategy at open. It runs until close and makes many decisions (for example, check delta every 30–60 s). Today scheduled runs are pinned to one decision (`worker_proposals.py:233`). `continuous` + `run_now` looping is allowed by the code but not proven by any example. Applies to equity signal, MIS, futures and options. Portfolio strategies keep one decision per run. |
| P1.2 | **Position Greeks computed by the platform** | Net delta, gamma, theta and vega per option run, from the fresh chain Greeks. Exposed to the strategy (`owned_work`), the strategy page and the watcher. Replaces each strategy's own `_net_delta`. |
| P1.3 | **Watcher triggers** | Underlying level, net-delta limit, combined premium, ₹ MTM, lock-profit ratchet and trail-to-entry. The watcher only exits or alerts; adjust decisions stay in the strategy. Tick-driven or at most 2 s, like OpenAlgo's tick feed. |
| P1.4 | **Relative legs resolved by the platform** | Expiry rank (weekly, next week, monthly), ATM ± offset, and `delta_target`, resolved at plan freeze. Today the production proposal path has no resolver, so the strategy has to pass fully resolved legs (2026-09-23 review, gap 5). |
| P1.5 | **Per-strategy `exit_time` square-off** that is always installed | "Started by a signal, closed by the clock." Needed for MIS and intraday options. |

### P2 — simplicity (the "too complex" problem)

| # | Item | What it means |
| --- | --- | --- |
| P2.1 | **Env holds infrastructure only** | A Settings → Live trading card (DB-backed and audited) holds lanes, the account (read automatically from the broker session), risk caps and the LIMIT drift/timeout values under "Advanced". `HOSTED_LIVE_ENABLED` stays in env as the one server-level master switch. |
| P2.2 | **One Paper/Live indicator that is always visible** | Paper mode re-themes the UI and every alert is tagged. Per-strategy mode stays fixed for the whole run, which avoids OpenAlgo's `force_live` bug. |
| P2.3 | **Two run styles** | **Rebalance** (runs at set times, one decision) and **Live session** (runs all market hours, many decisions). Evaluations, attempts, occurrences and finite/continuous become internal. |
| P2.4 | **Five user-facing states** | Running, Stopped, Waiting for you, Needs attention, Error. Each shows one sentence and one button. Plus "armed until 09:15" and pause reasons (holiday, weekend, before or after market). Refusal codes move behind a "details" link. |
| P2.5 | **Approvals inbox** | Pending live plans from every strategy in one list. Stronger than OpenAlgo's Action Center: approvals expire, price drift is bounded, and reductions are never queued. |
| P2.6 | **Status lights** | Broker session (with a "Reconnect broker" button), instruments, market-data freshness, and supervisor/worker health. |
| P2.7 | **One screen from code to running** | Paste the code, fill in the parameters form, choose Paper/Live and a run style, press Start. |

### P3 — reach and extras (after P0–P2)

| # | Item |
| --- | --- |
| P3.1 | Telegram commands: `/positions`, `/pnl`, `/closeall`, `/stop`, each with a confirm button. Today we have outbound alerts only (`backend/notifications/adapters/telegram.py`). |
| P3.2 | TradingView/Chartink webhook signals turned into governed proposals, with the same signal rules as OpenAlgo: a repeat is a no-op, a flip squares first. We have none today. |
| P3.3 | Options analytics: payoff and what-if in the plan preview, IV smile, straddle chart. We have chain, Greeks, PCR and max pain, but no payoff or IV views. |
| P3.4 | Diagnostics page and a downloadable support report. |
| P3.5 | Live SIP and portfolio rebalancing, marketed as such. We already execute `target_weights` live; OpenAlgo only backtests. |

### Explicitly not adopted

- OpenAlgo's six separate automation surfaces. We keep one governed model.
- Semi-auto that blocks exits; `.env` writable from the web UI; unscoped secrets; concurrent baskets;
  "always 200" responses.
- The analytics breadth race (GEX, vol surface) before P0–P2 are done.

---

## 3. Order of work and staffing

| Slice | Contents | Agents |
| --- | --- | --- |
| S1 | P0.4 fix, P0.1 freeze slicing, P0.2 index feed into the watcher | GLM (money path), with DeepSeek on tests |
| S2 | P2.1 Live settings card, P2.2 indicator, P2.6 status lights | DeepSeek (backend settings), Sonnet (frontend) |
| S3 | P0.3 daily loss (live realized-loss source), account cap, kill switch | GLM |
| S4 | P1.1 market-hours session, P2.3 run styles | DeepSeek (backend), Sonnet (UI) |
| S5 | P1.2 position Greeks, P1.3 watcher triggers, P1.5 `exit_time` | GLM (triggers), DeepSeek (Greeks) |
| S6 | P2.4 states, P2.5 approvals inbox, P2.7 one-screen flow | Sonnet, with DeepSeek on API |
| S7 | P0.5 real trades per lane (owner-run), then P1.4 relative legs | Owner + GLM |
| Later | P3 | — |

S1 and S2 can run in parallel. S1 through S3 close the P0 safety gaps; the real trades in P0.5 happen in S7, once
the options work has landed.

## 4. Decisions for the owner

1. **Master switch:** keep `HOSTED_LIVE_ENABLED` as the one env switch, with everything else in the UI
   (recommended), or move everything to the UI.
2. **Autonomy for live sessions:** use the existing autonomous standing grant for Live-session strategies, so
   delta adjustments don't wait for a click, or require approval for every plan.
3. **Order:** simplicity first (S2 before S1), or safety first. Recommended: run both in parallel.
