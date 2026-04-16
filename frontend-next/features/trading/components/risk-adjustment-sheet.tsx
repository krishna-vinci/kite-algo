"use client";

import { useCallback, useState } from "react";
import type { StrategyRiskControls } from "@/features/trading/types";
import { updatePaperStrategyRisk } from "@/features/trading/api";

type RiskAdjustmentSheetProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  strategyId: string;
  displayName: string;
  riskControls: StrategyRiskControls;
};

type FieldDef = {
  key: keyof StrategyRiskControls;
  apiKey: string;
  label: string;
};

const FIELDS: FieldDef[] = [
  { key: "combinedPremiumTarget", apiKey: "combined_premium_target", label: "Premium target" },
  { key: "combinedPremiumStoploss", apiKey: "combined_premium_stoploss", label: "Premium stoploss" },
  { key: "basketMtmTarget", apiKey: "basket_mtm_target", label: "Basket MTM target" },
  { key: "basketMtmStoploss", apiKey: "basket_mtm_stoploss", label: "Basket MTM stoploss" },
  { key: "indexLowerBoundary", apiKey: "index_lower_boundary", label: "Index lower boundary" },
  { key: "indexUpperBoundary", apiKey: "index_upper_boundary", label: "Index upper boundary" },
];

export function RiskAdjustmentSheet({
  open,
  onOpenChange,
  strategyId,
  displayName,
  riskControls,
}: RiskAdjustmentSheetProps) {
  const [values, setValues] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const f of FIELDS) {
      init[f.key] = riskControls[f.key]?.toString() ?? "";
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
      for (const f of FIELDS) {
        const raw = values[f.key]?.trim();
        payload[f.apiKey] = raw ? Number(raw) : null;
      }
      await updatePaperStrategyRisk(strategyId, payload);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update risk");
    } finally {
      setSaving(false);
    }
  }, [strategyId, values, onOpenChange]);

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
          {FIELDS.map((f) => (
            <div key={f.key} className="flex items-center gap-3">
              <label className="w-40 text-xs text-foreground/60">{f.label}</label>
              <input
                type="number"
                value={values[f.key] ?? ""}
                onChange={(e) => setValues((prev) => ({ ...prev, [f.key]: e.target.value }))}
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
            disabled={saving}
            className="rounded-md bg-primary/90 px-4 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
