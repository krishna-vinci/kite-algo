<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { Label } from '$lib/components/ui/label';
	import { Input } from '$lib/components/ui/input';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import * as Select from '$lib/components/ui/select';
	import { Loader, BookOpen } from '@lucide/svelte';
	import { getAvailableExpiries } from '../../lib/api';
	import {
		type StrategyCategory,
		neutralStrategies,
		bullishStrategies,
		bearishStrategies,
		type StrategyTemplate
	} from '../../lib/strategy-templates';

	interface Props {
		underlying: string;
		expiry: string;
		strategyType: string;
		targetDelta: number;
		onUpdate: (data: {
			underlying?: string;
			expiry?: string;
			strategyType?: string;
			targetDelta?: number;
		}) => void;
		onNext: () => void;
	}

	let { underlying, expiry, strategyType, targetDelta, onUpdate, onNext }: Props = $props();

	let loadingExpiries = $state(false);
	let expiryDates = $state<string[]>([]);
	let spotPrice = $state<number | null>(null);
	let expiryError = $state<string | null>(null);
	
	// Strategy template selection
	let activeCategory = $state<StrategyCategory>('neutral');
	let selectedTemplate = $state<StrategyTemplate | null>(null);
	
	const categories = [
		{ id: 'bullish' as const, label: 'Bullish', icon: '📈' },
		{ id: 'bearish' as const, label: 'Bearish', icon: '📉' },
		{ id: 'neutral' as const, label: 'Neutral', icon: '📊' }
	];
	
	const strategiesByCategory = $derived(
		activeCategory === 'bullish' ? bullishStrategies :
		activeCategory === 'bearish' ? bearishStrategies :
		activeCategory === 'neutral' ? neutralStrategies :
		[]
	);

	// Fetch available expiries when underlying changes
	async function fetchExpiries(und: string) {
		if (!und) return;
		
		loadingExpiries = true;
		expiryError = null;
		try {
			const data = await getAvailableExpiries(und);
			expiryDates = data.expiries;
			spotPrice = data.spot_ltp;
			
			// Auto-select first expiry if none selected
			if (expiryDates.length > 0 && !expiry) {
				onUpdate({ expiry: expiryDates[0] });
			}
			
			toast.success(`Loaded ${expiryDates.length} expiries for ${und}`);
		} catch (e) {
			console.error('Failed to fetch expiries:', e);
			expiryError = e instanceof Error ? e.message : 'Failed to load expiries';
			expiryDates = [];
			toast.error(`Failed to load expiries: ${expiryError}`);
		} finally {
			loadingExpiries = false;
		}
	}

	// Fetch expiries on mount if underlying is set
	onMount(() => {
		if (underlying) {
			fetchExpiries(underlying);
		}
	});

	// Refetch when underlying changes
	$effect(() => {
		if (underlying) {
			fetchExpiries(underlying);
		}
	});

	const strategyTypes = [
		{ value: 'straddle', label: 'Straddle (ATM CE + PE)' },
		{ value: 'strangle', label: 'Strangle (OTM CE + PE)' },
		{ value: 'single_leg', label: 'Single Leg (CE or PE)' },
		{ value: 'manual', label: 'Custom / Manual Selection' }
	];

	function handleTemplateSelected(template: StrategyTemplate) {
		selectedTemplate = template;
		// Map template to strategy type for backward compatibility
		const strategyTypeMap: Record<string, string> = {
			'short_straddle': 'straddle',
			'long_straddle': 'straddle',
			'short_strangle': 'strangle',
			'long_strangle': 'strangle',
			'buy_call': 'single_leg',
			'sell_call': 'single_leg',
			'buy_put': 'single_leg',
			'sell_put': 'single_leg'
		};
		const mappedType = strategyTypeMap[template.id] || 'manual';
		onUpdate({ strategyType: mappedType, selectedTemplate: template });
		toast.success(`${template.name} template selected - strikes will be auto-populated in next step`);
	}

	function handleNext() {
		if (!underlying || !expiry || !strategyType) {
			return;
		}
		onNext();
	}
</script>

