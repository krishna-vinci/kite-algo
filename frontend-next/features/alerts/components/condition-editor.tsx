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
import {
  operatorGroup,
  operatorLabel,
  type Condition,
  type Operand,
} from "@/features/alerts/lib/authoring";
import type { AlertsCapabilities } from "@/features/alerts/types";

type OperandEditorProps = Readonly<{
  value: Operand;
  onChange: (next: Operand) => void;
  capabilities: AlertsCapabilities;
  label: string;
}>;

function OperandEditor({ value, onChange, capabilities, label }: OperandEditorProps) {
  const indicatorOptions = Object.keys(capabilities.features).sort();
  const indicatorBounds =
    value.kind === "indicator" ? capabilities.features[value.name]?.params?.period : undefined;

  return (
    <div className="flex items-start gap-2">
      <Select
        value={value.kind}
        onValueChange={(kind) => {
          if (kind === "constant") onChange({ kind: "constant", value: 0 });
          else if (kind === "field") onChange({ kind: "field", name: capabilities.fields[0] ?? "close" });
          else onChange({ kind: "indicator", name: indicatorOptions[0] ?? "rsi" });
        }}
      >
        <SelectTrigger size="sm" aria-label={`${label} kind`} className="w-[9rem]">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="constant">value</SelectItem>
          <SelectItem value="field">bar field</SelectItem>
          <SelectItem value="indicator">indicator</SelectItem>
        </SelectContent>
      </Select>

      {value.kind === "constant" ? (
        <Input
          type="number"
          step="any"
          aria-label={`${label} value`}
          className="w-[9rem]"
          value={value.value}
          onChange={(event) => onChange({ kind: "constant", value: Number(event.target.value) })}
        />
      ) : null}

      {value.kind === "field" ? (
        <Select value={value.name} onValueChange={(name) => onChange({ kind: "field", name })}>
          <SelectTrigger size="sm" aria-label={`${label} field`} className="w-[11rem]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {capabilities.fields.map((field) => (
              <SelectItem key={field} value={field}>
                {field}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : null}

      {value.kind === "indicator" ? (
        <>
          <Select
            value={value.name}
            onValueChange={(name) => onChange({ kind: "indicator", name })}
          >
            <SelectTrigger size="sm" aria-label={`${label} indicator`} className="w-[9rem]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {indicatorOptions.map((indicator) => (
                <SelectItem key={indicator} value={indicator}>
                  {indicator}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {indicatorBounds ? (
            <div>
              <Input
                type="number"
                aria-label={`${label} indicator period`}
                className="w-[7rem]"
                min={indicatorBounds.min}
                max={indicatorBounds.max}
                value={value.period ?? capabilities.features[value.name]?.defaults?.period ?? indicatorBounds.min}
                onChange={(event) =>
                  onChange({ kind: "indicator", name: value.name, period: Number(event.target.value) })
                }
              />
              {/* Bounds come from the registry, never invented here. */}
              <p className="mt-1 text-[10px] text-muted-foreground">
                {indicatorBounds.min}–{indicatorBounds.max}
              </p>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

type ConditionEditorProps = Readonly<{
  conditions: Condition[];
  onChange: (next: Condition[]) => void;
  capabilities: AlertsCapabilities;
}>;

/**
 * Flat `all` group editor.
 *
 * The operator list is grouped by the backend's own classification, and each
 * option is labelled with whether it reports a *level* or a *crossing* — the
 * distinction that caused a live Phase 4 failure (handoff §7).
 */
export function ConditionEditor({ conditions, onChange, capabilities }: ConditionEditorProps) {
  const operatorEntries = Object.entries(capabilities.operators);

  const updateAt = (index: number, next: Condition) => {
    const copy = [...conditions];
    copy[index] = next;
    onChange(copy);
  };

  return (
    <div className="flex flex-col gap-3">
      {conditions.map((condition, index) => {
        const group = operatorGroup(condition.op, capabilities.operators);
        return (
          <div
            key={index}
            className="flex flex-col gap-2 rounded-lg border border-border/60 bg-background/40 p-3"
          >
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex flex-col gap-1">
                <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground/60">
                  Left
                </Label>
                <OperandEditor
                  label={`condition ${index + 1} left`}
                  value={condition.left}
                  capabilities={capabilities}
                  onChange={(left) => updateAt(index, { ...condition, left })}
                />
              </div>

              <div className="flex flex-col gap-1">
                <Label
                  htmlFor={`condition-op-${index}`}
                  className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground/60"
                >
                  Operator
                </Label>
                <Select
                  value={condition.op}
                  onValueChange={(op) => updateAt(index, { ...condition, op })}
                >
                  <SelectTrigger id={`condition-op-${index}`} size="sm" className="w-[16rem]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {operatorEntries.map(([op, kind]) => (
                      <SelectItem key={op} value={op}>
                        {operatorLabel(op)}
                        {kind ? <span className="text-muted-foreground"> · {kind}</span> : null}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="flex flex-col gap-1">
                <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground/60">
                  Right
                </Label>
                <OperandEditor
                  label={`condition ${index + 1} right`}
                  value={condition.right}
                  capabilities={capabilities}
                  onChange={(right) => updateAt(index, { ...condition, right })}
                />
              </div>

              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={`Remove condition ${index + 1}`}
                disabled={conditions.length === 1}
                onClick={() => onChange(conditions.filter((_, i) => i !== index))}
              >
                <Trash2Icon className="size-4" />
              </Button>
            </div>

            {group === "level" ? (
              <p className="text-xs text-muted-foreground">
                This reports whether the condition is currently true — it does not report that
                it <em>became</em> true.
              </p>
            ) : null}
          </div>
        );
      })}

      <Button
        type="button"
        variant="outline"
        size="sm"
        className="self-start"
        disabled={conditions.length >= capabilities.limits.max_conditions_per_group}
        onClick={() =>
          onChange([
            ...conditions,
            { left: { kind: "field", name: "close" }, op: "crosses_above", right: { kind: "constant", value: 0 } },
          ])
        }
      >
        <PlusIcon className="size-4" />
        Add condition
      </Button>
      {conditions.length >= capabilities.limits.max_conditions_per_group ? (
        <p className="text-xs text-muted-foreground">
          Limit reached: {capabilities.limits.max_conditions_per_group} conditions per group.
        </p>
      ) : null}
    </div>
  );
}
