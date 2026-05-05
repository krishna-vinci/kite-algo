"use client";

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { WorkspaceMode } from "@/components/workspace/workspace-provider";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ModeToggleProps = {
  value: WorkspaceMode;
  onValueChange: (value: WorkspaceMode) => void;
  disabled?: boolean;
  className?: string;
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

const MODES: { value: WorkspaceMode; label: string }[] = [
  { value: "live", label: "Live" },
  { value: "paper", label: "Paper" },
];

/**
 * ModeToggle — a controlled two-option toggle for selecting live vs paper mode.
 * Uses shadcn ToggleGroup with outline variant and compact size.
 */
export function ModeToggle({ value, onValueChange, disabled = false, className }: ModeToggleProps) {
  function handleValueChange(next: string) {
    // ToggleGroup can emit empty string when the active item is re-clicked;
    // treat that as a no-op to keep a value always selected.
    if (next === "live" || next === "paper") {
      onValueChange(next);
    }
  }

  return (
    <ToggleGroup
      type="single"
      value={value}
      onValueChange={handleValueChange}
      variant="outline"
      size="sm"
      spacing={0}
      disabled={disabled}
      aria-label="Trading mode"
      className={className}
    >
      {MODES.map((mode) => (
        <ToggleGroupItem
          key={mode.value}
          value={mode.value}
          aria-label={`${mode.label} mode`}
        >
          {mode.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}
