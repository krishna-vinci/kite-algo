<script lang="ts">
	import type { NiftyInstrument } from '$lib/types';

	type SectorAttribution = {
		name: string;
		attribution: number;
		weight: number;
	};

	type DisplayItem = {
		name: string;
		value: number;
		symbol?: string;
	};

	export let title: string;
	export let items: (NiftyInstrument | SectorAttribution)[] = [];
	export let valueField: 'change_percent_live' | 'attribution_pp' | 'attribution' | 'weight_live';
	export let nameField: 'tradingsymbol' | 'name' = 'tradingsymbol';
	export let unit = '';

	let displayItems: DisplayItem[] = [];
	$: {
		if (items) {
			displayItems = items.map((item) => ({
				// @ts-ignore
				name: item[nameField],
				// @ts-ignore
				value: item[valueField],
				symbol: 'tradingsymbol' in item ? item.tradingsymbol : undefined
			}));
		}
	}
</script>

<div class="bg-white dark:bg-gray-800 shadow-md rounded-lg p-4 mb-4">
	<h3 class="text-lg font-semibold mb-2">{title}</h3>
	<ul class="space-y-2">
		{#each displayItems as item}
			<li class="flex justify-between items-center text-sm">
				<span class="font-medium truncate" title={item.name}>{item.name}</span>
				<span
					class={`font-semibold ${item.value >= 0 ? 'text-green-600' : 'text-red-600'}`}
				>
					{item.value.toFixed(2)}{unit}
				</span>
			</li>
		{/each}
	</ul>
</div>