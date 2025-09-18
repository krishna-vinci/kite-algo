<script lang="ts">
	import { onMount, onDestroy } from 'svelte'; // Import onDestroy
	import SkeletonLoader from '$lib/components/SkeletonLoader.svelte';
	import { apiFetch, getApiBase } from '$lib/api';

	const API_BASE_URL = getApiBase();

	const REFRESH_INTERVAL = 5000; // 5 seconds

	interface MarginSegment {
		net: number;
		opening_balance: number;
		m2m_unrealised: number;
		m2m_realised: number;
	}

	interface MarginData {
		equity: MarginSegment;
	}

	interface Holding {
		tradingsymbol: string;
		isin: string;
		quantity: number;
		authorised_quantity: number;
		product: string;
		average_price: number;
		last_price: number;
		close_price: number;
		pnl: number;
		collateral_quantity: number;
		t1_quantity: number;
		realised_quantity: number;
		unrealised_quantity: number;
		exchange: string;
		instrument_token: number;
		// Frontend calculated properties
		dayChange: number;
		dayChangePercentage: number;
		lastPriceUpdated?: number; // Timestamp for visual update feedback
	}

	interface HistoricalDataProgress {
		status: 'idle' | 'in_progress' | 'completed' | 'failed';
		total_instruments: number;
		processed_instruments: number;
		current_instrument_symbol: string;
		start_time: string | null;
		end_time: string | null;
		error: string | null;
	}

	let margins: MarginData | null = null;
	let holdings: Holding[] = []; // Initialize as an empty array
	let error: string | null = null;
	let showHoldings: boolean = true; // State for collapsible holdings section
	let holdingsLoading: boolean = false;
	let lastUpdated: string | null = null; // New state for last updated timestamp
	let historicalDataProgress: HistoricalDataProgress = {
		status: 'idle',
		total_instruments: 0,
		processed_instruments: 0,
		current_instrument_symbol: '',
		start_time: null,
		end_time: null,
		error: null
	};
	let isUpdatingHistoricalData: boolean = false;

	let marginInterval: ReturnType<typeof setInterval>;
	let historicalDataProgressInterval: ReturnType<typeof setInterval>;
	// Removed holdingsInterval to stop auto-refresh for holdings

	onMount(async () => {
		await fetchMargins();
		await fetchHoldings();
		await fetchHistoricalDataProgress(); // Fetch initial progress state

		marginInterval = setInterval(fetchMargins, REFRESH_INTERVAL);
		historicalDataProgressInterval = setInterval(fetchHistoricalDataProgress, REFRESH_INTERVAL);
		// Removed setInterval for fetchHoldings
	});

	onDestroy(() => {
		clearInterval(marginInterval);
		clearInterval(historicalDataProgressInterval);
		// Removed clearInterval for holdingsInterval
	});

	async function fetchMargins() {
		try {
			const response = await apiFetch(`/broker/margins`);
			if (!response.ok) {
				throw new Error(`HTTP ${response.status}: ${response.statusText}`);
			}
			margins = await response.json();
			lastUpdated = new Date().toLocaleTimeString(); // Update timestamp on successful fetch
		} catch (e) {
			if (e instanceof Error) {
				error = e.message;
			} else {
				error = 'An unknown error occurred while fetching margins.';
			}
			console.error('Failed to fetch margins:', e);
		}
	}

	async function fetchHoldings() {
		holdingsLoading = true;
		try {
			const response = await apiFetch(`/broker/holdings_kite`, {
				cache: 'no-store' // Ensure fresh data is fetched
			});
			if (!response.ok) {
				throw new Error(`HTTP ${response.status}: ${response.statusText}`);
			}
			const newData: any[] = await response.json();
			const now = Date.now();
			let changed = false;

			const newHoldingsMap = new Map<number, any>(newData.map((h) => [h.instrument_token, h]));
			const tempHoldings: Holding[] = [];

			// Iterate over existing holdings to update them or mark for removal
			for (let i = 0; i < holdings.length; i++) {
				const existingHolding = holdings[i];
				const newH = newHoldingsMap.get(existingHolding.instrument_token);

				if (newH) {
					let itemChanged = false;
					// Update properties of the existing object directly
					if (existingHolding.last_price !== newH.last_price) {
						existingHolding.lastPriceUpdated = now; // Mark for visual feedback
						existingHolding.last_price = newH.last_price;
						itemChanged = true;
					}
					if (existingHolding.quantity !== newH.quantity) {
						existingHolding.quantity = newH.quantity;
						itemChanged = true;
					}
					if (existingHolding.authorised_quantity !== newH.authorised_quantity) {
						existingHolding.authorised_quantity = newH.authorised_quantity;
						itemChanged = true;
					}
					if (existingHolding.average_price !== newH.average_price) {
						existingHolding.average_price = newH.average_price;
						itemChanged = true;
					}
					if (existingHolding.close_price !== newH.close_price) {
						existingHolding.close_price = newH.close_price;
						itemChanged = true;
					}
					if (existingHolding.pnl !== newH.pnl) {
						existingHolding.pnl = newH.pnl;
						itemChanged = true;
					}
					if (existingHolding.collateral_quantity !== newH.collateral_quantity) {
						existingHolding.collateral_quantity = newH.collateral_quantity;
						itemChanged = true;
					}
					if (existingHolding.t1_quantity !== newH.t1_quantity) {
						existingHolding.t1_quantity = newH.t1_quantity;
						itemChanged = true;
					}
					if (existingHolding.realised_quantity !== newH.realised_quantity) {
						existingHolding.realised_quantity = newH.realised_quantity;
						itemChanged = true;
					}
					if (existingHolding.unrealised_quantity !== newH.unrealised_quantity) {
						existingHolding.unrealised_quantity = newH.unrealised_quantity;
						itemChanged = true;
					}

					// Always re-calculate dayChange and dayChangePercentage as they depend on last_price and close_price
					const newDayChange = existingHolding.last_price - existingHolding.close_price;
					const newDayChangePercentage =
						existingHolding.close_price > 0
							? (newDayChange / existingHolding.close_price) * 100
							: 0;
					if (existingHolding.dayChange !== newDayChange) {
						existingHolding.dayChange = newDayChange;
						itemChanged = true;
					}
					if (existingHolding.dayChangePercentage !== newDayChangePercentage) {
						existingHolding.dayChangePercentage = newDayChangePercentage;
						itemChanged = true;
					}

					if (itemChanged) {
						// Explicitly reassign the item in the array to trigger Svelte's reactivity for that specific item
						holdings[i] = existingHolding;
						changed = true;
					}
					tempHoldings.push(existingHolding);
					newHoldingsMap.delete(existingHolding.instrument_token); // Mark as processed
				} else {
					// This existing holding is no longer in newData, it will be filtered out
					changed = true;
				}
			}

			// Add any new holdings that were not in the original list
			newHoldingsMap.forEach((newH) => {
				tempHoldings.push({
					tradingsymbol: newH.tradingsymbol,
					isin: newH.isin,
					quantity: newH.quantity,
					authorised_quantity: newH.authorised_quantity,
					product: newH.product,
					average_price: newH.average_price,
					last_price: newH.last_price,
					close_price: newH.close_price,
					pnl: newH.pnl,
					collateral_quantity: newH.collateral_quantity,
					t1_quantity: newH.t1_quantity,
					realised_quantity: newH.realised_quantity,
					unrealised_quantity: newH.unrealised_quantity,
					exchange: newH.exchange,
					instrument_token: newH.instrument_token,
					dayChange: newH.last_price - newH.close_price,
					dayChangePercentage:
						newH.close_price > 0
							? ((newH.last_price - newH.close_price) / newH.close_price) * 100
							: 0,
					lastPriceUpdated: now // Mark new items for visual feedback
				});
				changed = true;
			});

			// If the array length changed (items added/removed) or any item was updated, reassign the array
			if (changed || holdings.length !== tempHoldings.length) {
				holdings = tempHoldings; // Reassign to trigger reactivity for array structure changes
				lastUpdated = new Date().toLocaleTimeString(); // Update timestamp on successful fetch
			}
		} catch (e) {
			if (e instanceof Error) {
				error = e.message;
			} else {
				error = 'An unknown error occurred while fetching holdings.';
			}
			console.error('Failed to fetch holdings:', e);
		} finally {
			holdingsLoading = false;
		}
	}

	async function updateHistoricalData() {
		isUpdatingHistoricalData = true;
		historicalDataProgress = {
			status: 'in_progress',
			total_instruments: 0,
			processed_instruments: 0,
			current_instrument_symbol: '',
			start_time: new Date().toISOString(),
			end_time: null,
			error: null
		};
		try {
			const response = await apiFetch(`/broker/update_historical_data`, {
				method: 'POST'
			});
			if (!response.ok) {
				throw new Error(`HTTP ${response.status}: ${response.statusText}`);
			}
			// The actual update runs in the background, so we just confirm it started.
			const result = await response.json();
			console.log(result.message);
		} catch (e) {
			if (e instanceof Error) {
				error = e.message;
			} else {
				error = 'An unknown error occurred while starting historical data update.';
			}
			console.error('Failed to start historical data update:', e);
			historicalDataProgress.status = 'failed';
			historicalDataProgress.error = error;
			isUpdatingHistoricalData = false;
		}
	}

	async function fetchHistoricalDataProgress() {
		try {
			const response = await apiFetch(`/broker/historical_data_progress`);
			if (!response.ok) {
				throw new Error(`HTTP ${response.status}: ${response.statusText}`);
			}
			const progress = await response.json();
			historicalDataProgress = progress;
			if (progress.status !== 'in_progress') {
				isUpdatingHistoricalData = false;
			} else {
				isUpdatingHistoricalData = true;
			}
		} catch (e) {
			console.error('Failed to fetch historical data progress:', e);
			// Optionally, set an error state for the progress fetching itself
		}
	}

	function formatCurrency(value: number): string {
		if (value === null || value === undefined) return '0.00';
		if (value >= 10000000) {
			return (value / 10000000).toFixed(2) + ' Cr';
		}
		if (value >= 100000) {
			return (value / 100000).toFixed(2) + ' L';
		}
		if (value >= 1000) {
			return (value / 1000).toFixed(2) + ' k';
		}
		return value.toFixed(2);
	}

	function formatPercentage(value: number): string {
		if (value === null || value === undefined) return '0.00%';
		return value.toFixed(2) + '%';
	}

	$: totalInvestment = holdings?.reduce((sum, h) => sum + h.quantity * h.average_price, 0) || 0;
	$: totalCurrentValue = holdings?.reduce((sum, h) => sum + h.quantity * h.last_price, 0) || 0;
	$: totalPnL = holdings?.reduce((sum, h) => sum + h.pnl, 0) || 0;
	$: totalDayPnL = holdings?.reduce((sum, h) => sum + h.dayChange * h.quantity, 0) || 0;
	$: totalDayPnLPercentage = totalCurrentValue > 0 ? (totalDayPnL / totalCurrentValue) * 100 : 0;
	$: totalPnLPercentage = totalInvestment > 0 ? (totalPnL / totalInvestment) * 100 : 0;
	$: progressPercentage =
		historicalDataProgress.total_instruments > 0
			? (historicalDataProgress.processed_instruments / historicalDataProgress.total_instruments) *
				100
			: 0;
