"use client";

/**
 * The owner's capital/risk limits, shared by the composer and the authorization
 * panel so both describe the same fields with the same words.
 *
 * Nothing here has a default: the platform never invents a user's capital or
 * loss tolerance, and a grant bound to unset limits would claim an enforcement
 * that does not exist.
 */

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export type LimitDraft = Record<
  | "allocation_inr"
  | "per_instrument_notional_inr"
  | "gross_notional_inr"
  | "max_open_instruments"
  | "admissions_per_window"
  | "admission_window_seconds"
  | "daily_loss_budget_inr",
  string
>;

export const EMPTY_LIMITS: LimitDraft = {
  allocation_inr: "",
  per_instrument_notional_inr: "",
  gross_notional_inr: "",
  max_open_instruments: "",
  admissions_per_window: "",
  admission_window_seconds: "",
  daily_loss_budget_inr: "",
};

export const LIMIT_FIELDS: Array<{ key: keyof LimitDraft; label: string; help: string }> = [
  {
    key: "allocation_inr",
    label: "Allocation (INR)",
    help: "Total capital this strategy may work with.",
  },
  {
    key: "per_instrument_notional_inr",
    label: "Per-instrument notional (INR)",
    help: "Largest position value allowed in one instrument.",
  },
  {
    key: "gross_notional_inr",
    label: "Gross notional (INR)",
    help: "Sum of all open position values.",
  },
  {
    key: "max_open_instruments",
    label: "Max open instruments",
    help: "How many instruments may be open at once.",
  },
  {
    key: "admissions_per_window",
    label: "Admissions per window",
    help: "How many new positions may be admitted inside the window.",
  },
  {
    key: "admission_window_seconds",
    label: "Admission window (seconds)",
    help: "The window the admission count applies to.",
  },
  {
    key: "daily_loss_budget_inr",
    label: "Daily loss budget (INR)",
    help: "Loss after which the platform stops admitting new exposure.",
  },
];

/** Only the fields the owner actually filled in are sent. */
export function limitsPayload(limits: LimitDraft): Record<string, number> {
  const payload: Record<string, number> = {};
  for (const field of LIMIT_FIELDS) {
    const raw = limits[field.key].trim();
    if (raw === "") continue;
    const parsed = Number(raw);
    if (Number.isFinite(parsed)) payload[field.key] = parsed;
  }
  return payload;
}

/** The recorded policy, back into the draft shape the form edits. */
export function limitsDraftFromPolicy(policy: Record<string, unknown> | null): LimitDraft {
  const draft: LimitDraft = { ...EMPTY_LIMITS };
  if (!policy) return draft;
  for (const field of LIMIT_FIELDS) {
    const value = policy[field.key];
    if (typeof value === "number" && Number.isFinite(value)) draft[field.key] = String(value);
  }
  return draft;
}

export function LimitsFields({
  limits,
  onChange,
  idPrefix = "limit",
}: Readonly<{
  limits: LimitDraft;
  onChange: (limits: LimitDraft) => void;
  idPrefix?: string;
}>) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {LIMIT_FIELDS.map((field) => (
        <div key={field.key} className="grid gap-1.5">
          <Label htmlFor={`${idPrefix}-${field.key}`}>{field.label}</Label>
          <Input
            id={`${idPrefix}-${field.key}`}
            inputMode="decimal"
            value={limits[field.key]}
            onChange={(event) => onChange({ ...limits, [field.key]: event.target.value })}
            placeholder="Not set"
          />
          <p className="text-xs text-muted-foreground">{field.help}</p>
        </div>
      ))}
    </div>
  );
}
