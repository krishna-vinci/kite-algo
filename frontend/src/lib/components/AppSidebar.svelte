<script lang="ts">
    import { onMount } from 'svelte';
    import { browser } from '$app/environment';
    import { getApiBase, getUserSubscriptions, saveUserSubscriptions } from '$lib/api';
    import { marketwatch } from '$lib/stores/marketwatch';
    import type { Instrument, Group, WatchlistData } from '$lib/types';
   
    // --- Icon Components (as SVG strings) ---
    const SearchIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>`;
    const SlidersIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line><line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line><line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line><line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line><line x1="17" y1="16" x2="23" y2="16"></line></svg>`;
    const BriefcaseIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="20" height="14" rx="2" ry="2"></rect><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"></path></svg>`;
    const ArrowUpIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg>`;
    const ArrowDownIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"></line><polyline points="19 12 12 19 5 12"></polyline></svg>`;
    const LayersIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>`;
    const PlusIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24" viewfill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>`;
    const TrashIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>`;

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
    		const url = `${getApiBase()}/broker/instruments/fuzzy-search?query=${encodeURIComponent(query)}`;
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
    let dropIndicator: { targetId: string | null; position: 'before' | 'after' } = { targetId: null, position: 'before' };

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
                allWatchlists = [{
                    groups: subs.groups,
                    activeGroupIndex: subs.activeGroupIndex || 0
                }];
                // Assuming we are only dealing with one watchlist for now
                currentWatchlistPage = 0;
            } else {
                // Initialize with a default structure if nothing is stored on the server
                allWatchlists = [
                    createDefaultWatchlist(),
                    createEmptyWatchlist(),
                    createEmptyWatchlist(),
                    createEmptyWatchlist(),
                    createEmptyWatchlist(),
                    createEmptyWatchlist(),
                    createEmptyWatchlist()
                ];
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
            allWatchlists[currentWatchlistPage].activeGroupIndex = groups.length -1;
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
        event.dataTransfer.setData('application/json', JSON.stringify({ instrumentId: instrument.id, fromGroupIndex: activeGroupIndex }));
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
        dropIndicator = { targetId: targetInstrument.id, position: event.clientY < midpointY ? 'before' : 'after' };
    }

    function handleInstrumentDrop(event: DragEvent, targetInstrument: Instrument) {
        event.preventDefault();
        const data = JSON.parse(event.dataTransfer.getData('application/json'));
        const { instrumentId, fromGroupIndex } = data;

        if (fromGroupIndex !== activeGroupIndex || instrumentId === targetInstrument.id) return;
        
        const instrumentToMove = activeGroup.instruments.find(inst => inst.id === instrumentId);
        if (!instrumentToMove) return;

        const items = activeGroup.instruments.filter(inst => inst.id !== instrumentId);
        const targetIndex = items.findIndex(inst => inst.id === targetInstrument.id);
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
        if (typeof liveData?.ohlc?.close === 'number' && typeof liveData?.last_price === 'number' && liveData.ohlc.close > 0) {
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

<aside class="sidebar">
 <!-- Top Section -->
	<div class="top-section">
		<div class="search-container">
			<div class="search-icon">{@html SearchIcon}</div>
			<input type="text" placeholder="Search eg: infy bse, nifty fut, etc" bind:value={searchTerm} />
			<div class="search-actions"><span class="kbd">Ctrl</span><span class="kbd">K</span><button class="icon-button">{@html SlidersIcon}</button></div>
			{#if searchResults.length > 0}
				<ul class="search-results">
					{#each searchResults as instrument (instrument.instrument_token)}
						<li on:click={() => addInstrumentToActiveGroup(instrument)}>
							<span>{instrument.tradingsymbol}</span>
							<span class="text-xs text-gray-500">{instrument.name}</span>
							<span class="add-indicator">+</span>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
		<div class="watchlist-info">
			<span>MW {currentWatchlistPage + 1} ({activeGroup?.instruments.length ?? 0} / 250)</span>
			<button class="link-button" on:click={addGroup}>+ New group</button>
		</div>
		<div class="group-list">
			{#if groups}
				{#each groups as group, i (group.id)}
					<button
						class="group-selector"
						class:active={activeGroupIndex === i}
						on:click={() => setActiveGroup(i)}
						on:dragover|preventDefault={(e) => (e.currentTarget as HTMLElement).classList.add('drag-over')}
						on:dragleave={(e) => (e.currentTarget as HTMLElement).classList.remove('drag-over')}
						on:drop={(e) => handleGroupDrop(e, i)}
					>
						<span class="group-name">{group.name}</span>
						<span class="group-count">({group.instruments.length})</span>
					</button>
				{/each}
			{/if}
		</div>
	</div>

	<!-- Middle Section -->
	<div class="middle-section">
		{#if activeGroup}
			<ul>
				{#each activeGroup.instruments as instrument (instrument.id)}
					{@const liveData = $marketwatch.instruments[instrument.instrument_token] || instrument}
					{@const percentChange = getPercentChange(liveData, instrument)}
					{@const priceChange = getPriceChange(liveData, instrument)}
					{@const isDown = (percentChange ?? 0) < 0}
					<li
						class="stock-item"
						draggable="true"
						on:dragstart={(e) => handleDragStart(e, instrument)}
						on:dragend={handleDragEnd}
						on:dragover={(e) => handleInstrumentDragOver(e, instrument)}
						on:dragleave={() => dropIndicator.targetId = null}
						on:drop={(e) => handleInstrumentDrop(e, instrument)}
					>
						{#if dropIndicator.targetId === instrument.id && dropIndicator.position === 'before'}<div class="drop-indicator top" />{/if}

						<span class="stock-name" class:red={isDown} class:green={!isDown}>{instrument.name}</span>
						<div class="stock-data">
							<span class="data-point change" class:red={isDown}>{(priceChange ?? 0).toFixed(2)}</span>
							<span class="data-point percent-change" class:red={isDown} class:green={!isDown}>
								{(percentChange ?? 0).toFixed(2)}%
								<span class="arrow-icon">{#if isDown}{@html ArrowDownIcon}{:else}{@html ArrowUpIcon}{/if}</span>
							</span>
							<span class="data-point price" class:red={isDown} class:green={!isDown}>{(liveData?.last_price ?? instrument.price ?? 0).toFixed(2)}</span>
						</div>
						<button class="delete-button" title="Delete Instrument" on:click|stopPropagation={() => deleteInstrument(instrument.id)}>
							{@html TrashIcon}
						</button>

						{#if dropIndicator.targetId === instrument.id && dropIndicator.position === 'after'}<div class="drop-indicator bottom" />{/if}
					</li>
				{:else}
					<div class="empty-state">This group is empty. Drag instruments here.</div>
				{/each}
			</ul>
		{:else}
			<div class="empty-state">Loading...</div>
		{/if}
	</div>

	<!-- Bottom Section -->
	<div class="bottom-section">
		<div class="pagination">
			{#each allWatchlists as _, i (i)}
				<button on:click={() => (currentWatchlistPage = i)} class="pagination-button" class:active={currentWatchlistPage === i}>
					{i + 1}
				</button>
			{/each}
		</div>
		<div class="bottom-actions">
			<button class="icon-button">{@html LayersIcon}</button>
			<button class="icon-button">{@html PlusIcon}</button>
		</div>
	</div>
</aside>

<style>
    :root { --border-color: #e5e7eb; --text-gray-400: #9ca3af; --text-gray-500: #6b7280; --text-gray-800: #1f2937; --text-gray-900: #111827; --bg-gray-50: #f9fafb; --bg-gray-100: #f3f4f6; --brand-blue: #2563eb; --brand-orange: #f97316; --brand-red: #dc2626; --brand-green: #16a34a; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    button { background: none; border: none; cursor: pointer; font-family: inherit; }
    ul { list-style: none; }
    .sidebar { width: 360px; height: 100%; display: flex; flex-direction: column; border-right: 1px solid var(--border-color); background-color: white; }
    .top-section { padding: 0.75rem; border-bottom: 1px solid var(--border-color); display: flex; flex-direction: column; gap: 0.75rem; }
    .middle-section { flex-grow: 1; overflow-y: auto; padding: 0.5rem 0.75rem; }
    .bottom-section { display: flex; align-items: center; justify-content: space-between; padding: 0.375rem 1rem; border-top: 1px solid var(--border-color); }
    .search-container { position: relative; }
    .search-container input { width: 100%; height: 36px; padding-left: 36px; padding-right: 96px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 14px; }
    .search-results { position: absolute; top: 100%; left: 0; right: 0; background: white; border: 1px solid #d1d5db; border-top: none; border-radius: 0 0 6px 6px; max-height: 200px; overflow-y: auto; z-index: 100; }
    .search-results li { display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; cursor: pointer; }
    .search-results li:hover { background-color: var(--bg-gray-100); }
    .search-results .add-indicator { color: var(--brand-green); }
    .search-icon { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); width: 16px; height: 16px; color: var(--text-gray-400); }
    .search-actions { position: absolute; right: 8px; top: 50%; transform: translateY(-50%); display: flex; align-items: center; gap: 4px; }
    .kbd { background-color: var(--bg-gray-100); border: 1px solid #d1d5db; border-radius: 4px; padding: 2px 6px; font-size: 12px; color: var(--text-gray-500); }
    .watchlist-info { display: flex; align-items: center; justify-content: space-between; font-size: 12px; color: var(--text-gray-500); }
    .link-button { color: var(--brand-blue); font-weight: 500; }
    .link-button:hover { text-decoration: underline; }
    .group-list { display: flex; flex-direction: column; gap: 4px; }
    .group-selector { width: 100%; height: 36px; border: 1px solid #d1d5db; background-color: white; border-radius: 6px; text-align: left; padding: 0 12px; transition: all 0.2s ease; }
    .group-selector.active { background-color: var(--bg-gray-100); border-color: #a1a1aa; }
    .group-selector.drag-over { border-color: var(--brand-blue); box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.4); }
    .group-name { font-weight: 600; color: var(--text-gray-800); }
    .group-count { margin-left: 4px; color: var(--text-gray-500); }
    .stock-item { position: relative; display: grid; grid-template-columns: 1fr auto; align-items: center; padding: 6px 32px 6px 8px; border-radius: 4px; cursor: grab; transition: background-color 0.2s ease; }
    .stock-item:hover { background-color: var(--bg-gray-100); }
    .stock-item.is-dragging { opacity: 0.5; background-color: #dbeafe; }
    .drop-indicator { position: absolute; left: 8px; right: 8px; height: 2px; background-color: var(--brand-blue); z-index: 10; }
    .drop-indicator.top { top: -1px; }
    .drop-indicator.bottom { bottom: -1px; }
    .stock-name { font-size: 14px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .stock-data { display: flex; align-items: center; gap: 8px; font-size: 14px; }
    .data-point { text-align: right; }
    .change { width: 64px; }
    .percent-change { width: 70px; gap: 2px; display: flex; align-items: center; justify-content: flex-end; }
    .price { width: 80px; font-weight: 500; }
    .red { color: var(--brand-red); }
    .green { color: var(--brand-green); }
    .delete-button { position: absolute; top: 50%; right: 8px; transform: translateY(-50%); display: none; align-items: center; justify-content: center; width: 24px; height: 24px; color: var(--text-gray-400); z-index: 5; }
    .stock-item:hover .delete-button { display: flex; }
    .delete-button:hover { color: var(--brand-red); }
    .empty-state { text-align: center; color: var(--text-gray-400); font-size: 14px; padding: 2rem 0; }
    .pagination { display: flex; align-items: center; gap: 16px; overflow-x: auto; }
    .pagination-button { position: relative; padding: 8px 4px; font-size: 14px; color: var(--text-gray-500); flex-shrink: 0; }
    .pagination-button.active { font-weight: 700; color: var(--text-gray-900); }
    .pagination-button.active::after { content: ''; position: absolute; bottom: 0; left: 50%; transform: translateX(-50%); width: 20px; height: 2px; background-color: var(--brand-orange); }
    .bottom-actions { display: flex; gap: 12px; color: var(--text-gray-500); }
    .icon-button { display: flex; align-items: center; justify-content: center; width: 32px; height: 32px; color: var(--text-gray-500); }
    .icon-button:hover { color: var(--text-gray-800); }
    .icon-button :global(svg) { width: 16px; height: 16px; }
    .arrow-icon :global(svg) { width: 14px; height: 14px; }
    .bottom-actions .icon-button :global(svg) { width: 20px; height: 20px; }
    .delete-button :global(svg) { width: 14px; height: 14px; }
   </style>