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

export function OptionsHeader({ status, onBrokerLogin, loginPending }: OptionsHeaderProps) {
  return (
    <header className="flex flex-wrap items-center gap-2 rounded-2xl border border-[var(--border)] bg-[var(--panel)] px-3 py-2">
      <div>
        <p className="text-[10px] uppercase tracking-[0.24em] text-[var(--dim)]">options command deck</p>
        <h1 className="mt-1 text-lg font-semibold text-[var(--text)]">Strategy-aware options workspace</h1>
      </div>
      <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
        <StatusPill label="broker" value={status.brokerConnected ? "connected" : "disconnected"} tone={status.brokerConnected ? "positive" : "negative"} />
        <StatusPill label="ws" value={status.websocketStatus} tone={status.websocketStatus === "connected" ? "positive" : "neutral"} />
        <StatusPill label="paper" value={status.paperAvailable ? "ready" : "unavailable"} tone={status.paperAvailable ? "positive" : "negative"} />
        <button
          type="button"
          onClick={onBrokerLogin}
          disabled={loginPending}
          className="rounded-md border border-[var(--accent-border)] bg-[var(--accent-soft)] px-3 py-2 text-[11px] font-semibold text-[var(--accent)] transition hover:bg-[var(--accent)]/15 disabled:cursor-not-allowed disabled:opacity-70"
        >
          {loginPending ? "Connecting…" : "Login to broker"}
        </button>
      </div>
    </header>
  );
}
