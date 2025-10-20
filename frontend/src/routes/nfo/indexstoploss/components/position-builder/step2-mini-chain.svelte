<script lang="ts">
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import * as Card from '$lib/components/ui/card';
	import * as Table from '$lib/components/ui/table';
	import * as Tabs from '$lib/components/ui/tabs';
	import { Loader, RefreshCw, TrendingUp, TrendingDown, Zap, List, LineChart } from '@lucide/svelte';
	import type { MiniChainResponse, SelectedStrike } from '../../types';
	import { calculateStrategyStrikes, findATMStrike } from '../../lib/strike-calculator';
	import type { StrategyTemplate } from '../../lib/strategy-templates';
	import PayoffChart from './payoff-chart.svelte';

	interface Props {
		underlying: string;
		expiry: string;
		targetDelta: number;
		selectedTemplate?: StrategyTemplate | null;
		selectedStrikes: SelectedStrike[];
		chainData: MiniChainResponse | null;
		loading: boolean;
		onUpdateSelectedStrikes: (strikes: SelectedStrike[]) => void;
		onNext: () => void;
		onBack: () => void;
		onReloadChain: () => void;
	}

	let {
		underlying,
		expiry,
		targetDelta,
		selectedTemplate,
		selectedStrikes,
		chainData,
		loading,
		onUpdateSelectedStrikes,
		onNext,
		onBack,
		onReloadChain
	}: Props = $props();
	
	// Mode selection
	let mode = $state<'chain' | 'quick' | 'payoff'>('chain');
	
	// Computed spot price for quick mode defaults
	let spotPrice = $derived(chainData?.spot_price || 0);
	let roundedSpot = $derived(spotPrice ? Math.round(spotPrice / 50) * 50 : 0);
	
	// Default strikes based on underlying when no chain data
	let defaultStrike = $derived(() => {
		if (roundedSpot > 0) return roundedSpot;
		// Fallback defaults if no chain data
		if (underlying === 'NIFTY') return 24000;
		if (underlying === 'BANKNIFTY') return 51000;
		return 0;
	});
	
	// Auto-populate strikes when template is selected and chain data is loaded
	$effect(() => {
		if (selectedTemplate && chainData && chainData.strikes.length > 0 && selectedStrikes.length === 0) {
			console.log('Auto-populating strikes for template:', selectedTemplate.name);
			
			// Find ATM strike
			const atmStrike = chainData.atm_strike || findATMStrike(chainData.strikes, chainData.spot_price);
			
			// Calculate strikes based on template
			const calculatedStrikes = calculateStrategyStrikes(selectedTemplate, atmStrike, underlying);
			
			// Convert calculated strikes to SelectedStrike format
			const autoStrikes: SelectedStrike[] = [];
			
			for (const calc of calculatedStrikes) {
				// Find the strike in chain data
				const chainStrike = chainData.strikes.find(s => s.strike === calc.strike);
				if (!chainStrike) {
					console.warn(`Strike ${calc.strike} not found in chain data`);
					continue;
				}
				
				// Get the correct side (CE or PE)
				const side = calc.optionType === 'CE' ? chainStrike.ce : chainStrike.pe;
				if (!side) {
					console.warn(`No ${calc.optionType} data for strike ${calc.strike}`);
					continue;
				}
				
				// Create selected strike
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
				console.log(`Auto-selected ${autoStrikes.length} strikes for ${selectedTemplate.name}`);
			} else {
				console.warn('Could not auto-populate strikes - please select manually');
			}
		}
	});

	function isStrikeSelected(instrumentToken: number): boolean {
		return selectedStrikes.some(s => s.instrument_token === instrumentToken);
	}

	function toggleStrike(
		strike: number,
		optionType: 'CE' | 'PE',
		side: any
	) {
		if (!side) return;

		const existing = selectedStrikes.find(s => s.instrument_token === side.instrument_token);

		if (existing) {
			// Remove if already selected
			onUpdateSelectedStrikes(
				selectedStrikes.filter(s => s.instrument_token !== side.instrument_token)
			);
		} else {
			// Add new selection
			const newStrike: SelectedStrike = {
				instrument_token: side.instrument_token,
				tradingsymbol: side.tradingsymbol,
				strike: strike,
				option_type: optionType,
				ltp: side.ltp,
				lot_size: side.lot_size,
				delta: side.greeks.delta,
				gamma: side.greeks?.gamma || 0,
				theta: side.greeks?.theta || 0,
				vega: side.greeks?.vega || 0,
				oi: side.oi,
				lots: 1, // Default to 1 lot
				transaction_type: 'SELL' // Default to SELL (premium collection)
			};
			onUpdateSelectedStrikes([...selectedStrikes, newStrike]);
		}
	}

	function updateStrikeLots(instrumentToken: number, lots: number) {
		onUpdateSelectedStrikes(
			selectedStrikes.map(s =>
				s.instrument_token === instrumentToken ? { ...s, lots } : s
			)
		);
	}

	function updateStrikeTransactionType(instrumentToken: number, transactionType: 'BUY' | 'SELL') {
		onUpdateSelectedStrikes(
			selectedStrikes.map(s =>
				s.instrument_token === instrumentToken ? { ...s, transaction_type: transactionType } : s
			)
		);
	}
	
	// Quick mode: Add strike manually
	function addQuickStrike(type: 'CE' | 'PE') {
		const strikeInput = document.getElementById(`${type.toLowerCase()}-strike-input`) as HTMLInputElement;
		const transactionTypeSelect = document.getElementById(`${type.toLowerCase()}-transaction-type`) as HTMLSelectElement;
		const lotsInput = document.getElementById(`${type.toLowerCase()}-lots-input`) as HTMLInputElement;
		
		const strike = parseFloat(strikeInput.value);
		const transactionType = transactionTypeSelect.value as 'BUY' | 'SELL';
		const lots = parseInt(lotsInput.value) || 1;
		
		if (!strike || strike <= 0) {
			console.warn('Please enter a valid strike price');
			return;
		}
		
		// Generate approximate tradingsymbol
		const expirySuffix = expiry.replace(/-/g, '').substring(2); // YYMMDD format
		const tradingsymbol = `${underlying}${expirySuffix}${strike}${type}`;
		
		// Create quick strike entry with approximate values
		const quickStrike: SelectedStrike = {
			instrument_token: Math.floor(Math.random() * 1000000), // Temporary token
			tradingsymbol,
			strike,
			option_type: type,
			ltp: 0, // Will be filled during execution
			lot_size: underlying === 'NIFTY' ? 50 : 15, // Approximate
			lots,
			transaction_type: transactionType,
			delta: type === 'CE' ? 0.50 : -0.50, // Approximate
		};
		
		// Check if already added
		if (selectedStrikes.some(s => s.strike === strike && s.option_type === type)) {
			console.warn(`${type} ${strike} already added`);
			return;
		}
		
		onUpdateSelectedStrikes([...selectedStrikes, quickStrike]);
		console.log(`Added ${type} ${strike} (${transactionType})`);
		
		// Clear inputs
		strikeInput.value = '';
		lotsInput.value = '1';
	}

	const hasSelections = $derived(selectedStrikes.length > 0);
