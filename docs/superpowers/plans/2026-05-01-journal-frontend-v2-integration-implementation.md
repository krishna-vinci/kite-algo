# Journal Frontend V2 Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing Journal frontend into a coherent V2-integrated workspace that exposes the backend's environment-scoped episodes, notes, unresolved queue, analytics, and strategy views properly.

**Architecture:** Keep the existing app shell and Journal routes, but introduce a Journal-scoped shared environment state and a small set of reusable Journal workspace components. Refactor the existing pages to consume shared Journal context, preserve `environment_id` safely, and present the backend's real V2 workflow: overview → episodes → episode detail → analytics/strategies/notes/unresolved.

**Tech Stack:** Next.js App Router, React client components, existing operator/journal UI components, existing `lib/journal/api.ts` helpers, Vitest, TypeScript.

---

## File structure and responsibilities

### Create

- `frontend-next/app/(app)/journal/layout.tsx` — Journal-only wrapper that mounts shared Journal workspace state around all Journal routes.
- `frontend-next/components/journal/journal-workspace-provider.tsx` — shared environment state, environment loading, URL/session sync, and helper hooks.
- `frontend-next/components/journal/journal-workspace-header.tsx` — reusable header area for Journal pages (title, nav, dev notice, environment selector, status/empty state).
- `frontend-next/components/journal/journal-page-link.tsx` — helper for Journal internal links that automatically preserve `environment_id`.

### Modify

- `frontend-next/components/journal/environment-selector.tsx` — support richer display and optional disabled/loading states.
- `frontend-next/components/journal/journal-nav.tsx` — align tabs to V2 workflow and preserve Journal environment query param.
- `frontend-next/components/journal/journal-v2-dev-notice.tsx` — convert from generic notice to concise backend-aligned operational note.
- `frontend-next/components/journal/markdown-note-editor.tsx` — support title/help/error/saved states needed for real note workflow.
- `frontend-next/lib/journal/types.ts` — add note revision types and any view-model types needed for Journal workspace state.
- `frontend-next/lib/journal/api.ts` — add note revision helper and any small Journal page helpers that reduce duplication.
- `frontend-next/app/(app)/journal/page.tsx` — rebuild overview around shared Journal state and V2 backend surfaces.
- `frontend-next/app/(app)/journal/episodes/page.tsx` — convert to shared environment-driven episode ledger.
- `frontend-next/app/(app)/journal/episodes/[episodeId]/page.tsx` — strengthen as episode review workspace with timeline + notes + revisions.
- `frontend-next/app/(app)/journal/analytics/page.tsx` — strengthen analytics page and make it consume shared Journal environment state.
- `frontend-next/app/(app)/journal/strategies/page.tsx` — strengthen strategy scorecards page.
- `frontend-next/app/(app)/journal/notes/page.tsx` — make notes archive and note workflow usable.
- `frontend-next/app/(app)/journal/unresolved/page.tsx` — make unresolved queue operational and environment-aware.
- `frontend-next/tests/journal-v2-pages.test.tsx` — update page tests for shared environment state and richer V2 workflow.

### Keep unchanged unless a task explicitly needs them

- `frontend-next/app/(app)/journal/calendar/page.tsx`
- `frontend-next/app/(app)/journal/trades/page.tsx`
- `frontend-next/app/(app)/journal/rules/page.tsx`
- `frontend-next/app/(app)/journal/insights/page.tsx`

These legacy/non-core pages can stay available, but the primary Journal V2 workflow should no longer depend on them.

---

## Task 1: Add shared Journal workspace state

**Files:**
- Create: `frontend-next/app/(app)/journal/layout.tsx`
- Create: `frontend-next/components/journal/journal-workspace-provider.tsx`
- Modify: `frontend-next/lib/journal/types.ts`
- Test: `frontend-next/tests/journal-v2-pages.test.tsx`

- [ ] **Step 1: Write the failing test for shared environment preservation**

Add a test that renders two Journal pages inside the new Journal layout and verifies selected environment state is reused.

```tsx
it("preserves selected Journal environment across Journal route renders", async () => {
  window.history.pushState({}, "", "/journal?environment_id=env-1");

  renderWithQueryClient(
    <JournalWorkspaceProvider>
      <JournalOverviewPage />
      <JournalEpisodesPage />
    </JournalWorkspaceProvider>,
  );

  await waitFor(() => expect(fetchJournalEnvironmentsMock).toHaveBeenCalled());
  expect(screen.getAllByDisplayValue("env-1").length).toBeGreaterThan(0);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd frontend-next && npm test -- journal-v2-pages.test.tsx
```

