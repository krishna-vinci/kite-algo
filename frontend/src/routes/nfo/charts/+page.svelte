<script lang="ts">
	console.log('🚀 [SCRIPT] NFO Charts component script loading...');
	
	import { onMount, onDestroy } from 'svelte';
	import { browser } from '$app/environment';
	import LightweightChart from '$lib/components/charts/LightweightChart.svelte';
	import TickerSearch from '$lib/components/TickerSearch.svelte';
	import type { Candle, LinePoint, SeriesSpec } from '$lib/components/charts/types';
	import {
		fetchCandles,
		getUserWatchlist,
		upsertUserWatchlist,
		getApiBase,
		clearCandleCache
	} from '$lib/api';
	import type { WatchlistInstrument } from '$lib/api';
	import type { CandlesResponse } from '$lib/api';
	import { CandleStreamManager, parseRawCandles, parseRawCandle } from '$lib/candleStream';
	import type { StreamState } from '$lib/candleStream';
	import { marketwatch } from '$lib/stores/marketwatch';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Tabs from '$lib/components/ui/tabs/index.js';
	import type { InstrumentRow } from '$lib/types';

	console.log('✅ [SCRIPT] All imports loaded successfully');

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
	let streamStates = $state(new Map<string, StreamState>());

	// Stream manager
	let streamManager: CandleStreamManager | null = null;

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
		console.log(`[LoadCandles] Fetching ${key}...`, opts);
		try {
			const response = await fetchCandles(token, {
				timeframe: interval,
				from: opts.from,
				to: opts.to,
				ingest: true
			});

			console.log(`[LoadCandles] Response for ${key}:`, {
				candles: response.candles.length,
				ingestion: response.ingestion.status,
				meta: response.meta
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
				console.log(`[LoadCandles] Replacing with ${finalCandles.length} candles`);
				// Clear cache on replace to force full recompute
				chartDataCache.delete(key);
			} else {
				// Merge, avoiding duplicates
				const existingTimes = new Set(existingCandles.map((c) => c.time));
				const uniqueNew = newCandles.filter((c) => !existingTimes.has(c.time));
				finalCandles = [...existingCandles, ...uniqueNew].sort((a, b) => a.time - b.time);
				console.log(`[LoadCandles] Merged: ${existingCandles.length} existing + ${uniqueNew.length} new = ${finalCandles.length} total`);
			}

			if (finalCandles.length > MAX_CANDLE_HISTORY) {
				finalCandles = finalCandles.slice(finalCandles.length - MAX_CANDLE_HISTORY);
			}

			candlesByTokenAndInterval.get(token)!.set(interval, finalCandles);
			candlesByTokenAndInterval = new Map(candlesByTokenAndInterval);
			console.log(`[LoadCandles] Stored ${finalCandles.length} candles for ${key}`);
		} catch (error) {
			console.error(`[LoadCandles] Failed to fetch candles for ${key}:`, error);
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

	// Cache for normalized chart data to maintain array references
	const chartDataCache = new Map<string, {
		candles: any[];
		ema: any[];
		rsi: any[];
		sourceLength: number;
	}>();

	// --- Chart Data Derivation ---
	function getChartSeriesForToken(token: number | null, interval: string | null): SeriesSpec[] {
		if (!token || !interval) return [];
		const candles = candlesByTokenAndInterval.get(token)?.get(interval) || [];
		console.log(`[ChartSeries] Getting series for ${token}|${interval}: ${candles.length} candles`);
		if (candles.length === 0) return [];

		const key = getKey(token, interval);
		const cached = chartDataCache.get(key);
		
		// IST offset: +5 hours 30 minutes = 19800 seconds
		const IST_OFFSET = 19800;

		// Check if we can reuse cached data (only last candle changed)
		if (cached && cached.sourceLength === candles.length) {
			// Update only the last candle in place
			const lastSourceCandle = candles[candles.length - 1];
			const lastCachedCandle = cached.candles[cached.candles.length - 1];
			
			lastCachedCandle.time = Math.round(Number(lastSourceCandle.time)) + IST_OFFSET;
			lastCachedCandle.open = Number(lastSourceCandle.open);
			lastCachedCandle.high = Number(lastSourceCandle.high);
			lastCachedCandle.low = Number(lastSourceCandle.low);
			lastCachedCandle.close = Number(lastSourceCandle.close);
			
			// Recompute indicators (they're small)
			cached.ema = generateEma(candles);
			cached.rsi = generateRsi(candles);
		} else {
			// Full recompute (new candles added or first load)
			const normalizedCandles = candles
				.map((c) => ({
					time: Math.round(Number(c.time)) + IST_OFFSET,
					open: Number(c.open),
					high: Number(c.high),
					low: Number(c.low),
					close: Number(c.close)
				}))
				.sort((a, b) => a.time - b.time);

			chartDataCache.set(key, {
				candles: normalizedCandles,
				ema: generateEma(candles),
				rsi: generateRsi(candles),
				sourceLength: candles.length
			});
		}

		const data = chartDataCache.get(key)!;
		
		// Use 'setData' for full replacement, 'updateLast' only when we know only last candle changed
		const mode = cached && cached.sourceLength === candles.length ? 'updateLast' : undefined;
		
		console.log(`[ChartSeries] Returning ${data.candles.length} candles with mode: ${mode || 'setData'}`);
		
		return [
			{ 
				id: 'candles', 
				type: 'candlestick', 
				data: data.candles,
				dataMode: mode // Use updateLast only for tick updates, not initial load
			},
			{ 
				id: 'ema', 
				type: 'line', 
				data: data.ema, 
				options: { color: '#2962FF', lineWidth: 2 },
				dataMode: mode
			},
			{ 
				id: 'rsi', 
				type: 'line', 
				data: data.rsi, 
				options: { color: '#f44336', lineWidth: 2, pane: 1 },
				dataMode: mode
			}
		];
	}

	// --- Indicator Helpers ---
	function generateEma(candles: Candle[], period = 20): LinePoint[] {
		if (candles.length < period) return [];
		// IST offset: +5 hours 30 minutes = 19800 seconds
		const IST_OFFSET = 19800;
		const ema: LinePoint[] = [];
		let sum = 0;
		for (let i = 0; i < period; i++) sum += candles[i].close;
		let lastEma = sum / period;
		ema.push({ time: Number(candles[period - 1].time) + IST_OFFSET, value: lastEma });
		for (let i = period; i < candles.length; i++) {
			const multiplier = 2 / (period + 1);
			const currentEma = (candles[i].close - lastEma) * multiplier + lastEma;
			ema.push({ time: Number(candles[i].time) + IST_OFFSET, value: currentEma });
			lastEma = currentEma;
		}
		return ema;
	}

	function generateRsi(candles: Candle[], period = 14): LinePoint[] {
		if (candles.length <= period) return [];
		// IST offset: +5 hours 30 minutes = 19800 seconds
		const IST_OFFSET = 19800;
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
			rsi.push({ time: Number(candles[i].time) + IST_OFFSET, value: 100 - 100 / (1 + rs) });
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

	let saveWatchlistTimeout: ReturnType<typeof setTimeout>;
	function saveWatchlist() {
		if (!browser) return;
		clearTimeout(saveWatchlistTimeout);
		saveWatchlistTimeout = setTimeout(async () => {
			try {
				const instruments: WatchlistInstrument[] = Array.from(subscribedInstruments.values()).map(inst => ({
					instrument_token: inst.instrument_token,
					tradingsymbol: inst.tradingsymbol,
					name: inst.name,
					exchange: inst.exchange,
					instrument_type: inst.instrument_type
				}));
				await upsertUserWatchlist(instruments, 'nfo-charts', true);
			} catch (e) {
				console.warn('Failed to save watchlist:', e);
			}
		}, 1000);
	}

	let saveLayoutsTimeout: ReturnType<typeof setTimeout>;
	function saveLayouts() {
		if (!browser) return;
		clearTimeout(saveLayoutsTimeout);
		saveLayoutsTimeout = setTimeout(() => {
			try {
				const layoutData = {
					version: 2,
					layouts,
					intervals: paneIntervals
				};
				localStorage.setItem('nfo-charts-layouts', JSON.stringify(layoutData));
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
		saveWatchlist();
		marketwatch.subscribeToInstruments([token], 'full');
	}

	function unsubscribe(token: number) {
		marketwatch.unsubscribeFromInstruments([token]);
		subscribedInstruments.delete(token);
		subscribedInstruments = new Map(subscribedInstruments);
		saveWatchlist();
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

	// --- Stream Management ---
	function startStream(token: number, interval: string) {
		console.log(`[startStream] Called for ${token}|${interval}`);
		
		if (!streamManager) {
			console.error('[startStream] streamManager is null!');
			return;
		}
		if (!browser) {
			console.error('[startStream] Not in browser context!');
			return;
		}

		const key = getKey(token, interval);
		console.log(`[startStream] Key: ${key}`);

		// Unsubscribe if already subscribed
		if (streamManager.isSubscribed(token, interval)) {
			console.log(`[startStream] Already subscribed, unsubscribing first...`);
			streamManager.unsubscribe(token, interval);
		}

		console.log(`[startStream] Calling streamManager.subscribe...`);
		streamManager.subscribe(token, interval, {
			onSnapshot: (snapshot) => {
				console.log(`[Stream] Snapshot received for ${key}:`, snapshot.candles.length, 'candles');
				const newCandles = parseRawCandles(snapshot.candles);
				if (newCandles.length > 0) {
					if (!candlesByTokenAndInterval.has(token)) {
						candlesByTokenAndInterval.set(token, new Map());
					}
					
					// MERGE snapshot with existing data (don't replace!)
					const existingCandles = candlesByTokenAndInterval.get(token)!.get(interval) ?? [];
					if (existingCandles.length === 0) {
						// No existing data, use snapshot
						candlesByTokenAndInterval.get(token)!.set(interval, newCandles);
					} else {
						// Merge: add any candles from snapshot that we don't have
						const existingTimes = new Set(existingCandles.map(c => c.time));
						const uniqueNew = newCandles.filter(c => !existingTimes.has(c.time));
						if (uniqueNew.length > 0) {
							const merged = [...existingCandles, ...uniqueNew].sort((a, b) => a.time - b.time);
							candlesByTokenAndInterval.get(token)!.set(interval, merged);
							console.log(`[Stream] Merged ${uniqueNew.length} new candles from snapshot`);
						}
					}
					candlesByTokenAndInterval = new Map(candlesByTokenAndInterval);
				}
			},
			onTick: (tickEvent) => {
				// Real-time tick-level updates for the FORMING candle
				const candle = parseRawCandle(tickEvent.candle);
				console.log(`[Stream] Tick update for ${key}:`, candle);
				if (!candlesByTokenAndInterval.has(token)) {
					candlesByTokenAndInterval.set(token, new Map());
				}
				const existingCandles = candlesByTokenAndInterval.get(token)!.get(interval) ?? [];
				
				// Find or create the forming candle
				const existingIndex = existingCandles.findIndex(c => c.time === candle.time);
				
				if (existingIndex >= 0) {
					// Update existing forming candle IN PLACE
					existingCandles[existingIndex] = candle;
				} else {
					// New forming candle
					existingCandles.push(candle);
					existingCandles.sort((a, b) => a.time - b.time);
					
					// Trim if exceeds max history
					if (existingCandles.length > MAX_CANDLE_HISTORY) {
						existingCandles.splice(0, existingCandles.length - MAX_CANDLE_HISTORY);
					}
				}
				
				// Trigger reactivity
				candlesByTokenAndInterval = new Map(candlesByTokenAndInterval);
			},
			onCandle: (candleEvent) => {
				// Completed candle
				const candle = parseRawCandle(candleEvent.candle);
				console.log(`[Stream] Candle completed for ${key}:`, candle);
				if (!candlesByTokenAndInterval.has(token)) {
					candlesByTokenAndInterval.set(token, new Map());
				}
				const existingCandles = candlesByTokenAndInterval.get(token)!.get(interval) ?? [];
				
				// Check if this candle already exists (update) or is new (append)
				const existingIndex = existingCandles.findIndex(c => c.time === candle.time);
				
				if (existingIndex >= 0) {
					// Update existing candle IN PLACE (keeps same array reference)
					console.log(`[Stream] Updating existing candle at index ${existingIndex}`);
					existingCandles[existingIndex] = candle;
				} else {
					// Append new candle
					console.log(`[Stream] Appending new candle`);
					existingCandles.push(candle);
					existingCandles.sort((a, b) => a.time - b.time);
					
					// Trim if exceeds max history
					if (existingCandles.length > MAX_CANDLE_HISTORY) {
						existingCandles.splice(0, existingCandles.length - MAX_CANDLE_HISTORY);
					}
				}
				
				// Trigger reactivity by creating a new Map reference
				candlesByTokenAndInterval = new Map(candlesByTokenAndInterval);
			},
			onStateChange: (state) => {
				console.log(`[Stream] State change for ${key}:`, state);
				streamStates.set(key, state);
				streamStates = new Map(streamStates);
			},
			onError: (error) => {
				console.error(`[Stream] Error for ${key}:`, error);
			}
		});
	}

	function stopStream(token: number, interval: string) {
		if (!streamManager) return;
		streamManager.unsubscribe(token, interval);
		const key = getKey(token, interval);
		streamStates.delete(key);
		streamStates = new Map(streamStates);
	}

	// --- Reactive Stream Management ---
	$effect(() => {
		console.log('[Effect] Stream management effect triggered');
		
		if (!streamManager) {
			console.warn('[Effect] streamManager not initialized');
			return;
		}
		if (!browser) {
			console.warn('[Effect] Not in browser');
			return;
		}

		// Get all active panes in current layout
		const panesToManage = layouts[activeLayout];
		const intervalsToManage = paneIntervals[activeLayout];
		
		console.log(`[Effect] Active layout: ${activeLayout}`, {
			panes: panesToManage,
			intervals: intervalsToManage
		});
		
		// IMPORTANT: Access candlesByTokenAndInterval to make this effect reactive to data changes
		const dataMap = candlesByTokenAndInterval;
		console.log('[Effect] Current data map:', {
			tokens: Array.from(dataMap.keys()),
			totalIntervals: Array.from(dataMap.values()).reduce((sum, m) => sum + m.size, 0)
		});
		
		// Start streams for visible panes
		panesToManage.forEach((token, i) => {
			if (token) {
				const interval = intervalsToManage[i];
				
				// Only start if we have candle data (historical loaded)
				const hasData = dataMap.get(token)?.has(interval);
				const candleCount = dataMap.get(token)?.get(interval)?.length || 0;
				const isSubscribed = streamManager.isSubscribed(token, interval);
				
				console.log(`[Effect] Pane ${i}: ${token}|${interval}`, {
					hasData,
					candleCount,
					isSubscribed,
					willStart: hasData && !isSubscribed
				});
				
				if (hasData && !isSubscribed) {
					console.log(`[Effect] ✅ Starting stream for ${token}|${interval} (${candleCount} candles loaded)`);
					startStream(token, interval);
				} else if (!hasData) {
					console.warn(`[Effect] ⚠️ No data loaded for ${token}|${interval} - stream not started`);
				} else if (isSubscribed) {
					console.log(`[Effect] ℹ️ Already subscribed to ${token}|${interval}`);
				}
			}
		});
	});

	async function refetchPane(paneIndex: number) {
		const token = layouts[activeLayout][paneIndex];
		if (!token) return;

		const interval = currentPaneIntervals[paneIndex];
		const key = getKey(token, interval);
		
		console.log(`[Refetch] Starting refetch for ${key}...`);

		try {
			// Step 1: Clear cache to force fresh data from Kite API
			console.log(`[Refetch] Clearing cache for ${token}...`);
			await clearCandleCache(token);
			
			// Step 2: Trigger ingestion with fresh fetch
			console.log(`[Refetch] Triggering ingestion for ${key}...`);
			const response = await fetchCandles(token, {
				timeframe: interval,
				ingest: true
			});

			console.log(`[Refetch] Initial response:`, {
				candles: response.candles.length,
				ingestion: response.ingestion.status
			});

			// If ingestion was triggered, poll until we have data
			if (response.ingestion.status === 'triggered') {
				console.log('[Refetch] Ingestion triggered, polling for data...');
				// Poll every 3 seconds for up to 45 seconds
				for (let i = 0; i < 15; i++) {
					await new Promise(resolve => setTimeout(resolve, 3000));
					console.log(`[Refetch] Poll attempt ${i + 1}/15...`);
					
					const retryResponse = await fetchCandles(token, {
						timeframe: interval,
						ingest: false
					});
					
					console.log(`[Refetch] Poll result: ${retryResponse.candles.length} candles`);
					
					if (retryResponse.candles.length > 10) { // Wait for substantial data
						console.log('[Refetch] Data received! Loading...');
						await loadCandles(token, interval, { replace: true });
						console.log(`[Refetch] Success! Loaded ${retryResponse.candles.length} candles`);
						break;
					}
				}
			} else if (response.candles.length > 0) {
				// Data already available
				console.log('[Refetch] Data already available, loading...');
				await loadCandles(token, interval, { replace: true });
				console.log(`[Refetch] Loaded ${response.candles.length} candles`);
			} else {
				console.warn('[Refetch] No data returned after ingestion');
			}
		} catch (error) {
			console.error(`[Refetch] Failed to refetch data for ${key}:`, error);
		}
	}

	onMount(async () => {
		console.log('[onMount] Component mounting...');
		if (!browser) return;

		// Initialize stream manager
		console.log('[onMount] Initializing CandleStreamManager...');
		streamManager = new CandleStreamManager();
		console.log('[onMount] StreamManager initialized:', streamManager);

		// Load watchlist
		try {
			const watchlistItems = await getUserWatchlist('nfo-charts');
			if (watchlistItems && watchlistItems.length > 0) {
				const instruments = new Map<number, Instrument>();
				watchlistItems.forEach((item) => {
					instruments.set(item.instrument_token, item as Instrument);
				});
				subscribedInstruments = instruments;
			}
		} catch (e) {
			console.warn('Could not fetch watchlist.', e);
		}

		// Load layouts and intervals from localStorage
		try {
			const savedStr = localStorage.getItem('nfo-charts-layouts');
			const saved = savedStr ? JSON.parse(savedStr) : {};
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
			
			// *** CRITICAL: Load historical data for saved layouts ***
			console.log('[Init] Loading historical data for saved layouts...', layouts);
			
			// Load data for active layout first (priority)
			const activeTokens = layouts[activeLayout];
			const activeIntervals = paneIntervals[activeLayout];
			console.log(`[Init] Active layout '${activeLayout}':`, activeTokens, activeIntervals);
			
			for (let i = 0; i < activeTokens.length; i++) {
				const token = activeTokens[i];
				const interval = activeIntervals[i];
				if (token && interval) {
					console.log(`[Init] Loading candles for pane ${i}: ${token}|${interval}`);
					// Load historical data for this pane
					loadCandles(token, interval, { replace: false }).catch(err => {
						console.warn(`Failed to load candles for ${token}|${interval}:`, err);
					});
				}
			}
			
			// Also load for other layouts (lower priority, in background)
			for (const [layoutKey, tokens] of Object.entries(layouts)) {
				if (layoutKey === activeLayout) continue; // Already loaded above
				const intervals = paneIntervals[layoutKey as LayoutType];
				for (let i = 0; i < tokens.length; i++) {
					const token = tokens[i];
					const interval = intervals[i];
					if (token && interval) {
						loadCandles(token, interval, { replace: false }).catch(err => {
							console.warn(`Failed to load candles for ${token}|${interval}:`, err);
						});
					}
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
		if (streamManager) {
			streamManager.unsubscribeAll();
			streamManager = null;
		}
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
						{@const streamState = streamStates.get(key)}

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
									<Button variant="ghost" size="sm" on:click={() => refetchPane(i)}>Refetch</Button>
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
									{#if streamState}
										<span
											class="text-xs px-2 py-1 rounded-md flex items-center gap-1
                        {streamState === 'connected'
												? 'bg-green-100 text-green-800'
												: streamState === 'connecting'
													? 'bg-yellow-100 text-yellow-800'
													: streamState === 'error'
														? 'bg-red-100 text-red-800'
														: 'bg-gray-100 text-gray-800'}"
										>
											{#if streamState === 'connected'}
												<span class="inline-block w-2 h-2 bg-green-600 rounded-full animate-pulse"></span>
												Live
											{:else if streamState === 'connecting'}
												<span class="inline-block w-2 h-2 bg-yellow-600 rounded-full"></span>
												Connecting
											{:else if streamState === 'error'}
												<span class="inline-block w-2 h-2 bg-red-600 rounded-full"></span>
												Error
											{:else}
												Disconnected
											{/if}
										</span>
									{/if}
								{/if}
							</div>
							<div class="flex-grow relative">
								{#if token}
									{@const series = getChartSeriesForToken(token, interval)}
									{#if series.length > 0}
										{@const isIntraday = interval !== '1d'}
										<LightweightChart
											{series}
											fitContentOnInit={true}
											options={{
												timeScale: {
													timeVisible: isIntraday,
													secondsVisible: false
												}
											}}
										/>
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
