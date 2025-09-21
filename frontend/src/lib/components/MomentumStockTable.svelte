<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	export let stocks: any[] = [];
	export let selectedStocks: { [key: string]: boolean };
	export let calculatedShares: { [key: string]: number };

	const dispatch = createEventDispatcher();

	function handleCheckboxChange() {
		dispatch('select', selectedStocks);
	}
</script>

<div class="overflow-x-auto">
	<table class="min-w-full divide-y divide-gray-200">
		<thead class="bg-gray-50">
			<tr>
				<th
					scope="col"
					class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider"
				>
					Select
				</th>
				<th
					scope="col"
					class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider"
				>
					Symbol
				</th>
				<th
					scope="col"
					class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider"
				>
					252-Day Return (%)
				</th>
				<th
					scope="col"
					class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider"
				>
					LTP (₹)
				</th>
				<th
					scope="col"
					class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider"
				>
					Shares
				</th>
			</tr>
		</thead>
		<tbody class="bg-white divide-y divide-gray-200">
			{#each stocks as stock (stock.symbol)}
				<tr
					class={calculatedShares[stock.symbol] === 0 && selectedStocks[stock.symbol]
						? 'bg-red-50'
						: ''}
				>
					<td class="px-6 py-4 whitespace-nowrap">
						<input
							type="checkbox"
							bind:checked={selectedStocks[stock.symbol]}
							on:change={handleCheckboxChange}
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