Expected:

```text
FAIL because JournalWorkspaceProvider does not exist and pages still own their own environment state
```

- [ ] **Step 3: Add Journal workspace provider and layout**

Create a typed context that loads environments once, resolves `selectedEnvironmentId` from URL/session, exposes the selected environment, and updates URL/session when selection changes.

```tsx
// frontend-next/components/journal/journal-workspace-provider.tsx
"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { fetchJournalEnvironments } from "@/lib/journal/api";
import type { JournalEnvironment } from "@/lib/journal/types";

type JournalWorkspaceContextValue = {
  environments: JournalEnvironment[];
  environmentsLoading: boolean;
  environmentsError: string | null;
  selectedEnvironmentId: string;
  selectedEnvironment: JournalEnvironment | null;
  setSelectedEnvironmentId: (value: string) => void;
};

const JournalWorkspaceContext = createContext<JournalWorkspaceContextValue | null>(null);

export function JournalWorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [environments, setEnvironments] = useState<JournalEnvironment[]>([]);
  const [environmentsLoading, setEnvironmentsLoading] = useState(true);
  const [environmentsError, setEnvironmentsError] = useState<string | null>(null);
  const [selectedEnvironmentId, setSelectedEnvironmentIdState] = useState("");

  useEffect(() => {
    const fromUrl = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("environment_id") ?? "" : "";
    const fromSession = typeof window !== "undefined" ? window.sessionStorage.getItem("journal.v2.environment_id") ?? "" : "";
    setSelectedEnvironmentIdState(fromUrl || fromSession);
  }, []);

  useEffect(() => {
    let closed = false;
    setEnvironmentsLoading(true);
    fetchJournalEnvironments()
      .then((items) => {
        if (closed) return;
        setEnvironments(items);
        setEnvironmentsError(null);
      })
      .catch((error) => {
        if (closed) return;
        setEnvironments([]);
        setEnvironmentsError(error instanceof Error ? error.message : "Failed to load environments");
      })
      .finally(() => {
        if (!closed) setEnvironmentsLoading(false);
      });
    return () => {
      closed = true;
    };
  }, []);

  function setSelectedEnvironmentId(value: string) {
    setSelectedEnvironmentIdState(value);
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (value) {
      url.searchParams.set("environment_id", value);
      window.sessionStorage.setItem("journal.v2.environment_id", value);
    } else {
      url.searchParams.delete("environment_id");
      window.sessionStorage.removeItem("journal.v2.environment_id");
    }
    window.history.replaceState({}, "", url.toString());
  }

  const selectedEnvironment = useMemo(
    () => environments.find((item) => item.id === selectedEnvironmentId) ?? null,
    [environments, selectedEnvironmentId],
  );

  const value = useMemo(
    () => ({ environments, environmentsLoading, environmentsError, selectedEnvironmentId, selectedEnvironment, setSelectedEnvironmentId }),
    [environments, environmentsLoading, environmentsError, selectedEnvironmentId, selectedEnvironment],
  );

  return <JournalWorkspaceContext.Provider value={value}>{children}</JournalWorkspaceContext.Provider>;
}

export function useJournalWorkspace() {
  const context = useContext(JournalWorkspaceContext);
  if (!context) throw new Error("useJournalWorkspace must be used inside JournalWorkspaceProvider");
  return context;
}
```

```tsx
// frontend-next/app/(app)/journal/layout.tsx
"use client";

import { JournalWorkspaceProvider } from "@/components/journal/journal-workspace-provider";

export default function JournalLayout({ children }: { children: React.ReactNode }) {
  return <JournalWorkspaceProvider>{children}</JournalWorkspaceProvider>;
}
```

- [ ] **Step 4: Add any missing shared types**

Extend Journal types with the note revision type now, since later tasks need it.

```ts
export type JournalNoteRevision = {
  note_id: string;
  revision_no: number;
  body_markdown: string;
  body_text?: string;
  edited_at?: string;
  change_reason?: string | null;
};
```

- [ ] **Step 5: Run the page test again**

Run:

```bash
cd frontend-next && npm test -- journal-v2-pages.test.tsx
```

