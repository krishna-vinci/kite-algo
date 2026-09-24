"use client";

/**
 * Parameter VALUES, not just their shape.
 *
 * One hook and two presentational components are shared by the composer (the
 * first run), the strategy page's Run now, and the schedule panel, so all three
 * collect, validate and send the same things:
 *
 * - a schema this app can show losslessly becomes ordinary inputs, including
 *   `required` fields with no default, `false`/`0` values, enums and ranges;
 * - a schema it cannot show becomes a JSON editor, and never a guess;
 * - the resulting object contains exactly the author's parameters. The platform
 *   does NOT stamp extra keys into user parameters: a strict
 *   (`additionalProperties: false`) schema would reject them, and the user's own
 *   parameter names are theirs.
 *
 * Callers that switch between versions/schemas must key the component (or the
 * hook's owner) by that identity, so drafts reset with the pinned revision.
 */

import { useMemo, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  type SchemaField,
  type SchemaFieldDraft,
  buildParamsFromFields,
  defaultFieldDrafts,
  fieldConstraintSummary,
  readSchemaFields,
} from "@/features/strategies/lib/schema";

export type HostedParamValues = {
  /** `null` means the schema cannot be shown losslessly (JSON mode). */
  fields: SchemaField[] | null;
  jsonMode: boolean;
  drafts: Record<string, SchemaFieldDraft>;
  setDraft: (name: string, value: SchemaFieldDraft) => void;
  jsonText: string;
  setJsonText: (value: string) => void;
  value: Record<string, unknown>;
  errors: Record<string, string>;
  valid: boolean;
};

const JSON_KEY = "__json__";

export function useHostedParamValues(schema: unknown): HostedParamValues {
  const fields = useMemo(() => readSchemaFields(schema), [schema]);
  // Drafts belong to ONE schema. Keying them by the schema itself means a
  // version/schema switch drops the previous drafts instead of silently
  // carrying values into a different parameter set.
  const schemaKey = useMemo(() => JSON.stringify(schema ?? null), [schema]);
  const [draftState, setDraftState] = useState<{ key: string; edits: Record<string, SchemaFieldDraft> }>(
    { key: schemaKey, edits: {} },
  );
  const [jsonState, setJsonState] = useState<{ key: string; text: string }>({
    key: schemaKey,
    text: "{}",
  });

  const drafts = useMemo(
    () => ({
      ...defaultFieldDrafts(fields ?? []),
      ...(draftState.key === schemaKey ? draftState.edits : {}),
    }),
    [draftState, fields, schemaKey],
  );
  const jsonText = jsonState.key === schemaKey ? jsonState.text : "{}";

  const built = useMemo(() => {
    if (fields === null) {
      const trimmed = jsonText.trim();
      if (!trimmed) {
        return { ok: false as const, errors: { [JSON_KEY]: "Enter the parameters as a JSON object." } };
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(trimmed);
      } catch {
        return { ok: false as const, errors: { [JSON_KEY]: "Parameters are not valid JSON." } };
      }
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return { ok: false as const, errors: { [JSON_KEY]: "Parameters must be a JSON object." } };
      }
      return { ok: true as const, value: parsed as Record<string, unknown> };
    }
    if (fields.length === 0) return { ok: true as const, value: {} };
    return buildParamsFromFields(fields, drafts);
  }, [drafts, fields, jsonText]);

  return {
    fields,
    jsonMode: fields === null,
    drafts,
    setDraft: (name, value) =>
      setDraftState((current) => ({
        key: schemaKey,
        edits: { ...(current.key === schemaKey ? current.edits : {}), [name]: value },
      })),
    jsonText,
    setJsonText: (text) => setJsonState({ key: schemaKey, text }),
    value: built.ok ? built.value : {},
    errors: built.ok ? {} : built.errors,
    valid: built.ok,
  };
}

/** The value editor for one field, used inline by the composer's field rows. */
export function ParamValueInput({
  field,
  value,
  onChange,
  idPrefix,
}: Readonly<{
  field: SchemaField;
  value: SchemaFieldDraft | undefined;
  onChange: (value: SchemaFieldDraft) => void;
  idPrefix: string;
}>) {
  const id = `${idPrefix}-${field.name}`;
  if (field.type === "boolean") {
    return (
      <Select
        value={value === true ? "true" : "false"}
        onValueChange={(next) => onChange(next === "true")}
      >
        <SelectTrigger id={id} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="true">Yes</SelectItem>
          <SelectItem value="false">No</SelectItem>
        </SelectContent>
      </Select>
    );
  }
  if (field.enumValues && field.enumValues.length > 0) {
    return (
      <Select value={String(value ?? "")} onValueChange={onChange}>
        <SelectTrigger id={id} className="w-full">
          <SelectValue placeholder="Choose a value" />
        </SelectTrigger>
        <SelectContent>
          {field.enumValues.map((option) => (
            <SelectItem key={option} value={option}>
              {option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }
  return (
    <Input
      id={id}
      inputMode={field.type === "string" ? "text" : "decimal"}
      value={String(value ?? "")}
      onChange={(event) => onChange(event.target.value)}
      placeholder={field.default !== undefined ? `Default ${JSON.stringify(field.default)}` : ""}
    />
  );
}

/** The whole value section for one pinned schema: inputs, or the JSON editor. */
export function HostedParamInputs({
  params,
  idPrefix,
  columns = 2,
}: Readonly<{
  params: HostedParamValues;
  idPrefix: string;
  columns?: 1 | 2;
}>) {
  if (params.jsonMode) {
    return (
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${idPrefix}-json`}>Parameters (JSON)</Label>
        <Textarea
          id={`${idPrefix}-json`}
          rows={4}
          className="font-mono text-xs"
          value={params.jsonText}
          onChange={(event) => params.setJsonText(event.target.value)}
        />
        {params.errors[JSON_KEY] ? (
          <p className="text-xs text-destructive" role="alert">
            {params.errors[JSON_KEY]}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">
            This schema uses shapes the form cannot show field by field, so the values are entered as
            JSON and validated by the server.
          </p>
        )}
      </div>
    );
  }
  if ((params.fields ?? []).length === 0) {
    return <p className="text-xs text-muted-foreground">This version takes no parameters.</p>;
  }
  return (
    <div className={columns === 2 ? "grid gap-4 md:grid-cols-2" : "flex flex-col gap-4"}>
      {(params.fields ?? []).map((field) => (
        <div key={field.name} className="grid gap-1.5">
          <Label htmlFor={`${idPrefix}-${field.name}`}>
            {field.name}
            {field.required ? " *" : ""}
          </Label>
          <ParamValueInput
            field={field}
            value={params.drafts[field.name]}
            onChange={(next) => params.setDraft(field.name, next)}
            idPrefix={idPrefix}
          />
          <p className="text-xs text-muted-foreground">
            {[field.description, fieldConstraintSummary(field)].filter(Boolean).join(" · ")}
          </p>
          {params.errors[field.name] ? (
            <p className="text-xs text-destructive" role="alert">
              {params.errors[field.name]}
            </p>
          ) : null}
        </div>
      ))}
    </div>
  );
}
