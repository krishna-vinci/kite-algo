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
	<div class="flex items-center justify-between p-4 border-b">
		<h3 class="font-semibold">Option Chain</h3>
		{#if loading}
			<div class="flex items-center gap-2 text-sm text-muted-foreground">
				<RefreshCw class="h-4 w-4 animate-spin" />
				Loading...
			</div>
		{/if}
	</div>
	
	<!-- Table -->
	<div class="overflow-x-auto">
		<table class="w-full">
			<thead class="bg-muted/50">
				<tr>
					<th class="px-4 py-3 text-left text-sm font-medium border-r-2 border-border" colspan="4">
						CALL SIDE
					</th>
					<th class="px-4 py-3 text-center text-sm font-medium w-24">
						STRIKE
					</th>
					<th class="px-4 py-3 text-left text-sm font-medium border-l-2 border-border" colspan="4">
						PUT SIDE
					</th>
				</tr>
				<tr class="text-xs text-muted-foreground">
					<!-- Call Headers -->
					<th class="px-2 py-2 text-right">LTP</th>
					<th class="px-2 py-2 text-right">Δ</th>
					<th class="px-2 py-2 text-right">IV</th>
					<th class="px-2 py-2 text-center border-r-2 border-border">Actions</th>
					
					<!-- Strike -->
					<th class="px-2 py-2"></th>
					
					<!-- Put Headers -->
					<th class="px-2 py-2 text-center border-l-2 border-border">Actions</th>
					<th class="px-2 py-2 text-right">LTP</th>
					<th class="px-2 py-2 text-right">Δ</th>
					<th class="px-2 py-2 text-right">IV</th>
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
						border-t transition-colors
						${isATM ? 'bg-blue-50 dark:bg-blue-950/20' : ''}
						${ceSelected || peSelected ? 'bg-primary/5' : 'hover:bg-muted/30'}
					`}>
						<!-- CALL SIDE -->
						<td class="px-2 py-3 text-right text-sm font-mono">
							{strike.ce?.ltp?.toFixed(2) || '-'}
						</td>
						<td class="px-2 py-3 text-right text-xs text-muted-foreground">
							{strike.ce?.greeks?.delta?.toFixed(2) || '-'}
						</td>
						<td class="px-2 py-3 text-right text-xs text-muted-foreground">
							{strike.ce?.greeks?.iv?.toFixed(1) || '-'}%
						</td>
						<td class="px-2 py-3 border-r-2 border-border">
							<div class="flex items-center justify-center gap-1">
								{#if ceSelected && ceStrike}
									<div class="flex items-center gap-1 px-2 py-1 rounded bg-primary/10 text-xs font-medium">
										✓ {ceStrike.transactionType === 'BUY' ? 'B' : 'S'}
									</div>
								{:else}
									<button
										onclick={() => handleBuySell(strike.strike, 'CE', 'BUY')}
										class="px-2 py-1 text-xs font-medium rounded bg-green-500 hover:bg-green-600 text-white transition-colors"
										title="Buy Call"
									>
										🟢 B
									</button>
									<button
										onclick={() => handleBuySell(strike.strike, 'CE', 'SELL')}
										class="px-2 py-1 text-xs font-medium rounded bg-red-500 hover:bg-red-600 text-white transition-colors"
										title="Sell Call"
									>
										🔴 S
									</button>
									<button
										onclick={() => onQuickTrade(strike.strike, 'CE', 'SELL')}
										class="px-2 py-1 text-xs font-medium rounded bg-yellow-500 hover:bg-yellow-600 text-white transition-colors"
										title="Quick Trade"
									>
										⚡
									</button>
								{/if}
							</div>
						</td>
						
						<!-- STRIKE -->
						<td class="px-4 py-3 text-center font-bold">
							{#if isATM}
								<div class="flex items-center justify-center gap-2">
									<span class="text-blue-600">★</span>
									{strike.strike}
									<span class="text-xs text-muted-foreground">(ATM)</span>
								</div>
							{:else}
								{strike.strike}
							{/if}
						</td>
						
						<!-- PUT SIDE -->
						<td class="px-2 py-3 border-l-2 border-border">
							<div class="flex items-center justify-center gap-1">
								{#if peSelected && peStrike}
									<div class="flex items-center gap-1 px-2 py-1 rounded bg-primary/10 text-xs font-medium">
										✓ {peStrike.transactionType === 'BUY' ? 'B' : 'S'}
									</div>
								{:else}
									<button
										onclick={() => handleBuySell(strike.strike, 'PE', 'BUY')}
										class="px-2 py-1 text-xs font-medium rounded bg-green-500 hover:bg-green-600 text-white transition-colors"
										title="Buy Put"
									>
										🟢 B
									</button>
									<button
										onclick={() => handleBuySell(strike.strike, 'PE', 'SELL')}
										class="px-2 py-1 text-xs font-medium rounded bg-red-500 hover:bg-red-600 text-white transition-colors"
										title="Sell Put"
									>
										🔴 S
									</button>
									<button
										onclick={() => onQuickTrade(strike.strike, 'PE', 'SELL')}
										class="px-2 py-1 text-xs font-medium rounded bg-yellow-500 hover:bg-yellow-600 text-white transition-colors"
										title="Quick Trade"
									>
										⚡
									</button>
								{/if}
							</div>
						</td>
						<td class="px-2 py-3 text-right text-sm font-mono">
							{strike.pe?.ltp?.toFixed(2) || '-'}
						</td>
						<td class="px-2 py-3 text-right text-xs text-muted-foreground">
							{strike.pe?.greeks?.delta?.toFixed(2) || '-'}
						</td>
						<td class="px-2 py-3 text-right text-xs text-muted-foreground">
							{strike.pe?.greeks?.iv?.toFixed(1) || '-'}%
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
