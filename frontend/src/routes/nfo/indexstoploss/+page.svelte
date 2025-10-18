<script lang="ts">
	import { onMount } from 'svelte';
	import { Tabs, TabsList, TabsTrigger, TabsContent } from '$lib/components/ui/tabs';
	import { Button } from '$lib/components/ui/button';
	import { Plus, LayoutDashboard, Settings, Hammer } from '@lucide/svelte';
	import {
		ResizableHandle,
		ResizablePane,
		ResizablePaneGroup
	} from '$lib/components/ui/resizable';
	
	import EngineHealthBadge from './components/shared/engine-health-badge.svelte';
	import SummaryCards from './components/dashboard/summary-cards.svelte';
	import StrategiesTable from './components/dashboard/strategies-table.svelte';
	import PositionsTable from './components/dashboard/positions-table.svelte';
	
	import { listStrategies, updateStrategyStatus } from './lib/api';
	import type { PageData } from './$types';
	
	interface Props {
		data: PageData;
	}
	
	let { data }: Props = $props();
	
	// State management
	let strategies = $state(data.strategies.strategies);
	let health = $state(data.health);
	let positions = $state([...(data.positions.net || []), ...(data.positions.day || [])]);
	let activeTab = $state('dashboard');
	let refreshing = $state(false);
	
	// Refresh strategies list
	async function refreshStrategies() {
		refreshing = true;
		try {
			const result = await listStrategies();
			strategies = result.strategies;
		} catch (e) {
			console.error('Failed to refresh strategies:', e);
		} finally {
			refreshing = false;
		}
	}
	
	// Handle view strategy details
	function handleViewDetails(strategyId: string) {
		console.log('View details for strategy:', strategyId);
		// TODO: Open strategy details sheet
		alert(`Strategy details sheet will be implemented in Phase 2. Strategy ID: ${strategyId}`);
	}
	
	// Handle pause/resume strategy
	async function handlePauseResume(strategyId: string, currentStatus: string) {
		const newStatus = currentStatus === 'paused' ? 'active' : 'paused';
		const action = newStatus === 'paused' ? 'pause' : 'resume';
		
		if (!confirm(`Are you sure you want to ${action} this strategy?`)) {
			return;
		}
		
		try {
			await updateStrategyStatus(strategyId, newStatus);
			await refreshStrategies();
			alert(`Strategy ${action}d successfully`);
		} catch (e) {
			alert(`Failed to ${action} strategy: ${e instanceof Error ? e.message : 'Unknown error'}`);
		}
	}
	
	// Handle create strategy
	function handleCreateStrategy() {
		console.log('Create strategy clicked');
		// TODO: Open create strategy sheet
		alert('Create strategy sheet will be implemented in Phase 4');
	}
	
	onMount(() => {
		console.log('IndexStopLoss page mounted');
		console.log('Initial data:', { strategies, health, positions });
	});
</script>

<svelte:head>
	<title>Position Protection System | NFO Index StopLoss</title>
</svelte:head>

<div class="container mx-auto p-6 space-y-6">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-3xl font-bold">Position Protection System</h1>
			<p class="text-muted-foreground mt-1">
				Real-time position monitoring with automated stoploss protection
			</p>
		</div>
		<div class="flex items-center gap-3">
			<EngineHealthBadge initialHealth={health} />
			<Button onclick={handleCreateStrategy}>
				<Plus class="h-4 w-4 mr-2" />
				Create Strategy
			</Button>
		</div>
	</div>
	
	<!-- Main Content with Tabs -->
	<Tabs bind:value={activeTab} class="w-full">
		<TabsList class="grid w-full grid-cols-3">
			<TabsTrigger value="dashboard" class="gap-2">
				<LayoutDashboard class="h-4 w-4" />
				Dashboard
			</TabsTrigger>
			<TabsTrigger value="manager" class="gap-2">
				<Settings class="h-4 w-4" />
				Strategy Manager
			</TabsTrigger>
			<TabsTrigger value="builder" class="gap-2">
				<Hammer class="h-4 w-4" />
				Position Builder
			</TabsTrigger>
		</TabsList>
		
		<!-- Dashboard Tab -->
		<TabsContent value="dashboard" class="space-y-6">
			<!-- Summary Cards -->
			<SummaryCards 
				strategies={strategies}
				positions={positions}
				engineRunning={health?.engine_running ?? false}
			/>
			
			<!-- Resizable Panel Layout -->
			<ResizablePaneGroup direction="horizontal" class="min-h-[600px] rounded-lg border">
				<!-- Left Panel: Strategies List -->
				<ResizablePane defaultSize={50} minSize={30}>
					<div class="h-full p-4 space-y-4">
						<div class="flex items-center justify-between">
							<h2 class="text-lg font-semibold">Active Strategies</h2>
							<Button 
								variant="ghost" 
								size="sm" 
								onclick={refreshStrategies}
								disabled={refreshing}
							>
								{refreshing ? 'Refreshing...' : 'Refresh'}
							</Button>
						</div>
						<StrategiesTable 
							strategies={strategies}
							onViewDetails={handleViewDetails}
							onPauseResume={handlePauseResume}
						/>
					</div>
				</ResizablePane>
				
				<ResizableHandle withHandle />
				
				<!-- Right Panel: Real-Time Positions -->
				<ResizablePane defaultSize={50} minSize={30}>
					<div class="h-full p-4">
						<PositionsTable initialPositions={positions} />
					</div>
				</ResizablePane>
			</ResizablePaneGroup>
		</TabsContent>
		
		<!-- Strategy Manager Tab -->
		<TabsContent value="manager" class="space-y-6">
			<div class="text-center py-20 text-muted-foreground">
				<Settings class="h-16 w-16 mx-auto mb-4 opacity-50" />
				<h3 class="text-xl font-semibold mb-2">Strategy Manager</h3>
				<p>Full strategy management interface will be implemented in Phase 2</p>
			</div>
		</TabsContent>
		
		<!-- Position Builder Tab -->
		<TabsContent value="builder" class="space-y-6">
			<div class="text-center py-20 text-muted-foreground">
				<Hammer class="h-16 w-16 mx-auto mb-4 opacity-50" />
				<h3 class="text-xl font-semibold mb-2">Position Builder</h3>
				<p>Delta-based position building wizard will be implemented in Phase 5</p>
			</div>
		</TabsContent>
	</Tabs>
</div>

<style>
	:global(.font-mono) {
		font-feature-settings: "tnum";
		font-variant-numeric: tabular-nums;
	}
</style>