</script>

<div class="p-4 sm:p-8">
	<div class="max-w-7xl mx-auto">
		<h1 class="text-2xl text-foreground mb-6">Hi, Kokkonda</h1>

		{#if error}
			<div
				class="bg-destructive/10 border border-destructive/20 text-destructive px-4 py-3 rounded relative mb-6 flex items-center justify-between"
				role="alert"
			>
				<div>
					<strong class="font-bold">Error: </strong>
					<span class="block sm:inline">{error}</span>
				</div>
				<button
					on:click={() => (error = null)}
					class="text-destructive hover:text-destructive/80 focus:outline-none"
				>
					<svg
						class="h-5 w-5"
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
						xmlns="http://www.w3.org/2000/svg"
						><path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M6 18L18 6M6 6l12 12"
						></path></svg
					>
				</button>
			</div>
		{/if}

		{#if !margins}
			<div class="grid grid-cols-1 md:grid-cols-2 gap-8 mb-8 min-h-[150px]">
				<!-- Added min-h to prevent layout shift -->
				<div class="bg-card p-6 border-t-2 border-primary shadow-sm rounded-lg">
					<SkeletonLoader width="60%" height="1.5em" className="mb-4" />
					<SkeletonLoader width="80%" height="2.5em" className="mb-2" />
					<SkeletonLoader width="40%" height="1em" />
				</div>
				<div class="bg-card p-6 border-t-2 border-primary shadow-sm rounded-lg">
					<SkeletonLoader width="60%" height="1.5em" className="mb-4" />
					<SkeletonLoader width="80%" height="2.5em" className="mb-2" />
					<SkeletonLoader width="40%" height="1em" />
				</div>
			</div>
		{:else}
			<div class="grid grid-cols-1 gap-8 mb-8">
				<!-- Equity Section -->
				<div
					class="bg-card p-6 border-t-4 border-primary rounded-lg shadow-md hover:shadow-lg transition-shadow duration-300"
				>
					<h2 class="text-xl font-semibold text-card-foreground mb-4 flex items-center">
						<svg
							xmlns="http://www.w3.org/2000/svg"
							class="h-6 w-6 mr-3 text-primary"
							fill="none"
							viewBox="0 0 24 24"
							stroke="currentColor"
						>
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"
							/>
						</svg>
						Equity
					</h2>
					<div class="flex flex-col sm:flex-row justify-between items-start sm:items-end">
						<div class="mb-4 sm:mb-0">
							<p class="text-4xl font-bold text-card-foreground">{formatCurrency(margins.equity.net)}</p>
							<p class="text-sm text-muted-foreground">Margin Available</p>
						</div>
						<div class="text-left sm:text-right space-y-1">
							<p class="text-md text-muted-foreground">
								Opening Balance: <span class="font-medium text-card-foreground"
									>{formatCurrency(margins.equity.opening_balance)}</span
								>
							</p>
							<p class="text-sm text-muted-foreground">
								Unrealised P&L: <span
									class="{margins.equity.m2m_unrealised >= 0
										? 'text-green-600 dark:text-green-400'
										: 'text-red-600 dark:text-red-400'} font-medium"
									>{formatCurrency(margins.equity.m2m_unrealised)}</span
								>
							</p>
							<p class="text-sm text-muted-foreground">
								Realised P&L: <span
									class="{margins.equity.m2m_realised >= 0
										? 'text-green-600 dark:text-green-400'
										: 'text-red-600 dark:text-red-400'} font-medium"
									>{formatCurrency(margins.equity.m2m_realised)}</span
								>
							</p>
						</div>
					</div>
				</div>
			</div>
		{/if}

		<!-- Historical Data Update Section -->
		<div class="bg-card p-6 shadow-sm mb-8 rounded-lg">
			<div class="flex justify-between items-center mb-4">
				<h2 class="text-lg font-medium text-muted-foreground flex items-center">
					<svg
						xmlns="http://www.w3.org/2000/svg"
						class="h-5 w-5 mr-2"
						fill="none"
						viewBox="0 0 24 24"
						stroke="currentColor"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M4 4v5h.582m15.356 2A8.001 8.001 0 004 12m7-7h-.582m.002 15.356A8.001 8.001 0 0020 12V8l-2.745 3.908C17.547 15.32 17 17.573 17 20H4"
						/>
					</svg>
					Historical Data
				</h2>
				<button
					on:click={updateHistoricalData}
					class="px-3 py-1 text-sm font-medium bg-primary text-primary-foreground rounded-md hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-ring disabled:opacity-50 disabled:cursor-not-allowed"
					disabled={isUpdatingHistoricalData}
				>
					{#if isUpdatingHistoricalData}
						Updating...
					{:else}
						Update Historical Data
					{/if}
				</button>
			</div>

			{#if isUpdatingHistoricalData || historicalDataProgress.status !== 'idle'}
				<div class="mt-4">
					<div class="flex justify-between text-sm text-muted-foreground mb-1">
						<span>
							{#if historicalDataProgress.status === 'in_progress'}
								Processing: {historicalDataProgress.current_instrument_symbol} ({historicalDataProgress.processed_instruments}/{historicalDataProgress.total_instruments})
							{:else if historicalDataProgress.status === 'completed'}
								Update Completed! Processed {historicalDataProgress.processed_instruments} instruments.
							{:else if historicalDataProgress.status === 'failed'}
								Update Failed: {historicalDataProgress.error}
							{/if}
						</span>
						<span>{progressPercentage.toFixed(1)}%</span>
					</div>
					<div class="w-full bg-muted rounded-full h-2.5">
						<div
							class="bg-primary h-2.5 rounded-full transition-all duration-500 ease-out"
							style="width: {progressPercentage}%;"
						></div>
					</div>
					{#if historicalDataProgress.start_time}
						<p class="text-xs text-muted-foreground mt-2">
							Started: {new Date(historicalDataProgress.start_time).toLocaleTimeString()}
						</p>
					{/if}
					{#if historicalDataProgress.end_time && historicalDataProgress.status !== 'in_progress'}
						<p class="text-xs text-muted-foreground">
							Finished: {new Date(historicalDataProgress.end_time).toLocaleTimeString()}
						</p>
					{/if}
				</div>
			{/if}
		</div>

		<!-- Holdings Section -->
		<div class="bg-card p-6 shadow-sm">
			<div class="flex justify-between items-center mb-4">
				<h2 class="text-lg font-medium text-muted-foreground flex items-center">
					<svg
						xmlns="http://www.w3.org/2000/svg"
						class="h-5 w-5 mr-2"
						fill="none"
						viewBox="0 0 24 24"
						stroke="currentColor"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"
						/>
					</svg>
					Holdings ({holdings ? holdings.length : 0})
				</h2>
				<div class="flex items-center space-x-4">
					{#if lastUpdated}
						<span class="text-xs text-muted-foreground">Last updated: {lastUpdated}</span>
					{/if}
					<button
						on:click={fetchHoldings}
						class="text-sm text-primary hover:text-primary/80 flex items-center"
						disabled={holdingsLoading}
					>
						<svg
							xmlns="http://www.w3.org/2000/svg"
							class="h-4 w-4 mr-1"
							fill="none"
							viewBox="0 0 24 24"
							stroke="currentColor"
						>
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M4 4v5h.582m15.356 2A8.001 8.001 0 004 12m7-7h-.582m.002 15.356A8.001 8.001 0 0020 12V8l-2.745 3.908C17.547 15.32 17 17.573 17 20H4"
							/>
						</svg>
						{holdingsLoading ? 'Refreshing...' : 'Reload'}
					</button>
					<button
						on:click={() => (showHoldings = !showHoldings)}
						class="text-muted-foreground hover:text-foreground"
					>
						{#if showHoldings}
							<svg
								xmlns="http://www.w3.org/2000/svg"
								class="h-5 w-5"
								fill="none"
								viewBox="0 0 24 24"
								stroke="currentColor"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									stroke-width="2"
									d="M5 15l7-7 7 7"
								/>
							</svg>
						{:else}
							<svg
								xmlns="http://www.w3.org/2000/svg"
								class="h-5 w-5"
								fill="none"
								viewBox="0 0 24 24"
								stroke="currentColor"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									stroke-width="2"
									d="M19 9l-7 7-7-7"
								/>
							</svg>
						{/if}
					</button>
				</div>
			</div>

			{#if showHoldings}
				{#if holdingsLoading}
					<div class="p-8 min-h-[400px]">
						<!-- Added min-h to prevent layout shift -->
						<div class="mb-4 grid grid-cols-2 md:grid-cols-4 gap-4">
							<SkeletonLoader width="90%" height="2em" />
							<SkeletonLoader width="90%" height="2em" />
							<SkeletonLoader width="90%" height="2em" />
							<SkeletonLoader width="90%" height="2em" />
						</div>
						<div class="overflow-x-auto">
							<table class="min-w-full divide-y divide-gray-200 rounded-lg overflow-hidden">
								<thead class="bg-gray-100">
									<tr>
										<th
											scope="col"
											class="px-4 py-3 text-left text-xs font-semibold text-gray-600 uppercase tracking-wider"
											><SkeletonLoader width="80px" height="1em" /></th
										>
										<th
											scope="col"
											class="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase tracking-wider"
											><SkeletonLoader width="40px" height="1em" /></th
										>
										<th
											scope="col"
											class="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase tracking-wider"
											><SkeletonLoader width="60px" height="1em" /></th
										>
										<th
											scope="col"
											class="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase tracking-wider"
											><SkeletonLoader width="60px" height="1em" /></th
										>
										<th
											scope="col"
											class="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase tracking-wider"
											><SkeletonLoader width="70px" height="1em" /></th
										>
										<th
											scope="col"
											class="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase tracking-wider"
											><SkeletonLoader width="70px" height="1em" /></th
										>
										<th
											scope="col"
											class="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase tracking-wider"
											><SkeletonLoader width="50px" height="1em" /></th
										>
										<th
											scope="col"
											class="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase tracking-wider"
											><SkeletonLoader width="60px" height="1em" /></th
										>
										<th
											scope="col"
											class="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase tracking-wider"
											><SkeletonLoader width="60px" height="1em" /></th
										>
									</tr>
								</thead>
								<tbody class="bg-white divide-y divide-gray-200">
									{#each Array(5) as _, i}
										<tr>
											<td class="px-4 py-3 whitespace-nowrap text-sm font-medium text-gray-900"
												><SkeletonLoader width="100px" height="1em" /></td
											>
											<td class="px-4 py-3 whitespace-nowrap text-sm text-gray-500 text-right"
												><SkeletonLoader width="30px" height="1em" /></td
											>
											<td class="px-4 py-3 whitespace-nowrap text-sm text-gray-500 text-right"
												><SkeletonLoader width="50px" height="1em" /></td
											>
											<td class="px-4 py-3 whitespace-nowrap text-sm text-gray-500 text-right"
												><SkeletonLoader width="50px" height="1em" /></td
											>
											<td class="px-4 py-3 whitespace-nowrap text-sm text-gray-500 text-right"
												><SkeletonLoader width="60px" height="1em" /></td
											>
											<td class="px-4 py-3 whitespace-nowrap text-sm text-gray-500 text-right"
												><SkeletonLoader width="60px" height="1em" /></td
											>
											<td class="px-4 py-3 whitespace-nowrap text-sm text-right"
												><SkeletonLoader width="40px" height="1em" /></td
											>
											<td class="px-4 py-3 whitespace-nowrap text-sm text-right"
												><SkeletonLoader width="50px" height="1em" /></td
											>
											<td class="px-4 py-3 whitespace-nowrap text-sm text-right"
												><SkeletonLoader width="50px" height="1em" /></td
											>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					</div>
				{:else if holdings && holdings.length > 0}
					<div class="mb-6 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
						<div class="bg-muted/50 p-4 rounded-lg shadow-sm">
							<p class="text-sm text-muted-foreground">Total Investment</p>
							<p class="text-lg font-semibold text-foreground">{formatCurrency(totalInvestment)}</p>
						</div>
						<div class="bg-muted/50 p-4 rounded-lg shadow-sm">
							<p class="text-sm text-muted-foreground">Current Value</p>
							<p class="text-lg font-semibold text-foreground">{formatCurrency(totalCurrentValue)}</p>
						</div>
						<div class="bg-muted/50 p-4 rounded-lg shadow-sm">
							<p class="text-sm text-muted-foreground">Day's P&L</p>
							<p
								class="text-lg font-semibold {totalDayPnL >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}"
							>
								{formatCurrency(totalDayPnL)} ({formatPercentage(totalDayPnLPercentage)})
							</p>
						</div>
						<div class="bg-muted/50 p-4 rounded-lg shadow-sm">
							<p class="text-sm text-muted-foreground">Total P&L</p>
							<p class="text-lg font-semibold {totalPnL >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}">
								{formatCurrency(totalPnL)} ({formatPercentage(totalPnLPercentage)})
							</p>
						</div>
					</div>

					<div class="overflow-x-auto">
						<table class="min-w-full divide-y divide-border rounded-lg overflow-hidden">
							<thead class="bg-muted/50">
								<tr>
									<th
										scope="col"
										class="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase tracking-wider"
										>Instrument</th
									>
									<th
										scope="col"
										class="px-4 py-3 text-right text-xs font-semibold text-muted-foreground uppercase tracking-wider"
										>Qty.</th
									>
									<th
										scope="col"
										class="px-4 py-3 text-right text-xs font-semibold text-muted-foreground uppercase tracking-wider"
										>Avg. Cost</th
									>
									<th
										scope="col"
										class="px-4 py-3 text-right text-xs font-semibold text-muted-foreground uppercase tracking-wider"
										>LTP</th
									>
									<th
										scope="col"
										class="px-4 py-3 text-right text-xs font-semibold text-muted-foreground uppercase tracking-wider"
										>Invested</th
									>
									<th
										scope="col"
										class="px-4 py-3 text-right text-xs font-semibold text-muted-foreground uppercase tracking-wider"
										>Cur. Val</th
									>
									<th
										scope="col"
										class="px-4 py-3 text-right text-xs font-semibold text-muted-foreground uppercase tracking-wider"
										>P&L</th
									>
									<th
										scope="col"
										class="px-4 py-3 text-right text-xs font-semibold text-muted-foreground uppercase tracking-wider"
										>Net Chg. (%)</th
									>
									<th
										scope="col"
										class="px-4 py-3 text-right text-xs font-semibold text-muted-foreground uppercase tracking-wider"
										>Day Chg. (%)</th
									>
								</tr>
							</thead>
							<tbody class="bg-card divide-y divide-border">
								{#each holdings as holding (holding.instrument_token)}
									<tr class="hover:bg-muted/30 transition-colors duration-150">
										<td class="px-4 py-3 whitespace-nowrap text-sm font-medium text-card-foreground"
											>{holding.tradingsymbol}</td
										>
										<td class="px-4 py-3 whitespace-nowrap text-sm text-card-foreground text-right"
											>{holding.quantity}</td
										>
										<td class="px-4 py-3 whitespace-nowrap text-sm text-card-foreground text-right"
											>{formatCurrency(holding.average_price)}</td
										>
										<td
											class="px-4 py-3 whitespace-nowrap text-sm text-card-foreground text-right {holding.lastPriceUpdated &&
											Date.now() - holding.lastPriceUpdated < 1000
												? 'bg-primary/20 transition-all duration-500'
												: ''}">{formatCurrency(holding.last_price)}</td
										>
										<td class="px-4 py-3 whitespace-nowrap text-sm text-card-foreground text-right"
											>{formatCurrency(holding.quantity * holding.average_price)}</td
										>
										<td class="px-4 py-3 whitespace-nowrap text-sm text-card-foreground text-right"
											>{formatCurrency(holding.quantity * holding.last_price)}</td
										>
										<td
											class="px-4 py-3 whitespace-nowrap text-sm text-right {holding.pnl >= 0
												? 'text-green-600 dark:text-green-400'
												: 'text-red-600 dark:text-red-400'} font-medium">{formatCurrency(holding.pnl)}</td
										>
										<td
											class="px-4 py-3 whitespace-nowrap text-sm text-right {holding.pnl >= 0
												? 'text-green-600 dark:text-green-400'
												: 'text-red-600 dark:text-red-400'} font-medium"
											>{formatPercentage(
												(holding.pnl / (holding.quantity * holding.average_price)) * 100
											)}</td
										>
										<td
											class="px-4 py-3 whitespace-nowrap text-sm text-right {holding.dayChange >= 0
												? 'text-green-600 dark:text-green-400'
												: 'text-red-600 dark:text-red-400'} font-medium"
											>{formatPercentage(holding.dayChangePercentage)}</td
										>
									</tr>
								{/each}
								<tr class="bg-muted/50 font-bold">
									<td class="px-4 py-3 whitespace-nowrap text-sm text-card-foreground" colspan="4"
										>Total</td
									>
									<td class="px-4 py-3 whitespace-nowrap text-sm text-card-foreground text-right"
										>{formatCurrency(totalInvestment)}</td
									>
									<td class="px-4 py-3 whitespace-nowrap text-sm text-card-foreground text-right"
										>{formatCurrency(totalCurrentValue)}</td
									>
									<td
										class="px-4 py-3 whitespace-nowrap text-sm text-right {totalPnL >= 0
											? 'text-green-600 dark:text-green-400'
											: 'text-red-600 dark:text-red-400'}">{formatCurrency(totalPnL)}</td
									>
									<td
										class="px-4 py-3 whitespace-nowrap text-sm text-right {totalPnL >= 0
											? 'text-green-600 dark:text-green-400'
											: 'text-red-600 dark:text-red-400'}">{formatPercentage(totalPnLPercentage)}</td
									>
									<td
										class="px-4 py-3 whitespace-nowrap text-sm text-right {totalDayPnL >= 0
											? 'text-green-600 dark:text-green-400'
											: 'text-red-600 dark:text-red-400'}">{formatPercentage(totalDayPnLPercentage)}</td
									>
								</tr>
							</tbody>
						</table>
					</div>
				{:else}
					<div class="text-center p-8">
						<p class="text-muted-foreground">No holdings data available.</p>
					</div>
				{/if}
			{/if}
		</div>
	</div>
</div>
