<script lang="ts">
    import { onMount } from 'svelte';
    import { fade } from 'svelte/transition';
    import SkeletonLoader from '$lib/components/SkeletonLoader.svelte';

    let momentumStocks: any[] = [];
    let availableMargin: number = 0;
    let marginAllocationPercentage: number = 20; // Default to 20%
    let selectedStocks: { [key: string]: boolean } = {};
    let calculatedShares: { [key: string]: number } = {};
    let totalInvestment: number = 0;
    let unallocatedFunds: number = 0;
    let loading = true;
    let error: string | null = null;

    let orderExecutionStatus: { [key: string]: string } = {}; // To store status for each stock
    let isExecutingOrders = false;
    let orderExecutionError: string | null = null;
    let successfulOrdersCount = 0;
    let failedOrdersCount = 0;

    let portfolioPerformance: any[] = [];
    let fetchingPerformance = false;
    let performanceError: string | null = null;

    const STRATEGY_NAME = "Momentum Portfolio"; // Define strategy name

    // Order placement options and margin preview state
    let useBasket: boolean = true;
    let allOrNone: boolean = false;
    let marginPreview: any = null;
    let previewingMargins: boolean = false;
    let marginPreviewError: string | null = null;

    onMount(async () => {
        await fetchInitialData();
        await fetchPortfolioPerformance();
    });

    async function fetchInitialData() {
        loading = true;
        error = null;
        try {
            // Fetch momentum stocks
            const stocksResponse = await fetch('/momentum-portfolio');
            if (!stocksResponse.ok) {
                throw new Error(`Failed to fetch momentum stocks: ${stocksResponse.statusText}`);
            }
            const stocksData = await stocksResponse.json();
            momentumStocks = stocksData.top_momentum_stocks;

            // Initialize all stocks as selected by default
            momentumStocks.forEach(stock => {
                selectedStocks[stock.symbol] = true;
            });

            // Fetch available margin
            const marginsResponse = await fetch('/broker/margins');
            if (!marginsResponse.ok) {
                throw new Error(`Failed to fetch available margins: ${marginsResponse.statusText}`);
            }
            const marginsData = await marginsResponse.json();
            availableMargin = marginsData.equity.net;

            calculateAllocations();

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
            const response = await fetch(`/broker/portfolio/performance?strategy_name=${encodeURIComponent(STRATEGY_NAME)}`);
            if (!response.ok) {
                if (response.status === 404) {
                    portfolioPerformance = []; // No data found is not an error, just empty
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

    function buildOrderLegs(): any[] {
        const legs: any[] = [];
        for (const stock of momentumStocks) {
            if (selectedStocks[stock.symbol] && calculatedShares[stock.symbol] > 0) {
                legs.push({
                    exchange: "NSE",
                    tradingsymbol: stock.symbol,
                    transaction_type: "BUY",
                    quantity: calculatedShares[stock.symbol],
                    product: "CNC",
                    order_type: "MARKET",
                    validity: "DAY",
                    variety: "regular",
                    tag: "strategy:momentum"
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
                throw new Error("No valid orders to preview. Adjust allocation or selections.");
            }
            const resp = await fetch('/broker/orders/preview_margins', {
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

    $: if (momentumStocks.length > 0 && availableMargin > 0 && marginAllocationPercentage >= 0) {
        calculateAllocations();
    }

    function calculateAllocations() {
        const currentlySelectedStocks = momentumStocks.filter(stock => selectedStocks[stock.symbol]);
        if (currentlySelectedStocks.length === 0) {
            calculatedShares = {};
            totalInvestment = 0;
            unallocatedFunds = availableMargin * (marginAllocationPercentage / 100);
            return;
        }

        let totalAllocatedCapital = availableMargin * (marginAllocationPercentage / 100);
        let remainingCapital = totalAllocatedCapital;
        let sharesPerStock: { [key: string]: number } = {};
        let affordableStocks = [...currentlySelectedStocks]; // Create a mutable copy

        // Iteratively allocate shares, removing unaffordable stocks
        while (affordableStocks.length > 0 && remainingCapital > 0) {
            const capitalPerAffordableStock = remainingCapital / affordableStocks.length;
            let newAffordableStocks: any[] = [];
            let capitalReallocated = 0;

            for (const stock of affordableStocks) {
                const ltp = stock.ltp;
                if (ltp > 0 && capitalPerAffordableStock >= ltp) {
                    const shares = Math.floor(capitalPerAffordableStock / ltp);
                    sharesPerStock[stock.symbol] = shares;
                    remainingCapital -= (shares * ltp);
                    newAffordableStocks.push(stock); // Keep this stock for next iteration if needed
                } else {
                    // This stock is too pricey for the current allocation, exclude it
                    sharesPerStock[stock.symbol] = 0;
                    capitalReallocated += capitalPerAffordableStock; // Reallocate its share
                }
            }

            if (newAffordableStocks.length === affordableStocks.length) {
                // No more stocks were removed, so we've reached a stable allocation
                break;
            } else {
                affordableStocks = newAffordableStocks;
                remainingCapital += capitalReallocated; // Add back capital from excluded stocks
            }
        }

        // Final pass to ensure all remaining capital is distributed among truly affordable stocks
        // This handles cases where initial allocation might leave some small unallocated amounts
        // or if a stock became affordable after others were removed.
        if (affordableStocks.length > 0 && remainingCapital > 0) {
            const additionalCapitalPerStock = remainingCapital / affordableStocks.length;
            for (const stock of affordableStocks) {
                const ltp = stock.ltp;
                if (ltp > 0) {
                    const additionalShares = Math.floor(additionalCapitalPerStock / ltp);
                    sharesPerStock[stock.symbol] = (sharesPerStock[stock.symbol] || 0) + additionalShares;
                    remainingCapital -= (additionalShares * ltp);
                }
            }
        }

        calculatedShares = sharesPerStock;
        totalInvestment = totalAllocatedCapital - remainingCapital;
        unallocatedFunds = remainingCapital;
    }


    async function executeOrders() {
        isExecutingOrders = true;
        orderExecutionError = null;
        orderExecutionStatus = {};
        successfulOrdersCount = 0;
        failedOrdersCount = 0;

        const ordersToPlace = momentumStocks.filter(stock =>
            selectedStocks[stock.symbol] && calculatedShares[stock.symbol] > 0
        );

        if (ordersToPlace.length === 0) {
            orderExecutionError = "No stocks selected or no shares calculated for purchase.";
            isExecutingOrders = false;
            return;
        }

        const snapshotData: any[] = [];

        if (useBasket) {
            // Set initial status for all selected stocks
            for (const stock of ordersToPlace) {
                orderExecutionStatus[stock.symbol] = "Placing in basket...";
            }
            try {
                const orders = buildOrderLegs();
                if (orders.length === 0) {
                    throw new Error("No valid orders to place.");
                }
                const resp = await fetch('/broker/orders/place_basket', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        orders,
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
                    const symbol = r.tradingsymbol;
                    if (r.status === 'success' && r.order_id) {
                        orderExecutionStatus[symbol] = `Success (Order ID: ${r.order_id})`;
                        successfulOrdersCount++;
                        const stock = momentumStocks.find(s => s.symbol === symbol);
                        if (stock) {
                            snapshotData.push({
                                strategy_name: STRATEGY_NAME,
                                symbol,
                                quantity: calculatedShares[symbol],
                                purchase_price: stock.ltp,
                                total_value: calculatedShares[symbol] * stock.ltp
                            });
                        }
                    } else {
                        orderExecutionStatus[symbol] = `Failed: ${r.error || 'Unknown error'}`;
                        failedOrdersCount++;
                    }
                }
                if (data.status === 'failed') {
                    orderExecutionError = "Basket placement failed for one or more legs.";
                }
            } catch (e: any) {
                orderExecutionError = e.message;
                console.error('Basket placement error:', e);
                // mark all as failed if none were processed
                for (const stock of ordersToPlace) {
                    if (!orderExecutionStatus[stock.symbol]) {
                        orderExecutionStatus[stock.symbol] = `Failed: ${e.message}`;
                        failedOrdersCount++;
                    }
                }
            } finally {
                isExecutingOrders = false;
            }
        } else {
            // Sequential single orders (fallback)
            for (const stock of ordersToPlace) {
                orderExecutionStatus[stock.symbol] = "Placing...";
                try {
                    const orderPayload = {
                        tradingsymbol: stock.symbol,
                        exchange: "NSE",
                        transaction_type: "BUY",
                        quantity: calculatedShares[stock.symbol],
                        product: "CNC",
                        order_type: "MARKET",
                        price: null,
                        validity: "DAY"
                    };

                    const response = await fetch('/broker/place_single_order', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(orderPayload)
                    });

                    const result = await response.json();
                    if (!response.ok) {
                        throw new Error(result.detail || `Failed to place order for ${stock.symbol}`);
                    }

                    orderExecutionStatus[stock.symbol] = `Success (Order ID: ${result.order_id})`;
                    successfulOrdersCount++;

                    snapshotData.push({
                        strategy_name: STRATEGY_NAME,
                        symbol: stock.symbol,
                        quantity: calculatedShares[stock.symbol],
                        purchase_price: stock.ltp,
                        total_value: calculatedShares[stock.symbol] * stock.ltp
                    });
                } catch (e: any) {
                    orderExecutionStatus[stock.symbol] = `Failed: ${e.message}`;
                    failedOrdersCount++;
                    orderExecutionError = `Some orders failed. Check individual statuses.`;
                    console.error(`Error placing order for ${stock.symbol}:`, e);
                }
            }
            isExecutingOrders = false;
        }

        // If there are successful orders, create a portfolio snapshot
        if (snapshotData.length > 0) {
            try {
                const snapshotResponse = await fetch('/broker/portfolio/snapshot', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(snapshotData)
                });

                if (!snapshotResponse.ok) {
                    const errorData = await snapshotResponse.json();
                    console.error('Failed to create portfolio snapshot:', errorData.detail || snapshotResponse.statusText);
                } else {
                    console.log('Portfolio snapshot created successfully.');
                    await fetchPortfolioPerformance();
                }
            } catch (e: any) {
                console.error('Error creating portfolio snapshot:', e);
            }
        }
    }
</script>

<div class="container mx-auto p-4">
    <h1 class="text-3xl font-bold mb-6">Momentum Portfolio Strategy</h1>

    {#if isExecutingOrders}
        <div class="bg-blue-100 border border-blue-400 text-blue-700 px-4 py-3 rounded relative mb-4" role="alert">
            <strong class="font-bold">Executing Orders...</strong>
            <span class="block sm:inline">Please wait.</span>
        </div>
    {/if}

    {#if orderExecutionError}
        <div class="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded relative mb-4" role="alert">
            <strong class="font-bold">Order Execution Error!</strong>
            <span class="block sm:inline">{orderExecutionError}</span>
        </div>
    {/if}

    {#if !isExecutingOrders && (successfulOrdersCount > 0 || failedOrdersCount > 0)}
        <div class="bg-green-100 border border-green-400 text-green-700 px-4 py-3 rounded relative mb-4" role="alert">
            <strong class="font-bold">Order Execution Complete!</strong>
            <span class="block sm:inline">
                {successfulOrdersCount} successful, {failedOrdersCount} failed.
            </span>
        </div>
    {/if}

    {#if loading}
        <SkeletonLoader />
    {:else if error}
        <div class="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded relative" role="alert">
            <strong class="font-bold">Error!</strong>
            <span class="block sm:inline">{error}</span>
        </div>
    {:else}
        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
            <div class="md:col-span-1 bg-white p-6 rounded-lg shadow-md">
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
                    <p class="text-sm text-gray-600">Available Equity Margin: ₹{availableMargin.toFixed(2)}</p>
                    <p class="text-sm text-gray-600">Total Capital for Strategy: ₹{(availableMargin * (marginAllocationPercentage / 100)).toFixed(2)}</p>
                    <p class="text-sm text-gray-600">Unallocated Funds: ₹{unallocatedFunds.toFixed(2)}</p>
                </div>
                <div class="mt-4">
                    <label class="inline-flex items-center">
                        <input type="checkbox" bind:checked={useBasket} class="h-4 w-4 text-indigo-600 border-gray-300 rounded" />
                        <span class="ml-2 text-sm text-gray-700">Use basket order</span>
                    </label>
                    {#if useBasket}
                        <label class="inline-flex items-center mt-2 block">
                            <input type="checkbox" bind:checked={allOrNone} class="h-4 w-4 text-indigo-600 border-gray-300 rounded" />
                            <span class="ml-2 text-sm text-gray-700">All-or-none basket (best-effort rollback)</span>
                        </label>
                        <div class="mt-3 flex gap-2">
                            <button
                                on:click={previewMargins}
                                class="flex-1 bg-gray-100 text-gray-800 py-2 px-4 rounded-md hover:bg-gray-200 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2"
                                disabled={previewingMargins}
                            >
                                {previewingMargins ? 'Previewing...' : 'Preview Margins'}
                            </button>
                            <button
                                on:click={executeOrders}
                                class="flex-1 bg-blue-600 text-white py-2 px-4 rounded-md hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
                            >
                                Execute Orders
                            </button>
                        </div>
                        {#if marginPreview}
                            <div class="mt-3">
                                <p class="text-sm text-gray-600">Margin preview:</p>
                                <pre class="text-xs bg-gray-100 p-2 rounded overflow-x-auto">{JSON.stringify(marginPreview, null, 2)}</pre>
                            </div>
                        {:else if marginPreviewError}
                            <div class="mt-3 text-sm text-red-600">{marginPreviewError}</div>
                        {/if}
                    {:else}
                        <button
                            on:click={executeOrders}
                            class="w-full bg-blue-600 text-white py-2 px-4 rounded-md hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
                        >
                            Execute Orders
                        </button>
                    {/if}
                </div>
            </div>

            <div class="md:col-span-2 bg-white p-6 rounded-lg shadow-md">
                <h2 class="text-xl font-semibold mb-4">Top 15 Momentum Stocks</h2>
                <div class="overflow-x-auto">
                    <table class="min-w-full divide-y divide-gray-200">
                        <thead class="bg-gray-50">
                            <tr>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                    Select
                                </th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                    Symbol
                                </th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                    252-Day Return (%)
                                </th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                    LTP (₹)
                                </th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                    Shares
                                </th>
                            </tr>
                        </thead>
                        <tbody class="bg-white divide-y divide-gray-200">
                            {#each momentumStocks as stock (stock.symbol)}
                                <tr class={calculatedShares[stock.symbol] === 0 && selectedStocks[stock.symbol] ? 'bg-red-50' : ''}>
                                    <td class="px-6 py-4 whitespace-nowrap">
                                        <input
                                            type="checkbox"
                                            bind:checked={selectedStocks[stock.symbol]}
                                            on:change={calculateAllocations}
                                            class="focus:ring-indigo-500 h-4 w-4 text-indigo-600 border-gray-300 rounded"
                                        />
                                    </td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">
                                        {stock.symbol}
                                        {#if calculatedShares[stock.symbol] === 0 && selectedStocks[stock.symbol]}
                                            <span class="ml-2 text-red-500 text-xs">(Too pricey)</span>
                                        {/if}
                                    </td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                        {stock.ret.toFixed(2)}
                                    </td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                        {stock.ltp.toFixed(2)}
                                    </td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                        {calculatedShares[stock.symbol] || 0}
                                    </td>
                                </tr>
                            {/each}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <div class="mt-8 bg-white p-6 rounded-lg shadow-md">
            <h2 class="text-xl font-semibold mb-4">Portfolio Performance ({STRATEGY_NAME})</h2>
            {#if fetchingPerformance}
                <p>Loading performance data...</p>
            {:else if performanceError}
                <div class="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded relative" role="alert">
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
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                    Date
                                </th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                    Total Capital (₹)
                                </th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                    Total Value (₹)
                                </th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                    P&L (₹)
                                </th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
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