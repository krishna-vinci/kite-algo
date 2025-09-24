<script lang="ts">
  import { onMount, createEventDispatcher } from 'svelte';
  import { toast } from '$lib/stores/toast';
  import {
    getAlerts,
    pauseAlert,
    resumeAlert,
    cancelAlert,
    reactivateAlert,
    deleteAlert,
    getUserSubscriptions
  } from '$lib/api';
  import type { Alert, ListAlertsResponse } from '$lib/types';
  import {
    Table,
    TableHeader,
    TableHead,
    TableRow,
    TableBody,
    TableCell,
    TableCaption
  } from '$lib/components/ui/table';
  import SkeletonLoader from '$lib/components/SkeletonLoader.svelte';
  import { marketwatch } from '$lib/stores/marketwatch';
  import { Pause, X, Play, RefreshCw, Copy, Trash2 } from 'lucide-svelte';

  let loading = false;
  let items: Alert[] = [];
  let total = 0;
  let limit = 50;
  let offset = 0;
  let sort: 'created_at' | '-created_at' | 'updated_at' | '-updated_at' = '-created_at';
  let statusFilter: string | undefined = undefined;
  let instrumentFilter: number | undefined = undefined;

  const limits = [20, 50, 100];

  function shortId(id: string) {
    return id?.slice(0, 8);
  }
  function cmpSymbol(c: string) {
    return c === 'gt' ? '>' : '<';
  }
  function statusClasses(s: string) {
    if (s === 'active') return 'bg-green-100 text-green-800';
    if (s === 'paused') return 'bg-amber-100 text-amber-800';
    if (s === 'canceled') return 'bg-gray-200 text-gray-800';
    if (s === 'triggered') return 'bg-red-100 text-red-800';
    return 'bg-slate-100 text-slate-800';
  }

  async function load() {
    loading = true;
    try {
      let token = undefined;
      if (instrumentFilter) {
        token = Number(instrumentFilter);
        if (isNaN(token)) {
          toast.error('Invalid instrument token');
          loading = false;
          return;
        }
      }
      const res: ListAlertsResponse = await getAlerts({
        status: statusFilter,
        instrument_token: undefined,
        instrument_name: token,
        limit,
        offset,
        sort
      });
      items = res.items ?? [];
      total = res.total ?? 0;

      // Warm symbol cache for visible rows so trading symbols show quickly
      try {
        for (const a of items) {
          const tok = Number(a?.instrument_token);
          if (Number.isFinite(tok) && !tokenToSymbol[tok] && a?.tradingsymbol) {
            void resolveSymbol(tok, a.tradingsymbol);
          }
        }
      } catch {
        // ignore
      }
    } catch (e: any) {
      toast.error(e?.message ?? 'Failed to load alerts');
    } finally {
      loading = false;
    }
  }

  // Expose refresh API to parent via bind:this
  export async function refresh() {
    await load();
  }

  function toggleCreatedSort() {
    sort = sort === '-created_at' ? 'created_at' : '-created_at';
    offset = 0;
    void load();
  }

  function nextPage() {
    const next = offset + limit;
    if (next < total) {
      offset = next;
      void load();
    }
  }
  function prevPage() {
    const prev = Math.max(0, offset - limit);
    if (prev !== offset) {
      offset = prev;
      void load();
    }
  }

  async function doAction(p: Promise<Alert>, successMsg: string) {
    try {
      await p;
      toast.success(successMsg);
      await load();
    } catch (e: any) {
      toast.error(e?.message ?? 'Operation failed');
    }
  }

  function onPause(a: Alert) {
    return doAction(pauseAlert(a.id), 'Alert paused');
  }
  function onResume(a: Alert) {
    return doAction(resumeAlert(a.id), 'Alert resumed');
  }
  function onCancel(a: Alert) {
    return doAction(cancelAlert(a.id), 'Alert canceled');
  }
  function onReactivate(a: Alert) {
    return doAction(reactivateAlert(a.id), 'Alert reactivated');
  }
  function onDelete(a: Alert) {
    if (!confirm('Permanently delete this alert? This cannot be undone.')) return;
    return doAction(deleteAlert(a.id, true), 'Alert deleted');
  }

  $: pageFrom = total === 0 ? 0 : offset + 1;
  $: pageTo = Math.min(total, offset + limit);

  const dispatch = createEventDispatcher();
  let tokenToSymbol: Record<number, string> = {};

  // Resolve trading symbol for a token with a tiny on-demand cache:
  // 1) cache hit
  // 2) try live marketwatch tick fields (if any has tradingsymbol/symbol)
  // 3) lazy fetch from instruments API by token and cache
  function symbolForToken(t: number): string {
    const tok = Number(t);
    if (tokenToSymbol[tok]) return tokenToSymbol[tok];

    // Try live tick for free (may not have symbol fields)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const mw: any = $marketwatch;
    const tick = mw?.instruments?.[tok];
    const fromMw = tick?.tradingsymbol || tick?.symbol || tick?.id;
    if (fromMw && typeof fromMw === 'string') {
      tokenToSymbol = { ...tokenToSymbol, [tok]: fromMw };
      return fromMw;
    }

    // Kick off a fire-and-forget resolve; UI shows TOKEN <tok> until it returns
    void resolveSymbol(tok, a?.tradingsymbol);
    return `TOKEN ${tok}`;
  }

  async function resolveSymbol(tok: number, tradingSymbol: string) {
    try {
      // Minimal, robust resolution:
      // 1) Try a direct endpoint if available
      let sym: string | null = null;

      // Attempt 1: backend may support by-token lookup (gracefully ignore failures)
      try {
        const res = await apiFetch(`/broker/instruments/by-token?token=${encodeURIComponent(tok)}`);
        if (res.ok) {
          const data = await res.json();
          const s = data?.tradingsymbol ?? data?.symbol ?? data?.data?.tradingsymbol ?? null;
          if (s && typeof s === 'string') sym = s;
        }
      } catch {
        // ignore
      }

      // Attempt 2: fallback to fuzzy-search with the token string
      if (!sym) {
        const res = await apiFetch(`/broker/instruments/fuzzy-search?query=${encodeURIComponent(tradingSymbol)}`);
        if (res.ok) {
          const list = (await res.json()) as Array<{ instrument_token?: number; tradingsymbol?: string; symbol?: string }>;
          const hit = Array.isArray(list) ? list.find(r => Number(r.instrument_token) === tok) ?? list[0] : null;
          const s = hit?.tradingsymbol ?? hit?.symbol ?? null;
          if (s && typeof s === 'string') sym = s;
        }
      }

      if (sym) {
        tokenToSymbol = { ...tokenToSymbol, [tok]: sym };
      }
    } catch {
      // silent
    }
  }

  async function preloadSymbols() {
    try {
      const scopes: Array<'sidebar' | 'marketwatch'> = ['sidebar', 'marketwatch'];
      for (const scope of scopes) {
        const data = await getUserSubscriptions(scope as any).catch(() => null);
        const arr = Array.isArray((data as any)?.items) ? (data as any).items : (Array.isArray(data) ? data : []);
        for (const it of arr) {
          const tok = Number(it?.instrument_token ?? it?.token ?? it?.instrumentToken);
          const sym = it?.tradingsymbol ?? it?.symbol ?? it?.tradingSymbol ?? it?.id;
          if (Number.isFinite(tok) && typeof sym === 'string') {
            tokenToSymbol[tok] = sym;
          }
        }
      }
    } catch {
      // noop
    }
  }

  onMount(() => {
    load();
    void preloadSymbols();
  });
