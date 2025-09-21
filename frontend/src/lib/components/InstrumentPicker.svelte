<script lang="ts">
	import { createEventDispatcher, onMount } from 'svelte';
	import { apiFetch } from '$lib/api';

	export type InstrumentRow = {
		instrument_token: number;
		tradingsymbol: string;
		name?: string;
		exchange: string;
		instrument_type?: string;
		segment?: string;
		underlying?: string;
		option_type?: 'CE' | 'PE';
		expiry?: string;
		strike?: number;
	};

  function formatExpiry(x?: string | null) {
    if (!x) return '';
    const d = new Date(x);
    if (isNaN(d.getTime())) return x;
    return d.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: 'numeric' });
  }
  function formatLabel(item: any) {
    const exch = item.exchange ? ` (${item.exchange})` : '';
    if (item.option_type) {
      const u = item.underlying ?? '';
      const exp = item.expiry_label ?? formatExpiry(item.expiry);
      const strike = item.strike ?? '';
      const ot = item.option_type ?? '';
      return `${u} ${exp} ${strike} ${ot}${exch}`.trim();
    }
    if ((item.instrument_type || '').toUpperCase() === 'FUT') {
      const u = item.underlying ?? '';
      const exp = formatExpiry(item.expiry);
      return `${u} ${exp} FUT${exch}`.trim();
    }
    const name = item.name ? ` — ${item.name}` : '';
    return `${item.tradingsymbol}${name}${exch}`.trim();
  }

	export let selected: InstrumentRow | null = null;
	export let placeholder = 'Search eg: infy bse, nifty fut, index';
	export let disabled = false;

	const dispatch = createEventDispatcher();

	let query = '';
	let open = false;
	let loading = false;
	let results: InstrumentRow[] = [];
	let topDefaults: InstrumentRow[] = [];
	let focusedIndex = -1;
	let inputEl: HTMLInputElement | null = null;

	onMount(async () => {
		try {
			const res = await apiFetch('/broker/instruments/top-defaults');
			if (res.ok) {
				const data = await res.json();
				topDefaults = data?.data ?? [];
			}
		} catch {
			// ignore
		}
	});

	let timer: any;
	function onInput(e: Event) {
		const t = e.target as HTMLInputElement;
		query = t.value;
		open = true;
		clearTimeout(timer);
		timer = setTimeout(runSearch, 200);
	}

	async function runSearch() {
		if (!query.trim()) {
			results = [];
			return;
		}
		loading = true;
		try {
			const url = `/broker/instruments/fuzzy-search?query=${encodeURIComponent(query)}`;
			const res = await apiFetch(url);
			if (res.ok) {
				results = await res.json();
			} else {
				results = [];
			}
		} catch {
			results = [];
		} finally {
			loading = false;
		}
	}

	function choose(item: InstrumentRow) {
		selected = item;
		query = `${item.tradingsymbol}`;
		open = false;
		results = [];
		focusedIndex = -1;
		dispatch('select', { instrument: item });
	}

	function onFocus() {
		open = true;
	}
	function onBlur() {
		// allow click to register
		setTimeout(() => (open = false), 150);
	}

	function keydown(ev: KeyboardEvent) {
		if (!open) return;
		const list = results.length ? results : topDefaults;
		if (!list.length) return;

		if (ev.key === 'ArrowDown') {
			focusedIndex = (focusedIndex + 1) % list.length;
			ev.preventDefault();
		} else if (ev.key === 'ArrowUp') {
			focusedIndex = (focusedIndex - 1 + list.length) % list.length;
			ev.preventDefault();
		} else if (ev.key === 'Enter' && list[focusedIndex]) {
			choose(list[focusedIndex]);
			ev.preventDefault();
		}
	}
</script>

<div class="picker">
	<input
		class="input"
		bind:this={inputEl}
		{placeholder}
		{disabled}
		value={selected ? `${selected.tradingsymbol}` : query}
		on:input={onInput}
		on:focus={onFocus}
		on:blur={onBlur}
		on:keydown={keydown}
	/>

	{#if open}
		<div class="dropdown">
			{#if loading}
				<div class="empty">Searching…</div>
			{:else if (results.length ? results : topDefaults).length === 0}
				<div class="empty">No matches</div>
			{:else}
				<ul class="results">
					{#each results.length ? results : topDefaults as r, i}
						<li
							class="row"
							class:focused={i === focusedIndex}
							on:mousedown|preventDefault={() => choose(r)}
						>
							<div class="ts">{formatLabel(r)}</div>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	{/if}
</div>

<style>
	.picker {
		position: relative;
	}
	.input {
		@apply border rounded px-2 py-1 w-full;
	}
	.dropdown {
		position: absolute;
		z-index: 40;
		left: 0;
		right: 0;
		margin-top: 4px;
		@apply bg-white border rounded shadow;
		max-height: 18rem;
		overflow: auto;
	}
	.empty {
		@apply text-sm text-gray-500 p-3;
	}
	.results {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.row {
		@apply px-3 py-2 cursor-pointer hover:bg-gray-50;
	}
	.row.focused {
		@apply bg-gray-100;
	}
	.ts {
		@apply font-medium;
	}
	.meta {
		@apply text-xs text-gray-600 flex gap-2 items-center mt-0.5;
	}
	.badge {
		@apply inline-block text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-gray-100 text-gray-700;
	}
	.badge.tone {
		@apply bg-blue-50 text-blue-700;
	}
	.badge.tone2 {
		@apply bg-purple-50 text-purple-700;
	}
</style>
