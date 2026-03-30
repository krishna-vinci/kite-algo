<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { marketwatch } from '$lib/stores/marketwatch';
	import type { PageData } from './$types';
	import { getApiBase } from '$lib/api';
	import type { NiftyInstrument, Sectors, SnapshotEntry } from '$lib/types';
	import { Chart } from 'svelte-echarts';
	import { init, use } from 'echarts/core';
	import { format } from 'echarts';
	import { TreemapChart } from 'echarts/charts';
	import { TooltipComponent, TitleComponent } from 'echarts/components';
	import { CanvasRenderer } from 'echarts/renderers';
	import MarketMovers from '$lib/components/MarketMovers.svelte';

	use([TreemapChart, TooltipComponent, TitleComponent, CanvasRenderer]);

	export let data: PageData;

	// --- Data Structures ---
	const allInstruments: NiftyInstrument[] = Object.values(data.sectors as Sectors).flat();
	const baselineByToken = new Map(allInstruments.map((inst) => [inst.instrument_token, inst]));
	let overlayByToken = new Map<
		number,
		{ last_price?: number; change_percent?: number; tick_timestamp?: number }
	>();
	let liveSectors: Sectors = {};
	let options = {};
	let status = 'Baseline-only'; // Initial status

	// --- Movers and Sectors ---
	let topGainers: NiftyInstrument[] = [];
	let topLosers: NiftyInstrument[] = [];
	let topByWeight: NiftyInstrument[] = [];
	let topSectors: { name: string; attribution: number; weight: number }[] = [];

	// --- Connection Management ---
	let updateMode: 'auto' | 'ws' | 'http' | 'baseline' = 'auto';
	let wsUnsubscribe: (() => void) | null = null;
	let httpPollInterval: ReturnType<typeof setInterval> | null = null;

	// --- Configuration ---
	const overlayPollIntervalMs = 4000; // Poll every 4 seconds

	// --- Derived Data Computation ---
	function computeDerivedRows() {
		const newSectors: Sectors = {};
		let total_ff_live = 0;

		// First pass: compute individual metrics and total free-float market cap
		for (const [sector, instruments] of Object.entries(data.sectors as Sectors)) {
			newSectors[sector] = [];
			for (const baseline of instruments) {
				const overlay = overlayByToken.get(baseline.instrument_token) || {};
				const baseline_close = baseline.close ?? 0;

				let r = 0;
				if (overlay.change_percent !== undefined && isFinite(overlay.change_percent)) {
					r = overlay.change_percent / 100;
				} else if (
					overlay.last_price !== undefined &&
					isFinite(overlay.last_price) &&
					baseline_close > 0
				) {
					r = overlay.last_price / baseline_close - 1;
				} else if (baseline.net_change_percent != null && isFinite(baseline.net_change_percent)) {
					r = baseline.net_change_percent / 100;
				}

				const ltp_live = overlay.last_price ?? baseline.ltp ?? baseline_close;
				const ff_mc_live = (baseline.freefloat_marketcap || 0) * (1 + r);

				if (isFinite(ff_mc_live)) {
					total_ff_live += ff_mc_live;
				}

				const change_percent_live = 100 * r;
				// Return attribution in percentage points (pp).
				// This is the stock's contribution to the index's percentage change.
				// Formula: Stock Weight (%) * Stock Return (%)
				const attribution_pp = (baseline.index_weight || 0) * r;

				newSectors[sector].push({
					...baseline,
					ltp_live,
					change_percent_live: isFinite(change_percent_live) ? change_percent_live : 0,
					ff_mc_live: isFinite(ff_mc_live) ? ff_mc_live : 0,
					attribution_pp: isFinite(attribution_pp) ? attribution_pp : 0
				});
			}
		}

		// Second pass: compute live index weight
		liveSectors = Object.fromEntries(
			Object.entries(newSectors).map(([sector, instruments]) => {
				const newInstruments = instruments.map((row: any) => {
					const weight_live = total_ff_live > 0 ? row.ff_mc_live / total_ff_live : undefined;
					return {
						...row,
						weight_live: weight_live !== undefined ? 100 * weight_live : row.index_weight
					};
				});
				return [sector, newInstruments];
			})
		);

		// Update status
		updateStatus();
	}

	function updateStatus() {
		if (updateMode !== 'auto') {
			status = `Manual (${updateMode.toUpperCase()})`;
			return;
		}
		const now = Date.now();
		let hasWsUpdates = false;
		let hasSnapshotUpdates = false;
		let staleSnapshots = 0;

		for (const overlay of overlayByToken.values()) {
			if (overlay.tick_timestamp) {
				hasSnapshotUpdates = true;
				if (now - overlay.tick_timestamp > 2 * overlayPollIntervalMs) {
					staleSnapshots++;
				}
			} else {
				hasWsUpdates = true; // WS ticks don't have a timestamp in this design
			}
		}

		if (hasWsUpdates) {
			status = 'Live (WebSocket)';
		} else if (hasSnapshotUpdates) {
			status = 'Live (HTTP)';
			if (staleSnapshots > overlayByToken.size / 2) {
				status += ' - Stale';
			}
		} else {
			status = 'Baseline-only';
		}
	}

	// --- Connection Logic ---
	function connectWs() {
		if (wsUnsubscribe) return;
		const tokens = allInstruments
			.map((inst) => inst.instrument_token)
			.filter((t) => t !== undefined && t !== null);
		if (tokens.length === 0) {
			console.warn('No tokens available for WebSocket subscription');
			return;
		}
		marketwatch.subscribeToInstruments(tokens, 'quote');
		marketwatch.connect();

		wsUnsubscribe = marketwatch.subscribe((value) => {
			let updated = false;
			for (const [tokenStr, tick] of Object.entries(value.instruments)) {
				const token = parseInt(tokenStr, 10);
				if (
					baselineByToken.has(token) &&
					tick.last_price !== undefined &&
					isFinite(tick.last_price) &&
					tick.last_price > 0
				) {
					overlayByToken.set(token, {
						last_price: tick.last_price,
						change_percent: tick.change
					});
					updated = true;
				}
			}
			if (updated) computeDerivedRows();
		});
	}

	function disconnectWs() {
		if (!wsUnsubscribe) return;
		const tokens = allInstruments.map((inst) => inst.instrument_token);
		marketwatch.unsubscribeFromInstruments(tokens);
		wsUnsubscribe();
		wsUnsubscribe = null;
	}

	function startHttpPoll() {
		if (httpPollInterval) return;
		const tokens = allInstruments
			.map((inst) => inst.instrument_token)
			.filter((t) => t !== undefined && t !== null);
		if (tokens.length === 0) {
			console.warn('No tokens available for HTTP polling');
			return;
		}
		httpPollInterval = setInterval(async () => {
			try {
				const tokensQuery = tokens.map((t) => `token=${t}`).join('&');
				const response = await fetch(
					`${getApiBase()}/api/marketwatch/nifty50/overlay-snapshot?${tokensQuery}`
				);
				if (response.ok) {
					const snapshotData = await response.json();
					if (snapshotData.status === 'success' && typeof snapshotData.data === 'object') {
						let updated = false;
						for (const [tokenStr, entry] of Object.entries(
							snapshotData.data as Record<string, SnapshotEntry>
						)) {
							const token = parseInt(tokenStr, 10);
							if (
								baselineByToken.has(token) &&
								entry.last_price !== undefined &&
								isFinite(entry.last_price) &&
								entry.last_price > 0
							) {
								overlayByToken.set(token, {
									last_price: entry.last_price,
									change_percent: entry.change_percent,
									tick_timestamp: entry.tick_timestamp
								});
								updated = true;
							}
						}
						if (updated) computeDerivedRows();
					}
				}
			} catch (error) {
				console.error('Failed to fetch overlay snapshot:', error);
			}
		}, overlayPollIntervalMs);
	}

	function stopHttpPoll() {
		if (!httpPollInterval) return;
		clearInterval(httpPollInterval);
		httpPollInterval = null;
	}

	function setUpdateMode(mode: 'auto' | 'ws' | 'http' | 'baseline') {
		updateMode = mode;

		disconnectWs();
		stopHttpPoll();

		if (mode === 'auto' || mode === 'ws') {
			connectWs();
		}
		if (mode === 'auto' || mode === 'http') {
			startHttpPoll();
		}
		if (mode === 'baseline') {
			overlayByToken.clear();
			computeDerivedRows();
		}
	}

	// --- Lifecycle Hooks ---
	onMount(() => {
		setUpdateMode('auto');

		// Initial computation
		computeDerivedRows();

		return () => {
			disconnectWs();
			stopHttpPoll();
		};
	});

	function getLevelOption() {
		return [
			{
				// Root level
				itemStyle: {
					borderColor: '#777',
					borderWidth: 0,
					gapWidth: 1
				},
				upperLabel: {
					show: false
				}
			},
			{
				// Sector level
				itemStyle: {
					borderWidth: 3,
					borderColor: '#333',
					gapWidth: 3
				}
			},
			{
				// Instrument level
				itemStyle: {
					gapWidth: 1
				}
			}
		];
	}

	$: {
		if (Object.keys(liveSectors).length > 0) {
			// --- Calculate Movers and Sectors ---
			const allLiveInstruments = Object.values(liveSectors).flat() as (NiftyInstrument & {
				ltp_live: number;
				change_percent_live: number;
				attribution_pp: number;
				ff_mc_live: number;
				weight_live: number;
			})[];

			const sortedByChange = [...allLiveInstruments].sort(
				(a, b) => (b.change_percent_live ?? 0) - (a.change_percent_live ?? 0)
			);
			topGainers = sortedByChange.slice(0, 5);
			topLosers = sortedByChange.slice(-5).reverse();

			topByWeight = [...allLiveInstruments]
				.sort((a, b) => (b.weight_live ?? 0) - (a.weight_live ?? 0))
				.slice(0, 5);

			topSectors = Object.entries(liveSectors)
				.map(([sector, instruments]) => {
					const sectorAttribution = instruments.reduce(
						(sum, inst) => sum + ((inst as any).attribution_pp ?? 0),
						0
					);
					const sectorWeight = instruments.reduce(
						(sum, inst) => sum + ((inst as any).weight_live ?? 0),
						0
					);
					return { name: sector, attribution: sectorAttribution, weight: sectorWeight };
				})
				.sort((a, b) => b.attribution - a.attribution)
				.slice(0, 5);

			const treemapData = {
				name: 'Nifty 50',
				children: Object.entries(liveSectors).map(([sector, instruments]) => ({
					name: sector,
					children: [...instruments]
						.sort((a, b) => (b as any).attribution_pp - (a as any).attribution_pp)
						.map((instrument) => {
							const change = (instrument as any).change_percent_live || 0;
							const returnAttribution = (instrument as any).attribution_pp || 0;
							return {
								name: instrument.tradingsymbol,
								value: (instrument as any).ff_mc_live,
								change_percent_live: change,
								attribution_pp: returnAttribution,
								itemStyle: {
									color: change > 0 ? '#269f3c' : change < 0 ? '#942e38' : '#aaa'
								}
							};
						})
				}))
			};
			options = {
				title: {
					text: 'Nifty 50 Market Cap',
					left: 'center'
				},
				tooltip: {
					formatter: function (info: any) {
						const { name, treePathInfo } = info;
						const { value, change_percent_live, attribution_pp } = info.data;

						if (change_percent_live === undefined || !isFinite(change_percent_live)) {
							// This is a sector or has invalid data
							const displayValue = isFinite(value) ? format.addCommas(Math.round(value)) : 'N/A';
							return `${name}<br/>Market Cap: ${displayValue}`;
						}

						const treePath = treePathInfo
							.map((item: any) => item.name)
							.slice(1)
							.join('/');

						return [
							`<div class="tooltip-title">${format.encodeHTML(treePath)}</div>`,
							`Market Cap: ${format.addCommas(Math.round(value))}`,
							`% Chg: ${change_percent_live.toFixed(2)}%`,
							`Return Attr: ${attribution_pp.toFixed(2)}`
						].join('<br/>');
					}
				},
				series: [
					{
						name: 'Nifty 50',
						type: 'treemap',
						visibleMin: 300,
						label: {
							show: true,
							formatter: (params: any) => {
								const { name, data } = params;
								if (data.change_percent_live !== undefined && isFinite(data.change_percent_live)) {
									return `${name}\n${data.change_percent_live.toFixed(2)}%`;
								}
								return name;
							}
						},
						upperLabel: {
							show: true,
							height: 30
						},
						itemStyle: {
							borderColor: 'black'
						},
						levels: getLevelOption(),
						data: [treemapData]
					}
				]
			};
		}
	}
