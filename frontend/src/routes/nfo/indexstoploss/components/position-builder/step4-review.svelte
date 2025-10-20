<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import * as Card from '$lib/components/ui/card';
	import * as Table from '$lib/components/ui/table';
	import * as AlertDialog from '$lib/components/ui/alert-dialog';
	import { Loader, CheckCircle, AlertTriangle } from '@lucide/svelte';
	import { buildPosition } from '../../lib/api';
	import type { SelectedStrike, BuildPositionResponse, PositionBuildPlan } from '../../types';

	interface Props {
		underlying: string;
		expiry: string;
		strategyType: string;
		selectedStrikes: SelectedStrike[];
		protectionConfig: any;
		multiplier: number;
		onBack: () => void;
		onComplete: (response: BuildPositionResponse) => void;
	}

	let { underlying, expiry, strategyType, selectedStrikes, protectionConfig, multiplier = 1, onBack, onComplete }: Props = $props();

	let loading = $state(false);
	let dryRunPlan = $state<PositionBuildPlan | null>(null);
	let showExecuteConfirm = $state(false);
	let executing = $state(false);

	const totalCost = $derived(
		selectedStrikes.reduce((sum, strike) => {
			const orderValue = strike.ltp * strike.lot_size * strike.lots * multiplier;
			return sum + (strike.transaction_type === 'BUY' ? orderValue : -orderValue);
		}, 0)
	);

	const totalLots = $derived(
		selectedStrikes.reduce((sum, strike) => sum + (strike.lots * multiplier), 0)
	);

	async function runDryRun() {
		loading = true;
		try {
			// Apply multiplier to lots before sending
			const strikesWithMultiplier = selectedStrikes.map(s => ({
				...s,
				lots: s.lots * multiplier
			}));

			const response = await buildPosition({
				underlying,
				expiry,
				strategy_type: strategyType,
				selected_strikes: strikesWithMultiplier,  // Send manually selected strikes with multiplier
				protection_config: protectionConfig.enabled ? protectionConfig : undefined,
				place_orders: false
			});

			if (response.plan) {
				dryRunPlan = response.plan;
				toast.success('Dry run complete');
			}
		} catch (e) {
			console.error('Dry run failed:', e);
			toast.error(`Dry run failed: ${e instanceof Error ? e.message : 'Unknown error'}`);
		} finally {
			loading = false;
		}
	}

	async function executeOrders() {
		executing = true;
		try {
			// Apply multiplier to lots before sending
			const strikesWithMultiplier = selectedStrikes.map(s => ({
				...s,
				lots: s.lots * multiplier
			}));

			const response = await buildPosition({
				underlying,
				expiry,
				strategy_type: strategyType,
				selected_strikes: strikesWithMultiplier,  // Send manually selected strikes with multiplier
				protection_config: protectionConfig.enabled ? protectionConfig : undefined,
				place_orders: true
			});

			if (response.status === 'success' || response.status === 'partial') {
				toast.success('Position built successfully!');
				onComplete(response);
			} else {
				toast.error('Failed to build position');
			}
		} catch (e) {
			console.error('Execution failed:', e);
			toast.error(`Execution failed: ${e instanceof Error ? e.message : 'Unknown error'}`);
		} finally {
			executing = false;
			showExecuteConfirm = false;
		}
	}
</script>

