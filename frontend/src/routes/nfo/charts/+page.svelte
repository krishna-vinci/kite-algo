<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { browser } from '$app/environment';
	import LightweightChart from '$lib/components/charts/LightweightChart.svelte';
	import TickerSearch from '$lib/components/TickerSearch.svelte';
	import type { Candle, LinePoint, SeriesSpec } from '$lib/components/charts/types';
	import { fetchCandles, getUserSubscriptions, saveUserSubscriptions, getApiBase } from '$lib/api';
	import type { CandlesResponse } from '$lib/api';
	import { marketwatch } from '$lib/stores/marketwatch';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Tabs from '$lib/components/ui/tabs/index.js';
	import type { InstrumentRow } from '$lib/types';

	// --- Type Definitions ---
	type Instrument = InstrumentRow;
	type LayoutType = 'single' | 'dual' | 'tri' | 'quad' | 'hexa' | 'octa';
	type TabType = LayoutType | 'instruments';
	const SUPPORTED_INTERVALS: { value: string; label: string }[] = [
		{ value: '1m', label: '1 Minute' },
		{ value: '3m', label: '3 Minutes' },
		{ value: '5m', label: '5 Minutes' },
		{ value: '15m', label: '15 Minutes' },
		{ value: '30m', label: '30 Minutes' },
		{ value: '1h', label: '1 Hour' },
		{ value: '1d', label: '1 Day' }
	];

	interface Tick {
		instrument_token: number;
		last_price?: number;
		change?: number;
		exchange_timestamp?: string;
		last_trade_time?: string;
	}

	// --- Component State ---
	let searchInput = $state('');
	let searchResults = $state<Instrument[]>([]);
	let subscribedInstruments = $state(new Map<number, Instrument>());

	// --- Layout & Persistence State ---
	const LAYOUT_CONFIG: Record<LayoutType, number> = {
		single: 1,
		dual: 2,
		tri: 3,
		quad: 4,
		hexa: 6,
		octa: 8
	};
	let activeTab = $state<TabType>('single');
	let activeLayout = $state<LayoutType>('single');
	let layouts = $state<Record<LayoutType, (number | null)[]>>({
		single: [256265], // Default to NIFTY 50
		dual: [null, null],
		tri: [null, null, null],
		quad: [null, null, null, null],
		hexa: Array(6).fill(null),
		octa: Array(8).fill(null)
	});
	let paneIntervals = $state<Record<LayoutType, string[]>>({
		single: ['5m'],
		dual: ['5m', '5m'],
		tri: ['5m', '5m', '5m'],
		quad: ['5m', '5m', '5m', '5m'],
		hexa: Array(6).fill('5m'),
		octa: Array(8).fill('5m')
	});

	// --- Live Data State ---
	const MAX_CANDLE_HISTORY = 5000;
	let candlesByTokenAndInterval = $state(new Map<number, Map<string, Candle[]>>());
	let ingestionStatus = $state(new Map<string, CandlesResponse['ingestion']['status']>());

	// --- Derived State ---
	const websocketStatus = $derived($marketwatch.connection ? 'CONNECTED' : 'DISCONNECTED');
	const liveTicks = $derived(
		new Map(
			Object.entries($marketwatch.instruments).map(([token, data]) => [
				parseInt(token),
				data as Tick
			])
		)
	);
	const paneTokens = $derived(layouts[activeLayout]);
	const currentPaneIntervals = $derived(paneIntervals[activeLayout]);

	$effect(() => {
		const layoutKeys = Object.keys(LAYOUT_CONFIG);
		if (layoutKeys.includes(activeTab)) {
			const newActiveLayout = activeTab as LayoutType;
			activeLayout = newActiveLayout;

			// Auto-refresh charts in the new layout when the tab is clicked
			if (browser) {
				const panesToRefresh = layouts[newActiveLayout];
				const intervalsToRefresh = paneIntervals[newActiveLayout];
				panesToRefresh.forEach((token, i) => {
					if (token) {
						const interval = intervalsToRefresh[i];
						// Using refreshCandles to ensure data is always fresh on tab switch
						refreshCandles(token, interval);
					}
				});
			}
		}
	});

	// --- Helper Functions ---
	const getKey = (token: number, interval: string) => `${token}|${interval}`;

	// --- Data Fetching ---
	async function loadCandles(
		token: number,
		interval: string,
		opts: { from?: number; to?: number; replace?: boolean } = {}
	) {
		const key = getKey(token, interval);
		try {
			const response = await fetchCandles(token, {
				timeframe: interval,
				from: opts.from,
				to: opts.to,
				ingest: true
			});

			ingestionStatus.set(key, response.ingestion.status);
			ingestionStatus = new Map(ingestionStatus);

			if (!candlesByTokenAndInterval.has(token)) {
				candlesByTokenAndInterval.set(token, new Map());
			}
			const existingCandles = candlesByTokenAndInterval.get(token)!.get(interval) ?? [];
			let newCandles = response.candles.sort((a, b) => a.time - b.time);

			let finalCandles: Candle[];
			if (opts.replace) {
				finalCandles = newCandles;
			} else {
				// Merge, avoiding duplicates
				const existingTimes = new Set(existingCandles.map((c) => c.time));
				const uniqueNew = newCandles.filter((c) => !existingTimes.has(c.time));
				finalCandles = [...existingCandles, ...uniqueNew].sort((a, b) => a.time - b.time);
			}

			if (finalCandles.length > MAX_CANDLE_HISTORY) {
				finalCandles = finalCandles.slice(finalCandles.length - MAX_CANDLE_HISTORY);
			}

			candlesByTokenAndInterval.get(token)!.set(interval, finalCandles);
			candlesByTokenAndInterval = new Map(candlesByTokenAndInterval);
		} catch (error) {
			console.error(`Failed to fetch candles for ${key}`, error);
		}
	}

	async function loadOlderCandles(token: number, interval: string) {
		const existing = candlesByTokenAndInterval.get(token)?.get(interval) ?? [];
		if (existing.length === 0) return;
		const earliestTs = existing[0].time;
		await loadCandles(token, interval, { to: earliestTs });
	}

	async function refreshCandles(token: number, interval: string) {
		await loadCandles(token, interval, { replace: true });
	}

	// --- Chart Data Derivation ---
	function getChartSeriesForToken(token: number | null, interval: string | null): SeriesSpec[] {
		if (!token || !interval) return [];
		const candles = candlesByTokenAndInterval.get(token)?.get(interval) || [];
		if (candles.length === 0) return [];

		const normalizedCandles = candles
			.map((c) => ({
				time: Math.round(Number(c.time)),
				open: Number(c.open),
				high: Number(c.high),
				low: Number(c.low),
				close: Number(c.close)
			}))
			.sort((a, b) => a.time - b.time);

		const emaData = generateEma(candles);
		const rsiData = generateRsi(candles);

		return [
			{ id: 'candles', type: 'candlestick', data: normalizedCandles },
			{ id: 'ema', type: 'line', data: emaData, options: { color: '#2962FF', lineWidth: 2 } },
			{ id: 'rsi', type: 'line', data: rsiData, options: { color: '#f44336', lineWidth: 2, pane: 1 } }
		];
	}

	// --- Indicator Helpers ---
	function generateEma(candles: Candle[], period = 20): LinePoint[] {
		if (candles.length < period) return [];
		const ema: LinePoint[] = [];
		let sum = 0;
		for (let i = 0; i < period; i++) sum += candles[i].close;
		let lastEma = sum / period;
		ema.push({ time: candles[period - 1].time, value: lastEma });
		for (let i = period; i < candles.length; i++) {
			const multiplier = 2 / (period + 1);
			const currentEma = (candles[i].close - lastEma) * multiplier + lastEma;
			ema.push({ time: candles[i].time, value: currentEma });
			lastEma = currentEma;
		}
		return ema;
	}

	function generateRsi(candles: Candle[], period = 14): LinePoint[] {
		if (candles.length <= period) return [];
		const rsi: LinePoint[] = [];
		let gains = 0,
			losses = 0;
		for (let i = 1; i <= period; i++) {
			const change = candles[i].close - candles[i - 1].close;
			if (change > 0) gains += change;
			else losses -= change;
		}
		let avgGain = gains / period;
		let avgLoss = losses / period;
		for (let i = period; i < candles.length; i++) {
			const change = candles[i].close - candles[i - 1].close;
			avgGain = (avgGain * (period - 1) + (change > 0 ? change : 0)) / period;
			avgLoss = (avgLoss * (period - 1) + (change < 0 ? -change : 0)) / period;
			const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
			rsi.push({ time: candles[i].time, value: 100 - 100 / (1 + rs) });
		}
		return rsi;
	}

	// --- Search and Subscription Logic ---
	async function fetchSearchResults(query: string) {
		if (query.length < 2) {
			searchResults = [];
			return;
		}
		try {
			const url = `${getApiBase()}/broker/instruments/fuzzy-search?query=${encodeURIComponent(
				query
			)}`;
			const response = await fetch(url, { credentials: 'include' });
			searchResults = response.ok ? await response.json() : [];
		} catch (error) {
			searchResults = [];
		}
	}

	$effect(() => {
		const currentSearch = searchInput;
		if (currentSearch) {
			const timeoutId = setTimeout(() => fetchSearchResults(currentSearch), 300);
			return () => clearTimeout(timeoutId);
		} else {
			searchResults = [];
		}
	});

	let saveUnionTimeout: ReturnType<typeof setTimeout>;
	function saveUnionSubscriptions() {
		if (!browser) return;
		clearTimeout(saveUnionTimeout);
		saveUnionTimeout = setTimeout(async () => {
			try {
				const payload = {
					subscriptions: {
						groups: [{ name: 'NFO Charts', instruments: Array.from(subscribedInstruments.values()) }],
						mode: 'full'
					}
				};
				await saveUserSubscriptions(payload, 'nfo-charts');
			} catch (e) {
				console.warn('Failed to save union subscriptions:', e);
			}
		}, 1000);
	}

	let saveLayoutsTimeout: ReturnType<typeof setTimeout>;
	function saveLayouts() {
		if (!browser) return;
		clearTimeout(saveLayoutsTimeout);
		saveLayoutsTimeout = setTimeout(async () => {
			try {
				const payload = {
					subscriptions: {
						version: 2, // Bump version for new structure
						layouts,
						intervals: paneIntervals
					}
				};
				await saveUserSubscriptions(payload, 'nfo-charts-layouts');
			} catch (e) {
				console.warn('Failed to save chart layouts:', e);
			}
		}, 1000);
	}

	function subscribe(instrument: Instrument) {
		const token = instrument.instrument_token;
		if (subscribedInstruments.has(token)) return;
		subscribedInstruments.set(token, instrument);
		subscribedInstruments = new Map(subscribedInstruments);
		saveUnionSubscriptions();
		marketwatch.subscribeToInstruments([token], 'full');
	}

	function unsubscribe(token: number) {
		marketwatch.unsubscribeFromInstruments([token]);
		subscribedInstruments.delete(token);
		subscribedInstruments = new Map(subscribedInstruments);
		saveUnionSubscriptions();
	}

	async function handlePaneSelect(instrument: Instrument, paneIndex: number) {
		const newLayouts = { ...layouts };
		const newPanes = [...newLayouts[activeLayout]];
		newPanes[paneIndex] = instrument.instrument_token;
		newLayouts[activeLayout] = newPanes;
		layouts = newLayouts;

		subscribe(instrument);
		saveLayouts();

		const token = instrument.instrument_token;
		const interval = currentPaneIntervals[paneIndex];
		await loadCandles(token, interval, { replace: true });
	}

	async function handleIntervalChange(interval: string, paneIndex: number) {
		const newPaneIntervals = { ...paneIntervals };
		const newIntervals = [...newPaneIntervals[activeLayout]];
		newIntervals[paneIndex] = interval;
		newPaneIntervals[activeLayout] = newIntervals;
		paneIntervals = newPaneIntervals;

		saveLayouts();

		const token = layouts[activeLayout][paneIndex];
		if (token) {
			await loadCandles(token, interval, { replace: true });
		}
	}

	function clearPane(paneIndex: number) {
		const newLayouts = { ...layouts };
		const newPanes = [...newLayouts[activeLayout]];
		newPanes[paneIndex] = null;
		newLayouts[activeLayout] = newPanes;
		layouts = newLayouts;

		saveLayouts();
	}

	onMount(async () => {
		if (!browser) return;

		// Load union subscriptions
		try {
			const resp = await getUserSubscriptions('nfo-charts');
			const subs = resp?.subscriptions ?? {};
			if (subs?.groups) {
				const instruments = new Map<number, Instrument>();
				subs.groups.forEach((g: any) =>
					g.instruments?.forEach((i: any) => instruments.set(i.instrument_token, i))
				);
				if (instruments.size > 0) subscribedInstruments = instruments;
			}
		} catch (e) {
			console.warn('Could not fetch nfo-charts subscriptions.', e);
		}

		// Load layouts and intervals
		try {
			const resp = await getUserSubscriptions('nfo-charts-layouts');
			const saved = resp?.subscriptions ?? {};
			if (saved.version === 2 && saved.layouts && saved.intervals) {
				const newLayouts = { ...layouts };
				for (const layoutKey in saved.layouts) {
					if (Object.prototype.hasOwnProperty.call(LAYOUT_CONFIG, layoutKey)) {
						const savedPanes = saved.layouts[layoutKey];
						const expectedLength = LAYOUT_CONFIG[layoutKey as LayoutType];
						if (Array.isArray(savedPanes)) {
							const newPanes = [...savedPanes];
							while (newPanes.length < expectedLength) {
								newPanes.push(null);
							}
							newLayouts[layoutKey as LayoutType] = newPanes.slice(0, expectedLength);
						}
					}
				}
				layouts = newLayouts;

				const newIntervals = { ...paneIntervals };
				for (const layoutKey in saved.intervals) {
					if (Object.prototype.hasOwnProperty.call(LAYOUT_CONFIG, layoutKey)) {
						const savedIntervals = saved.intervals[layoutKey];
						const expectedLength = LAYOUT_CONFIG[layoutKey as LayoutType];
						const defaultInterval = '5m';
						if (Array.isArray(savedIntervals)) {
							const newIntervalPanes = [...savedIntervals];
							while (newIntervalPanes.length < expectedLength) {
								newIntervalPanes.push(defaultInterval);
							}
							newIntervals[layoutKey as LayoutType] = newIntervalPanes.slice(
								0,
								expectedLength
							);
						}
					}
				}
				paneIntervals = newIntervals;
			} else if (saved.layouts) {
				// Backward compatibility for old layout format
				const newLayouts = { ...layouts };
				for (const layoutKey in saved.layouts) {
					if (Object.prototype.hasOwnProperty.call(LAYOUT_CONFIG, layoutKey)) {
						const savedPanes = saved.layouts[layoutKey];
						const expectedLength = LAYOUT_CONFIG[layoutKey as LayoutType];
						if (Array.isArray(savedPanes)) {
							const newPanes = [...savedPanes];
							while (newPanes.length < expectedLength) {
								newPanes.push(null);
							}
							newLayouts[layoutKey as LayoutType] = newPanes.slice(0, expectedLength);
						}
					}
				}
				layouts = newLayouts;
			}

			const tokensInLayouts = new Set<number>();
			Object.values(layouts)
				.flat()
				.forEach((token) => token && tokensInLayouts.add(token));
			for (const token of tokensInLayouts) {
				if (!subscribedInstruments.has(token)) {
					// A minimal instrument object is needed for the subscription map
					subscribe({ instrument_token: token, tradingsymbol: `TOKEN ${token}` } as Instrument);
				}
			}
		} catch (e) {
			console.warn('Could not fetch nfo-charts-layouts.', e);
		}


		// Subscribe to all required tokens via websocket for LTP
		const allTokens = Array.from(subscribedInstruments.keys());
		if (allTokens.length > 0) {
			marketwatch.subscribeToInstruments(allTokens, 'full');
		}
		marketwatch.connect();
	});

	onDestroy(() => {
		// No SSE connections to close anymore
	});

	function formatChange(change: number | undefined): string {
		if (change === undefined || change === null) return 'N/A';
		return `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
	}
</script>

<div class="w-full flex flex-col gap-4 flex-grow">
	<Tabs.Root bind:value={activeTab} class="w-full flex flex-col flex-grow">
		<Tabs.List class="grid grid-cols-7 gap-2 w-full">
			{#each Object.keys(LAYOUT_CONFIG) as layout}
				<Tabs.Trigger value={layout}>{layout.charAt(0).toUpperCase() + layout.slice(1)}</Tabs.Trigger>
			{/each}
			<Tabs.Trigger value="instruments">Instruments</Tabs.Trigger>
		</Tabs.List>

		<Tabs.Content value="instruments" class="flex-grow pt-2">
			<div class="p-4 border rounded-lg">
				<div class="flex items-center justify-between mb-4">
					<h2 class="text-xl font-semibold">Instrument Search &amp; Subscriptions</h2>
					<p>
						WebSocket:
						<span
							class="font-semibold {websocketStatus === 'CONNECTED'
								? 'text-green-500'
								: 'text-red-500'}"
							>{websocketStatus}</span
						>
					</p>
				</div>

				<div class="grid grid-cols-1 md:grid-cols-2 gap-6">
					<div>
						<input
							type="text"
							bind:value={searchInput}
							placeholder="Search to subscribe (e.g., NIFTY, BANKNIFTY)"
							class="border p-2 rounded w-full"
						/>
						{#if searchResults.length > 0}
							<div class="mt-2 border rounded shadow-md max-h-48 overflow-y-auto">
								{#each searchResults as instrument (instrument.instrument_token)}
									<div
										class="flex justify-between items-center p-2 border-b last:border-b-0 hover:bg-muted/50"
									>
										<div>
											<p class="font-medium">{instrument.tradingsymbol}</p>
											<p class="text-sm text-muted-foreground">{instrument.name}</p>
										</div>
										<button
											on:click={() => subscribe(instrument)}
											disabled={subscribedInstruments.has(instrument.instrument_token)}
											class="bg-primary text-primary-foreground p-2 rounded text-sm disabled:bg-muted"
										>
											{subscribedInstruments.has(instrument.instrument_token) ? 'Added' : 'Add'}
										</button>
									</div>
								{/each}
							</div>
						{/if}
					</div>

					<div>
						<h3 class="font-semibold mb-2">Subscribed Instruments</h3>
						<div class="max-h-48 overflow-y-auto space-y-2">
							{#if subscribedInstruments.size === 0}
								<p class="text-muted-foreground text-sm">No instruments subscribed for charting.</p>
							{:else}
								{#each Array.from(subscribedInstruments.values()) as instrument (instrument.instrument_token)}
									<div class="flex items-center justify-between p-2 border rounded">
										<div>
											<p class="font-semibold">{instrument.tradingsymbol}</p>
											<p class="text-xs text-muted-foreground">
												LTP: {liveTicks.get(instrument.instrument_token)?.last_price?.toFixed(2) ??
													'--'}
												<span
													class={liveTicks.get(instrument.instrument_token)?.change ?? 0 >= 0
														? 'text-green-600'
														: 'text-red-600'}
												>
													({formatChange(liveTicks.get(instrument.instrument_token)?.change)})
												</span>
											</p>
										</div>
										<div class="flex items-center gap-2">
											<button
												on:click={() => unsubscribe(instrument.instrument_token)}
												class="bg-destructive text-destructive-foreground p-1 px-2 rounded text-xs"
											>
												Remove
											</button>
										</div>
									</div>
								{/each}
							{/if}
						</div>
					</div>
				</div>
			</div>
		</Tabs.Content>

		{#each Object.entries(LAYOUT_CONFIG) as [layout, count]}
			<Tabs.Content value={layout} class="flex-grow pt-2">
				<div
					class="grid gap-2 w-full h-full"
					style:grid-template-columns={count > 1 && count !== 4 && count !== 6 && count !== 8 ? `repeat(${Math.min(count, 3)}, 1fr)` : count === 4 ? 'repeat(2, 1fr)' : count === 6 ? 'repeat(3, 1fr)' : '1fr'}
					style:grid-template-rows={count > 3 ? 'repeat(2, 1fr)' : '1fr'}
				>
					{#each { length: count } as _, i}
						{@const token = layouts[layout as LayoutType][i]}
						{@const instrument = token ? subscribedInstruments.get(token) : null}
						{@const interval = currentPaneIntervals[i]}
						{@const key = token ? getKey(token, interval) : ''}
						{@const status = ingestionStatus.get(key)}

						<div class="w-full h-full rounded-lg border flex flex-col">
							<div class="p-2 border-b flex items-center gap-2 flex-wrap">
								<div class="flex-grow min-w-[150px]">
									<TickerSearch
										value={instrument}
										on:select={(e) => handlePaneSelect(e.detail, i)}
									/>
								</div>
								{#if token}
									<select
										class="w-[100px] h-8 text-xs border rounded-md bg-transparent p-1"
										value={interval}
										on:change={(e) => {
											if (e.currentTarget.value) {
												handleIntervalChange(e.currentTarget.value, i);
											}
										}}
									>
										{#each SUPPORTED_INTERVALS as { value, label }}
											<option {value}>{label}</option>
										{/each}
									</select>
									<Button variant="ghost" size="sm" on:click={() => loadOlderCandles(token, interval)}
										>Load Older</Button
									>
									<Button variant="ghost" size="sm" on:click={() => refreshCandles(token, interval)}
										>Refresh</Button
									>
									<Button variant="ghost" size="sm" on:click={() => clearPane(i)}>Clear</Button>
									{#if status}
										<span
											class="text-xs px-2 py-1 rounded-md
                        {status === 'triggered'
												? 'bg-blue-100 text-blue-800'
												: status === 'up_to_date'
													? 'bg-green-100 text-green-800'
													: 'bg-gray-100 text-gray-800'}"
										>
											{status.replace('_', ' ')}
										</span>
									{/if}
								{/if}
							</div>
							<div class="flex-grow relative">
								{#if token}
									{@const series = getChartSeriesForToken(token, interval)}
									{#if series.length > 0}
										<LightweightChart {series} fitContentOnInit={true} />
									{:else}
										<div class="flex items-center justify-center h-full text-muted-foreground">
											Loading data...
										</div>
									{/if}
								{:else}
									<div class="flex items-center justify-center h-full text-muted-foreground">
										Select an instrument
									</div>
								{/if}
							</div>
						</div>
					{/each}
				</div>
			</Tabs.Content>
		{/each}
	</Tabs.Root>
</div>
