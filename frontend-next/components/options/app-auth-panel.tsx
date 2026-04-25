"use client";

import { useState } from "react";

type AppAuthPanelProps = Readonly<{
  onLogin: (payload: { username: string; password: string }) => Promise<void>;
  pending: boolean;
  compact?: boolean;
}>;

export function AppAuthPanel({ onLogin, pending, compact = false }: AppAuthPanelProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  return (
    <section className={`rounded-2xl border border-[var(--accent-border)] bg-[var(--panel)] ${compact ? "p-4" : "p-5"}`}>
      <p className="text-[10px] uppercase tracking-[0.24em] text-[var(--dim)]">app authentication required</p>
      <h2 className="mt-2 text-lg font-semibold text-[var(--text)]">Login to unlock live options, paper runtime, and broker actions</h2>
      <p className="mt-2 text-[12px] text-[var(--muted)]">Use dashboard sign-in once. Session refresh should now stay consistent across the app.</p>
      <form
        className="mt-4 grid gap-3 md:grid-cols-[1fr_1fr_auto]"
        onSubmit={async (event) => {
          event.preventDefault();
          await onLogin({ username, password });
        }}
      >
        <input
          aria-label="app username"
          value={username}
          onChange={(event) => setUsername(event.currentTarget.value)}
          placeholder="Username"
          className="rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-[var(--text)] outline-none"
        />
        <input
          aria-label="app password"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.currentTarget.value)}
          placeholder="Password"
          className="rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-[var(--text)] outline-none"
        />
        <button type="submit" disabled={pending || !username || !password} className="rounded-md border border-[var(--accent-border)] bg-[var(--accent-soft)] px-4 py-2 text-[12px] font-semibold text-[var(--accent)] disabled:opacity-60">
          {pending ? "Logging in…" : "Login"}
        </button>
      </form>
    </section>
  );
}
