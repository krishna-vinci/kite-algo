<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import * as Table from '$lib/components/ui/table';
	import { Checkbox } from '$lib/components/ui/checkbox';
	import { Badge } from '$lib/components/ui/badge';

	export let stocks: any[] = [];
	export let selectedStocks: { [key: string]: boolean };
	export let calculatedShares: { [key: string]: number };

	const dispatch = createEventDispatcher();

	function handleCheckboxChange(symbol: string, checked: boolean) {
		selectedStocks[symbol] = checked;
		dispatch('select', selectedStocks);
	}
</script>

<div class="rounded-md border">
	<Table.Root>
		<Table.Header>
			<Table.Row>
				<Table.Head class="w-[50px]">Select</Table.Head>
				<Table.Head>Symbol</Table.Head>
				<Table.Head>252-Day Return (%)</Table.Head>
				<Table.Head>LTP (₹)</Table.Head>
				<Table.Head>Shares</Table.Head>
			</Table.Row>
		</Table.Header>
		<Table.Body>
			{#each stocks as stock (stock.symbol)}
				<Table.Row
					class={calculatedShares[stock.symbol] === 0 && selectedStocks[stock.symbol]
						? 'bg-red-50 hover:bg-red-100'
						: ''}
				>
					<Table.Cell>
						<Checkbox
							checked={selectedStocks[stock.symbol]}
							onCheckedChange={(v) => handleCheckboxChange(stock.symbol, v as boolean)}
						/>
					</Table.Cell>
					<Table.Cell class="font-medium">
						{stock.symbol}
						{#if calculatedShares[stock.symbol] === 0 && selectedStocks[stock.symbol]}
							<span class="ml-2 text-red-500 text-xs">(Too pricey)</span>
						{/if}
					</Table.Cell>
					<Table.Cell>
						{stock.ret.toFixed(2)}
					</Table.Cell>
					<Table.Cell>
						{stock.ltp.toFixed(2)}
					</Table.Cell>
					<Table.Cell>
						{calculatedShares[stock.symbol] || 0}
					</Table.Cell>
				</Table.Row>
			{/each}
		</Table.Body>
	</Table.Root>
</div>