Expected:

```text
PASS or fail only on the next missing shared-environment page assumptions
```

- [ ] **Step 6: Commit**

```bash
git add frontend-next/app/(app)/journal/layout.tsx frontend-next/components/journal/journal-workspace-provider.tsx frontend-next/lib/journal/types.ts frontend-next/tests/journal-v2-pages.test.tsx
git commit -m "feat: add shared journal workspace state"
```

## Task 2: Align Journal nav and common header behavior to V2

**Files:**
- Create: `frontend-next/components/journal/journal-page-link.tsx`
- Create: `frontend-next/components/journal/journal-workspace-header.tsx`
- Modify: `frontend-next/components/journal/journal-nav.tsx`
- Modify: `frontend-next/components/journal/environment-selector.tsx`
- Modify: `frontend-next/components/journal/journal-v2-dev-notice.tsx`
- Test: `frontend-next/tests/journal-v2-pages.test.tsx`

- [ ] **Step 1: Write the failing test for V2 nav and preserved environment links**

```tsx
it("renders V2 Journal nav tabs and preserves environment query in links", async () => {
  window.history.pushState({}, "", "/journal?environment_id=env-1");
  renderWithQueryClient(<JournalOverviewPage />);

  await waitFor(() => expect(fetchJournalEnvironmentsMock).toHaveBeenCalled());
  expect(screen.getByRole("link", { name: /Episodes/i })).toHaveAttribute("href", expect.stringContaining("environment_id=env-1"));
  expect(screen.getByRole("link", { name: /Unresolved/i })).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd frontend-next && npm test -- journal-v2-pages.test.tsx
```

Expected:

```text
FAIL because nav still uses old tabs and does not consistently preserve Journal environment state
```

- [ ] **Step 3: Add a shared Journal link helper and reusable Journal page header**

```tsx
// frontend-next/components/journal/journal-page-link.tsx
"use client";

import Link from "next/link";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";

export function JournalPageLink({ href, children, className }: { href: string; children: React.ReactNode; className?: string }) {
  const { selectedEnvironmentId } = useJournalWorkspace();
  const nextHref = selectedEnvironmentId
    ? `${href}${href.includes("?") ? "&" : "?"}environment_id=${encodeURIComponent(selectedEnvironmentId)}`
    : href;
  return <Link href={nextHref} className={className}>{children}</Link>;
}
```

```tsx
// frontend-next/components/journal/journal-workspace-header.tsx
"use client";

import { EnvironmentSelector } from "@/components/journal/environment-selector";
import { JournalHeader } from "@/components/journal/journal-header";
import { JournalNav } from "@/components/journal/journal-nav";
import { JournalV2DevNotice } from "@/components/journal/journal-v2-dev-notice";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import type { AnalysisPeriod } from "@/lib/journal/types";

export function JournalWorkspaceHeader({ period, setPeriod }: { period: AnalysisPeriod; setPeriod: (next: AnalysisPeriod) => void }) {
  const { environments, environmentsLoading, environmentsError, selectedEnvironmentId, setSelectedEnvironmentId } = useJournalWorkspace();

  return (
    <div className="space-y-4">
      <JournalHeader period={period} onPeriodChange={setPeriod} showPeriodSelector={false} />
      <JournalNav />
      <JournalV2DevNotice />
      <EnvironmentSelector
        environments={environments}
        selectedEnvironmentId={selectedEnvironmentId}
        onSelectEnvironment={setSelectedEnvironmentId}
        loading={environmentsLoading}
        error={environmentsError}
      />
    </div>
  );
}
```

- [ ] **Step 4: Convert nav and selector to V2-first behavior**

```tsx
// frontend-next/components/journal/journal-nav.tsx
const primaryTabs = [
  { label: "Overview", href: "/journal" },
  { label: "Episodes", href: "/journal/episodes" },
  { label: "Analytics", href: "/journal/analytics" },
  { label: "Notes", href: "/journal/notes" },
  { label: "Unresolved", href: "/journal/unresolved" },
  { label: "Strategies", href: "/journal/strategies" },
] as const;
```

```tsx
// frontend-next/components/journal/environment-selector.tsx
type EnvironmentSelectorProps = {
  environments: JournalEnvironment[];
  selectedEnvironmentId?: string;
  onSelectEnvironment: (environmentId: string) => void;
  loading?: boolean;
  error?: string | null;
};
```

