<script lang="ts">
	import { onMount } from 'svelte';
	import SkeletonLoader from '$lib/components/SkeletonLoader.svelte';
	import MomentumStockTable from '$lib/components/MomentumStockTable.svelte';
	import AllocationSummary from '$lib/components/AllocationSummary.svelte';
	import OrderExecution from '$lib/components/OrderExecution.svelte';
	import { apiFetch, getApiBase } from '$lib/api';

	const API_BASE_URL = getApiBase();

	let momentumStocks: any[] = [];
	let investableMargin: number = 0; // Renamed from availableMargin
	let marginAllocationPercentage: number = 20; // Default to 20%
	let selectedStocks: { [key: string]: boolean } = {};
	let calculatedAllocations: any[] = []; // Stores the full allocation details from backend
	let totalAllocatedValue: number = 0;
	let unallocatedCapital: number = 0;
	let loading = true;
	let error: string | null = null;

	// Order execution state (passed to OrderExecution component)
	let useBasket: boolean = true;
	let allOrNone: boolean = false;
	let marginPreview: any = null;
	let previewingMargins: boolean = false;
	let marginPreviewError: string | null = null;
	let isExecutingOrders = false;
	let orderExecutionError: string | null = null;
	let successfulOrdersCount = 0;
	let failedOrdersCount = 0;

	let portfolioPerformance: any[] = [];
	let fetchingPerformance = false;
	let performanceError: string | null = null;

	const STRATEGY_NAME = 'Momentum Portfolio';

	onMount(async () => {
		await fetchInitialData();
		await fetchPortfolioPerformance();
	});

	async function fetchInitialData() {
		loading = true;
		error = null;
		try {
			// Fetch momentum stocks
			const stocksResponse = await apiFetch(`/broker/momentum-portfolio`);
			if (!stocksResponse.ok) {
				throw new Error(`Failed to fetch momentum stocks: ${stocksResponse.statusText}`);
			}
			const stocksData = await stocksResponse.json();
			momentumStocks = stocksData.top_momentum_stocks;

			// Initialize all stocks as selected by default
			momentumStocks.forEach((stock) => {
				selectedStocks[stock.symbol] = true;
			});

			// Fetch investable margin from the new endpoint
			const marginsResponse = await apiFetch(`/broker/momentum-portfolio/investable-margin`);
			if (!marginsResponse.ok) {
				throw new Error(`Failed to fetch investable margin: ${marginsResponse.statusText}`);
			}
			const marginsData = await marginsResponse.json();
			investableMargin = marginsData.investable_margin;

			await calculateEquiAllocations(); // Call the new allocation calculation
		} catch (e: any) {
			console.error('Error fetching initial data:', e);
			error = e.message;
		} finally {
			loading = false;
		}
	}

	async function fetchPortfolioPerformance() {
		fetchingPerformance = true;
		performanceError = null;
		try {
			const response = await apiFetch(
				`/broker/portfolio/performance?strategy_name=${encodeURIComponent(STRATEGY_NAME)}`
			);
			if (!response.ok) {
				if (response.status === 404) {
					portfolioPerformance = [];
				} else {
					throw new Error(`Failed to fetch portfolio performance: ${response.statusText}`);
				}
			} else {
				portfolioPerformance = await response.json();
			}
		} catch (e: any) {
			console.error('Error fetching portfolio performance:', e);
			performanceError = e.message;
		} finally {
			fetchingPerformance = false;
		}
	}

	async function calculateEquiAllocations() {
		const selectedSymbols = momentumStocks
			.filter((stock) => selectedStocks[stock.symbol])
			.map((stock) => stock.symbol);

		if (selectedSymbols.length === 0) {
			calculatedAllocations = [];
			totalAllocatedValue = 0;
			unallocatedCapital = investableMargin * (marginAllocationPercentage / 100);
			return;
		}

		const capitalForAllocation = investableMargin * (marginAllocationPercentage / 100);

		try {
			const response = await apiFetch(`/broker/momentum-portfolio/calculate-equi-allocation`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					selected_symbols: selectedSymbols,
					investable_capital: capitalForAllocation,
					excluded_symbols: [] // Backend handles exclusions based on price now
				})
			});

			if (!response.ok) {
				const errorData = await response.json();
				throw new Error(errorData.detail || 'Failed to calculate equi-allocation');
			}

			const data = await response.json();
			calculatedAllocations = data.allocations;
			totalAllocatedValue = data.total_allocated_value;
			unallocatedCapital = data.unallocated_capital;
		} catch (e: any) {
			console.error('Error calculating equi-allocations:', e);
			error = e.message; // Display error on the page
			calculatedAllocations = [];
			totalAllocatedValue = 0;
			unallocatedCapital = capitalForAllocation;
		}
	}

	// Reactively call allocation calculation when dependencies change
	$: if (momentumStocks.length > 0 && investableMargin > 0 && marginAllocationPercentage >= 0) {
		calculateEquiAllocations();
	}
	$: if (selectedStocks) {
		calculateEquiAllocations();
	}

	function handleStockSelectionChange(event: CustomEvent) {
		selectedStocks = event.detail;
		// Recalculation is triggered by the reactive statement above
	}

	function buildOrderLegs(): any[] {
		const legs: any[] = [];
		for (const allocation of calculatedAllocations) {
			if (allocation.status === 'ALLOCATED' && allocation.quantity > 0) {
				legs.push({
					exchange: 'NSE',
					tradingsymbol: allocation.symbol,
					transaction_type: 'BUY',
					quantity: allocation.quantity,
					product: 'CNC',
					order_type: 'MARKET',
					validity: 'DAY',
					variety: 'regular',
					tag: 'strategy:momentum'
				});
			}
		}
		return legs;
	}

	async function previewMargins() {
		marginPreview = null;
		marginPreviewError = null;
		previewingMargins = true;
		try {
			const orders = buildOrderLegs();
			if (orders.length === 0) {
				throw new Error('No valid orders to preview. Adjust allocation or selections.');
			}
			const resp = await apiFetch(`/broker/orders/preview_margins`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					orders,
					consider_positions: true,
					margin_mode: 'compact'
				})
			});
			const data = await resp.json();
			if (!resp.ok) {
				throw new Error(data.detail || 'Failed to preview margins');
			}
			marginPreview = data.data || data.margins || data;
		} catch (e: any) {
			console.error('Margin preview error:', e);
			marginPreviewError = e.message;
		} finally {
			previewingMargins = false;
		}
	}

	async function executeOrders() {
		isExecutingOrders = true;
		orderExecutionError = null;
		successfulOrdersCount = 0;
		failedOrdersCount = 0;

		const ordersToPlace = buildOrderLegs();

		if (ordersToPlace.length === 0) {
			orderExecutionError = 'No stocks selected or no shares calculated for purchase.';
			isExecutingOrders = false;
			return;
		}

		const snapshotData: any[] = [];

		if (useBasket) {
			try {
				const resp = await apiFetch(`/broker/orders/place_basket`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({
						orders: ordersToPlace,
						consider_positions: true,
						margin_mode: 'compact',
						all_or_none: allOrNone,
						dry_run: false
					})
				});
				const data = await resp.json();
				if (!resp.ok) {
					throw new Error(data.detail || 'Basket placement failed');
				}

				const results = data.results || [];
				for (const r of results) {
					if (r.status === 'success' && r.order_id) {
						successfulOrdersCount++;
						const stock = momentumStocks.find((s) => s.symbol === r.tradingsymbol);
						const allocated = calculatedAllocations.find((a) => a.symbol === r.tradingsymbol);
						if (stock && allocated) {
							snapshotData.push({
								strategy_name: STRATEGY_NAME,
								symbol: r.tradingsymbol,
								quantity: allocated.quantity,
								purchase_price: stock.ltp, // Use the LTP from momentumStocks for snapshot
								total_value: allocated.quantity * stock.ltp
							});
						}
					} else {
						failedOrdersCount++;
					}
				}
				if (data.status === 'failed') {
					orderExecutionError = 'Basket placement failed for one or more legs.';
				}
			} catch (e: any) {
				orderExecutionError = e.message;
				console.error('Basket placement error:', e);
			} finally {
				isExecutingOrders = false;
			}
		} else {
			// Sequential single orders (fallback) - This path is less ideal for equi-weighting
			// and might not be used if basket orders are preferred.
			// For simplicity, we'll keep it but note that the backend /broker/place_single_order
			// was removed. This section needs to be updated or removed.
			orderExecutionError =
				'Single order placement is not supported for this strategy. Please use basket orders.';
			isExecutingOrders = false;
			return;
		}

		// If there are successful orders, create a portfolio snapshot
		if (snapshotData.length > 0) {
			try {
				const snapshotResponse = await apiFetch(`/broker/portfolio/snapshot`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify(snapshotData)
				});

				if (!snapshotResponse.ok) {
					const errorData = await snapshotResponse.json();
					console.error(
						'Failed to create portfolio snapshot:',
						errorData.detail || snapshotResponse.statusText
					);
				} else {
					console.log('Portfolio snapshot created successfully.');
					await fetchPortfolioPerformance();
				}
			} catch (e: any) {
				console.error('Error creating portfolio snapshot:', e);
			}
		}
	}

	async function refreshLivePrices() {
		loading = true;
		error = null;
		try {
			const symbolsToFetch = momentumStocks.map((stock) => stock.symbol);
			if (symbolsToFetch.length === 0) {
				console.warn('No momentum stocks to refresh LTP for.');
				return;
			}

			const queryParams = new URLSearchParams();
			symbolsToFetch.forEach((symbol) => {
				queryParams.append('symbols', symbol);
			});

			const response = await apiFetch(
				`/broker/momentum-portfolio/live-ltp?${queryParams.toString()}`
			);

			if (!response.ok) {
				throw new Error(`Failed to fetch live LTP: ${response.statusText}`);
			}

			const liveLtpData = await response.json();

			// Update the ltp for each stock in momentumStocks
			momentumStocks = momentumStocks.map((stock) => {
				if (liveLtpData[stock.symbol] !== undefined) {
					return { ...stock, ltp: liveLtpData[stock.symbol] };
				}
				return stock;
			});

			// Trigger recalculation of allocations with new LTPs
			await calculateEquiAllocations();
		} catch (e: any) {
			console.error('Error refreshing live prices:', e);
			error = e.message;
		} finally {
			loading = false;
		}
	}
