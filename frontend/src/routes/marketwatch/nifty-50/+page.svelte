<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { marketwatch } from '$lib/stores/marketwatch';
	import type { PageData } from './$types';
	import { getApiBase } from '$lib/api';

	export let data: PageData;

	// --- Data Structures ---
	const baselineByToken = new Map(data.instruments.map((inst) => [inst.instrument_token, inst]));
	let overlayByToken = new Map<
		number,
		{ last_price?: number; change_percent?: number; tick_timestamp?: number }
	>();
	let liveRows = [];
	let status = 'Baseline-only'; // Initial status

	// --- Configuration ---
	const overlayPollIntervalMs = 4000; // Poll every 4 seconds
	let snapshotPollInterval: ReturnType<typeof setInterval>;

	// --- Derived Data Computation ---
	function computeDerivedRows() {
		const newRows = [];
		let total_ff_live = 0;

		// First pass: compute individual metrics and total free-float market cap
		for (const baseline of baselineByToken.values()) {
			const overlay = overlayByToken.get(baseline.instrument_token) || {};
			const baseline_close = baseline.ltp ?? 0;

			let r = 0;
			if (overlay.change_percent !== undefined && isFinite(overlay.change_percent)) {
				r = overlay.change_percent / 100;
			} else if (
				overlay.last_price !== undefined &&
				isFinite(overlay.last_price) &&
				baseline_close > 0
			) {
				r = overlay.last_price / baseline_close - 1;
			}

			const ltp_live = overlay.last_price ?? baseline_close;
			const ff_mc_live = (baseline.freefloat_marketcap || 0) * (1 + r);

			if (isFinite(ff_mc_live)) {
				total_ff_live += ff_mc_live;
			}

			newRows.push({
				...baseline,
				ltp_live,
				change_percent_live: 100 * r,
				ff_mc_live,
				attribution_pp: 100 * ((baseline.index_weight || 0) / 100) * r
			});
		}

		// Second pass: compute live index weight
		liveRows = newRows.map((row) => {
			const weight_live = total_ff_live > 0 ? row.ff_mc_live / total_ff_live : undefined;
			return {
				...row,
				weight_live: weight_live !== undefined ? 100 * weight_live : row.index_weight
			};
		});

		// Update status
		updateStatus();
	}

	function updateStatus() {
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

	// --- Lifecycle Hooks ---
	onMount(() => {
		const tokens = data.instruments.map((inst) => inst.instrument_token);
		marketwatch.subscribeToInstruments(tokens, 'quote');
		marketwatch.connect();

		const unsubscribe = marketwatch.subscribe((value) => {
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

		// HTTP Snapshot Polling
		snapshotPollInterval = setInterval(async () => {
			try {
				const tokensQuery = tokens.map((t) => `token=${t}`).join('&');
				const response = await fetch(
					`${getApiBase()}/api/marketwatch/nifty50/overlay-snapshot?${tokensQuery}`
				);
				if (response.ok) {
					const snapshotData = await response.json();
					if (snapshotData.status === 'success' && typeof snapshotData.data === 'object') {
						let updated = false;
						for (const [tokenStr, entry] of Object.entries(snapshotData.data)) {
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

		// Initial computation
		computeDerivedRows();

		return () => {
			marketwatch.unsubscribeFromInstruments(tokens);
			unsubscribe();
			clearInterval(snapshotPollInterval);
		};
	});
</script>

<div class="container mx-auto p-4">
	<h1 class="text-2xl font-bold mb-4">
		Nifty 50
		<span class="text-sm font-normal ml-2">({status})</span>
	</h1>

	<table class="min-w-full bg-white">
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
			{#each liveRows as instrument (instrument.instrument_token)}
				<tr>
					<td class="border px-4 py-2">{instrument.tradingsymbol}</td>
					<td class="border px-4 py-2">{instrument.ltp_live?.toFixed(2) ?? '...'}</td>
					<td
						class="border px-4 py-2 {instrument.change_percent_live >= 0
							? 'text-green-500'
							: 'text-red-500'}"
					>
						{instrument.change_percent_live?.toFixed(2) ?? '0.00'}%
					</td>
					<td class="border px-4 py-2">{instrument.attribution_pp?.toFixed(2) ?? '...'}</td>
					<td class="border px-4 py-2">{instrument.ff_mc_live?.toLocaleString('en-IN', { maximumFractionDigits: 2 }) ?? '...'}</td>
					<td class="border px-4 py-2">{instrument.weight_live?.toFixed(2) ?? '...'}</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>