"use client";

import { useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, KeyRound, RefreshCw, ShieldCheck, Terminal, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import {
  createAlgoWorkerToken,
  listAlgoWorkerTokens,
  revokeAlgoWorkerToken,
  type AlgoWorkerToken,
  type CreatedAlgoWorkerToken,
} from "@/lib/algo-workers/api";

const TOKEN_QUERY_KEY = ["algo-worker-tokens"];
const DEFAULT_ACCOUNT_SCOPE = "kite:paper-a";

function splitTemplateList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
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
          <span>Scope: {token.accountScope || "Any paper scope"}</span>
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
  const createRunSnippet = `curl -X POST "$API_BASE/api/algo-workers/worker/runs" \\
  -H "${authHeader}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "strategy_run_id": "${runId}",
    "template_id": "mean-reversion",
    "account_scope": "${token?.accountScope || DEFAULT_ACCOUNT_SCOPE}",
    "execution_mode": "paper",
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
  const [templatesText, setTemplatesText] = useState("");
  const [createdToken, setCreatedToken] = useState<CreatedAlgoWorkerToken | null>(null);

  const tokensQuery = useQuery({
    queryKey: TOKEN_QUERY_KEY,
    queryFn: listAlgoWorkerTokens,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      createAlgoWorkerToken({
        name: name.trim() || "paper-worker",
        accountScope: accountScope.trim() || null,
        allowedTemplates: splitTemplateList(templatesText),
      }),
    onSuccess: (token) => {
      setCreatedToken(token);
      queryClient.invalidateQueries({ queryKey: TOKEN_QUERY_KEY });
      toast.success("Worker token created");
    },
    onError: () => toast.error("Could not create worker token"),
  });

  const revokeMutation = useMutation({
    mutationFn: revokeAlgoWorkerToken,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: TOKEN_QUERY_KEY });
      toast.success("Worker token revoked");
    },
    onError: () => toast.error("Could not revoke worker token"),
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
            <p className="text-sm font-semibold text-foreground">New paper worker token</p>
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
                value={accountScope}
                onChange={(event) => setAccountScope(event.target.value)}
                className="rounded-xl border border-border/70 bg-background/70 px-3 py-2 font-mono text-sm text-foreground outline-none transition-colors focus:border-primary/45"
              />
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
            <IconButton
              icon={<KeyRound aria-hidden size={14} />}
              variant="primary"
              onClick={() => createMutation.mutate()}
              disabled={createMutation.isPending}
            >
              Generate
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
              No worker tokens yet. Generate one for paper or dry-run strategy development.
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
              Set <span className="font-mono text-primary">API_BASE</span>, send the bearer token, create one strategy run, submit idempotent intents, patch risk only through the run, and close the run when the strategy exits.
            </p>
          </div>
          <StatusBadge tone="warning">paper v1</StatusBadge>
        </div>
        <WorkerQuickGuide token={createdToken} />
      </div>
    </Panel>
  );
}
