# Frontend Next Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `frontend-next/` as a real Next.js foundation that implements the locked UI system, shared app shell, and reference-draft pages while leaving deeper product behavior open.

**Architecture:** Create a standalone App Router app with a shared provider layer, a reusable app shell, small composable operator UI primitives, and one route per draft module. Keep backend integration thin and typed. Implement the current mockups as reference pages first, then deepen behavior in follow-up plans.

**Tech Stack:** Next.js App Router, TypeScript, Tailwind CSS, shadcn/ui, React Query, Zustand, React Hook Form, Zod, TanStack Table, lightweight-charts, next-themes, Vitest, Testing Library.

---

## Scope check

The revised spec covers multiple product areas. To keep this plan executable, this plan covers:

- frontend scaffold
- locked design system
- shared shell
- reference pages for the current draft modules
- route-level smoke tests
- typed API/query skeletons

This plan does **not** fully implement live data behavior for options, alerts, charts, or custom display. Those should become follow-up plans once the foundation is running.

## File structure map

### New top-level app

- Create: `frontend-next/`
- Create: `frontend-next/package.json` — Next app scripts and dependencies
- Create: `frontend-next/components.json` — shadcn config
- Create: `frontend-next/src/app/` — App Router entry

### Shared app foundation

- Create: `frontend-next/src/app/globals.css` — locked tokens and global styles
- Create: `frontend-next/src/app/layout.tsx` — root HTML and providers mount
- Create: `frontend-next/src/app/(app)/layout.tsx` — shell wrapper for authenticated app routes
- Create: `frontend-next/src/components/providers/app-providers.tsx` — Query/theme/sonner providers
- Create: `frontend-next/src/lib/utils.ts` — `cn()` helper
- Create: `frontend-next/src/lib/query/client.ts` — shared QueryClient factory
- Create: `frontend-next/src/lib/api/client.ts` — typed fetch wrapper
- Create: `frontend-next/src/config/navigation.ts` — rail/top-level navigation config

### Shared UI primitives

- Create: `frontend-next/src/components/app-shell/app-shell.tsx`
- Create: `frontend-next/src/components/app-shell/left-rail.tsx`
- Create: `frontend-next/src/components/app-shell/top-bar.tsx`
- Create: `frontend-next/src/components/app-shell/bottom-dock.tsx`
- Create: `frontend-next/src/components/operator/panel.tsx`
- Create: `frontend-next/src/components/operator/kpi-card.tsx`
- Create: `frontend-next/src/components/operator/status-badge.tsx`
- Create: `frontend-next/src/components/operator/section-label.tsx`

### Routes

- Create: `frontend-next/src/app/(app)/dashboard/page.tsx`
- Create: `frontend-next/src/app/(app)/options/page.tsx`
- Create: `frontend-next/src/app/(app)/algos/page.tsx`
- Create: `frontend-next/src/app/(app)/alerts/page.tsx`
- Create: `frontend-next/src/app/(app)/screeners/page.tsx`
- Create: `frontend-next/src/app/(app)/paper/page.tsx`
- Create: `frontend-next/src/app/(app)/charts/page.tsx`
- Create: `frontend-next/src/app/(app)/custom-display/page.tsx`
- Create: `frontend-next/src/app/(app)/settings/page.tsx`

### Test files

- Create: `frontend-next/vitest.config.ts`
- Create: `frontend-next/src/test/setup.ts`
- Create: `frontend-next/src/app/(app)/__tests__/shell-layout.test.tsx`
- Create: `frontend-next/src/app/(app)/__tests__/reference-pages.test.tsx`

---

### Task 1: Scaffold the Next.js app and install the base stack

**Files:**
- Create: `frontend-next/` via `create-next-app`
- Modify: `frontend-next/package.json`
- Create: `frontend-next/components.json`

- [ ] **Step 1: Scaffold the app**

Run:

```bash
npm create next-app@latest frontend-next -- --ts --tailwind --eslint --app --src-dir --import-alias "@/*" --use-npm
```

Expected: a new `frontend-next/` folder with `src/app`, `next.config.*`, `tsconfig.json`, and Tailwind-enabled defaults.

