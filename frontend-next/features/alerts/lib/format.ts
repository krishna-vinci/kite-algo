/**
 * Pure formatting helpers for the alerts UI. No React, no fetch — every
 * function here is deterministic and unit-testable.
 */

/** Compact relative age: "0s", "45s", "12m", "3h", "2d". `null` stays null. */
export function formatAge(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return null;
  }
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

/** "3m ago" / "never". The "never" case is deliberate, not an empty string. */
export function formatAgeAgo(seconds: number | null | undefined): string {
  const age = formatAge(seconds);
  return age === null ? "never" : `${age} ago`;
}

/**
 * Render an ISO timestamp as a short local time, or `null` when absent.
 *
 * Returns null rather than "Invalid Date" / "--" so callers decide the
 * fallback: an absent timestamp means UNKNOWN, and the UI should say so in its
 * own words rather than receiving a fake value.
 */
export function formatTimestamp(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Thousands-separated integer, `null` preserved. */
export function formatCount(value: number | null | undefined): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return value.toLocaleString();
}

/** Truncate a long identifier list for a table cell. */
export function summarizeList(items: string[], visible = 3): string {
  if (items.length === 0) return "none";
  if (items.length <= visible) return items.join(", ");
  return `${items.slice(0, visible).join(", ")} +${items.length - visible} more`;
}