<Card.Root>
	<Card.Header>
		<Card.Title>Select Market & Strategy</Card.Title>
		<Card.Description>
			Choose underlying, expiry date, and strategy type
		</Card.Description>
	</Card.Header>
	<Card.Content class="space-y-6">
		<!-- Underlying Selection -->
		<div class="space-y-2">
			<div class="flex items-center justify-between">
				<Label for="underlying">Underlying</Label>
				{#if spotPrice}
					<span class="text-sm font-mono font-semibold text-green-600">
						Spot: ₹{spotPrice.toFixed(2)}
					</span>
				{/if}
			</div>
			<div class="grid grid-cols-2 gap-2">
				<Button
					variant={underlying === 'NIFTY' ? 'default' : 'outline'}
					onclick={() => onUpdate({ underlying: 'NIFTY' })}
					class="w-full"
					disabled={loadingExpiries}
				>
					NIFTY
				</Button>
				<Button
					variant={underlying === 'BANKNIFTY' ? 'default' : 'outline'}
					onclick={() => onUpdate({ underlying: 'BANKNIFTY' })}
					class="w-full"
					disabled={loadingExpiries}
				>
					BANKNIFTY
				</Button>
			</div>
		</div>

		<!-- Expiry Date Selection -->
		<div class="space-y-2">
			<div class="flex items-center justify-between">
				<Label for="expiry">Expiry Date</Label>
				{#if loadingExpiries}
					<span class="text-xs text-muted-foreground flex items-center gap-1">
						<Loader class="h-3 w-3 animate-spin" />
						Loading...
					</span>
				{/if}
			</div>
			
			{#if expiryError}
				<div class="p-3 rounded-md bg-yellow-500/10 border border-yellow-500/20 text-sm text-yellow-600">
					{expiryError}
					<p class="text-xs mt-1">
						Start an options session for {underlying} in the <a href="/nfo/option-chain" class="underline">Option Chain</a> page first.
					</p>
				</div>
			{:else}
				<select
					id="expiry"
					value={expiry}
					onchange={(e) => onUpdate({ expiry: e.currentTarget.value })}
					disabled={loadingExpiries || expiryDates.length === 0}
					class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
				>
					<option value="" disabled>
						{loadingExpiries ? 'Loading expiries...' : expiryDates.length === 0 ? 'No expiries available' : 'Select expiry date'}
					</option>
					{#each expiryDates as date}
						<option value={date}>
							{new Date(date + 'T00:00:00').toLocaleDateString('en-GB', { 
								day: '2-digit',
								month: 'short',
								year: 'numeric'
							})}
						</option>
					{/each}
				</select>
			{/if}
		</div>

		<!-- Strategy Template Selection -->
		<div class="space-y-3">
			<div class="flex items-center justify-between">
				<Label>Quick Strategy Templates</Label>
				<a
					href="https://zerodha.com/varsity/module/option-strategies/"
					target="_blank"
					class="flex items-center gap-1 text-xs text-primary hover:underline"
				>
					<BookOpen class="h-3 w-3" />
					Learn
				</a>
			</div>
			
			<!-- Category Tabs -->
			<div class="flex gap-2">
				{#each categories as category}
					<button
						type="button"
						onclick={() => (activeCategory = category.id)}
						class={`
							px-4 py-2 rounded-full text-sm font-medium transition-all
							${activeCategory === category.id
								? 'bg-primary text-primary-foreground shadow-sm'
								: 'bg-muted text-muted-foreground hover:bg-muted/80'
							}
						`}
					>
						<span class="mr-1">{category.icon}</span>
						{category.label}
					</button>
				{/each}
			</div>
			
			<!-- Strategy Cards Grid -->
			<div class="grid grid-cols-2 gap-2 max-h-60 overflow-y-auto">
				{#each strategiesByCategory as strategy}
					<button
						type="button"
						onclick={() => handleTemplateSelected(strategy)}
						class={`
							p-3 rounded-md border text-left transition-all text-sm
							hover:shadow-md hover:scale-105
							${selectedTemplate?.id === strategy.id
								? 'border-primary bg-primary/5 shadow-sm'
								: 'border-border bg-card hover:border-primary/50'
							}
						`}
					>
						<div class="font-semibold mb-1 text-xs">{strategy.name}</div>
						<div class="text-xs text-muted-foreground line-clamp-2">
							{strategy.shortDesc}
						</div>
						{#if selectedTemplate?.id === strategy.id}
							<div class="text-xs text-primary font-medium mt-1">✓ Selected</div>
						{/if}
					</button>
				{/each}
			</div>
		</div>

		<!-- OR Divider -->
		<div class="relative">
			<div class="absolute inset-0 flex items-center">
				<div class="w-full border-t"></div>
			</div>
			<div class="relative flex justify-center text-xs uppercase">
				<span class="bg-background px-2 text-muted-foreground">Or manual selection</span>
			</div>
		</div>

		<!-- Manual Strategy Type Selection -->
		<div class="space-y-2">
			<Label for="strategyType">Manual Strategy Type</Label>
			<select
				id="strategyType"
				value={strategyType}
				onchange={(e) => { onUpdate({ strategyType: e.currentTarget.value }); selectedTemplate = null; }}
				class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
			>
				<option value="" disabled>Select strategy type</option>
				{#each strategyTypes as st}
					<option value={st.value}>{st.label}</option>
				{/each}
			</select>
		</div>

		<!-- Target Delta -->
		<div class="space-y-2">
			<Label for="targetDelta">Target Delta (for strike selection)</Label>
			<Input
				id="targetDelta"
				type="number"
				step="0.05"
				min="0.10"
				max="0.50"
				value={targetDelta}
				oninput={(e) => onUpdate({ targetDelta: parseFloat(e.currentTarget.value) || 0.30 })}
			/>
			<p class="text-xs text-muted-foreground">
				Default: 0.30 (30 delta). Lower values select more OTM strikes.
			</p>
		</div>
	</Card.Content>
	<Card.Footer class="flex justify-end">
		<Button
			onclick={handleNext}
			disabled={!underlying || !expiry || !strategyType}
		>
			Next: View Option Chain
		</Button>
	</Card.Footer>
</Card.Root>
