<script lang="ts">
	import { toast } from 'svelte-sonner';
	import {
		Sheet,
		SheetContent,
		SheetDescription,
		SheetHeader,
		SheetTitle
	} from '$lib/components/ui/sheet';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { Tabs, TabsList, TabsTrigger, TabsContent } from '$lib/components/ui/tabs';
	import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card';
	import { Switch } from '$lib/components/ui/switch';
	import { Separator } from '$lib/components/ui/separator';
	import { Loader2 } from '@lucide/svelte';
	
	import { createProtectionStrategy } from '../lib/api';
	import { getRealtimePositions } from '../lib/api';
	import type {
		CreateProtectionRequest,
		MonitoringMode,
		StrategyType,
		OrderType,
		TrailingMode,
		ExitLogic,
		CombinedPremiumEntryType,
		RealtimePosition
	} from '../types';
	
	interface Props {
		open: boolean;
		onClose: () => void;
		onCreated?: () => void;
	}
	
	let { open = $bindable(), onClose, onCreated }: Props = $props();
	
	// Form state
	let loading = $state(false);
	let loadingPositions = $state(false);
	let selectedMode = $state<MonitoringMode>('index');
	
	// Basic fields
	let strategyName = $state('');
	let strategyType = $state<StrategyType>('manual');
	let notes = $state('');
	
	// Index config
	let indexToken = $state(256265); // NIFTY 50 default
	let indexSymbol = $state('NIFTY 50');
	let indexExchange = $state('NSE');
	let upperStoploss = $state<number | ''>('');
	let lowerStoploss = $state<number | ''>('');
	let upperTarget = $state<number | ''>(''); // Profit target if index reaches this (upside)
	let lowerTarget = $state<number | ''>(''); // Profit target if index reaches this (downside)
	let orderType = $state<OrderType>('MARKET');
	let limitOffset = $state<number | ''>('');
	
	// Smart SL helper
	let protectionDirection = $state<'both' | 'upside' | 'downside'>('both');
	
	// Trailing config
	let trailingEnabled = $state(false);
	let trailingMode = $state<TrailingMode>('continuous');
	let trailingDistance = $state<number | ''>('');
	let trailingUnit = $state<'points' | 'percent'>('points');
	let trailingLockProfit = $state<number | ''>('');
	
	// Premium config (simplified for now - user will configure per position)
	let premiumConfigEnabled = $state(false);
	
	// Hybrid config
	let exitLogic = $state<ExitLogic>('any');
	
	// Combined premium config
	let combinedEntryType = $state<CombinedPremiumEntryType>('credit');
	let combinedProfitTarget = $state<number | ''>('');
	let combinedTrailingEnabled = $state(false);
	let combinedTrailingDistance = $state<number | ''>('');
	let combinedTrailingLockProfit = $state<number | ''>('');
	
	// Position filter
	let filterExchange = $state('NFO');
	let filterProduct = $state('MIS');
	let filterSymbols = $state('');
	let filterTokens = $state('');
	
	// Position preview
	let matchedPositions = $state<RealtimePosition[]>([]);
	let totalLots = $state(0);
	
	// Load positions for preview
	async function loadPositionPreview() {
		if (!open) return;
		
		loadingPositions = true;
		try {
			const positions = await getRealtimePositions();
			const allPositions = [...(positions.net || []), ...(positions.day || [])];
			
			// Filter positions
			const filtered = allPositions.filter(pos => {
				if (filterExchange && pos.exchange !== filterExchange) return false;
				if (filterProduct && pos.product !== filterProduct) return false;
				if (filterSymbols) {
					const symbols = filterSymbols.split(',').map(s => s.trim().toUpperCase());
					if (!symbols.includes(pos.tradingsymbol.toUpperCase())) return false;
				}
				if (filterTokens) {
					const tokens = filterTokens.split(',').map(s => parseInt(s.trim()));
					if (!tokens.includes(pos.instrument_token)) return false;
				}
				return pos.quantity !== 0; // Only non-zero positions
			});
			
			matchedPositions = filtered;
			// Calculate total lots (assuming lot size of 25 for NFO - this is simplified)
			totalLots = filtered.reduce((sum, pos) => sum + Math.abs(pos.quantity) / 25, 0);
			
		} catch (e) {
			console.error('Failed to load positions:', e);
			toast.error('Failed to load positions preview');
		} finally {
			loadingPositions = false;
		}
	}
	
	// Watch for filter changes
	$effect(() => {
		if (open) {
			loadPositionPreview();
		}
	});
	
	// Validate form
	function validateForm(): string | null {
		if (!strategyName.trim()) {
			return 'Please provide a strategy name';
		}
		
		if (selectedMode === 'index' || selectedMode === 'combined_premium') {
			if (!indexToken) return 'Index token is required';
			if (!upperStoploss && !lowerStoploss && !upperTarget && !lowerTarget) {
				return 'At least one exit condition (stop-loss or target) is required for index monitoring';
			}
		}
		
		if (selectedMode === 'combined_premium') {
			if (!combinedProfitTarget && !combinedTrailingEnabled) {
				return 'Combined premium mode requires at least profit target or trailing';
			}
		}
		
		// Position filter is now optional - allow creating strategies for future positions
		// No validation needed here
		
		return null;
	}
	
	// Handle submit
	async function handleSubmit() {
		const validationError = validateForm();
		if (validationError) {
			toast.error(validationError);
			return;
		}
		
		loading = true;
		
		try {
			// Build request
			const request: CreateProtectionRequest = {
				name: strategyName,
				strategy_type: strategyType,
				notes: notes || undefined,
				monitoring_mode: selectedMode,
				position_filter: {
					exchange: filterExchange || undefined,
					product: filterProduct || undefined,
					tradingsymbols: filterSymbols ? filterSymbols.split(',').map(s => s.trim()) : undefined,
					instrument_tokens: filterTokens ? filterTokens.split(',').map(s => parseInt(s.trim())) : undefined
				}
			};
			
			// Add mode-specific config
			if (selectedMode === 'index' || selectedMode === 'combined_premium') {
				request.index_instrument_token = indexToken;
				request.index_tradingsymbol = indexSymbol;
				request.index_exchange = indexExchange;
				request.index_upper_stoploss = upperStoploss || undefined;
				request.index_lower_stoploss = lowerStoploss || undefined;
				request.index_upper_target = upperTarget || undefined;
				request.index_lower_target = lowerTarget || undefined;
				request.stoploss_order_type = orderType;
				request.stoploss_limit_offset = limitOffset || undefined;
				
				if (trailingEnabled) {
					request.trailing_mode = trailingMode;
					request.trailing_distance = trailingDistance || undefined;
					request.trailing_unit = trailingUnit;
					request.trailing_lock_profit = trailingLockProfit || undefined;
				}
			}
			
			if (selectedMode === 'combined_premium') {
				request.combined_premium_entry_type = combinedEntryType;
				request.combined_premium_profit_target = combinedProfitTarget || undefined;
				request.combined_premium_trailing_enabled = combinedTrailingEnabled;
				request.combined_premium_trailing_distance = combinedTrailingDistance || undefined;
				request.combined_premium_trailing_lock_profit = combinedTrailingLockProfit || undefined;
			}
			
			// Create strategy
			const result = await createProtectionStrategy(request);
			
			toast.success(`Strategy "${strategyName}" created successfully!`);
			
			// Reset form
			resetForm();
			
			// Notify parent
			if (onCreated) onCreated();
			onClose();
			
		} catch (e) {
			console.error('Failed to create strategy:', e);
			toast.error(`Failed to create strategy: ${e instanceof Error ? e.message : 'Unknown error'}`);
		} finally {
			loading = false;
		}
	}
	
	function resetForm() {
		strategyName = '';
		strategyType = 'manual';
		notes = '';
		selectedMode = 'index';
		upperStoploss = '';
		lowerStoploss = '';
		upperTarget = '';
		lowerTarget = '';
		protectionDirection = 'both';
		trailingEnabled = false;
		trailingDistance = '';
		premiumConfigEnabled = false;
		combinedProfitTarget = '';
		filterSymbols = '';
		filterTokens = '';
		matchedPositions = [];
	}
