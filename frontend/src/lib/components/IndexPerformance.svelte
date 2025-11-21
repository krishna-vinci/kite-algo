<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { apiFetch, getApiBase } from '$lib/api';
  import type { Instrument } from '$lib/types';
  import { Button } from "$lib/components/ui/button";
  import * as Dialog from "$lib/components/ui/dialog";
  import { Checkbox } from "$lib/components/ui/checkbox";
  import EChart from "$lib/components/charts/EChart.svelte";
  import { RefreshCw, TrendingUp, TrendingDown, ArrowUp, ArrowDown, BarChart2 } from '@lucide/svelte';

  let performanceData: Record<string, Record<string, any>> = $state({});
  let loading = $state(true);
  let selectedIndices: string[] = $state([]);
  let searchTerm = $state('');
  let searchResults: Instrument[] = $state([]);
  
  // Sorting state
  let sortColumn: string | null = $state(null);
  let sortDirection: 'asc' | 'desc' = $state('asc');

  // Selection for comparison
  let compareSelection: string[] = $state([]);

  // Modal states
  let showHistoryModal = $state(false);
  let showCompareModal = $state(false);
  let selectedHistoryIndex: string | null = $state(null);
  let historyData: any[] = $state([]);
  let compareData: { title: string; data: { time: number; value: number }[] }[] = $state([]);
  let modalLoading = $state(false);
  let refreshInterval: ReturnType<typeof setInterval>;

  const CloseIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>`;

  const defaultIndices = [
    "NIFTY 50",
    "NIFTY NEXT 50",
    "NIFTY MIDCAP 150",
    "NIFTY SMLCAP 250",
    "NIFTY MICROCAP250"
  ];

  onMount(() => {
    const savedIndices = localStorage.getItem('performanceIndices');
    selectedIndices = savedIndices ? JSON.parse(savedIndices) : defaultIndices;
    loadPerformanceData();
    
    // Setup interval to refresh 1D data during market hours
    startRefreshInterval();
  });

  onDestroy(() => {
    if (refreshInterval) clearInterval(refreshInterval);
  });

  // Helper to get IST time components
  // Returns a Date object where getUTC...() methods return IST values
  function getISTTime(date: Date) {
    const utc = date.getTime();
    const istOffset = 5.5 * 60 * 60 * 1000; // IST is UTC + 5:30
    return new Date(utc + istOffset);
  }

  function startRefreshInterval() {
    if (refreshInterval) clearInterval(refreshInterval);
    
    refreshInterval = setInterval(() => {
        const ist = getISTTime(new Date());
        const hour = ist.getUTCHours();
        const minute = ist.getUTCMinutes();
        const day = ist.getUTCDay();

        // Market hours: 09:15 - 15:30, Mon-Fri (1-5)
        const isMarketHours = day >= 1 && day <= 5 && 
            ((hour > 9 || (hour === 9 && minute >= 15)) && (hour < 15 || (hour === 15 && minute <= 30)));

        if (isMarketHours) {
             loadPerformanceData(false, true); // background refresh
        }
    }, 60000); // Every minute
  }

  async function loadPerformanceData(forceRefresh = false, isBackground = false) {
    if (!isBackground) loading = true;
    
    try {
      const response = await apiFetch('/broker/performance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ indices: selectedIndices })
      });
      
      if (response.ok) {
        performanceData = await response.json();
      } else {
        console.error("Error fetching performance data:", response.statusText);
      }
    } catch (error) {
      console.error("Error fetching performance data:", error);
    } finally {
      if (!isBackground) loading = false;
    }
  }

  // Alias for backward compatibility if needed, or just use loadPerformanceData
  const fetchPerformanceData = () => loadPerformanceData(true);

  async function fetchSearchResults(query: string) {
    if (query.length < 2) {
      searchResults = [];
      return;
    }
    try {
      const url = `${getApiBase()}/broker/instruments/fuzzy-search?query=${encodeURIComponent(query)}`;
      const response = await fetch(url, { credentials: 'include' });
      if (response.ok) {
        searchResults = (await response.json()).filter(
          (instrument: Instrument) => instrument.tradingsymbol && !selectedIndices.includes(instrument.tradingsymbol)
        );
      } else {
        searchResults = [];
      }
    } catch (error) {
      searchResults = [];
    }
  }

  let searchTimeout: ReturnType<typeof setTimeout>;
  $effect(() => {
    clearTimeout(searchTimeout);
    if (searchTerm) {
      searchTimeout = setTimeout(() => fetchSearchResults(searchTerm), 300);
    } else {
      searchResults = [];
    }
  });

  function addIndex(index: Instrument) {
    if (index.tradingsymbol && !selectedIndices.includes(index.tradingsymbol)) {
      selectedIndices = [...selectedIndices, index.tradingsymbol];
      localStorage.setItem('performanceIndices', JSON.stringify(selectedIndices));
      loadPerformanceData(true); // Force refresh on list change
      searchTerm = '';
    }
  }

  function removeIndex(indexToRemove: string) {
    selectedIndices = selectedIndices.filter(index => index !== indexToRemove);
    localStorage.setItem('performanceIndices', JSON.stringify(selectedIndices));
    loadPerformanceData(true); // Force refresh on list change
  }

  const getCellClass = (value: string) => {
    if (!value || value === 'N/A' || value === 'Data not available' || value === 'Error') return 'bg-gray-200 text-gray-500';
    const numericValue = parseFloat(value);
    if (numericValue > 2) return 'bg-green-500 text-white';
    if (numericValue > 0) return 'bg-green-200 text-green-800';
    if (numericValue < -2) return 'bg-red-500 text-white';
    if (numericValue < 0) return 'bg-red-200 text-red-800';
    return 'bg-gray-100';
  };

  // Sorting Logic
  function toggleSort(column: string) {
    if (sortColumn === column) {
      sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
    } else {
      sortColumn = column;
      sortDirection = 'desc'; // Default to desc for performance (highest first)
    }
  }

  let sortedIndices = $derived([...selectedIndices].sort((a, b) => {
    if (!sortColumn) return 0;
    const valA = performanceData[a]?.[sortColumn];
    const valB = performanceData[b]?.[sortColumn];
    
    const parse = (v: string) => {
        if (!v || v === 'N/A' || v === 'Data not available' || v === 'Error') return -999999;
        return parseFloat(v.replace('%', ''));
    };

    const numA = parse(valA);
    const numB = parse(valB);

    return sortDirection === 'asc' ? numA - numB : numB - numA;
  }));

  // Sparkline Rendering
  function renderSparkline(data: number[]) {
    if (!data || data.length < 2) return '';
    const width = 50;
    const height = 20;
    const min = Math.min(...data);
    const max = Math.max(...data);
    const range = max - min || 1;
    
    const points = data.map((val, i) => {
      const x = (i / (data.length - 1)) * width;
      const y = height - ((val - min) / range) * height;
      return `${x},${y}`;
    }).join(' ');

    const color = data[data.length - 1] >= data[0] ? 'green' : 'red';
    return `<svg width="${width}" height="${height}" class="overflow-visible"><polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5" /></svg>`;
  }

  // Historical View
  let historyOptions: any = $state(null);
  let chartCache: Record<string, any> = {};

  async function openHistory(index: string, period: string = '1Y') {
    selectedHistoryIndex = index;
    showHistoryModal = true;
    
    // Check in-memory cache first
    const cacheKey = `${index}-${period}`;
    if (chartCache[cacheKey]) {
        historyOptions = chartCache[cacheKey];
        modalLoading = false;
        return;
    }

    modalLoading = true;
    historyOptions = null;

    let fromDate: string | undefined;
    const now = new Date();
    
    if (period === '1W') {
        const d = new Date(now);
        d.setDate(d.getDate() - 10); // ~10 calendar days
        fromDate = d.toISOString().split('T')[0];
    } else if (period === '1M') {
        const d = new Date(now);
        d.setDate(d.getDate() - 40); // ~40 calendar days
        fromDate = d.toISOString().split('T')[0];
    }
    // For 3M, 6M, 1Y we use default (full year or as handled by backend default if param omitted)
    
    let url = `/broker/candles/NSE:${encodeURIComponent(index)}?timeframe=day&ingest=false&passthrough=true`;
    if (fromDate) {
        url += `&from=${fromDate}`;
    }

    try {
        const response = await apiFetch(url);
        if (response.ok) {
            const data = await response.json();
            if (!data.candles || data.candles.length === 0) {
                 historyData = [];
                 return;
            }
            
            const dates = data.candles.map((c: any) => new Date(c.time * 1000).toLocaleDateString());
            const values = data.candles.map((c: any) => [c.open, c.close, c.low, c.high]);
            const volumes = data.candles.map((c: any, i: number) => [i, c.volume, c.open > c.close ? 1 : -1]);

            const options = {
                tooltip: {
                    trigger: 'axis',
                    axisPointer: { type: 'cross' }
                },
                axisPointer: { link: [{ xAxisIndex: 'all' }] },
                grid: [
                    { left: '10%', right: '8%', height: '50%' },
                    { left: '10%', right: '8%', top: '63%', height: '16%' }
                ],
                xAxis: [
                    {
                        type: 'category',
                        data: dates,
                        scale: true,
                        boundaryGap: false,
                        axisLine: { onZero: false },
                        splitLine: { show: false },
                        splitNumber: 20,
                        min: 'dataMin',
                        max: 'dataMax'
                    },
                    {
                        type: 'category',
                        gridIndex: 1,
                        data: dates,
                        scale: true,
                        boundaryGap: false,
                        axisLine: { onZero: false },
                        axisTick: { show: false },
                        splitLine: { show: false },
                        axisLabel: { show: false },
                        min: 'dataMin',
                        max: 'dataMax'
                    }
                ],
                yAxis: [
                    { scale: true, splitArea: { show: true } },
                    { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } }
                ],
                dataZoom: [
                    { type: 'inside', xAxisIndex: [0, 1], start: 50, end: 100 },
                    { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: 10, start: 50, end: 100 }
                ],
                series: [
                    {
                        name: index,
                        type: 'candlestick',
                        data: values,
                        itemStyle: {
                            color: '#0CF49B',
                            color0: '#FD1050',
                            borderColor: '#0CF49B',
                            borderColor0: '#FD1050'
                        }
                    },
                    {
                        name: 'Volume',
                        type: 'bar',
                        xAxisIndex: 1,
                        yAxisIndex: 1,
                        data: volumes,
                        itemStyle: {
                            color: (params: any) => {
                                return params.value[2] === 1 ? '#FD1050' : '#0CF49B';
                            }
                        }
                    }
                ]
            };
            historyOptions = options;
            chartCache[cacheKey] = options;
        }
    } catch (e) {
        console.error("Failed to fetch history", e);
    } finally {
        modalLoading = false;
    }
  }

  // Comparison
  let compareOptions: any = $state(null);

  function toggleComparison(index: string) {
    if (compareSelection.includes(index)) {
        compareSelection = compareSelection.filter(i => i !== index);
    } else {
        compareSelection = [...compareSelection, index];
    }
  }

  async function openComparison() {
    if (compareSelection.length < 2) return;
    showCompareModal = true;
    modalLoading = true;
    compareOptions = null;
    
    try {
        const colors = ['#2962FF', '#E91E63', '#4CAF50', '#FF9800', '#9C27B0', '#00BCD4'];
        const seriesList: any[] = [];
        let allDates: string[] = [];

        // Fetch data for all selected indices
        const promises = compareSelection.map(async (idx, i) => {
             const response = await apiFetch(`/broker/candles/NSE:${encodeURIComponent(idx)}?timeframe=day&ingest=false&passthrough=true`);
             if (response.ok) {
                 const json = await response.json();
                 const candles = json.candles;
                 if (!candles || candles.length === 0) return null;
                 
                 const startPrice = candles[0].close;
                 const data = candles.map((c: any) => ((c.close - startPrice) / startPrice) * 100);
                 
                 if (i === 0) { // Use dates from first series (assuming alignment)
                     allDates = candles.map((c: any) => new Date(c.time * 1000).toLocaleDateString());
                 }

                 return {
                     name: idx,
                     type: 'line',
                     data: data,
                     showSymbol: false,
                     itemStyle: { color: colors[i % colors.length] }
                 };
             }
             return null;
        });
        
        const results = await Promise.all(promises);
        const validSeries = results.filter(r => r !== null);

        if (validSeries.length > 0) {
            compareOptions = {
                tooltip: {
                    trigger: 'axis',
                    formatter: (params: any) => {
                        let res = params[0].name + '<br/>';
                        params.forEach((item: any) => {
                            res += `<span style="display:inline-block;margin-right:5px;border-radius:10px;width:9px;height:9px;background-color:${item.color}"></span>`;
                            res += `${item.seriesName}: ${item.value.toFixed(2)}%<br/>`;
                        });
                        return res;
                    }
                },
                legend: { data: compareSelection },
                grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
                xAxis: { type: 'category', boundaryGap: false, data: allDates },
                yAxis: { type: 'value', axisLabel: { formatter: '{value} %' } },
                series: validSeries
            };
        }
        
    } catch (e) {
        console.error("Comparison error", e);
    } finally {
        modalLoading = false;
    }
  }