- [ ] **Step 2: Install runtime dependencies**

Run:

```bash
npm install @tanstack/react-query zustand react-hook-form zod @hookform/resolvers lucide-react sonner cmdk clsx tailwind-merge date-fns @tanstack/react-table lightweight-charts next-themes
```

Expected: install completes without peer dependency errors.

- [ ] **Step 3: Install test dependencies**

Run:

```bash
npm install -D vitest jsdom @testing-library/react @testing-library/jest-dom @testing-library/user-event @vitejs/plugin-react typescript
```

Expected: Vitest + Testing Library are available for route smoke tests.

- [ ] **Step 4: Add useful scripts**

Update `frontend-next/package.json` scripts to include:

```json
{
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start",
    "lint": "next lint",
    "typecheck": "tsc --noEmit",
    "test": "vitest run",
    "test:watch": "vitest"
  }
}
```

- [ ] **Step 5: Initialize shadcn/ui**

Run:

```bash
npx shadcn@latest init -d
```

Use:

- style: `default`
- base color: `slate`
- CSS variables: `yes`
- tailwind css file: `src/app/globals.css`
- import alias: `@/components`, `@/lib/utils`

Expected: `components.json` is created and aliases are wired.

- [ ] **Step 6: Verify the scaffold**

Run:

```bash
npm run lint && npm run typecheck && npm run build
```

Expected: all three commands pass.

- [ ] **Step 7: Commit**

```bash
git add frontend-next
git commit -m "feat: scaffold frontend-next foundation"
```

---

### Task 2: Add locked design tokens, providers, and test harness

**Files:**
- Modify: `frontend-next/src/app/globals.css`
- Modify: `frontend-next/src/app/layout.tsx`
- Create: `frontend-next/src/components/providers/app-providers.tsx`
- Create: `frontend-next/src/lib/utils.ts`
- Create: `frontend-next/src/lib/query/client.ts`
- Create: `frontend-next/vitest.config.ts`
- Create: `frontend-next/src/test/setup.ts`
- Test: `frontend-next/src/app/(app)/__tests__/shell-layout.test.tsx`

- [ ] **Step 1: Add the locked tokens to `globals.css`**

Replace the generated globals with a token-first baseline like:

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

:root {
  --bg: #0f1117;
  --bg-soft: #161820;
  --panel: #1a1d27;
  --panel-hover: #1f2230;
  --border: #2a2d3a;
  --border-soft: #232636;
  --text: #e2e4ea;
  --muted: #8b8fa4;
  --dim: #5a5e72;
  --accent: #f97316;
  --accent-soft: rgba(249,115,22,.1);
  --accent-border: rgba(249,115,22,.3);
  --green: #34d399;
  --green-soft: rgba(52,211,153,.08);
  --red: #f87171;
  --red-soft: rgba(248,113,113,.08);
  --blue: #60a5fa;
  --blue-soft: rgba(96,165,250,.1);
  --blue-border: rgba(96,165,250,.25);
  --yellow: #fbbf24;
  --font-mono: 'JetBrains Mono','Fira Code','Cascadia Code',monospace;
}

html, body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-mono);
}

* { box-sizing: border-box; }
```

- [ ] **Step 2: Add the provider wrapper**

Create `src/components/providers/app-providers.tsx`:

```tsx
'use client'

