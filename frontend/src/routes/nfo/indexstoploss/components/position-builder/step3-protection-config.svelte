<script lang="ts">
	import { Label } from '$lib/components/ui/label';
	import { Input } from '$lib/components/ui/input';
	import { Button } from '$lib/components/ui/button';
	import { Checkbox } from '$lib/components/ui/checkbox';
	import * as Card from '$lib/components/ui/card';
	import * as Tabs from '$lib/components/ui/tabs';
	import { Info } from '@lucide/svelte';

	interface ProtectionConfig {
		enabled: boolean;
		monitoring_mode: 'index' | 'premium' | 'hybrid';
		// Index mode fields
		index_tradingsymbol?: string;
		index_upper_stoploss?: number;
		index_lower_stoploss?: number;
		// Trailing config
		trailing_enabled: boolean;
		trailing_distance?: number;
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
			<Tabs.Root value={protectionConfig.monitoring_mode} onValueChange={(mode) => onUpdateProtection({ monitoring_mode: mode as any })}>
				<Tabs.List class="grid w-full grid-cols-3">
					<Tabs.Trigger value="index">INDEX</Tabs.Trigger>
					<Tabs.Trigger value="premium">PREMIUM</Tabs.Trigger>
					<Tabs.Trigger value="hybrid">HYBRID</Tabs.Trigger>
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

				<!-- PREMIUM Mode -->
				<Tabs.Content value="premium" class="space-y-4">
					<div class="flex items-start gap-2 p-3 rounded-md bg-purple-500/10 text-sm">
						<Info class="h-4 w-4 mt-0.5 text-purple-500" />
						<p>
							Monitor individual option premiums. Set stop-loss and profit targets per position.
						</p>
					</div>
					<p class="text-sm text-muted-foreground">
						Premium mode configuration will be available in the advanced settings after position creation.
					</p>
				</Tabs.Content>

				<!-- HYBRID Mode -->
				<Tabs.Content value="hybrid" class="space-y-4">
					<div class="flex items-start gap-2 p-3 rounded-md bg-orange-500/10 text-sm">
						<Info class="h-4 w-4 mt-0.5 text-orange-500" />
						<p>
							Combine index-based protection with premium monitoring for maximum control.
						</p>
					</div>
					<p class="text-sm text-muted-foreground">
						Hybrid mode configuration will be available in the advanced settings after position creation.
					</p>
				</Tabs.Content>
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
