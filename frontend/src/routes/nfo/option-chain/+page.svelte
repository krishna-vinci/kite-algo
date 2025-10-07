<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { browser } from '$app/environment';
  import {
    getOptionsWatchlist,
    startOptionsSessions,
    getOptionsSnapshot,
    stopOptionsSession,
    buildOptionsSessionSseUrl
  } from '$lib/api';
  import type {
    WatchlistItem,
    OptionsSessionSnapshot,
    SessionRequestItem,
    ErrorResponse,
    InstrumentRow
  } from '$lib/types';
  import InstrumentPicker from '$lib/components/InstrumentPicker.svelte';

  let underlyings = ['NIFTY', 'BANKNIFTY'];
  let underlying: string = underlyings[0];
  let replaceWatchlist = false;
  let cadenceSec = 5;

  let watchlist: WatchlistItem[] = [];
  let loadingWatchlist = false;

  let snapshot: OptionsSessionSnapshot | null = null;
  let loadingSnapshot = false;
  let snapshotError: string | null = null;

  let selectedExpiry: string | null = null;

  let eventSource: EventSource | null = null;
  let currentStreamUnderlying: string | null = null;
  let lastCadenceByUnderlying = new Map<string, number>();

  let showInstrumentPicker = false;

  async function handleInstrumentSelect(e: CustomEvent<InstrumentRow>) {
    const instrument = e.detail;
    if (!instrument) return;

    const newUnderlying = instrument.tradingsymbol;
    showInstrumentPicker = false;

    if (!underlyings.includes(newUnderlying)) {
      underlyings = [...underlyings, newUnderlying];
      try {
        // Start the new session without replacing existing ones
        await startOptionsSessions([{ underlying: newUnderlying, cadence_sec: cadenceSec }], false);
        await refreshWatchlist();
      } catch (err) {
        console.error(`Failed to start session for ${newUnderlying}`, err);
        // Optionally remove from list if start fails
        underlyings = underlyings.filter(u => u !== newUnderlying);
      }
    }
    
    // Switch view to the newly selected instrument
    setUnderlying(newUnderlying);
  }

  async function refreshWatchlist() {
    loadingWatchlist = true;
    try {
      watchlist = await getOptionsWatchlist();
    } catch (e) {
      console.error('Failed to fetch watchlist', e);
    } finally {
      loadingWatchlist = false;
    }
  }

  async function ensureSnapshot(u: string) {
    loadingSnapshot = true;
    snapshotError = null;
    try {
      const data = await getOptionsSnapshot(u);
      snapshot = data;
      if (data.expiries?.length) {
        selectedExpiry = selectedExpiry && data.expiries.includes(selectedExpiry)
          ? selectedExpiry
          : data.expiries[0];
      } else {
        selectedExpiry = null;
      }
    } catch (e: any) {
      const code = e?.code;
      if (code === 'OPTION_SESSION_NOT_FOUND') {
        snapshot = null;
        snapshotError = `No active session for ${u}. Start a session to begin streaming.`;
      } else {
        console.error('Snapshot error', e);
        snapshotError = 'Failed to load snapshot';
      }
    } finally {
      loadingSnapshot = false;
    }
  }

  function connectSSE(u: string) {
    if (!browser || currentStreamUnderlying === u) return;
    closeSSE();

    const url = buildOptionsSessionSseUrl(u);
    eventSource = new EventSource(url);
    currentStreamUnderlying = u;

    eventSource.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        snapshot = data;
        if (data?.expiries?.length) {
          if (!selectedExpiry || !data.expiries.includes(selectedExpiry)) {
            selectedExpiry = data.expiries[0];
          }
        }
      } catch (err) {
        console.error('SSE parse error', err);
      }
    };

    eventSource.onerror = (ev) => {
      console.error('SSE error', ev);
      // EventSource will automatically try to reconnect.
    };
  }

  function closeSSE() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    currentStreamUnderlying = null;
  }

  async function onStartSession() {
    const items: SessionRequestItem[] = [{ underlying, window: 12, cadence_sec: cadenceSec }];
    try {
      await startOptionsSessions(items, replaceWatchlist);
      await refreshWatchlist();
      await ensureSnapshot(underlying);
      connectSSE(underlying);
    } catch (e) {
      console.error('Start session failed', e);
    }
  }

  async function onStopSession(u: string) {
    try {
      await stopOptionsSession(u);
      if (u === underlying) {
        closeSSE();
        snapshot = null;
        selectedExpiry = null;
      }
      await refreshWatchlist();
    } catch (e) {
      console.error('Stop session failed', e);
    }
  }

  async function setUnderlying(u: string) {
    underlying = u;
    // Reactive guard will handle SSE connection. We just ensure snapshot is loaded.
    await ensureSnapshot(u);
  }

  onMount(async () => {
    // On initial load, just fetch the watchlist and a static snapshot if a session is running.
    // Do not auto-connect the stream.
    await refreshWatchlist();
    const runningSession = watchlist.find(item => item.underlying === underlying && item.is_running);
    if (runningSession) {
      await ensureSnapshot(underlying);
      connectSSE(underlying);
    }
  });

  onDestroy(() => {
    closeSSE();
  });

  $: currentExpiryRows =
    snapshot && selectedExpiry ? snapshot.per_expiry?.[selectedExpiry]?.rows ?? [] : [];

  $: currentExpiryData = snapshot && selectedExpiry ? snapshot.per_expiry?.[selectedExpiry] : null;
  $: atmStrike = currentExpiryData?.atm_strike;
  $: isSessionRunning = watchlist.find(item => item.underlying === underlying)?.is_running ?? false;

  // Reactive statement to auto-update cadence, debounced to avoid churn.
  $: if (browser && isSessionRunning && underlying && cadenceSec > 0) {
    const prev = lastCadenceByUnderlying.get(underlying);
    if (prev !== cadenceSec) {
      lastCadenceByUnderlying.set(underlying, cadenceSec);
      startOptionsSessions([{ underlying, cadence_sec: cadenceSec }], false)
        .catch(err => console.error('Auto-update cadence failed', err));
    }
  }

  // Reactive guard to manage SSE connection lifecycle.
  // Connects when switching to a running session, disconnects when session stops.
  $: if (browser && underlying && isSessionRunning) {
    if (currentStreamUnderlying !== underlying) {
      connectSSE(underlying);
    }
  } else {
    if (currentStreamUnderlying) {
      closeSSE();
    }
  }

  function formatOi(oi: number | null | undefined) {
    if (oi === null || oi === undefined) return '-';
    return (oi / 100000).toFixed(1) + 'L';
  }
