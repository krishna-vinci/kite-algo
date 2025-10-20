<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { Label } from '$lib/components/ui/label';
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import * as Card from '$lib/components/ui/card';
	import * as Tabs from '$lib/components/ui/tabs';
	import * as Table from '$lib/components/ui/table';
	import { BookOpen, Loader, RefreshCw, Info, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, Trash2, Calculator } from '@lucide/svelte';
	import { getAvailableExpiries, getMiniChain } from '../../lib/api';
	import {
		type StrategyCategory,
		neutralStrategies,
		bullishStrategies,
		bearishStrategies,
		type StrategyTemplate
	} from '../../lib/strategy-templates';
	import { calculateStrategyStrikes, findATMStrike } from '../../lib/strike-calculator';
	import { generatePayoffData, calculateMetrics, formatCurrency } from '../../lib/payoff-calculator';
	import type { SelectedStrike, MiniChainResponse } from '../../types';
	import EnhancedPayoffChart from './enhanced-payoff-chart.svelte';

	interface Props {
		underlying: string;
		expiry: string;
		strategyType: string;
		targetDelta: number;
		selectedStrikes: SelectedStrike[];
		chainData: MiniChainResponse | null;
		chainLoading: boolean;
		multiplier: number;
		onUpdate: (data: any) => void;
		onUpdateSelectedStrikes: (strikes: SelectedStrike[]) => void;
		onNext: () => void;
		onReloadChain: () => void;
	}

	let {
		underlying,
		expiry,
		strategyType,
		targetDelta,
		selectedStrikes,
		chainData,
		chainLoading,
		multiplier = $bindable(1),
		onUpdate,
		onUpdateSelectedStrikes,
		onNext,
		onReloadChain
	}: Props = $props();

	let loadingExpiries = $state(false);
	let expiryDates = $state<string[]>([]);
	let spotPrice = $state<number | null>(null);
	let expiryError = $state<string | null>(null);
	let activeCategory = $state<StrategyCategory>('neutral');
	let selectedTemplate = $state<StrategyTemplate | null>(null);
	let targetPriceOffset = $state(0);
	let daysToExpiry = $state(0);
	let activeView = $state<'payoff' | 'greeks' | 'chain'>('payoff');
	let originalPrices = $state<Map<number, number>>(new Map());
	let sidebarCollapsed = $state(false);

	const categories = [
		{ id: 'bullish' as const, label: 'Bullish', icon: '📈' },
		{ id: 'bearish' as const, label: 'Bearish', icon: '📉' },
		{ id: 'neutral' as const, label: 'Neutral', icon: '📊' }
	];

	const strategiesByCategory = $derived(
		activeCategory === 'bullish' ? bullishStrategies :
		activeCategory === 'bearish' ? bearishStrategies :
		activeCategory === 'neutral' ? neutralStrategies : []
	);

	const currentSpot = $derived(spotPrice || chainData?.spot_price || 0);
	const projectedSpot = $derived(currentSpot * (1 + targetPriceOffset / 100));
	
	const expiryDate = $derived(() => {
		if (!expiry) return null;
		return new Date(expiry + 'T00:00:00');
	});
	
	const daysUntilExpiry = $derived(() => {
		if (!expiryDate()) return 0;
		const now = new Date();
		const diff = expiryDate()!.getTime() - now.getTime();
		return Math.max(0, Math.ceil(diff / (1000 * 60 * 60 * 24)));
	});

	const payoffData = $derived(
		selectedStrikes.length === 0 || !currentSpot
			? []
			: generatePayoffData(selectedStrikes, projectedSpot, 0.15, multiplier)
	);

	const metrics = $derived(
		selectedStrikes.length === 0 || !currentSpot
			? null
			: calculateMetrics(selectedStrikes, projectedSpot, multiplier)
	);

	const projectedPnL = $derived(() => {
		if (!metrics || payoffData.length === 0) return 0;
		const closest = payoffData.reduce((prev, curr) => {
			return Math.abs(curr.price - projectedSpot) < Math.abs(prev.price - projectedSpot)
				? curr
				: prev;
		});
		return closest.pnl;
	});

	const positionGreeks = $derived(() => {
		if (selectedStrikes.length === 0) return { delta: 0, gamma: 0, theta: 0, vega: 0 };
		return selectedStrikes.reduce((acc, strike) => {
			const multiplier = strike.transaction_type === 'BUY' ? 1 : -1;
			const qty = strike.lots * strike.lot_size;
			return {
				delta: acc.delta + (strike.delta || 0) * multiplier * qty,
				gamma: acc.gamma + (strike.gamma || 0) * multiplier * qty,
				theta: acc.theta + (strike.theta || 0) * multiplier * qty,
				vega: acc.vega + (strike.vega || 0) * multiplier * qty
			};
		}, { delta: 0, gamma: 0, theta: 0, vega: 0 });
	});

	async function fetchExpiries(und: string) {
		if (!und) return;
		loadingExpiries = true;
		expiryError = null;
		try {
			const data = await getAvailableExpiries(und);
			expiryDates = data.expiries;
			spotPrice = data.spot_ltp;
			if (expiryDates.length > 0 && !expiry) {
				onUpdate({ expiry: expiryDates[0] });
			}
		} catch (e) {
			console.error('Failed to fetch expiries:', e);
			expiryError = e instanceof Error ? e.message : 'Failed to load expiries';
			expiryDates = [];
		} finally {
			loadingExpiries = false;
		}
	}


	function handleTemplateSelected(template: StrategyTemplate) {
		selectedTemplate = template;
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
		
		if (chainData && chainData.strikes.length > 0) {
			const atmStrike = chainData.atm_strike || findATMStrike(chainData.strikes, chainData.spot_price);
			const calculatedStrikes = calculateStrategyStrikes(template, atmStrike, underlying);
			const autoStrikes: SelectedStrike[] = [];
			
			for (const calc of calculatedStrikes) {
				const chainStrike = chainData.strikes.find(s => s.strike === calc.strike);
				if (!chainStrike) continue;
				const side = calc.optionType === 'CE' ? chainStrike.ce : chainStrike.pe;
				if (!side) continue;
				
				autoStrikes.push({
					instrument_token: side.instrument_token,
					tradingsymbol: side.tradingsymbol,
					strike: calc.strike,
					option_type: calc.optionType,
					ltp: side.ltp,
					lot_size: side.lot_size,
					delta: side.greeks?.delta || 0,
					gamma: side.greeks?.gamma || 0,
					theta: side.greeks?.theta || 0,
					vega: side.greeks?.vega || 0,
					oi: side.oi,
					lots: 1,
					transaction_type: calc.transactionType
				});
			}
			
			if (autoStrikes.length > 0) {
				onUpdateSelectedStrikes(autoStrikes);
				toast.success(`${template.name} strikes selected`);
			}
		}
	}

	function toggleStrike(strike: number, optionType: 'CE' | 'PE', side: any) {
		if (!side) return;
		const existing = selectedStrikes.find(s => s.instrument_token === side.instrument_token);
		if (existing) {
			onUpdateSelectedStrikes(selectedStrikes.filter(s => s.instrument_token !== side.instrument_token));
		} else {
			const newStrike: SelectedStrike = {
				instrument_token: side.instrument_token,
				tradingsymbol: side.tradingsymbol,
				strike: strike,
				option_type: optionType,
				ltp: side.ltp,
				lot_size: side.lot_size,
				delta: side.greeks?.delta || 0,
				gamma: side.greeks?.gamma || 0,
				theta: side.greeks?.theta || 0,
				vega: side.greeks?.vega || 0,
				oi: side.oi,
				lots: 1,
				transaction_type: 'SELL'
			};
			onUpdateSelectedStrikes([...selectedStrikes, newStrike]);
		}
	}

	function isStrikeSelected(instrumentToken: number): boolean {
		return selectedStrikes.some(s => s.instrument_token === instrumentToken);
	}

	onMount(() => {
		if (underlying) fetchExpiries(underlying);
	});

	$effect(() => {
		if (underlying) fetchExpiries(underlying);
	});

	$effect(() => {
		if (chainData?.spot_price) {
			spotPrice = chainData.spot_price;
		}
	});

	$effect(() => {
		if (expiryDate()) {
			daysToExpiry = daysUntilExpiry();
		}
	});

	// Store original prices when strikes are selected
	$effect(() => {
		selectedStrikes.forEach(strike => {
			if (!originalPrices.has(strike.instrument_token)) {
				originalPrices.set(strike.instrument_token, strike.ltp);
			}
		});
	});

	function updateStrikeField(instrumentToken: number, field: string, value: any) {
		const updated = selectedStrikes.map(s => 
			s.instrument_token === instrumentToken ? { ...s, [field]: value } : s
		);
		onUpdateSelectedStrikes(updated);
	}

	function removeStrike(instrumentToken: number) {
		onUpdateSelectedStrikes(selectedStrikes.filter(s => s.instrument_token !== instrumentToken));
	}

	function resetPrices() {
		if (!chainData) return;
		
		const updated = selectedStrikes.map(strike => {
			const chainStrike = chainData.strikes.find(s => s.strike === strike.strike);
			if (!chainStrike) return strike;
			
			const side = strike.option_type === 'CE' ? chainStrike.ce : chainStrike.pe;
			if (!side) return strike;
			
			return { ...strike, ltp: side.ltp };
		});
		
		onUpdateSelectedStrikes(updated);
		toast.success('Prices reset to current market values');
	}

	function adjustStrike(instrumentToken: number, direction: 'up' | 'down') {
		if (!chainData) return;
		
		const strike = selectedStrikes.find(s => s.instrument_token === instrumentToken);
		if (!strike) return;
		
		const currentIndex = chainData.strikes.findIndex(s => s.strike === strike.strike);
		if (currentIndex === -1) return;
		
		const newIndex = direction === 'up' ? currentIndex + 1 : currentIndex - 1;
		if (newIndex < 0 || newIndex >= chainData.strikes.length) return;
		
		const newStrikeData = chainData.strikes[newIndex];
		const side = strike.option_type === 'CE' ? newStrikeData.ce : newStrikeData.pe;
		if (!side) return;
		
		const updated = selectedStrikes.map(s => {
			if (s.instrument_token === instrumentToken) {
				return {
					...s,
					strike: newStrikeData.strike,
					instrument_token: side.instrument_token,
					tradingsymbol: side.tradingsymbol,
					ltp: side.ltp,
					lot_size: side.lot_size,
					delta: side.greeks?.delta || 0,
					gamma: side.greeks?.gamma || 0,
					theta: side.greeks?.theta || 0,
					vega: side.greeks?.vega || 0,
					oi: side.oi
				};
			}
			return s;
		});
		
		onUpdateSelectedStrikes(updated);
	}
