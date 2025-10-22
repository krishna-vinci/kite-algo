<script lang="ts">
	import {
		Table,
		TableBody,
		TableCell,
		TableHead,
		TableHeader,
		TableRow
	} from '$lib/components/ui/table';
	import { Button } from '$lib/components/ui/button';
	import { Eye, Pause, Play, Pencil } from '@lucide/svelte';
	import ModeBadge from '../shared/mode-badge.svelte';
	import StatusBadge from '../shared/status-badge.svelte';
	import type { StrategyListItem } from '../../types';
	import { formatRelativeTime, truncate } from '../../lib/utils';
	
	interface Props {
		strategies: StrategyListItem[];
		onViewDetails?: (strategyId: string) => void;
		onEdit?: (strategyId: string) => void;
		onPauseResume?: (strategyId: string, currentStatus: string) => void;
	}
	
	let { strategies, onViewDetails, onEdit, onPauseResume }: Props = $props();
	
	function handleView(strategyId: string) {
		onViewDetails?.(strategyId);
	}
	
	function handleEdit(strategyId: string) {
		onEdit?.(strategyId);
	}
	
	function handlePauseResume(strategyId: string, status: string) {
		onPauseResume?.(strategyId, status);
	}
	
	// Check if strategy can be edited (not completed, triggered, or error)
	function canEdit(status: string): boolean {
		return !['completed', 'triggered', 'error'].includes(status);
	}
</script>

<div class="rounded-md border">
	<Table>
		<TableHeader>
			<TableRow>
				<TableHead class="w-[200px]">Name</TableHead>
				<TableHead class="w-[100px]">Mode</TableHead>
				<TableHead class="w-[100px]">Status</TableHead>
				<TableHead class="w-[100px] text-right">Lots</TableHead>
				<TableHead class="w-[120px]">Index Token</TableHead>
				<TableHead class="w-[120px]">Upper SL</TableHead>
				<TableHead class="w-[120px]">Lower SL</TableHead>
				<TableHead class="w-[120px]">Last Evaluated</TableHead>
				<TableHead class="w-[150px] text-right">Actions</TableHead>
			</TableRow>
		</TableHeader>
		<TableBody>
			{#if strategies.length === 0}
				<TableRow>
					<TableCell colspan={9} class="text-center text-muted-foreground py-8">
						No strategies found. Create your first strategy to get started.
					</TableCell>
				</TableRow>
			{:else}
				{#each strategies as strategy (strategy.strategy_id)}
					<TableRow class="hover:bg-muted/50">
						<TableCell class="font-medium">
							{strategy.name ? truncate(strategy.name, 30) : 'Unnamed'}
						</TableCell>
						<TableCell>
							<ModeBadge mode={strategy.monitoring_mode} />
						</TableCell>
						<TableCell>
							<StatusBadge status={strategy.status} />
						</TableCell>
						<TableCell class="text-right font-mono">
							{strategy.total_lots.toFixed(1)}
						</TableCell>
						<TableCell class="font-mono text-xs">
							{strategy.index_instrument_token || '-'}
						</TableCell>
						<TableCell class="font-mono text-xs">
							{strategy.index_upper_stoploss?.toFixed(2) || '-'}
						</TableCell>
						<TableCell class="font-mono text-xs">
							{strategy.index_lower_stoploss?.toFixed(2) || '-'}
						</TableCell>
						<TableCell class="text-xs text-muted-foreground">
							{strategy.last_evaluated_at ? formatRelativeTime(strategy.last_evaluated_at) : 'Never'}
						</TableCell>
						<TableCell class="text-right">
							<div class="flex items-center justify-end gap-1">
								<Button
									variant="ghost"
									size="sm"
									onclick={() => handleView(strategy.strategy_id)}
									title="View Details"
								>
									<Eye class="h-4 w-4" />
								</Button>
								{#if canEdit(strategy.status)}
									<Button
										variant="ghost"
										size="sm"
										onclick={() => handleEdit(strategy.strategy_id)}
										title="Edit Strategy"
									>
										<Pencil class="h-4 w-4" />
									</Button>
								{/if}
								{#if strategy.status === 'active' || strategy.status === 'partial'}
									<Button
										variant="ghost"
										size="sm"
										onclick={() => handlePauseResume(strategy.strategy_id, strategy.status)}
										title="Pause Strategy"
									>
										<Pause class="h-4 w-4" />
									</Button>
								{:else if strategy.status === 'paused'}
									<Button
										variant="ghost"
										size="sm"
										onclick={() => handlePauseResume(strategy.strategy_id, strategy.status)}
										title="Resume Strategy"
									>
										<Play class="h-4 w-4" />
									</Button>
								{/if}
							</div>
						</TableCell>
					</TableRow>
				{/each}
			{/if}
		</TableBody>
	</Table>
</div>
