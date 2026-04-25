"use client";

import { cn } from "@/lib/utils";

type ModeSafetyBannerProps = {
  mode: "preview" | "dry_run" | "paper" | "live";
  title: string;
  description: string;
  compact?: boolean;
};

const toneMap: Record<ModeSafetyBannerProps["mode"], string> = {
  preview: "border-slate-500/40 bg-slate-500/10 text-slate-200",
  dry_run: "border-amber-500/40 bg-amber-500/10 text-amber-200",
  paper: "border-sky-500/40 bg-sky-500/10 text-sky-200",
  live: "border-rose-500/40 bg-rose-500/10 text-rose-200",
};

export function ModeSafetyBanner({ mode, title, description, compact = false }: ModeSafetyBannerProps) {
  return (
    <div className={cn("rounded-lg border px-3 py-2", toneMap[mode])}>
      <div className="flex items-center gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-[0.2em]">{mode.replace("_", " ")}</span>
        <span className={cn("font-semibold", compact ? "text-xs" : "text-sm")}>{title}</span>
      </div>
      <p className={cn("mt-1", compact ? "text-[11px]" : "text-xs")}>{description}</p>
    </div>
  );
}