<div class="space-y-4">
	<!-- Order Summary -->
	<Card.Root>
		<Card.Header>
			<Card.Title>Order Summary</Card.Title>
			<Card.Description>
				Review your position before execution
			</Card.Description>
		</Card.Header>
		<Card.Content class="space-y-4">
			<div class="grid grid-cols-3 gap-4 p-4 rounded-md border">
				<div>
					<p class="text-sm text-muted-foreground">Underlying</p>
					<p class="font-semibold">{underlying}</p>
				</div>
				<div>
					<p class="text-sm text-muted-foreground">Expiry</p>
					<p class="font-semibold">{new Date(expiry).toLocaleDateString()}</p>
				</div>
				<div>
					<p class="text-sm text-muted-foreground">Strategy</p>
					<Badge variant="outline">{strategyType.toUpperCase()}</Badge>
				</div>
			</div>

			<!-- Selected Strikes Table -->
			<div class="rounded-md border">
				<Table.Root>
					<Table.Header>
						<Table.Row>
							<Table.Head>Symbol</Table.Head>
							<Table.Head class="text-center">Type</Table.Head>
							<Table.Head class="text-center">Action</Table.Head>
							<Table.Head class="text-right">LTP</Table.Head>
							<Table.Head class="text-right">Lots</Table.Head>
							<Table.Head class="text-right">Qty</Table.Head>
							<Table.Head class="text-right">Value</Table.Head>
						</Table.Row>
					</Table.Header>
					<Table.Body>
						{#each selectedStrikes as strike}
							{@const qty = strike.lot_size * strike.lots * multiplier}
							{@const value = strike.ltp * qty}
							<Table.Row>
								<Table.Cell class="font-medium">{strike.tradingsymbol}</Table.Cell>
								<Table.Cell class="text-center">
									<Badge variant={strike.option_type === 'CE' ? 'default' : 'destructive'}>
										{strike.option_type}
									</Badge>
								</Table.Cell>
								<Table.Cell class="text-center">
									<Badge variant={strike.transaction_type === 'SELL' ? 'success' : 'secondary'}>
										{strike.transaction_type}
									</Badge>
								</Table.Cell>
								<Table.Cell class="text-right font-mono">₹{strike.ltp.toFixed(2)}</Table.Cell>
								<Table.Cell class="text-right font-mono">{strike.lots * multiplier}</Table.Cell>
								<Table.Cell class="text-right font-mono">{qty}</Table.Cell>
								<Table.Cell class="text-right font-mono {strike.transaction_type === 'SELL' ? 'text-green-500' : 'text-red-500'}">
									{strike.transaction_type === 'SELL' ? '+' : '-'}₹{value.toFixed(2)}
								</Table.Cell>
							</Table.Row>
						{/each}
						<Table.Row class="font-semibold bg-muted/50">
							<Table.Cell colspan="6" class="text-right">Net Premium:</Table.Cell>
							<Table.Cell class="text-right font-mono {totalCost >= 0 ? 'text-green-500' : 'text-red-500'}">
								{totalCost >= 0 ? '+' : ''}₹{totalCost.toFixed(2)}
							</Table.Cell>
						</Table.Row>
					</Table.Body>
				</Table.Root>
			</div>

			<!-- Protection Summary -->
			{#if protectionConfig.enabled}
				<div class="p-4 rounded-md border bg-blue-500/5">
					<p class="font-medium text-sm mb-2">Protection Strategy</p>
					<div class="grid grid-cols-2 gap-2 text-sm">
						<div>
							<span class="text-muted-foreground">Mode:</span>
							<Badge variant="outline" class="ml-2">{protectionConfig.monitoring_mode.toUpperCase()}</Badge>
						</div>
						{#if protectionConfig.monitoring_mode === 'index'}
							<div>
								<span class="text-muted-foreground">Upper SL:</span>
								<span class="font-mono ml-2">{protectionConfig.index_upper_stoploss}</span>
							</div>
							<div>
								<span class="text-muted-foreground">Lower SL:</span>
								<span class="font-mono ml-2">{protectionConfig.index_lower_stoploss}</span>
							</div>
							{#if protectionConfig.trailing_enabled}
								<div>
									<span class="text-muted-foreground">Trailing:</span>
									<span class="font-mono ml-2">Yes ({protectionConfig.trailing_distance} pts)</span>
								</div>
							{/if}
						{/if}
					</div>
				</div>
			{:else}
				<div class="p-4 rounded-md border bg-muted/50">
					<p class="text-sm text-muted-foreground">No protection strategy configured</p>
				</div>
			{/if}
		</Card.Content>
	</Card.Root>

	<!-- Dry Run Result -->
	{#if dryRunPlan}
		<Card.Root class="border-green-500/50">
			<Card.Header>
				<div class="flex items-center gap-2">
					<CheckCircle class="h-5 w-5 text-green-500" />
					<Card.Title>Dry Run Successful</Card.Title>
				</div>
			</Card.Header>
			<Card.Content class="space-y-2">
				<div class="grid grid-cols-3 gap-4 text-sm">
					<div>
						<span class="text-muted-foreground">Total Orders:</span>
						<span class="font-semibold ml-2">{dryRunPlan.orders.length}</span>
					</div>
					<div>
						<span class="text-muted-foreground">Est. Cost:</span>
						<span class="font-mono font-semibold ml-2">₹{dryRunPlan.estimated_cost.toFixed(2)}</span>
					</div>
					<div>
						<span class="text-muted-foreground">Est. Margin:</span>
						<span class="font-mono font-semibold ml-2">₹{dryRunPlan.estimated_margin.toFixed(2)}</span>
					</div>
				</div>
			</Card.Content>
		</Card.Root>
	{/if}

	<!-- Navigation -->
	<div class="flex justify-between items-center">
		<Button variant="outline" onclick={onBack}>
			Back
		</Button>
		<div class="flex gap-2">
			<Button
				variant="outline"
				onclick={runDryRun}
				disabled={loading || executing}
			>
				{#if loading}
					<Loader class="h-4 w-4 mr-2 animate-spin" />
				{/if}
				Run Dry Run
			</Button>
			<Button
				onclick={() => showExecuteConfirm = true}
				disabled={!dryRunPlan || executing}
			>
				{#if executing}
					<Loader class="h-4 w-4 mr-2 animate-spin" />
				{/if}
				Execute Orders
			</Button>
		</div>
	</div>
</div>

<!-- Execute Confirmation Dialog -->
<AlertDialog.Root bind:open={showExecuteConfirm}>
	<AlertDialog.Content>
		<AlertDialog.Header>
			<AlertDialog.Title class="flex items-center gap-2">
				<AlertTriangle class="h-5 w-5 text-yellow-500" />
				Confirm Order Execution
			</AlertDialog.Title>
			<AlertDialog.Description>
				This will place {selectedStrikes.length} market order(s) for {totalLots} lot(s).
				Total estimated cost: <span class="font-mono font-semibold">₹{Math.abs(totalCost).toFixed(2)}</span>
				<br /><br />
				Are you sure you want to proceed?
			</AlertDialog.Description>
		</AlertDialog.Header>
		<AlertDialog.Footer>
			<AlertDialog.Cancel disabled={executing}>Cancel</AlertDialog.Cancel>
			<AlertDialog.Action onclick={executeOrders} disabled={executing}>
				{#if executing}
					<Loader class="h-4 w-4 mr-2 animate-spin" />
				{/if}
				Execute Now
			</AlertDialog.Action>
		</AlertDialog.Footer>
	</AlertDialog.Content>
</AlertDialog.Root>
