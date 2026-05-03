"use client";

import type { JournalEnvironment } from "@/lib/journal/types";

type EnvironmentSelectorProps = {
  environments: JournalEnvironment[];
  selectedEnvironmentId?: string;
  onSelectEnvironment: (environmentId: string) => void;
  label?: string;
  loading?: boolean;
  error?: string | null;
  disabled?: boolean;
};

export function EnvironmentSelector({
  environments,
  selectedEnvironmentId,
  onSelectEnvironment,
  label = "Environment",
  loading = false,
  error = null,
  disabled = false,
}: EnvironmentSelectorProps) {
  const selected = environments.find((item) => item.id === selectedEnvironmentId) ?? null;
  const selectorDisabled = disabled || loading;

  const statusText = loading
    ? "Loading environments…"
    : error
      ? error
      : selected
        ? `${selected.mode} / ${selected.account_scope}`
        : "Not selected";

  return (
    <section className="rounded-xl border border-border/60 bg-background/60 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="journal-v2-environment" className="text-xs uppercase tracking-[0.18em] text-foreground/60">
          {label}
        </label>
        <select
          id="journal-v2-environment"
          aria-label="Journal V2 environment"
          value={selectedEnvironmentId ?? ""}
          onChange={(event) => onSelectEnvironment(event.target.value)}
          disabled={selectorDisabled}
          className="rounded-md border border-border/70 bg-background px-2 py-1 text-sm"
        >
          <option value="">Select environment</option>
          {environments.map((environment) => (
            <option key={environment.id} value={environment.id}>
              {environment.display_name || `${environment.mode} · ${environment.account_scope}`}
            </option>
          ))}
        </select>
      </div>
      <p className="mt-2 text-sm text-foreground/80">
        <span className="font-medium">Selected mode/account:</span>{" "}
        {statusText}
      </p>
    </section>
  );
}