</script>

<Sheet bind:open>
	<SheetContent class="sm:max-w-[800px] overflow-y-auto">
		<SheetHeader>
			<SheetTitle>Create Protection Strategy</SheetTitle>
			<SheetDescription>
				Set up automated protection for your existing positions
			</SheetDescription>
		</SheetHeader>
		
		<div class="space-y-6 py-6">
			<!-- Basic Info -->
			<div class="space-y-4">
				<div class="grid gap-3">
					<Label for="name">Strategy Name *</Label>
					<Input
						id="name"
						bind:value={strategyName}
						placeholder="e.g., NIFTY Short Straddle Protection"
					/>
				</div>
				
				<div class="grid gap-3">
					<Label for="type">Strategy Type</Label>
					<select
						id="type"
						bind:value={strategyType}
						class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
					>
						<option value="manual">Manual</option>
						<option value="straddle">Straddle</option>
						<option value="strangle">Strangle</option>
						<option value="iron_condor">Iron Condor</option>
						<option value="single_leg">Single Leg</option>
					</select>
				</div>
				
				<div class="grid gap-3">
					<Label for="notes">Notes (Optional)</Label>
					<Input
						id="notes"
						bind:value={notes}
						placeholder="Additional notes about this strategy"
					/>
				</div>
			</div>
			
			<Separator />
			
			<!-- Monitoring Mode Selection -->
			<div>
				<h3 class="text-lg font-semibold mb-4">Monitoring Mode</h3>
				
				<Tabs bind:value={selectedMode} class="w-full">
					<TabsList class="grid w-full grid-cols-2">
						<TabsTrigger value="index">INDEX</TabsTrigger>
						<TabsTrigger value="combined_premium">COMBINED</TabsTrigger>
					</TabsList>
					
					<!-- INDEX Mode -->
					<TabsContent value="index" class="space-y-4 mt-4">
						<Card>
							<CardHeader>
								<CardTitle>Index-Based Protection</CardTitle>
								<CardDescription>
									Exit positions when index breaches stop-loss levels
								</CardDescription>
							</CardHeader>
							<CardContent class="space-y-4">
								<div class="grid grid-cols-2 gap-4">
									<div class="space-y-2">
										<Label for="index-token">Index Token</Label>
										<Input
											id="index-token"
											type="number"
											bind:value={indexToken}
											placeholder="256265"
										/>
									</div>
									<div class="space-y-2">
										<Label for="index-symbol">Index Symbol</Label>
										<Input
											id="index-symbol"
											bind:value={indexSymbol}
											placeholder="NIFTY 50"
										/>
									</div>
								</div>
								
								<!-- Protection Direction Helper -->
								<div class="p-3 bg-blue-50 dark:bg-blue-950 rounded-md border border-blue-200 dark:border-blue-800">
									<p class="text-sm font-medium mb-2">💡 Protection Direction Guide:</p>
									<ul class="text-xs space-y-1 text-muted-foreground">
										<li><strong>Both:</strong> Neutral strategies (straddle/strangle) - protect both sides</li>
										<li><strong>Upside only:</strong> Bearish/short strategies - protect against rallies (upper SL only)</li>
										<li><strong>Downside only:</strong> Bullish/long strategies - protect against falls (lower SL only)</li>
									</ul>
								</div>
								
								<div class="space-y-2">
									<Label for="protection-dir">Protection Direction</Label>
									<select
										id="protection-dir"
										bind:value={protectionDirection}
										class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
									>
										<option value="both">Both Sides (Bracket Protection)</option>
										<option value="upside">Upside Only (For Short/Bearish Positions)</option>
										<option value="downside">Downside Only (For Long/Bullish Positions)</option>
									</select>
								</div>
								
								<!-- Stop-Loss Levels -->
								<div class="grid grid-cols-2 gap-4">
									<div class="space-y-2">
										<Label for="upper-sl">
											Upper Stop-Loss {protectionDirection === 'downside' ? '(Optional)' : ''}
										</Label>
										<Input
											id="upper-sl"
											type="number"
											bind:value={upperStoploss}
											placeholder="e.g., 24500"
											disabled={protectionDirection === 'downside'}
										/>
										<p class="text-xs text-muted-foreground">
											{protectionDirection === 'downside' ? 'Not needed for bullish positions' : 'Exit if index ≥ this (protect from rally)'}
										</p>
									</div>
									<div class="space-y-2">
										<Label for="lower-sl">
											Lower Stop-Loss {protectionDirection === 'upside' ? '(Optional)' : ''}
										</Label>
										<Input
											id="lower-sl"
											type="number"
											bind:value={lowerStoploss}
											placeholder="e.g., 23500"
											disabled={protectionDirection === 'upside'}
										/>
										<p class="text-xs text-muted-foreground">
											{protectionDirection === 'upside' ? 'Not needed for bearish positions' : 'Exit if index ≤ this (protect from crash)'}
										</p>
									</div>
								</div>
								
								<!-- Profit Target Levels (NEW!) -->
								<div class="grid grid-cols-2 gap-4">
									<div class="space-y-2">
										<Label for="upper-target">
											Upper Profit Target (Optional)
										</Label>
										<Input
											id="upper-target"
											type="number"
											bind:value={upperTarget}
											placeholder="e.g., 25000"
										/>
										<p class="text-xs text-muted-foreground">Take profit if index ≥ this</p>
									</div>
									<div class="space-y-2">
										<Label for="lower-target">
											Lower Profit Target (Optional)
										</Label>
										<Input
											id="lower-target"
											type="number"
											bind:value={lowerTarget}
											placeholder="e.g., 23000"
										/>
										<p class="text-xs text-muted-foreground">Take profit if index ≤ this</p>
									</div>
								</div>
								
								<div class="space-y-3">
									<div class="flex items-center justify-between">
										<Label for="trailing">Enable Trailing Stop-Loss</Label>
										<Switch id="trailing" bind:checked={trailingEnabled} />
									</div>
									
									{#if trailingEnabled}
										<div class="grid grid-cols-2 gap-4 pl-4 border-l-2">
											<div class="space-y-2">
												<Label for="trail-distance">Trailing Distance</Label>
												<Input
													id="trail-distance"
													type="number"
													bind:value={trailingDistance}
													placeholder="e.g., 50"
												/>
											</div>
											<div class="space-y-2">
												<Label for="trail-unit">Unit</Label>
												<select
													id="trail-unit"
													bind:value={trailingUnit}
													class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
												>
													<option value="points">Points</option>
													<option value="percent">Percent</option>
												</select>
											</div>
										</div>
									{/if}
								</div>
							</CardContent>
						</Card>
					</TabsContent>
					
					
					<!-- COMBINED PREMIUM Mode -->
					<TabsContent value="combined_premium" class="space-y-4 mt-4">
						<Card>
							<CardHeader>
								<CardTitle>Combined Premium Protection</CardTitle>
								<CardDescription>
									Monitor net premium across all positions with partial exits
								</CardDescription>
							</CardHeader>
							<CardContent class="space-y-4">
								<div class="grid grid-cols-2 gap-4">
									<div class="space-y-2">
										<Label for="entry-type">Entry Type</Label>
										<select
											id="entry-type"
											bind:value={combinedEntryType}
											class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
										>
											<option value="credit">CREDIT (Sell strategies)</option>
											<option value="debit">DEBIT (Buy strategies)</option>
										</select>
									</div>
									<div class="space-y-2">
										<Label for="profit-target">Profit Target (points)</Label>
										<Input
											id="profit-target"
											type="number"
											bind:value={combinedProfitTarget}
											placeholder="e.g., 50"
										/>
									</div>
								</div>
								
								<div class="space-y-3">
									<div class="flex items-center justify-between">
										<Label for="combined-trailing">Enable Trailing</Label>
										<Switch id="combined-trailing" bind:checked={combinedTrailingEnabled} />
									</div>
									
									{#if combinedTrailingEnabled}
										<div class="grid grid-cols-2 gap-4 pl-4 border-l-2">
											<div class="space-y-2">
												<Label for="combined-trail-distance">Trailing Distance</Label>
												<Input
													id="combined-trail-distance"
													type="number"
													bind:value={combinedTrailingDistance}
													placeholder="e.g., 20"
												/>
											</div>
											<div class="space-y-2">
												<Label for="combined-lock-profit">Lock Profit After</Label>
												<Input
													id="combined-lock-profit"
													type="number"
													bind:value={combinedTrailingLockProfit}
													placeholder="e.g., 30"
												/>
											</div>
										</div>
									{/if}
								</div>
								
								<p class="text-xs text-muted-foreground">
									Index bracket protection (upper/lower SL) is also required for safety
								</p>
							</CardContent>
						</Card>
					</TabsContent>
				</Tabs>
			</div>
			
			<Separator />
			
			<!-- Position Filter -->
			<div>
				<div class="flex items-center justify-between mb-4">
					<h3 class="text-lg font-semibold">Position Filter (Optional)</h3>
					<span class="text-xs text-muted-foreground">Leave blank to protect all positions</span>
				</div>
				
				<Card>
					<CardContent class="space-y-4 pt-6">
						<div class="grid grid-cols-2 gap-4">
							<div class="space-y-2">
								<Label for="filter-exchange">Exchange</Label>
								<Input
									id="filter-exchange"
									bind:value={filterExchange}
									placeholder="NFO"
								/>
							</div>
							<div class="space-y-2">
								<Label for="filter-product">Product</Label>
								<Input
									id="filter-product"
									bind:value={filterProduct}
									placeholder="MIS"
								/>
							</div>
						</div>
						
						<div class="space-y-2">
							<Label for="filter-symbols">Trading Symbols (comma-separated, optional)</Label>
							<Input
								id="filter-symbols"
								bind:value={filterSymbols}
								placeholder="e.g., NIFTY24023CE, NIFTY24023PE"
							/>
						</div>
						
						<div class="space-y-2">
							<Label for="filter-tokens">Instrument Tokens (comma-separated, optional)</Label>
							<Input
								id="filter-tokens"
								bind:value={filterTokens}
								placeholder="e.g., 12345678, 87654321"
							/>
						</div>
						
						<!-- Position Preview -->
						<div class="mt-4 p-4 bg-muted rounded-md">
							{#if loadingPositions}
								<div class="flex items-center gap-2">
									<Loader2 class="h-4 w-4 animate-spin" />
									<span class="text-sm">Loading positions...</span>
								</div>
							{:else}
								<div class="space-y-2">
									<p class="text-sm font-medium">
										Matched Positions: <span class="text-primary">{matchedPositions.length}</span>
									</p>
									<p class="text-sm text-muted-foreground">
										Total Lots: <span class="font-mono">{totalLots.toFixed(1)}</span>
									</p>
									{#if matchedPositions.length > 0}
										<div class="mt-2 max-h-32 overflow-y-auto text-xs space-y-1">
											{#each matchedPositions.slice(0, 5) as pos}
												<div class="flex justify-between">
													<span>{pos.tradingsymbol}</span>
													<span class="font-mono">{pos.quantity > 0 ? '+' : ''}{pos.quantity}</span>
												</div>
											{/each}
											{#if matchedPositions.length > 5}
												<p class="text-muted-foreground">...and {matchedPositions.length - 5} more</p>
											{/if}
										</div>
									{/if}
								</div>
							{/if}
						</div>
					</CardContent>
				</Card>
			</div>
			
			<!-- Actions -->
			<div class="flex gap-3 justify-end pt-4">
				<Button variant="outline" onclick={onClose} disabled={loading}>
					Cancel
				</Button>
				<Button onclick={handleSubmit} disabled={loading}>
					{#if loading}
						<Loader2 class="h-4 w-4 mr-2 animate-spin" />
						Creating...
					{:else}
						Create Strategy
					{/if}
				</Button>
			</div>
		</div>
	</SheetContent>
</Sheet>
