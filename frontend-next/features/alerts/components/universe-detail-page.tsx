"use client";

import Link from "next/link";
import { AlertCircleIcon, ArrowLeftIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  useAlertsUniverse,
  useAlertsUniverseMutations,
  useAlertsUniverseRevisions,
} from "@/features/alerts/hooks/use-alerts-queries";
import { alertsErrorMessage, isNotFound } from "@/features/alerts/lib/errors";
import { formatTimestamp, summarizeList } from "@/features/alerts/lib/format";

/** Coverage is a small map; render it readably instead of dumping JSON. */
function summarizeCoverage(coverage: Record<string, unknown>): string | null {
  const entries = Object.entries(coverage).filter(([, value]) => value !== null && value !== undefined);
  if (entries.length === 0) return null;
  return entries.map(([key, value]) => `${key}: ${String(value)}`).join(" · ");
}

export function UniverseDetailPage({
  name,
  scope,
}: Readonly<{ name: string; scope: string | null }>) {
  const universeQuery = useAlertsUniverse(name, scope);
  const revisionsQuery = useAlertsUniverseRevisions(name, scope);
  const { resolve } = useAlertsUniverseMutations(scope);

  if (universeQuery.isLoading) return <Skeleton className="h-96 w-full rounded-xl" />;

  if (universeQuery.error || !universeQuery.data) {
    const notFound = !universeQuery.error || isNotFound(universeQuery.error);
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>{notFound ? "Universe not found" : "Could not load this universe"}</AlertTitle>
        <AlertDescription>
          {notFound
            ? "This universe does not exist in the selected scope."
            : alertsErrorMessage(universeQuery.error, "The request failed.")}
        </AlertDescription>
      </Alert>
    );
  }

  const universe = universeQuery.data;
  const revisions = revisionsQuery.data?.revisions ?? [];
  const members = universe.latest_members ?? [];

  return (
    <div className="flex flex-col gap-6 pb-8">
      <div>
        <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
          <Link href="/alerts/universes">
            <ArrowLeftIcon className="size-4" aria-hidden />
            All universes
          </Link>
        </Button>
        <SectionLabel eyebrow="Universe" title={universe.name} />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Badge variant="outline">{universe.kind}</Badge>
          <Badge variant={universe.enabled ? "secondary" : "destructive"}>
            {universe.enabled ? "enabled" : "disabled"}
          </Badge>
          <span className="text-xs text-muted-foreground">
            {universe.latest_revision
              ? `latest revision r${universe.latest_revision.revision} · ${universe.latest_revision.member_count} members`
              : "never resolved"}
          </span>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          size="sm"
          disabled={resolve.isPending}
          onClick={() => resolve.mutate(name)}
        >
          {resolve.isPending ? "Resolving…" : "Resolve now"}
        </Button>
        <span className="text-xs text-muted-foreground">
          Resolution consults the live catalog and persists a new revision.
        </span>
        {resolve.error ? (
          <span className="text-xs text-rose-300">
            {alertsErrorMessage(resolve.error, "Resolution failed")}
          </span>
        ) : null}
      </div>

      <Panel tone="subtle">
        <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
          Current membership
        </p>
        {members.length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">
            {universe.latest_revision
              ? "The latest revision resolved to zero members."
              : "This universe has never been resolved, so it has no membership yet."}
          </p>
        ) : (
          <p className="mt-2 break-words font-mono text-xs">{members.join(", ")}</p>
        )}
        {summarizeCoverage(universe.latest_coverage ?? {}) ? (
          <p className="mt-3 text-xs text-muted-foreground">
            coverage: {summarizeCoverage(universe.latest_coverage ?? {})}
          </p>
        ) : null}
      </Panel>

      <div className="flex flex-col gap-2">
        <h3 className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
          Revision history
        </h3>
        {revisionsQuery.isLoading ? (
          <Skeleton className="h-32 w-full rounded-xl" />
        ) : revisionsQuery.error ? (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertTitle>Could not load revision history</AlertTitle>
            <AlertDescription>
              {alertsErrorMessage(revisionsQuery.error, "The revisions request failed.")}
            </AlertDescription>
          </Alert>
        ) : revisions.length === 0 ? (
          <p className="text-sm text-muted-foreground">No revisions yet.</p>
        ) : (
          <div className="rounded-xl border border-border/70 bg-card/60">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Revision</TableHead>
                  <TableHead>Members</TableHead>
                  <TableHead>Source generation</TableHead>
                  <TableHead>Resolved</TableHead>
                  <TableHead>Sample</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {revisions.map((revision) => (
                  <TableRow key={revision.revision_id}>
                    <TableCell className="font-mono text-sm">r{revision.revision}</TableCell>
                    <TableCell className="text-sm">{revision.member_count}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {revision.source_generation ?? "—"}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatTimestamp(revision.resolved_at ?? revision.created_at) ?? "—"}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {summarizeList(revision.members, 4)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>
    </div>
  );
}
