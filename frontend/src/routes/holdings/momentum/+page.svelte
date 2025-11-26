<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { apiFetch } from '$lib/api';

	// Debounce utility
	let debounceTimer: any;
	function debounce(fn: Function, delay: number = 300) {
		clearTimeout(debounceTimer);
		debounceTimer = setTimeout(fn, delay);
	}
	import * as Card from '$lib/components/ui/card';
	import * as Table from '$lib/components/ui/table';
	import * as Alert from '$lib/components/ui/alert';
	import * as AlertDialog from '$lib/components/ui/alert-dialog';
	import * as Tabs from '$lib/components/ui/tabs';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import { Checkbox } from '$lib/components/ui/checkbox';
	import { Separator } from '$lib/components/ui/separator';
	import { Loader2, RefreshCw, TrendingUp, TrendingDown, DollarSign, Package, LogOut, RotateCcw, Check, X, AlertCircle, Plus, Minus } from '@lucide/svelte';

	// State
	let activeTab: string = 'new';
	let sources: string[] = [];
	let selectedSource: string = 'Nifty50';
	
	// New Entry State
	let momentumStocks: any[] = [];
	let investableMargin: number = 0;
	let allocationAmount: number = 0;  // Direct amount input instead of percentage
	let selectedStocks: { [key: string]: boolean } = {};
	let calculatedAllocations: any[] = [];
	let totalAllocatedValue: number = 0;
	let unallocatedCapital: number = 0;
	
	// Active Portfolio State - grouped by index
	let activeHoldings: any[] = [];
	let holdingsByIndex: { [key: string]: any[] } = {};  // Grouped holdings
	let activePortfolioValue: number = 0;
	let activePortfolioPnL: number = 0;
	
	// Insufficient funds warning
	let insufficientFunds: boolean = false;
	
	// Rebalance State
	let rebalanceAnalysis: any = null;
	let showRebalanceDialog = false;
	let rebalancing = false;
	let executingRebalance = false;
	
	// Functions to adjust rebalance quantities
	function updateRebalanceEntryQty(symbol: string, delta: number) {
		if (!rebalanceAnalysis?.entries) return;
		const entry = rebalanceAnalysis.entries.find((e: any) => e.symbol === symbol);
		if (entry) {
			const newQty = Math.max(0, (entry.quantity || 0) + delta);
			entry.quantity = newQty;
			entry.allocated_value = newQty * entry.ltp;
			// Update buy_orders
			const buyOrder = rebalanceAnalysis.buy_orders?.find((b: any) => b.symbol === symbol);
			if (buyOrder) {
				buyOrder.quantity = newQty;
			}
			// Recalculate summary
			recalculateRebalanceSummary();
			rebalanceAnalysis = { ...rebalanceAnalysis }; // Trigger reactivity
		}
	}
	
	function updateRebalanceHoldQty(symbol: string, delta: number) {
		if (!rebalanceAnalysis?.holds_adjust) return;
		const hold = rebalanceAnalysis.holds_adjust.find((h: any) => h.symbol === symbol);
		if (hold) {
			const newQty = Math.max(0, (hold.new_quantity || hold.current_quantity) + delta);
			const adjustment = newQty - hold.current_quantity;
			hold.new_quantity = newQty;
			hold.adjustment_quantity = adjustment;
			hold.adjustment_value = adjustment * (hold.ltp || 0);
			hold.action = adjustment > 0 ? 'BUY_MORE' : adjustment < 0 ? 'SELL_PARTIAL' : 'HOLD';
			
			// Update buy_orders or sell_orders
			if (adjustment > 0) {
				// Remove from sell_orders, add/update buy_orders
				rebalanceAnalysis.sell_orders = rebalanceAnalysis.sell_orders?.filter((s: any) => s.symbol !== symbol) || [];
				const existingBuy = rebalanceAnalysis.buy_orders?.find((b: any) => b.symbol === symbol);
				if (existingBuy) {
					existingBuy.quantity = adjustment;
				} else {
					rebalanceAnalysis.buy_orders = [...(rebalanceAnalysis.buy_orders || []), { symbol, quantity: adjustment, ltp: hold.ltp || 0, exchange: 'NSE' }];
				}
			} else if (adjustment < 0) {
				// Remove from buy_orders, add/update sell_orders
				rebalanceAnalysis.buy_orders = rebalanceAnalysis.buy_orders?.filter((b: any) => b.symbol !== symbol) || [];
				const existingSell = rebalanceAnalysis.sell_orders?.find((s: any) => s.symbol !== symbol);
				if (existingSell) {
					existingSell.quantity = Math.abs(adjustment);
				} else {
					rebalanceAnalysis.sell_orders = [...(rebalanceAnalysis.sell_orders || []), { symbol, quantity: Math.abs(adjustment), ltp: hold.ltp || 0, exchange: 'NSE' }];
				}
			} else {
				// No adjustment - remove from both
				rebalanceAnalysis.buy_orders = rebalanceAnalysis.buy_orders?.filter((b: any) => b.symbol !== symbol) || [];
				rebalanceAnalysis.sell_orders = rebalanceAnalysis.sell_orders?.filter((s: any) => s.symbol !== symbol) || [];
			}
			
			// Recalculate summary
			recalculateRebalanceSummary();
			rebalanceAnalysis = { ...rebalanceAnalysis }; // Trigger reactivity
		}
	}
	
	function recalculateRebalanceSummary() {
		if (!rebalanceAnalysis) return;
		const totalBuyValue = (rebalanceAnalysis.buy_orders || []).reduce((sum: number, b: any) => sum + (b.quantity * b.ltp), 0);
		const totalSellValue = (rebalanceAnalysis.sell_orders || []).reduce((sum: number, s: any) => sum + (s.quantity * s.ltp), 0);
		const exitValue = (rebalanceAnalysis.exits || []).reduce((sum: number, e: any) => sum + (e.exit_value || 0), 0);
		
		rebalanceAnalysis.summary = {
			...rebalanceAnalysis.summary,
			total_buy_value: totalBuyValue,
			total_sell_value: totalSellValue + exitValue,
			net_cash_required: totalBuyValue - totalSellValue - exitValue,
			entry_count: rebalanceAnalysis.entries?.filter((e: any) => e.quantity > 0).length || 0,
			adjust_count: rebalanceAnalysis.holds_adjust?.filter((h: any) => h.action !== 'HOLD').length || 0
		};
	}
	
	// Capital adjustment
	let capitalAdjustmentPercentage: number = 0;
	let showCapitalAdjustment = false;
	let capitalAdjustmentType: 'increase' | 'decrease' = 'increase';
	
	// Margin preview state
	let marginPreview: any = null;
	let totalMarginPreview: any = null;
	let previewingMargins = false;
	let previewingTotalMargins = false;
	
	// AMO (After Market Order) toggle
	let useAMO = false;
	
	// Loading states
	let loading = true;
	let loadingSources = true;
	let refreshing = false;
	let calculating = false;
	let executing = false;
	let exiting = false;
	
	// Error states
	let error: string | null = null;
	let allocationError: string | null = null;
	let executionError: string | null = null;
	let marginError: string | null = null;

	// Confirmation state
	let showConfirmation = false;
	let showExitConfirmation = false;
	
	// Execution results
	let executionResults: any = null;
	
	// Toast notifications
	let toastMessage: string = '';
	let toastType: 'success' | 'error' | 'info' = 'info';
	let showToast = false;
	
	function showNotification(message: string, type: 'success' | 'error' | 'info' = 'info') {
		toastMessage = message;
		toastType = type;
		showToast = true;
		setTimeout(() => {
			showToast = false;
		}, 5000);
	}

	const STRATEGY_NAME = 'Nifty50 Momentum'; // Matches backend default

	onMount(async () => {
		await Promise.all([fetchSources(), fetchInitialData()]);
	});

	async function fetchSources() {
		loadingSources = true;
		try {
			const response = await apiFetch('/broker/momentum-portfolio/sources');
			if (response.ok) {
				const data = await response.json();
				sources = data.sources || [];
				if (sources.length > 0 && !selectedSource) {
					selectedSource = sources[0];
				}
			}
		} catch (e: any) {
			console.error('Error fetching sources:', e);
		} finally {
			loadingSources = false;
		}
	}

	async function fetchInitialData() {
		loading = true;
		error = null;
		try {
			// 1. Check for Active Holdings (including PENDING that will be verified)
			const holdingsResponse = await apiFetch(
				`/broker/momentum-portfolio/holdings?strategy_name=${encodeURIComponent(STRATEGY_NAME)}&status=ACTIVE`
			);
			if (holdingsResponse.ok) {
				const data = await holdingsResponse.json();
				activeHoldings = data.holdings || [];
				
				// Also check for PENDING holdings (orders placed but not yet verified)
				const pendingResponse = await apiFetch(
					`/broker/momentum-portfolio/holdings?strategy_name=${encodeURIComponent(STRATEGY_NAME)}&status=PENDING`
				);
				if (pendingResponse.ok) {
					const pendingData = await pendingResponse.json();
					// Add pending holdings to the list
					activeHoldings = [...activeHoldings, ...(pendingData.holdings || [])];
				}
				
				// Group holdings by linked_index_symbol
				holdingsByIndex = {};
				activeHoldings.forEach(h => {
					const index = h.linked_index_symbol || 'Unknown';
					if (!holdingsByIndex[index]) {
						holdingsByIndex[index] = [];
					}
					holdingsByIndex[index].push(h);
				});
				
				if (activeHoldings.length > 0) {
					activeTab = 'active'; // Default to active if holdings exist, but user can switch back
					calculatePortfolioStats();
					// Fetch current prices for holdings to update P&L
					refreshHoldingsPrices(); 
				} else {
					activeTab = 'new';
				}
			}

			// 2. Fetch Market Data (needed for both views technically, but essential for 'new')
			const params = selectedSource ? `?source_list=${encodeURIComponent(selectedSource)}` : '';
			const [stocksResponse, marginsResponse] = await Promise.all([
				apiFetch(`/broker/momentum-portfolio${params}`),
				apiFetch('/broker/momentum-portfolio/investable-margin')
			]);

			if (!stocksResponse.ok) throw new Error('Failed to fetch momentum stocks');
			if (!marginsResponse.ok) throw new Error('Failed to fetch margins');

			const stocksData = await stocksResponse.json();
			const marginsData = await marginsResponse.json();

			momentumStocks = stocksData.top_momentum_stocks || [];
			investableMargin = marginsData.investable_margin || 0;

			// Initialize selection for new entries
			selectedStocks = {};
			momentumStocks.forEach((stock) => {
				selectedStocks[stock.symbol] = true;
			});
			await calculateAllocations();
			await previewTotalMargins();

		} catch (e: any) {
			console.error('Error fetching data:', e);
			error = e.message;
		} finally {
			loading = false;
		}
	}

	// Fetch only momentum stocks for the selected source (without switching tabs)
	async function fetchMomentumDataOnly() {
		loading = true;
		error = null;
		try {
			const params = selectedSource ? `?source_list=${encodeURIComponent(selectedSource)}` : '';
			const [stocksResponse, marginsResponse] = await Promise.all([
				apiFetch(`/broker/momentum-portfolio${params}`),
				apiFetch('/broker/momentum-portfolio/investable-margin')
			]);

			if (!stocksResponse.ok) throw new Error('Failed to fetch momentum stocks');
			if (!marginsResponse.ok) throw new Error('Failed to fetch margins');

			const stocksData = await stocksResponse.json();
			const marginsData = await marginsResponse.json();

			momentumStocks = stocksData.top_momentum_stocks || [];
			investableMargin = marginsData.investable_margin || 0;

			// Reset selection for new source
			selectedStocks = {};
			momentumStocks.forEach((stock) => {
				selectedStocks[stock.symbol] = true;
			});
			await calculateAllocations();
		} catch (e: any) {
			console.error('Error fetching momentum data:', e);
			error = e.message;
		} finally {
			loading = false;
		}
	}

	function calculatePortfolioStats() {
		activePortfolioValue = activeHoldings.reduce((sum, h) => sum + (h.quantity * h.last_price), 0);
		const totalInvested = activeHoldings.reduce((sum, h) => sum + h.invested_amount, 0);
		activePortfolioPnL = activePortfolioValue - totalInvested;
	}

	async function refreshHoldingsPrices() {
		try {
			const symbols = activeHoldings.map(h => h.symbol);
			if (symbols.length === 0) return;

			const queryParams = new URLSearchParams();
			symbols.forEach((symbol) => queryParams.append('symbols', symbol));

			const response = await apiFetch(`/broker/momentum-portfolio/live-ltp?${queryParams.toString()}`);
			if (response.ok) {
				const liveLtpData = await response.json();
				activeHoldings = activeHoldings.map(h => ({
					...h,
					last_price: liveLtpData[h.symbol] || h.last_price
				}));
				calculatePortfolioStats();
			}
		} catch (e) {
			console.error("Failed to refresh holdings prices", e);
		}
	}

	async function calculateAllocations() {
		calculating = true;
		allocationError = null;
		insufficientFunds = false;
		
		try {
			const selectedSymbols = momentumStocks
				.filter((stock) => selectedStocks[stock.symbol])
				.map((stock) => stock.symbol);

			if (selectedSymbols.length === 0 || allocationAmount <= 0) {
				calculatedAllocations = [];
				totalAllocatedValue = 0;
				unallocatedCapital = allocationAmount;
				return;
			}

			// Check if allocation amount exceeds available margin
			if (allocationAmount > investableMargin) {
				insufficientFunds = true;
			}

			const response = await apiFetch('/broker/momentum-portfolio/calculate-equi-allocation', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					selected_symbols: selectedSymbols,
					investable_capital: allocationAmount,
					excluded_symbols: []
				})
			});

			if (!response.ok) {
				const errorData = await response.json();
				throw new Error(errorData.detail || 'Failed to calculate allocation');
			}

			const data = await response.json();
			calculatedAllocations = data.allocations || [];
			totalAllocatedValue = data.total_allocated_value || 0;
			unallocatedCapital = data.unallocated_capital || 0;
		} catch (e: any) {
			console.error('Error calculating allocations:', e);
			allocationError = e.message;
			calculatedAllocations = [];
			totalAllocatedValue = 0;
			unallocatedCapital = allocationAmount;
		} finally {
			calculating = false;
		}
	}

	async function refreshPrices() {
		refreshing = true;
		try {
			if (activeTab === 'active') {
				await refreshHoldingsPrices();
			}
			
			// Always refresh market data if we have it
			if (momentumStocks.length > 0) {
				const symbolsToFetch = momentumStocks.map((stock) => stock.symbol);
				if (symbolsToFetch.length > 0) {
					const queryParams = new URLSearchParams();
					symbolsToFetch.forEach((symbol) => queryParams.append('symbols', symbol));

					const response = await apiFetch(`/broker/momentum-portfolio/live-ltp?${queryParams.toString()}`);
					if (!response.ok) throw new Error('Failed to fetch live prices');

					const liveLtpData = await response.json();
					
					momentumStocks = momentumStocks.map((stock) => ({
						...stock,
						ltp: liveLtpData[stock.symbol] ?? stock.ltp
					}));
				}
			}
		} catch (e: any) {
			console.error('Error refreshing prices:', e);
		} finally {
			refreshing = false;
		}
	}

	async function previewMargins() {
		previewingMargins = true;
		marginError = null;
		marginPreview = null;
		try {
			const orders = calculatedAllocations
				.filter((a) => a.status === 'ALLOCATED' && a.quantity > 0)
				.map((a) => ({
					exchange: 'NSE',
					tradingsymbol: a.symbol,
					transaction_type: 'BUY',
					variety: 'regular',
					product: 'CNC',
					order_type: 'MARKET',
					quantity: a.quantity,
					price: 0,
					trigger_price: 0
				}));

			if (orders.length === 0) {
				marginPreview = null;
				return;
			}

			const response = await apiFetch('/broker/margins/basket?consider_positions=true&mode=compact', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(orders)
			});

			if (!response.ok) {
				const errorData = await response.json();
				throw new Error(errorData.detail || 'Failed to preview margins');
			}

			marginPreview = await response.json();
		} catch (e: any) {
			console.error('Error previewing margins:', e);
			marginError = e.message;
		} finally {
			previewingMargins = false;
		}
	}

	async function previewTotalMargins() {
		previewingTotalMargins = true;
		try {
			// Calculate allocation for ALL stocks with full capital
			const response = await apiFetch('/broker/momentum-portfolio/calculate-equi-allocation', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					selected_symbols: momentumStocks.map(s => s.symbol),
					investable_capital: investableMargin,
					excluded_symbols: []
				})
			});

			if (!response.ok) return;

			const data = await response.json();
			const allAllocations = data.allocations || [];
			
			const orders = allAllocations
				.filter((a: any) => a.status === 'ALLOCATED' && a.quantity > 0)
				.map((a: any) => ({
					exchange: 'NSE',
					tradingsymbol: a.symbol,
					transaction_type: 'BUY',
					variety: 'regular',
					product: 'CNC',
					order_type: 'MARKET',
					quantity: a.quantity,
					price: 0,
					trigger_price: 0
				}));

			if (orders.length === 0) return;

			const marginResponse = await apiFetch('/broker/margins/basket?consider_positions=true&mode=compact', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(orders)
			});

			if (marginResponse.ok) {
				totalMarginPreview = await marginResponse.json();
			}
		} catch (e: any) {
			console.error('Error previewing total margins:', e);
		} finally {
			previewingTotalMargins = false;
		}
	}

	async function executeBasketOrders() {
		executing = true;
		executionError = null;
		executionResults = null;
		try {
			const selectedSymbols = calculatedAllocations
				.filter((a) => a.status === 'ALLOCATED' && a.quantity > 0)
				.map((a) => a.symbol);

			if (selectedSymbols.length === 0) {
				throw new Error('No valid orders to execute');
			}

			// Use the new atomic place-and-enter endpoint
			const linkedIndex = selectedSource === 'Nifty50' ? 'NIFTY 50' 
				: selectedSource === 'Nifty500' ? 'NIFTY 500' 
				: selectedSource === 'NiftyLargeMidcap250' ? 'NIFTY LARGEMIDCAP 250'
				: 'NIFTY 50';
			
			const payload = {
				selected_symbols: selectedSymbols,
				investable_capital: allocationAmount,
				excluded_symbols: [],
				strategy_name: STRATEGY_NAME,
				strategy_type: 'MOMENTUM',
				linked_index_symbol: linkedIndex,
				use_amo: useAMO
			};
			
			console.log('Place-and-enter payload:', JSON.stringify(payload, null, 2));
			
			const response = await apiFetch('/broker/momentum-portfolio/place-and-enter', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload)
			});

			if (!response.ok) {
				const errorData = await response.json();
				console.error('Place-and-enter error:', errorData);
				
				if (errorData.detail) {
					if (Array.isArray(errorData.detail)) {
						const errors = errorData.detail.map((e: any) => `${e.loc?.join('.') || ''}: ${e.msg}`).join(', ');
						throw new Error(`Validation failed: ${errors}`);
					} else if (typeof errorData.detail === 'string') {
						throw new Error(errorData.detail);
					} else {
						throw new Error(JSON.stringify(errorData.detail));
					}
				}
				throw new Error('Order execution failed');
			}

			const data = await response.json();
			executionResults = data;
			marginPreview = null;
			
			console.log('Place-and-enter response:', data);

			if (data.status === 'success') {
				showNotification(`Portfolio '${data.portfolio_tag}' created with ${data.holdings_created} holdings!`, 'success');
				setTimeout(() => {
					window.location.reload();
				}, 2000);
			} else if (data.status === 'partial') {
				showNotification(`Partial success: ${data.holdings_created}/${data.orders_placed} holdings created`, 'info');
				setTimeout(() => {
					window.location.reload();
				}, 3000);
			} else {
				executionError = data.message || 'Order execution failed';
			}

		} catch (e: any) {
			console.error('Error executing orders:', e);
			executionError = e.message;
		} finally {
			executing = false;
		}
	}

	// Variable to track which index portfolio to exit
	let exitingIndexSymbol: string | null = null;

	async function exitAllPositions(indexSymbol: string | null = null) {
		exiting = true;
		try {
			// Filter holdings by index if specified
			const linkedIndex = indexSymbol || (selectedSource === 'Nifty50' ? 'NIFTY 50' 
				: selectedSource === 'Nifty500' ? 'NIFTY 500' 
				: selectedSource === 'NiftyLargeMidcap250' ? 'NIFTY LARGEMIDCAP 250'
				: null);
			
			const holdingsToExit = linkedIndex 
				? activeHoldings.filter(h => h.linked_index_symbol === linkedIndex)
				: activeHoldings;
			
			if (holdingsToExit.length === 0) {
				showNotification('No holdings to exit for this index', 'info');
				return;
			}
			
			// 1. Place Sell Orders
			const sellOrders = holdingsToExit.map(h => {
				const order: any = {
					exchange: h.exchange,
					tradingsymbol: h.symbol,
					transaction_type: 'SELL',
					variety: useAMO ? 'amo' : 'regular',
					product: 'CNC',
					order_type: useAMO ? 'LIMIT' : 'MARKET',
					quantity: h.quantity,
					validity: 'DAY',
					tag: `MOM-EXIT`
				};
				
				if (useAMO) {
					order.price = h.last_price || 0;
				}
				
				return order;
			});

			const orderResponse = await apiFetch('/broker/orders/basket', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					orders: sellOrders,
					all_or_none: false,
					dry_run: false
				})
			});

			if (!orderResponse.ok) throw new Error("Failed to place exit orders");

			const orderData = await orderResponse.json();
			
			// 2. Mark as Exited in DB with order results
			await apiFetch('/broker/momentum-portfolio/exit-portfolio', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					strategy_name: STRATEGY_NAME,
					exit_orders: orderData.results || [],
					linked_index_symbol: linkedIndex
				})
			});

			showNotification(`Portfolio exit initiated for ${linkedIndex || 'all holdings'}`, 'success');
			setTimeout(() => window.location.reload(), 2000);

		} catch (e: any) {
			console.error("Exit failed", e);
			showNotification("Exit failed: " + e.message, 'error');
		} finally {
			exiting = false;
			showExitConfirmation = false;
			exitingIndexSymbol = null;
		}
	}

	async function analyzeRebalance(customCapital: number | null = null, indexSymbol: string | null = null) {
		rebalancing = true;
		try {
			// Determine the linked_index_symbol to filter by
			const linkedIndex = indexSymbol || (selectedSource === 'Nifty50' ? 'NIFTY 50' 
				: selectedSource === 'Nifty500' ? 'NIFTY 500' 
				: selectedSource === 'NiftyLargeMidcap250' ? 'NIFTY LARGEMIDCAP 250'
				: null);
			
			// Derive the correct source_list from linkedIndex for fetching new top stocks
			const sourceForIndex = linkedIndex === 'NIFTY 50' ? 'Nifty50'
				: linkedIndex === 'NIFTY 500' ? 'Nifty500'
				: linkedIndex === 'NIFTY LARGEMIDCAP 250' ? 'NiftyLargeMidcap250'
				: selectedSource;
			
			// Get fresh top stocks for this specific index
			const response = await apiFetch(`/broker/momentum-portfolio?source_list=${encodeURIComponent(sourceForIndex)}`);
			if (!response.ok) throw new Error("Failed to fetch fresh data");
			const data = await response.json();
			const newTopStocks = data.top_momentum_stocks || [];

			// Filter holdings by linked_index_symbol (only rebalance this index's portfolio)
			const holdingsForIndex = linkedIndex 
				? activeHoldings.filter(h => h.linked_index_symbol === linkedIndex)
				: activeHoldings;
			
			console.log(`Rebalancing ${linkedIndex}: ${holdingsForIndex.length} holdings, ${newTopStocks.length} new top stocks`);

			// Use custom capital if provided, otherwise maintain current value
			const payload: any = {
				current_holdings: holdingsForIndex,
				new_top_stocks: newTopStocks,
				linked_index_symbol: linkedIndex
			};
			
			if (customCapital !== null && customCapital > 0) {
				payload.target_capital = customCapital;
			}

			const rebalanceResponse = await apiFetch('/broker/momentum-portfolio/rebalance', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload)
			});

			if (rebalanceResponse.ok) {
				rebalanceAnalysis = await rebalanceResponse.json();
				showRebalanceDialog = true;
			} else {
				const errorData = await rebalanceResponse.json();
				throw new Error(errorData.detail || 'Rebalance analysis failed');
			}
		} catch (e: any) {
			console.error("Rebalance analysis failed", e);
			showNotification("Rebalance analysis failed: " + e.message, 'error');
		} finally {
			rebalancing = false;
		}
	}
	
	async function executeRebalance() {
		executingRebalance = true;
		try {
			if (!rebalanceAnalysis) throw new Error('No rebalance analysis available');
			
			// Get linked_index_symbol from rebalance analysis or derive from selectedSource
			const linkedIndex = rebalanceAnalysis.linked_index_symbol || (selectedSource === 'Nifty50' ? 'NIFTY 50' 
				: selectedSource === 'Nifty500' ? 'NIFTY 500' 
				: selectedSource === 'NiftyLargeMidcap250' ? 'NIFTY LARGEMIDCAP 250'
				: 'NIFTY 50');
			
			const response = await apiFetch('/broker/momentum-portfolio/execute-rebalance', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					strategy_name: STRATEGY_NAME,
					tag: activeHoldings[0]?.tag || null,
					sell_orders: rebalanceAnalysis.sell_orders || [],
					buy_orders: rebalanceAnalysis.buy_orders || [],
					exits: rebalanceAnalysis.exits || [],
					entries: rebalanceAnalysis.entries || [],
					holds_adjust: rebalanceAnalysis.holds_adjust || [],
					use_amo: useAMO,
					linked_index_symbol: linkedIndex
				})
			});
			
			if (!response.ok) {
				const errorData = await response.json();
				throw new Error(errorData.detail || 'Rebalance execution failed');
			}
			
			const result = await response.json();
			showNotification(result.message || 'Rebalance executed successfully', 'success');
			showRebalanceDialog = false;
			
			// Reload after a delay
			setTimeout(() => window.location.reload(), 2000);
		} catch (e: any) {
			console.error('Rebalance execution failed', e);
			showNotification('Rebalance failed: ' + e.message, 'error');
		} finally {
			executingRebalance = false;
		}
	}
	
	function openCapitalAdjustment() {
		capitalAdjustmentPercentage = 0;
		capitalAdjustmentType = 'increase';
		showCapitalAdjustment = true;
	}
	
	async function applyCapitalAdjustment() {
		if (capitalAdjustmentPercentage === 0) {
			showNotification('Please enter an allocation percentage', 'error');
			return;
		}
		
		const currentValue = activeHoldings.reduce((sum, h) => sum + (h.quantity * h.last_price), 0);
		let targetCapital: number;
		
		if (capitalAdjustmentType === 'increase') {
			// Allocate additional percentage from available margin
			const additionalCapital = (investableMargin * capitalAdjustmentPercentage) / 100;
			targetCapital = currentValue + additionalCapital;
		} else {
			// Reduce to percentage of current value
			targetCapital = (currentValue * capitalAdjustmentPercentage) / 100;
		}
		
		showCapitalAdjustment = false;
		await analyzeRebalance(Math.round(targetCapital));
	}

	function toggleStockSelection(symbol: string) {
		selectedStocks[symbol] = !selectedStocks[symbol];
		selectedStocks = { ...selectedStocks }; // Trigger reactivity
	}

	function selectAllStocks() {
		momentumStocks.forEach((stock) => {
			selectedStocks[stock.symbol] = true;
		});
		selectedStocks = { ...selectedStocks }; // Trigger reactivity
	}

	function deselectAllStocks() {
		momentumStocks.forEach((stock) => {
			selectedStocks[stock.symbol] = false;
		});
		selectedStocks = { ...selectedStocks }; // Trigger reactivity
	}

	function updateQuantity(symbol: string, delta: number) {
		const allocation = calculatedAllocations.find(a => a.symbol === symbol);
		if (!allocation) return;
		
		const newQuantity = Math.max(0, (allocation.quantity || 0) + delta);
		const ltp = allocation.ltp || momentumStocks.find(s => s.symbol === symbol)?.ltp || 0;
		
		// Update allocation in place
		calculatedAllocations = calculatedAllocations.map(a => {
			if (a.symbol === symbol) {
				const allocated_value = newQuantity * ltp;
				return {
					...a,
					quantity: newQuantity,
					allocated_value: allocated_value,
					status: newQuantity > 0 ? 'ALLOCATED' : 'EXCLUDED'
				};
			}
			return a;
		});
		
		// Recalculate totals
		totalAllocatedValue = calculatedAllocations.reduce((sum, a) => sum + (a.allocated_value || 0), 0);
		unallocatedCapital = capitalForAllocation - totalAllocatedValue;
		
		// Trigger margin preview update
		debounce(() => previewMargins(), 200);
	}

	// Reactive statements
	// Re-fetch momentum stocks when source changes (only fetch momentum data, don't switch tabs)
	$: if (selectedSource && activeTab === 'new' && !loading) {
		debounce(() => {
			fetchMomentumDataOnly();
		}, 100);
	}

	$: selectedCount = Object.values(selectedStocks).filter(Boolean).length;
	$: validOrderCount = calculatedAllocations.filter(a => a.status === 'ALLOCATED' && a.quantity > 0).length;
	
	// Trigger recalculation when selection or allocation amount changes
	$: if (activeTab === 'new' && !loading && momentumStocks.length > 0 && (selectedCount >= 0 || allocationAmount > 0)) {
		debounce(() => calculateAllocations(), 150);
	}
	
	// Auto-preview margins when valid orders exist
	$: if (activeTab === 'new' && validOrderCount > 0 && !calculating) {
		debounce(() => previewMargins(), 200);
	}
	
	// Calculate total charges from order-level charges
	$: totalCharges = marginPreview?.orders?.reduce((sum: number, order: any) => {
		return sum + (order.charges?.total || 0);
	}, 0) || 0;
	
	$: totalAllCharges = totalMarginPreview?.orders?.reduce((sum: number, order: any) => {
		return sum + (order.charges?.total || 0);
	}, 0) || 0;
