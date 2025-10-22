<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import type { StrategyTemplate, StrategyCategory } from '../../lib/strategy-templates';
	import type { CalculatedStrike } from '../../lib/strike-calculator';
	import type { OptionChainStrike } from '../../types';
	
	import StrategySelector from './strategy-selector.svelte';
	import OptionChainLive from './option-chain-live.svelte';
	import SelectedPositions from './selected-positions.svelte';
	import { getMiniChain } from '../../lib/api';
	import { calculateStrategyStrikes, findATMStrike, enrichStrikesWithChainData } from '../../lib/strike-calculator';
	
	// Props
	interface Props {
		onComplete?: (response: any) => void;
	}
	
	let { onComplete }: Props = $props();
	
	// State
	let underlying = $state('NIFTY');
	let expiry = $state('');
	let lots = $state(1);
	let selectedTemplate = $state<StrategyTemplate | null>(null);
	let selectedStrikes = $state<CalculatedStrike[]>([]);
	let chainStrikes = $state<OptionChainStrike[]>([]);
	let atmStrike = $state(0);
	let spotPrice = $state(0);
	let loading = $state(false);
	
	// Available underlyings
	const underlyings = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY'];
	
	// Available expiries (will be fetched from API later)
	let expiries = $state<string[]>([]);
	
	// Fetch mini chain when underlying/expiry changes
	$effect(() => {
		if (underlying && expiry) {
			loadMiniChain();
		}
	});
	
	async function loadMiniChain() {
		loading = true;
		try {
			const response = await getMiniChain(underlying, expiry);
			chainStrikes = response.strikes || [];
			atmStrike = response.atm_strike || 0;
			spotPrice = response.spot_price || 0;
			
			// If we have a selected template, recalculate strikes
			if (selectedTemplate) {
				applyStrategyTemplate(selectedTemplate);
			}
		} catch (e) {
			console.error('Failed to load mini chain:', e);
			toast.error('Failed to load option chain');
		} finally {
			loading = false;
		}
	}
	
	function handleTemplateSelected(template: StrategyTemplate) {
		selectedTemplate = template;
		applyStrategyTemplate(template);
		toast.success(`${template.name} selected`);
	}
	
	function applyStrategyTemplate(template: StrategyTemplate) {
		if (atmStrike === 0) {
			toast.error('Please wait for option chain to load');
			return;
		}
		
		// Calculate strikes based on template
		let strikes = calculateStrategyStrikes(template, atmStrike, underlying);
		
		// Enrich with chain data (LTP, Greeks)
		strikes = enrichStrikesWithChainData(strikes, chainStrikes);
		
		selectedStrikes = strikes;
	}
	
	function handleStrikeAdded(strike: CalculatedStrike) {
		selectedStrikes = [...selectedStrikes, strike];
		toast.success('Strike added');
	}
	
	function handleStrikeRemoved(index: number) {
		selectedStrikes = selectedStrikes.filter((_, i) => i !== index);
		toast.success('Strike removed');
	}
	
	function handleQuickTrade(strike: number, optionType: 'CE' | 'PE', transactionType: 'BUY' | 'SELL') {
		// TODO: Implement quick trade
		toast.info(`Quick trade: ${transactionType} ${strike} ${optionType}`);
	}
	
	function handleExecuteWithoutProtection() {
		if (selectedStrikes.length === 0) {
			toast.error('Please select at least one strike');
			return;
		}
		
		toast.info('Executing without protection...');
		// TODO: Call build position API
	}
	
	function handleAddProtection() {
		if (selectedStrikes.length === 0) {
			toast.error('Please select at least one strike');
			return;
		}
		
		toast.info('Opening protection configuration...');
		// TODO: Open protection dialog
	}
	
	onMount(() => {
		// Set default expiry (nearest Thursday for NIFTY)
		const today = new Date();
		const nextThursday = getNextThursday(today);
		expiry = formatDate(nextThursday);
		expiries = [expiry]; // TODO: Fetch from API
	});
	
	function getNextThursday(date: Date): Date {
		const day = date.getDay();
		const daysUntilThursday = (4 - day + 7) % 7 || 7;
		const thursday = new Date(date);
		thursday.setDate(date.getDate() + daysUntilThursday);
		return thursday;
	}
	
	function formatDate(date: Date): string {
		const year = date.getFullYear();
		const month = String(date.getMonth() + 1).padStart(2, '0');
		const day = String(date.getDate()).padStart(2, '0');
		return `${year}-${month}-${day}`;
	}
</script>

<div class="space-y-4">
	<!-- Top Section: Underlying, Expiry, Lots -->
	<div class="flex items-center gap-4 p-4 rounded-lg border bg-card">
		<div class="flex-1">
			<label class="text-sm font-medium">Underlying</label>
			<select
				bind:value={underlying}
				class="w-full mt-1 px-3 py-2 border rounded-md bg-background"
			>
				{#each underlyings as u}
					<option value={u}>{u}</option>
				{/each}
			</select>
		</div>
		
		<div class="flex-1">
			<label class="text-sm font-medium">Expiry</label>
			<input
				type="date"
				bind:value={expiry}
				class="w-full mt-1 px-3 py-2 border rounded-md bg-background"
			/>
		</div>
		
		<div class="w-32">
			<label class="text-sm font-medium">Lots</label>
			<input
				type="number"
				bind:value={lots}
				min="1"
				class="w-full mt-1 px-3 py-2 border rounded-md bg-background"
			/>
		</div>
		
		{#if spotPrice > 0}
			<div class="text-sm">
				<div class="text-muted-foreground">Spot Price</div>
				<div class="font-semibold text-lg">{spotPrice.toFixed(2)}</div>
			</div>
		{/if}
	</div>
	
	<!-- Strategy Selector -->
	<StrategySelector
		onTemplateSelected={handleTemplateSelected}
		selectedTemplateId={selectedTemplate?.id}
	/>
	
	<!-- Option Chain -->
	<OptionChainLive
		strikes={chainStrikes}
		atmStrike={atmStrike}
		selectedStrikes={selectedStrikes}
		loading={loading}
		onStrikeAdded={handleStrikeAdded}
		onQuickTrade={handleQuickTrade}
	/>
	
	<!-- Selected Positions -->
	{#if selectedStrikes.length > 0}
		<SelectedPositions
			strikes={selectedStrikes}
			lots={lots}
			underlying={underlying}
			onRemove={handleStrikeRemoved}
		/>
		
		<!-- Action Buttons -->
		<div class="flex gap-3">
			<button
				onclick={handleAddProtection}
				class="flex-1 px-6 py-3 bg-primary text-primary-foreground rounded-md font-medium hover:bg-primary/90 transition-colors"
			>
				Add Protection
			</button>
			
			<button
				onclick={handleExecuteWithoutProtection}
				class="flex-1 px-6 py-3 border border-primary text-primary rounded-md font-medium hover:bg-primary/10 transition-colors"
			>
				Execute Without Protection
			</button>
		</div>
	{/if}
</div>