</script>

<div class="container mx-auto p-4">
	<h1 class="text-3xl font-bold mb-6">Momentum Portfolio Strategy</h1>

	{#if loading}
		<SkeletonLoader />
	{:else if error}
		<div
			class="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded relative"
			role="alert"
		>
			<strong class="font-bold">Error!</strong>
			<span class="block sm:inline">{error}</span>
		</div>
	{:else}
		<div class="grid grid-cols-1 md:grid-cols-3 gap-6">
			<div class="md:col-span-1">
				<div class="bg-white p-6 rounded-lg shadow-md mb-6">
					<h2 class="text-xl font-semibold mb-4">Strategy Settings</h2>
					<div class="mb-4">
						<label for="marginAllocation" class="block text-sm font-medium text-gray-700">
							Allocate Margin (%): {marginAllocationPercentage}%
						</label>
						<input
							type="range"
							id="marginAllocation"
							min="0"
							max="100"
							step="1"
							bind:value={marginAllocationPercentage}
							class="mt-1 block w-full"
						/>
						<input
							type="number"
							min="0"
							max="100"
							step="1"
							bind:value={marginAllocationPercentage}
							class="mt-2 block w-full border-gray-300 rounded-md shadow-sm focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm"
						/>
					</div>
					<div class="mb-4">
						<p class="text-sm text-gray-600">
							Total Investable Margin: ₹{investableMargin.toFixed(2)}
						</p>
						<p class="text-sm text-gray-600">
							Capital for Allocation: ₹{(
								investableMargin *
								(marginAllocationPercentage / 100)
							).toFixed(2)}
						</p>
					</div>
				</div>

				<OrderExecution
					bind:useBasket
					bind:allOrNone
					bind:previewingMargins
					bind:marginPreview
					bind:marginPreviewError
					bind:isExecutingOrders
					bind:orderExecutionError
					bind:successfulOrdersCount
					bind:failedOrdersCount
					on:previewMargins={previewMargins}
					on:executeOrders={executeOrders}
				/>
			</div>

			<div class="md:col-span-2">
				<div class="bg-white p-6 rounded-lg shadow-md mb-6">
					<div class="flex justify-between items-center mb-4">
						<h2 class="text-xl font-semibold">Top Momentum Stocks</h2>
						<button
							on:click={refreshLivePrices}
							class="bg-green-500 hover:bg-green-600 text-white px-4 py-2 rounded-md transition duration-200"
							disabled={loading}
						>
							Refresh Live Prices
						</button>
					</div>
					<MomentumStockTable
						stocks={momentumStocks}
						bind:selectedStocks
						calculatedShares={Object.fromEntries(
							calculatedAllocations.map((a) => [a.symbol, a.quantity])
						)}
						on:select={handleStockSelectionChange}
					/>
				</div>

				<AllocationSummary
					investableCapital={investableMargin * (marginAllocationPercentage / 100)}
					{totalAllocatedValue}
					{unallocatedCapital}
					allocations={calculatedAllocations}
				/>
			</div>
		</div>

		<div class="mt-8 bg-white p-6 rounded-lg shadow-md">
			<h2 class="text-xl font-semibold mb-4">Portfolio Performance ({STRATEGY_NAME})</h2>
			{#if fetchingPerformance}
				<p>Loading performance data...</p>
			{:else if performanceError}
				<div
					class="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded relative"
					role="alert"
				>
					<strong class="font-bold">Error!</strong>
					<span class="block sm:inline">{performanceError}</span>
				</div>
			{:else if portfolioPerformance.length === 0}
				<p>No performance data available yet. Execute some orders to start tracking!</p>
			{:else}
				<div class="overflow-x-auto">
					<table class="min-w-full divide-y divide-gray-200">
						<thead class="bg-gray-50">
							<tr>
								<th
									scope="col"
									class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider"
								>
									Date
								</th>
								<th
									scope="col"
									class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider"
								>
									Total Capital (₹)
								</th>
								<th
									scope="col"
									class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider"
								>
									Total Value (₹)
								</th>
								<th
									scope="col"
									class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider"
								>
									P&L (₹)
								</th>
								<th
									scope="col"
									class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider"
								>
									P&L (%)
								</th>
							</tr>
						</thead>
						<tbody class="bg-white divide-y divide-gray-200">
							{#each portfolioPerformance as record (record.id)}
								<tr>
									<td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">
										{new Date(record.timestamp).toLocaleDateString()}
									</td>
									<td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
										{record.total_capital.toFixed(2)}
									</td>
									<td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
										{record.total_value.toFixed(2)}
									</td>
									<td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
										{record.profit_loss.toFixed(2)}
									</td>
									<td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
										{record.percentage_change.toFixed(2)}%
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>
	{/if}
</div>
