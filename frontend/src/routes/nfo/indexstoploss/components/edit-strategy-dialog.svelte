<script lang="ts">
	import { toast } from 'svelte-sonner';
	import {
		Dialog,
		DialogContent,
		DialogDescription,
		DialogFooter,
		DialogHeader,
		DialogTitle
	} from '$lib/components/ui/dialog';
	import { Button } from '$lib/components/ui/button';
	import { Label } from '$lib/components/ui/label';
	import { Input } from '$lib/components/ui/input';
	import { Badge } from '$lib/components/ui/badge';
	import { Tabs, TabsList, TabsTrigger, TabsContent } from '$lib/components/ui/tabs';
	import { AlertCircle, Save, X } from '@lucide/svelte';
	
	import { getStrategy, updateStrategy } from '../lib/api';
	import type { ProtectionStrategyResponse } from '../types';
	
	interface Props {
		open: boolean;
		strategyId: string | null;
		onClose: () => void;
		onUpdated?: () => void;
	}
	
	let { open = $bindable(), strategyId, onClose, onUpdated }: Props = $props();
	
	// State
	let loading = $state(false);
	let saving = $state(false);
	let strategy = $state<ProtectionStrategyResponse | null>(null);
	let errors = $state<Record<string, string>>({});
	
	// Form fields
	let name = $state('');
	let upperStoploss = $state<number | undefined>();
	let lowerStoploss = $state<number | undefined>();
	let trailingMode = $state('none');
	let trailingDistance = $state<number | undefined>();
	let trailingLockProfit = $state<number | undefined>();
	
	// Track which fields have changed
	let hasChanges = $derived(
		(name !== strategy?.name) ||
		(upperStoploss !== strategy?.index_upper_stoploss) ||
		(lowerStoploss !== strategy?.index_lower_stoploss) ||
		(trailingMode !== strategy?.trailing_mode) ||
		(trailingDistance !== strategy?.trailing_distance) ||
		(trailingLockProfit !== strategy?.trailing_lock_profit)
	);
	
	// Check if strategy can be edited
	const canEdit = $derived(
		strategy && !['completed', 'triggered', 'error'].includes(strategy.status)
	);
	
	// Load strategy data when dialog opens
	async function loadStrategy() {
		if (!strategyId) return;
		
		loading = true;
		try {
			const data = await getStrategy(strategyId);
			strategy = data;
			
			// Initialize form fields
			name = data.name || '';
			upperStoploss = data.index_upper_stoploss ?? undefined;
			lowerStoploss = data.index_lower_stoploss ?? undefined;
			trailingMode = data.trailing_mode || 'none';
			trailingDistance = data.trailing_distance ?? undefined;
			trailingLockProfit = data.trailing_lock_profit ?? undefined;
			
			errors = {};
		} catch (e) {
			console.error('Failed to load strategy:', e);
			toast.error('Failed to load strategy details');
			onClose();
		} finally {
			loading = false;
		}
	}
	
	// Validate form
	function validateForm(): boolean {
		errors = {};
		
		// Validate stoploss levels
		if (upperStoploss !== undefined && lowerStoploss !== undefined) {
			if (upperStoploss <= lowerStoploss) {
				errors.stoploss = 'Upper stoploss must be greater than lower stoploss';
			}
		}
		
		// Validate trailing distance
		if (trailingMode !== 'none' && (!trailingDistance || trailingDistance <= 0)) {
			errors.trailing = 'Trailing distance must be greater than 0 when trailing is enabled';
		}
		
		return Object.keys(errors).length === 0;
	}
	
	// Handle save
	async function handleSave() {
		if (!strategy || !canEdit) return;
		if (!validateForm()) return;
		
		saving = true;
		try {
			// Collect only changed fields
			const updates: any = {};
			
			if (name !== strategy.name) {
				updates.name = name;
			}
			if (upperStoploss !== strategy.index_upper_stoploss) {
				updates.index_upper_stoploss = upperStoploss;
			}
			if (lowerStoploss !== strategy.index_lower_stoploss) {
				updates.index_lower_stoploss = lowerStoploss;
			}
			if (trailingMode !== strategy.trailing_mode) {
				updates.trailing_mode = trailingMode;
			}
			if (trailingDistance !== strategy.trailing_distance) {
				updates.trailing_distance = trailingDistance;
			}
			if (trailingLockProfit !== strategy.trailing_lock_profit) {
				updates.trailing_lock_profit = trailingLockProfit;
			}
			
			// Call API
			await updateStrategy(strategy.strategy_id, updates);
			
			toast.success('Strategy updated successfully');
			onUpdated?.();
			onClose();
		} catch (e) {
			const errorMsg = e instanceof Error ? e.message : 'Failed to update strategy';
			toast.error(errorMsg);
		} finally {
			saving = false;
		}
	}
	
	// Watch for dialog open/close
	$effect(() => {
		if (open && strategyId) {
			loadStrategy();
		}
	});
</script>