</script>

<div class="grid grid-cols-1 gap-4" class:lg:grid-cols-[480px_1fr]={!sidebarCollapsed} class:lg:grid-cols-[48px_1fr]={sidebarCollapsed}>
	<!-- SIDEBAR -->
	<div class="space-y-3 overflow-y-auto max-h-[calc(100vh-200px)] relative">
		<!-- Collapse Toggle Button -->
		<button
			type="button"
			onclick={() => sidebarCollapsed = !sidebarCollapsed}
			class="absolute top-2 right-2 z-10 h-8 w-8 rounded-md border border-input bg-background hover:bg-accent flex items-center justify-center transition-all"
			title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
		>
			{#if sidebarCollapsed}
				<ChevronsRight class="h-4 w-4" />
			{:else}
				<ChevronsLeft class="h-4 w-4" />
			{/if}
		</button>

		{#if !sidebarCollapsed}
		<Card.Root>
			<Card.Header class="pb-3">
				<Card.Title class="text-base">Build Strategy</Card.Title>
				<Card.Description class="text-xs">Market, template & strikes</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-3">
				<!-- Underlying -->
				<div class="space-y-2">
					<Label class="text-xs">Underlying</Label>
					<div class="grid grid-cols-2 gap-2">
						<Button
							variant={underlying === 'NIFTY' ? 'default' : 'outline'}
							onclick={() => onUpdate({ underlying: 'NIFTY' })}
							size="sm"
						>
							NIFTY
						</Button>
						<Button
							variant={underlying === 'BANKNIFTY' ? 'default' : 'outline'}
							onclick={() => onUpdate({ underlying: 'BANKNIFTY' })}
							size="sm"
						>
							BANKNIFTY
						</Button>
					</div>
					{#if spotPrice}
						<div class="text-xs text-center text-green-600 font-mono font-semibold">
							₹{spotPrice.toFixed(2)}
						</div>
					{/if}
				</div>

				<!-- Expiry Date -->
				<div class="space-y-2">
					<Label class="text-xs">Expiry Date</Label>
					<select
						value={expiry}
						onchange={(e) => onUpdate({ expiry: e.currentTarget.value })}
						class="flex h-8 w-full rounded-md border border-input bg-background px-2 py-1 text-xs"
					>
						<option value="" disabled>Select expiry</option>
						{#each expiryDates as date}
							<option value={date}>
								{new Date(date + 'T00:00:00').toLocaleDateString('en-GB', {
									day: '2-digit',
									month: 'short'
								})}
							</option>
						{/each}
					</select>
				</div>

				<!-- Strategy Templates -->
				<div class="space-y-2">
					<div class="flex items-center justify-between">
						<Label class="text-xs">Strategy Templates</Label>
						<a
							href="https://zerodha.com/varsity/module/option-strategies/"
							target="_blank"
							class="text-xs text-primary hover:underline"
						>
							<BookOpen class="h-3 w-3 inline" />
						</a>
					</div>

					<!-- Category Tabs -->
					<div class="flex gap-1">
						{#each categories as category}
							<button
								type="button"
								onclick={() => (activeCategory = category.id)}
								class={`
									px-2 py-1 rounded text-[10px] font-medium transition-all
									${activeCategory === category.id
										? 'bg-primary text-primary-foreground'
										: 'bg-muted text-muted-foreground'
									}
								`}
							>
								{category.icon}
							</button>
						{/each}
					</div>

					<!-- Strategy Cards - 2 Column Grid -->
					<div class="grid grid-cols-2 gap-1.5 max-h-72 overflow-y-auto">
						{#each strategiesByCategory as strategy}
							<button
								type="button"
								onclick={() => handleTemplateSelected(strategy)}
								class={`
									p-2 rounded border text-left transition-all
									${selectedTemplate?.id === strategy.id
										? 'border-primary bg-primary/5'
										: 'border-border hover:border-primary/50'
									}
								`}
							>
								<div class="text-xs font-semibold truncate">{strategy.name}</div>
								<div class="text-[10px] text-muted-foreground line-clamp-2">
									{strategy.shortDesc}
								</div>
							</button>
						{/each}
					</div>
				</div>
			</Card.Content>
		</Card.Root>

		<!-- Strategy Details -->
		{#if selectedStrikes.length > 0}
			<div>
				<!-- Header -->
				<div class="flex items-center justify-between mb-2 px-1">
					<div class="flex items-center gap-2 text-sm font-medium">
						<input type="checkbox" checked class="h-4 w-4 rounded border-gray-300" />
						<label class="select-none">{selectedStrikes.length} trades selected</label>
					</div>
					<Button variant="link" class="h-auto p-0 text-primary" onclick={resetPrices}>
						<RefreshCw class="h-3.5 w-3.5 mr-1" />
						Reset Prices
					</Button>
				</div>

				<!-- Table -->
				<div class="space-y-2">
					<!-- Table Header -->
					<div class="grid grid-cols-[auto_40px_90px_110px_40px_80px_auto] gap-x-2 px-1 text-xs text-muted-foreground font-medium">
						<div /> <!-- Checkbox col -->
						<div>B/S</div>
						<div>Expiry</div>
						<div class="text-center">Strike</div>
						<div>Type</div>
						<div class="text-center">Price</div>
						<div /> <!-- Actions col -->
					</div>

					<!-- Table Body -->
					{#each selectedStrikes as strike, i}
						<div class="grid grid-cols-[auto_40px_90px_110px_40px_80px_auto] gap-x-2 items-center">
							<input type="checkbox" checked class="h-4 w-4 rounded border-gray-300" />
							
							<!-- B/S -->
							<Button
								variant="outline"
								class={`h-8 w-8 text-xs font-bold ${strike.transaction_type === 'BUY' ? 'bg-blue-50 text-blue-700 border-blue-200 hover:bg-blue-100' : 'bg-red-50 text-red-700 border-red-200 hover:bg-red-100'}`}
								onclick={() => updateStrikeField(strike.instrument_token, 'transaction_type', strike.transaction_type === 'BUY' ? 'SELL' : 'BUY')}
							>
								{strike.transaction_type === 'BUY' ? 'B' : 'S'}
							</Button>

							<!-- Expiry -->
							<select
								class="h-8 w-full rounded-md border border-input bg-transparent px-1 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
								value={expiry}
								onchange={(e) => onUpdate({ expiry: e.currentTarget.value })}
							>
								{#each expiryDates as date}
									<option value={date}>
										{new Date(date + 'T00:00:00').toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })}
									</option>
								{/each}
							</select>

							<!-- Strike -->
							<div class="flex items-center">
								<Button variant="outline" size="icon" class="h-8 w-7 rounded-r-none" onclick={() => adjustStrike(strike.instrument_token, 'down')}>-</Button>
								<input type="number" class="h-8 w-full text-center text-xs rounded-none border-y focus-visible:ring-offset-0 focus-visible:ring-0" value={strike.strike} />
								<Button variant="outline" size="icon" class="h-8 w-7 rounded-l-none" onclick={() => adjustStrike(strike.instrument_token, 'up')}>+</Button>
							</div>

							<!-- Type -->
							<div class="text-center text-sm font-medium text-muted-foreground">{strike.option_type}</div>

							<!-- Price -->
							<input type="number" class="h-8 text-center text-xs border rounded-md" value={strike.ltp} oninput={(e) => updateStrikeField(strike.instrument_token, 'ltp', parseFloat(e.currentTarget.value))} />

							<!-- Actions -->
							<div class="flex items-center justify-center">
								<Button variant="ghost" size="icon" class="h-8 w-8" onclick={() => removeStrike(strike.instrument_token)}>
									
								</Button>
							</div>
						</div>
					{/each}
				</div>

				<!-- Footer -->
				<div class="mt-4 flex items-end justify-between">
					<div class="flex items-end gap-4">
						<!-- Multiplier -->
						<div class="flex items-center gap-2">
							<label for="multiplier" class="text-sm font-medium">Multiplier</label>
							<select bind:value={multiplier} class="h-8 w-20 rounded-md border border-input bg-transparent px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-ring">
								{#each Array.from({ length: 50 }, (_, i) => i + 1) as val}
									<option value={val}>{val}</option>
								{/each}
							</select>
						</div>

						<!-- Price Pay -->
						<div>
							<div class="text-xs text-muted-foreground">Price Pay</div>
							<div class="text-sm font-semibold">{Math.abs(metrics?.netPremium || 0).toFixed(1)}</div>
						</div>

						<!-- Premium Pay -->
						<div>
							<div class="text-xs text-muted-foreground">Premium Pay</div>
							<div class="text-sm font-semibold">{(Math.abs(metrics?.netPremium || 0) * multiplier * (selectedStrikes[0]?.lot_size || 1)).toLocaleString('en-IN')}</div>
						</div>
					</div>

					<!-- Charges -->
					<Button variant="link" class="h-auto p-0 text-primary">
						<Calculator class="h-4 w-4 mr-1" />
						Charges
					</Button>
				</div>
			</div>
		{/if}
	{:else}
		<!-- Collapsed sidebar view -->
		<div class="flex flex-col items-center gap-2 pt-16">
			<div class="writing-mode-vertical text-xs font-medium text-muted-foreground">
				Build Strategy
			</div>
		</div>
	{/if}
	</div>

	<!-- MAIN CONTENT -->
	<div class="space-y-3">
		<!-- Metrics - Compact -->
		{#if metrics && selectedStrikes.length > 0}
			<div class="grid grid-cols-4 gap-1.5">
				<Card.Root>
					<Card.Content class="p-2">
						<div class="text-[9px] text-muted-foreground uppercase">Max Profit</div>
						<div class="text-sm font-bold text-green-600">
							{#if metrics.maxProfit === 'unlimited'}
								Unlimited
							{:else}
								{formatCurrency(metrics.maxProfit)}
							{/if}
						</div>
					</Card.Content>
				</Card.Root>
				<Card.Root>
					<Card.Content class="p-2">
						<div class="text-[9px] text-muted-foreground uppercase">Max Loss</div>
						<div class="text-sm font-bold text-red-600">
							{#if metrics.maxLoss === 'unlimited'}
								Unlimited
							{:else}
								{formatCurrency(metrics.maxLoss, false)}
							{/if}
						</div>
					</Card.Content>
				</Card.Root>
				<Card.Root>
					<Card.Content class="p-2">
						<div class="text-[9px] text-muted-foreground uppercase">R/R</div>
						<div class="text-sm font-bold">1.03</div>
					</Card.Content>
				</Card.Root>
				<Card.Root>
					<Card.Content class="p-2">
						<div class="text-[9px] text-muted-foreground uppercase">Premium</div>
						<div class="text-sm font-bold">{formatCurrency(metrics.netPremium)}</div>
					</Card.Content>
				</Card.Root>
			</div>

		{/if}

		<!-- Main Tabs -->
		<Card.Root>
			<Card.Header class="pb-2">
				<Tabs.Root bind:value={activeView}>
					<Tabs.List class="grid grid-cols-2">
						<Tabs.Trigger value="payoff">Payoff Graph</Tabs.Trigger>
						<Tabs.Trigger value="chain">Option Chain</Tabs.Trigger>
					</Tabs.List>
				</Tabs.Root>
			</Card.Header>
			<Card.Content>
				{#if activeView === 'payoff'}
					{#if selectedStrikes.length === 0}
						<div class="py-12 text-center text-muted-foreground">
							<p>Select a strategy to view payoff</p>
						</div>
					{:else}
						<EnhancedPayoffChart
							strikes={selectedStrikes}
							spotPrice={currentSpot}
							underlying={underlying}
							bind:targetPriceOffset
							bind:daysToExpiry
							maxDaysToExpiry={daysUntilExpiry()}
							expiry={expiry}
							chainData={chainData}
							positionGreeks={positionGreeks()}
						/>
					{/if}
				{:else if activeView === 'chain'}
					<div class="space-y-2">
						<div class="flex justify-between items-center">
							<span class="text-sm font-semibold">Option Chain</span>
							<Button variant="ghost" size="sm" onclick={onReloadChain}>
								<RefreshCw class="h-4 w-4 {chainLoading ? 'animate-spin' : ''}" />
							</Button>
						</div>
						{#if chainLoading}
							<div class="py-12 text-center">
								<Loader class="h-8 w-8 animate-spin mx-auto" />
							</div>
						{:else if chainData && chainData.strikes.length > 0}
							<div class="max-h-96 overflow-y-auto">
								<Table.Root>
									<Table.Header>
										<Table.Row>
											<Table.Head class="text-xs">Strike</Table.Head>
											<Table.Head class="text-xs">CE LTP</Table.Head>
											<Table.Head class="text-xs">PE LTP</Table.Head>
											<Table.Head class="text-xs">Actions</Table.Head>
										</Table.Row>
									</Table.Header>
									<Table.Body>
										{#each chainData.strikes.slice(0, 15) as strike}
											<Table.Row class={strike.is_atm ? 'bg-primary/5' : ''}>
												<Table.Cell class="font-mono text-sm">
													{strike.strike}
													{#if strike.is_atm}<Badge class="ml-1 text-[10px]">ATM</Badge>{/if}
												</Table.Cell>
												<Table.Cell class="font-mono text-sm">
													{strike.ce ? `₹${strike.ce.ltp.toFixed(2)}` : '—'}
												</Table.Cell>
												<Table.Cell class="font-mono text-sm">
													{strike.pe ? `₹${strike.pe.ltp.toFixed(2)}` : '—'}
												</Table.Cell>
												<Table.Cell>
													<div class="flex gap-1">
														{#if strike.ce}
															<Button
																variant={isStrikeSelected(strike.ce.instrument_token) ? 'default' : 'ghost'}
																size="sm"
																onclick={() => toggleStrike(strike.strike, 'CE', strike.ce)}
															>
																CE
															</Button>
														{/if}
														{#if strike.pe}
															<Button
																variant={isStrikeSelected(strike.pe.instrument_token) ? 'default' : 'ghost'}
																size="sm"
																onclick={() => toggleStrike(strike.strike, 'PE', strike.pe)}
															>
																PE
															</Button>
														{/if}
													</div>
												</Table.Cell>
											</Table.Row>
										{/each}
									</Table.Body>
								</Table.Root>
							</div>
						{:else}
							<div class="py-8 text-center text-muted-foreground">
								<p>No chain data available</p>
							</div>
						{/if}
					</div>
				{/if}
			</Card.Content>
		</Card.Root>

		<!-- Next Button -->
		{#if selectedStrikes.length > 0}
			<div class="flex justify-end">
				<Button onclick={onNext}>
					Next: Configure Protection ({selectedStrikes.length} selected)
				</Button>
			</div>
		{/if}
	</div>
</div>

<style>
	.writing-mode-vertical {
		writing-mode: vertical-rl;
		text-orientation: mixed;
	}
</style>