</script>

<div class="container mx-auto p-6 space-y-6">
	<!-- Header -->
	<div class="flex items-center justify-end gap-3">
			<!-- Universe Selector -->
			<div class="w-40">
				<select
					id="source-select"
					bind:value={selectedSource}
					class="w-full px-3 py-2 rounded-md border border-input bg-background text-foreground text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
				>
					{#each sources as source}
						<option value={source}>{source}</option>
					{/each}
				</select>
			</div>
			
			<Button
				variant="outline"
				size="sm"
				on:click={refreshPrices}
				disabled={refreshing || loading}
			>
				{#if refreshing}
					<Loader2 class="mr-2 h-4 w-4 animate-spin" />
				{:else}
					<RefreshCw class="mr-2 h-4 w-4" />
				{/if}
				Refresh
			</Button>

			{#if activeTab === 'active' && activeHoldings.length > 0}
				<Button
					variant="outline"
					size="sm"
					on:click={() => analyzeRebalance(null)}
					disabled={rebalancing}
				>
					{#if rebalancing}
						<Loader2 class="mr-2 h-4 w-4 animate-spin" />
					{:else}
						<RotateCcw class="mr-2 h-4 w-4" />
					{/if}
					Rebalance All
				</Button>
			{/if}
		</div>

	{#if error}
		<Alert.Root variant="destructive">
			<Alert.Title>Error</Alert.Title>
			<Alert.Description>{error}</Alert.Description>
		</Alert.Root>
	{/if}
	
	{#if executionError}
		<Alert.Root variant="destructive">
			<Alert.Description>{executionError}</Alert.Description>
		</Alert.Root>
	{/if}

	{#if loading}
		<div class="flex items-center justify-center p-12">
			<Loader2 class="h-8 w-8 animate-spin text-muted-foreground" />
		</div>
	{:else}
		<Tabs.Root bind:value={activeTab} class="w-full">
			<Tabs.List>
				<Tabs.Trigger value="new">New Strategy</Tabs.Trigger>
				<Tabs.Trigger value="active">Active Positions ({activeHoldings.length})</Tabs.Trigger>
			</Tabs.List>

			<Tabs.Content value="active">
				{#if activeHoldings.length === 0}
					<div class="flex flex-col items-center justify-center p-12 border rounded-md border-dashed text-muted-foreground mt-4">
						<Package class="h-12 w-12 mb-4 opacity-50" />
						<p class="text-lg font-medium">No active positions</p>
						<p class="text-sm">Execute a strategy from the "New Strategy" tab to see positions here.</p>
					</div>
				{:else}
					<!-- ACTIVE PORTFOLIO VIEW - Grouped by Index -->
					<div class="space-y-6 mt-4">
						{#each Object.entries(holdingsByIndex) as [indexSymbol, holdings]}
							{@const indexValue = holdings.reduce((s, h) => s + (h.quantity * h.last_price), 0)}
							{@const indexInvested = holdings.reduce((s, h) => s + h.invested_amount, 0)}
							{@const indexPnL = indexValue - indexInvested}
							{@const indexPnLPct = indexInvested > 0 ? (indexPnL / indexInvested) * 100 : 0}
							
							<Card.Root>
								<Card.Header>
									<div class="flex items-center justify-between">
										<div>
											<Card.Title class="text-lg">{indexSymbol}</Card.Title>
											<p class="text-sm text-muted-foreground">{holdings.length} holdings</p>
										</div>
										<div class="flex items-center gap-4">
											<!-- Portfolio Summary inline -->
											<div class="text-right">
												<p class="text-sm text-muted-foreground">Value</p>
												<p class="font-semibold">₹{indexValue.toLocaleString('en-IN', {maximumFractionDigits: 0})}</p>
											</div>
											<div class="text-right">
												<p class="text-sm text-muted-foreground">P&L</p>
												<p class="font-semibold {indexPnL >= 0 ? 'text-green-600' : 'text-red-600'}">
													{indexPnL >= 0 ? '+' : ''}₹{Math.abs(indexPnL).toLocaleString('en-IN', {maximumFractionDigits: 0})}
													<span class="text-xs">({indexPnLPct.toFixed(1)}%)</span>
												</p>
											</div>
											<Button
												variant="outline"
												size="sm"
												on:click={() => analyzeRebalance(null, indexSymbol)}
												disabled={rebalancing}
											>
												<RotateCcw class="mr-1 h-4 w-4" />
												Rebalance
											</Button>
											<Button
												variant="destructive"
												size="sm"
												on:click={() => { exitingIndexSymbol = indexSymbol; showExitConfirmation = true; }}
												disabled={exiting}
											>
												<LogOut class="mr-1 h-4 w-4" />
												Exit
											</Button>
										</div>
									</div>
								</Card.Header>
								<Card.Content>
									<Table.Root>
										<Table.Header>
											<Table.Row>
												<Table.Head>Symbol</Table.Head>
												<Table.Head class="text-right">Qty</Table.Head>
												<Table.Head class="text-right">Avg Price</Table.Head>
												<Table.Head class="text-right">LTP</Table.Head>
												<Table.Head class="text-right">Value</Table.Head>
												<Table.Head class="text-right">P&L</Table.Head>
												<Table.Head class="text-right">Status</Table.Head>
											</Table.Row>
										</Table.Header>
										<Table.Body>
											{#each holdings as holding}
												{@const currentVal = holding.quantity * holding.last_price}
												{@const pnl = currentVal - holding.invested_amount}
												{@const pnlPct = holding.invested_amount > 0 ? (pnl / holding.invested_amount) * 100 : 0}
												<Table.Row>
													<Table.Cell class="font-medium">{holding.symbol}</Table.Cell>
													<Table.Cell class="text-right">{holding.quantity}</Table.Cell>
													<Table.Cell class="text-right">₹{holding.entry_price?.toFixed(2) || '0.00'}</Table.Cell>
													<Table.Cell class="text-right">₹{holding.last_price?.toFixed(2) || '0.00'}</Table.Cell>
													<Table.Cell class="text-right">₹{currentVal.toLocaleString('en-IN', {maximumFractionDigits: 0})}</Table.Cell>
													<Table.Cell class="text-right">
														<span class={pnl >= 0 ? 'text-green-600' : 'text-red-600'}>
															{pnl >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%
														</span>
													</Table.Cell>
													<Table.Cell class="text-right">
														<Badge variant={holding.status === 'ACTIVE' ? 'default' : 'secondary'}>
															{holding.status}
														</Badge>
													</Table.Cell>
												</Table.Row>
											{/each}
										</Table.Body>
									</Table.Root>
								</Card.Content>
							</Card.Root>
						{/each}
					</div>
				{/if}
			</Tabs.Content>

			<Tabs.Content value="new">
				<!-- NEW PORTFOLIO VIEW -->
				<div class="grid gap-6 md:grid-cols-3">
					<!-- Left Column: Settings & Stats -->
					<div class="space-y-6">
						<!-- Capital Allocation -->
						<Card.Root>
							<Card.Header>
								<Card.Title class="flex items-center gap-2">
									<DollarSign class="h-5 w-5" />
									Capital Allocation
								</Card.Title>
							</Card.Header>
							<Card.Content class="space-y-4">
								<div class="space-y-2">
									<Label for="allocation-amount">Allocation Amount (₹)</Label>
									<Input
										id="allocation-amount"
										type="number"
										min="0"
										step="1000"
										bind:value={allocationAmount}
										placeholder="Enter amount to allocate"
										class="text-lg font-medium"
									/>
								</div>
								
								<div class="space-y-2 text-sm">
									<div class="flex justify-between">
										<span class="text-muted-foreground">Available Margin</span>
										<span class="font-medium text-foreground">₹{investableMargin.toLocaleString('en-IN', {maximumFractionDigits: 0})}</span>
									</div>
									{#if allocationAmount > 0}
										<div class="flex justify-between">
											<span class="text-muted-foreground">Allocation</span>
											<span class="font-medium text-foreground">₹{allocationAmount.toLocaleString('en-IN', {maximumFractionDigits: 0})}</span>
										</div>
									{/if}
								</div>
								
								{#if insufficientFunds}
									<Alert.Root variant="destructive">
										<AlertCircle class="h-4 w-4" />
										<Alert.Description class="text-sm">
											Insufficient funds! Available: ₹{investableMargin.toLocaleString('en-IN', {maximumFractionDigits: 0})}. 
											Please add ₹{(allocationAmount - investableMargin).toLocaleString('en-IN', {maximumFractionDigits: 0})} to proceed.
										</Alert.Description>
									</Alert.Root>
								{/if}
								
								<!-- Quick allocation buttons -->
								<div class="flex gap-2 flex-wrap">
									<Button variant="outline" size="sm" on:click={() => allocationAmount = Math.round(investableMargin * 0.25)}>25%</Button>
									<Button variant="outline" size="sm" on:click={() => allocationAmount = Math.round(investableMargin * 0.5)}>50%</Button>
									<Button variant="outline" size="sm" on:click={() => allocationAmount = Math.round(investableMargin * 0.75)}>75%</Button>
									<Button variant="outline" size="sm" on:click={() => allocationAmount = investableMargin}>100%</Button>
								</div>
							</Card.Content>
						</Card.Root>

						<!-- Allocation Summary -->
						<Card.Root>
							<Card.Header>
								<Card.Title class="flex items-center gap-2">
									<Package class="h-5 w-5" />
									Summary
								</Card.Title>
							</Card.Header>
							<Card.Content class="space-y-3">
								<div class="grid grid-cols-2 gap-3">
									<div class="space-y-1">
										<p class="text-xs text-muted-foreground">Stocks Selected</p>
										<p class="text-2xl font-bold text-foreground">{selectedCount}</p>
									</div>
									<div class="space-y-1">
										<p class="text-xs text-muted-foreground">Total Stocks</p>
										<p class="text-2xl font-bold text-foreground">{momentumStocks.length}</p>
									</div>
								</div>
								
								<Separator />
								
								<div class="space-y-2 text-sm">
									<div class="flex justify-between">
										<span class="text-muted-foreground">Allocated Value</span>
										<span class="font-medium text-foreground">₹{totalAllocatedValue.toLocaleString('en-IN', {maximumFractionDigits: 0})}</span>
									</div>
									<div class="flex justify-between">
										<span class="text-muted-foreground">Unallocated</span>
										<span class="font-medium text-muted-foreground">₹{unallocatedCapital.toLocaleString('en-IN', {maximumFractionDigits: 0})}</span>
									</div>
								</div>
								
								<Separator />
								
								{#if totalMarginPreview}
									<div class="space-y-2 text-sm bg-muted/30 p-3 rounded-md">
										<p class="text-xs font-semibold text-muted-foreground uppercase">All Stocks (100% Capital)</p>
										<div class="flex justify-between">
											<span class="text-muted-foreground">Required Margin</span>
											<span class="font-medium text-foreground">₹{totalMarginPreview.final?.total?.toLocaleString('en-IN', {maximumFractionDigits: 0}) || 0}</span>
										</div>
										<div class="flex justify-between">
											<span class="text-muted-foreground">Est. Charges</span>
											<span class="font-medium text-muted-foreground">₹{totalAllCharges?.toLocaleString('en-IN', {maximumFractionDigits: 2}) || 0}</span>
										</div>
									</div>
								{/if}
								
								{#if marginPreview}
									<div class="space-y-2 text-sm bg-primary/5 p-3 rounded-md border border-primary/20">
										<p class="text-xs font-semibold text-primary uppercase">Selected Stocks ({selectedCount})</p>
										<div class="flex justify-between">
											<span class="text-muted-foreground">Required Margin</span>
											<span class="font-medium text-foreground">₹{marginPreview.final?.total?.toLocaleString('en-IN', {maximumFractionDigits: 0}) || 0}</span>
										</div>
										<div class="flex justify-between">
											<span class="text-muted-foreground">Est. Charges</span>
											<span class="font-medium text-muted-foreground">₹{totalCharges?.toLocaleString('en-IN', {maximumFractionDigits: 2}) || 0}</span>
										</div>
										<div class="flex justify-between pt-1 border-t border-primary/20">
											<span class="font-medium text-foreground">Total Cost</span>
											<span class="font-bold text-primary">₹{((marginPreview.final?.total || 0) + totalCharges).toLocaleString('en-IN', {maximumFractionDigits: 0})}</span>
										</div>
									</div>
								{/if}
								
								{#if marginPreview || totalMarginPreview}
									<Separator />
								{/if}
								
								<div class="flex items-center justify-between p-3 bg-muted/20 rounded-md">
									<div class="flex flex-col gap-1">
										<Label for="amo-toggle" class="text-sm font-medium cursor-pointer">
											After Market Order (AMO)
										</Label>
										<span class="text-xs text-muted-foreground">
											{useAMO ? 'Orders placed after market hours' : 'Regular market orders'}
										</span>
									</div>
									<Checkbox
										id="amo-toggle"
										checked={useAMO}
										onCheckedChange={(checked) => useAMO = checked}
									/>
								</div>
								
								<Button
									class="w-full"
									on:click={() => (showConfirmation = true)}
									disabled={executing || validOrderCount === 0}
								>
									{#if executing}
										<Loader2 class="mr-2 h-4 w-4 animate-spin" />
										Executing...
									{:else}
										Execute Basket Orders {useAMO ? '(AMO)' : ''}
									{/if}
								</Button>

								<AlertDialog.Root bind:open={showConfirmation}>
									<AlertDialog.Content>
										<AlertDialog.Header>
											<AlertDialog.Title>Confirm Order Execution</AlertDialog.Title>
											<AlertDialog.Description>
												You are about to place {validOrderCount} orders with a total value of ₹{totalAllocatedValue.toLocaleString(
													'en-IN',
													{ maximumFractionDigits: 0 }
												)}.
												{#if useAMO}
													<br /><br />
													<strong>Note:</strong> These will be placed as After Market Orders (AMO).
												{/if}
											</AlertDialog.Description>
										</AlertDialog.Header>
										<AlertDialog.Footer>
											<AlertDialog.Cancel>Cancel</AlertDialog.Cancel>
											<Button on:click={() => { executeBasketOrders(); showConfirmation = false; }}>Confirm Execution</Button>
										</AlertDialog.Footer>
									</AlertDialog.Content>
								</AlertDialog.Root>
								
								{#if marginError}
									<Alert.Root variant="destructive">
										<Alert.Description class="text-xs">{marginError}</Alert.Description>
									</Alert.Root>
								{/if}
								
								{#if executionResults}
									<Alert.Root>
										<Alert.Description class="text-xs">
											{executionResults.status === 'success' ? 
												`All ${executionResults.results?.length || 0} orders placed successfully` :
												`Placed ${executionResults.results?.filter((r: any) => r.status === 'success').length || 0} orders`
											}
										</Alert.Description>
									</Alert.Root>
								{/if}
							</Card.Content>
						</Card.Root>
					</div>

					<!-- Right Column: Stock Table -->
					<div class="md:col-span-2">
						<Card.Root>
							<Card.Header>
								<div class="flex items-center justify-between">
									<Card.Title>Top Momentum Stocks ({selectedSource})</Card.Title>
									<div class="flex gap-2">
										<Button variant="ghost" size="sm" on:click={selectAllStocks}>
											Select All
										</Button>
										<Button variant="ghost" size="sm" on:click={deselectAllStocks}>
											Clear
										</Button>
									</div>
								</div>
							</Card.Header>
							<Card.Content>
								<div class="rounded-md border">
									<Table.Root>
										<Table.Header>
											<Table.Row>
												<Table.Head class="w-12"></Table.Head>
												<Table.Head>Symbol</Table.Head>
												<Table.Head class="text-right">Return %</Table.Head>
												<Table.Head class="text-right">LTP</Table.Head>
												<Table.Head class="text-right">Quantity</Table.Head>
												<Table.Head class="text-right">Value</Table.Head>
												<Table.Head class="w-24">Status</Table.Head>
											</Table.Row>
										</Table.Header>
										<Table.Body>
											{#each momentumStocks as stock}
												{@const allocation = calculatedAllocations.find(a => a.symbol === stock.symbol)}
												{#key `${stock.symbol}-${allocation?.quantity || 0}-${allocation?.allocated_value || 0}`}
												<Table.Row class="hover:bg-muted/50">
													<Table.Cell>
														<Checkbox
															checked={selectedStocks[stock.symbol]}
															onCheckedChange={() => toggleStockSelection(stock.symbol)}
														/>
													</Table.Cell>
													<Table.Cell class="font-medium text-foreground">{stock.symbol}</Table.Cell>
													<Table.Cell class="text-right">
														<div class="flex items-center justify-end gap-1">
															{#if stock.ret > 0}
																<TrendingUp class="h-3 w-3 text-green-500" />
																<span class="font-medium text-green-600 dark:text-green-400">
																	+{stock.ret.toFixed(2)}%
																</span>
															{:else}
																<TrendingDown class="h-3 w-3 text-red-500" />
																<span class="font-medium text-red-600 dark:text-red-400">
																	{stock.ret.toFixed(2)}%
																</span>
															{/if}
														</div>
													</Table.Cell>
													<Table.Cell class="text-right text-muted-foreground">
														₹{stock.ltp.toFixed(2)}
													</Table.Cell>
													<Table.Cell class="text-right">
														{#if selectedStocks[stock.symbol] && allocation}
															<div class="flex items-center justify-end gap-1">
																<Button
																	variant="ghost"
																	size="sm"
																	class="h-6 w-6 p-0"
																	on:click={() => updateQuantity(stock.symbol, -1)}
																	disabled={(allocation?.quantity || 0) === 0}
																>
																	-
																</Button>
																<Input
																	type="number"
																	value={allocation?.quantity || 0}
																	on:input={(e) => {
																		const newQty = parseInt(e.currentTarget.value) || 0;
																		const currQty = allocation?.quantity || 0;
																		updateQuantity(stock.symbol, newQty - currQty);
																	}}
																	class="h-7 w-16 text-center px-1"
																	min="0"
																/>
																<Button
																	variant="ghost"
																	size="sm"
																	class="h-6 w-6 p-0"
																	on:click={() => updateQuantity(stock.symbol, 1)}
																>
																	+
																</Button>
															</div>
														{:else}
															<span class="text-muted-foreground">-</span>
														{/if}
													</Table.Cell>
													<Table.Cell class="text-right text-muted-foreground">
														₹{(allocation?.allocated_value || 0).toLocaleString('en-IN', {maximumFractionDigits: 0})}
													</Table.Cell>
													<Table.Cell>
														{#if allocation?.status === 'ALLOCATED'}
															<Badge variant="default">Allocated</Badge>
														{:else if allocation?.status === 'IMPOSSIBLE'}
															<Badge variant="destructive">Too Expensive</Badge>
														{:else}
															<Badge variant="secondary">-</Badge>
														{/if}
													</Table.Cell>
												</Table.Row>
												{/key}
											{/each}
										</Table.Body>
									</Table.Root>
								</div>
							</Card.Content>
						</Card.Root>
					</div>
				</div>
			</Tabs.Content>
		</Tabs.Root>

		<!-- Exit Confirmation -->
		<AlertDialog.Root bind:open={showExitConfirmation}>
			<AlertDialog.Content>
				<AlertDialog.Header>
					<AlertDialog.Title>Exit {exitingIndexSymbol || 'Portfolio'}?</AlertDialog.Title>
					<AlertDialog.Description>
						{#if exitingIndexSymbol}
							{@const holdingsToExit = holdingsByIndex[exitingIndexSymbol] || []}
							This will place SELL orders for {holdingsToExit.length} positions in {exitingIndexSymbol} at market price.
						{:else}
							This will place SELL orders for all {activeHoldings.length} positions at market price.
						{/if}
						Are you sure you want to exit?
					</AlertDialog.Description>
				</AlertDialog.Header>
				<AlertDialog.Footer>
					<AlertDialog.Cancel on:click={() => exitingIndexSymbol = null}>Cancel</AlertDialog.Cancel>
					<Button variant="destructive" on:click={() => exitAllPositions(exitingIndexSymbol)}>Confirm Exit</Button>
				</AlertDialog.Footer>
			</AlertDialog.Content>
		</AlertDialog.Root>
		
		<!-- Rebalance Dialog -->
		<AlertDialog.Root bind:open={showRebalanceDialog}>
			<AlertDialog.Content class="max-w-6xl max-h-[85vh] overflow-hidden flex flex-col">
				{#if rebalanceAnalysis}
					{#if rebalanceAnalysis.no_changes}
						<!-- No Changes State -->
						<AlertDialog.Header>
							<div class="flex items-center justify-center mb-4">
								<div class="rounded-full bg-green-100 dark:bg-green-900 p-4">
									<Check class="h-12 w-12 text-green-600 dark:text-green-400" />
								</div>
							</div>
							<AlertDialog.Title class="text-center text-2xl">Portfolio Up to Date</AlertDialog.Title>
							<AlertDialog.Description class="text-center">
								Your {rebalanceAnalysis.linked_index_symbol || 'momentum'} portfolio already holds the top momentum stocks.
								No rebalancing required at this time.
							</AlertDialog.Description>
						</AlertDialog.Header>
						<AlertDialog.Footer class="mt-6">
							<Button variant="default" on:click={() => showRebalanceDialog = false}>Close</Button>
						</AlertDialog.Footer>
					{:else}
						<!-- Changes Needed State -->
						<AlertDialog.Header>
							<AlertDialog.Title class="flex items-center gap-2">
								Rebalance Portfolio
								{#if rebalanceAnalysis.linked_index_symbol}
									<Badge variant="outline">{rebalanceAnalysis.linked_index_symbol}</Badge>
								{/if}
							</AlertDialog.Title>
							<AlertDialog.Description>
								Review the changes below and execute when ready. Only holdings from this index will be affected.
							</AlertDialog.Description>
						</AlertDialog.Header>
						
						<div class="flex-1 overflow-y-auto py-4 space-y-4">
							<!-- Capital Summary -->
							<Card.Root>
								<Card.Content class="pt-4">
									<div class="grid grid-cols-3 gap-4 text-sm">
										<div>
											<p class="text-muted-foreground">Current Value</p>
											<p class="text-lg font-semibold">₹{rebalanceAnalysis.current_value?.toLocaleString('en-IN', {maximumFractionDigits: 0})}</p>
										</div>
										<div>
											<p class="text-muted-foreground">Target Capital</p>
											<p class="text-lg font-semibold">₹{rebalanceAnalysis.target_capital?.toLocaleString('en-IN', {maximumFractionDigits: 0})}</p>
										</div>
										<div>
											<p class="text-muted-foreground">Capital Change</p>
											<p class="text-lg font-semibold {rebalanceAnalysis.capital_change >= 0 ? 'text-green-600' : 'text-red-600'}">
												{rebalanceAnalysis.capital_change >= 0 ? '+' : ''}₹{Math.abs(rebalanceAnalysis.capital_change || 0).toLocaleString('en-IN', {maximumFractionDigits: 0})}
											</p>
										</div>
									</div>
								</Card.Content>
							</Card.Root>
							
							<!-- Actions Summary -->
							<div class="grid grid-cols-3 gap-3 text-center text-sm">
								<div class="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 rounded-md p-3">
									<p class="text-2xl font-bold text-red-600">{rebalanceAnalysis.summary?.exit_count || 0}</p>
									<p class="text-muted-foreground">Exits</p>
								</div>
								<div class="bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-800 rounded-md p-3">
									<p class="text-2xl font-bold text-green-600">{rebalanceAnalysis.summary?.entry_count || 0}</p>
									<p class="text-muted-foreground">New Entries</p>
								</div>
								<div class="bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 rounded-md p-3">
									<p class="text-2xl font-bold text-blue-600">{rebalanceAnalysis.summary?.adjust_count || 0}</p>
									<p class="text-muted-foreground">Adjustments</p>
								</div>
							</div>
							
							<!-- Detailed Actions -->
							<Tabs.Root value="all" class="w-full">
								<Tabs.List class="grid w-full grid-cols-3">
									<Tabs.Trigger value="exits">Exits ({rebalanceAnalysis.exits?.length || 0})</Tabs.Trigger>
									<Tabs.Trigger value="entries">Entries ({rebalanceAnalysis.entries?.length || 0})</Tabs.Trigger>
									<Tabs.Trigger value="adjustments">Holdings ({rebalanceAnalysis.holds_adjust?.length || 0})</Tabs.Trigger>
								</Tabs.List>
								
								<Tabs.Content value="exits" class="mt-4">
									{#if rebalanceAnalysis.exits && rebalanceAnalysis.exits.length > 0}
										<div class="border rounded-md">
											<Table.Root>
												<Table.Header>
													<Table.Row>
														<Table.Head>Symbol</Table.Head>
														<Table.Head class="text-right">Quantity</Table.Head>
														<Table.Head class="text-right">Exit Price</Table.Head>
														<Table.Head class="text-right">Exit Value</Table.Head>
														<Table.Head class="text-right">P&L</Table.Head>
													</Table.Row>
												</Table.Header>
												<Table.Body>
													{#each rebalanceAnalysis.exits as exit}
														<Table.Row>
															<Table.Cell class="font-medium">{exit.symbol}</Table.Cell>
															<Table.Cell class="text-right">{exit.quantity}</Table.Cell>
															<Table.Cell class="text-right">₹{exit.exit_price?.toFixed(2)}</Table.Cell>
															<Table.Cell class="text-right">₹{exit.exit_value?.toLocaleString('en-IN', {maximumFractionDigits: 0})}</Table.Cell>
															<Table.Cell class="text-right">
																<span class={exit.pnl >= 0 ? 'text-green-600' : 'text-red-600'}>
																	{exit.pnl >= 0 ? '+' : ''}₹{Math.abs(exit.pnl || 0).toLocaleString('en-IN', {maximumFractionDigits: 0})}
																</span>
															</Table.Cell>
														</Table.Row>
													{/each}
												</Table.Body>
											</Table.Root>
										</div>
									{:else}
										<p class="text-center text-muted-foreground py-8">No exits required</p>
									{/if}
								</Tabs.Content>
								
								<Tabs.Content value="entries" class="mt-4">
									{#if rebalanceAnalysis.entries && rebalanceAnalysis.entries.length > 0}
										<div class="border rounded-md">
											<Table.Root>
												<Table.Header>
													<Table.Row>
														<Table.Head>Symbol</Table.Head>
														<Table.Head class="text-right">Quantity</Table.Head>
														<Table.Head class="text-right">LTP</Table.Head>
														<Table.Head class="text-right">Value</Table.Head>
														<Table.Head class="text-right">Momentum %</Table.Head>
													</Table.Row>
												</Table.Header>
												<Table.Body>
													{#each rebalanceAnalysis.entries as entry}
														<Table.Row>
															<Table.Cell class="font-medium">{entry.symbol}</Table.Cell>
															<Table.Cell class="text-right">
																<div class="flex items-center justify-end gap-1">
																	<Button
																		variant="ghost"
																		size="sm"
																		class="h-6 w-6 p-0"
																		on:click={() => updateRebalanceEntryQty(entry.symbol, -1)}
																		disabled={entry.quantity <= 0}
																	>
																		<Minus class="h-3 w-3" />
																	</Button>
																	<span class="w-10 text-center font-medium">{entry.quantity}</span>
																	<Button
																		variant="ghost"
																		size="sm"
																		class="h-6 w-6 p-0"
																		on:click={() => updateRebalanceEntryQty(entry.symbol, 1)}
																	>
																		<Plus class="h-3 w-3" />
																	</Button>
																</div>
															</Table.Cell>
															<Table.Cell class="text-right">₹{entry.ltp?.toFixed(2)}</Table.Cell>
															<Table.Cell class="text-right">₹{entry.allocated_value?.toLocaleString('en-IN', {maximumFractionDigits: 0})}</Table.Cell>
															<Table.Cell class="text-right text-green-600">+{entry.momentum_return?.toFixed(2)}%</Table.Cell>
														</Table.Row>
													{/each}
												</Table.Body>
											</Table.Root>
										</div>
									{:else}
										<p class="text-center text-muted-foreground py-8">No new entries</p>
									{/if}
								</Tabs.Content>
								
								<Tabs.Content value="adjustments" class="mt-4">
									{#if rebalanceAnalysis.holds_adjust && rebalanceAnalysis.holds_adjust.length > 0}
										<div class="border rounded-md">
											<Table.Root>
												<Table.Header>
													<Table.Row>
														<Table.Head>Symbol</Table.Head>
														<Table.Head class="text-right">Current</Table.Head>
														<Table.Head class="text-center">Adjust</Table.Head>
														<Table.Head class="text-right">New Qty</Table.Head>
														<Table.Head>Action</Table.Head>
													</Table.Row>
												</Table.Header>
												<Table.Body>
													{#each rebalanceAnalysis.holds_adjust as hold}
														<Table.Row class={hold.action !== 'HOLD' ? 'bg-muted/30' : ''}>
															<Table.Cell class="font-medium">{hold.symbol}</Table.Cell>
															<Table.Cell class="text-right text-muted-foreground">{hold.current_quantity}</Table.Cell>
															<Table.Cell>
																<div class="flex items-center justify-center gap-1">
																	<Button
																		variant="ghost"
																		size="sm"
																		class="h-6 w-6 p-0"
																		on:click={() => updateRebalanceHoldQty(hold.symbol, -1)}
																		disabled={(hold.new_quantity || hold.current_quantity) <= 0}
																	>
																		<Minus class="h-3 w-3" />
																	</Button>
																	<span class="w-10 text-center font-medium {hold.adjustment_quantity > 0 ? 'text-green-600' : hold.adjustment_quantity < 0 ? 'text-red-600' : ''}">
																		{hold.adjustment_quantity > 0 ? '+' : ''}{hold.adjustment_quantity || 0}
																	</span>
																	<Button
																		variant="ghost"
																		size="sm"
																		class="h-6 w-6 p-0"
																		on:click={() => updateRebalanceHoldQty(hold.symbol, 1)}
																	>
																		<Plus class="h-3 w-3" />
																	</Button>
																</div>
															</Table.Cell>
															<Table.Cell class="text-right font-medium">{hold.new_quantity || hold.current_quantity}</Table.Cell>
															<Table.Cell>
																{#if hold.action === 'BUY_MORE'}
																	<Badge variant="default">Buy</Badge>
																{:else if hold.action === 'SELL_PARTIAL'}
																	<Badge variant="destructive">Sell</Badge>
																{:else}
																	<Badge variant="outline">Hold</Badge>
																{/if}
															</Table.Cell>
														</Table.Row>
													{/each}
												</Table.Body>
											</Table.Root>
										</div>
									{:else}
										<p class="text-center text-muted-foreground py-8">No holdings to adjust</p>
									{/if}
								</Tabs.Content>
							</Tabs.Root>
							
							<!-- Summary Info -->
							<Card.Root>
								<Card.Content class="pt-4">
									<div class="grid grid-cols-2 gap-4 text-sm">
										<div>
											<p class="text-muted-foreground">Total Buy Value</p>
											<p class="font-semibold text-green-600">₹{rebalanceAnalysis.summary?.total_buy_value?.toLocaleString('en-IN', {maximumFractionDigits: 0})}</p>
										</div>
										<div>
											<p class="text-muted-foreground">Total Sell Value</p>
											<p class="font-semibold text-red-600">₹{rebalanceAnalysis.summary?.total_sell_value?.toLocaleString('en-IN', {maximumFractionDigits: 0})}</p>
										</div>
									</div>
									<Separator class="my-3" />
									<div>
										<p class="text-muted-foreground text-sm">Net Cash Required</p>
										<p class="text-lg font-bold {rebalanceAnalysis.summary?.net_cash_required >= 0 ? 'text-red-600' : 'text-green-600'}">
											{rebalanceAnalysis.summary?.net_cash_required >= 0 ? '+' : ''}₹{Math.abs(rebalanceAnalysis.summary?.net_cash_required || 0).toLocaleString('en-IN', {maximumFractionDigits: 0})}
										</p>
									</div>
								</Card.Content>
							</Card.Root>
							
							<!-- AMO Toggle -->
							<div class="flex items-center justify-between p-3 bg-muted/20 rounded-md">
								<div class="flex flex-col gap-1">
									<Label for="amo-rebalance" class="text-sm font-medium cursor-pointer">
										After Market Order (AMO)
									</Label>
									<span class="text-xs text-muted-foreground">
										{useAMO ? 'Orders will be placed after market hours' : 'Regular market orders'}
									</span>
								</div>
								<Checkbox
									id="amo-rebalance"
									checked={useAMO}
									onCheckedChange={(checked) => useAMO = checked}
								/>
							</div>
						</div>
						
						<AlertDialog.Footer class="mt-4">
							<AlertDialog.Cancel>Cancel</AlertDialog.Cancel>
							<Button 
								variant="default" 
								on:click={executeRebalance}
								disabled={executingRebalance}
							>
								{#if executingRebalance}
									<Loader2 class="mr-2 h-4 w-4 animate-spin" />
									Executing...
								{:else}
									Execute Rebalance {useAMO ? '(AMO)' : ''}
								{/if}
							</Button>
						</AlertDialog.Footer>
					{/if}
				{/if}
			</AlertDialog.Content>
		</AlertDialog.Root>
		
		<!-- Capital Adjustment Dialog -->
		<AlertDialog.Root bind:open={showCapitalAdjustment}>
			<AlertDialog.Content class="max-w-lg">
				<AlertDialog.Header>
					<AlertDialog.Title>Adjust Portfolio Capital</AlertDialog.Title>
					<AlertDialog.Description>
						Allocate additional capital or reduce your strategy allocation.
					</AlertDialog.Description>
				</AlertDialog.Header>
				<div class="py-4 space-y-4">
					<!-- Adjustment Type Selection -->
					<div class="grid grid-cols-2 gap-2">
						<Button
							variant={capitalAdjustmentType === 'increase' ? 'default' : 'outline'}
							class="w-full"
							on:click={() => capitalAdjustmentType = 'increase'}
						>
							<TrendingUp class="mr-2 h-4 w-4" />
							Increase Capital
						</Button>
						<Button
							variant={capitalAdjustmentType === 'decrease' ? 'default' : 'outline'}
							class="w-full"
							on:click={() => capitalAdjustmentType = 'decrease'}
						>
							<TrendingDown class="mr-2 h-4 w-4" />
							Decrease Capital
						</Button>
					</div>
					
					<!-- Current Portfolio Info -->
					{#if activeHoldings.length > 0}
						{@const currentValue = activeHoldings.reduce((sum, h) => sum + (h.quantity * h.last_price), 0)}
						<Card.Root>
						<Card.Content class="pt-4">
							<div class="grid grid-cols-2 gap-4 text-sm">
								<div>
									<p class="text-muted-foreground">Current Portfolio Value</p>
									<p class="text-lg font-semibold">₹{currentValue.toLocaleString('en-IN', {maximumFractionDigits: 0})}</p>
								</div>
								<div>
									<p class="text-muted-foreground">Available Margin</p>
									<p class="text-lg font-semibold">₹{investableMargin.toLocaleString('en-IN', {maximumFractionDigits: 0})}</p>
								</div>
							</div>
						</Card.Content>
						</Card.Root>
					{/if}
					
					<!-- Percentage Input and Preview -->
					{#if activeHoldings.length > 0}
						{@const currentValue = activeHoldings.reduce((sum, h) => sum + (h.quantity * h.last_price), 0)}
						
						<div class="space-y-2">
							{#if capitalAdjustmentType === 'increase'}
								<Label for="allocation-percentage">
									Allocate Additional % from Available Margin
								</Label>
								<div class="flex items-center gap-2">
									<Input
										id="allocation-percentage"
										type="number"
										bind:value={capitalAdjustmentPercentage}
										placeholder="e.g., 20 for 20%"
										min="0"
										max="100"
										step="5"
										class="flex-1"
									/>
									<span class="text-muted-foreground">%</span>
								</div>
								<p class="text-xs text-muted-foreground">
									Example: 20% of ₹{investableMargin.toLocaleString('en-IN', {maximumFractionDigits: 0})} = ₹{((investableMargin * 20) / 100).toLocaleString('en-IN', {maximumFractionDigits: 0})}
								</p>
							{:else}
								<Label for="allocation-percentage">
									Reduce to % of Current Portfolio
								</Label>
								<div class="flex items-center gap-2">
									<Input
										id="allocation-percentage"
										type="number"
										bind:value={capitalAdjustmentPercentage}
										placeholder="e.g., 80 to keep 80%"
										min="1"
										max="99"
										step="5"
										class="flex-1"
									/>
									<span class="text-muted-foreground">%</span>
								</div>
								<p class="text-xs text-muted-foreground">
									Example: 80% of ₹{currentValue.toLocaleString('en-IN', {maximumFractionDigits: 0})} = ₹{((currentValue * 80) / 100).toLocaleString('en-IN', {maximumFractionDigits: 0})} (Exit ₹{((currentValue * 20) / 100).toLocaleString('en-IN', {maximumFractionDigits: 0})})
								</p>
							{/if}
						</div>
						
						<!-- Preview -->
						{#if capitalAdjustmentPercentage > 0}
							{@const additionalCapital = capitalAdjustmentType === 'increase' 
								? (investableMargin * capitalAdjustmentPercentage) / 100 
								: 0}
							{@const targetCapital = capitalAdjustmentType === 'increase'
								? currentValue + additionalCapital
								: (currentValue * capitalAdjustmentPercentage) / 100}
							{@const change = targetCapital - currentValue}
							
							<Alert.Root variant={capitalAdjustmentType === 'increase' ? 'default' : 'destructive'}>
								<Alert.Description>
									<div class="space-y-1 text-sm">
										<p><strong>Current Portfolio:</strong> ₹{currentValue.toLocaleString('en-IN', {maximumFractionDigits: 0})}</p>
										{#if capitalAdjustmentType === 'increase'}
											<p><strong>Additional Capital:</strong> +₹{additionalCapital.toLocaleString('en-IN', {maximumFractionDigits: 0})}</p>
										{/if}
										<p><strong>New Portfolio Value:</strong> ₹{targetCapital.toLocaleString('en-IN', {maximumFractionDigits: 0})}</p>
										<Separator class="my-2" />
										<p class={change >= 0 ? 'text-green-600 font-semibold' : 'text-red-600 font-semibold'}>
											<strong>Net Change:</strong> {change >= 0 ? '+' : ''}₹{Math.abs(change).toLocaleString('en-IN', {maximumFractionDigits: 0})}
											({((change / currentValue) * 100).toFixed(1)}%)
										</p>
									</div>
								</Alert.Description>
							</Alert.Root>
						{/if}
					{/if}
				</div>
				<AlertDialog.Footer>
					<AlertDialog.Cancel>Cancel</AlertDialog.Cancel>
					<Button 
						variant="default" 
						on:click={applyCapitalAdjustment}
						disabled={capitalAdjustmentPercentage <= 0}
					>
						Analyze Rebalance
					</Button>
				</AlertDialog.Footer>
			</AlertDialog.Content>
		</AlertDialog.Root>
	{/if}
	
	<!-- Toast Notification -->
	{#if showToast}
		<div class="fixed bottom-4 right-4 z-50 animate-in slide-in-from-bottom-5 duration-300">
			<Alert.Root variant={toastType === 'error' ? 'destructive' : 'default'} class="w-96 shadow-lg">
				<div class="flex items-start gap-3">
					{#if toastType === 'success'}
						<Check class="h-5 w-5 text-green-600" />
					{:else if toastType === 'error'}
						<X class="h-5 w-5" />
					{:else}
						<AlertCircle class="h-5 w-5" />
					{/if}
					<div class="flex-1">
						<Alert.Description class="text-sm">{toastMessage}</Alert.Description>
					</div>
					<button
						on:click={() => showToast = false}
						class="text-muted-foreground hover:text-foreground transition-colors"
					>
						<X class="h-4 w-4" />
					</button>
				</div>
			</Alert.Root>
		</div>
	{/if}
</div>
