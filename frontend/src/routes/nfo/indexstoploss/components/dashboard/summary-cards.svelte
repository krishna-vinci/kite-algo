<script lang="ts">
	import { Card, CardContent } from '$lib/components/ui/card';
	import { Activity, Shield, TrendingUp } from '@lucide/svelte';
	import type { StrategyListItem, RealtimePosition } from '../../types';
	import { formatCurrency, getPnLColor, calculateTotalPnL } from '../../lib/utils';
	
	interface Props {
		strategies: StrategyListItem[];
		positions: RealtimePosition[];
		engineRunning: boolean;
	}
	
	let { strategies, positions, engineRunning }: Props = $props();
	
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

<div class="grid grid-cols-1 md:grid-cols-3 gap-4">
	<!-- Active Strategies Card -->
	<Card>
		<CardContent class="pt-6">
			<div class="flex items-center justify-between">
				<div>
					<p class="text-sm font-medium text-muted-foreground">Active Strategies</p>
					<h3 class="text-3xl font-bold mt-2 font-mono">{activeCount}</h3>
					<p class="text-xs text-muted-foreground mt-1">
						{totalLots.toFixed(1)} lots protected
					</p>
				</div>
				<div class="h-12 w-12 rounded-full bg-blue-500/10 flex items-center justify-center">
					<Shield class="h-6 w-6 text-blue-500" />
				</div>
			</div>
		</CardContent>
	</Card>
	
	<!-- Total P&L Card -->
	<Card>
		<CardContent class="pt-6">
			<div class="flex items-center justify-between">
				<div>
					<p class="text-sm font-medium text-muted-foreground">Total P&L</p>
					<h3 class={`text-3xl font-bold mt-2 font-mono ${getPnLColor(totalPnL)}`}>
						{formatCurrency(totalPnL, 0)}
					</h3>
					<p class="text-xs text-muted-foreground mt-1">
						{positions.length} positions
					</p>
				</div>
				<div class={`h-12 w-12 rounded-full flex items-center justify-center ${
					totalPnL >= 0 ? 'bg-green-500/10' : 'bg-red-500/10'
				}`}>
					<TrendingUp class={`h-6 w-6 ${
						totalPnL >= 0 ? 'text-green-500' : 'text-red-500'
					}`} />
				</div>
			</div>
		</CardContent>
	</Card>
	
	<!-- Engine Status Card -->
	<Card>
		<CardContent class="pt-6">
			<div class="flex items-center justify-between">
				<div>
					<p class="text-sm font-medium text-muted-foreground">Engine Status</p>
					<h3 class={`text-3xl font-bold mt-2 ${
						engineRunning ? 'text-green-500' : 'text-red-500'
					}`}>
						{engineRunning ? 'Healthy' : 'Stopped'}
					</h3>
					<p class="text-xs text-muted-foreground mt-1">
						Real-time monitoring
					</p>
				</div>
				<div class={`h-12 w-12 rounded-full flex items-center justify-center ${
					engineRunning ? 'bg-green-500/10' : 'bg-red-500/10'
				}`}>
					<Activity class={`h-6 w-6 ${
						engineRunning ? 'text-green-500 animate-pulse' : 'text-red-500'
					}`} />
				</div>
			</div>
		</CardContent>
	</Card>
</div>
