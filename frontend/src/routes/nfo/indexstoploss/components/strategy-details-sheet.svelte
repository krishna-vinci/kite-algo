<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from '$lib/components/ui/sheet';
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import { Card, CardContent, CardHeader, CardTitle } from '$lib/components/ui/card';
	import { Separator } from '$lib/components/ui/separator';
	import { ScrollArea } from '$lib/components/ui/scroll-area';
	import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from '$lib/components/ui/alert-dialog';
	import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '$lib/components/ui/table';
	import { Play, Pause, Trash2, RefreshCw, TrendingUp, TrendingDown, Activity } from '@lucide/svelte';
	
	import StatusBadge from './shared/status-badge.svelte';
	import ModeBadge from './shared/mode-badge.svelte';
	import EventsTimeline from './events-timeline.svelte';
	
	import { getStrategy, getStrategyEvents, updateStrategyStatus, deleteStrategy } from '../lib/api';
	import { formatCurrency, formatRelativeTime } from '../lib/utils';
	import type { ProtectionStrategyResponse, EventsResponse } from '../types';
	
	interface Props {
		open: boolean;
		strategyId: string | null;
		onClose: () => void;
		onDeleted: () => void;
		onStatusChanged: () => void;
	}
	
	let { open = $bindable(), strategyId, onClose, onDeleted, onStatusChanged }: Props = $props();
	
	let strategy = $state<ProtectionStrategyResponse | null>(null);
	let events = $state<EventsResponse | null>(null);
	let loading = $state(false);
	let actionLoading = $state(false);
	
	// Fetch strategy details and events
	async function loadStrategyData() {
		if (!strategyId) return;
		
		loading = true;
		try {
			const [strategyData, eventsData] = await Promise.all([
				getStrategy(strategyId),
				getStrategyEvents(strategyId, 100)
			]);
			
			strategy = strategyData;
			events = eventsData;
		} catch (e) {
			console.error('Failed to load strategy:', e);
			toast.error('Failed to load strategy details');
			onClose();
		} finally {
			loading = false;
		}
	}
	
	// Handle pause/resume
	async function handlePauseResume() {
		if (!strategy) return;
		
		const newStatus = strategy.status === 'paused' ? 'active' : 'paused';
		const action = newStatus === 'paused' ? 'pause' : 'resume';
		
		actionLoading = true;
		try {
			await updateStrategyStatus(strategy.strategy_id, newStatus);
			toast.success(`Strategy ${action}d successfully`);
			onStatusChanged();
			await loadStrategyData(); // Reload to get updated status
		} catch (e) {
			toast.error(`Failed to ${action} strategy: ${e instanceof Error ? e.message : 'Unknown error'}`);
		} finally {
			actionLoading = false;
		}
	}
	
	// Handle delete
	async function handleDelete() {
		if (!strategy) return;
		
		actionLoading = true;
		try {
			await deleteStrategy(strategy.strategy_id);
			toast.success('Strategy deleted successfully');
			onDeleted();
			onClose();
		} catch (e) {
			toast.error(`Failed to delete strategy: ${e instanceof Error ? e.message : 'Unknown error'}`);
		} finally {
			actionLoading = false;
		}
	}
	
	// Load data when sheet opens
	$effect(() => {
		if (open && strategyId) {
			loadStrategyData();
		}
	});
</script>