</script>

{#if showInstrumentPicker}
  <InstrumentPicker
    on:close={() => showInstrumentPicker = false}
    on:select={handleInstrumentSelect}
  />
{/if}

<div class="container mx-auto p-4 space-y-4 text-sm text-gray-800 dark:text-gray-200">
  <header class="flex flex-wrap items-center gap-4 p-2 bg-gray-100 dark:bg-slate-800 rounded">
    <div class="flex items-center gap-2">
      <select class="bg-white dark:bg-slate-700 border border-gray-300 dark:border-slate-600 rounded px-3 py-1.5"
        bind:value={underlying}
        on:change={(e) => setUnderlying((e.target as HTMLSelectElement).value)}>
        {#each underlyings as sym}
          <option value={sym}>{sym}</option>
        {/each}
      </select>
      <button class="px-2 py-1.5 rounded bg-gray-200 dark:bg-slate-700 hover:bg-gray-300 dark:hover:bg-slate-600" on:click={() => showInstrumentPicker = true}>
        + Add
      </button>
      {#if snapshot?.spot_ltp}
        <span class="ml-2 font-semibold text-green-600 dark:text-green-400">{snapshot.spot_ltp.toFixed(2)}</span>
      {/if}
    </div>

    <div>
      <label>
        Expiry:
        <select class="ml-2 bg-white dark:bg-slate-700 border border-gray-300 dark:border-slate-600 rounded px-3 py-1.5"
          bind:value={selectedExpiry}>
          {#if snapshot?.expiries?.length}
            {#each snapshot.expiries as exp}
              <option value={exp}>{new Date(exp + 'T00:00:00').toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })}</option>
            {/each}
          {/if}
        </select>
      </label>
    </div>

    {#if currentExpiryData?.forward}
      <div class="font-mono">
        Synth Fut: <span class="font-bold">{currentExpiryData.forward.toFixed(2)}</span>
      </div>
    {/if}

    <div class="flex-grow"></div>

    <div class="flex items-center gap-4">
        <label class="flex items-center gap-2">
            Cadence (s):
            <input type="number" bind:value={cadenceSec} class="w-16 bg-white dark:bg-slate-700 border border-gray-300 dark:border-slate-600 rounded px-2 py-1" />
        </label>
        <label class="flex items-center gap-2">
            <input type="checkbox" bind:checked={replaceWatchlist} />
            Replace
        </label>
        {#if !isSessionRunning}
          <button class="px-4 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700" on:click={onStartSession}>
              Start
          </button>
        {/if}
        <button class="px-4 py-1.5 rounded bg-rose-600 text-white hover:bg-rose-700" on:click={() => onStopSession(underlying)}>
            Stop
        </button>
    </div>
  </header>

  {#if loadingSnapshot}
    <p>Loading snapshot…</p>
  {:else if snapshotError}
    <div class="p-4 bg-yellow-100 dark:bg-yellow-900/50 text-yellow-800 dark:text-yellow-200 rounded text-center">
      {snapshotError}
      <div class="mt-4">
        <button class="px-4 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700" on:click={onStartSession}>
          Start Session for {underlying}
        </button>
      </div>
    </div>
  {:else if !snapshot}
    <p class="text-center opacity-70">No data available. Start a session to see the option chain.</p>
  {:else}
    <div class="grid grid-cols-[1fr_auto_1fr] gap-1 bg-gray-200 dark:bg-slate-800 p-2 rounded-lg">
      <!-- Headers -->
      <div class="p-2 text-center font-bold text-red-600 dark:text-red-400 col-span-1">CALLS</div>
      <div class="p-2 text-center font-bold col-span-1">STRIKE</div>
      <div class="p-2 text-center font-bold text-green-600 dark:text-green-400 col-span-1">PUTS</div>

      <!-- Sub-headers -->
      <div class="grid grid-cols-6 gap-2 text-xs text-gray-500 dark:text-gray-400 text-right">
        <span class="text-left">Gamma</span>
        <span>Vega</span>
        <span>Theta</span>
        <span>Delta</span>
        <span>OI</span>
        <span>LTP</span>
      </div>
      <div />
      <div class="grid grid-cols-6 gap-2 text-xs text-gray-500 dark:text-gray-400 text-right">
        <span class="text-left">LTP</span>
        <span>OI</span>
        <span>Delta</span>
        <span>Theta</span>
        <span>Vega</span>
        <span>Gamma</span>
      </div>

      <!-- Chain Data -->
      {#each [...currentExpiryRows].sort((a, b) => a.strike - b.strike) as row (row.strike)}
        {@const isAtm = row.strike === atmStrike}
        <!-- Call Row -->
        <div class="grid grid-cols-6 gap-2 text-right p-1.5 rounded-l-md" class:bg-amber-100={isAtm} class:dark:bg-amber-900={isAtm}>
          <span class="text-left">{row.CE?.gamma?.toFixed(4) ?? '-'}</span>
          <span>{row.CE?.vega?.toFixed(2) ?? '-'}</span>
          <span>{row.CE?.theta?.toFixed(2) ?? '-'}</span>
          <span>{row.CE?.delta?.toFixed(2) ?? '-'}</span>
          <span>{formatOi(row.CE?.oi)}</span>
          <span class="font-semibold">{row.CE?.ltp?.toFixed(2) ?? '-'}</span>
        </div>

        <!-- Strike Price -->
        <div class="flex items-center justify-center font-bold p-1.5" class:bg-amber-300={isAtm} class:dark:bg-amber-500={isAtm} class:text-slate-900={isAtm}>
          {row.strike}
        </div>

        <!-- Put Row -->
        <div class="grid grid-cols-6 gap-2 text-right p-1.5 rounded-r-md" class:bg-amber-100={isAtm} class:dark:bg-amber-900={isAtm}>
          <span class="text-left font-semibold">{row.PE?.ltp?.toFixed(2) ?? '-'}</span>
          <span>{formatOi(row.PE?.oi)}</span>
          <span>{row.PE?.delta?.toFixed(2) ?? '-'}</span>
          <span>{row.PE?.theta?.toFixed(2) ?? '-'}</span>
          <span>{row.PE?.vega?.toFixed(2) ?? '-'}</span>
          <span>{row.PE?.gamma?.toFixed(4) ?? '-'}</span>
        </div>
      {/each}
    </div>
    <div class="text-xs opacity-70 mt-2 text-center">
      Updated at: {new Date(snapshot.updated_at).toLocaleString()}
      {#if snapshot.spot_ltp !== null}
        • Spot: {snapshot.spot_ltp}
      {/if}
      • Cadence: {snapshot.cadence_sec}s
    </div>
  {/if}

  <section class="bg-gray-100 dark:bg-slate-800 border border-gray-200 dark:border-slate-700 rounded p-3 mt-4">
    <h2 class="font-semibold mb-2">Watchlist</h2>
    {#if loadingWatchlist}
      <p>Loading watchlist…</p>
    {:else if watchlist.length === 0}
      <p class="text-sm opacity-80">No active sessions.</p>
    {:else}
      <ul class="text-sm grid gap-2">
        {#each watchlist as item}
          <li class="flex items-center justify-between bg-white dark:bg-slate-700 rounded px-3 py-2">
            <span>
              <strong>{item.underlying}</strong>
              <span class:text-green-500={item.is_running} class:text-red-500={!item.is_running} class="ml-3">
                {item.is_running ? '● Running' : '○ Stopped'}
              </span>
              <span class="ml-3 opacity-80">({item.desired_tokens} tokens)</span>
            </span>
            <div class="flex items-center gap-2">
              <button class="px-3 py-1 rounded bg-gray-200 dark:bg-slate-600 hover:bg-gray-300 dark:hover:bg-slate-500"
                on:click={() => setUnderlying(item.underlying)}>
                View
              </button>
              <button class="px-3 py-1 rounded bg-rose-600 text-white hover:bg-rose-500"
                on:click={() => onStopSession(item.underlying)}>
                Stop
              </button>
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
</div>