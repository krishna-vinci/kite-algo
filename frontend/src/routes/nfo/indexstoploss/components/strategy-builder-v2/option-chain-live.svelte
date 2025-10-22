<script lang="ts">
	import { RefreshCw } from '@lucide/svelte';
	import type { OptionChainStrike } from '../../types';
	import type { CalculatedStrike } from '../../lib/strike-calculator';
	
	interface Props {
		strikes: OptionChainStrike[];
		atmStrike: number;
		selectedStrikes: CalculatedStrike[];
		loading?: boolean;
		onStrikeAdded: (strike: CalculatedStrike) => void;
		onQuickTrade: (strike: number, optionType: 'CE' | 'PE', transactionType: 'BUY' | 'SELL') => void;
	}
	
	let { strikes, atmStrike, selectedStrikes, loading = false, onStrikeAdded, onQuickTrade }: Props = $props();
	
	function isStrikeSelected(strike: number, optionType: 'CE' | 'PE'): boolean {
		return selectedStrikes.some(
			(s) => s.strike === strike && s.optionType === optionType
		);
	}
	
	function getSelectedStrike(strike: number, optionType: 'CE' | 'PE'): CalculatedStrike | undefined {
		return selectedStrikes.find(
			(s) => s.strike === strike && s.optionType === optionType
		);
	}
	
	function handleBuySell(strike: number, optionType: 'CE' | 'PE', transactionType: 'BUY' | 'SELL') {
		const calculatedStrike: CalculatedStrike = {
			strike,
			optionType,
			transactionType,
			strikeOffset: 0, // Will be calculated later if needed
			ltp: optionType === 'CE' 
				? strikes.find(s => s.strike === strike)?.ce?.ltp
				: strikes.find(s => s.strike === strike)?.pe?.ltp,
			delta: optionType === 'CE'
				? strikes.find(s => s.strike === strike)?.ce?.greeks?.delta
				: strikes.find(s => s.strike === strike)?.pe?.greeks?.delta
		};
		
		onStrikeAdded(calculatedStrike);
	}
</script>

<div class="rounded-lg border bg-card">
	<!-- Header -->
	<div class="flex items-center justify-between p-3 border-b">
		<h3 class="font-semibold text-sm">Option Chain</h3>
		{#if loading}
			<div class="flex items-center gap-2 text-xs text-muted-foreground">
				<RefreshCw class="h-3 w-3 animate-spin" />
				Loading...
			</div>
		{/if}
	</div>
	
	<!-- Table -->
	<div class="overflow-x-auto max-h-[500px]">
		<table class="w-full text-xs">
			<thead class="sticky top-0 bg-background border-b">
				<tr>
					<th colspan="6" class="py-2 text-center font-semibold text-red-600 border-r">CALLS</th>
					<th class="py-2 text-center font-semibold">STRIKE</th>
					<th colspan="6" class="py-2 text-center font-semibold text-green-600 border-l">PUTS</th>
				</tr>
				<tr class="bg-muted/50">
					<!-- CALLS Columns -->
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground">Gamma</th>
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground">Vega</th>
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground">Theta</th>
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground">Delta</th>
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground">OI</th>
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground border-r">LTP</th>
					<!-- STRIKE -->
					<th class="px-3 py-1.5 text-center font-medium">Strike</th>
					<!-- PUTS Columns -->
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground border-l">LTP</th>
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground">OI</th>
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground">Delta</th>
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground">Theta</th>
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground">Vega</th>
					<th class="px-2 py-1.5 text-right font-medium text-muted-foreground">Gamma</th>
				</tr>
			</thead>
			
			<tbody>
				{#each strikes as strike}
					{@const isATM = strike.strike === atmStrike}
					{@const ceSelected = isStrikeSelected(strike.strike, 'CE')}
					{@const peSelected = isStrikeSelected(strike.strike, 'PE')}
					{@const ceStrike = getSelectedStrike(strike.strike, 'CE')}
					{@const peStrike = getSelectedStrike(strike.strike, 'PE')}
					
					<tr class={`
						border-b transition-colors
						${isATM ? 'bg-yellow-50 dark:bg-yellow-950/20' : ''}
						${ceSelected || peSelected ? 'bg-primary/5' : 'hover:bg-muted/30'}
					`}>
						<!-- CALLS Data -->
						<td class="px-2 py-1.5 text-right font-mono text-xs text-muted-foreground">
							{strike.ce?.greeks?.gamma?.toFixed(4) || '—'}
						</td>
						<td class="px-2 py-1.5 text-right font-mono text-xs text-muted-foreground">
							{strike.ce?.greeks?.vega?.toFixed(2) || '—'}
						</td>
						<td class="px-2 py-1.5 text-right font-mono text-xs text-muted-foreground">
							{strike.ce?.greeks?.theta?.toFixed(2) || '—'}
						</td>
						<td class="px-2 py-1.5 text-right font-mono text-xs text-muted-foreground">
							{strike.ce?.greeks?.delta?.toFixed(2) || '—'}
						</td>
						<td class="px-2 py-1.5 text-right font-mono text-xs text-muted-foreground">
							{strike.ce?.oi ? (strike.ce.oi / 100000).toFixed(1) + 'L' : '—'}
						</td>
						<td 
							class={`px-2 py-1.5 text-right font-mono font-medium border-r cursor-pointer ${ceSelected ? 'bg-blue-100 dark:bg-blue-900/30 font-bold' : 'hover:bg-blue-50'}`}
							onclick={() => handleBuySell(strike.strike, 'CE', 'SELL')}
						>
							{strike.ce?.ltp?.toFixed(2) || '—'}
							{#if ceSelected}<span class="ml-1 text-blue-600">✓</span>{/if}
						</td>
						
						<!-- STRIKE -->
						<td class="px-3 py-1.5 text-center font-mono font-semibold">
							{strike.strike}
						</td>
						
						<!-- PUTS Data -->
						<td 
							class={`px-2 py-1.5 text-right font-mono font-medium border-l cursor-pointer ${peSelected ? 'bg-green-100 dark:bg-green-900/30 font-bold' : 'hover:bg-green-50'}`}
							onclick={() => handleBuySell(strike.strike, 'PE', 'SELL')}
						>
							{strike.pe?.ltp?.toFixed(2) || '—'}
							{#if peSelected}<span class="ml-1 text-green-600">✓</span>{/if}
						</td>
						<td class="px-2 py-1.5 text-right font-mono text-xs text-muted-foreground">
							{strike.pe?.oi ? (strike.pe.oi / 100000).toFixed(1) + 'L' : '—'}
						</td>
						<td class="px-2 py-1.5 text-right font-mono text-xs text-muted-foreground">
							{strike.pe?.greeks?.delta?.toFixed(2) || '—'}
						</td>
						<td class="px-2 py-1.5 text-right font-mono text-xs text-muted-foreground">
							{strike.pe?.greeks?.theta?.toFixed(2) || '—'}
						</td>
						<td class="px-2 py-1.5 text-right font-mono text-xs text-muted-foreground">
							{strike.pe?.greeks?.vega?.toFixed(2) || '—'}
						</td>
						<td class="px-2 py-1.5 text-right font-mono text-xs text-muted-foreground">
							{strike.pe?.greeks?.gamma?.toFixed(4) || '—'}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	
	{#if strikes.length === 0 && !loading}
		<div class="p-8 text-center text-muted-foreground">
			No option chain data available. Please select underlying and expiry.
		</div>
	{/if}
</div>
