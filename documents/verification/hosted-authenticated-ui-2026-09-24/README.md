# Authenticated deployed UI smoke — 2026-09-24

Root tested the deployed LAN frontend through a real Chrome browser using the
normal login form and the user-authorized account. No fabricated cookies or auth
bypass. Credentials are excluded from this evidence.

Verified: signed-in composer, starter source insertion, configuration of a finite
paper data-only job, Create and run, actual supervisor handoff and child progress,
confirmed process cleanup, job detail, server-authorized data-only reconciliation,
and disabling the test strategy after completion. The strategy had no trade or
notification capabilities. No orders or notifications were submitted.

- Strategy: `hs_58efbe77cf324cc5b2eea1eaee947aa1`
- Job: `hsj_0af8f5f396254dce85d00badabbccca1`
- Run: `run_3f9465e0da1f42ab8e2749924adf0d74`
- Final job: stopped, process cleanup confirmed, replacement block cleared.
- Final strategy: disabled, retained as test evidence; no schedule created.

Screenshots capture the actual deployed UI: composer, terminal job, reconciled
job, disabled strategy. Browser and temporary browser profile were cleaned up.

Finding forwarded to implementation: strategy detail continued to display Queued
after the job API reported recovery_required with cleanup confirmed. Navigating
to job detail displayed the true state. Active job list refresh needs correction.

Limits: this proves the signed-in data-only lifecycle, not trading, valid quote
content, recurring held positions, or options execution. The progress endpoint
persists its timestamp, not the message/quote value; no quote-content claim is
made. Recurring portfolio and options acceptance are separate work in progress.