</script>

<div class="container mx-auto p-4 space-y-4">
  <div class="flex justify-between items-center">
    <div class="flex items-center gap-2">
      <h1 class="text-2xl font-bold">Market Performance</h1>
      <Button variant="outline" size="icon" on:click={() => loadPerformanceData(true)} disabled={loading} title="Refresh Data">
        <RefreshCw class="h-4 w-4 {loading ? 'animate-spin' : ''}" />
      </Button>
      <Button 
        variant="secondary" 
        size="sm" 
        disabled={compareSelection.length < 2}
        on:click={openComparison}
      >
        <BarChart2 class="h-4 w-4 mr-2" />
        Compare Selected
      </Button>
    </div>

    <div class="relative w-64">
      <input 
        type="text" 
        placeholder="Search to add index..." 
        class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50" 
        bind:value={searchTerm} 
      />
      {#if searchResults.length > 0}
        <ul class="absolute z-20 bg-popover text-popover-foreground border rounded-md mt-1 w-full shadow-md max-h-60 overflow-auto">
          {#each searchResults as instrument (instrument.instrument_token)}
            <li on:click={() => addIndex(instrument)} class="p-2 hover:bg-muted cursor-pointer text-sm">
              {instrument.tradingsymbol}
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  </div>

  <div class="rounded-md border">
    <table class="w-full caption-bottom text-sm">
      <thead class="[&_tr]:border-b">
        <tr class="border-b transition-colors hover:bg-muted/50 data-[state=selected]:bg-muted">
          <th class="h-12 px-4 text-left align-middle font-medium text-muted-foreground w-[50px]">
             <!-- Checkbox header could go here if we implemented select all -->
          </th>
          <th class="h-12 px-4 text-left align-middle font-medium text-muted-foreground">Index</th>
          {#each ["1D", "1W", "1M", "3M", "6M", "1Y"] as period}
             <th 
               class="h-12 px-4 text-center align-middle font-medium text-muted-foreground cursor-pointer hover:text-foreground select-none"
               on:click={() => toggleSort(period)}
             >
               <div class="flex items-center justify-center gap-1">
                 {period}
                 {#if sortColumn === period}
                   {#if sortDirection === 'asc'}
                     <ArrowUp class="h-3 w-3" />
                   {:else}
                     <ArrowDown class="h-3 w-3" />
                   {/if}
                 {/if}
               </div>
             </th>
          {/each}
          <th class="h-12 px-4 text-center align-middle font-medium text-muted-foreground w-[50px]"></th>
        </tr>
      </thead>
      <tbody class="[&_tr:last-child]:border-0">
        {#if loading && selectedIndices.length === 0}
           <tr>
             <td colspan="9" class="h-24 text-center">Loading...</td>
           </tr>
        {:else}
            {#each sortedIndices as index (index)}
              <tr class="border-b transition-colors hover:bg-muted/50 data-[state=selected]:bg-muted group">
                <td class="p-4 align-middle">
                    <Checkbox 
                        checked={compareSelection.includes(index)} 
                        onCheckedChange={() => toggleComparison(index)} 
                    />
                </td>
                <td class="p-4 align-middle font-medium">
                    <div class="flex items-center gap-2">
                        <span>{index}</span>
                        {#if performanceData[index]?.sparkline}
                            {@html renderSparkline(performanceData[index].sparkline)}
                        {/if}
                    </div>
                </td>
                {#if performanceData[index]}
                  {#each ["1D", "1W", "1M", "3M", "6M", "1Y"] as period}
                    <!-- svelte-ignore a11y-click-events-have-key-events -->
                    <td 
                        class="p-4 align-middle text-center cursor-pointer hover:opacity-80 transition-opacity {getCellClass(performanceData[index][period])}"
                        on:click={() => openHistory(index, period)}
                    >
                      {performanceData[index][period] || 'N/A'}
                    </td>
                  {/each}
                {:else}
                  <td colspan="7" class="p-4 text-center">Loading...</td>
                {/if}
                <td class="p-4 align-middle text-center">
                  <button on:click={() => removeIndex(index)} class="text-muted-foreground hover:text-destructive invisible group-hover:visible transition-colors">
                    {@html CloseIcon}
                  </button>
                </td>
              </tr>
            {/each}
        {/if}
      </tbody>
    </table>
  </div>

    <!-- Historical View Modal -->
    <Dialog.Root bind:open={showHistoryModal}>
        <Dialog.Content class="sm:max-w-[800px]">
            <Dialog.Header>
                <Dialog.Title>Historical Performance: {selectedHistoryIndex}</Dialog.Title>
                <Dialog.Description>Daily price movement for the last year</Dialog.Description>
            </Dialog.Header>
            <div class="h-[500px] w-full">
                {#if modalLoading}
                    <div class="flex items-center justify-center h-full">Loading chart...</div>
                {:else if historyOptions}
                     <EChart options={historyOptions} />
                {:else}
                     <div class="flex items-center justify-center h-full">No data available</div>
                {/if}
            </div>
        </Dialog.Content>
    </Dialog.Root>

    <!-- Comparison Modal -->
    <Dialog.Root bind:open={showCompareModal}>
        <Dialog.Content class="sm:max-w-[900px]">
            <Dialog.Header>
                <Dialog.Title>Performance Comparison</Dialog.Title>
                <Dialog.Description>Comparing normalized returns (%)</Dialog.Description>
            </Dialog.Header>
            <div class="h-[500px] w-full">
                 {#if modalLoading}
                    <div class="flex items-center justify-center h-full">Loading comparison...</div>
                 {:else if compareOptions}
                      <EChart options={compareOptions} />
                 {:else}
                    <div class="flex items-center justify-center h-full">No data available</div>
                 {/if}
            </div>
        </Dialog.Content>
    </Dialog.Root>
</div>
