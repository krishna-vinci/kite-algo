"use client";

import { useSearchParams } from "next/navigation";

/**
 * /analytics/strategies — landing page shown when the Strategy tab is clicked
 * but no specific template has been selected yet.
 *
 * Users arrive here from the Strategy tab in the analytics shell; individual
 * strategy rows on the dashboard link to /analytics/strategies/[templateId].
 */
export default function StrategiesIndexPage() {
  const searchParams = useSearchParams();
  const hasEnv = !!searchParams.get("env");

  if (!hasEnv) {
    return (
      <div className="rounded-xl border border-border/60 bg-muted/20 px-4 py-8 text-center text-sm text-muted-foreground">
        Select an environment to view strategy analytics.
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-border/60 bg-muted/20 px-4 py-8 text-center text-sm text-muted-foreground">
      Select a strategy from the{" "}
      <a
        href={`/analytics?${searchParams.toString()}`}
        className="underline underline-offset-2 hover:text-foreground"
      >
        Dashboard
      </a>{" "}
      to view its deep-dive analytics.
    </div>
  );
}