</script>

<div class="container mx-auto p-4">
	<h1 class="text-2xl font-bold mb-4">
		Nifty 50
		<span class="text-sm font-normal ml-2">({status})</span>
	</h1>

	<div class="flex space-x-2 mb-4">
		<button
			class="px-3 py-1 text-sm font-medium rounded-md {updateMode === 'auto'
				? 'bg-blue-500 text-white'
				: 'bg-gray-200'}"
			on:click={() => setUpdateMode('auto')}>Auto</button
		>
		<button
			class="px-3 py-1 text-sm font-medium rounded-md {updateMode === 'ws'
				? 'bg-blue-500 text-white'
				: 'bg-gray-200'}"
			on:click={() => setUpdateMode('ws')}>WebSocket</button
		>
		<button
			class="px-3 py-1 text-sm font-medium rounded-md {updateMode === 'http'
				? 'bg-blue-500 text-white'
				: 'bg-gray-200'}"
			on:click={() => setUpdateMode('http')}>HTTP</button
		>
		<button
			class="px-3 py-1 text-sm font-medium rounded-md {updateMode === 'baseline'
				? 'bg-blue-500 text-white'
				: 'bg-gray-200'}"
			on:click={() => setUpdateMode('baseline')}>Baseline</button
		>
	</div>

	<p class="text-sm text-gray-600 dark:text-gray-400 mb-4">
		<b>Return Attribution:</b> A stock's contribution to the Nifty 50 index's daily percentage
		change. It's calculated as:
		<code class="text-xs bg-gray-200 dark:bg-gray-700 p-1 rounded"
			>Stock Index Weight (%) × Stock Return (%)</code
		>. The sum of attributions for all 50 stocks equals the index's total change.
	</p>

	{#if Object.keys(liveSectors).length > 0}
		<div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
			<div class="md:col-span-2" style="height: 600px;">
				<Chart {init} {options} />
			</div>
			<div class="md:col-span-1 space-y-4">
				<MarketMovers
					title="Top Gainers"
					items={topGainers}
					valueField="change_percent_live"
					unit="%"
				/>
				<MarketMovers
					title="Top Losers"
					items={topLosers}
					valueField="change_percent_live"
					unit="%"
				/>
				<MarketMovers
					title="Top Sectors by Impact"
					items={topSectors}
					valueField="attribution"
					nameField="name"
					unit="pp"
				/>
				<MarketMovers
					title="Top Stocks by Weight"
					items={topByWeight}
					valueField="weight_live"
					unit="%"
				/>
			</div>
		</div>
	{/if}

	{#each Object.entries(liveSectors) as [sector, instruments]}
		<details class="mb-4">
			<summary class="text-xl font-semibold cursor-pointer">{sector}</summary>
			<table class="min-w-full bg-white mt-2">
				<thead>
					<tr>
						<th class="py-2">Symbol</th>
						<th class="py-2">LTP</th>
						<th class="py-2">% Chg</th>
						<th class="py-2">Return Attribution</th>
						<th class="py-2">Freefloat Marketcap</th>
						<th class="py-2">Index Weight</th>
					</tr>
				</thead>
				<tbody>
					{#each instruments as instrument (instrument.instrument_token)}
						{@const liveInstrument = instrument as NiftyInstrument & {
							ltp_live: number;
							change_percent_live: number;
							attribution_pp: number;
							ff_mc_live: number;
							weight_live: number;
						}}
						<tr>
							<td class="border px-4 py-2">{liveInstrument.tradingsymbol}</td>
							<td class="border px-4 py-2">{liveInstrument.ltp_live?.toFixed(2) ?? '...'}</td>
							<td
								class="border px-4 py-2 {liveInstrument.change_percent_live >= 0
									? 'text-green-500'
									: 'text-red-500'}"
							>
								{liveInstrument.change_percent_live?.toFixed(2) ?? '0.00'}%
							</td>
							<td class="border px-4 py-2">{liveInstrument.attribution_pp?.toFixed(2) ?? '...'}</td>
							<td class="border px-4 py-2">
								{liveInstrument.ff_mc_live?.toLocaleString('en-IN', {
									maximumFractionDigits: 2
								}) ?? '...'}
							</td>
							<td class="border px-4 py-2">{liveInstrument.weight_live?.toFixed(2) ?? '...'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</details>
	{/each}
</div>
