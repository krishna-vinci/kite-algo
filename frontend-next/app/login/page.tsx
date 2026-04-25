"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { toast } from "sonner";
import type { RuntimeStatus } from "@/components/options/types";
import { fetchRuntimeStatus, loginApp } from "@/lib/options/api";

function fallbackStatus(): RuntimeStatus {
  return {
    brokerConnected: false,
    brokerStatus: "unknown",
    brokerMode: "system",
    brokerLastSuccessAt: null,
    brokerLastFailureAt: null,
    brokerLastError: null,
    brokerNextRefreshAt: null,
    websocketStatus: "unknown",
    paperAvailable: false,
    appAuthenticated: false,
  };
}

function formatDateTime(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function sanitizeNextHref(value: string | null) {
  if (!value || !value.startsWith("/") || value.startsWith("//")) {
    return "/dashboard";
  }
  return value;
}

export default function LoginPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const nextHref = useMemo(() => sanitizeNextHref(searchParams.get("next")), [searchParams]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [status, setStatus] = useState<RuntimeStatus>(fallbackStatus());

  useEffect(() => {
    let disposed = false;
    async function load() {
      try {
        const next = await fetchRuntimeStatus();
        if (disposed) return;
        setStatus(next);
        if (next.appAuthenticated) {
          router.replace(nextHref);
        }
      } catch {
        if (!disposed) {
          setStatus(fallbackStatus());
        }
      }
    }
    void load();
    return () => {
      disposed = true;
    };
  }, [nextHref, router]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    try {
      await loginApp({ username, password });
      toast.success("Signed in");
      router.replace(nextHref);
      router.refresh();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Login failed");
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-[var(--bg)] px-4 py-10 text-[var(--text)]">
      <div className="grid w-full max-w-5xl gap-4 lg:grid-cols-[1.1fr_0.9fr]">
        <section className="rounded-3xl border border-[var(--border)] bg-[var(--panel)] p-6 shadow-2xl shadow-black/20">
          <p className="text-[11px] uppercase tracking-[0.28em] text-[var(--dim)]">kite algo operator access</p>
          <h1 className="mt-3 text-3xl font-semibold text-[var(--text)]">App login</h1>
          <p className="mt-3 max-w-xl text-sm text-[var(--muted)]">This login only unlocks the operator UI. Live algos and broker connectivity are owned by the backend system session, so browser logout should not stop active strategy execution.</p>

          <form className="mt-6 grid gap-4" onSubmit={handleSubmit}>
            <label className="grid gap-2 text-sm text-[var(--muted)]">
              Username
              <input
                aria-label="app username"
                value={username}
                onChange={(event) => setUsername(event.currentTarget.value)}
                placeholder="Username"
                className="rounded-xl border border-[var(--border)] bg-[var(--bg)] px-4 py-3 text-[var(--text)] outline-none"
              />
            </label>
            <label className="grid gap-2 text-sm text-[var(--muted)]">
              Password
              <input
                aria-label="app password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.currentTarget.value)}
                placeholder="Password"
                className="rounded-xl border border-[var(--border)] bg-[var(--bg)] px-4 py-3 text-[var(--text)] outline-none"
              />
            </label>
            <button
              type="submit"
              disabled={pending || !username || !password}
              className="rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] px-4 py-3 text-sm font-semibold text-[var(--accent)] disabled:opacity-60"
            >
              {pending ? "Signing in…" : "Login to app"}
            </button>
          </form>
        </section>

        <aside className="rounded-3xl border border-[var(--border)] bg-[var(--panel)] p-6">
          <p className="text-[11px] uppercase tracking-[0.28em] text-[var(--dim)]">system broker health</p>
          <div className="mt-4 space-y-3 text-sm text-[var(--muted)]">
            <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg)]/70 px-4 py-3">
              <p className="text-[11px] uppercase tracking-[0.2em] text-[var(--dim)]">broker status</p>
              <p className="mt-2 font-mono text-lg text-[var(--text)]">{status.brokerStatus}</p>
            </div>
            <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg)]/70 px-4 py-3">
              <p className="text-[11px] uppercase tracking-[0.2em] text-[var(--dim)]">scheduled refresh</p>
              <p className="mt-2 font-mono text-lg text-[var(--text)]">08:00 IST daily</p>
              <p className="mt-2 text-xs">Next refresh: {formatDateTime(status.brokerNextRefreshAt)}</p>
            </div>
            <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg)]/70 px-4 py-3">
              <p className="text-[11px] uppercase tracking-[0.2em] text-[var(--dim)]">last success</p>
              <p className="mt-2 font-mono text-lg text-[var(--text)]">{formatDateTime(status.brokerLastSuccessAt)}</p>
            </div>
            <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg)]/70 px-4 py-3">
              <p className="text-[11px] uppercase tracking-[0.2em] text-[var(--dim)]">last failure</p>
              <p className="mt-2 font-mono text-lg text-[var(--text)]">{formatDateTime(status.brokerLastFailureAt)}</p>
              <p className="mt-2 text-xs">{status.brokerLastError ?? "No broker login error recorded."}</p>
            </div>
          </div>
        </aside>
      </div>
    </main>
  );
}
