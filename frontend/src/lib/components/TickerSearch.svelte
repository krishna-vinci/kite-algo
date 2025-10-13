<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { getApiBase } from '$lib/api';
	import type { InstrumentRow } from '$lib/types';

	let {
		value = null,
		placeholder = 'Search instrument...',
		maxResults = 5
	}: {
		value?: InstrumentRow | null;
		placeholder?: string;
		maxResults?: number;
	} = $props();

	const dispatch = createEventDispatcher<{
		select: InstrumentRow;
	}>();

	let query = $state('');
	let results = $state<InstrumentRow[]>([]);
	let open = $state(false);
	let loading = $state(false);
	let focusedIndex = $state(-1);

	const formatLabel = (item: InstrumentRow) => {
		return `${item.exchange}:${item.tradingsymbol} (token: ${item.instrument_token})`;
	};

	$effect(() => {
		if (value) {
			query = formatLabel(value);
		} else {
			query = '';
		}
	});

	let searchTimeout: ReturnType<typeof setTimeout>;
	$effect(() => {
		const currentQuery = query;
		clearTimeout(searchTimeout);

		if (currentQuery.length < 2 || (value && formatLabel(value) === currentQuery)) {
			results = [];
			open = false;
			return;
		}

		loading = true;
		searchTimeout = setTimeout(async () => {
			try {
				const url = `${getApiBase()}/broker/instruments/fuzzy-search?query=${encodeURIComponent(
					currentQuery
				)}`;
				const response = await fetch(url, { credentials: 'include' });
				if (response.ok) {
					const data = await response.json();
					results = data.slice(0, maxResults);
					open = true;
				} else {
					results = [];
				}
			} catch (error) {
				results = [];
			} finally {
				loading = false;
			}
		}, 300);

		return () => clearTimeout(searchTimeout);
	});

	function choose(item: InstrumentRow) {
		value = item;
		open = false;
		results = [];
		focusedIndex = -1;
		dispatch('select', item);
	}

	function onBlur() {
		setTimeout(() => {
			open = false;
		}, 150);
	}

	function keydown(ev: KeyboardEvent) {
		if (!open || results.length === 0) return;

		if (ev.key === 'ArrowDown') {
			focusedIndex = (focusedIndex + 1) % results.length;
			ev.preventDefault();
		} else if (ev.key === 'ArrowUp') {
			focusedIndex = (focusedIndex - 1 + results.length) % results.length;
			ev.preventDefault();
		} else if (ev.key === 'Enter' && results[focusedIndex]) {
			choose(results[focusedIndex]);
			ev.preventDefault();
		} else if (ev.key === 'Escape') {
			open = false;
		}
	}
</script>

<div class="relative w-full">
	<input
		type="text"
		bind:value={query}
		{placeholder}
		on:focus={() => {
			if (results.length > 0) open = true;
		}}
		on:blur={onBlur}
		on:keydown={keydown}
		class="w-full bg-transparent border-0 border-b text-sm focus:ring-0 focus:outline-none"
	/>

	{#if open && (results.length > 0 || loading)}
		<div class="absolute z-10 w-full mt-1 bg-background border rounded-md shadow-lg max-h-60 overflow-y-auto">
			{#if loading}
				<div class="p-2 text-sm text-muted-foreground">Searching...</div>
			{:else}
				<ul>
					{#each results as instrument, i (instrument.instrument_token)}
						<li
							on:mousedown|preventDefault={() => choose(instrument)}
							class="px-3 py-2 text-sm cursor-pointer hover:bg-muted"
							class:bg-muted={i === focusedIndex}
						>
							{formatLabel(instrument)}
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	{/if}
</div>