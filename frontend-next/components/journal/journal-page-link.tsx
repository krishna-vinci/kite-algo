"use client";

import Link from "next/link";

import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";

type JournalPageLinkProps = {
  href: string;
  children: React.ReactNode;
  className?: string;
  "aria-current"?: "page";
};

function appendEnvironmentId(href: string, environmentId: string) {
  if (!environmentId) {
    return href;
  }
  return `${href}${href.includes("?") ? "&" : "?"}environment_id=${encodeURIComponent(environmentId)}`;
}

export function JournalPageLink({ href, children, className, "aria-current": ariaCurrent }: JournalPageLinkProps) {
  let selectedEnvironmentId = "";
  try {
    selectedEnvironmentId = useJournalWorkspace().selectedEnvironmentId;
  } catch {
    selectedEnvironmentId =
      typeof window !== "undefined" ? (new URLSearchParams(window.location.search).get("environment_id") ?? "") : "";
  }

  const nextHref = appendEnvironmentId(href, selectedEnvironmentId);

  return (
    <Link href={nextHref} className={className} aria-current={ariaCurrent}>
      {children}
    </Link>
  );
}
