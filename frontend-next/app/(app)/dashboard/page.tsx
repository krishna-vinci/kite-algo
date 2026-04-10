"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { AppAuthPanel } from "@/components/options/app-auth-panel";
import type { RuntimeStatus } from "@/components/options/types";
import { KpiCard } from "@/components/operator/kpi-card";
import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
import { StatusBadge } from "@/components/operator/status-badge";
import { fetchRuntimeStatus, loginApp } from "@/lib/options/api";

const metrics = [
  { label: "Index", value: "25,304.60", delta: "+0.42%", note: "mock market snapshot" },
  { label: "Positions", value: "12", delta: "+2", note: "6 hedged / 4 open" },
  { label: "Latency", value: "18ms", delta: "-4ms", note: "gateway round trip" },
  { label: "Risk", value: "GREEN", note: "exposure within band" },
];

const feed = [
  { time: "09:15:02", message: "feed synced", tone: "positive" as const },
  { time: "09:15:07", message: "watchlist refreshed", tone: "neutral" as const },
  { time: "09:15:11", message: "risk checks clear", tone: "positive" as const },
  { time: "09:15:18", message: "awaiting trigger", tone: "warning" as const },
];

const watchlist = [
  { symbol: "NIFTY", bias: "bullish", price: "25,304.60" },
  { symbol: "BANKNIFTY", bias: "neutral", price: "51,682.20" },
  { symbol: "FINNIFTY", bias: "bearish", price: "24,207.85" },
];

function fallbackStatus(): RuntimeStatus {
  return { brokerConnected: false, websocketStatus: "degraded", paperAvailable: true, appAuthenticated: false };
}

export default function DashboardPage() {
  const [status, setStatus] = useState<RuntimeStatus>(fallbackStatus());
  const [loginPending, setLoginPending] = useState(false);

  useEffect(() => {
    let disposed = false;
    async function load() {
      try {
        const next = await fetchRuntimeStatus();
        if (!disposed) {
          setStatus(next);
        }
      } catch {
        if (!disposed) {
          setStatus(fallbackStatus());
        }
      }
    }
    void load();
    const interval = window.setInterval(load, 30000);
    return () => {
      disposed = true;
      window.clearInterval(interval);
    };
  }, []);

  async function handleLogin(payload: { username: string; password: string }) {
    setLoginPending(true);
    try {
      await loginApp(payload);
      const refreshed = await fetchRuntimeStatus();
      setStatus(refreshed);
      toast.success("Dashboard sign-in successful");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Login failed");
    } finally {
      setLoginPending(false);
    }
  }

  return (
    <div className="space-y-4 pb-4">
      {!status.appAuthenticated ? <AppAuthPanel onLogin={handleLogin} pending={loginPending} compact /> : null}

      <Panel eyebrow="dashboard" title="Operator overview" action={<StatusBadge tone={status.appAuthenticated ? "positive" : "warning"}>{status.appAuthenticated ? "signed in" : "sign in required"}</StatusBadge>}>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {metrics.map((item) => (
            <KpiCard key={item.label} {...item} />
          ))}
        </div>
      </Panel>

      <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <Panel eyebrow="feed" title="Execution stream">
          <div className="space-y-3 font-mono text-sm">
            {feed.map((entry) => (
              <div key={entry.time} className="flex items-center justify-between gap-4 rounded-xl border border-border/60 bg-background/60 px-3 py-2">
                <span className="text-foreground/40">{entry.time}</span>
                <span className="flex-1 text-right text-foreground/80">{entry.message}</span>
                <StatusBadge tone={entry.tone}>{entry.tone}</StatusBadge>
              </div>
            ))}
          </div>
        </Panel>

        <Panel eyebrow="watchlist" title="Pinned symbols">
          <div className="space-y-3">
            {watchlist.map((item) => (
              <div key={item.symbol} className="flex items-center justify-between gap-3 rounded-2xl border border-border/60 bg-background/60 px-4 py-3">
                <div>
                  <p className="font-mono text-sm font-semibold tracking-[0.2em] text-primary">{item.symbol}</p>
                  <p className="mt-1 text-xs uppercase tracking-[0.3em] text-foreground/40">{item.bias}</p>
                </div>
                <p className="font-mono text-base text-foreground/80">{item.price}</p>
              </div>
            ))}
          </div>
          <SectionLabel className="mt-4" eyebrow="session" title="Console state" description={status.appAuthenticated ? "App auth is active. Options page can use real backend flows and paper execution." : "Sign in here first so the options workspace can use real sessions, broker status, and paper execution."} />
        </Panel>
      </div>
    </div>
  );
}