import { ThemeProvider } from 'next-themes'
import { QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'sonner'
import { getQueryClient } from '@/lib/query/client'

export function AppProviders({ children }: { children: React.ReactNode }) {
  const queryClient = getQueryClient()

  return (
    <ThemeProvider attribute="class" forcedTheme="dark">
      <QueryClientProvider client={queryClient}>
        {children}
        <Toaster theme="dark" />
      </QueryClientProvider>
    </ThemeProvider>
  )
}
```

- [ ] **Step 3: Add the Query client and `cn` utility**

Create `src/lib/query/client.ts`:

```tsx
import { QueryClient } from '@tanstack/react-query'

let client: QueryClient | null = null

export function getQueryClient() {
  if (!client) {
    client = new QueryClient({
      defaultOptions: {
        queries: { refetchOnWindowFocus: false, retry: 1 },
        mutations: { retry: 0 },
      },
    })
  }
  return client
}
```

Create `src/lib/utils.ts`:

```tsx
import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
```

- [ ] **Step 4: Mount providers in the root layout**

Update `src/app/layout.tsx`:

```tsx
import './globals.css'
import { AppProviders } from '@/components/providers/app-providers'

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <AppProviders>{children}</AppProviders>
      </body>
    </html>
  )
}
```

- [ ] **Step 5: Add the test harness**

Create `vitest.config.ts`:

```ts
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  plugins: [react()],
  test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'] },
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
})
```

Create `src/test/setup.ts`:

```ts
import '@testing-library/jest-dom/vitest'
```

- [ ] **Step 6: Write the first smoke test**

Create `src/app/(app)/__tests__/shell-layout.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'

function TestScreen() {
  return <div>dashboard content</div>
}

test('sanity check for test harness', () => {
  render(<TestScreen />)
  expect(screen.getByText('dashboard content')).toBeInTheDocument()
})
```

- [ ] **Step 7: Verify**

Run:

```bash
npm run test && npm run lint && npm run typecheck
```

Expected: all commands pass.

- [ ] **Step 8: Commit**

```bash
git add frontend-next/src frontend-next/vitest.config.ts
git commit -m "feat: add providers tokens and test harness"
```

---

### Task 3: Build the shared app shell and navigation config

**Files:**
- Create: `frontend-next/src/config/navigation.ts`
- Create: `frontend-next/src/components/app-shell/app-shell.tsx`
- Create: `frontend-next/src/components/app-shell/left-rail.tsx`
- Create: `frontend-next/src/components/app-shell/top-bar.tsx`
- Create: `frontend-next/src/components/app-shell/bottom-dock.tsx`
- Create: `frontend-next/src/app/(app)/layout.tsx`
- Test: `frontend-next/src/app/(app)/__tests__/shell-layout.test.tsx`

- [ ] **Step 1: Define navigation config**

Create `src/config/navigation.ts`:

```ts
export const primaryNav = [
  { label: 'Dashboard', short: 'D', href: '/dashboard' },
  { label: 'Options', short: 'O', href: '/options' },
  { label: 'Algos', short: 'A', href: '/algos' },
  { label: 'Alerts', short: '!', href: '/alerts' },
  { label: 'Screeners', short: 'S', href: '/screeners' },
  { label: 'Paper', short: 'P', href: '/paper' },
  { label: 'Charts', short: 'C', href: '/charts' },
  { label: 'Display', short: 'X', href: '/custom-display' },
]
```

- [ ] **Step 2: Build shell primitives**

Create `src/components/app-shell/app-shell.tsx` with the main grid:

```tsx
import { LeftRail } from './left-rail'
import { TopBar } from './top-bar'
import { BottomDock } from './bottom-dock'

export function AppShell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="grid h-screen grid-cols-[48px_1fr] grid-rows-[40px_1fr_auto] bg-[var(--bg)] text-[var(--text)]">
      <LeftRail />
      <TopBar title={title} />
      <main className="overflow-auto p-3">{children}</main>
      <BottomDock />
    </div>
  )
}
```

Use matching visual rules in `left-rail.tsx`, `top-bar.tsx`, and `bottom-dock.tsx` from the approved mockups.

- [ ] **Step 3: Mount the shell in `(app)/layout.tsx`**

Create `src/app/(app)/layout.tsx`:

```tsx
import { AppShell } from '@/components/app-shell/app-shell'

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return <AppShell title="DASHBOARD">{children}</AppShell>
}
```

Then refactor `AppShell` to accept the page title from each route via a small wrapper component or page-level prop.

- [ ] **Step 4: Upgrade the shell test**

Replace the earlier test with:

```tsx
import { render, screen } from '@testing-library/react'
import { AppShell } from '@/components/app-shell/app-shell'