```tsx
// frontend-next/components/journal/journal-v2-dev-notice.tsx
export function JournalV2DevNotice() {
  return (
    <div className="rounded-xl border border-blue-400/25 bg-blue-500/10 px-4 py-3 text-sm text-blue-100">
      Journal V2 is environment-scoped. Episodes, notes, unresolved items, and analytics shown here never mix paper and live data implicitly.
    </div>
  );
}
```

- [ ] **Step 5: Run test again**

Run:

```bash
cd frontend-next && npm test -- journal-v2-pages.test.tsx
```

Expected:

```text
PASS or fail only on the next page-level layout assumptions
```

- [ ] **Step 6: Commit**

```bash
git add frontend-next/components/journal/journal-page-link.tsx frontend-next/components/journal/journal-workspace-header.tsx frontend-next/components/journal/journal-nav.tsx frontend-next/components/journal/environment-selector.tsx frontend-next/components/journal/journal-v2-dev-notice.tsx frontend-next/tests/journal-v2-pages.test.tsx
git commit -m "feat: align journal navigation to v2 workflow"
```

## Task 3: Rebuild overview and episodes around shared Journal state

**Files:**
- Modify: `frontend-next/app/(app)/journal/page.tsx`
- Modify: `frontend-next/app/(app)/journal/episodes/page.tsx`
- Test: `frontend-next/tests/journal-v2-pages.test.tsx`

- [ ] **Step 1: Write the failing tests for overview and episodes workspace behavior**

```tsx
it("shows Journal V2 overview panels from shared environment state", async () => {
  window.history.pushState({}, "", "/journal?environment_id=env-1");
  fetchJournalEpisodesMock.mockResolvedValue([
    {
      id: "ep-1",
      environment_id: "env-1",
      execution_context_id: "ctx-1",
      episode_seq: 1,
      status: "closed",
      opened_at: "2026-05-01T10:00:00Z",
      closed_at: "2026-05-01T10:05:00Z",
      metadata: {},
    },
  ]);

  renderWithQueryClient(<JournalOverviewPage />);

  await waitFor(() => expect(fetchJournalV2AnalyticsSummaryMock).toHaveBeenCalledWith("env-1"));
  expect(screen.getByText(/Recent V2 episodes/i)).toBeInTheDocument();
  expect(screen.getByText(/Unresolved queue summary/i)).toBeInTheDocument();
});
```

```tsx
it("renders episode ledger entries linked with environment_id", async () => {
  window.history.pushState({}, "", "/journal/episodes?environment_id=env-1");
  fetchJournalEpisodesMock.mockResolvedValue([
    {
      id: "ep-1",
      environment_id: "env-1",
      execution_context_id: "ctx-1",
      episode_seq: 1,
      status: "open",
      opened_at: "2026-05-01T10:00:00Z",
      closed_at: null,
      metadata: {},
    },
  ]);

  renderWithQueryClient(<JournalEpisodesPage />);

  await waitFor(() => expect(fetchJournalEpisodesMock).toHaveBeenCalledWith({ environment_id: "env-1" }));
  expect(screen.getByRole("link", { name: /Episode #1/i })).toHaveAttribute("href", expect.stringContaining("environment_id=env-1"));
});
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
cd frontend-next && npm test -- journal-v2-pages.test.tsx
```

Expected:

```text
FAIL because pages still fetch/manage state directly instead of using the shared Journal workspace consistently
```

- [ ] **Step 3: Refactor overview to use shared Journal workspace state**

```tsx
// frontend-next/app/(app)/journal/page.tsx
const { environments, environmentsLoading, environmentsError, selectedEnvironmentId, selectedEnvironment } = useJournalWorkspace();

useEffect(() => {
  if (!selectedEnvironmentId) {
    setV2Metrics(null);
    setEpisodes([]);
    setUnresolvedItems([]);
    return;
  }
  // fetch summary, episodes, unresolved using selectedEnvironmentId
}, [selectedEnvironmentId]);

return (
  <div className="space-y-5 pb-5">
    <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />
    <Panel eyebrow="Journal V2" title="Environment-scoped overview">
      {/* current environment, live-state notice, no-environment states */}
    </Panel>
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">{/* KPI cards */}</div>
    <div className="grid gap-4 xl:grid-cols-[1.6fr_1fr]">{/* recent episodes, unresolved, quick links */}</div>
  </div>
);
```

