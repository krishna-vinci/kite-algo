/**
 * Pure helpers for rendering the top-bar platform status: the mode chip and
 * the broker / market-data / strategy-runner dots. Kept dependency-free so
 * they can be unit tested without a DOM or network.
 */

import type { PlatformComponentState, PlatformStatus } from "@/lib/platform/types";

export type ComponentTone = "positive" | "warning" | "danger" | "neutral";

const OK_STATES = new Set(["ok", "connected"]);
const STALE_STATES = new Set(["stale"]);
const DOWN_STATES = new Set(["down", "expired"]);

export function componentTone(state: PlatformComponentState | null | undefined): ComponentTone {
  const normalized = String(state ?? "unknown").toLowerCase();
  if (OK_STATES.has(normalized)) return "positive";
  if (STALE_STATES.has(normalized)) return "warning";
  if (DOWN_STATES.has(normalized)) return "danger";
  return "neutral";
}

/**
 * Tooltip copy for one status dot. A broker in the `expired` state always
 * says "reconnect needed" — that phrase is load-bearing for operators.
 */
export function componentTooltip(
  label: string,
  state: PlatformComponentState | null | undefined,
  detail?: string | null,
): string {
  const normalized = String(state ?? "unknown").toLowerCase();
  if (normalized === "expired") {
    return `${label}: expired — reconnect needed`;
  }
  const base = `${label}: ${normalized}`;
  return detail ? `${base} (${detail})` : base;
}

/** The top-bar mode chip text: "PAPER" or "LIVE · <lane, lane, ...>". */
export function liveModeSummary(status: Pick<PlatformStatus, "mode" | "live">): string {
  if (status.mode !== "live") return "PAPER";
  return status.live.lanes_open.length > 0 ? `LIVE · ${status.live.lanes_open.join(", ")}` : "LIVE";
}
