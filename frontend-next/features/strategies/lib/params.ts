/**
 * Parameters for a launch are entered as JSON in the browser and validated
 * against the pinned version's schema *server-side*. This only catches obvious
 * shape errors before a request; it is not authoritative validation.
 */

export type ParamsParseResult =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; error: string };

export function parseParamsInput(text: string): ParamsParseResult {
  const trimmed = text.trim();
  if (!trimmed) return { ok: true, value: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { ok: false, error: "Parameters are not valid JSON." };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, error: "Parameters must be a JSON object." };
  }
  return { ok: true, value: parsed as Record<string, unknown> };
}
