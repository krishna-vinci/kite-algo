"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { AlertCircleIcon, CodeIcon, FileWarningIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { SectionLabel } from "@/components/operator/section-label";
import { OperatorIssueList } from "@/features/alerts/components/operator-issue-list";
import { patchAlertsWorkflow, validateAlertsWorkflow } from "@/features/alerts/api";
import { alertsKeys } from "@/features/alerts/hooks/keys";
import type { AlertsIssue } from "@/features/alerts/types";
import { ApiClientError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

type EditorMode = "yaml" | "json";

export type AdvancedDefinitionEditorProps = Readonly<{
  workflowId: string;
  scope: string | null;
  name: string;
  /** The revision the loaded definition came from; PATCH is compare-and-insert. */
  expectedRevision: number;
  initialYaml?: string | null;
  initialDocument?: Record<string, unknown> | null;
  /** Why the structured form is unavailable, shown to the operator. */
  reason?: string;
}>;

/**
 * Advanced YAML/JSON definition editor.
 *
 * This is the lossless escape hatch for definitions the structured form does
 * not model. It edits the EXACT serialized definition and saves through the
 * normal revisioned `PATCH` path with `expected_revision`, so:
 *   - nothing is reconstructed from a partial model (no field can be dropped);
 *   - server validation stays authoritative (the compiler is the only judge);
 *   - a `409` preserves the operator's unsaved text and offers a reload, rather
 *     than silently overwriting with a refreshed revision.
 *
 * A read-only page is never called an editor; this one really writes.
 */
export function AdvancedDefinitionEditor({
  workflowId,
  scope,
  name,
  expectedRevision,
  initialYaml,
  initialDocument,
  reason,
}: AdvancedDefinitionEditorProps) {
  const router = useRouter();
  const queryClient = useQueryClient();

  const hasYaml = typeof initialYaml === "string" && initialYaml.trim() !== "";
  const [mode, setMode] = useState<EditorMode>(hasYaml ? "yaml" : "json");
  const [text, setText] = useState(() =>
    hasYaml ? (initialYaml as string) : JSON.stringify(initialDocument ?? {}, null, 2),
  );
  const [parseError, setParseError] = useState<string | null>(null);
  const [issues, setIssues] = useState<AlertsIssue[] | null>(null);
  const [conflict, setConflict] = useState(false);

  const currentText = useMemo(() => text, [text]);

  const buildPayload = (): { yaml_text?: string; document?: Record<string, unknown> } | { error: string } => {
    if (mode === "yaml") return { yaml_text: currentText };
    try {
      const parsed = JSON.parse(currentText);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return { error: "The JSON document must be an object." };
      }
      return { document: parsed as Record<string, unknown> };
    } catch (error) {
      return { error: error instanceof Error ? error.message : "Invalid JSON." };
    }
  };

  const validateMutation = useMutation({
    mutationFn: async () => {
      const payload = buildPayload();
      if ("error" in payload) throw new Error(payload.error);
      return validateAlertsWorkflow(payload, scope);
    },
    onMutate: () => {
      setParseError(null);
      setConflict(false);
    },
    onSuccess: (response) => setIssues(response.issues),
    onError: (error) => setParseError(error instanceof Error ? error.message : "Validation failed."),
  });

  const saveMutation = useMutation({
    mutationFn: async () => {
      const payload = buildPayload();
      if ("error" in payload) throw new Error(payload.error);
      return patchAlertsWorkflow(workflowId, { ...payload, expected_revision: expectedRevision }, scope);
    },
    onMutate: () => {
      setParseError(null);
      setConflict(false);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: alertsKeys.workflow(workflowId, scope) });
      await queryClient.invalidateQueries({ queryKey: alertsKeys.workflows(scope, false) });
      router.push(`/alerts/${workflowId}`);
    },
    onError: (error) => {
      if (error instanceof ApiClientError && error.status === 409) {
        setConflict(true);
        return;
      }
      setParseError(error instanceof Error ? error.message : "Could not save.");
    },
  });

  const hasErrors = (issues ?? []).some((issue) => issue.severity === "error");

  return (
    <div className="flex flex-col gap-4 pb-8">
      <SectionLabel
        eyebrow="Advanced"
        title={`Edit ${name} as ${mode === "yaml" ? "YAML" : "JSON"}`}
        description="The canonical definition, saved through the normal revisioned path."
      />

      <Alert>
        <FileWarningIcon />
        <AlertTitle>Advanced definition editing</AlertTitle>
        <AlertDescription>
          <p>
            {reason ??
              "This definition is edited as raw YAML/JSON because the structured form does not model every part of it."}{" "}
            Nothing is reconstructed from a partial model here: the text below IS the definition,
            and saving validates it on the server and writes a new draft revision under the
            revision you loaded (revision {expectedRevision}).
          </p>
          <p className="mt-2">
            A no-op save keeps the canonical semantics.{" "}
            <Link href={`/alerts/${workflowId}`} className="underline">
              Open the read-only definition
            </Link>{" "}
            if you only want to inspect it.
          </p>
        </AlertDescription>
      </Alert>

      <div className="flex flex-wrap gap-2">
        {(["yaml", "json"] as const).map((option) => (
          <Button
            key={option}
            type="button"
            size="sm"
            variant={mode === option ? "default" : "outline"}
            aria-pressed={mode === option}
            onClick={() => {
              if (option === mode) return;
              if (option === "json") {
                setText(JSON.stringify(initialDocument ?? {}, null, 2));
              } else if (hasYaml) {
                setText(initialYaml as string);
              }
              setMode(option);
              setIssues(null);
              setParseError(null);
            }}
          >
            {option.toUpperCase()}
          </Button>
        ))}
      </div>

      <div className="flex flex-col gap-1">
        <Label htmlFor="advanced-definition">
          {mode === "yaml" ? "Definition (YAML)" : "Definition (JSON)"}
        </Label>
        <Textarea
          id="advanced-definition"
          value={text}
          onChange={(event) => setText(event.target.value)}
          rows={24}
          spellCheck={false}
          aria-label={`Workflow definition as ${mode.toUpperCase()}`}
          className={cn("font-mono text-xs", hasErrors && "border-rose-400/60")}
        />
      </div>

      {parseError ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Not saved</AlertTitle>
          <AlertDescription>{parseError}</AlertDescription>
        </Alert>
      ) : null}

      {issues ? (
        <div className="rounded-lg border border-border/60 p-3">
          {issues.length === 0 ? (
            <p className="text-sm text-emerald-300">Valid, no issues.</p>
          ) : (
            <OperatorIssueList issues={issues} bare />
          )}
        </div>
      ) : null}

      {conflict ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>This alert changed while you were editing</AlertTitle>
          <AlertDescription>
            The revision moved on, so your text was not applied and nothing was overwritten. Your
            edit is still in the box above. Reload the latest revision in another tab, merge your
            change, then save again.{" "}
            <button type="button" className="underline" onClick={() => router.refresh()}>
              Reload
            </button>
          </AlertDescription>
        </Alert>
      ) : null}

      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          disabled={validateMutation.isPending}
          onClick={() => validateMutation.mutate()}
        >
          {validateMutation.isPending ? "Validating…" : "Validate"}
        </Button>
        <Button
          type="button"
          disabled={saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          <CodeIcon className="size-4" aria-hidden />
          {saveMutation.isPending ? "Saving…" : "Save as new draft revision"}
        </Button>
      </div>
    </div>
  );
}