<Dialog bind:open>
	<DialogContent class="max-w-2xl max-h-[90vh] overflow-y-auto">
		<DialogHeader>
			<DialogTitle>Edit Strategy</DialogTitle>
			<DialogDescription>
				Update strategy parameters. Changes will take effect immediately for active strategies.
			</DialogDescription>
		</DialogHeader>
		
		{#if loading}
			<div class="flex items-center justify-center py-12">
				<div class="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full"></div>
			</div>
		{:else if !strategy}
			<div class="text-center py-8 text-muted-foreground">
				<AlertCircle class="h-12 w-12 mx-auto mb-4" />
				<p>Failed to load strategy</p>
			</div>
		{:else if !canEdit}
			<div class="text-center py-8">
				<AlertCircle class="h-12 w-12 mx-auto mb-4 text-destructive" />
				<p class="text-lg font-medium mb-2">Cannot Edit Strategy</p>
				<p class="text-sm text-muted-foreground">
					Strategies with status '{strategy.status}' cannot be edited.
				</p>
			</div>
		{:else}
			<div class="space-y-6">
				<!-- Strategy Info -->
				<div class="flex items-center gap-2 p-3 bg-muted/50 rounded-lg">
					<div class="flex-1">
						<p class="text-sm text-muted-foreground">Strategy ID</p>
						<p class="font-mono text-sm">{strategy.strategy_id.substring(0, 8)}...</p>
					</div>
					<div>
						<Badge variant={strategy.status === 'active' ? 'default' : 'secondary'}>
							{strategy.status}
						</Badge>
					</div>
				</div>
				
				<!-- Warning for active strategies -->
				{#if strategy.status === 'active' || strategy.status === 'partial'}
					<div class="flex items-start gap-2 p-3 bg-yellow-500/10 border border-yellow-500/20 rounded-lg">
						<AlertCircle class="h-5 w-5 text-yellow-600 mt-0.5" />
						<div class="flex-1 text-sm">
							<p class="font-medium text-yellow-900 dark:text-yellow-100">
								Editing Active Strategy
							</p>
							<p class="text-yellow-700 dark:text-yellow-300">
								Changes will take effect immediately. The monitoring engine will reload with new parameters.
							</p>
						</div>
					</div>
				{/if}
				
				<!-- Form Tabs -->
				<Tabs value="basic" class="w-full">
					<TabsList class="grid w-full grid-cols-2">
						<TabsTrigger value="basic">Basic & StopLoss</TabsTrigger>
						<TabsTrigger value="trailing">Trailing Config</TabsTrigger>
					</TabsList>
					
					<!-- Basic Info & StopLoss Tab -->
					<TabsContent value="basic" class="space-y-4 pt-4">
						<!-- Name -->
						<div class="space-y-2">
							<Label for="name">Strategy Name</Label>
							<Input
								id="name"
								bind:value={name}
								placeholder="Enter strategy name"
							/>
						</div>
						
						<!-- StopLoss Levels -->
						<div class="space-y-4">
							<h4 class="text-sm font-medium">Index StopLoss Levels</h4>
							
							<div class="grid grid-cols-2 gap-4">
								<div class="space-y-2">
									<Label for="upper">Upper StopLoss</Label>
									<Input
										id="upper"
										type="number"
										step="0.01"
										bind:value={upperStoploss}
										placeholder="e.g., 24500"
									/>
								</div>
								
								<div class="space-y-2">
									<Label for="lower">Lower StopLoss</Label>
									<Input
										id="lower"
										type="number"
										step="0.01"
										bind:value={lowerStoploss}
										placeholder="e.g., 24000"
									/>
								</div>
							</div>
							
							{#if errors.stoploss}
								<p class="text-sm text-destructive flex items-center gap-1">
									<AlertCircle class="h-4 w-4" />
									{errors.stoploss}
								</p>
							{/if}
						</div>
					</TabsContent>
					
					<!-- Trailing Config Tab -->
					<TabsContent value="trailing" class="space-y-4 pt-4">
						<!-- Trailing Mode -->
						<div class="space-y-2">
							<Label for="trailing-mode">Trailing Mode</Label>
							<select
								id="trailing-mode"
								bind:value={trailingMode}
								class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background"
							>
								<option value="none">None</option>
								<option value="continuous">Continuous</option>
								<option value="step">Step</option>
								<option value="atr">ATR-based</option>
							</select>
						</div>
						
						{#if trailingMode !== 'none'}
							<div class="grid grid-cols-2 gap-4">
								<div class="space-y-2">
									<Label for="trailing-distance">Trailing Distance</Label>
									<Input
										id="trailing-distance"
										type="number"
										step="0.01"
										bind:value={trailingDistance}
										placeholder="e.g., 50"
									/>
								</div>
								
								<div class="space-y-2">
									<Label for="lock-profit">Lock Profit At</Label>
									<Input
										id="lock-profit"
										type="number"
										step="0.01"
										bind:value={trailingLockProfit}
										placeholder="Optional"
									/>
								</div>
							</div>
							
							{#if errors.trailing}
								<p class="text-sm text-destructive flex items-center gap-1">
									<AlertCircle class="h-4 w-4" />
									{errors.trailing}
								</p>
							{/if}
							
							<div class="p-3 bg-blue-500/10 border border-blue-500/20 rounded-lg">
								<p class="text-sm text-blue-900 dark:text-blue-100">
									<strong>Note:</strong> Changing trailing configuration will reset any active trailing state.
								</p>
							</div>
						{/if}
					</TabsContent>
				</Tabs>
			</div>
			
			<DialogFooter>
				<Button variant="outline" onclick={onClose} disabled={saving}>
					<X class="h-4 w-4 mr-2" />
					Cancel
				</Button>
				<Button onclick={handleSave} disabled={saving || !hasChanges}>
					{#if saving}
						<div class="animate-spin h-4 w-4 border-2 border-current border-t-transparent rounded-full mr-2"></div>
						Saving...
					{:else}
						<Save class="h-4 w-4 mr-2" />
						Save Changes
					{/if}
				</Button>
			</DialogFooter>
		{/if}
	</DialogContent>
</Dialog>