- [ ] **Step 4: Refactor episodes page to use shared Journal workspace state**

```tsx
// frontend-next/app/(app)/journal/episodes/page.tsx
const { selectedEnvironmentId, selectedEnvironment } = useJournalWorkspace();

useEffect(() => {
  if (!selectedEnvironmentId) {
    setEpisodes([]);
    return;
  }
  fetchJournalEpisodes({ environment_id: selectedEnvironmentId }).then(setEpisodes);
}, [selectedEnvironmentId]);

return (
  <div className="space-y-5 pb-5">
    <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />
    <Panel eyebrow={selectedEnvironment ? `${selectedEnvironment.display_name || selectedEnvironment.account_scope} · ${selectedEnvironment.mode}` : "Episodes"} title="Episode ledger">
      {/* loading/error/empty states and linked episode cards */}
    </Panel>
  </div>
);
```

- [ ] **Step 5: Run tests again**

Run:

```bash
cd frontend-next && npm test -- journal-v2-pages.test.tsx
```

Expected:

```text
PASS for overview/episodes tests or fail only on episode detail/note assumptions from the next task
```

- [ ] **Step 6: Commit**

```bash
git add frontend-next/app/(app)/journal/page.tsx frontend-next/app/(app)/journal/episodes/page.tsx frontend-next/tests/journal-v2-pages.test.tsx
git commit -m "feat: refactor journal overview and episodes to shared v2 state"
```

## Task 4: Strengthen episode detail, notes, and revisions workflow

**Files:**
- Modify: `frontend-next/app/(app)/journal/episodes/[episodeId]/page.tsx`
- Modify: `frontend-next/components/journal/markdown-note-editor.tsx`
- Modify: `frontend-next/lib/journal/api.ts`
- Modify: `frontend-next/lib/journal/types.ts`
- Test: `frontend-next/tests/journal-v2-pages.test.tsx`

- [ ] **Step 1: Write the failing test for episode review workspace behavior**

```tsx
it("renders episode review workspace with timeline and note workflow", async () => {
  window.history.pushState({}, "", "/journal/episodes/ep-1?environment_id=env-1");
  fetchJournalTimelineMock.mockResolvedValue([
    { id: "evt-1", environment_id: "env-1", episode_id: "ep-1", execution_context_id: "ctx-1", subject_type: "episode", subject_id: "ep-1", event_type: "episode_opened", channel: "entry", actor_type: "system", correlation_id: null, causation_id: null, occurred_at: "2026-05-01T10:00:00Z", payload: {} },
  ]);
  fetchJournalNotesMock.mockResolvedValue([
    { id: "note-1", environment_id: "env-1", subject_type: "episode", subject_id: "ep-1", episode_id: "ep-1", note_type: "post_exit_review", title: "Review", body_markdown: "# Review", tags: [], updated_at: "2026-05-01T10:05:00Z" },
  ]);

  renderWithQueryClient(<JournalEpisodeDetailPage params={{ episodeId: "ep-1" }} />);

  await waitFor(() => expect(fetchJournalEpisodeMock).toHaveBeenCalledWith("ep-1", "env-1"));
  expect(screen.getByRole("heading", { name: /Episode activity/i })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /Episode note/i })).toBeInTheDocument();
  expect(screen.getByDisplayValue("# Review")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd frontend-next && npm test -- journal-v2-pages.test.tsx
```

Expected:

```text
FAIL because note revisions helper/types do not exist yet or episode detail still behaves as a minimal shell
```

- [ ] **Step 3: Add note revisions helper and richer editor props**

```ts
// frontend-next/lib/journal/api.ts
export async function fetchJournalNoteRevisions(noteId: string, environment_id: string): Promise<JournalNoteRevision[]> {
  const environmentId = requireEnvironmentId(environment_id, "fetchJournalNoteRevisions");
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>(
    `/api/journal/v2/notes/${noteId}/revisions${toSearchParams({ environment_id: environmentId })}`,
  );
  return (response.items ?? []).map((item) => ({
    note_id: String(item.note_id ?? noteId),
    revision_no: Number(item.revision_no ?? 0),
    body_markdown: String(item.body_markdown ?? ""),
    body_text: item.body_text != null ? String(item.body_text) : undefined,
    edited_at: item.edited_at != null ? String(item.edited_at) : undefined,
    change_reason: item.change_reason != null ? String(item.change_reason) : null,
  }));
}
```