</script>

<section class="space-y-3">
  <div class="flex flex-wrap items-center justify-between gap-2">
    <div class="flex items-center gap-2">
      <label class="text-sm text-gray-600" for="alerts_status">Status</label>
      <select id="alerts_status" class="input h-9" bind:value={statusFilter} on:change={() => { offset = 0; load(); }}>
        <option value={undefined}>All</option>
        <option value="active">active</option>
        <option value="paused">paused</option>
        <option value="canceled">canceled</option>
        <option value="triggered">triggered</option>
      </select>

      
            <label class="text-sm text-gray-600" for="alerts_token">Symbol</label>
            <input
              id="alerts_token"
              class="input h-9 w-36"
              type="text"
              bind:value={instrumentFilter}
              placeholder="tradingsymbol"
              on:input={() => { offset = 0; load(); }}
              on:change={() => { offset = 0; load(); }}
            />
      <label class="text-sm text-gray-600" for="alerts_limit">Limit</label>
      <select id="alerts_limit" class="input h-9" bind:value={limit} on:change={() => { offset = 0; load(); }}>
        {#each limits as l}
          <option value={l}>{l}</option>
        {/each}
      </select>
    </div>

    <div class="flex items-center gap-2">
      <button class="btn btn-secondary" on:click={() => { offset = 0; load(); }} disabled={loading}>
        {loading ? 'Loading…' : 'Refresh'}
      </button>
    </div>
  </div>

  <div class="rounded-lg border bg-white">
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Symbol</TableHead>
          <TableHead>Comparator</TableHead>
          <TableHead>Target Price</TableHead>
          <TableHead>Initial Price</TableHead>
          <TableHead>
            <button class="underline" on:click={toggleCreatedSort} title="Toggle sort">
              Created At {sort === '-created_at' ? '↓' : sort === 'created_at' ? '↑' : ''}
            </button>
          </TableHead>
          <TableHead>Status</TableHead>
          <TableHead class="text-right">Actions</TableHead>
        </TableRow>
      </TableHeader>

      <TableBody>
        {#if loading}
          {#each Array.from({ length: 5 }) as _, i}
            <TableRow>
              {#each Array.from({ length: 7 }) as __}
                <TableCell><SkeletonLoader height="1rem" /></TableCell>
              {/each}
            </TableRow>
          {/each}
        {:else if items.length === 0}
          <TableRow>
            <TableCell colspan={7}>
              <div class="p-6 text-sm text-gray-600">No alerts found.</div>
            </TableCell>
          </TableRow>
        {:else}
          {#each items as a}
            <TableRow>
              <TableCell>{a.tradingsymbol || (a.instrument_token ? symbolForToken(a.instrument_token) : '-')}</TableCell>
              <TableCell>{@html cmpSymbol(a.comparator)}</TableCell>
              <TableCell>{a.absolute_target ?? '-'}</TableCell>
              <TableCell>{a.baseline_price ?? '-'}</TableCell>
              <TableCell class="whitespace-nowrap">{new Date(a.created_at).toLocaleString()}</TableCell>
              <TableCell>
                <span class={"inline-block rounded-full px-2 py-0.5 text-xs " + statusClasses(a.status)}>{a.status}</span>
              </TableCell>
              <TableCell class="text-right">
                <div class="flex justify-end gap-2">
                  {#if a.status === 'active'}
                    <button class="btn btn-xs" on:click={() => onPause(a)} title="Pause"><Pause class="h-4 w-4" /></button>
                    <button class="btn btn-xs" on:click={() => onCancel(a)} title="Cancel"><X class="h-4 w-4" /></button>
                  {:else if a.status === 'paused'}
                    <button class="btn btn-xs" on:click={() => onResume(a)} title="Resume"><Play class="h-4 w-4" /></button>
                    <button class="btn btn-xs" on:click={() => onCancel(a)} title="Cancel"><X class="h-4 w-4" /></button>
                  {/if}
                  {#if a.status === 'triggered' || a.status === 'canceled' || a.status === 'paused'}
                    <button class="btn btn-xs" on:click={() => onReactivate(a)} title="Reactivate"><RefreshCw class="h-4 w-4" /></button>
                  {/if}
                  <button class="btn btn-xs" on:click={() => dispatch('recreate', { alert: a })} title="Recreate"><Copy class="h-4 w-4" /></button>
                  <button class="btn btn-xs btn-danger" on:click={() => onDelete(a)} title="Delete"><Trash2 class="h-4 w-4" /></button>
                </div>
              </TableCell>
            </TableRow>
          {/each}
        {/if}
      </TableBody>

      <TableCaption class="text-left">
        <div class="flex items-center justify-between py-2">
          <div class="text-sm text-gray-600">Showing {pageFrom}–{pageTo} of {total}</div>
          <div class="flex items-center gap-2">
            <button class="btn btn-secondary btn-sm" on:click={prevPage} disabled={offset === 0}>Prev</button>
            <button class="btn btn-secondary btn-sm" on:click={nextPage} disabled={offset + limit >= total}>Next</button>
          </div>
        </div>
      </TableCaption>
    </Table>
  </div>
</section>

<style>
  @reference "tailwindcss";
  .btn {
    @apply px-3 py-1.5 rounded-md font-medium transition-colors duration-150;
  }
  .btn-sm {
    @apply text-sm;
  }
  .btn-xs {
    @apply text-xs px-2 py-1 border border-gray-300 bg-white hover:bg-gray-50;
  }
  .btn-danger {
    @apply border-red-300 text-red-700 hover:bg-red-50;
  }
  .btn-secondary {
    @apply bg-gray-200 text-gray-800 hover:bg-gray-300 disabled:opacity-50;
  }
  .input {
    @apply border border-gray-300 rounded-md px-2 py-1 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-shadow duration-200;
  }
</style>
