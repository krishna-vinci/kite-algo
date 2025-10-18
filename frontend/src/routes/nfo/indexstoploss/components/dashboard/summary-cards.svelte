<script lang="ts">
	import { Card, CardContent } from '$lib/components/ui/card';
	import { Shield, TrendingUp } from '@lucide/svelte';
	import type { StrategyListItem, RealtimePosition } from '../../types';
	import { formatCurrency, getPnLColor, calculateTotalPnL } from '../../lib/utils';
	
	interface Props {
		strategies: StrategyListItem[];
		positions: RealtimePosition[];
	}
	
	let { strategies, positions }: Props = $props();
	
	const activeCount = $derived(
		strategies.filter(s => s.status === 'active' || s.status === 'partial').length
	);
	
	const totalLots = $derived(
		strategies.reduce((sum, s) => sum + s.total_lots, 0)
	);
	
	const totalPnL = $derived(
		calculateTotalPnL(positions)
	);
</script>

<div class="grid grid-cols-2 gap-3">
	<!-- Active Strategies Card -->
	<Card>
		<CardContent class="pt-4 pb-4">
			<div class="flex items-center justify-between">
				<div>
					<p class="text-xs font-medium text-muted-foreground">Active Strategies</p>
					<h3 class="text-2xl font-bold mt-1 font-mono">{activeCount}</h3>
					<p class="text-xs text-muted-foreground mt-0.5">
						{totalLots.toFixed(1)} lots protected
					</p>
				</div>
				<div class="h-10 w-10 rounded-full bg-blue-500/10 flex items-center justify-center">
					<Shield class="h-5 w-5 text-blue-500" />
				</div>
			</div>
		</CardContent>
	</Card>
	
	<!-- Total P&L Card -->
	<Card>
		<CardContent class="pt-4 pb-4">
			<div class="flex items-center justify-between">
				<div>
					<p class="text-xs font-medium text-muted-foreground">Total P&L</p>
					<h3 class={`text-2xl font-bold mt-1 font-mono ${getPnLColor(totalPnL)}`}>
						{formatCurrency(totalPnL, 0)}
					</h3>
					<p class="text-xs text-muted-foreground mt-0.5">
						{positions.length} positions
					</p>
				</div>
				<div class={`h-10 w-10 rounded-full flex items-center justify-center ${
					totalPnL >= 0 ? 'bg-green-500/10' : 'bg-red-500/10'
				}`}>
					<TrendingUp class={`h-5 w-5 ${
						totalPnL >= 0 ? 'text-green-500' : 'text-red-500'
					}`} />
				</div>
			</div>
		</CardContent>
	</Card>
</div>
