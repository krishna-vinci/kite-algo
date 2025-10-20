<script lang="ts">
	import { Label } from '$lib/components/ui/label';
	import { Input } from '$lib/components/ui/input';
	import { Button } from '$lib/components/ui/button';
	import { Checkbox } from '$lib/components/ui/checkbox';
	import * as Card from '$lib/components/ui/card';
	import * as Tabs from '$lib/components/ui/tabs';
	import { Info } from '@lucide/svelte';

	interface PremiumThreshold {
		instrument_token: number;
		tradingsymbol: string;
		entry_price: number; // Estimated from LTP, updated after execution
		stop_loss_percent: number; // Default 10%
		target_percent: number; // Default 10%
	}

	interface ProtectionConfig {
		enabled: boolean;
		monitoring_mode: 'index' | 'premium' | 'hybrid' | 'combined_premium';
		// Index mode fields
		index_tradingsymbol?: string;
		index_upper_stoploss?: number;
		index_lower_stoploss?: number;
		// Trailing config
		trailing_enabled: boolean;
		trailing_distance?: number;
		// Combined premium fields
		combined_premium_entry_type?: 'credit' | 'debit';
		combined_premium_profit_target?: number;
		// Premium mode fields (NEW!)
		premium_thresholds?: PremiumThreshold[];
	}

	interface SelectedStrike {
		instrument_token: number;
		tradingsymbol: string;
		strike: number;
		option_type: 'CE' | 'PE';
		ltp: number;
		lot_size: number;
		delta: number;
		lots: number;
		transaction_type: 'BUY' | 'SELL';
	}

	interface Props {
		underlying: string;
		selectedStrikes: SelectedStrike[];
		protectionConfig: ProtectionConfig;
		onUpdateProtection: (config: Partial<ProtectionConfig>) => void;
		onNext: () => void;
		onBack: () => void;
	}

	let { underlying, selectedStrikes, protectionConfig, onUpdateProtection, onNext, onBack }: Props = $props();
	
	// Show simple modes by default, advanced on request
	let showAdvancedModes = $state(false);
	
	// Initialize premium thresholds from selected strikes
	function initializePremiumThresholds() {
		const thresholds: PremiumThreshold[] = selectedStrikes.map(strike => ({
			instrument_token: strike.instrument_token,
			tradingsymbol: strike.tradingsymbol,
			entry_price: strike.ltp, // Use current LTP as entry estimate
			stop_loss_percent: 10, // Default 10% stop-loss
			target_percent: 10 // Default 10% profit target
		}));
		onUpdateProtection({ premium_thresholds: thresholds });
	}
	
	// Auto-initialize when premium mode is selected
	$effect(() => {
		if (protectionConfig.monitoring_mode === 'premium' || protectionConfig.monitoring_mode === 'hybrid') {
			if (!protectionConfig.premium_thresholds || protectionConfig.premium_thresholds.length === 0) {
				initializePremiumThresholds();
			}
		}
	});

	// Default index symbols
	const indexSymbols = {
		NIFTY: 'NIFTY 50',
		BANKNIFTY: 'NIFTY BANK'
	};

	// Determine which stoplosses are required based on position structure
	// For index mode only - premium/hybrid modes don't use index SL
	const requiresUpperSL = $derived(() => {
		if (protectionConfig.monitoring_mode !== 'index') return false;
		
		// For multi-leg positions, always need both
		if (selectedStrikes.length > 1) return true;
		
		// For single leg: CE SELL or PE BUY needs upper SL
		const strike = selectedStrikes[0];
		return (strike.option_type === 'CE' && strike.transaction_type === 'SELL') ||
		       (strike.option_type === 'PE' && strike.transaction_type === 'BUY');
	});

	const requiresLowerSL = $derived(() => {
		if (protectionConfig.monitoring_mode !== 'index') return false;
		
		// For multi-leg positions, always need both
		if (selectedStrikes.length > 1) return true;
		
		// For single leg: CE BUY or PE SELL needs lower SL
		const strike = selectedStrikes[0];
		return (strike.option_type === 'CE' && strike.transaction_type === 'BUY') ||
		       (strike.option_type === 'PE' && strike.transaction_type === 'SELL');
	});

	const isValid = $derived(() => {
		if (!protectionConfig.enabled) return true;
		if (protectionConfig.monitoring_mode !== 'index') return true;
		
		// Check required stoplosses are filled
		const upperValid = !requiresUpperSL() || !!protectionConfig.index_upper_stoploss;
		const lowerValid = !requiresLowerSL() || !!protectionConfig.index_lower_stoploss;
		
		return upperValid && lowerValid;
	});
