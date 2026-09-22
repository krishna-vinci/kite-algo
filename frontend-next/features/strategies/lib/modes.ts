/**
 * Execution-mode semantics for the hosted-strategy operator surface.
 *
 * The server owns the vocabulary and the capability: `/api/strategies/options`
 * reports the modes this deployment offers and — when live is offered — the
 * lanes a live plan may use. Nothing here hardcodes that list, and a strategy
 * row alone never proves that live is available.
 */

import type { HostedJobSummary, HostedStrategyOptions } from "@/lib/hosted-strategies/types";

import { jobStatusLabel } from "./format";

export const PAPER_MODE = "paper";
export const DRY_RUN_MODE = "dry_run";
export const LIVE_MODE = "live";

const MODE_LABELS: Record<string, string> = {
  [PAPER_MODE]: "Paper",
  [DRY_RUN_MODE]: "Dry run",
  [LIVE_MODE]: "Live",
};

export function executionModeLabel(mode: string | null | undefined): string {
  const key = String(mode ?? "").trim();
  if (!key) return "Unknown";
  return MODE_LABELS[key] ?? key;
}

/** The modes this deployment's server actually offers, in server order. */
export function supportedExecutionModes(options: HostedStrategyOptions | undefined): string[] {
  return options?.execution_modes ?? [];
}

export function isModeSupported(
  options: HostedStrategyOptions | undefined,
  mode: string,
): boolean {
  return supportedExecutionModes(options).includes(String(mode ?? "").trim());
}

export function liveModeSupported(options: HostedStrategyOptions | undefined): boolean {
  return isModeSupported(options, LIVE_MODE);
}

/**
 * Lane codes are the server's (`cnc`, `mis`, `futures`, `options`); the label
 * is presentation only, and an unknown lane keeps its raw code.
 */
export const LIVE_LANE_LABELS: Record<string, string> = {
  cnc: "CNC / portfolio",
  mis: "MIS",
  futures: "Futures / rolls",
  options: "Options",
};

export function liveLaneLabel(lane: string): string {
  const key = String(lane ?? "").trim();
  return LIVE_LANE_LABELS[key] ?? key;
}

export function liveLanes(options: HostedStrategyOptions | undefined): string[] {
  return options?.live_lanes ?? [];
}

/** `null` when the server reports no lanes (an unknown capability, not "none"). */
export function liveLaneSummary(options: HostedStrategyOptions | undefined): string | null {
  const lanes = liveLanes(options);
  if (lanes.length === 0) return null;
  return lanes.map(liveLaneLabel).join(" · ");
}

/**
 * Live always requires the owner's own plan approval. A server that does not
 * report the flag is an older server, whose live path is approval-gated too, so
 * only an explicit `false` relaxes this.
 */
export function liveRequiresOwnerApproval(options: HostedStrategyOptions | undefined): boolean {
  return options?.live_requires_owner_approval !== false;
}

/**
 * The mode a create form starts on: `paper` whenever the deployment offers it.
 * A first-time default must never be the live mode.
 *
 * The offered list is never auto-selected when it is `live` alone (or empty): a
 * live-only deployment starts on `paper`, which the form marks "not offered
 * here" and which the server refuses if it is ever submitted.
 */
export function preferredCreateMode(modes: readonly string[]): string {
  const list = modes.map((mode) => String(mode));
  if (list.includes(PAPER_MODE)) return PAPER_MODE;
  if (list.includes(DRY_RUN_MODE)) return DRY_RUN_MODE;
  return PAPER_MODE;
}

/**
 * The attempt that would refuse a new launch. `replacement_blocked` is the
 * server's own answer, and the status mirror below is the same rule the store
 * applies (`queued`/`starting`/`running`, or an unreconciled `recovery_required`),
 * so a summary that predates the flag behaves identically.
 */
const BLOCKING_JOB_STATUSES = new Set(["queued", "starting", "running"]);

export function blockingJob(jobs: readonly HostedJobSummary[]): HostedJobSummary | undefined {
  return jobs.find(
    (job) =>
      job.replacement_blocked === true ||
      BLOCKING_JOB_STATUSES.has(String(job.status)) ||
      (String(job.status) === "recovery_required" && !job.reconciled_at),
  );
}

export function unsupportedModeReason(mode: string): string {
  if (String(mode) === LIVE_MODE) {
    return "Live execution is not enabled on this deployment, so this strategy cannot start an attempt here. Its live mode, history and schedules are untouched, and paper/dry-run strategies are unaffected.";
  }
  return `This deployment does not offer ${executionModeLabel(mode)}.`;
}

export function blockingJobReason(job: HostedJobSummary): string {
  if (job.status === "recovery_required") {
    return `Attempt #${job.attempt} is in recovery and is not reconciled, so a new attempt is refused until it is reconciled.`;
  }
  return `Attempt #${job.attempt} is ${jobStatusLabel(job.status).toLowerCase()}; stop it before starting another attempt.`;
}

export type RunNowGate = { blocked: boolean; reason: string | null };

/** Whether the options query has answered yet. */
export type ModeCapabilityState = "ready" | "loading" | "unavailable";

export function modeCapabilityState(
  options: HostedStrategyOptions | undefined,
  failed: boolean,
): ModeCapabilityState {
  if (options) return "ready";
  return failed ? "unavailable" : "loading";
}

/**
 * Why Run now is unavailable, kept explicit about which condition applies: the
 * server's capability for the strategy's pinned mode, or the status of an
 * attempt that is still running.
 *
 * Until the capability is known the launch is HELD rather than allowed: the
 * browser cannot prove the pinned mode is offered, and the server stays the
 * authority for the launch itself.
 */
export function runNowGate({
  mode,
  options,
  jobs,
  capability = options ? "ready" : "loading",
}: {
  mode: string;
  options: HostedStrategyOptions | undefined;
  jobs: readonly HostedJobSummary[];
  capability?: ModeCapabilityState;
}): RunNowGate {
  const pinned = String(mode ?? "").trim();
  if (capability === "loading") {
    return {
      blocked: true,
      reason:
        "Waiting for this deployment's supported execution modes to load. Run now stays disabled until the server has answered.",
    };
  }
  if (capability === "unavailable" || !options) {
    return {
      blocked: true,
      reason:
        "This deployment's supported execution modes could not be loaded, so Run now stays disabled. Reload the page to ask the server again.",
    };
  }
  if (!isModeSupported(options, pinned)) {
    return { blocked: true, reason: unsupportedModeReason(pinned) };
  }
  const blocker = blockingJob(jobs);
  if (blocker) {
    return { blocked: true, reason: blockingJobReason(blocker) };
  }
  return { blocked: false, reason: null };
}
