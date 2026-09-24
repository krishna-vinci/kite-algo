/**
 * Ordinary parameter fields for the supported part of JSON Schema.
 *
 * The composer renders scalar/enum/boolean fields as real controls. A schema
 * this module cannot represent *losslessly* returns `null`, and the caller
 * offers the JSON editor instead: guessing at a nested or composed schema would
 * silently submit something other than what the author wrote.
 */

export type SchemaFieldType = "string" | "number" | "integer" | "boolean";

export type SchemaField = {
  name: string;
  type: SchemaFieldType;
  required: boolean;
  description: string | null;
  default?: unknown;
  enumValues: string[] | null;
  minimum: number | null;
  maximum: number | null;
};

export type SchemaFieldDraft = string | boolean;

const SCALAR_TYPES: readonly SchemaFieldType[] = ["string", "number", "integer", "boolean"];

/** Top-level keys the round trip preserves; anything else means "use JSON". */
const MODELED_SCHEMA_KEYS = new Set([
  "type",
  "properties",
  "required",
  "title",
  "description",
  "additionalProperties",
  "$schema",
]);

const MODELED_FIELD_KEYS = new Set([
  "type",
  "title",
  "description",
  "default",
  "enum",
  "minimum",
  "maximum",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function scalarOrUndefined(value: unknown, type: SchemaFieldType): unknown | undefined {
  if (value === undefined) return undefined;
  if (type === "string") return typeof value === "string" ? value : undefined;
  if (type === "boolean") return typeof value === "boolean" ? value : undefined;
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/** `true` when the schema says nothing at all, i.e. the strategy takes none. */
export function isSchemaEmpty(schema: unknown): boolean {
  if (schema === null || schema === undefined) return true;
  if (!isRecord(schema)) return false;
  if (Object.keys(schema).length === 0) return true;
  const properties = schema.properties;
  if (!isRecord(properties)) return false;
  return (
    Object.keys(properties).length === 0 &&
    (schema.type === undefined || schema.type === "object") &&
    (schema.required === undefined || (Array.isArray(schema.required) && schema.required.length === 0))
  );
}

/**
 * The fields a schema can be shown as, or `null` when it cannot be represented
 * losslessly (nested objects/arrays, `$ref`, `oneOf`/`anyOf`/`allOf`, patterns,
 * non-scalar defaults, unknown keywords, ...).
 */
export function readSchemaFields(schema: unknown): SchemaField[] | null {
  if (isSchemaEmpty(schema)) return [];
  if (!isRecord(schema)) return null;
  if (Object.keys(schema).some((key) => !MODELED_SCHEMA_KEYS.has(key))) return null;
  if (schema.type !== undefined && schema.type !== "object") return null;
  if (
    schema.additionalProperties !== undefined &&
    typeof schema.additionalProperties !== "boolean"
  ) {
    return null;
  }

  const properties = schema.properties;
  if (properties !== undefined && !isRecord(properties)) return null;
  const requiredRaw = schema.required;
  if (requiredRaw !== undefined && !Array.isArray(requiredRaw)) return null;
  const required = new Set<string>(
    Array.isArray(requiredRaw) ? requiredRaw.filter((entry): entry is string => typeof entry === "string") : [],
  );
  if (Array.isArray(requiredRaw) && required.size !== requiredRaw.length) return null;

  const fields: SchemaField[] = [];
  for (const [name, raw] of Object.entries(properties ?? {})) {
    if (!isRecord(raw)) return null;
    if (required.has(name) && requiredRaw === undefined) return null;
    if (Object.keys(raw).some((key) => !MODELED_FIELD_KEYS.has(key))) return null;
    const type = raw.type;
    if (typeof type !== "string" || !SCALAR_TYPES.includes(type as SchemaFieldType)) return null;
    const fieldType = type as SchemaFieldType;

    let enumValues: string[] | null = null;
    if (raw.enum !== undefined) {
      if (fieldType !== "string" || !Array.isArray(raw.enum)) return null;
      if (!raw.enum.every((entry) => typeof entry === "string")) return null;
      enumValues = raw.enum as string[];
    }
    for (const bound of ["minimum", "maximum"] as const) {
      if (raw[bound] !== undefined && typeof raw[bound] !== "number") return null;
    }
    if (fieldType === "boolean" && (raw.minimum !== undefined || raw.maximum !== undefined)) return null;
    if (raw.description !== undefined && typeof raw.description !== "string") return null;
    if (raw.title !== undefined && typeof raw.title !== "string") return null;
    const defaultValue = scalarOrUndefined(raw.default, fieldType);
    if (raw.default !== undefined && defaultValue === undefined) return null;

    fields.push({
      name,
      type: fieldType,
      required: required.has(name),
      description:
        typeof raw.description === "string"
          ? raw.description
          : typeof raw.title === "string"
            ? raw.title
            : null,
      default: defaultValue,
      enumValues,
      minimum: typeof raw.minimum === "number" ? raw.minimum : null,
      maximum: typeof raw.maximum === "number" ? raw.maximum : null,
    });
  }

  const unlisted = [...required].filter((name) => !(name in (properties ?? {})));
  if (unlisted.length > 0) return null;
  return fields;
}

/**
 * The schema a set of ordinary fields describes.
 *
 * `additionalProperties` is deliberately left at the JSON Schema default so a
 * platform-stamped value (the strategy identity the child must send back) can
 * ride along with the author's own fields.
 */
export function buildParametersSchema(fields: readonly SchemaField[]): Record<string, unknown> {
  if (fields.length === 0) return {};
  const properties: Record<string, unknown> = {};
  const required: string[] = [];
  for (const field of fields) {
    const spec: Record<string, unknown> = { type: field.type };
    if (field.description) spec.description = field.description;
    if (field.default !== undefined) spec.default = field.default;
    if (field.enumValues && field.enumValues.length > 0) spec.enum = [...field.enumValues];
    if (field.minimum !== null) spec.minimum = field.minimum;
    if (field.maximum !== null) spec.maximum = field.maximum;
    properties[field.name] = spec;
    if (field.required) required.push(field.name);
  }
  return { type: "object", properties, required };
}

/** Human-readable constraints, so a field explains itself without the raw JSON. */
export function fieldConstraintSummary(field: SchemaField): string {
  const parts: string[] = [];
  if (field.enumValues && field.enumValues.length > 0) parts.push(field.enumValues.join(" / "));
  if (field.minimum !== null && field.maximum !== null) {
    parts.push(`${field.minimum} to ${field.maximum}`);
  } else if (field.minimum !== null) {
    parts.push(`at least ${field.minimum}`);
  } else if (field.maximum !== null) {
    parts.push(`at most ${field.maximum}`);
  }
  if (field.required) parts.push("required");
  if (field.default !== undefined) parts.push(`defaults to ${JSON.stringify(field.default)}`);
  return parts.join(" · ");
}

/** Starting values for the launch form: defaults only, never invented numbers. */
export function defaultFieldDrafts(fields: readonly SchemaField[]): Record<string, SchemaFieldDraft> {
  const drafts: Record<string, SchemaFieldDraft> = {};
  for (const field of fields) {
    if (field.type === "boolean") {
      drafts[field.name] = field.default === true;
    } else if (field.default !== undefined) {
      drafts[field.name] = String(field.default);
    } else {
      drafts[field.name] = "";
    }
  }
  return drafts;
}

export type ParamsBuildResult =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; errors: Record<string, string> };

/**
 * Turn the form drafts into the params object, refusing what the client can
 * already see is wrong. The server validates again; this only makes the error
 * land on the field the operator typed into.
 */
export function buildParamsFromFields(
  fields: readonly SchemaField[],
  drafts: Record<string, SchemaFieldDraft>,
): ParamsBuildResult {
  const value: Record<string, unknown> = {};
  const errors: Record<string, string> = {};

  for (const field of fields) {
    const draft = drafts[field.name];
    if (field.type === "boolean") {
      const raw = draft === undefined ? field.default === true : draft === true;
      // A yes/no control always has a state, so the value is always sent. That
      // is unambiguous under `additionalProperties: false` too, because the
      // property is one the schema declares.
      value[field.name] = raw;
      continue;
    }
    const text = draft === undefined ? "" : String(draft).trim();
    if (text === "") {
      if (field.default !== undefined) {
        value[field.name] = field.default;
      } else if (field.required) {
        errors[field.name] =
          field.enumValues && field.enumValues.length > 0
            ? `Choose one of: ${field.enumValues.join(", ")}.`
            : "This parameter is required.";
      }
      continue;
    }
    if (field.enumValues && field.enumValues.length > 0 && !field.enumValues.includes(text)) {
      errors[field.name] = `Choose one of: ${field.enumValues.join(", ")}.`;
      continue;
    }
    if (field.type === "string") {
      value[field.name] = text;
      continue;
    }
    const numeric = Number(text);
    if (!Number.isFinite(numeric)) {
      errors[field.name] = "Enter a number.";
      continue;
    }
    if (field.type === "integer" && !Number.isInteger(numeric)) {
      errors[field.name] = "Enter a whole number.";
      continue;
    }
    if (field.minimum !== null && numeric < field.minimum) {
      errors[field.name] = `Must be at least ${field.minimum}.`;
      continue;
    }
    if (field.maximum !== null && numeric > field.maximum) {
      errors[field.name] = `Must be at most ${field.maximum}.`;
      continue;
    }
    value[field.name] = numeric;
  }

  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return { ok: true, value };
}
