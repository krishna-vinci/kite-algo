"use client";

import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import type { JournalEnvironment } from "@/lib/journal/types";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type EnvironmentSelectorProps = {
  environments: JournalEnvironment[];
  selectedEnvironmentId?: string;
  onSelectEnvironment: (environmentId: string) => void;
  label?: string;
  loading?: boolean;
  error?: string | null;
  disabled?: boolean;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function envLabel(env: JournalEnvironment): string {
  return env.display_name || `${env.mode} · ${env.account_scope}`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * EnvironmentSelector — controlled shadcn Select over the loaded environments list.
 * Handles loading, error, and empty states inline.
 */
export function EnvironmentSelector({
  environments,
  selectedEnvironmentId = "",
  onSelectEnvironment,
  label = "Environment",
  loading = false,
  error = null,
  disabled = false,
}: EnvironmentSelectorProps) {
  const isDisabled = disabled || loading || !!error;

  if (loading) {
    return (
      <div className="flex flex-col gap-1">
        <span className="text-xs text-muted-foreground">{label}</span>
        <Skeleton className="h-9 w-48 rounded-md" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col gap-1">
        <span className="text-xs text-muted-foreground">{label}</span>
        <span className="text-xs text-destructive">{error}</span>
      </div>
    );
  }

  // Only 1 environment — the mode toggle already distinguishes live/paper, so this dropdown adds no value
  if (environments.length <= 1 && !loading && !error) {
    return null;
  }

  // No environments and not loading — show helpful empty state
  if (environments.length === 0 && !loading && !error) {
    return (
      <div className="flex flex-col gap-1">
        <span className="text-xs text-muted-foreground">{label}</span>
        <div className="flex h-9 items-center rounded-md border border-dashed border-muted-foreground/30 px-3 text-xs text-muted-foreground">
          No trading environments yet. Run the algobot to create one.
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <label
        htmlFor="workspace-environment-select"
        className="text-xs text-muted-foreground"
      >
        {label}
      </label>
      <Select
        value={selectedEnvironmentId}
        onValueChange={onSelectEnvironment}
        disabled={isDisabled}
      >
        <SelectTrigger
          id="workspace-environment-select"
          size="sm"
          className="w-fit min-w-40"
          aria-label={label}
        >
          <SelectValue placeholder="Select environment" />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectLabel>{label}</SelectLabel>
            {environments.map((env) => (
              <SelectItem key={env.id} value={env.id}>
                {envLabel(env)}
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
    </div>
  );
}
