<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import { Card, CardContent } from '$lib/components/ui/card';
	import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '$lib/components/ui/table';
	import { Search, RefreshCw, Eye, Play, Pause, Filter, X } from '@lucide/svelte';
	
	import StatusBadge from './shared/status-badge.svelte';
	import ModeBadge from './shared/mode-badge.svelte';
	
	import { listStrategies } from '../lib/api';
	import { formatRelativeTime } from '../lib/utils';
	import type { StrategyListItem, StrategyStatus, MonitoringMode } from '../types';
	
	interface Props {
		onViewDetails: (strategyId: string) => void;
		onPauseResume: (strategyId: string, currentStatus: string) => void;
	}
	
	let { onViewDetails, onPauseResume }: Props = $props();
	
	// State
	let strategies = $state<StrategyListItem[]>([]);
	let filteredStrategies = $state<StrategyListItem[]>([]);
	let loading = $state(false);
	let searchQuery = $state('');
	let statusFilter = $state<string>('all');
	let modeFilter = $state<string>('all');
	
	// Load strategies
	async function loadStrategies() {
		loading = true;
		try {
			const result = await listStrategies(undefined, undefined, 200);
			strategies = result.strategies;
			applyFilters();
		} catch (e) {
			console.error('Failed to load strategies:', e);
			toast.error('Failed to load strategies');
		} finally {
			loading = false;
		}
	}
	
	// Apply filters
	function applyFilters() {
		let result = [...strategies];
		
		// Search filter
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			result = result.filter(s => 
				s.name?.toLowerCase().includes(query) ||
				s.strategy_id.toLowerCase().includes(query) ||
				s.index_tradingsymbol?.toLowerCase().includes(query)
			);
		}
		
		// Status filter
		if (statusFilter !== 'all') {
			result = result.filter(s => s.status === statusFilter);
		}
		
		// Mode filter
		if (modeFilter !== 'all') {
			result = result.filter(s => s.monitoring_mode === modeFilter);
		}
		
		filteredStrategies = result;
	}
	
	// Clear all filters
	function clearFilters() {
		searchQuery = '';
		statusFilter = 'all';
		modeFilter = 'all';
		applyFilters();
	}
	
	// Watch for filter changes
	$effect(() => {
		applyFilters();
		// Dependencies: searchQuery, statusFilter, modeFilter, strategies
		void searchQuery;
		void statusFilter;
		void modeFilter;
		void strategies;
	});
	
	onMount(() => {
		loadStrategies();
	});
</script>

