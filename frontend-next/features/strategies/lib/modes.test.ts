import { describe, expect, it } from "vitest";

import type { HostedJobSummary, HostedStrategyOptions } from "@/lib/hosted-strategies/types";

import {
  blockingJob,
  executionModeLabel,
  isModeSupported,
  liveLaneSummary,
  liveModeSupported,
  liveRequiresOwnerApproval,
  modeCapabilityState,
  preferredCreateMode,
  runNowGate,
  supportedExecutionModes,
} from "./modes";

function options(partial: Partial<HostedStrategyOptions> = {}): HostedStrategyOptions {
  return {
    account_scopes: ["kite:paper"],
    execution_modes: ["paper", "dry_run"],
    job_kinds: ["finite", "continuous"],
    stale_exit_policies: ["none"],
    hosted_execution_only: true,
    ...partial,
  };
}

function job(partial: Partial<HostedJobSummary> = {}): HostedJobSummary {
  return {
    job_id: "j-1",
    strategy_id: "s-1",
    owner_id: "owner",
    attempt: 2,
    status: "stopped",
    desired_state: "stopped",
    execution_mode: "paper",
    account_scope: "kite:paper",
    run_id: "run-1",
    replacement_blocked: false,
    recovery_required_at: null,
    reconciled_at: null,
    created_at: null,
    updated_at: null,
    ...partial,
  };
}

describe("hosted execution-mode helpers", () => {
  it("labels the known modes and keeps an unknown mode raw", () => {
    expect(executionModeLabel("paper")).toBe("Paper");
    expect(executionModeLabel("dry_run")).toBe("Dry run");
    expect(executionModeLabel("live")).toBe("Live");
    expect(executionModeLabel("simulated")).toBe("simulated");
    expect(executionModeLabel(null)).toBe("Unknown");
  });

  it("reads live support from the server, never from a strategy row", () => {
    expect(liveModeSupported(options({ execution_modes: ["paper", "dry_run"] }))).toBe(false);
    expect(liveModeSupported(options({ execution_modes: ["paper", "dry_run", "live"] }))).toBe(true);
    // Options not loaded yet is "unknown", not "unsupported".
    expect(supportedExecutionModes(undefined)).toEqual([]);
    expect(isModeSupported(options(), "paper")).toBe(true);
    expect(isModeSupported(options(), "live")).toBe(false);
  });

  it("defaults a new strategy to paper even when live is offered", () => {
    expect(preferredCreateMode(["live", "paper", "dry_run"])).toBe("paper");
    expect(preferredCreateMode(["live", "dry_run"])).toBe("dry_run");
    expect(preferredCreateMode(["live", "paper"])).toBe("paper");
    expect(preferredCreateMode([])).toBe("paper");
  });

  it("never auto-selects live, not even when it is the only offered mode", () => {
    expect(preferredCreateMode(["live"])).toBe("paper");
    expect(preferredCreateMode(["live"])).not.toBe("live");
  });

  it("labels the supported live lanes and reports an unreported list as unknown", () => {
    expect(liveLaneSummary(options({ live_lanes: ["cnc", "mis", "futures", "options"] }))).toBe(
      "CNC / portfolio · MIS · Futures / rolls · Options",
    );
    expect(liveLaneSummary(options({ live_lanes: ["cnc", "custom_lane"] }))).toBe(
      "CNC / portfolio · custom_lane",
    );
    expect(liveLaneSummary(options({ live_lanes: [] }))).toBeNull();
    expect(liveLaneSummary(options())).toBeNull();
  });

  it("treats owner approval as mandatory unless the server says otherwise", () => {
    expect(liveRequiresOwnerApproval(options())).toBe(true);
    expect(liveRequiresOwnerApproval(options({ live_requires_owner_approval: true }))).toBe(true);
    expect(liveRequiresOwnerApproval(options({ live_requires_owner_approval: false }))).toBe(false);
  });

  it("names the blocking attempt through the server's own flag", () => {
    expect(blockingJob([job()])).toBeUndefined();
    expect(blockingJob([job({ status: "running", replacement_blocked: true })])?.job_id).toBe("j-1");
  });

  it("mirrors the store's blocking rule when the summary omits the flag", () => {
    expect(blockingJob([job({ status: "queued", replacement_blocked: undefined })])).toBeDefined();
    expect(
      blockingJob([job({ status: "recovery_required", replacement_blocked: undefined })]),
    ).toBeDefined();
    expect(
      blockingJob([
        job({
          status: "recovery_required",
          replacement_blocked: undefined,
          reconciled_at: "2026-09-22T05:00:00Z",
        }),
      ]),
    ).toBeUndefined();
    // `fencing` is not in the store's blocking set, so the UI does not invent one.
    expect(blockingJob([job({ status: "fencing", replacement_blocked: false })])).toBeUndefined();
  });

  it("blocks a live strategy while the deployment does not offer live", () => {
    const gate = runNowGate({ mode: "live", options: options(), jobs: [] });
    expect(gate.blocked).toBe(true);
    expect(gate.reason).toMatch(/Live execution is not enabled on this deployment/);
    // The strategy keeps its live mode; nothing is rewritten to paper.
    expect(gate.reason).not.toMatch(/switched/i);
  });

  it("leaves paper strategies usable while live is disabled", () => {
    expect(runNowGate({ mode: "paper", options: options(), jobs: [] })).toEqual({
      blocked: false,
      reason: null,
    });
  });

  it("reports a running attempt separately from a server capability gap", () => {
    const running = runNowGate({
      mode: "paper",
      options: options(),
      jobs: [job({ status: "running", replacement_blocked: true })],
    });
    expect(running.blocked).toBe(true);
    expect(running.reason).toMatch(/Attempt #2 is running/);
    expect(running.reason).not.toMatch(/not enabled on this deployment/);

    const recovered = runNowGate({
      mode: "paper",
      options: options(),
      jobs: [job({ status: "recovery_required", replacement_blocked: true, reconciled_at: null })],
    });
    expect(recovered.reason).toMatch(/reconciled/);
  });

  it("holds the launch while the capability is unknown instead of allowing it", () => {
    const loading = runNowGate({ mode: "live", options: undefined, jobs: [] });
    expect(loading.blocked).toBe(true);
    expect(loading.reason).toMatch(/Waiting for this deployment's supported execution modes/);

    const unavailable = runNowGate({
      mode: "live",
      options: undefined,
      jobs: [],
      capability: "unavailable",
    });
    expect(unavailable.blocked).toBe(true);
    expect(unavailable.reason).toMatch(/could not be loaded/);
  });

  it("holds a launch for a paper strategy too until the capability is known", () => {
    expect(runNowGate({ mode: "paper", options: undefined, jobs: [] }).blocked).toBe(true);
    expect(
      runNowGate({ mode: "paper", options: options(), jobs: [] }).blocked,
    ).toBe(false);
  });

  it("maps the options query onto the capability state", () => {
    expect(modeCapabilityState(undefined, false)).toBe("loading");
    expect(modeCapabilityState(undefined, true)).toBe("unavailable");
    expect(modeCapabilityState(options(), false)).toBe("ready");
  });
});
