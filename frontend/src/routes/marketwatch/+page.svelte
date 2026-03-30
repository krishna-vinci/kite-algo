<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { browser } from '$app/environment';
	import { getApiBase, getUserSubscriptions, saveUserSubscriptions } from '$lib/api';
	import { marketwatch } from '$lib/stores/marketwatch';

	interface Instrument {
		instrument_token: number;
		tradingsymbol: string;
		name: string;
		exchange: string;
		instrument_type: string;
	}

	type Mode = 'ltp' | 'quote' | 'full';

	interface OHLC {
		open?: number;
		high?: number;
		low?: number;
		close?: number;
	}

	interface DepthLevel {
		price: number;
		orders: number;
		quantity: number;
	}

	interface Tick {
		instrument_token: number;
		last_price?: number;
		change?: number;
		exchange_timestamp?: string;
		ohlc?: OHLC;
		volume_traded?: number;
		total_buy_quantity?: number;
		total_sell_quantity?: number;
		depth?: {
			buy: DepthLevel[];
			sell: DepthLevel[];
		};
		oi?: number;
		oi_day_high?: number;
		oi_day_low?: number;
		last_trade_time?: string;
	}

	let searchInput: string = '';
	let searchResults: Instrument[] = [];

	// Subscribed instruments and desired modes per instrument
	let subscribedInstruments: Map<number, Instrument> = new Map();
	let desiredModes: Map<number, Mode> = new Map(); // default 'quote' when subscribing

	// Derive websocket status from store connection
	$: websocketStatus = $marketwatch.connection ? 'CONNECTED' : 'DISCONNECTED';
	// Get live ticks from store
	$: liveTicks = new Map(
		Object.entries($marketwatch.instruments).map(([token, data]) => [parseInt(token), data as Tick])
	);

	// Fetch instruments search results
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
		if (searchInput) {
			searchTimeout = setTimeout(() => {
				fetchSearchResults(searchInput);
			}, 300);
		} else {
			searchResults = [];
		}
	}

	// Persist subscriptions to the server (scoped to marketwatch)
	let saveTimeout: ReturnType<typeof setTimeout>;
	function saveSubscriptions() {
		if (!browser) return;
		clearTimeout(saveTimeout);
		saveTimeout = setTimeout(async () => {
			try {
				// Construct payload matching the tolerant backend structure
				const groups = [
					{
						name: 'Marketwatch',
						instruments: Array.from(subscribedInstruments.values())
					}
				];
				const mode = 'quote'; // Default mode for now, can be enhanced later

				await saveUserSubscriptions({ subscriptions: { groups, mode } }, 'marketwatch');
			} catch (e) {
				console.warn('Failed to save subscriptions to server:', e);
			}
		}, 1000); // Debounce saves by 1 second
	}

	function subscribe(instrument: Instrument) {
		const token = instrument.instrument_token;
		// Default mode is quote
		desiredModes.set(token, 'quote');
		subscribedInstruments.set(token, instrument);
		subscribedInstruments = new Map(subscribedInstruments); // Trigger reactivity
		saveSubscriptions();
		// Use store method - it handles connection state and queuing
		marketwatch.subscribeToInstruments([token], 'quote');
	}

	function unsubscribe(instrument_token: number) {
		marketwatch.unsubscribeFromInstruments([instrument_token]);
		subscribedInstruments.delete(instrument_token);
		desiredModes.delete(instrument_token);
		saveSubscriptions();
		// trigger reactivity
		subscribedInstruments = new Map(subscribedInstruments);
		desiredModes = new Map(desiredModes);
	}

	function setMode(instrument_token: number, mode: Mode) {
		if (!subscribedInstruments.has(instrument_token)) {
			console.warn('Instrument not subscribed; subscribe first.');
			return;
		}
		desiredModes.set(instrument_token, mode);
		desiredModes = new Map(desiredModes);
		saveSubscriptions();
		// Use store method to set mode for specific tokens
		marketwatch.subscribeToInstruments([instrument_token], mode);
	}

	// Removed legacy resubscribeAll() to avoid localStorage-driven clobbering.

	onMount(async () => {
		if (!browser) return;

		// Load subscriptions from the server (scoped: marketwatch)
		try {
			// getUserSubscriptions returns an envelope: { subscriptions: ... }
			const resp = await getUserSubscriptions('marketwatch');
			const subs = resp?.subscriptions ?? {};

			if (subs && subs.groups) {
				const tokens = new Set<number>();
				const instrumentsByToken = new Map<number, Instrument>();

				subs.groups.forEach((group: any) => {
					// Prefer full instrument objects when present
					if (Array.isArray(group?.instruments)) {
						group.instruments.forEach((inst: any) => {
							const raw = inst?.instrument_token ?? inst?.token;
							const token = typeof raw === 'string' ? parseInt(raw, 10) : Number(raw);
							if (Number.isFinite(token)) {
								tokens.add(token);
								// Restore richer details from persisted instruments
								const instrument: Instrument = {
									instrument_token: token,
									tradingsymbol: String(inst?.tradingsymbol ?? inst?.symbol ?? `TOKEN ${token}`),
									name: String(inst?.name ?? inst?.tradingsymbol ?? `Token ${token}`),
									exchange: String(inst?.exchange ?? ''),
									instrument_type: String(inst?.instrument_type ?? '')
								};
								instrumentsByToken.set(token, instrument);
							}
						});
					} else if (Array.isArray(group?.tokens)) {
						// Fallback: tokens array only
						group.tokens.forEach((t: any) => {
							const token = typeof t === 'string' ? parseInt(t, 10) : Number(t);
							if (Number.isFinite(token)) {
								tokens.add(token);
								// Only create a minimal placeholder if no instrument details exist for this token
								if (!instrumentsByToken.has(token)) {
									instrumentsByToken.set(token, {
										instrument_token: token,
										tradingsymbol: String(token),
										name: '',
										exchange: '',
										instrument_type: ''
									});
								}
							}
						});
					}
				});

				// Apply restored instruments to UI map (do not overwrite later)
				if (instrumentsByToken.size > 0) {
					subscribedInstruments = new Map(instrumentsByToken);
				}

				const validTokens = Array.from(tokens);
				const mode =
					subs && ['ltp', 'quote', 'full'].includes(subs.mode) ? (subs.mode as Mode) : 'quote';

				if (validTokens.length > 0) {
					// Subscribe in quote mode by default; adjust mode if needed
					marketwatch.subscribeToInstruments(validTokens, 'quote');
					if (mode !== 'quote') {
						marketwatch.setMode(mode);
					}
				}
			}
		} catch (e) {
			console.warn(
				'Could not fetch subscriptions from server (marketwatch scope). Starting fresh.',
				e
			);
		}

		// Use the store to connect - singleton guard will prevent duplicate connections
		marketwatch.connect();
	});

	onDestroy(() => {
		// No need to close socket - the store manages the singleton connection
		// Other components like Sidebar might still be using it
	});

	// Helper to format change percentage
	function formatChange(change: number | undefined): string {
		if (change === undefined || change === null) return 'N/A';
		const sign = change >= 0 ? '+' : '';
		return `${sign}${change.toFixed(2)}%`;
	}
