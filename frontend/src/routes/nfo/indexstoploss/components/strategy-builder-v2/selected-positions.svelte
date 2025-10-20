<script lang="ts">
	import { Trash2, ChevronUp, ChevronDown } from '@lucide/svelte';
	import type { CalculatedStrike } from '../../lib/strike-calculator';
	import { calculateNetPremium, getStrikeLabel } from '../../lib/strike-calculator';
	
	interface Props {
		strikes: CalculatedStrike[];
		lots: number;
		underlying: string;
		onRemove: (index: number) => void;
	}
	
	let { strikes, lots, underlying, onRemove }: Props = $props();
	
	const lotSize = 25; // TODO: Get from instruments API based on underlying
	
	const premiumDetails = $derived(() => {
		return calculateNetPremium(strikes, lots, lotSize);
	});
	
	function formatCurrency(amount: number): string {
		return `₹${amount.toLocaleString('en-IN', { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
	}
</script>

<div class="rounded-lg border bg-card">
	<!-- Header -->
	<div class="flex items-center justify-between p-4 border-b">
		<h3 class="font-semibold">Selected Positions</h3>
		<div class="flex items-center gap-4 text-sm">
			<div>
				<span class="text-muted-foreground">Lots:</span>
				<span class="font-semibold ml-1">{lots}</span>
			</div>
			<div>
				<span class="text-muted-foreground">Lot Size:</span>
				<span class="font-semibold ml-1">{lotSize}</span>
			</div>
		</div>
	</div>
	
	<!-- Positions List -->
	<div class="divide-y">
		{#each strikes as strike, index}
			{@const label = getStrikeLabel(strike.strike, strike.strike, strike.optionType, strike.transactionType)}
			{@const premium = strike.ltp || 0}
			{@const totalValue = premium * lots * lotSize}
			{@const isCredit = strike.transactionType === 'SELL'}
			
			<div class="p-4 hover:bg-muted/50 transition-colors">
				<div class="flex items-center gap-4">
					<!-- Position Details -->
					<div class="flex-1">
						<div class="flex items-center gap-2">
							<span class="font-semibold">{underlying} {strike.strike} {strike.optionType}</span>
							<span class={`
								px-2 py-0.5 rounded-full text-xs font-medium
								${strike.transactionType === 'BUY' 
									? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400' 
									: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
								}
							`}>
								{strike.transactionType}
							</span>
							<span class="text-sm text-muted-foreground">
								{lots} lot{lots > 1 ? 's' : ''}
							</span>
						</div>
						
						<div class="text-sm text-muted-foreground mt-1">
							LTP: ₹{premium.toFixed(2)}
							{#if strike.delta}
								• Δ: {strike.delta.toFixed(2)}
							{/if}
						</div>
					</div>
					
					<!-- Premium Display -->
					<div class="text-right">
						<div class={`font-semibold ${isCredit ? 'text-green-600' : 'text-red-600'}`}>
							{isCredit ? '+' : '-'}{formatCurrency(totalValue)}
						</div>
						<div class="text-xs text-muted-foreground">
							{isCredit ? 'Credit' : 'Debit'}
						</div>
					</div>
					
					<!-- Adjustment Buttons (Future) -->
					<div class="flex flex-col gap-1">
						<button
							class="p-1 rounded hover:bg-muted transition-colors"
							title="Shift strike up"
							disabled
						>
							<ChevronUp class="h-4 w-4 text-muted-foreground" />
						</button>
						<button
							class="p-1 rounded hover:bg-muted transition-colors"
							title="Shift strike down"
							disabled
						>
							<ChevronDown class="h-4 w-4 text-muted-foreground" />
						</button>
					</div>
					
					<!-- Remove Button -->
					<button
						onclick={() => onRemove(index)}
						class="p-2 rounded hover:bg-destructive/10 hover:text-destructive transition-colors"
						title="Remove position"
					>
						<Trash2 class="h-4 w-4" />
					</button>
				</div>
			</div>
		{/each}
	</div>
	
	<!-- Summary -->
	<div class="p-4 border-t bg-muted/30">
		<div class="flex items-center justify-between">
			<div>
				<div class="text-sm text-muted-foreground">Net Premium</div>
				<div class="text-xs text-muted-foreground mt-1">
					{strikes.length} leg{strikes.length > 1 ? 's' : ''} • {lots * strikes.length} total lots
				</div>
			</div>
			
			<div class="text-right">
				<div class={`text-2xl font-bold ${premiumDetails.creditDebit === 'CREDIT' ? 'text-green-600' : 'text-red-600'}`}>
					{premiumDetails.creditDebit === 'CREDIT' ? '+' : '-'}{formatCurrency(premiumDetails.totalCost)}
				</div>
				<div class="text-sm text-muted-foreground">
					{premiumDetails.creditDebit}
				</div>
			</div>
		</div>
		
		<!-- Margin Estimate (Placeholder) -->
		<div class="mt-3 pt-3 border-t flex items-center justify-between text-sm">
			<span class="text-muted-foreground">Estimated Margin:</span>
			<span class="font-semibold">₹47,500</span>
			<span class="text-xs text-muted-foreground">(approx.)</span>
		</div>
	</div>
</div>