<Sheet bind:open>
	<SheetContent class="w-full sm:max-w-3xl overflow-hidden flex flex-col">
		<SheetHeader>
			<SheetTitle>Strategy Details</SheetTitle>
			<SheetDescription>
				View configuration, positions, and event history
			</SheetDescription>
		</SheetHeader>
		
		{#if loading}
			<div class="flex items-center justify-center h-64">
				<RefreshCw class="h-8 w-8 animate-spin text-muted-foreground" />
			</div>
		{:else if strategy}
			<ScrollArea class="flex-1 pr-4">
				<div class="space-y-6 pb-6">
					<!-- Header Info -->
					<div class="space-y-2">
						<div class="flex items-center justify-between">
							<h3 class="text-lg font-semibold">{strategy.name || 'Unnamed Strategy'}</h3>
							<div class="flex gap-2">
								<StatusBadge status={strategy.status} />
								<ModeBadge mode={strategy.monitoring_mode} />
							</div>
						</div>
						<div class="flex gap-4 text-sm text-muted-foreground">
							<span>Created: {formatRelativeTime(strategy.created_at)}</span>
							{#if strategy.last_evaluated_at}
								<span>Last Evaluated: {formatRelativeTime(strategy.last_evaluated_at)}</span>
							{/if}
						</div>
					</div>
					
					<Separator />
					
					<!-- Actions -->
					<div class="flex gap-2">
						{#if strategy.status === 'active' || strategy.status === 'partial'}
							<Button
								variant="outline"
								size="sm"
								onclick={handlePauseResume}
								disabled={actionLoading}
							>
								<Pause class="h-4 w-4 mr-2" />
								Pause Strategy
							</Button>
						{:else if strategy.status === 'paused'}
							<Button
								variant="outline"
								size="sm"
								onclick={handlePauseResume}
								disabled={actionLoading}
							>
								<Play class="h-4 w-4 mr-2" />
								Resume Strategy
							</Button>
						{/if}
						
						{#if strategy.status === 'paused' || strategy.status === 'completed' || strategy.status === 'triggered'}
							<AlertDialog>
								<AlertDialogTrigger asChild let:builder>
									<Button builders={[builder]} variant="destructive" size="sm" disabled={actionLoading}>
										<Trash2 class="h-4 w-4 mr-2" />
										Delete
									</Button>
								</AlertDialogTrigger>
								<AlertDialogContent>
									<AlertDialogHeader>
										<AlertDialogTitle>Delete Strategy?</AlertDialogTitle>
										<AlertDialogDescription>
											This action cannot be undone. The strategy and all its event history will be permanently deleted.
										</AlertDialogDescription>
									</AlertDialogHeader>
									<AlertDialogFooter>
										<AlertDialogCancel>Cancel</AlertDialogCancel>
										<AlertDialogAction onclick={handleDelete}>Delete</AlertDialogAction>
									</AlertDialogFooter>
								</AlertDialogContent>
							</AlertDialog>
						{/if}
					</div>
					
					<Separator />
					
					<!-- Configuration -->
					<Card>
						<CardHeader>
							<CardTitle class="text-base">Configuration</CardTitle>
						</CardHeader>
						<CardContent class="space-y-3 text-sm">
							<div class="grid grid-cols-2 gap-3">
								<div>
									<span class="text-muted-foreground">Strategy Type:</span>
									<div class="font-medium">{strategy.strategy_type.toUpperCase()}</div>
								</div>
								<div>
									<span class="text-muted-foreground">Monitoring Mode:</span>
									<div class="font-medium">{strategy.monitoring_mode.toUpperCase()}</div>
								</div>
							</div>
							
							{#if strategy.index_instrument_token}
								<Separator />
								<div class="space-y-2">
									<h4 class="font-medium">Index Monitoring</h4>
									<div class="grid grid-cols-2 gap-3">
										<div>
											<span class="text-muted-foreground">Symbol:</span>
											<div class="font-mono">{strategy.index_tradingsymbol}</div>
										</div>
										<div>
											<span class="text-muted-foreground">Token:</span>
											<div class="font-mono">{strategy.index_instrument_token}</div>
										</div>
										{#if strategy.index_upper_stoploss}
											<div>
												<span class="text-muted-foreground">Upper SL:</span>
												<div class="font-mono text-red-500">{strategy.index_upper_stoploss}</div>
											</div>
										{/if}
										{#if strategy.index_lower_stoploss}
											<div>
												<span class="text-muted-foreground">Lower SL:</span>
												<div class="font-mono text-green-500">{strategy.index_lower_stoploss}</div>
											</div>
										{/if}
									</div>
								</div>
							{/if}
							
							{#if strategy.trailing_mode && strategy.trailing_mode !== 'none'}
								<Separator />
								<div class="space-y-2">
									<h4 class="font-medium">Trailing Configuration</h4>
									<div class="grid grid-cols-2 gap-3">
										<div>
											<span class="text-muted-foreground">Mode:</span>
											<div class="font-medium">{strategy.trailing_mode.toUpperCase()}</div>
										</div>
										<div>
											<span class="text-muted-foreground">Distance:</span>
											<div class="font-mono">{strategy.trailing_distance}</div>
										</div>
										<div>
											<span class="text-muted-foreground">Activated:</span>
											<Badge variant={strategy.trailing_activated ? 'success' : 'secondary'}>
												{strategy.trailing_activated ? 'Yes' : 'No'}
											</Badge>
										</div>
										{#if strategy.trailing_current_level}
											<div>
												<span class="text-muted-foreground">Current Level:</span>
												<div class="font-mono">{strategy.trailing_current_level}</div>
											</div>
										{/if}
									</div>
								</div>
							{/if}
						</CardContent>
					</Card>
					
					<!-- Position Snapshot -->
					<Card>
						<CardHeader>
							<CardTitle class="text-base">
								Position Snapshot
								<span class="text-sm font-normal text-muted-foreground ml-2">
									({strategy.positions_captured} positions, {strategy.total_lots.toFixed(1)} lots)
								</span>
							</CardTitle>
						</CardHeader>
						<CardContent>
							<div class="rounded-md border">
								<Table>
									<TableHeader>
										<TableRow>
											<TableHead>Symbol</TableHead>
											<TableHead>Type</TableHead>
											<TableHead class="text-right">Qty</TableHead>
											<TableHead class="text-right">Lots</TableHead>
											<TableHead class="text-right">Avg Price</TableHead>
											{#if strategy.position_snapshot.some(p => p.current_ltp)}
												<TableHead class="text-right">LTP</TableHead>
											{/if}
										</TableRow>
									</TableHeader>
									<TableBody>
										{#each strategy.position_snapshot as position}
											<TableRow>
												<TableCell class="font-medium font-mono">{position.tradingsymbol}</TableCell>
												<TableCell>
													<Badge variant={position.transaction_type === 'SELL' ? 'destructive' : 'default'} class="text-xs">
														{position.transaction_type}
													</Badge>
												</TableCell>
												<TableCell class="text-right font-mono">{position.quantity}</TableCell>
												<TableCell class="text-right font-mono">{position.lots.toFixed(1)}</TableCell>
												<TableCell class="text-right font-mono">{formatCurrency(position.average_price)}</TableCell>
												{#if position.current_ltp}
													<TableCell class="text-right font-mono">{formatCurrency(position.current_ltp)}</TableCell>
												{/if}
											</TableRow>
										{/each}
									</TableBody>
								</Table>
							</div>
						</CardContent>
					</Card>
					
					<!-- Events Timeline -->
					{#if events && events.events.length > 0}
						<Card>
							<CardHeader>
								<CardTitle class="text-base">
									Event History
									<span class="text-sm font-normal text-muted-foreground ml-2">
										({events.total_events} events)
									</span>
								</CardTitle>
							</CardHeader>
							<CardContent>
								<EventsTimeline events={events.events} />
							</CardContent>
						</Card>
					{/if}
				</div>
			</ScrollArea>
		{/if}
	</SheetContent>
</Sheet>
