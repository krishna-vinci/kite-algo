"use client";

import { useCallback, useMemo, useState } from "react";
import type { StrategyRiskField } from "@/features/trading/types";
import { updatePaperStrategyRisk } from "@/features/trading/api";

type RiskAdjustmentSheetProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  strategyId: string;
  displayName: string;
  riskSchema: StrategyRiskField[];
};

export function RiskAdjustmentSheet({
  open,
  onOpenChange,
  strategyId,
  displayName,
  riskSchema,
}: RiskAdjustmentSheetProps) {
  const editableFields = useMemo(() => riskSchema.filter((field) => field.key && field.type !== "boolean"), [riskSchema]);
  const [values, setValues] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const field of editableFields) {
      init[field.key] = field.value == null ? "" : String(field.value);
    }
    return init;
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      const payload: Record<string, number | null> = {};
      for (const field of editableFields) {
        const raw = values[field.key]?.trim();
        payload[field.key] = raw ? Number(raw) : null;
      }
      await updatePaperStrategyRisk(strategyId, payload);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update risk");
    } finally {
      setSaving(false);
    }
  }, [editableFields, strategyId, values, onOpenChange]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center" data-testid="risk-sheet">
      <div className="fixed inset-0 bg-black/40" onClick={() => onOpenChange(false)} />
      <div className="relative z-10 w-full max-w-md rounded-t-2xl border border-border/70 bg-card p-5 shadow-xl sm:rounded-2xl">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">risk controls</p>
            <h3 className="mt-1 text-sm font-semibold">{displayName}</h3>
          </div>
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-md px-2 py-1 text-xs text-foreground/50 hover:text-foreground/80"
          >
            Close
          </button>
        </div>

        <div className="space-y-3">
          {editableFields.length === 0 ? (
            <p className="text-xs text-foreground/50">No editable risk fields are available for this run.</p>
          ) : null}
          {editableFields.map((field) => (
            <div key={field.key} className="flex items-center gap-3">
              <label className="w-40 text-xs text-foreground/60">
                {field.label}
                {field.unit ? <span className="ml-1 text-foreground/40">{field.unit}</span> : null}
              </label>
              <input
                type={field.type === "number" ? "number" : "text"}
                value={values[field.key] ?? ""}
                onChange={(e) => setValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
                className="flex-1 rounded-md border border-border/60 bg-background/60 px-2 py-1.5 font-mono text-sm text-foreground outline-none focus:border-primary/50"
                placeholder="—"
              />
            </div>
          ))}
        </div>

        {error && <p className="mt-3 text-xs text-rose-400">{error}</p>}

        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-md px-3 py-1.5 text-xs text-foreground/60 hover:text-foreground/80"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving || editableFields.length === 0}
            className="rounded-md bg-primary/90 px-4 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
