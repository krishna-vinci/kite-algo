"use client";

/**
 * The one header pattern for the alerts area: a working way back, the trail
 * naming where you are, and a right-hand slot for page-specific controls.
 * `onBack` lets a page route the chevron through its dirty guard instead of
 * navigating directly.
 */

import Link from "next/link";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import type { ReactNode } from "react";

export type BreadcrumbItem = { label: string; href?: string };

export function AlertsPageHeader({
  trail,
  backHref,
  right,
  onBack,
}: {
  trail: BreadcrumbItem[];
  backHref: string;
  right?: ReactNode;
  onBack?: (href: string) => void;
}) {
  const backContent = (
    <>
      <ChevronLeftIcon className="size-4" aria-hidden />
      {trail[0]?.label ?? "Back"}
    </>
  );
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 flex-wrap items-center gap-1 text-sm">
        {onBack ? (
          <button
            type="button"
            onClick={() => onBack(backHref)}
            className="flex shrink-0 items-center gap-1 text-muted-foreground hover:text-foreground"
          >
            {backContent}
          </button>
        ) : (
          <Link
            href={backHref}
            className="flex shrink-0 items-center gap-1 text-muted-foreground hover:text-foreground"
          >
            {backContent}
          </Link>
        )}
        <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1">
          {trail.map((item, index) => (
            <span key={`${item.label}-${index}`} className="flex min-w-0 items-center gap-1">
              <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground/50" aria-hidden />
              {item.href && index < trail.length - 1 ? (
                <Link
                  href={item.href}
                  className="truncate text-muted-foreground hover:text-foreground"
                >
                  {item.label}
                </Link>
              ) : (
                <span
                  aria-current={index === trail.length - 1 ? "page" : undefined}
                  className="truncate font-medium"
                >
                  {item.label}
                </span>
              )}
            </span>
          ))}
        </nav>
      </div>
      {right ? <div className="flex shrink-0 items-center gap-2">{right}</div> : null}
    </div>
  );
}
