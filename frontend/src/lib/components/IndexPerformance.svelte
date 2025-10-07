<script lang="ts">
  import { onMount } from 'svelte';
  import { apiFetch, getApiBase } from '$lib/api';
  import type { Instrument } from '$lib/types';

  let performanceData: Record<string, Record<string, string>> = {};
  let loading = true;
  let selectedIndices: string[] = [];
  let searchTerm = '';
  let searchResults: Instrument[] = [];

  const CloseIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>`;

  const defaultIndices = [
    "NIFTY 50",
    "NIFTY NEXT 50",
    "NIFTY MIDCAP 150",
    "NIFTY SMALLCAP 250",
    "NIFTY MICROCAP 250"
  ];

  onMount(() => {
    const savedIndices = localStorage.getItem('performanceIndices');
    selectedIndices = savedIndices ? JSON.parse(savedIndices) : defaultIndices;
    fetchPerformanceData();
  });

  async function fetchPerformanceData() {
    loading = true;
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
      loading = false;
    }
  }

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
          (instrument: Instrument) => !selectedIndices.includes(instrument.tradingsymbol)
        );
      } else {
        searchResults = [];
      }
    } catch (error) {
      searchResults = [];
    }
  }

  let searchTimeout: ReturnType<typeof setTimeout>;
  $: {
    clearTimeout(searchTimeout);
    if (searchTerm) {
      searchTimeout = setTimeout(() => fetchSearchResults(searchTerm), 300);
    } else {
      searchResults = [];
    }
  }

  function addIndex(index: Instrument) {
    if (!selectedIndices.includes(index.tradingsymbol)) {
      selectedIndices = [...selectedIndices, index.tradingsymbol];
      localStorage.setItem('performanceIndices', JSON.stringify(selectedIndices));
      fetchPerformanceData();
      searchTerm = '';
    }
  }

  function removeIndex(indexToRemove: string) {
    selectedIndices = selectedIndices.filter(index => index !== indexToRemove);
    localStorage.setItem('performanceIndices', JSON.stringify(selectedIndices));
    fetchPerformanceData();
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
</script>

<div class="container mx-auto p-4">
  <div class="flex justify-between items-center mb-4">
    <h1 class="text-2xl font-bold">Headline</h1>
    <div class="relative">
      <input type="text" placeholder="Search to add index..." class="border rounded p-2" bind:value={searchTerm} />
      {#if searchResults.length > 0}
        <ul class="absolute z-10 bg-white border rounded mt-1 w-full">
          {#each searchResults as instrument (instrument.instrument_token)}
            <li on:click={() => addIndex(instrument)} class="p-2 hover:bg-gray-200 cursor-pointer">
              {instrument.tradingsymbol}
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  </div>
  {#if loading}
    <p>Loading...</p>
  {:else}
    <table class="min-w-full bg-white border">
      <thead class="bg-gray-100">
        <tr>
          <th class="py-2 px-4 border-b text-left"></th>
          <th class="py-2 px-4 border-b">1D</th>
          <th class="py-2 px-4 border-b">1W</th>
          <th class="py-2 px-4 border-b">1M</th>
          <th class="py-2 px-4 border-b">3M</th>
          <th class="py-2 px-4 border-b">6M</th>
          <th class="py-2 px-4 border-b">1Y</th>
          <th class="py-2 px-4 border-b"></th>
        </tr>
      </thead>
      <tbody>
        {#each selectedIndices as index (index)}
          <tr class="border-b group">
            <td class="py-2 px-4 font-semibold">{index}</td>
            {#if performanceData[index]}
              {#each ["1D", "1W", "1M", "3M", "6M", "1Y"] as period}
                <td class="py-2 px-4 text-center {getCellClass(performanceData[index][period])}">
                  {performanceData[index][period] || 'N/A'}
                </td>
              {/each}
            {:else}
              <td colspan="6" class="py-2 px-4 text-center">Loading...</td>
            {/if}
            <td class="py-2 px-4 text-center">
              <button on:click={() => removeIndex(index)} class="text-gray-400 hover:text-red-500 invisible group-hover:visible">
                {@html CloseIcon}
              </button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>