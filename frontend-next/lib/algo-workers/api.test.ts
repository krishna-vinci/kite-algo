import { afterEach, describe, expect, it, vi } from "vitest";

import { createAlgoWorkerToken, getKiteProfile } from "@/lib/algo-workers/api";

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("algo worker API client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("creates live-enabled tokens with explicit modes and broker account scope", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        token_id: "worker_1",
        token: "kwa_secret",
        name: "live-worker",
        account_scope: "kite:AB1234",
        allowed_modes: ["paper", "dry_run", "live"],
        allowed_actions: ["runs:create"],
        allowed_templates: [],
        status: "active",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const token = await createAlgoWorkerToken({
      name: "live-worker",
      accountScope: "kite:AB1234",
      allowedModes: ["paper", "dry_run", "live"],
      allowedActions: ["runs:create"],
      allowedTemplates: ["mean-reversion"],
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/algo-workers/tokens",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({
          name: "live-worker",
          account_scope: "kite:AB1234",
          allowed_modes: ["paper", "dry_run", "live"],
          allowed_actions: ["runs:create"],
          allowed_templates: ["mean-reversion"],
          expires_at: null,
        }),
      }),
    );
    expect(token.allowedModes).toEqual(["paper", "dry_run", "live"]);
  });

  it("normalizes the Kite profile user id used for live account scopes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ user_id: " AB1234 ", user_name: "Krishna" })),
    );

    await expect(getKiteProfile()).resolves.toMatchObject({
      userId: "AB1234",
      userName: "Krishna",
    });
  });
});
