"use client";

import { Database, Globe, Layers, Monitor } from "lucide-react";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import { useOptionalWorkspace } from "@/components/workspace/workspace-provider";
import type { JournalEnvironment } from "@/lib/journal/types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function modeLabel(mode: string): string {
  if (mode === "live") return "Live";
  if (mode === "dry_run_preview") return "Dry-run preview";
  return "Paper";
}

function modeTone(mode: string): "positive" | "warning" | "neutral" {
  if (mode === "live") return "warning";
  if (mode === "dry_run_preview") return "neutral";
  return "positive";
}

function envDisplayName(env: JournalEnvironment): string {
  if (env.display_name) return env.display_name;
  return `${modeLabel(env.mode)} · ${env.account_scope}`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function InfoSlot({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="rounded-xl border border-border/60 bg-background/60 p-2.5">
      <p className="text-[9px] uppercase tracking-[0.35em] text-foreground/40">{label}</p>
      <p className={`mt-1 text-xs text-primary truncate ${mono ? "font-mono" : ""}`}>{value}</p>
    </div>
  );
}

function EnvironmentRow({ env, isActive }: { env: JournalEnvironment; isActive: boolean }) {
  return (
    <div
      className={[
        "flex items-start justify-between gap-3 rounded-xl border px-3 py-2.5 text-xs",
        isActive
          ? "border-primary/30 bg-primary/8 text-foreground"
          : "border-border/60 bg-background/40 text-foreground/70",
      ].join(" ")}
    >
      <div className="min-w-0 space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-medium text-foreground/90">{envDisplayName(env)}</span>
          <StatusBadge tone={modeTone(env.mode)}>{modeLabel(env.mode)}</StatusBadge>
          {isActive && (
            <span className="rounded-lg border border-primary/30 px-1.5 py-0.5 text-[9px] uppercase tracking-[0.22em] text-primary/80">
              active
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[10px] text-foreground/45">
          {env.account_scope && (
            <span className="font-mono">{env.account_scope}</span>
          )}
          {env.broker_user_id && (
            <span>Broker: <span className="font-mono text-foreground/60">{env.broker_user_id}</span></span>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main export
// ---------------------------------------------------------------------------

/**
 * WorkspaceContextPanel
 *
 * Read-only display of the current workspace / environment context.
 * Uses useOptionalWorkspace so it degrades gracefully when rendered outside
 * the WorkspaceProvider (e.g. in tests without the full provider tree).
 *
 * No backend mutations. No new APIs invented.
 */
export function WorkspaceContextPanel() {
  const workspace = useOptionalWorkspace();

  const loading = workspace?.environmentsLoading ?? false;
  const error = workspace?.environmentsError ?? null;
  const environments = workspace?.environments ?? [];
  const selectedMode = workspace?.selectedMode ?? "paper";
  const selectedEnvironment = workspace?.selectedEnvironment ?? null;

  const liveEnvs = environments.filter((e) => e.mode === "live");
  const paperEnvs = environments.filter((e) => e.mode !== "live");

  const activeTone = selectedMode === "live" ? "warning" : "positive";

  return (
    <Panel
      id="workspace-context"
      eyebrow="environment"
      title="Workspace context"
      action={
        <StatusBadge tone={loading ? "neutral" : activeTone}>
          {loading ? "loading" : selectedMode}
        </StatusBadge>
      }
    >
      <p className="mb-4 max-w-3xl text-xs leading-5 text-foreground/50">
        Read-only view of the active trading environment context. Workspace state is shared across the
        operator panel, journal, and strategy surfaces. Switch the mode toggle in the header to change
        context. Environment configuration is managed by the algo backend — no manual edits are needed here.
      </p>

      {error ? (
        <div className="rounded-xl border border-rose-400/20 bg-rose-400/5 px-3 py-2 text-xs text-rose-300">
          Could not load environment context: {error}
        </div>
      ) : loading ? (
        <div className="grid gap-2 sm:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-16 animate-pulse rounded-xl border border-border/40 bg-background/40" />
          ))}
        </div>
      ) : environments.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border/60 bg-background/35 p-4 text-sm text-foreground/50">
          No trading environments detected. Start the algo backend to create environments automatically.
        </div>
      ) : (
        <div className="space-y-4">
          {/* Current context summary */}
          <div className="grid gap-2 sm:grid-cols-3">
            <InfoSlot label="Active mode" value={modeLabel(selectedMode)} />
            <InfoSlot
              label="Active environment"
              value={selectedEnvironment ? envDisplayName(selectedEnvironment) : "None selected"}
              mono={!selectedEnvironment?.display_name}
            />
            <InfoSlot
              label="Account scope"
              value={selectedEnvironment?.account_scope ?? "—"}
              mono
            />
          </div>

          {/* Environment list */}
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-[9px] uppercase tracking-[0.3em] text-foreground/35">
              <Layers aria-hidden size={11} />
              <span>All environments ({environments.length})</span>
            </div>
            {liveEnvs.length > 0 && (
              <div className="space-y-1.5">
                <div className="flex items-center gap-1.5 text-[9px] uppercase tracking-[0.25em] text-foreground/30">
                  <Globe aria-hidden size={10} />
                  <span>Live</span>
                </div>
                {liveEnvs.map((env) => (
                  <EnvironmentRow
                    key={env.id}
                    env={env}
                    isActive={env.id === selectedEnvironment?.id}
                  />
                ))}
              </div>
            )}
            {paperEnvs.length > 0 && (
              <div className="space-y-1.5">
                <div className="flex items-center gap-1.5 text-[9px] uppercase tracking-[0.25em] text-foreground/30">
                  <Monitor aria-hidden size={10} />
                  <span>Paper / simulation</span>
                </div>
                {paperEnvs.map((env) => (
                  <EnvironmentRow
                    key={env.id}
                    env={env}
                    isActive={env.id === selectedEnvironment?.id}
                  />
                ))}
              </div>
            )}
          </div>

          {/* Derived facts */}
          {selectedEnvironment?.broker_user_id && (
            <div className="rounded-xl border border-border/60 bg-background/40 p-3 text-[11px] leading-5 text-foreground/55">
              <div className="flex items-center gap-2 mb-1">
                <Database aria-hidden size={12} className="text-primary/60" />
                <span className="font-semibold text-foreground/70">Broker binding</span>
              </div>
              <p>
                Active environment is bound to broker user{" "}
                <span className="font-mono text-primary">{selectedEnvironment.broker_user_id}</span>.
                Algo-worker tokens with <span className="font-mono text-primary">live</span> mode resolve
                to this user for live order routing.
              </p>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