```tsx
// frontend-next/components/journal/markdown-note-editor.tsx
type MarkdownNoteEditorProps = {
  initialMarkdown?: string;
  title?: string;
  helperText?: string;
  placeholder?: string;
  disabled?: boolean;
  onSave: (markdown: string) => Promise<void>;
};
```

- [ ] **Step 4: Refactor episode detail into a real review workspace**

```tsx
// frontend-next/app/(app)/journal/episodes/[episodeId]/page.tsx
const { selectedEnvironmentId } = useJournalWorkspace();
const environmentId = (typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("environment_id")?.trim() : "") || selectedEnvironmentId;

useEffect(() => {
  if (!environmentId) return;
  fetchJournalEpisode(params.episodeId, environmentId).then(setEpisode);
  fetchJournalTimeline(params.episodeId, environmentId).then(setTimeline);
  fetchJournalNotes({ environment_id: environmentId, episode_id: params.episodeId, subject_type: "episode", subject_id: params.episodeId, limit: 10 }).then((items) => setNotes(items));
}, [environmentId, params.episodeId]);

return (
  <div className="space-y-5 pb-5">
    <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />
    <Panel eyebrow="Episode detail" title={episode ? `Episode #${episode.episode_seq}` : `Episode ${params.episodeId}`}>
      {/* status badge, environment, context, opened/closed */}
    </Panel>
    <div className="grid gap-4 lg:grid-cols-2">
      <Panel eyebrow="Timeline" title="Episode activity"><EpisodeTimeline events={timeline} /></Panel>
      <Panel eyebrow="Notes" title="Episode note">
        <MarkdownNoteEditor
          title={activeNote?.title || `Episode ${episode?.episode_seq ?? params.episodeId} note`}
          initialMarkdown={activeNote?.body_markdown ?? ""}
          helperText={activeNote ? "Updates are revisioned on the backend." : "Create the first note for this episode."}
          onSave={handleSaveNote}
        />
      </Panel>
    </div>
    <Panel eyebrow="History" title="Note revisions">{/* latest revisions or empty state */}</Panel>
  </div>
);
```

- [ ] **Step 5: Run the page tests again**

Run:

```bash
cd frontend-next && npm test -- journal-v2-pages.test.tsx
```

Expected:

```text
PASS for episode detail page tests or fail only on analytics/notes page expectations from later tasks
```

- [ ] **Step 6: Commit**

```bash
git add frontend-next/app/(app)/journal/episodes/[episodeId]/page.tsx frontend-next/components/journal/markdown-note-editor.tsx frontend-next/lib/journal/api.ts frontend-next/lib/journal/types.ts frontend-next/tests/journal-v2-pages.test.tsx
git commit -m "feat: build journal episode review workspace"
```

## Task 5: Align analytics, strategies, notes, and unresolved pages to the shared workspace

**Files:**
- Modify: `frontend-next/app/(app)/journal/analytics/page.tsx`
- Modify: `frontend-next/app/(app)/journal/strategies/page.tsx`
- Modify: `frontend-next/app/(app)/journal/notes/page.tsx`
- Modify: `frontend-next/app/(app)/journal/unresolved/page.tsx`
- Test: `frontend-next/tests/journal-v2-pages.test.tsx`

- [ ] **Step 1: Write the failing tests for analytics and notes/unresolved integration**

```tsx
it("shows analytics page from shared environment selection without mixed totals", async () => {
  window.history.pushState({}, "", "/journal/analytics?environment_id=env-1");
  renderWithQueryClient(<JournalAnalyticsPage />);

  await waitFor(() => expect(fetchJournalV2AnalyticsSummaryMock).toHaveBeenCalledWith("env-1"));
  expect(screen.queryByText(/Combined P&L/i)).not.toBeInTheDocument();
  expect(screen.getByText(/Select a template plus explicit paper and live environments/i)).toBeInTheDocument();
});
```

```tsx
it("shows notes archive and unresolved queue from shared Journal environment", async () => {
  window.history.pushState({}, "", "/journal/notes?environment_id=env-1");
  fetchJournalNotesMock.mockResolvedValue([
    { id: "note-1", environment_id: "env-1", subject_type: "episode", subject_id: "ep-1", episode_id: "ep-1", note_type: "post_exit_review", title: "Review", body_markdown: "# Review", tags: [], updated_at: "2026-05-01T10:05:00Z" },
  ]);

  renderWithQueryClient(<JournalNotesPage />);

  await waitFor(() => expect(fetchJournalNotesMock).toHaveBeenCalled());
  expect(screen.getByText(/Notes archive/i)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```bash
cd frontend-next && npm test -- journal-v2-pages.test.tsx
```

Expected:

```text
FAIL because these pages still own their own environment selection and lack consistent V2 workspace behavior
```

- [ ] **Step 3: Refactor analytics and strategies pages to shared environment state**

```tsx
// frontend-next/app/(app)/journal/analytics/page.tsx
const { environments, selectedEnvironmentId } = useJournalWorkspace();

useEffect(() => {
  if (!selectedEnvironmentId) {
    setSummaryMetrics(null);
    setStrategyItems([]);
    return;
  }
  fetchJournalV2AnalyticsSummary(selectedEnvironmentId).then((payload) => setSummaryMetrics(payload.metrics));
  fetchJournalV2AnalyticsStrategies(selectedEnvironmentId).then((payload) => setStrategyItems(payload.items || []));
}, [selectedEnvironmentId]);
```

```tsx
// frontend-next/app/(app)/journal/strategies/page.tsx
const { selectedEnvironmentId, selectedEnvironment } = useJournalWorkspace();
```

- [ ] **Step 4: Refactor notes and unresolved pages into usable V2 tools**

```tsx
// frontend-next/app/(app)/journal/notes/page.tsx
const { selectedEnvironmentId } = useJournalWorkspace();

useEffect(() => {
  if (!selectedEnvironmentId) {
    setNotes([]);
    return;
  }
  fetchJournalNotes({ environment_id: selectedEnvironmentId, limit: 50 }).then(setNotes);
}, [selectedEnvironmentId]);

return (
  <div className="space-y-5 pb-5">
    <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />
    <Panel title="Notes archive">{/* note list with title, note type, updated_at, subject */}</Panel>
    <Panel title="Quick note capture">{/* environment-scoped note creation helper */}</Panel>
  </div>
);
```

```tsx
// frontend-next/app/(app)/journal/unresolved/page.tsx
const { selectedEnvironmentId } = useJournalWorkspace();

useEffect(() => {
  if (!selectedEnvironmentId) {
    setItems([]);
    return;
  }
  fetchJournalV2Unresolved(selectedEnvironmentId).then((payload) => setItems(payload.items || []));
}, [selectedEnvironmentId]);

return (
  <Panel title="Unresolved identity/activity queue">
    {/* reason, source, raw identity, candidate mapping summary, status */}
  </Panel>
);
```

- [ ] **Step 5: Run full Journal frontend verification**

Run:

```bash
cd frontend-next && npm run typecheck
cd frontend-next && npm test -- journal-v2-api.test.ts journal-v2-pages.test.tsx
```

Expected:

```text
typecheck passes
journal v2 frontend tests pass
```

- [ ] **Step 6: Commit**

```bash
git add frontend-next/app/(app)/journal/analytics/page.tsx frontend-next/app/(app)/journal/strategies/page.tsx frontend-next/app/(app)/journal/notes/page.tsx frontend-next/app/(app)/journal/unresolved/page.tsx frontend-next/tests/journal-v2-pages.test.tsx
git commit -m "feat: align journal workspace pages to backend v2 features"
```

---

## Plan self-review

### Spec coverage

- shared Journal environment state: Task 1
- V2 navigation alignment: Task 2
- overview as operational entry: Task 3
- episodes and detail as real workflow: Task 3 and Task 4
- analytics and strategies strengthening: Task 5
- notes and unresolved strengthening: Task 4 and Task 5
- note revisions path: Task 4
- backend environment-safety fidelity: Tasks 1–5

No spec gaps remain.

### Placeholder scan

- No `TBD`, `TODO`, or “implement later” placeholders remain.
- Each task includes concrete file paths, code direction, and verification commands.

### Type consistency

- shared environment state uses `selectedEnvironmentId`
- note revisions use `JournalNoteRevision`
- internal links preserve `environment_id`
- all page tasks are aligned to `useJournalWorkspace()`

No naming mismatches remain.
