"use client";

import { PlusIcon, Trash2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  emptyUniverseDraft,
  universeDraftIssues,
  type UniverseDraft,
  type UniverseRefDraft,
  type UniverseRefKind,
} from "@/features/alerts/lib/authoring";
import { useAlertsCapabilities, useAlertsUniverses } from "@/features/alerts/hooks/use-alerts-queries";

type UniverseTargetingEditorProps = Readonly<{
  scope: string | null;
  value: UniverseDraft;
  onChange: (next: UniverseDraft) => void;
}>;

const KIND_HELP: Record<UniverseRefKind, string> = {
  universe: "a saved universe (explicit members, a screener's output, or a portfolio)",
  index: "an index constituent list resolved from the ingested catalog",
  watchlist: "alias of a saved universe",
};

export type UniverseNameOption = { name: string; hint: string };

type SavedUniverseLike = {
  name: string;
  kind: string;
  latest_revision?: { revision: number } | null;
};

/**
 * The names a reference of `kind` may take.
 *
 * Extracted and exported so it can be tested without opening a Radix Select
 * (which does not open under jsdom). The behaviour worth pinning is that the
 * list is DERIVED from capabilities and live data — a hard-coded copy would
 * drift from what the validator accepts, which is the whole reason the server
 * now publishes `universe_index_source_lists`.
 */
export function universeNameOptions(
  kind: UniverseRefKind,
  indexLists: string[],
  savedUniverses: SavedUniverseLike[],
): UniverseNameOption[] {
  if (kind === "index") {
    return indexLists.map((name) => ({ name, hint: "index" }));
  }
  return savedUniverses.map((universe) => ({
    name: universe.name,
    hint: `${universe.kind}${
      universe.latest_revision ? ` · r${universe.latest_revision.revision}` : " · never resolved"
    }`,
  }));
}

function RefRow({
  refValue,
  index,
  scope,
  onChange,
  onRemove,
  canRemove,
}: Readonly<{
  refValue: UniverseRefDraft;
  index: number;
  scope: string | null;
  onChange: (next: UniverseRefDraft) => void;
  onRemove: () => void;
  canRemove: boolean;
}>) {
  const capabilitiesQuery = useAlertsCapabilities(scope);
  const universesQuery = useAlertsUniverses(scope);

  const kinds = capabilitiesQuery.data?.capabilities.universe_ref_kinds ?? ["universe", "index"];
  const indexLists = capabilitiesQuery.data?.capabilities.universe_index_source_lists ?? [];
  const savedUniverses = universesQuery.data?.universes ?? [];

  // Names are offered from live data, never typed freely: a free-text name is
  // how a typo becomes a silently empty universe (the parser accepts any
  // non-empty name and resolution then finds nothing).
  const options = universeNameOptions(refValue.kind, indexLists, savedUniverses);

  return (
    <div className="flex flex-wrap items-end gap-2">
      <div className="flex flex-col gap-1">
        <Label htmlFor={`universe-kind-${index}`}>Reference kind</Label>
        <Select
          value={refValue.kind}
          onValueChange={(next) => onChange({ kind: next as UniverseRefKind, name: "" })}
        >
          <SelectTrigger id={`universe-kind-${index}`} className="w-[10rem]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {kinds.map((kind) => (
              <SelectItem key={kind} value={kind}>
                {kind}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex min-w-[16rem] flex-1 flex-col gap-1">
        <Label htmlFor={`universe-name-${index}`}>Name</Label>
        {options.length > 0 ? (
          <Select value={refValue.name} onValueChange={(name) => onChange({ ...refValue, name })}>
            <SelectTrigger id={`universe-name-${index}`}>
              <SelectValue placeholder={`Select ${refValue.kind === "index" ? "an index list" : "a saved universe"}`} />
            </SelectTrigger>
            <SelectContent>
              {options.map((option) => (
                <SelectItem key={option.name} value={option.name}>
                  {option.name} <span className="text-muted-foreground">· {option.hint}</span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <Input
            id={`universe-name-${index}`}
            value={refValue.name}
            placeholder={
              refValue.kind === "index"
                ? "no index lists are ingested yet"
                : "no saved universes yet — create one first"
            }
            onChange={(event) => onChange({ ...refValue, name: event.target.value })}
          />
        )}
      </div>

      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-label={`Remove reference ${index + 1}`}
        disabled={!canRemove}
        onClick={onRemove}
      >
        <Trash2Icon className="size-4" aria-hidden />
      </Button>
    </div>
  );
}

export function UniverseTargetingEditor({
  scope,
  value,
  onChange,
}: UniverseTargetingEditorProps) {
  const capabilitiesQuery = useAlertsCapabilities(scope);
  const issues = universeDraftIssues(value);
  const refKinds = capabilitiesQuery.data?.capabilities.universe_ref_kinds ?? ["universe", "index"];

  const updateUnion = (index: number, next: UniverseRefDraft) =>
    onChange({ ...value, union: value.union.map((ref, i) => (i === index ? next : ref)) });
  const updateExclude = (index: number, next: UniverseRefDraft) =>
    onChange({ ...value, exclude: value.exclude.map((ref, i) => (i === index ? next : ref)) });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          Membership is the union of these references, minus the exclusions. A reference kind is{" "}
          {refKinds.join(" / ")}.
        </p>
        <label className="flex items-center gap-3 text-sm">
          <Switch
            checked={value.deduplicate}
            onCheckedChange={(deduplicate) => onChange({ ...value, deduplicate })}
          />
          Deduplicate members
        </label>
      </div>

      <div className="flex flex-col gap-3">
        {value.union.map((refValue, index) => (
          <RefRow
            key={`union-${index}`}
            refValue={refValue}
            index={index}
            scope={scope}
            onChange={(next) => updateUnion(index, next)}
            onRemove={() =>
              onChange({
                ...value,
                union: value.union.filter((_, i) => i !== index),
              })
            }
            canRemove={value.union.length > 1}
          />
        ))}
      </div>

      <Button
        type="button"
        variant="outline"
        size="sm"
        className="self-start"
        disabled={value.union.length >= (capabilitiesQuery.data?.capabilities.limits.max_instruments ?? 1000)}
        onClick={() =>
          onChange({
            ...value,
            union: [...value.union, { kind: value.union[0]?.kind ?? "universe", name: "" }],
          })
        }
      >
        <PlusIcon className="size-4" aria-hidden />
        Add a union reference
      </Button>

      <details className="rounded-lg border border-border/60 p-3">
        <summary className="cursor-pointer text-xs text-muted-foreground">
          Exclusions ({value.exclude.length})
        </summary>
        <div className="mt-3 flex flex-col gap-3">
          {value.exclude.map((refValue, index) => (
            <RefRow
              key={`exclude-${index}`}
              refValue={refValue}
              index={index}
              scope={scope}
              onChange={(next) => updateExclude(index, next)}
              onRemove={() =>
                onChange({ ...value, exclude: value.exclude.filter((_, i) => i !== index) })
              }
              canRemove
            />
          ))}
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="self-start"
            onClick={() =>
              onChange({ ...value, exclude: [...value.exclude, { kind: "universe", name: "" }] })
            }
          >
            <PlusIcon className="size-4" aria-hidden />
            Add an exclusion
          </Button>
        </div>
      </details>

      {issues.length > 0 ? (
        <ul className="flex flex-col gap-1 text-sm text-rose-300">
          {issues.map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      ) : null}

      <p className="text-xs text-muted-foreground">{KIND_HELP[value.union[0]?.kind ?? "universe"]}</p>
    </div>
  );
}

export { emptyUniverseDraft };