</script>

<div class="space-y-4">
	<Card.Root>
		<Card.Header>
			<div class="flex items-center justify-between">
				<div>
					<Card.Title>Select Strikes</Card.Title>
					<Card.Description>
						{underlying} • Expiry: {new Date(expiry + 'T00:00:00').toLocaleDateString('en-GB')}
					</Card.Description>
				</div>
				<Button
					variant="ghost"
					size="sm"
					onclick={onReloadChain}
					disabled={loading}
				>
					<RefreshCw class="h-4 w-4 {loading ? 'animate-spin' : ''}" />
				</Button>
			</div>
		</Card.Header>
		<Card.Content>
			<Tabs.Root bind:value={mode} class="w-full">
				<Tabs.List class="grid w-full grid-cols-3">
					<Tabs.Trigger value="chain" class="flex items-center gap-2" disabled={loading}>
						<List class="h-4 w-4" />
						Chain Mode
					</Tabs.Trigger>
					<Tabs.Trigger value="quick" class="flex items-center gap-2">
						<Zap class="h-4 w-4" />
						Quick Mode
					</Tabs.Trigger>
					<Tabs.Trigger value="payoff" class="flex items-center gap-2" disabled={selectedStrikes.length === 0}>
						<LineChart class="h-4 w-4" />
						Payoff Chart
					</Tabs.Trigger>
				</Tabs.List>
				
				<!-- CHAIN MODE -->
				<Tabs.Content value="chain" class="space-y-4 mt-4">
					{#if loading}
						<div class="flex flex-col items-center justify-center py-12 space-y-2">
							<Loader class="h-8 w-8 animate-spin text-muted-foreground" />
							<p class="text-sm text-muted-foreground">Loading option chain...</p>
							<Button variant="outline" size="sm" onclick={() => { loading = false; mode = 'quick'; }}>
								Skip to Quick Mode
							</Button>
						</div>
					{:else if chainData && chainData.strikes && chainData.strikes.length > 0}
						<div class="space-y-2">
							<div class="flex items-center gap-4 text-sm text-muted-foreground">
								<span>Spot: <span class="font-mono font-semibold text-foreground">₹{chainData.spot_price.toFixed(2)}</span></span>
								<span>ATM: <span class="font-mono font-semibold text-foreground">{chainData.atm_strike}</span></span>
								<span class="text-xs">({chainData.strikes.length} strikes)</span>
							</div>

							<!-- Table -->
							<div class="rounded-md border overflow-x-auto">
								<Table.Root>
									<Table.Header>
										<Table.Row>
											<Table.Head colspan={4} class="text-center bg-green-500/10">CE (Call)</Table.Head>
											<Table.Head class="text-center font-bold">Strike</Table.Head>
											<Table.Head colspan={4} class="text-center bg-red-500/10">PE (Put)</Table.Head>
										</Table.Row>
										<Table.Row class="text-xs">
											<Table.Head class="text-right">Delta</Table.Head>
											<Table.Head class="text-right">LTP</Table.Head>
											<Table.Head class="text-right">IV</Table.Head>
											<Table.Head class="text-center">Select</Table.Head>
											<Table.Head class="text-center">Strike</Table.Head>
											<Table.Head class="text-center">Select</Table.Head>
											<Table.Head class="text-right">IV</Table.Head>
											<Table.Head class="text-right">LTP</Table.Head>
											<Table.Head class="text-right">Delta</Table.Head>
										</Table.Row>
									</Table.Header>
									<Table.Body>
										{#each chainData.strikes as strikeData}
											<Table.Row class={strikeData.is_atm ? 'bg-primary/5' : ''}>
												<!-- CE Side -->
												{#if strikeData.ce}
													<Table.Cell class="text-right font-mono text-xs">
														{strikeData.ce.greeks.delta.toFixed(3)}
													</Table.Cell>
													<Table.Cell class="text-right font-mono">
														₹{strikeData.ce.ltp.toFixed(2)}
													</Table.Cell>
													<Table.Cell class="text-right font-mono text-xs">
														{(strikeData.ce.greeks.iv * 100).toFixed(1)}%
													</Table.Cell>
													<Table.Cell class="text-center">
														<Button
															variant={isStrikeSelected(strikeData.ce.instrument_token) ? 'default' : 'ghost'}
															size="sm"
															onclick={() => toggleStrike(strikeData.strike, 'CE', strikeData.ce)}
														>
															{isStrikeSelected(strikeData.ce.instrument_token) ? '✓' : '+'}
														</Button>
													</Table.Cell>
												{:else}
													<Table.Cell colspan={4}></Table.Cell>
												{/if}

												<!-- Strike -->
												<Table.Cell class="text-center font-bold font-mono">
													{strikeData.strike}
													{#if strikeData.is_atm}
														<Badge variant="outline" class="ml-2 text-xs">ATM</Badge>
													{/if}
												</Table.Cell>

												<!-- PE Side -->
												{#if strikeData.pe}
													<Table.Cell class="text-center">
														<Button
															variant={isStrikeSelected(strikeData.pe.instrument_token) ? 'default' : 'ghost'}
															size="sm"
															onclick={() => toggleStrike(strikeData.strike, 'PE', strikeData.pe)}
														>
															{isStrikeSelected(strikeData.pe.instrument_token) ? '✓' : '+'}
														</Button>
													</Table.Cell>
													<Table.Cell class="text-right font-mono text-xs">
														{(strikeData.pe.greeks.iv * 100).toFixed(1)}%
													</Table.Cell>
													<Table.Cell class="text-right font-mono">
														₹{strikeData.pe.ltp.toFixed(2)}
													</Table.Cell>
													<Table.Cell class="text-right font-mono text-xs">
														{strikeData.pe.greeks.delta.toFixed(3)}
													</Table.Cell>
												{:else}
													<Table.Cell colspan={4}></Table.Cell>
												{/if}
											</Table.Row>
										{/each}
									</Table.Body>
								</Table.Root>
							</div>
						</div>
					{:else}
						<div class="text-center py-8 space-y-3">
							<p class="text-muted-foreground">No chain data available</p>
							<p class="text-sm text-muted-foreground">
								Make sure an options session is active for {underlying}
							</p>
							<Button variant="outline" size="sm" onclick={onReloadChain}>
								<RefreshCw class="h-4 w-4 mr-2" />
								Retry Loading Chain
							</Button>
						</div>
					{/if}
				</Tabs.Content>
				
				<!-- QUICK MODE -->
				<Tabs.Content value="quick" class="space-y-4 mt-4">
					<div class="space-y-4">
						<div class="space-y-2">
							<p class="text-sm text-muted-foreground">
								Manually enter strike prices without loading the full chain. Perfect for quick position entry.
							</p>
							{#if spotPrice > 0}
								<div class="text-sm flex items-center gap-2">
									<span class="text-muted-foreground">Live Spot:</span>
									<span class="font-mono font-semibold">₹{spotPrice.toFixed(2)}</span>
									<span class="text-muted-foreground">→</span>
									<span class="font-mono font-semibold">₹{roundedSpot}</span>
								</div>
							{:else}
								<div class="text-xs text-yellow-600 bg-yellow-500/10 p-2 rounded">
									Using default strikes. Start an options session for live spot prices.
								</div>
							{/if}
						</div>
						
						<div class="grid grid-cols-2 gap-4">
							<!-- Add CE Strike -->
							<div class="space-y-3 p-4 rounded-lg border">
								<div class="flex items-center gap-2">
									<div class="h-8 w-8 rounded-full bg-green-500/10 flex items-center justify-center">
										<TrendingUp class="h-4 w-4 text-green-600" />
									</div>
									<h4 class="font-semibold">Add Call (CE)</h4>
								</div>
								<div class="space-y-2">
									<Label>Strike Price</Label>
									<Input
										type="number"
										placeholder={defaultStrike() > 0 ? `${defaultStrike()}` : "e.g., 24000"}
										value={defaultStrike() || ''}
										id="ce-strike-input"
										step="50"
									/>
								</div>
								<div class="space-y-2">
									<Label>Transaction Type</Label>
									<select
										id="ce-transaction-type"
										class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
									>
										<option value="SELL">SELL (Short)</option>
										<option value="BUY">BUY (Long)</option>
									</select>
								</div>
								<div class="space-y-2">
									<Label>Lots</Label>
									<Input
										type="number"
										min="1"
										max="100"
										value="1"
										id="ce-lots-input"
									/>
								</div>
								<Button class="w-full" variant="outline" onclick={() => addQuickStrike('CE')}>
									Add CE Strike
								</Button>
							</div>
							
							<!-- Add PE Strike -->
							<div class="space-y-3 p-4 rounded-lg border">
								<div class="flex items-center gap-2">
									<div class="h-8 w-8 rounded-full bg-red-500/10 flex items-center justify-center">
										<TrendingDown class="h-4 w-4 text-red-600" />
									</div>
									<h4 class="font-semibold">Add Put (PE)</h4>
								</div>
								<div class="space-y-2">
									<Label>Strike Price</Label>
									<Input
										type="number"
										placeholder={defaultStrike() > 0 ? `${defaultStrike()}` : "e.g., 24000"}
										value={defaultStrike() || ''}
										id="pe-strike-input"
										step="50"
									/>
								</div>
								<div class="space-y-2">
									<Label>Transaction Type</Label>
									<select
										id="pe-transaction-type"
										class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
									>
										<option value="SELL">SELL (Short)</option>
										<option value="BUY">BUY (Long)</option>
									</select>
								</div>
								<div class="space-y-2">
									<Label>Lots</Label>
									<Input
										type="number"
										min="1"
										max="100"
										value="1"
										id="pe-lots-input"
									/>
								</div>
								<Button class="w-full" variant="outline" onclick={() => addQuickStrike('PE')}>
									Add PE Strike
								</Button>
							</div>
						</div>
						
						<div class="p-3 rounded-md bg-blue-500/10 border border-blue-500/20 text-sm text-blue-600">
							<p class="font-semibold">Quick Mode Features:</p>
							<ul class="text-xs mt-1 space-y-1 list-disc list-inside">
								<li>Strikes default to ATM (rounded to nearest 50)</li>
								<li>Use arrow keys or type to adjust strikes in 50-point increments</li>
								<li>Exact Greeks and LTP will be fetched during execution</li>
							</ul>
						</div>
					</div>
				</Tabs.Content>
				
				<!-- PAYOFF CHART MODE -->
				<Tabs.Content value="payoff" class="mt-4">
					<PayoffChart
						strikes={selectedStrikes}
						spotPrice={spotPrice}
						underlying={underlying}
					/>
				</Tabs.Content>
			</Tabs.Root>
		</Card.Content>
	</Card.Root>

	<!-- Selected Strikes Summary -->
	{#if selectedStrikes.length > 0}
		<Card.Root>
			<Card.Header>
				<Card.Title>Selected Strikes ({selectedStrikes.length})</Card.Title>
			</Card.Header>
			<Card.Content>
				<div class="space-y-3">
					{#each selectedStrikes as strike}
						<div class="flex items-center gap-4 p-3 rounded-md border">
							<div class="flex-1">
								<p class="font-medium text-sm">{strike.tradingsymbol}</p>
								<p class="text-xs text-muted-foreground">
									Strike: {strike.strike} | LTP: ₹{strike.ltp} | Delta: {strike.delta.toFixed(3)}
								</p>
							</div>
							<div class="flex items-center gap-2">
								<select
									value={strike.transaction_type}
									onchange={(e) => updateStrikeTransactionType(strike.instrument_token, e.currentTarget.value as 'BUY' | 'SELL')}
									class="h-8 rounded-md border border-input bg-background px-2 text-sm"
								>
									<option value="SELL">SELL</option>
									<option value="BUY">BUY</option>
								</select>
								<input
									type="number"
									min="1"
									max="100"
									value={strike.lots}
									oninput={(e) => updateStrikeLots(strike.instrument_token, parseInt(e.currentTarget.value) || 1)}
									class="w-16 h-8 rounded-md border border-input bg-background px-2 text-sm text-center font-mono"
								/>
								<span class="text-xs text-muted-foreground">lots</span>
							</div>
						</div>
					{/each}
				</div>
			</Card.Content>
		</Card.Root>
	{/if}

	<!-- Navigation -->
	<div class="flex justify-between">
		<Button variant="outline" onclick={onBack}>
			Back
		</Button>
		<Button onclick={onNext} disabled={!hasSelections}>
			Next: Configure Protection ({selectedStrikes.length} selected)
		</Button>
	</div>
</div>
