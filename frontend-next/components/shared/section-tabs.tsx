"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";

import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type SectionTab = {
  /** href base path — query params from current URL are preserved and merged */
  href: string;
  label: string;
  /** If true, active detection uses exact match; default is startsWith */
  exact?: boolean;
};

type SectionTabsProps = {
  tabs: SectionTab[];
  className?: string;
  /** Additional query params to preserve / override when navigating */
  preserveParams?: string[];
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * SectionTabs — App Router-aware nav link row that preserves current query
 * params when switching tabs.  Renders a semantic <nav> + <a> row; no
 * Radix Tabs (those require a value + content slot structure).
 *
 * Active detection:
 *   - By default: pathname.startsWith(tab.href)
 *   - With exact=true: pathname === tab.href
 */
export function SectionTabs({
  tabs,
  className,
  preserveParams = [],
}: SectionTabsProps) {
  const pathname = usePathname();
  const searchParams = useSearchParams();

  /**
   * Build href for a tab — keep current search params whose keys are listed
   * in preserveParams, then append them to the tab's href.
   */
  function buildHref(tabHref: string): string {
    if (preserveParams.length === 0) return tabHref;

    const params = new URLSearchParams();
    for (const key of preserveParams) {
      const v = searchParams.get(key);
      if (v !== null) params.set(key, v);
    }

    const qs = params.toString();
    return qs ? `${tabHref}?${qs}` : tabHref;
  }

  function isActive(tab: SectionTab): boolean {
    return tab.exact
      ? pathname === tab.href
      : pathname.startsWith(tab.href);
  }

  return (
    <nav
      className={cn("flex items-end gap-0 border-b border-border", className)}
      aria-label="Section navigation"
    >
      {tabs.map((tab) => {
        const active = isActive(tab);
        return (
          <Link
            key={tab.href}
            href={buildHref(tab.href)}
            aria-current={active ? "page" : undefined}
            className={cn(
              // Base
              "relative inline-flex items-center px-3 pb-2 pt-1.5 text-xs font-medium",
              "transition-colors duration-150 select-none outline-none",
              "focus-visible:rounded-sm focus-visible:ring-2 focus-visible:ring-ring",
              // Inactive
              "text-muted-foreground hover:text-foreground",
              // Active — underline indicator using an absolutely-positioned bar
              active && "text-foreground",
              // Active indicator bar
              "after:absolute after:inset-x-0 after:bottom-0 after:h-0.5",
              active
                ? "after:bg-primary"
                : "after:bg-transparent after:hover:bg-border"
            )}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
