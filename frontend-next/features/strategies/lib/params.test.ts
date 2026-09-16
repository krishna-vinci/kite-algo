import { describe, expect, it } from "vitest";

import { parseParamsInput } from "./params";

describe("parseParamsInput", () => {
  it("treats empty input as an empty object", () => {
    expect(parseParamsInput("   ")).toEqual({ ok: true, value: {} });
  });

  it("parses a JSON object", () => {
    expect(parseParamsInput('{"lots": 2}')).toEqual({ ok: true, value: { lots: 2 } });
  });

  it("rejects non-object and invalid JSON", () => {
    expect(parseParamsInput("[1, 2]").ok).toBe(false);
    expect(parseParamsInput('"text"').ok).toBe(false);
    expect(parseParamsInput("{oops").ok).toBe(false);
  });
});
