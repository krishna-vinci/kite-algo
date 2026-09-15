import { describe, expect, it } from "vitest";

import { alertsErrorMessage, isForbidden, isNotFound } from "./errors";
import { ApiClientError } from "@/lib/api/client";

describe("alertsErrorMessage", () => {
  it("prefers a string detail over the status text", () => {
    const error = new ApiClientError(403, { detail: "the scope is not authorized" }, "Forbidden");
    expect(alertsErrorMessage(error, "fallback")).toBe("the scope is not authorized");
  });

  it("maps a CSRF origin refusal to the fix rather than the backend wording", () => {
    const error = new ApiClientError(
      403,
      { detail: "cross-origin request refused for this cookie-authenticated route: http://192.168.0.128:13000" },
      "Forbidden",
    );
    const shown = alertsErrorMessage(error, "fallback");
    expect(shown).toContain("APP_ALLOWED_ORIGINS");
    expect(shown).toContain("192.168.0.128:13000");
  });

  it("reads the nested missing_env_secret message and names the variable", () => {
    // The channel test-send 400 names the unresolved env var, which is the
    // actionable part; the status text ("Bad Request") is not.
    const error = new ApiClientError(
      400,
      {
        detail: {
          error: "missing_env_secret",
          secret_env: "TELEGRAM_BOT_TOKEN",
          message: "environment variable TELEGRAM_BOT_TOKEN is not set on the server, so this channel cannot send",
        },
      },
      "Bad Request",
    );
    const message = alertsErrorMessage(error, "fallback");
    expect(message).toContain("TELEGRAM_BOT_TOKEN");
    expect(message).toContain("not set");
  });

  it("falls back to status-specific copy when the detail is empty", () => {
    expect(alertsErrorMessage(new ApiClientError(404, null, "Not Found"), "fallback")).toBe(
      "Not found in the selected scope.",
    );
    expect(alertsErrorMessage(new ApiClientError(503, null, "Service Unavailable"), "fallback")).toBe(
      "A required dependency (catalog or source) is unavailable. This is retryable.",
    );
  });

  it("uses a plain Error message and finally the fallback", () => {
    expect(alertsErrorMessage(new Error("boom"), "fallback")).toBe("boom");
    expect(alertsErrorMessage("nope", "fallback")).toBe("fallback");
  });

  it("classifies forbidden and not-found", () => {
    expect(isForbidden(new ApiClientError(403, null))).toBe(true);
    expect(isForbidden(new ApiClientError(404, null))).toBe(false);
    expect(isNotFound(new ApiClientError(404, null))).toBe(true);
    expect(isNotFound(new Error("x"))).toBe(false);
  });
});

describe("origin refusals", () => {
  it("turns the CSRF refusal into the action that fixes it", () => {
    const message =
      "cross-origin request refused for this cookie-authenticated route: http://192.168.0.128:13000";
    const error = new ApiClientError(403, { detail: message }, "Forbidden");
    const shown = alertsErrorMessage(error, "fallback");
    expect(shown).toContain("192.168.0.128:13000");
    expect(shown).toContain("APP_ALLOWED_ORIGINS");
    expect(shown).not.toContain("cookie-authenticated route");
  });

  it("names an unknown origin generically rather than dropping the refusal", () => {
    const error = new ApiClientError(403, { detail: "cross-origin request refused" }, "Forbidden");
    expect(alertsErrorMessage(error, "fallback")).toContain("this address");
  });
});
