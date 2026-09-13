"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { AlertsScopeOption } from "@/features/alerts/types";

type ScopeSelectProps = Readonly<{
  scopes: AlertsScopeOption[];
  value: string | null;
  onChange: (scope: string) => void;
  disabled?: boolean;
}>;

/**
 * Picker over the scopes the SERVER authorized.
 *
 * The list is never client-derived: an operator whose alerts live under a token
 * scope can select it here, and the server still rejects any scope it did not
 * authorize. Showing which scopes "hold data" prevents the empty-page-is-empty-
 * configuration misreading.
 */
export function ScopeSelect({ scopes, value, onChange, disabled }: ScopeSelectProps) {
  if (scopes.length === 0) {
    return <span className="text-sm text-muted-foreground">No authorized scope</span>;
  }

  return (
    <Select value={value ?? undefined} onValueChange={onChange} disabled={disabled}>
      <SelectTrigger size="sm" aria-label="Alert scope" className="min-w-[14rem]">
        <SelectValue placeholder="Select scope" />
      </SelectTrigger>
      <SelectContent>
        {scopes.map((option) => (
          <SelectItem key={option.scope} value={option.scope}>
            <span className="font-mono text-xs">{option.scope}</span>
            {option.has_data ? null : (
              <span className="text-muted-foreground">· no alerts yet</span>
            )}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