</script>

<Card.Root>
	<Card.Header>
		<Card.Title>Configure Protection Strategy</Card.Title>
		<Card.Description>
			Optional: Add automated stop-loss protection for your position
		</Card.Description>
	</Card.Header>
	<Card.Content class="space-y-6">
		<!-- Enable Protection Toggle -->
		<div class="flex items-center gap-3 p-4 rounded-md border">
			<Checkbox
				id="enable-protection"
				checked={protectionConfig.enabled}
				onCheckedChange={(checked) => onUpdateProtection({ enabled: !!checked })}
			/>
			<div class="flex-1">
				<Label for="enable-protection" class="font-medium">
					Enable Stop-Loss Protection
				</Label>
				<p class="text-xs text-muted-foreground mt-1">
					Automatically place exit orders when index price hits stop-loss levels
				</p>
			</div>
		</div>

		{#if protectionConfig.enabled}
			<!-- Simple/Advanced Toggle -->
			<div class="flex items-center justify-between p-3 bg-muted/50 rounded-md">
				<div class="space-y-1">
					<p class="text-sm font-medium">Monitoring Modes</p>
					<p class="text-xs text-muted-foreground">
						{showAdvancedModes ? 'All 4 modes available' : 'Showing recommended modes'}
					</p>
				</div>
				<Button 
					variant="outline" 
					size="sm"
					onclick={() => showAdvancedModes = !showAdvancedModes}
				>
					{showAdvancedModes ? 'Hide' : 'Show'} Advanced Modes
				</Button>
			</div>
			
			<Tabs.Root value={protectionConfig.monitoring_mode} onValueChange={(mode) => onUpdateProtection({ monitoring_mode: mode as any })}>
				<Tabs.List class={showAdvancedModes ? "grid w-full grid-cols-4" : "grid w-full grid-cols-2"}>
					<Tabs.Trigger value="index">INDEX</Tabs.Trigger>
					<Tabs.Trigger value="combined_premium">COMBINED</Tabs.Trigger>
					{#if showAdvancedModes}
						<Tabs.Trigger value="premium">PREMIUM</Tabs.Trigger>
						<Tabs.Trigger value="hybrid">HYBRID</Tabs.Trigger>
					{/if}
				</Tabs.List>

				<!-- INDEX Mode -->
				<Tabs.Content value="index" class="space-y-4">
					<div class="flex items-start gap-2 p-3 rounded-md bg-blue-500/10 text-sm">
						<Info class="h-4 w-4 mt-0.5 text-blue-500" />
						<div class="space-y-1">
							<p>
								Monitor {underlying} spot price. Exit all positions when index crosses stop-loss levels.
							</p>
							{#if selectedStrikes.length === 1}
								{@const strike = selectedStrikes[0]}
								<p class="text-xs font-medium mt-1">
									For {strike.option_type} {strike.transaction_type}:
									{#if requiresUpperSL() && !requiresLowerSL()}
										Only <span class="font-semibold">Upper SL</span> is required (protects against index rising)
									{:else if requiresLowerSL() && !requiresUpperSL()}
										Only <span class="font-semibold">Lower SL</span> is required (protects against index falling)
									{:else}
										Both stop-losses required
									{/if}
								</p>
							{/if}
						</div>
					</div>

					<div class="space-y-2">
						<Label>Index Symbol</Label>
						<Input
							value={indexSymbols[underlying as keyof typeof indexSymbols] || underlying}
							disabled
						/>
					</div>

					<div class="grid grid-cols-2 gap-4">
						<div class="space-y-2">
							<Label for="upper-sl" class={requiresUpperSL() ? '' : 'opacity-50'}>
								Upper Stop-Loss {requiresUpperSL() ? '' : '(Optional)'}
							</Label>
							<Input
								id="upper-sl"
								type="number"
								step="50"
								placeholder="e.g., 24500"
								value={protectionConfig.index_upper_stoploss || ''}
								disabled={!requiresUpperSL()}
								oninput={(e) => onUpdateProtection({ 
									index_upper_stoploss: parseFloat(e.currentTarget.value) || undefined 
								})}
							/>
							<p class="text-xs text-muted-foreground">
								{#if requiresUpperSL()}
									Exit when index rises above this
								{:else}
									Not required for this position
								{/if}
							</p>
						</div>

						<div class="space-y-2">
							<Label for="lower-sl" class={requiresLowerSL() ? '' : 'opacity-50'}>
								Lower Stop-Loss {requiresLowerSL() ? '' : '(Optional)'}
							</Label>
							<Input
								id="lower-sl"
								type="number"
								step="50"
								placeholder="e.g., 23500"
								value={protectionConfig.index_lower_stoploss || ''}
								disabled={!requiresLowerSL()}
								oninput={(e) => onUpdateProtection({ 
									index_lower_stoploss: parseFloat(e.currentTarget.value) || undefined 
								})}
							/>
							<p class="text-xs text-muted-foreground">
								{#if requiresLowerSL()}
									Exit when index falls below this
								{:else}
									Not required for this position
								{/if}
							</p>
						</div>
					</div>

					<!-- Trailing -->
					<div class="space-y-3">
						<div class="flex items-center gap-3">
							<Checkbox
								id="trailing"
								checked={protectionConfig.trailing_enabled}
								onCheckedChange={(checked) => onUpdateProtection({ trailing_enabled: !!checked })}
							/>
							<Label for="trailing" class="font-medium">Enable Trailing Stop-Loss</Label>
						</div>

						{#if protectionConfig.trailing_enabled}
							<div class="space-y-2 ml-7">
								<Label for="trailing-distance">Trailing Distance (points)</Label>
								<Input
									id="trailing-distance"
									type="number"
									step="10"
									placeholder="e.g., 100"
									value={protectionConfig.trailing_distance || ''}
									oninput={(e) => onUpdateProtection({ 
										trailing_distance: parseFloat(e.currentTarget.value) || undefined 
									})}
								/>
								<p class="text-xs text-muted-foreground">
									Stop-loss trails behind favorable price movement
								</p>
							</div>
						{/if}
					</div>
				</Tabs.Content>

				<!-- COMBINED PREMIUM Mode (NEW!) -->
				<Tabs.Content value="combined_premium" class="space-y-4">
					<div class="flex items-start gap-2 p-3 rounded-md bg-green-500/10 text-sm">
						<Info class="h-4 w-4 mt-0.5 text-green-500" />
						<div class="space-y-1">
							<p class="font-medium">Recommended for multi-leg strategies</p>
							<p>
								Monitor net premium across all positions. Exit when profit target reached or index stop-loss triggered.
							</p>
						</div>
					</div>

					<div class="space-y-4">
						<div class="space-y-2">
							<Label>Entry Type</Label>
							<select
								class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
								value={protectionConfig.combined_premium_entry_type || 'credit'}
								onchange={(e) => onUpdateProtection({ combined_premium_entry_type: e.currentTarget.value as any })}
							>
								<option value="credit">CREDIT (Sell strategies - collect premium)</option>
								<option value="debit">DEBIT (Buy strategies - pay premium)</option>
							</select>
							<p class="text-xs text-muted-foreground">
								Select based on whether you collected or paid net premium
							</p>
						</div>

						<div class="space-y-2">
							<Label for="profit-target">Net Profit Target (points)</Label>
							<Input
								id="profit-target"
								type="number"
								step="10"
								placeholder="e.g., 50"
								value={protectionConfig.combined_premium_profit_target || ''}
								oninput={(e) => onUpdateProtection({ 
									combined_premium_profit_target: parseFloat(e.currentTarget.value) || undefined 
								})}
							/>
							<p class="text-xs text-muted-foreground">
								Exit all positions when net profit reaches this many points
							</p>
						</div>

						<div class="p-3 bg-blue-50 dark:bg-blue-950 rounded-md text-xs">
							<p class="font-medium mb-1">💡 Note:</p>
							<p>Index stop-loss levels (configured above) will still apply for risk management.</p>
						</div>
					</div>
				</Tabs.Content>

				<!-- PREMIUM Mode (Advanced) - NOW FUNCTIONAL! -->
				{#if showAdvancedModes}
					<Tabs.Content value="premium" class="space-y-4">
						<div class="flex items-start gap-2 p-3 rounded-md bg-purple-500/10 text-sm">
							<Info class="h-4 w-4 mt-0.5 text-purple-500" />
							<div class="space-y-1">
								<p class="font-medium">Per-Position Premium Monitoring</p>
								<p>
									Set individual stop-loss and profit targets for each strike. Entry prices estimated from current LTP.
								</p>
							</div>
						</div>

						<!-- Per-Strike Configuration -->
						<div class="space-y-4">
							{#if protectionConfig.premium_thresholds && protectionConfig.premium_thresholds.length > 0}
								{#each protectionConfig.premium_thresholds as threshold, idx}
									{@const strike = selectedStrikes[idx]}
									<Card.Root class="border-2">
										<Card.Header class="pb-3">
											<Card.Title class="text-base flex items-center justify-between">
												<span>{threshold.tradingsymbol}</span>
												<span class="text-xs font-mono text-muted-foreground">
													{strike.transaction_type} {strike.lots}x
												</span>
											</Card.Title>
										</Card.Header>
										<Card.Content class="space-y-3">
											<div class="grid grid-cols-3 gap-3">
												<div class="space-y-1.5">
													<Label class="text-xs">Entry Price (Est.)</Label>
													<Input
														type="number"
														step="0.5"
														value={threshold.entry_price}
														oninput={(e) => {
															const updated = [...(protectionConfig.premium_thresholds || [])];
															updated[idx].entry_price = parseFloat(e.currentTarget.value) || strike.ltp;
															onUpdateProtection({ premium_thresholds: updated });
														}}
													/>
													<p class="text-xs text-muted-foreground">LTP: ₹{strike.ltp}</p>
												</div>
												<div class="space-y-1.5">
													<Label class="text-xs">Stop-Loss %</Label>
													<Input
														type="number"
														step="1"
														value={threshold.stop_loss_percent}
														oninput={(e) => {
															const updated = [...(protectionConfig.premium_thresholds || [])];
															updated[idx].stop_loss_percent = parseFloat(e.currentTarget.value) || 10;
															onUpdateProtection({ premium_thresholds: updated });
														}}
													/>
													<p class="text-xs text-muted-foreground">
														₹{(threshold.entry_price * (1 + threshold.stop_loss_percent / 100)).toFixed(2)}
													</p>
												</div>
												<div class="space-y-1.5">
													<Label class="text-xs">Target %</Label>
													<Input
														type="number"
														step="1"
														value={threshold.target_percent}
														oninput={(e) => {
															const updated = [...(protectionConfig.premium_thresholds || [])];
															updated[idx].target_percent = parseFloat(e.currentTarget.value) || 10;
															onUpdateProtection({ premium_thresholds: updated });
														}}
													/>
													<p class="text-xs text-muted-foreground">
														₹{(threshold.entry_price * (1 - threshold.target_percent / 100)).toFixed(2)}
													</p>
												</div>
											</div>
											
											<div class="p-2 bg-muted rounded-md text-xs">
												<p>
													<strong>Exit Rules:</strong> {strike.transaction_type === 'SELL' ? 
														`Buy back if premium ≥ ₹${(threshold.entry_price * (1 + threshold.stop_loss_percent / 100)).toFixed(2)} (loss) or ≤ ₹${(threshold.entry_price * (1 - threshold.target_percent / 100)).toFixed(2)} (profit)` :
														`Sell if premium ≤ ₹${(threshold.entry_price * (1 - threshold.stop_loss_percent / 100)).toFixed(2)} (loss) or ≥ ₹${(threshold.entry_price * (1 + threshold.target_percent / 100)).toFixed(2)} (profit)`
													}
												</p>
											</div>
										</Card.Content>
									</Card.Root>
								{/each}
							{:else}
								<p class="text-sm text-muted-foreground">Loading thresholds...</p>
							{/if}
						</div>

						<div class="p-3 bg-blue-50 dark:bg-blue-950 rounded-md text-xs space-y-1">
							<p class="font-medium">💡 Note:</p>
							<ul class="list-disc list-inside space-y-0.5">
								<li>Entry prices use current LTP as estimate</li>
								<li>Actual entry prices will be updated after order execution</li>
								<li>Stop-loss triggers when premium moves AGAINST you</li>
								<li>Target triggers when premium moves IN YOUR FAVOR</li>
							</ul>
						</div>
					</Tabs.Content>

					<!-- HYBRID Mode (Advanced) -->
					<Tabs.Content value="hybrid" class="space-y-4">
						<div class="flex items-start gap-2 p-3 rounded-md bg-orange-500/10 text-sm">
							<Info class="h-4 w-4 mt-0.5 text-orange-500" />
							<div class="space-y-1">
								<p class="font-medium">Hybrid Protection (Index + Premium)</p>
								<p>
									Combines INDEX stop-loss levels with per-position premium monitoring.
								</p>
							</div>
						</div>

						<div class="p-4 bg-muted rounded-md space-y-3">
							<div>
								<p class="text-sm font-medium mb-2">Index Protection:</p>
								<p class="text-xs text-muted-foreground">
									Configure index stop-loss levels in the INDEX tab above. They will apply alongside premium monitoring.
								</p>
							</div>
							<div>
								<p class="text-sm font-medium mb-2">Premium Monitoring:</p>
								<p class="text-xs text-muted-foreground">
									Per-position thresholds will be configured same as PREMIUM mode (see PREMIUM tab).
								</p>
							</div>
						</div>

						<div class="p-3 bg-orange-50 dark:bg-orange-950 rounded-md text-xs">
							<p class="font-medium mb-1">💡 Exit Logic:</p>
							<p>Position exits when <strong>ANY</strong> condition triggers:</p>
							<ul class="list-disc list-inside mt-1 space-y-0.5">
								<li>Index crosses upper/lower stop-loss (exits ALL positions)</li>
								<li>Individual position premium hits its stop-loss/target (exits THAT position only)</li>
							</ul>
						</div>
					</Tabs.Content>
				{/if}
			</Tabs.Root>
		{:else}
			<div class="text-center py-8 text-muted-foreground">
				<p>No protection strategy will be created.</p>
				<p class="text-sm">You can add protection manually later from the dashboard.</p>
			</div>
		{/if}
	</Card.Content>
	<Card.Footer class="flex justify-between">
		<Button variant="outline" onclick={onBack}>
			Back
		</Button>
		<Button onclick={onNext} disabled={!isValid}>
			Next: Review & Execute
		</Button>
	</Card.Footer>
</Card.Root>