test('app shell renders shared operator chrome', () => {
  render(
    <AppShell title="DASHBOARD">
      <div>dashboard content</div>
    </AppShell>,
  )

  expect(screen.getByText('DASHBOARD')).toBeInTheDocument()
  expect(screen.getByPlaceholderText('⌘K  jump to anything...')).toBeInTheDocument()
  expect(screen.getByText('positions')).toBeInTheDocument()
})
```

- [ ] **Step 5: Verify**

Run:

```bash
npm run test && npm run lint && npm run typecheck
```

- [ ] **Step 6: Commit**

```bash
git add frontend-next/src/config frontend-next/src/components/app-shell frontend-next/src/app/(app)/layout.tsx frontend-next/src/app/(app)/__tests__/shell-layout.test.tsx
git commit -m "feat: add shared app shell for frontend-next"
```

---

### Task 4: Add reusable operator primitives and the typed API skeleton

**Files:**
- Create: `frontend-next/src/components/operator/panel.tsx`
- Create: `frontend-next/src/components/operator/kpi-card.tsx`
- Create: `frontend-next/src/components/operator/status-badge.tsx`
- Create: `frontend-next/src/components/operator/section-label.tsx`
- Create: `frontend-next/src/lib/api/client.ts`

- [ ] **Step 1: Add reusable primitives**

Create `panel.tsx`:

```tsx
import { cn } from '@/lib/utils'

export function Panel({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('rounded-[8px] border border-[var(--border)] bg-[var(--panel)]', className)} {...props} />
}
```

Create `kpi-card.tsx`:

```tsx
export function KpiCard({ label, value, valueClassName = '' }: { label: string; value: React.ReactNode; valueClassName?: string }) {
  return (
    <div className="rounded-[8px] border border-[var(--border)] bg-[var(--panel)] p-3">
      <div className="text-[9px] uppercase tracking-[0.08em] text-[var(--dim)]">{label}</div>
      <div className={`mt-1 text-sm font-bold ${valueClassName}`}>{value}</div>
    </div>
  )
}
```

- [ ] **Step 2: Add typed API wrapper**

Create `src/lib/api/client.ts`:

```ts
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })

  if (!res.ok) {
    throw new ApiError(res.status, `API request failed for ${path}`)
  }

  return res.json() as Promise<T>
}
```

- [ ] **Step 3: Verify**

Run:

```bash
npm run lint && npm run typecheck
```

- [ ] **Step 4: Commit**

```bash
git add frontend-next/src/components/operator frontend-next/src/lib/api/client.ts
git commit -m "feat: add operator ui primitives and api client"
```

---

### Task 5: Implement Dashboard and Options reference pages

**Files:**
- Create: `frontend-next/src/app/(app)/dashboard/page.tsx`
- Create: `frontend-next/src/app/(app)/options/page.tsx`
- Test: `frontend-next/src/app/(app)/__tests__/reference-pages.test.tsx`

- [ ] **Step 1: Build the Dashboard page**

Use the approved mockup as the source. Build:

- KPI strip
- active strategies table
- market panel
- activity feed
- quick actions

The page component should look like:

```tsx
import { KpiCard } from '@/components/operator/kpi-card'
import { Panel } from '@/components/operator/panel'

