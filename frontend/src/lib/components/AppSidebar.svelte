<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import { getApiBase, getUserSubscriptions, saveUserSubscriptions } from '$lib/api';
	import { marketwatch } from '$lib/stores/marketwatch';
	import type { Instrument, Group, WatchlistData } from '$lib/types';
	export let isCollapsed = true;
	// --- Icon Components (as SVG strings) ---
	const SearchIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>`;
	const SlidersIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line><line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line><line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line><line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line><line x1="17" y1="16" x2="23" y2="16"></line></svg>`;
	const BriefcaseIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="20" height="14" rx="2" ry="2"></rect><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"></path></svg>`;
	const ArrowUpIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg>`;
	const ArrowDownIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"></line><polyline points="19 12 12 19 5 12"></polyline></svg>`;
	const LayersIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>`;
	const PlusIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24" viewfill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>`;
	const TrashIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>`;
	const ChevronLeftIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>`;
	const ChevronRightIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>`;

	// --- Primary State ---
	let allWatchlists: WatchlistData[] = [];
	let currentWatchlistPage = 0; // This is the index for the bottom pagination (0 = Page 1)
	let searchTerm = '';
	let searchResults: Instrument[] = [];

	// --- Derived State ---
	$: activeWatchlist = allWatchlists[currentWatchlistPage];
	$: groups = activeWatchlist ? activeWatchlist.groups : [];
	$: activeGroupIndex = activeWatchlist ? activeWatchlist.activeGroupIndex : 0;
	$: activeGroup = groups ? groups[activeGroupIndex] : undefined;

	$: {
		if (activeGroup) {
			const tokens = activeGroup.instruments.map((inst) => inst.instrument_token);
			marketwatch.subscribeToInstruments(tokens);
		}
	}

	// --- Search Logic ---
	async function fetchSearchResults(query: string) {
		if (query.length < 2) {
			searchResults = [];
			return;
		}
		try {
			const url = `${getApiBase()}/api/instruments/fuzzy-search?query=${encodeURIComponent(query)}`;
			const response = await fetch(url, { credentials: 'include' });
			if (response.ok) {
				searchResults = await response.json();
			} else {
				console.error('Failed to fetch search results:', response.status, response.statusText);
				searchResults = [];
			}
		} catch (error) {
			console.error('Error fetching search results:', error);
			searchResults = [];
		}
	}

	// Debounce search
	let searchTimeout: ReturnType<typeof setTimeout>;
	$: {
		clearTimeout(searchTimeout);
		if (searchTerm) {
			searchTimeout = setTimeout(() => {
				fetchSearchResults(searchTerm);
			}, 300);
		} else {
			searchResults = [];
		}
	}

	// Drag & Drop state
	let dropIndicator: { targetId: string | null; position: 'before' | 'after' } = {
		targetId: null,
		position: 'before'
	};

	function generateId() {
		return crypto?.randomUUID ? crypto.randomUUID() : Math.random().toString(36).substring(2, 15);
	}

	function createDefaultWatchlist(): WatchlistData {
		return {
			groups: [
				{
					id: generateId(),
					name: 'Default',
					instruments: [
						{
							instrument_token: 2953217,
							id: generateId(),
							name: 'TCS',
							qty: 10,
							change: 15.5,
							percentChange: 0.45,
							price: 3450.0
						}
					]
				}
			],
			activeGroupIndex: 0
		};
	}

	function createEmptyWatchlist(): WatchlistData {
		return {
			groups: [{ id: generateId(), name: 'Default', instruments: [] }],
			activeGroupIndex: 0
		};
	}

	// --- Persistence ---
	onMount(async () => {
		marketwatch.connect();
		try {
			// Try scoped sidebar data first
			let resp = await getUserSubscriptions('sidebar');
			let subs = resp?.subscriptions;

			// Fallback to legacy (unscoped) if empty/missing
			if (!subs || !subs.groups || subs.groups.length === 0) {
				const legacy = await getUserSubscriptions();
				subs = legacy?.subscriptions || legacy; // support legacy shape temporarily
			}

			if (subs && subs.groups && subs.groups.length > 0) {
				// The API returns a single watchlist, so we wrap it in an array
				allWatchlists = [
					{
						groups: subs.groups,
						activeGroupIndex: subs.activeGroupIndex || 0
					}
				];
				// Assuming we are only dealing with one watchlist for now
				currentWatchlistPage = 0;
			} else {
				// Initialize with a default structure if nothing is stored on the server
				allWatchlists = [createDefaultWatchlist()];
			}
			while (allWatchlists.length < 5) {
				allWatchlists.push(createEmptyWatchlist());
			}
		} catch (e) {
			console.warn('Could not fetch subscriptions from server. Using default.', e);
			allWatchlists = [createDefaultWatchlist()];
		}
	});

	let saveTimeout: ReturnType<typeof setTimeout>;
	function saveData() {
		if (!activeWatchlist) return;
		clearTimeout(saveTimeout);
		saveTimeout = setTimeout(async () => {
			try {
				await saveUserSubscriptions(
					{
						subscriptions: {
							groups: activeWatchlist.groups,
							activeGroupId: activeGroup?.id
						}
					},
					'sidebar'
				);
			} catch (e) {
				console.warn('Could not save to server.', e);
			}
		}, 1000); // Debounce for 1 second
	}

	function setActiveGroup(index: number) {
		allWatchlists[currentWatchlistPage].activeGroupIndex = index;
		allWatchlists = allWatchlists; // Trigger reactivity
		saveData();
	}

	function addGroup() {
		const name = prompt('Enter new group name:', `My Group ${groups.length + 1}`);
		if (name) {
			const newGroup = { id: generateId(), name: name.trim(), instruments: [] };
			allWatchlists[currentWatchlistPage].groups.push(newGroup);
			allWatchlists[currentWatchlistPage].activeGroupIndex = groups.length - 1;
			allWatchlists = allWatchlists;
			saveData();
		}
	}

	function addInstrumentToActiveGroup(instrument: any) {
		if (!activeGroup) return;
		// Create a new instrument object to avoid reactivity issues
		const newInstrument: Instrument = {
			instrument_token: instrument.instrument_token,
			id: generateId(),
			name: instrument.tradingsymbol,
			qty: 1,
			price: 0, // Will be updated by marketwatch
			percentChange: 0 // will be updated by marketwatch
		};
		activeGroup.instruments.push(newInstrument);
		marketwatch.subscribeToInstruments([newInstrument.instrument_token]);
		allWatchlists = allWatchlists; // Trigger reactivity
		saveData();
		// Clear search
		searchTerm = '';
	}

	function deleteInstrument(instrumentId: string) {
		const group = groups[activeGroupIndex];
		const instrument = group.instruments.find((inst) => inst.id === instrumentId);
		if (instrument) {
			marketwatch.unsubscribeFromInstruments([instrument.instrument_token]);
		}
		group.instruments = group.instruments.filter((inst) => inst.id !== instrumentId);
		allWatchlists = allWatchlists;
		saveData();
	}

	// --- Drag and Drop Handlers ---
	function handleDragStart(event: DragEvent, instrument: Instrument) {
		event.dataTransfer.effectAllowed = 'move';
		event.dataTransfer.setData(
			'application/json',
			JSON.stringify({ instrumentId: instrument.id, fromGroupIndex: activeGroupIndex })
		);
		(event.target as HTMLElement).classList.add('is-dragging');
	}

	function handleDragEnd(event: DragEvent) {
		(event.target as HTMLElement).classList.remove('is-dragging');
		dropIndicator = { targetId: null, position: 'before' };
	}

	function handleGroupDrop(event: DragEvent, toGroupIndex: number) {
		event.preventDefault();
		const data = JSON.parse(event.dataTransfer.getData('application/json'));
		const { instrumentId, fromGroupIndex } = data;
		(event.currentTarget as HTMLElement).classList.remove('drag-over');

		if (fromGroupIndex === toGroupIndex) return;

		const fromGroup = groups[fromGroupIndex];
		const toGroup = groups[toGroupIndex];
		const instrumentToMove = fromGroup.instruments.find((inst) => inst.id === instrumentId);

		if (instrumentToMove) {
			fromGroup.instruments = fromGroup.instruments.filter((inst) => inst.id !== instrumentId);
			toGroup.instruments.push(instrumentToMove);
			allWatchlists = allWatchlists;
			saveData();
		}
	}

	function handleInstrumentDragOver(event: DragEvent, targetInstrument: Instrument) {
		event.preventDefault();
		const targetEl = event.currentTarget as HTMLElement;
		const rect = targetEl.getBoundingClientRect();
		const midpointY = rect.top + rect.height / 2;
		dropIndicator = {
			targetId: targetInstrument.id,
			position: event.clientY < midpointY ? 'before' : 'after'
		};
	}

	function handleInstrumentDrop(event: DragEvent, targetInstrument: Instrument) {
		event.preventDefault();
		const data = JSON.parse(event.dataTransfer.getData('application/json'));
		const { instrumentId, fromGroupIndex } = data;

		if (fromGroupIndex !== activeGroupIndex || instrumentId === targetInstrument.id) return;

		const instrumentToMove = activeGroup.instruments.find((inst) => inst.id === instrumentId);
		if (!instrumentToMove) return;

		const items = activeGroup.instruments.filter((inst) => inst.id !== instrumentId);
		const targetIndex = items.findIndex((inst) => inst.id === targetInstrument.id);
		const newIndex = dropIndicator.position === 'before' ? targetIndex : targetIndex + 1;
		items.splice(newIndex, 0, instrumentToMove);

		activeGroup.instruments = items;
		allWatchlists = allWatchlists;
		dropIndicator = { targetId: null, position: 'before' };
		saveData();
	}

	function getPercentChange(liveData: any, instrument: Instrument): number {
		if (typeof liveData?.change === 'number') {
			return liveData.change;
		}
		if (
			typeof liveData?.ohlc?.close === 'number' &&
			typeof liveData?.last_price === 'number' &&
			liveData.ohlc.close > 0
		) {
			return ((liveData.last_price - liveData.ohlc.close) * 100) / liveData.ohlc.close;
		}
		return instrument.percentChange || 0;
	}

	// Absolute price change helper: last_price - close (falls back to 0)
	function getPriceChange(liveData: any, instrument: Instrument): number {
		const last = typeof liveData?.last_price === 'number' ? liveData.last_price : undefined;
		const close = typeof liveData?.ohlc?.close === 'number' ? liveData.ohlc.close : undefined;
		if (typeof last === 'number' && typeof close === 'number') {
			const diff = last - close;
			return Number.isFinite(diff) ? diff : 0;
		}
		// Optional fallback if instrument has a cached close; else 0
		const instPrice = typeof instrument?.price === 'number' ? instrument.price : undefined;
		const instClose = (instrument as any)?.close;
		if (typeof instPrice === 'number' && typeof instClose === 'number') {
			const diff = instPrice - instClose;
			return Number.isFinite(diff) ? diff : 0;
		}
		return 0;
	}
</script>

<aside
	class="relative h-full flex flex-col border-r border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 transition-all duration-200 ease-in-out"
	class:w-0={isCollapsed}
	class:border-r-0={isCollapsed}
	class:w-90={!isCollapsed}
>
	<div
		class="flex flex-col h-full overflow-hidden"
		class:hidden={isCollapsed}
		class:w-90={!isCollapsed}
	>
		<!-- Top Section -->
		<div class="p-3 border-b border-gray-200 dark:border-gray-700 flex flex-col gap-3">
			<div class="relative">
				<div
					class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 dark:text-gray-500"
				>
					{@html SearchIcon}
				</div>
				<input
					type="text"
					placeholder="Search eg: infy bse, nifty fut, etc"
					bind:value={searchTerm}
					class="w-full h-9 pl-9 pr-24 border border-gray-300 dark:border-gray-700 rounded-md text-sm bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-200"
				/>
				<div class="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-1">
					<span
						class="bg-gray-100 dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded px-1.5 py-0.5 text-xs text-gray-500 dark:text-gray-400"
						>Ctrl</span
					><span
						class="bg-gray-100 dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded px-1.5 py-0.5 text-xs text-gray-500 dark:text-gray-400"
						>K</span
					><button
						class="flex items-center justify-center w-8 h-8 text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
						>{@html SlidersIcon}</button
					>
				</div>
				{#if searchResults.length > 0}
					<ul
						class="absolute top-full left-0 right-0 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 border-t-0 rounded-b-md max-h-50 overflow-y-auto z-10"
					>
						{#each searchResults as instrument (instrument.instrument_token)}
							<li
								on:click={() => addInstrumentToActiveGroup(instrument)}
								class="flex justify-between items-center px-3 py-2 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700"
							>
								<span class="text-gray-800 dark:text-gray-200">{instrument.tradingsymbol}</span>
								<span class="text-xs text-gray-500 dark:text-gray-400">{instrument.name}</span>
								<span class="text-green-600 dark:text-green-400">+</span>
							</li>
						{/each}
					</ul>
				{/if}
			</div>
			<div class="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
				<span>MW {currentWatchlistPage + 1} ({activeGroup?.instruments.length ?? 0} / 250)</span>
				<button
					class="text-blue-600 dark:text-blue-400 font-medium hover:underline"
					on:click={addGroup}>+ New group</button
				>
			</div>
			<div class="flex flex-col gap-1">
				{#if groups}
					{#each groups as group, i (group.id)}
						<button
							class="w-full h-9 border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 rounded-md text-left px-3 transition-all duration-200 ease-in-out"
							class:bg-gray-100={activeGroupIndex === i}
							class:dark:bg-gray-700={activeGroupIndex === i}
							class:border-gray-400={activeGroupIndex === i}
							class:dark:border-gray-500={activeGroupIndex === i}
							on:click={() => setActiveGroup(i)}
							on:dragover|preventDefault={(e) =>
								(e.currentTarget as HTMLElement).classList.add('drag-over')}
							on:dragleave={(e) => (e.currentTarget as HTMLElement).classList.remove('drag-over')}
							on:drop={(e) => handleGroupDrop(e, i)}
						>
							<span class="font-semibold text-gray-800 dark:text-gray-200">{group.name}</span>
							<span class="ml-1 text-gray-500 dark:text-gray-400">({group.instruments.length})</span
							>
						</button>
					{/each}
				{/if}
			</div>
		</div>

		<!-- Middle Section -->
		<div class="flex-grow overflow-y-auto px-3 py-2">
			{#if activeGroup}
				<ul>
					{#each activeGroup.instruments as instrument (instrument.id)}
						{@const liveData = $marketwatch.instruments[instrument.instrument_token] || instrument}
						{@const percentChange = getPercentChange(liveData, instrument)}
						{@const priceChange = getPriceChange(liveData, instrument)}
						{@const isDown = (percentChange ?? 0) < 0}
						<li
							class="relative grid grid-cols-[1fr_auto] items-center px-2 py-1.5 pr-8 rounded cursor-grab transition-colors duration-200 ease-in-out hover:bg-gray-100 dark:hover:bg-gray-800"
							draggable="true"
							on:dragstart={(e) => handleDragStart(e, instrument)}
							on:dragend={handleDragEnd}
							on:dragover={(e) => handleInstrumentDragOver(e, instrument)}
							on:dragleave={() => (dropIndicator.targetId = null)}
							on:drop={(e) => handleInstrumentDrop(e, instrument)}
						>
							{#if dropIndicator.targetId === instrument.id && dropIndicator.position === 'before'}
								<div
									class="absolute left-2 right-2 h-0.5 bg-blue-600 dark:bg-blue-400 z-10 top-px"
								/>
							{/if}

							<span
								class="text-sm font-medium whitespace-nowrap overflow-hidden text-ellipsis"
								class:text-red-600={isDown}
								class:dark:text-red-400={isDown}
								class:text-green-600={!isDown}
								class:dark:text-green-400={!isDown}>{instrument.name}</span
							>
							<div class="flex items-center gap-2 text-sm">
								<span
									class="text-right w-16"
									class:text-red-600={isDown}
									class:dark:text-red-400={isDown}>{(priceChange ?? 0).toFixed(2)}</span
								>
								<span
									class="w-17.5 gap-0.5 flex items-center justify-end"
									class:text-red-600={isDown}
									class:dark:text-red-400={isDown}
									class:text-green-600={!isDown}
									class:dark:text-green-400={!isDown}
								>
									{(percentChange ?? 0).toFixed(2)}%
									<span class="w-3.5 h-3.5">{@html isDown ? ArrowDownIcon : ArrowUpIcon}</span>
								</span>
								<span
									class="text-right w-20 font-medium"
									class:text-red-600={isDown}
									class:dark:text-red-400={isDown}
									class:text-green-600={!isDown}
									class:dark:text-green-400={!isDown}
								>
									{(liveData?.last_price ?? instrument.price ?? 0).toFixed(2)}
								</span>
							</div>
							<button
								class="absolute top-1/2 right-2 -translate-y-1/2 hidden items-center justify-center w-6 h-6 text-gray-400 dark:text-gray-500 z-50 hover:text-red-600 dark:hover:text-red-400"
								title="Delete Instrument"
								on:click|stopPropagation={() => deleteInstrument(instrument.id)}
							>
								{@html TrashIcon}
							</button>

							{#if dropIndicator.targetId === instrument.id && dropIndicator.position === 'after'}
								<div
									class="absolute left-2 right-2 h-0.5 bg-blue-600 dark:bg-blue-400 z-10 bottom-px"
								/>
							{/if}
						</li>
					{:else}
						<div class="text-center text-gray-400 dark:text-gray-500 text-sm py-8">
							This group is empty. Drag instruments here.
						</div>
					{/each}
				</ul>
			{:else}
				<div class="text-center text-gray-400 dark:text-gray-500 text-sm py-8">Loading...</div>
			{/if}
		</div>

		<!-- Bottom Section -->
		<div
			class="flex items-center justify-between px-4 py-1.5 border-t border-gray-200 dark:border-gray-700"
		>
			<div class="flex items-center gap-4 overflow-x-auto">
				{#each allWatchlists as _, i (i)}
					<button
						on:click={() => (currentWatchlistPage = i)}
						class="relative px-1 py-2 text-sm text-gray-500 dark:text-gray-400 flex-shrink-0"
						class:font-bold={currentWatchlistPage === i}
						class:text-gray-900={currentWatchlistPage === i}
						class:dark:text-white={currentWatchlistPage === i}
					>
						{i + 1}
					</button>
				{/each}
			</div>
			<div class="flex gap-3 text-gray-500 dark:text-gray-400">
				<button
					class="flex items-center justify-center w-8 h-8 text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
					>{@html LayersIcon}</button
				>
				<button
					class="flex items-center justify-center w-8 h-8 text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
					>{@html PlusIcon}</button
				>
			</div>
		</div>
	</div>
	<button
		class="absolute top-1/2 left-full -translate-x-1/2 -translate-y-1/2 z-50 w-6 h-6 rounded-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 flex items-center justify-center cursor-pointer shadow-md transition-all duration-200 ease-in-out"
		class:left-3={isCollapsed}
		on:click={() => (isCollapsed = !isCollapsed)}
		title={isCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
	>
		{@html isCollapsed ? ChevronRightIcon : ChevronLeftIcon}
	</button>
</aside>