<div class="space-y-4">
	<!-- Filters Bar -->
	<Card>
		<CardContent class="pt-6">
			<div class="flex flex-col gap-4">
				<!-- Search and Refresh -->
				<div class="flex gap-2">
					<div class="relative flex-1">
						<Search class="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
						<input
							type="text"
							placeholder="Search by name, ID, or symbol..."
							bind:value={searchQuery}
							class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 pl-9 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
						/>
					</div>
					<Button variant="outline" onclick={loadStrategies} disabled={loading}>
						<RefreshCw class="h-4 w-4 mr-2 {loading ? 'animate-spin' : ''}" />
						Refresh
					</Button>
				</div>
				
				<!-- Filter Dropdowns -->
				<div class="flex gap-2 items-center flex-wrap">
					<Filter class="h-4 w-4 text-muted-foreground" />
					
					<select 
						bind:value={statusFilter}
						class="flex h-10 w-[180px] items-center justify-between rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
					>
						<option value="all">All Statuses</option>
						<option value="active">Active</option>
						<option value="paused">Paused</option>
						<option value="partial">Partial</option>
						<option value="triggered">Triggered</option>
						<option value="completed">Completed</option>
						<option value="error">Error</option>
					</select>
					
					<select 
						bind:value={modeFilter}
						class="flex h-10 w-[180px] items-center justify-between rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
					>
						<option value="all">All Modes</option>
						<option value="index">Index</option>
						<option value="premium">Premium</option>
						<option value="hybrid">Hybrid</option>
						<option value="combined_premium">Combined</option>
					</select>
					
					{#if searchQuery || statusFilter !== 'all' || modeFilter !== 'all'}
						<Button variant="ghost" size="sm" onclick={clearFilters}>
							<X class="h-4 w-4 mr-2" />
							Clear Filters
						</Button>
					{/if}
					
					<div class="ml-auto text-sm text-muted-foreground">
						Showing {filteredStrategies.length} of {strategies.length} strategies
					</div>
				</div>
			</div>
		</CardContent>
	</Card>
	
	<!-- Strategies Table -->
	<Card>
		<CardContent class="p-0">
			{#if loading}
				<div class="flex items-center justify-center h-64">
					<RefreshCw class="h-8 w-8 animate-spin text-muted-foreground" />
				</div>
			{:else if filteredStrategies.length === 0}
				<div class="text-center py-20 text-muted-foreground">
					<p class="text-lg font-medium mb-2">No strategies found</p>
					<p class="text-sm">
						{#if searchQuery || statusFilter !== 'all' || modeFilter !== 'all'}
							Try adjusting your filters
						{:else}
							Create your first strategy to get started
						{/if}
					</p>
				</div>
			{:else}
				<div class="rounded-md border">
					<Table>
						<TableHeader>
							<TableRow>
								<TableHead>Name</TableHead>
								<TableHead>Status</TableHead>
								<TableHead>Mode</TableHead>
								<TableHead class="text-right">Lots</TableHead>
								<TableHead>Index Symbol</TableHead>
								<TableHead>Levels</TableHead>
								<TableHead>Last Evaluated</TableHead>
								<TableHead>Created</TableHead>
								<TableHead class="text-right">Actions</TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{#each filteredStrategies as strategy}
								<TableRow class="cursor-pointer hover:bg-muted/50" onclick={() => onViewDetails(strategy.strategy_id)}>
									<TableCell class="font-medium">
										{strategy.name || 'Unnamed'}
										<div class="text-xs text-muted-foreground font-mono">{strategy.strategy_id.substring(0, 8)}...</div>
									</TableCell>
									<TableCell>
										<StatusBadge status={strategy.status as StrategyStatus} />
									</TableCell>
									<TableCell>
										<ModeBadge mode={strategy.monitoring_mode as MonitoringMode} />
									</TableCell>
									<TableCell class="text-right font-mono">
										{strategy.total_lots.toFixed(1)}
									</TableCell>
									<TableCell class="font-mono text-sm">
										{strategy.index_tradingsymbol || '-'}
									</TableCell>
									<TableCell>
										<div class="flex gap-1 text-xs">
											{#if strategy.index_upper_stoploss}
												<Badge variant="outline" class="text-red-500 border-red-500/20">
													↑ {strategy.index_upper_stoploss}
												</Badge>
											{/if}
											{#if strategy.index_lower_stoploss}
												<Badge variant="outline" class="text-green-500 border-green-500/20">
													↓ {strategy.index_lower_stoploss}
												</Badge>
											{/if}
										</div>
									</TableCell>
									<TableCell class="text-sm">
										{strategy.last_evaluated_at ? formatRelativeTime(strategy.last_evaluated_at) : '-'}
									</TableCell>
									<TableCell class="text-sm">
										{formatRelativeTime(strategy.created_at)}
									</TableCell>
									<TableCell class="text-right">
										<div class="flex gap-1 justify-end" role="group" onclick={(e) => e.stopPropagation()}>
											<Button
												variant="ghost"
												size="icon"
												class="h-8 w-8"
												onclick={() => onViewDetails(strategy.strategy_id)}
											>
												<Eye class="h-4 w-4" />
											</Button>
											{#if strategy.status === 'active' || strategy.status === 'partial'}
												<Button
													variant="ghost"
													size="icon"
													class="h-8 w-8"
													onclick={() => onPauseResume(strategy.strategy_id, strategy.status)}
												>
													<Pause class="h-4 w-4" />
												</Button>
											{:else if strategy.status === 'paused'}
												<Button
													variant="ghost"
													size="icon"
													class="h-8 w-8"
													onclick={() => onPauseResume(strategy.strategy_id, strategy.status)}
												>
													<Play class="h-4 w-4" />
												</Button>
											{/if}
										</div>
									</TableCell>
								</TableRow>
							{/each}
						</TableBody>
					</Table>
				</div>
			{/if}
		</CardContent>
	</Card>
</div>
