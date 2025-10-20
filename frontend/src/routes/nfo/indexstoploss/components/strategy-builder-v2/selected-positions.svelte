<script lang="ts">
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Checkbox } from '$lib/components/ui/checkbox';
	import { Trash2, RefreshCw, GripVertical, Calculator } from '@lucide/svelte';
	import type { CalculatedStrike } from '../../lib/strike-calculator';
	import { createEventDispatcher } from 'svelte';

	let {
		strikes,
		expiries,
		underlying,
		onRemove
	}: {
		strikes: (CalculatedStrike & { expiry: string })[];
		expiries: string[];
		underlying: string;
		onRemove: (index: number) => void;
	} = $props();

	const dispatch = createEventDispatcher();
	let multiplier = $state(1);

	function updateStrike(index: number, newStrike: number) {
		// This needs to be handled in the parent component
	}

	function updateTransactionType(index: number) {
		// This needs to be handled in the parent component
	}

	function updateLTP(index: number, ltp: number) {
		// This needs to be handled in the parent component
	}

	function formatDate(dateString: string) {
		if (!dateString) return '';
		const date = new Date(dateString);
		return date.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
	}

	const pricePay = $derived(strikes.reduce((acc, s) => acc + (s.ltp || 0), 0));
	const premiumPay = $derived(pricePay * 25 * multiplier); // Assuming lot size 25
</script>

<div>
	<!-- Header -->
	<div class="flex items-center justify-between mb-3 px-1">
		<div class="flex items-center gap-2 text-sm font-medium">
			<Checkbox id="select-all" />
			<label for="select-all" class="select-none">{strikes.length} trades selected</label>
		</div>
		<Button variant="link" class="h-auto p-0 text-primary" on:click={() => dispatch('reset')}>
			<RefreshCw class="h-3.5 w-3.5 mr-1" />
			Reset Prices
		</Button>
	</div>

	<!-- Table -->
	<div class="space-y-2">
		<!-- Table Header -->
		<div class="grid grid-cols-[auto_40px_90px_110px_40px_80px_auto] gap-x-2 px-1 text-xs text-muted-foreground font-medium">
			<div /> <!-- Checkbox col -->
			<div>B/S</div>
			<div>Expiry</div>
			<div class="text-center">Strike</div>
			<div>Type</div>
			<div class="text-center">Price</div>
			<div /> <!-- Actions col -->
		</div>

		<!-- Table Body -->
		{#each strikes as strike, i}
			<div class="grid grid-cols-[auto_40px_90px_110px_40px_80px_auto] gap-x-2 items-center">
				<Checkbox />
				
				<!-- B/S -->
				<Button
					variant="outline"
					class={`h-8 w-8 text-xs font-bold ${strike.transactionType === 'BUY' ? 'bg-blue-50 text-blue-700 border-blue-200 hover:bg-blue-100' : 'bg-red-50 text-red-700 border-red-200 hover:bg-red-100'}`}
					on:click={() => updateTransactionType(i)}
				>
					{strike.transactionType === 'BUY' ? 'B' : 'S'}
				</Button>

				<!-- Expiry -->
				<select
					class="h-8 w-full rounded-md border border-input bg-transparent px-1 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
					value={strike.expiry}
				>
					<option value={strike.expiry} disabled selected>{formatDate(strike.expiry)}</option>
				</select>

				<!-- Strike -->
				<div class="flex items-center">
					<Button variant="outline" size="icon" class="h-8 w-7 rounded-r-none" on:click={() => updateStrike(i, strike.strike - 50)}>-</Button>
					<Input type="number" class="h-8 w-full text-center rounded-none focus-visible:ring-offset-0 focus-visible:ring-0" value={strike.strike} />
					<Button variant="outline" size="icon" class="h-8 w-7 rounded-l-none" on:click={() => updateStrike(i, strike.strike + 50)}>+</Button>
				</div>

				<!-- Type -->
				<div class="text-center text-sm font-medium text-muted-foreground">{strike.optionType}</div>

				<!-- Price -->
				<Input type="number" class="h-8 text-center" value={strike.ltp} oninput={(e) => updateLTP(i, parseFloat(e.currentTarget.value))} />

				<!-- Actions -->
				<div class="flex items-center justify-center">
					<Button variant="ghost" size="icon" class="h-8 w-8 cursor-grab">
						<GripVertical class="h-4 w-4 text-muted-foreground" />
					</Button>
					<Button variant="ghost" size="icon" class="h-8 w-8" on:click={() => onRemove(i)}>
						<Trash2 class="h-4 w-4 text-muted-foreground" />
					</Button>
				</div>
			</div>
		{/each}
	</div>

	<!-- Footer -->
	<div class="mt-4 flex items-center justify-between">
		<div class="flex items-center gap-2">
			<label for="multiplier" class="text-sm font-medium">Multiplier</label>
			<Input type="number" id="multiplier" class="h-8 w-20" bind:value={multiplier} />
		</div>
		<div class="flex items-center gap-4 text-sm">
			<span>Price <span class="font-semibold">Pay {pricePay.toFixed(1)}</span></span>
			<span>Premium <span class="font-semibold">Pay {premiumPay.toLocaleString('en-IN')}</span></span>
			<Button variant="ghost" class="h-auto p-0 text-primary">
				<Calculator class="h-4 w-4 mr-1" />
				Charges
			</Button>
		</div>
	</div>
</div>
