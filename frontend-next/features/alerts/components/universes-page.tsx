"use client";

import Link from "next/link";
import { AlertCircleIcon, RefreshCwIcon } from "lucide-react";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { SectionLabel } from "@/components/operator/section-label";
import {
  useAlertsCapabilities,
  useAlertsUniverseMutations,
  useAlertsUniverses,
  useAlertsWorkflows,
} from "@/features/alerts/hooks/use-alerts-queries";
import { formatTimestamp } from "@/features/alerts/lib/format";

const KIND_HELP: Record<string, string> = {
  explicit: "A hand-picked list of EXCHANGE:SYMBOL keys.",
  index: "The constituents of a supported index list this install can resolve.",
  portfolio: "Owner-scoped holdings. Read-only and provider-driven — no configuration.",
  screener: "The ranked members of a screener workflow, feeding downstream alerts.",
};

export function UniversesPage({ scope }: Readonly<{ scope: string | null }>) {
  const universesQuery = useAlertsUniverses(scope);
  const capabilitiesQuery = useAlertsCapabilities(scope);
  const workflowsQuery = useAlertsWorkflows(scope, false);
  const { create, preview } = useAlertsUniverseMutations(scope);

  const [name, setName] = useState("");
  const [kind, setKind] = useState("explicit");
  const [membersText, setMembersText] = useState("");
  const [indexSourceList, setIndexSourceList] = useState("");
  const [screenerWorkflow, setScreenerWorkflow] = useState("");
  const [screenerTopN, setScreenerTopN] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedConfig, setAdvancedConfig] = useState("{}");
  const [configError, setConfigError] = useState<string | null>(null);

  const capabilities = capabilitiesQuery.data?.capabilities;
  const kinds = capabilities?.universe_source_kinds ?? ["explicit"];
  const indexOptions = capabilities?.universe_index_source_lists ?? [];
  const screenerOptions = (workflowsQuery.data?.workflows ?? []).filter(
    (workflow) => workflow.kind === "screener" && !workflow.archived,
  );
  const effectiveIndex = indexSourceList || indexOptions[0] || "";
  const effectiveScreener = screenerWorkflow || screenerOptions[0]?.name || "";
  const universes = universesQuery.data?.universes ?? [];

  const buildConfig = (): Record<string, unknown> | null => {
    if (advancedOpen) {
      try {
        const parsed = JSON.parse(advancedConfig || "{}");
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error("source_config must be a JSON object");
        }
        setConfigError(null);
        return parsed as Record<string, unknown>;
      } catch (error) {
        setConfigError(error instanceof Error ? error.message : "invalid JSON");
        return null;
      }
    }

    if (kind === "explicit") {
      const members = membersText
        .split(/[\s,]+/)
        .map((item) => item.trim())
        .filter(Boolean);
      if (members.length === 0) {
        setConfigError("Explicit universes need at least one EXCHANGE:SYMBOL member.");
        return null;
      }
      setConfigError(null);
      return { members };
    }
    if (kind === "index") {
      if (!effectiveIndex) {
        setConfigError("No supported index source list is available in this install.");
        return null;
      }
      setConfigError(null);
      return { source_list: effectiveIndex };
    }
    if (kind === "screener") {
      if (!effectiveScreener) {
        setConfigError("Create a screener first; a screener universe references one by name.");
        return null;
      }
      const config: Record<string, unknown> = { workflow: effectiveScreener };
      if (screenerTopN.trim() !== "") config.top_n = Number(screenerTopN);
      setConfigError(null);
      return config;
    }
    // portfolio
    setConfigError(null);
    return {};
  };

  return (
    <div className="flex flex-col gap-6 pb-8">
      <SectionLabel
        eyebrow="Alerts"
        title="Universes"
        description="Saved membership sources. A universe is resolved on demand and each resolution persists a revision, so membership changes are inspectable rather than implicit."
      />

      <section className="flex flex-col gap-3 rounded-xl border border-border/70 bg-card/60 p-5">
        <h3 className="text-sm font-semibold">Create a universe</h3>
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="universe-name">Name</Label>
            <Input id="universe-name" value={name} onChange={(event) => setName(event.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="universe-kind">Kind</Label>
            <Select value={kind} onValueChange={setKind}>
              <SelectTrigger id="universe-kind">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {kinds.map((option) => (
                  <SelectItem key={option} value={option}>
                    {option}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">{KIND_HELP[kind] ?? ""}</p>

        {!advancedOpen ? (
          <>
            {kind === "explicit" ? (
              <div className="flex flex-col gap-1">
                <Label htmlFor="universe-members">Members (EXCHANGE:SYMBOL, one per line)</Label>
                <Textarea
                  id="universe-members"
                  rows={4}
                  className="font-mono text-xs"
                  placeholder={"NSE:RELIANCE\nNSE:INFY"}
                  value={membersText}
                  onChange={(event) => setMembersText(event.target.value)}
                />
              </div>
            ) : null}

            {kind === "index" ? (
              <div className="flex flex-col gap-1 sm:max-w-sm">
                <Label htmlFor="universe-index">Index source list</Label>
                {indexOptions.length > 0 ? (
                  <Select value={effectiveIndex} onValueChange={setIndexSourceList}>
                    <SelectTrigger id="universe-index">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {indexOptions.map((option) => (
                        <SelectItem key={option} value={option}>
                          {option}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <Input
                    id="universe-index"
                    value={indexSourceList}
                    placeholder="Nifty50"
                    onChange={(event) => setIndexSourceList(event.target.value)}
                  />
                )}
                <p className="text-xs text-muted-foreground">
                  Only lists this install can resolve are offered; a value it cannot resolve is
                  rejected at validation.
                </p>
              </div>
            ) : null}

            {kind === "portfolio" ? (
              <p className="text-sm text-muted-foreground">
                Portfolio membership is derived from the authorized holdings and is read-only. There
                is nothing to configure — resolve it to snapshot current members.
              </p>
            ) : null}

            {kind === "screener" ? (
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="flex flex-col gap-1">
                  <Label htmlFor="universe-screener">Screener workflow</Label>
                  {screenerOptions.length > 0 ? (
                    <Select value={effectiveScreener} onValueChange={setScreenerWorkflow}>
                      <SelectTrigger id="universe-screener">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {screenerOptions.map((workflow) => (
                          <SelectItem key={workflow.workflow_id} value={workflow.name}>
                            {workflow.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : (
                    <Input
                      id="universe-screener"
                      value={screenerWorkflow}
                      placeholder="screener-name"
                      onChange={(event) => setScreenerWorkflow(event.target.value)}
                    />
                  )}
                </div>
                <div className="flex flex-col gap-1">
                  <Label htmlFor="universe-screener-topn">Top N (optional)</Label>
                  <Input
                    id="universe-screener-topn"
                    type="number"
                    min={1}
                    max={1000}
                    value={screenerTopN}
                    onChange={(event) => setScreenerTopN(event.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">
                    Leave blank to use the screener&apos;s own top_n.
                  </p>
                </div>
              </div>
            ) : null}
          </>
        ) : (
          <div className="flex flex-col gap-1">
            <Label htmlFor="universe-config">source_config (advanced JSON)</Label>
            <Textarea
              id="universe-config"
              rows={3}
              className="font-mono text-xs"
              value={advancedConfig}
              onChange={(event) => setAdvancedConfig(event.target.value)}
            />
          </div>
        )}

        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={advancedOpen}
            onChange={(event) => setAdvancedOpen(event.target.checked)}
          />
          Advanced: edit raw source_config JSON instead
        </label>

        {configError ? <p className="text-xs text-rose-300">{configError}</p> : null}

        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={preview.isPending}
            onClick={() => {
              const config = buildConfig();
              if (config) preview.mutate({ kind, source_config: config });
            }}
          >
            {preview.isPending ? "Resolving…" : "Preview members"}
          </Button>
          <Button
            disabled={!name || create.isPending}
            onClick={() => {
              const config = buildConfig();
              if (config) create.mutate({ name, kind, source_config: config });
            }}
          >
            Create
          </Button>
        </div>

        {preview.data ? (
          <div className="rounded-lg border border-border/60 p-3 text-sm">
            {/* Preview is explicitly non-persisting; saying so prevents an
                operator thinking membership has been saved. */}
            <p>
              {preview.data.members.length} member(s), {preview.data.rejected.length} rejected — not
              saved.
            </p>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              {preview.data.members.slice(0, 12).join(", ")}
              {preview.data.members.length > 12 ? ` +${preview.data.members.length - 12} more` : ""}
            </p>
          </div>
        ) : null}

        {create.error ? (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertTitle>Could not create</AlertTitle>
            <AlertDescription>
              {create.error instanceof Error ? create.error.message : "Unknown error"}
            </AlertDescription>
          </Alert>
        ) : null}
      </section>

      {universesQuery.isLoading ? (
        <Skeleton className="h-40 w-full rounded-xl" />
      ) : universes.length === 0 ? (
        <p className="text-sm text-muted-foreground">No universes in this scope yet.</p>
      ) : (
        <div className="rounded-xl border border-border/70 bg-card/60">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Latest revision</TableHead>
                <TableHead>Members</TableHead>
                <TableHead>Resolved</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {universes.map((universe) => (
                <TableRow key={universe.universe_id}>
                  <TableCell>
                    <Link
                      href={`/alerts/universes/${encodeURIComponent(universe.name)}`}
                      className="font-medium hover:underline"
                    >
                      {universe.name}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline">{universe.kind}</Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {/* A universe with no revision has never been resolved —
                        distinct from one resolved to zero members. */}
                    {universe.latest_revision ? `r${universe.latest_revision.revision}` : "never resolved"}
                  </TableCell>
                  <TableCell className="text-sm">
                    {universe.latest_revision ? universe.latest_revision.member_count : "—"}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatTimestamp(universe.latest_revision?.resolved_at ?? null) ?? "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <p className="flex items-center gap-2 text-xs text-muted-foreground">
        <RefreshCwIcon className="size-3" aria-hidden />
        Resolution consults the live catalog and writes a revision — it is deliberately not
        automatic.
      </p>
    </div>
  );
}