export default function DashboardPage() {
  return (
    <div className="flex flex-col gap-3">
      <section className="grid grid-cols-5 gap-2">
        <KpiCard label="Available" value="4,98,200" />
        <KpiCard label="Used margin" value="1,84,200" />
        <KpiCard label="Day P/L" value="+7,180" valueClassName="text-[var(--green)]" />
        <KpiCard label="Open positions" value="4" />
        <KpiCard label="Working orders" value="2" />
      </section>
      <Panel className="p-4">{/* active strategies table */}</Panel>
      <div className="grid grid-cols-2 gap-3">
        <Panel className="p-4">{/* market */}</Panel>
        <Panel className="p-4">{/* activity */}</Panel>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Build the Options page**

Implement the approved reference draft with:

- strategy builder header
- legs table
- payoff panel
- protection strip
- scrollable option chain with delta search

Keep the first pass static, using local arrays for rows.

- [ ] **Step 3: Write page smoke tests**

Create `src/app/(app)/__tests__/reference-pages.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import DashboardPage from '@/app/(app)/dashboard/page'
import OptionsPage from '@/app/(app)/options/page'

test('dashboard renders core panels', () => {
  render(<DashboardPage />)
  expect(screen.getByText('Available')).toBeInTheDocument()
  expect(screen.getByText('Active strategies')).toBeInTheDocument()
})

test('options page renders builder and chain', () => {
  render(<OptionsPage />)
  expect(screen.getByText('Protection')).toBeInTheDocument()
  expect(screen.getByText('Option chain')).toBeInTheDocument()
  expect(screen.getByDisplayValue('0.30')).toBeInTheDocument()
})
```

- [ ] **Step 4: Verify**

Run:

```bash
npm run test && npm run lint && npm run typecheck
```

- [ ] **Step 5: Commit**

```bash
git add frontend-next/src/app/(app)/dashboard frontend-next/src/app/(app)/options frontend-next/src/app/(app)/__tests__/reference-pages.test.tsx
git commit -m "feat: add dashboard and options reference pages"
```

---

### Task 6: Implement Algos, Alerts, and Screeners reference pages

**Files:**
- Create: `frontend-next/src/app/(app)/algos/page.tsx`
- Create: `frontend-next/src/app/(app)/alerts/page.tsx`
- Create: `frontend-next/src/app/(app)/screeners/page.tsx`
- Modify: `frontend-next/src/app/(app)/__tests__/reference-pages.test.tsx`

- [ ] **Step 1: Build the Algos page**

Implement:

- instance list split panel
- detail panel with KPIs
- log view with level badges
- paper account block in sidebar

- [ ] **Step 2: Build the Alerts page**

Implement:

- stats strip
- quick-create form
- active alerts table
- triggered history panel

- [ ] **Step 3: Build the Screeners page**

Implement:

- saved screeners sidebar
- filter builder rows
- quick stats strip
- results table with visual OI bars

- [ ] **Step 4: Extend tests**

Add:

```tsx
import AlgosPage from '@/app/(app)/algos/page'
import AlertsPage from '@/app/(app)/alerts/page'
import ScreenersPage from '@/app/(app)/screeners/page'

test('algos page renders instances and logs', () => {
  render(<AlgosPage />)
  expect(screen.getByText('Algo instances')).toBeInTheDocument()
  expect(screen.getByText('logs')).toBeInTheDocument()
})

test('alerts page renders quick create and active alerts', () => {
  render(<AlertsPage />)
  expect(screen.getByText('Quick create')).toBeInTheDocument()
  expect(screen.getByText('Active alerts')).toBeInTheDocument()
})

test('screeners page renders saved list and results', () => {
  render(<ScreenersPage />)
  expect(screen.getByText('Saved')).toBeInTheDocument()
  expect(screen.getByText('Filter conditions')).toBeInTheDocument()
})
```

- [ ] **Step 5: Verify**

Run:

```bash
npm run test && npm run lint && npm run typecheck
```

- [ ] **Step 6: Commit**

```bash
git add frontend-next/src/app/(app)/algos frontend-next/src/app/(app)/alerts frontend-next/src/app/(app)/screeners frontend-next/src/app/(app)/__tests__/reference-pages.test.tsx
git commit -m "feat: add algos alerts and screeners reference pages"
```

---

### Task 7: Implement Paper, Settings, Charts, and Custom Display reference pages

**Files:**
- Create: `frontend-next/src/app/(app)/paper/page.tsx`
- Create: `frontend-next/src/app/(app)/settings/page.tsx`
- Create: `frontend-next/src/app/(app)/charts/page.tsx`
- Create: `frontend-next/src/app/(app)/custom-display/page.tsx`
- Modify: `frontend-next/src/app/(app)/__tests__/reference-pages.test.tsx`

- [ ] **Step 1: Build the Paper page**

Implement:

- account selector cards
- metrics strip
- grouped positions table
- account controls row

- [ ] **Step 2: Build the Settings page**

Implement:

- settings nav sidebar
- trading defaults section
- protection defaults section
- session configuration section

- [ ] **Step 3: Build the Charts page**

Implement a reference page with:

- chart header with symbol/timeframe controls
- one `lightweight-charts` mount area placeholder component
- side panel for indicators/events/watch context

The first pass can use a client component with mocked candle data.

- [ ] **Step 4: Build the Custom Display page**

Implement a reference page with:

- saved layouts list
- main canvas area using fixed widgets (not drag/drop)
- widgets for market, alerts, positions, and chart preview

This should communicate the concept without locking the final builder model.

- [ ] **Step 5: Extend tests**

Add:

```tsx
import PaperPage from '@/app/(app)/paper/page'
import SettingsPage from '@/app/(app)/settings/page'
import ChartsPage from '@/app/(app)/charts/page'
import CustomDisplayPage from '@/app/(app)/custom-display/page'

test('paper page renders account metrics', () => {
  render(<PaperPage />)
  expect(screen.getByText('Balance')).toBeInTheDocument()
})

test('settings page renders trading defaults', () => {
  render(<SettingsPage />)
  expect(screen.getByText('Trading defaults')).toBeInTheDocument()
})

test('charts page renders chart controls', () => {
  render(<ChartsPage />)
  expect(screen.getByText('Timeframe')).toBeInTheDocument()
})

test('custom display page renders layouts and widgets', () => {
  render(<CustomDisplayPage />)
  expect(screen.getByText('Saved layouts')).toBeInTheDocument()
})
```

- [ ] **Step 6: Verify**

Run:

```bash
npm run test && npm run lint && npm run typecheck && npm run build
```

- [ ] **Step 7: Commit**

```bash
git add frontend-next/src/app/(app)/paper frontend-next/src/app/(app)/settings frontend-next/src/app/(app)/charts frontend-next/src/app/(app)/custom-display frontend-next/src/app/(app)/__tests__/reference-pages.test.tsx
git commit -m "feat: add remaining reference pages for foundation"
```

---

### Task 8: Final quality pass and handoff

**Files:**
- Modify: `frontend-next/README.md`
- Modify: `docs/superpowers/specs/2026-04-08-frontend-next-foundation-design.md` only if implementation drift requires note

- [ ] **Step 1: Add a local README for the new app**

Document:

- how to run `frontend-next`
- where locked tokens live
- where shell components live
- which routes are reference pages only
- which modules still need follow-up implementation plans

Suggested README sections:

```md
# frontend-next

## Commands
- npm install
- npm run dev
- npm run test
- npm run lint
- npm run typecheck
- npm run build

## Current status
- shared shell implemented
- reference pages implemented
- API/query skeleton implemented
- live data integrations still pending
```

- [ ] **Step 2: Run the final verification suite**

Run:

```bash
npm run test && npm run lint && npm run typecheck && npm run build
```

Expected: all commands pass with no warnings that block shipping the foundation.

- [ ] **Step 3: Commit**

```bash
git add frontend-next/README.md docs/superpowers/specs/2026-04-08-frontend-next-foundation-design.md
git commit -m "docs: add frontend-next foundation handoff notes"
```

---

## Self-review

### Spec coverage

- locked UI system -> Tasks 2, 3, 4
- shared shell -> Task 3
- options approved interactions as reference -> Task 5
- algos/alerts/screeners/paper/settings pages -> Tasks 6 and 7
- charts and custom display explicitly included -> Task 7
- typed API/query foundation -> Tasks 2 and 4

### Placeholder scan

- No `TODO` or `TBD` markers remain in task instructions.
- Charts and custom display are deliberately defined as reference implementations, not deferred placeholders.

### Type consistency

- Route names match the revised spec: `/custom-display`, not `/display`.
- Shared utilities use `@/` alias consistently.

## Follow-up plans after this one

After this foundation plan is executed, write separate plans for:

1. live backend integration for dashboard/paper/algos
2. options workspace real behavior and execution flows
3. alerts data model + edit/create flows
4. charts interactions and overlays
5. custom display persistence and layout editing

Plan complete and saved to `docs/superpowers/plans/2026-04-08-frontend-next-foundation-implementation.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
