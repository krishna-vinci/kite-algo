# kite-algo production roadmap: phases in priority order

**Date:** 2026-09-26. Owner-approved direction.
**Goal:** a platform that is easy, efficient, effective and intuitive, and ready for production and live trading.
**Ground truth:** `documents/kite-algo-platform-reference.md`. Every item below maps to a verified gap in its §12.
**Background (UX ideas only):** `documents/openalgo-competitive-production-plan-2026-09-26.md`.

| Phase | Theme | Items | Status |
| --- | --- | --- | --- |
| **0** | Stabilise the base | Commit the lane gate. Fix the failing live resize test. Confirm and fix possible bugs: owner policy marking option protection "unreadable", Greeks time to expiry in UTC, resolver `lot_size=1`, live sub-lot round-up, scheduled child cannot read its occurrence. Restore the read-only broker order and position routes. | **done**, deployed 2026-09-26 (finance-app, alerts-worker, strategy-runner at `8618542`; not yet pushed) |
| 1 | Live safety | Freeze-quantity slicing (try `autoslice`), including protective exits. Option index, premium and MTM stops that are fed and actually **exit**. A live daily loss limit. An account-wide loss cap. A kill switch (stop all and flatten, including live non-option positions). A market-hours and holiday gate. Stop with flatten. Security basics: market-runtime auth, default DB and Redis credentials, a required MCP token. | **done**, deployed 2026-09-26/27 |
| 2 | Easy and intuitive (parallel with Phase 1) | A Live trading settings page (lanes, account read automatically, caps, risk-policy editor; env keeps only the master switch). An always-visible Paper/Live indicator. Five plain states. An approvals inbox. Status lights. One screen from code to running. Two run styles: Rebalance and Live session. | **done**, deployed 2026-09-26/27 |
| 1b | First real trade | One small owner-approved CNC trade straight after Phase 1. | planned |
| 3 | Real time | Live session run style: scheduled start, runs all market hours, many decisions, holiday-aware. Several strategy children at once. Live log streaming. Execution that survives the child exiting. Prove it with an all-day paper straddle. | **done**, deployed 2026-09-26/27 |
| 4 | Options edge | Server-side position Greeks. Delta and underlying triggers. Relative legs inside plans. A roll and expiry-warning scheduler. Per-strike IV. Chain and payoff UI. | **done**, deployed 2026-09-26/27 |
| 5 | Go live, lane by lane | MIS, then futures, then options: small real trades the owner approves, following the runbooks. | planned |
| 6 | Reach | Telegram commands. TradingView and Chartink webhooks. Analytics. | planned |

## Phase 0 slices

| Slice | Agent | Worktree / branch | Scope |
| --- | --- | --- | --- |
| lane gate | orchestrator | merged `25beed9` | `HOSTED_LIVE_LANES` |
| p0-adjust | GLM (precise) | `codex/p0-adjust` | Failing live resize test; owner-policy "unreadable" bug |
| p0-options-market | DeepSeek | `codex/p0-options-market` | Greeks T in IST; resolver lot size from the contract |
| p0-hosted-occurrence | DeepSeek | `codex/p0-hosted-occurrence` | Scheduled child exposes its bound occurrence |
| p0-order-routes | DeepSeek | `codex/p0-order-routes` | Restore read-only order and position routes lost in `c598871`; write routes stay off pending an owner decision |
| p0-sublot | DeepSeek | `codex/p0-sublot` | Live weight sizing floors like admission and paper |
| p0-test-hygiene | DeepSeek | `codex/p0-test-hygiene` | Repair 20 stale `tests/options` failures (tests only) |

**Phase 0 exit:**
- Every slice has been reviewed and its targeted tests pass.
- The slices are merged into `development`.
- The reference §12 is updated.
- Nothing is pushed or deployed without the owner's go-ahead.