</script>

<div class="container mx-auto p-4">
	<h1 class="text-2xl font-bold mb-4">Market Watch</h1>

	<div class="mb-4">
		<p>
			WebSocket Status:
			<span
				class="font-semibold {websocketStatus === 'CONNECTED'
					? 'text-green-500'
					: websocketStatus === 'DISCONNECTED'
						? 'text-red-500'
						: 'text-yellow-500'}"
			>
				{websocketStatus}
			</span>
		</p>
	</div>

	<div class="mb-6">
		<h2 class="text-xl font-semibold mb-2">Search Instruments</h2>
		<input
			type="text"
			bind:value={searchInput}
			placeholder="Search by symbol or name (e.g., RELIANCE, NIFTY)"
			class="border p-2 rounded w-full md:w-1/2"
		/>

		{#if searchResults.length > 0}
			<div class="mt-4 border rounded shadow-md max-h-60 overflow-y-auto">
				{#each searchResults as instrument (instrument.instrument_token)}
					<div
						class="flex justify-between items-center p-2 border-b last:border-b-0 hover:bg-gray-50"
					>
						<div>
							<p class="font-medium">{instrument.tradingsymbol} - {instrument.name}</p>
							<p class="text-sm text-gray-500">
								{instrument.exchange}:{instrument.instrument_type} (Token: {instrument.instrument_token})
							</p>
						</div>
						<button
							on:click={() => subscribe(instrument)}
							disabled={subscribedInstruments.has(instrument.instrument_token)}
							class="bg-blue-500 text-white p-2 rounded text-sm disabled:bg-gray-400"
						>
							{#if subscribedInstruments.has(instrument.instrument_token)}
								Subscribed
							{:else}
								Subscribe (quote)
							{/if}
						</button>
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<div class="mb-6">
		<h2 class="text-xl font-semibold mb-2">Subscribed Instruments</h2>
		{#if subscribedInstruments.size === 0}
			<p class="text-gray-600">No instruments subscribed yet. Search and subscribe above!</p>
		{:else}
			<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
				{#each Array.from(subscribedInstruments.values()) as instrument (instrument.instrument_token)}
					<div class="border p-4 rounded shadow-md">
						<div class="flex justify-between items-center mb-2">
							<h3 class="font-bold text-lg">{instrument.tradingsymbol}</h3>
							<button
								on:click={() => unsubscribe(instrument.instrument_token)}
								class="bg-red-500 text-white p-1 px-2 rounded text-sm"
							>
								Unsubscribe
							</button>
						</div>
						<p class="text-gray-700">{instrument.name}</p>
						<p class="text-sm text-gray-500">{instrument.exchange}:{instrument.instrument_type}</p>

						<div class="mt-2 flex items-center gap-2">
							<span class="text-sm text-gray-600">Mode:</span>
							<button
								class="px-2 py-1 rounded text-xs border {desiredModes.get(
									instrument.instrument_token
								) === 'quote'
									? 'bg-blue-500 text-white'
									: 'bg-white'}"
								on:click={() => setMode(instrument.instrument_token, 'quote')}
							>
								Quote
							</button>
							<button
								class="px-2 py-1 rounded text-xs border {desiredModes.get(
									instrument.instrument_token
								) === 'full'
									? 'bg-blue-500 text-white'
									: 'bg-white'}"
								on:click={() => setMode(instrument.instrument_token, 'full')}
							>
								Full
							</button>
						</div>

						{#if liveTicks.has(instrument.instrument_token)}
							{@const tick = liveTicks.get(instrument.instrument_token)}
							<div class="mt-3 space-y-1">
								<p>
									<strong>LTP:</strong>
									{tick?.last_price !== undefined ? tick.last_price.toFixed(2) : 'N/A'}
								</p>
								<p>
									<strong>Change:</strong>
									<span
										class={tick?.change !== undefined && tick.change >= 0
											? 'text-green-600'
											: 'text-red-600'}
									>
										{formatChange(tick?.change)}
									</span>
								</p>
								<p><strong>Volume:</strong> {tick?.volume_traded ?? 'N/A'}</p>
								{#if tick?.ohlc}
									<p class="text-sm text-gray-600">
										<strong>OHLC:</strong>
										O {tick.ohlc.open ?? '-'} H {tick.ohlc.high ?? '-'} L {tick.ohlc.low ?? '-'} C {tick
											.ohlc.close ?? '-'}
									</p>
								{/if}
							</div>

							{#if tick?.depth}
								<div class="mt-3 grid grid-cols-2 gap-4">
									<div>
										<p class="font-semibold text-sm mb-1">Buy Depth</p>
										<table class="text-xs w-full">
											<thead>
												<tr class="text-gray-500">
													<th class="text-left">Qty</th>
													<th class="text-left">Price</th>
													<th class="text-left">Orders</th>
												</tr>
											</thead>
											<tbody>
												{#each tick.depth.buy.slice(0, 5) as lvl}
													<tr>
														<td>{lvl.quantity}</td>
														<td>{lvl.price}</td>
														<td>{lvl.orders}</td>
													</tr>
												{/each}
											</tbody>
										</table>
									</div>
									<div>
										<p class="font-semibold text-sm mb-1">Sell Depth</p>
										<table class="text-xs w-full">
											<thead>
												<tr class="text-gray-500">
													<th class="text-left">Qty</th>
													<th class="text-left">Price</th>
													<th class="text-left">Orders</th>
												</tr>
											</thead>
											<tbody>
												{#each tick.depth.sell.slice(0, 5) as lvl}
													<tr>
														<td>{lvl.quantity}</td>
														<td>{lvl.price}</td>
														<td>{lvl.orders}</td>
													</tr>
												{/each}
											</tbody>
										</table>
									</div>
								</div>
							{/if}
						{:else}
							<p class="mt-2 text-gray-500">Waiting for data...</p>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	</div>
</div>
