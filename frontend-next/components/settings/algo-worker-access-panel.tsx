"use client";

import { useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Copy, KeyRound, RefreshCw, ShieldCheck, Terminal, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import {
  createAlgoWorkerToken,
  getKiteProfile,
  listAlgoWorkerTokens,
  revokeAlgoWorkerToken,
  type AlgoWorkerToken,
  type CreatedAlgoWorkerToken,
} from "@/lib/algo-workers/api";

const TOKEN_QUERY_KEY = ["algo-worker-tokens"];
const DEFAULT_ACCOUNT_SCOPE = "kite:paper-a";

const MODE_OPTIONS = [
  {
    value: "paper",
    label: "Paper",
    description: "Simulated execution without broker order placement.",
  },
  {
    value: "dry_run",
    label: "Dry run",
    description: "Validate order intents and risk flows without live execution.",
  },
  {
    value: "live",
    label: "Live",
    description: "Allow real broker-backed runs for the resolved Kite account scope.",
  },
] as const;

const ACTION_OPTIONS = [
  {
    value: "heartbeat",
    label: "Heartbeat",
    description: "Worker health pings and telemetry updates.",
  },
  {
    value: "runs:create",
    label: "Create runs",
    description: "Create new strategy runs.",
  },
  {
    value: "runs:read",
    label: "Read runs",
    description: "Read run state, orders, trades, funds, and P&L.",
  },
  {
    value: "intents:submit",
    label: "Submit intents",
    description: "Submit order/basket intents for the run.",
  },
  {
    value: "risk:update",
    label: "Update risk",
    description: "Patch risk and protection values.",
  },
  {
    value: "runs:exit",
    label: "Exit runs",
    description: "Request backend-managed exits.",
  },
  {
    value: "funds:read",
    label: "Read funds",
    description: "Read account and run funds snapshots.",
  },
  {
    value: "market:read",
    label: "Read market",
    description: "Read symbols, quotes, candles, snapshots, and options market data.",
  },
  {
    value: "market:stream",
    label: "Stream market",
    description: "Stream worker market ticks/candles.",
  },
] as const;

function isPaperScope(value: string | null | undefined): boolean {
  const normalized = String(value || "").trim().toLowerCase();
  if (!normalized) return false;
  const identifier = normalized.startsWith("kite:") ? normalized.slice(5) : normalized;
  return (
    identifier === "paper" ||
    identifier.startsWith("paper-") ||
    identifier.startsWith("paper_") ||
    identifier.startsWith("test-paper") ||
    identifier.endsWith("-paper") ||
    identifier.endsWith("_paper")
  );
}

function describeTokenScope(token: AlgoWorkerToken): string {
  const scope = token.accountScope?.trim() || "";
  if (!scope) return "Any paper scope";
  if (token.allowedModes.includes("live") && !isPaperScope(scope)) {
    return `Live ${scope} + any paper scope`;
  }
  return scope;
}

function splitTemplateList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function normalizeLiveAccountScopeInput(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return "";
  if (/^kite:/i.test(trimmed)) {
    const brokerUserId = trimmed.slice(5).trim();
    return brokerUserId ? `kite:${brokerUserId}` : "";
  }
  if (trimmed.includes(":")) return "";
  return `kite:${trimmed}`;
}

function toggleSelection(values: string[], value: string, checked: boolean): string[] {
  if (checked) {
    return values.includes(value) ? values : [...values, value];
  }
  return values.filter((item) => item !== value);
}

function formatDate(value: string | null): string {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return date.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

async function copyText(value: string, label: string) {
  try {
    await navigator.clipboard.writeText(value);
    toast.success(`${label} copied`);
  } catch {
    toast.error(`Could not copy ${label.toLowerCase()}`);
  }
}

function IconButton({
  icon,
  children,
  onClick,
  disabled,
  variant = "default",
}: {
  icon: ReactNode;
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  variant?: "default" | "primary" | "danger";
}) {
  const variants = {
    default: "border-border/70 bg-background/60 text-foreground/70 hover:border-primary/30 hover:text-foreground",
    primary: "border-primary/40 bg-primary/10 text-primary hover:bg-primary/20",
    danger: "border-rose-400/30 bg-rose-400/10 text-rose-300 hover:bg-rose-400/20",
  };

  return (
    <button
      type="button"
      className={`inline-flex items-center gap-2 rounded-xl border px-3 py-2 text-[11px] font-medium uppercase tracking-[0.16em] transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${variants[variant]}`}
      onClick={onClick}
      disabled={disabled}
    >
      {icon}
      <span>{children}</span>
    </button>
  );
}

function CodeBlock({ value }: { value: string }) {
  return (
    <div className="rounded-xl border border-border/70 bg-background/80">
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap p-3 font-mono text-[11px] leading-5 text-foreground/75">
        <code>{value}</code>
      </pre>
    </div>
  );
}

function TokenRow({
  token,
  onRevoke,
  revoking,
}: {
  token: AlgoWorkerToken;
  onRevoke: (tokenId: string) => void;
  revoking: boolean;
}) {
  const statusTone = token.status === "active" ? "positive" : "neutral";

  return (
    <div className="grid gap-3 rounded-xl border border-border/70 bg-background/50 p-3 lg:grid-cols-[minmax(0,1fr)_auto]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <p className="truncate text-sm font-semibold text-foreground">{token.name}</p>
          <StatusBadge tone={statusTone}>{token.status}</StatusBadge>
        </div>
        <div className="mt-2 grid gap-2 text-xs text-foreground/55 sm:grid-cols-2 xl:grid-cols-4">
          <span className="min-w-0 truncate font-mono">{token.tokenId}</span>
          <span>Scope: {describeTokenScope(token)}</span>
          <span>Last used: {formatDate(token.lastUsedAt)}</span>
          <span>Expires: {formatDate(token.expiresAt)}</span>
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {token.allowedModes.map((mode) => (
            <span key={mode} className="rounded-lg border border-border/60 px-2 py-0.5 text-[10px] uppercase tracking-[0.16em] text-foreground/50">
              {mode}
            </span>
          ))}
          {token.allowedTemplates.length > 0 ? (
            token.allowedTemplates.map((template) => (
              <span key={template} className="rounded-lg border border-primary/20 px-2 py-0.5 font-mono text-[10px] text-primary/80">
                {template}
              </span>
            ))
          ) : (
            <span className="rounded-lg border border-border/60 px-2 py-0.5 text-[10px] uppercase tracking-[0.16em] text-foreground/50">
              all templates
            </span>
          )}
        </div>
      </div>
      <div className="flex items-start justify-end gap-2">
        <IconButton
          icon={<Trash2 aria-hidden size={14} />}
          variant="danger"
          onClick={() => onRevoke(token.tokenId)}
          disabled={token.status !== "active" || revoking}
        >
          Revoke
        </IconButton>
      </div>
    </div>
  );
}

function WorkerQuickGuide({ token }: { token: CreatedAlgoWorkerToken | null }) {
  const rawToken = token?.token || "kwa_your_token";
  const runId = "run_mean_reversion_001";
  const authHeader = `Authorization: Bearer ${rawToken}`;
  const sampleMode = token?.allowedModes.includes("paper") ? "paper" : token?.allowedModes.includes("dry_run") ? "dry_run" : "live";
  const sampleAccountScope = sampleMode === "live" ? token?.accountScope || "kite:YOUR_BROKER_USER_ID" : DEFAULT_ACCOUNT_SCOPE;
  const createRunSnippet = `curl -X POST "$API_BASE/api/algo-workers/worker/runs" \\
  -H "${authHeader}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "strategy_run_id": "${runId}",
    "template_id": "mean-reversion",
    "account_scope": "${sampleAccountScope}",
    "execution_mode": "${sampleMode}",
    "metadata": {
      "strategy_family": "indicator_strategy",
      "strategy_name": "Mean Reversion"
    },
    "risk_schema": [
      {"key": "stop_loss_pct", "label": "Stop loss %", "type": "number", "value": 1.2, "editable": true},
      {"key": "target_pct", "label": "Target %", "type": "number", "value": 2.4, "editable": true}
    ],
    "runtime_state": {"risk": {"stop_loss_pct": 1.2, "target_pct": 2.4}}
  }'`;

  const orderSnippet = `curl -X POST "$API_BASE/api/algo-workers/worker/runs/${runId}/intents" \\
  -H "${authHeader}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "intent_type": "place_order",
    "idempotency_key": "${runId}:entry:1",
    "payload": {
      "order": {
        "symbol": "NIFTY 50",
        "side": "BUY",
        "quantity": 1,
        "order_type": "MARKET"
      }
    }
  }'`;

  const riskSnippet = `curl -X PATCH "$API_BASE/api/algo-workers/worker/runs/${runId}/risk" \\
  -H "${authHeader}" \\
  -H "Content-Type: application/json" \\
  -d '{"patch": {"stop_loss_pct": 0.8}, "reason": "volatility update"}'`;

  return (
    <div className="grid gap-3 xl:grid-cols-3">
      <div className="rounded-xl border border-border/70 bg-background/45 p-3">
        <div className="mb-3 flex items-center gap-2">
          <Terminal aria-hidden size={15} className="text-primary" />
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-foreground/60">Create run</p>
        </div>
        {token?.allowedModes.includes("live") && token.accountScope && !isPaperScope(token.accountScope) ? (
          <p className="mb-3 text-[11px] leading-5 text-foreground/55">
            This token is live-bound to <span className="font-mono text-primary">{token.accountScope}</span>, but it can still create paper or dry-run runs with <span className="font-mono text-primary">account_scope: &quot;{DEFAULT_ACCOUNT_SCOPE}&quot;</span> when those modes are enabled.
          </p>
        ) : null}
        <CodeBlock value={createRunSnippet} />
      </div>
      <div className="rounded-xl border border-border/70 bg-background/45 p-3">
        <div className="mb-3 flex items-center gap-2">
          <ShieldCheck aria-hidden size={15} className="text-primary" />
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-foreground/60">Submit intent</p>
        </div>
        <CodeBlock value={orderSnippet} />
      </div>
      <div className="rounded-xl border border-border/70 bg-background/45 p-3">
        <div className="mb-3 flex items-center gap-2">
          <RefreshCw aria-hidden size={15} className="text-primary" />
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-foreground/60">Patch risk</p>
        </div>
        <CodeBlock value={riskSnippet} />
      </div>
    </div>
  );
}

export function AlgoWorkerAccessPanel() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("paper-worker");
  const [accountScope, setAccountScope] = useState(DEFAULT_ACCOUNT_SCOPE);
  const [manualLiveAccountScope, setManualLiveAccountScope] = useState("");
  const [templatesText, setTemplatesText] = useState("");
  const [selectedModes, setSelectedModes] = useState<string[]>(["paper", "dry_run"]);
  const [selectedActions, setSelectedActions] = useState<string[]>(ACTION_OPTIONS.map((item) => item.value));
  const [liveAcknowledged, setLiveAcknowledged] = useState(false);
  const [createdToken, setCreatedToken] = useState<CreatedAlgoWorkerToken | null>(null);

  const tokensQuery = useQuery({
    queryKey: TOKEN_QUERY_KEY,
    queryFn: listAlgoWorkerTokens,
  });

  const kiteProfileQuery = useQuery({
    queryKey: ["kite-profile-for-worker-token"],
    queryFn: getKiteProfile,
  });

  const brokerUserId = kiteProfileQuery.data?.userId ?? null;
  const liveModeEnabled = selectedModes.includes("live");
  const resolvedLiveAccountScope = brokerUserId
    ? `kite:${brokerUserId}`
    : normalizeLiveAccountScopeInput(manualLiveAccountScope);
  const effectiveAccountScope = liveModeEnabled ? resolvedLiveAccountScope || null : accountScope.trim() || null;
  const liveTokenBlocked = liveModeEnabled && (!resolvedLiveAccountScope || !liveAcknowledged);
  const invalidSelection = selectedModes.length === 0 || selectedActions.length === 0;

  const createMutation = useMutation({
    mutationFn: () =>
      createAlgoWorkerToken({
        name: name.trim() || "paper-worker",
        accountScope: effectiveAccountScope,
        allowedModes: selectedModes,
        allowedActions: selectedActions,
        allowedTemplates: splitTemplateList(templatesText),
      }),
    onSuccess: (token) => {
      setCreatedToken(token);
      queryClient.invalidateQueries({ queryKey: TOKEN_QUERY_KEY });
      toast.success("Worker token created");
    },
    onError: (error: unknown) => {
      const detail =
        error instanceof Error
          ? error.message
          : typeof error === "string"
            ? error
            : "Could not create worker token";
      toast.error(detail);
    },
  });

  const revokeMutation = useMutation({
    mutationFn: revokeAlgoWorkerToken,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: TOKEN_QUERY_KEY });
      toast.success("Worker token revoked");
    },
    onError: (error: unknown) => {
      const detail =
        error instanceof Error
          ? error.message
          : typeof error === "string"
            ? error
            : "Could not revoke worker token";
      toast.error(detail);
    },
  });

  const sortedTokens = useMemo(() => tokensQuery.data ?? [], [tokensQuery.data]);
  const activeCount = sortedTokens.filter((token) => token.status === "active").length;

  return (
    <Panel
      id="algo-worker-access"
      eyebrow="automation"
      title="Algo worker access"
      action={<StatusBadge tone={activeCount > 0 ? "positive" : "neutral"}>{activeCount} active</StatusBadge>}
    >
      <div className="grid gap-4 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)]">
        <div className="rounded-xl border border-border/70 bg-background/45 p-4">
          <div className="flex items-center gap-2">
            <KeyRound aria-hidden size={16} className="text-primary" />
            <p className="text-sm font-semibold text-foreground">New worker token</p>
          </div>
          <div className="mt-4 grid gap-3">
            <label className="grid gap-1.5 text-xs text-foreground/55">
              Name
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="rounded-xl border border-border/70 bg-background/70 px-3 py-2 text-sm text-foreground outline-none transition-colors focus:border-primary/45"
              />
            </label>
            <label className="grid gap-1.5 text-xs text-foreground/55">
              Account scope
              <input
                value={liveModeEnabled ? (brokerUserId ? resolvedLiveAccountScope : manualLiveAccountScope) : accountScope}
                onChange={(event) => {
                  if (liveModeEnabled) {
                    setManualLiveAccountScope(event.target.value);
                    return;
                  }
                  setAccountScope(event.target.value);
                }}
                disabled={liveModeEnabled && Boolean(brokerUserId)}
                placeholder={liveModeEnabled ? "AB1234 or kite:AB1234" : undefined}
                className="rounded-xl border border-border/70 bg-background/70 px-3 py-2 font-mono text-sm text-foreground outline-none transition-colors focus:border-primary/45"
              />
              {liveModeEnabled ? (
                <span className="text-[11px] leading-4 text-foreground/45">
                  Live worker tokens are bound to a Kite broker user id for live runs. Current profile: {brokerUserId ? <span className="font-mono text-primary">{brokerUserId}</span> : "not available"}. When auto-resolve is unavailable, enter the broker user id manually. The same token can still create paper or dry-run runs on scopes like <span className="font-mono text-primary">{DEFAULT_ACCOUNT_SCOPE}</span> when those modes are enabled.
                </span>
              ) : null}
            </label>
            <label className="grid gap-1.5 text-xs text-foreground/55">
              Template allow-list
              <input
                value={templatesText}
                onChange={(event) => setTemplatesText(event.target.value)}
                placeholder="mean-reversion, option-master"
                className="rounded-xl border border-border/70 bg-background/70 px-3 py-2 font-mono text-sm text-foreground outline-none transition-colors focus:border-primary/45"
              />
            </label>
            <div className="rounded-xl border border-border/70 bg-background/50 p-3">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-foreground/55">Execution modes</p>
              <div className="mt-2 grid gap-2 md:grid-cols-3">
                {MODE_OPTIONS.map((mode) => (
                  <label key={mode.value} className="flex items-start gap-2 rounded-lg border border-border/70 bg-background/35 px-2.5 py-2 text-xs text-foreground/80">
                    <input
                      type="checkbox"
                      checked={selectedModes.includes(mode.value)}
                      onChange={(event) => {
                        setSelectedModes((previous) => toggleSelection(previous, mode.value, event.target.checked));
                        if (mode.value === "live" && !event.target.checked) {
                          setLiveAcknowledged(false);
                        }
                      }}
                      className="mt-0.5 h-4 w-4 rounded border-border/70"
                    />
                    <span>
                      <span className="block font-semibold uppercase tracking-[0.14em] text-foreground/85">{mode.label}</span>
                      <span className="mt-1 block text-[11px] leading-4 text-foreground/55">{mode.description}</span>
                    </span>
                  </label>
                ))}
              </div>
              {selectedModes.length === 0 ? <p className="mt-2 text-[11px] text-amber-200">Select at least one execution mode.</p> : null}
            </div>

            <div className="rounded-xl border border-border/70 bg-background/50 p-3">
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-foreground/55">Worker capabilities</p>
                <button
                  type="button"
                  className="rounded-lg border border-border/70 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-foreground/60 transition-colors hover:border-primary/30 hover:text-foreground"
                  onClick={() => setSelectedActions(ACTION_OPTIONS.map((item) => item.value))}
                >
                  Select all
                </button>
              </div>
              <div className="mt-2 grid gap-2 md:grid-cols-2">
                {ACTION_OPTIONS.map((action) => (
                  <label key={action.value} className="flex items-start gap-2 rounded-lg border border-border/70 bg-background/35 px-2.5 py-2 text-xs text-foreground/80">
                    <input
                      type="checkbox"
                      checked={selectedActions.includes(action.value)}
                      onChange={(event) => {
                        setSelectedActions((previous) => toggleSelection(previous, action.value, event.target.checked));
                      }}
                      className="mt-0.5 h-4 w-4 rounded border-border/70"
                    />
                    <span>
                      <span className="block font-semibold text-foreground/90">{action.label}</span>
                      <span className="block font-mono text-[10px] text-primary/85">{action.value}</span>
                      <span className="mt-1 block text-[11px] leading-4 text-foreground/55">{action.description}</span>
                    </span>
                  </label>
                ))}
              </div>
              {selectedActions.length === 0 ? <p className="mt-2 text-[11px] text-amber-200">Select at least one worker capability.</p> : null}
            </div>

            {liveModeEnabled ? (
              <div className="rounded-xl border border-amber-400/30 bg-amber-400/10 p-3 text-xs leading-5 text-amber-200">
                <div className="flex items-start gap-2">
                  <AlertTriangle aria-hidden size={15} className="mt-0.5 shrink-0" />
                  <div>
                    <p className="font-semibold text-amber-100">Live worker tokens can place real broker orders.</p>
                    <p className="mt-1 text-amber-100/80">
                      Keep this token only on trusted worker machines. The worker must still set <span className="font-mono">KITE_ALGO_ENABLE_LIVE=1</span> and create runs with required live metadata. If paper or dry-run mode is also enabled, this same token can be reused without generating another token.
                    </p>
                  </div>
                </div>
                <label className="mt-3 flex items-start gap-2 text-amber-100/90">
                  <input
                    type="checkbox"
                    checked={liveAcknowledged}
                    onChange={(event) => setLiveAcknowledged(event.target.checked)}
                    disabled={!resolvedLiveAccountScope}
                    className="mt-1 h-4 w-4 rounded border-amber-200/60"
                  />
                  <span>I understand this token can place live orders for {resolvedLiveAccountScope || "the resolved Kite account"}.</span>
                </label>
                {kiteProfileQuery.isPending ? (
                  <p className="mt-2 text-amber-100/80">Loading Kite profile. If it does not resolve, you can enter the broker user id manually.</p>
                ) : null}
                {kiteProfileQuery.isError ? (
                  <p className="mt-2 text-amber-100/80">Could not load Kite profile. Login to Kite and refresh, or enter the Kite broker user id manually.</p>
                ) : null}
                {!brokerUserId && !kiteProfileQuery.isPending ? (
                  <p className="mt-2 text-amber-100/80">Enter a Kite broker user id such as <span className="font-mono">AB1234</span>. We will bind the token to <span className="font-mono">{resolvedLiveAccountScope || "kite:YOUR_BROKER_USER_ID"}</span>.</p>
                ) : null}
              </div>
            ) : null}

            <div className="rounded-xl border border-border/70 bg-background/40 p-3 text-[11px] leading-5 text-foreground/60">
              <p>
                <span className="font-semibold text-foreground/80">Options SDK note:</span> <span className="font-mono text-primary">market:read</span> and <span className="font-mono text-primary">runs:read</span> are required for option market snapshots and run-state reads via <span className="font-mono text-primary">/api/algo-workers/worker/options/*</span>.
              </p>
            </div>
            <IconButton
              icon={<KeyRound aria-hidden size={14} />}
              variant="primary"
              onClick={() => {
                if (invalidSelection) {
                  toast.error("Select at least one execution mode and worker capability");
                  return;
                }
                if (liveModeEnabled && !resolvedLiveAccountScope) {
                  toast.error("Login to Kite or enter a Kite broker user id before creating a live token");
                  return;
                }
                createMutation.mutate();
              }}
              disabled={createMutation.isPending || liveTokenBlocked || invalidSelection}
            >
              {liveModeEnabled ? (selectedModes.includes("paper") ? "Generate paper + live token" : "Generate live-only token") : "Generate"}
            </IconButton>
          </div>

          {createdToken ? (
            <div className="mt-4 rounded-xl border border-primary/30 bg-primary/10 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs font-semibold uppercase tracking-[0.18em] text-primary">Token shown once</p>
                <IconButton
                  icon={<Copy aria-hidden size={14} />}
                  onClick={() => copyText(createdToken.token, "Worker token")}
                >
                  Copy
                </IconButton>
              </div>
              <textarea
                readOnly
                value={createdToken.token}
                className="mt-3 min-h-20 w-full resize-none rounded-xl border border-primary/30 bg-background/80 p-3 font-mono text-xs text-foreground outline-none"
              />
            </div>
          ) : null}
        </div>

        <div className="space-y-3">
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-foreground/50">Issued tokens</p>
            <IconButton
              icon={<RefreshCw aria-hidden size={14} />}
              onClick={() => tokensQuery.refetch()}
              disabled={tokensQuery.isFetching}
            >
              Refresh
            </IconButton>
          </div>
          {tokensQuery.isLoading ? (
            <div className="rounded-xl border border-border/70 bg-background/45 p-4 text-sm text-foreground/55">Loading worker tokens...</div>
          ) : tokensQuery.isError ? (
            <div className="rounded-xl border border-rose-400/30 bg-rose-400/10 p-4 text-sm text-rose-300">Could not load worker tokens.</div>
          ) : sortedTokens.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border/70 bg-background/40 p-4 text-sm text-foreground/55">
              No worker tokens yet. Generate one for paper, dry-run, or a combined paper + dry-run + live worker.
            </div>
          ) : (
            sortedTokens.map((token) => (
              <TokenRow
                key={token.tokenId}
                token={token}
                onRevoke={(tokenId) => revokeMutation.mutate(tokenId)}
                revoking={revokeMutation.isPending && revokeMutation.variables === token.tokenId}
              />
            ))
          )}
        </div>
      </div>

      <div className="mt-4 rounded-xl border border-border/70 bg-background/35 p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="text-sm font-semibold text-foreground">Worker usage contract</p>
            <p className="mt-1 text-xs leading-5 text-foreground/55">
              Set <span className="font-mono text-primary">API_BASE</span>, send the bearer token, create one strategy run, submit idempotent intents, patch risk only through the run, and close the run when the strategy exits. A live-bound token can still be reused for paper or dry-run runs when those modes are enabled.
            </p>
          </div>
          <StatusBadge tone="warning">multi-mode v1</StatusBadge>
        </div>
        <WorkerQuickGuide token={createdToken} />
      </div>
    </Panel>
  );
}
