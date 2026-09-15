/**
 * Turn an API failure into operator-readable text.
 *
 * The alerts operator routes put the actionable part of a failure in a
 * structured `detail` field — a plain string for scope/origin/state conflicts,
 * or an object for the channel test-send's `missing_env_secret` (handoff §10:
 * "show the variable name, because that is the actionable part"). But
 * `ApiClientError.message` is only the HTTP status text ("Forbidden", "Bad
 * Request"), so reading `error.message` discards the explanation the server
 * took the trouble to send.
 *
 * This helper reads the body first, then falls back to status-specific copy,
 * then to the raw message. It never invents an explanation for a status the
 * server did not describe.
 */
import { ApiClientError } from "@/lib/api/client";

/**
 * The one backend refusal an operator can act on themselves.
 *
 * The API refuses cookie-authenticated changes whose Origin is not on the
 * deployment's allowlist (CSRF protection). That is correct behaviour, but its
 * message names the route and the origin in backend terms; this turns it into
 * the action that fixes it.
 */
export function isOriginRefused(message: string): boolean {
  return /cross-origin request refused/i.test(message);
}

export function originRefusedMessage(message: string): string {
  const match = message.match(/https?:\/\/[^\s]+/);
  const origin = match ? match[0] : "this address";
  return (
    `This browser address (${origin}) is not allowed to make changes on the server. ` +
    "Add it to APP_ALLOWED_ORIGINS in the deployment configuration, or open the app " +
    "from an address that is already listed."
  );
}

export function alertsErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiClientError) {
    const detail = (error.body as { detail?: unknown } | null | undefined)?.detail;

    if (typeof detail === "string" && detail.trim() !== "") {
      return isOriginRefused(detail) ? originRefusedMessage(detail) : detail;
    }

    if (detail && typeof detail === "object") {
      const record = detail as Record<string, unknown>;
      if (typeof record.message === "string" && record.message.trim() !== "") {
        return record.message;
      }
      if (typeof record.error === "string" && record.error.trim() !== "") {
        const secretEnv = typeof record.secret_env === "string" ? record.secret_env : null;
        return secretEnv ? `${record.error} (${secretEnv})` : record.error;
      }
    }

    if (error.status === 403) {
      return "This scope or origin is not authorized for you. Nothing was changed.";
    }
    if (error.status === 404) {
      return "Not found in the selected scope.";
    }
    if (error.status === 503) {
      return "A required dependency (catalog or source) is unavailable. This is retryable.";
    }
    return error.message || fallback;
  }

  if (error instanceof Error) return error.message || fallback;
  return fallback;
}

/** True when the request failed because the operator is not authorized. */
export function isForbidden(error: unknown): boolean {
  return error instanceof ApiClientError && error.status === 403;
}

/** True when the resource does not exist in the selected scope. */
export function isNotFound(error: unknown): boolean {
  return error instanceof ApiClientError && error.status === 404;
}
