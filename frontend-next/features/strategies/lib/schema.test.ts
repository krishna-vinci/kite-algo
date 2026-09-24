import { describe, expect, it } from "vitest";

import {
  buildParametersSchema,
  buildParamsFromFields,
  defaultFieldDrafts,
  fieldConstraintSummary,
  isSchemaEmpty,
  readSchemaFields,
  type SchemaField,
} from "./schema";

const QUANTITY: SchemaField = {
  name: "quantity",
  type: "integer",
  required: true,
  description: "Contracts to trade",
  enumValues: null,
  minimum: 1,
  maximum: 10,
};

const MODE: SchemaField = {
  name: "mode",
  type: "string",
  required: false,
  description: null,
  enumValues: ["intraday", "positional"],
  minimum: null,
  maximum: null,
};

describe("parameter schema fields", () => {
  it("treats an empty or parameterless schema as no parameters", () => {
    expect(isSchemaEmpty(null)).toBe(true);
    expect(isSchemaEmpty({})).toBe(true);
    expect(isSchemaEmpty({ type: "object", properties: {}, required: [] })).toBe(true);
    expect(readSchemaFields({})).toEqual([]);
  });

  it("round-trips the scalar fields it can show", () => {
    const built = buildParametersSchema([QUANTITY, MODE]);
    const read = readSchemaFields(built);
    expect(read).not.toBeNull();
    expect(read?.map((field) => [field.name, field.type, field.required])).toEqual([
      ["quantity", "integer", true],
      ["mode", "string", false],
    ]);
    expect(read?.[0].minimum).toBe(1);
    expect(read?.[1].enumValues).toEqual(["intraday", "positional"]);
  });

  it("adds no required or default values the author did not ask for", () => {
    expect(buildParametersSchema([MODE])).toEqual({
      type: "object",
      properties: { mode: { type: "string", enum: ["intraday", "positional"] } },
      required: [],
    });
    expect(buildParametersSchema([])).toEqual({});
  });

  it("refuses schemas it cannot represent losslessly", () => {
    expect(
      readSchemaFields({ type: "object", properties: { nested: { type: "object", properties: {} } } }),
    ).toBeNull();
    expect(readSchemaFields({ type: "object", properties: { list: { type: "array" } } })).toBeNull();
    expect(readSchemaFields({ $ref: "#/definitions/x" })).toBeNull();
    expect(readSchemaFields({ oneOf: [{ type: "object" }] })).toBeNull();
    expect(
      readSchemaFields({ type: "object", properties: { x: { type: "number", pattern: "^1" } } }),
    ).toBeNull();
    expect(
      readSchemaFields({ type: "object", properties: { x: { type: "string", default: 3 } } }),
    ).toBeNull();
    expect(readSchemaFields({ type: "object", properties: {}, required: ["missing"] })).toBeNull();
  });

  it("describes a field's constraints in words", () => {
    expect(fieldConstraintSummary(QUANTITY)).toContain("1 to 10");
    expect(fieldConstraintSummary(QUANTITY)).toContain("required");
    expect(fieldConstraintSummary(MODE)).toContain("intraday / positional");
  });

  it("builds params from the form and refuses missing or out-of-range values", () => {
    expect(defaultFieldDrafts([QUANTITY, MODE])).toEqual({ quantity: "", mode: "" });

    const missing = buildParamsFromFields([QUANTITY], { quantity: "" });
    expect(missing.ok).toBe(false);
    if (!missing.ok) expect(missing.errors.quantity).toBe("This parameter is required.");

    const tooBig = buildParamsFromFields([QUANTITY], { quantity: "11" });
    expect(tooBig.ok).toBe(false);
    if (!tooBig.ok) expect(tooBig.errors.quantity).toBe("Must be at most 10.");

    expect(buildParamsFromFields([QUANTITY], { quantity: "1.5" }).ok).toBe(false);

    expect(buildParamsFromFields([QUANTITY, MODE], { quantity: "3", mode: "positional" })).toEqual({
      ok: true,
      value: { quantity: 3, mode: "positional" },
    });

    expect(buildParamsFromFields([MODE], { mode: "" })).toEqual({ ok: true, value: {} });
  });

  it("sends explicit boolean, zero and enum values, and invents nothing", () => {
    const flag: SchemaField = {
      name: "enabled",
      type: "boolean",
      required: false,
      description: null,
      enumValues: null,
      minimum: null,
      maximum: null,
    };
    const quantity: SchemaField = {
      name: "quantity",
      type: "number",
      required: false,
      description: null,
      enumValues: null,
      minimum: null,
      maximum: null,
    };
    expect(buildParamsFromFields([flag, quantity], { enabled: false, quantity: "0" })).toEqual({
      ok: true,
      value: { enabled: false, quantity: 0 },
    });
    expect(buildParamsFromFields([MODE], { mode: "positional" })).toEqual({
      ok: true,
      value: { mode: "positional" },
    });
    // An out-of-enum value is refused rather than sent.
    expect(buildParamsFromFields([MODE], { mode: "overnight" }).ok).toBe(false);
    // Nothing is invented: an untouched optional field is not added.
    expect(buildParamsFromFields([quantity], { quantity: "" })).toEqual({ ok: true, value: {} });
  });
});
