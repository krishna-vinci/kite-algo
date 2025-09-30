<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { optionsStore, type OptionChainSnapshot, type PerExpiryData } from '$lib/stores/options';
	import { Button } from '$lib/components/ui/button/';
	import * as Select from '$lib/components/ui/select/index.js';

	export let underlyingSymbol: string;

	let snapshot: OptionChainSnapshot | null = null;
	let status: string = 'idle';
	let selectedExpiry: string | null = null;
	let currentExpiryData: PerExpiryData | null = null;

	const unsubscribe = optionsStore.subscribe((state) => {
		snapshot = state.snapshot;
		status = state.status;
		if (snapshot && snapshot.expiries.length > 0) {
			if (!selectedExpiry || !snapshot.expiries.includes(selectedExpiry)) {
				selectedExpiry = snapshot.expiries[0];
			}
			currentExpiryData = snapshot.per_expiry[selectedExpiry];
		} else {
			currentExpiryData = null;
		}
	});

	onMount(() => {
		if (underlyingSymbol) {
			optionsStore.connect(underlyingSymbol);
		}
	});

	onDestroy(() => {
		optionsStore.disconnect();
		unsubscribe();
	});

	function formatVal(val: number | null | undefined, dp: number = 2): string {
		if (val === null || val === undefined) return '-';
		return val.toFixed(dp);
	}
</script>

<div class="container mx-auto p-4 bg-background text-foreground">
	{#if status === 'loading'}
		<p>Loading option chain for {underlyingSymbol}...</p>
	{:else if status === 'error'}
		<p class="text-red-500">Error loading option chain.</p>
	{:else if snapshot}
		<div class="controls-header flex items-center space-x-4 mb-4 p-2 rounded-lg">
			<h1 class="text-xl font-bold">{snapshot.underlying}</h1>
			<span class="font-semibold text-lg">
				{snapshot.spot_ltp ? formatVal(snapshot.spot_ltp, 2) : '--'}
			</span>
			<div class="expiry-selector">
				<Select.Root bind:value={selectedExpiry}>
					<Select.Trigger class="w-[180px]">
						<Select.Value placeholder="Select expiry" />
					</Select.Trigger>
					<Select.Content>
						{#each snapshot.expiries as expiry}
							<Select.Item value={expiry}>{expiry}</Select.Item>
						{/each}
					</Select.Content>
				</Select.Root>
			</div>
			<span class="text-sm">Fut Price: {formatVal(currentExpiryData?.forward, 2)}</span>
			<span class="text-sm">IV: {formatVal((currentExpiryData?.sigma_expiry ?? 0) * 100, 2)}%</span>
		</div>

		<div class="option-chain-table-wrapper overflow-x-auto">
			<table class="min-w-full border-collapse">
				<thead>
					<tr class="bg-muted">
						<th class="p-2 text-center text-xs font-semibold uppercase" colspan="6">Calls</th>
						<th class="p-2 text-center text-xs font-semibold uppercase bg-background"
							>Strike</th
						>
						<th class="p-2 text-center text-xs font-semibold uppercase" colspan="6">Puts</th>
					</tr>
					<tr class="bg-muted/50">
						<!-- Calls Headers -->
						<th class="p-2 text-xs font-medium text-muted-foreground">IV</th>
						<th class="p-2 text-xs font-medium text-muted-foreground">Vega</th>
						<th class="p-2 text-xs font-medium text-muted-foreground">Theta</th>
						<th class="p-2 text-xs font-medium text-muted-foreground">Delta</th>
						<th class="p-2 text-xs font-medium text-muted-foreground">LTP</th>
						<th class="p-2 text-xs font-medium text-muted-foreground">OI</th>

						<!-- Strike Header -->
						<th class="p-2 text-xs font-medium bg-background"></th>

						<!-- Puts Headers -->
						<th class="p-2 text-xs font-medium text-muted-foreground">OI</th>
						<th class="p-2 text-xs font-medium text-muted-foreground">LTP</th>
						<th class="p-2 text-xs font-medium text-muted-foreground">Delta</th>
						<th class="p-2 text-xs font-medium text-muted-foreground">Theta</th>
						<th class="p-2 text-xs font-medium text-muted-foreground">Vega</th>
						<th class="p-2 text-xs font-medium text-muted-foreground">IV</th>
					</tr>
				</thead>
				<tbody>
					{#if currentExpiryData}
						{#each currentExpiryData.rows as row}
							{@const isAtm = row.strike === currentExpiryData.atm_strike}
							<tr
								class="border-b border-border {isAtm ? 'bg-muted' : 'hover:bg-muted/50'}"
							>
								<!-- Call Data -->
								<td class="p-2 text-center text-sm">{formatVal(row.CE?.iv, 4)}</td>
								<td class="p-2 text-center text-sm">{formatVal(row.CE?.vega, 2)}</td>
								<td class="p-2 text-center text-sm">{formatVal(row.CE?.theta, 2)}</td>
								<td class="p-2 text-center text-sm">{formatVal(row.CE?.delta, 2)}</td>
								<td class="p-2 text-center text-sm font-semibold text-primary"
									>{formatVal(row.CE?.ltp, 2)}</td
								>
								<td class="p-2 text-center text-sm">-</td>

								<!-- Strike Price -->
								<td
									class="p-2 text-center text-sm font-bold bg-background"
									class:text-primary={isAtm}>{row.strike}</td
								>

								<!-- Put Data -->
								<td class="p-2 text-center text-sm">-</td>
								<td class="p-2 text-center text-sm font-semibold text-primary"
									>{formatVal(row.PE?.ltp, 2)}</td
								>
								<td class="p-2 text-center text-sm">{formatVal(row.PE?.delta, 2)}</td>
								<td class="p-2 text-center text-sm">{formatVal(row.PE?.theta, 2)}</td>
								<td class="p-2 text-center text-sm">{formatVal(row.PE?.vega, 2)}</td>
								<td class="p-2 text-center text-sm">{formatVal(row.PE?.iv, 4)}</td>
							</tr>
						{/each}
					{/if}
				</tbody>
			</table>
		</div>
	{:else}
		<p>Waiting for data...</p>
	{/if}
</div>