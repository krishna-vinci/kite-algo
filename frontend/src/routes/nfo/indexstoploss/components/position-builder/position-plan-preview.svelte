<script lang="ts">
	import * as Card from '$lib/components/ui/card';
	import { Badge } from '$lib/components/ui/badge';
	import { Separator } from '$lib/components/ui/separator';
	import type { SelectedStrike } from '../../types';

	interface Props {
		underlying: string;
		expiry: string;
		strategyType: string;
		selectedStrikes: SelectedStrike[];
		protectionEnabled: boolean;
	}

	let { underlying, expiry, strategyType, selectedStrikes, protectionEnabled }: Props = $props();

	const totalLots = $derived(
		selectedStrikes.reduce((sum, strike) => sum + strike.lots, 0)
	);

	const totalPremium = $derived(
		selectedStrikes.reduce((sum, strike) => {
			const value = strike.ltp * strike.lot_size * strike.lots;
			return sum + (strike.transaction_type === 'SELL' ? value : -value);
		}, 0)
	);

	const maxLoss = $derived(
		// Simplified: sum of sell premiums (worst case all options exercised)
		selectedStrikes
			.filter(s => s.transaction_type === 'SELL')
			.reduce((sum, strike) => {
				return sum + (strike.ltp * strike.lot_size * strike.lots);
			}, 0)
	);

	const marginRequired = $derived(
		// Rough estimate: ~25% of notional value for short options
		selectedStrikes
			.filter(s => s.transaction_type === 'SELL')
			.reduce((sum, strike) => {
				const notional = strike.strike * strike.lot_size * strike.lots;
				return sum + (notional * 0.25);
			}, 0)
	);
</script>

<div class="sticky top-4">
	<Card.Root>
		<Card.Header>
			<Card.Title class="text-lg">Position Plan</Card.Title>
			<Card.Description>
				Live summary of your position
			</Card.Description>
		</Card.Header>
		<Card.Content class="space-y-4">
			<!-- Basic Info -->
			<div class="space-y-2">
				<div class="flex justify-between text-sm">
					<span class="text-muted-foreground">Underlying:</span>
					<span class="font-semibold">{underlying || '-'}</span>
				</div>
				<div class="flex justify-between text-sm">
					<span class="text-muted-foreground">Expiry:</span>
					<span class="font-mono text-xs">
						{expiry ? new Date(expiry).toLocaleDateString() : '-'}
					</span>
				</div>
				<div class="flex justify-between text-sm">
					<span class="text-muted-foreground">Strategy:</span>
					{#if strategyType}
						<Badge variant="outline" class="text-xs">{strategyType.toUpperCase()}</Badge>
					{:else}
						<span>-</span>
					{/if}
				</div>
			</div>

			<Separator />

			<!-- Selected Strikes -->
			<div class="space-y-2">
				<p class="text-sm font-medium">Selected Strikes ({selectedStrikes.length})</p>
				{#if selectedStrikes.length > 0}
					<div class="space-y-1.5 max-h-40 overflow-y-auto">
						{#each selectedStrikes as strike}
							<div class="flex items-center justify-between text-xs p-2 rounded-md bg-muted/50">
								<div class="flex items-center gap-2">
									<Badge 
										variant={strike.option_type === 'CE' ? 'default' : 'destructive'} 
										class="text-xs px-1.5 py-0"
									>
										{strike.option_type}
									</Badge>
									<span class="font-mono">{strike.strike}</span>
								</div>
								<div class="flex items-center gap-1">
									<Badge 
										variant={strike.transaction_type === 'SELL' ? 'success' : 'secondary'}
										class="text-xs px-1.5 py-0"
									>
										{strike.transaction_type}
									</Badge>
									<span class="font-mono">{strike.lots}L</span>
								</div>
							</div>
						{/each}
					</div>
				{:else}
					<p class="text-xs text-muted-foreground text-center py-4">
						No strikes selected yet
					</p>
				{/if}
			</div>

			<Separator />

			<!-- Metrics -->
			<div class="space-y-2">
				<div class="flex justify-between text-sm">
					<span class="text-muted-foreground">Total Lots:</span>
					<span class="font-mono font-semibold">{totalLots}</span>
				</div>
				<div class="flex justify-between text-sm">
					<span class="text-muted-foreground">Net Premium:</span>
					<span class="font-mono font-semibold {totalPremium >= 0 ? 'text-green-500' : 'text-red-500'}">
						{totalPremium >= 0 ? '+' : ''}₹{totalPremium.toFixed(0)}
					</span>
				</div>
				<div class="flex justify-between text-sm">
					<span class="text-muted-foreground">Est. Margin:</span>
					<span class="font-mono text-xs">₹{marginRequired.toFixed(0)}</span>
				</div>
			</div>

			<Separator />

			<!-- Protection Status -->
			<div class="flex items-center justify-between text-sm">
				<span class="text-muted-foreground">Protection:</span>
				{#if protectionEnabled}
					<Badge variant="success" class="text-xs">Enabled</Badge>
				{:else}
					<Badge variant="outline" class="text-xs">Disabled</Badge>
				{/if}
			</div>
		</Card.Content>
	</Card.Root>
</div>
