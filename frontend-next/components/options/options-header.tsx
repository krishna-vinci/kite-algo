"use client";

import type { RuntimeStatus } from "@/components/options/types";

type OptionsHeaderProps = Readonly<{
  status: RuntimeStatus;
  onBrokerLogin: () => void;
  loginPending: boolean;
}>;

function StatusPill({ label, value, tone }: Readonly<{ label: string; value: string; tone: "neutral" | "positive" | "negative" }>) {
  const toneClass =
    tone === "positive"
      ? "border-[var(--green)]/30 text-[var(--green)]"
      : tone === "negative"
        ? "border-[var(--red)]/30 text-[var(--red)]"
        : "border-[var(--border)] text-[var(--muted)]";

  return (
    <div className={`rounded-md border px-2 py-1 text-[10px] uppercase tracking-[0.12em] ${toneClass}`}>
      <span className="text-[var(--dim)]">{label}</span> {value}
    </div>
  );
}

function brokerTone(status: RuntimeStatus["brokerStatus"]): "neutral" | "positive" | "negative" {
  if (status === "connected") return "positive";
  if (status === "degraded" || status === "disconnected") return "negative";
  return "neutral";
}

export function OptionsHeader({ status, onBrokerLogin, loginPending }: OptionsHeaderProps) {
  const brokerLabel = status.brokerStatus === "connected" ? "on" : status.brokerStatus;
  return (
    <header className="flex flex-wrap items-center gap-2 rounded-2xl border border-[var(--border)] bg-[var(--panel)] px-3 py-2">
      <h1 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-[var(--text)]">Options</h1>
      <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
        <StatusPill label="broker" value={brokerLabel} tone={brokerTone(status.brokerStatus)} />
        <StatusPill label="ws" value={status.websocketStatus === "connected" ? "on" : status.websocketStatus} tone={status.websocketStatus === "connected" ? "positive" : "neutral"} />
        <StatusPill label="paper" value={status.paperAvailable ? "on" : "off"} tone={status.paperAvailable ? "positive" : "negative"} />
        {status.brokerStatus !== "connected" && (
          <span className="rounded-md border border-[var(--yellow)]/30 px-2 py-1 text-[10px] text-[var(--yellow)]">system broker {status.brokerStatus}</span>
        )}
        <button
          type="button"
          onClick={onBrokerLogin}
          disabled={loginPending}
          className="cursor-pointer rounded-md border border-[var(--accent-border)] bg-[var(--accent-soft)] px-3 py-1.5 text-[10px] font-semibold text-[var(--accent)] transition-colors duration-200 hover:bg-[var(--accent)]/15 disabled:cursor-not-allowed disabled:opacity-70"
        >
          {loginPending ? "Refreshing…" : "Refresh broker"}
        </button>
      </div>
    </header>
  );
}
