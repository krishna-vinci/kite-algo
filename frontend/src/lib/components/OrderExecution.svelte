<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	export let useBasket: boolean;
	export let allOrNone: boolean;
	export let previewingMargins: boolean;
	export let marginPreview: any;
	export let marginPreviewError: string | null;
	export let isExecutingOrders: boolean;
	export let orderExecutionError: string | null;
	export let successfulOrdersCount: number;
	export let failedOrdersCount: number;

	const dispatch = createEventDispatcher();

	function handlePreviewMargins() {
		dispatch('previewMargins');
	}

	function handleExecuteOrders() {
		dispatch('executeOrders');
	}
</script>

<div class="bg-white p-6 rounded-lg shadow-md">
	<h2 class="text-xl font-semibold mb-4">Order Execution</h2>

	{#if isExecutingOrders}
		<div
			class="bg-blue-100 border border-blue-400 text-blue-700 px-4 py-3 rounded relative mb-4"
			role="alert"
		>
			<strong class="font-bold">Executing Orders...</strong>
			<span class="block sm:inline">Please wait.</span>
		</div>
	{/if}

	{#if orderExecutionError}
		<div
			class="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded relative mb-4"
			role="alert"
		>
			<strong class="font-bold">Order Execution Error!</strong>
			<span class="block sm:inline">{orderExecutionError}</span>
		</div>
	{/if}

	{#if !isExecutingOrders && (successfulOrdersCount > 0 || failedOrdersCount > 0)}
		<div
			class="bg-green-100 border border-green-400 text-green-700 px-4 py-3 rounded relative mb-4"
			role="alert"
		>
			<strong class="font-bold">Order Execution Complete!</strong>
			<span class="block sm:inline">
				{successfulOrdersCount} successful, {failedOrdersCount} failed.
			</span>
		</div>
	{/if}

	<div class="mb-4">
		<label class="inline-flex items-center">
			<input
				type="checkbox"
				bind:checked={useBasket}
				class="h-4 w-4 text-indigo-600 border-gray-300 rounded"
			/>
			<span class="ml-2 text-sm text-gray-700">Use basket order</span>
		</label>
		{#if useBasket}
			<label class="inline-flex items-center mt-2 block">
				<input
					type="checkbox"
					bind:checked={allOrNone}
					class="h-4 w-4 text-indigo-600 border-gray-300 rounded"
				/>
				<span class="ml-2 text-sm text-gray-700">All-or-none basket (best-effort rollback)</span>
			</label>
			<div class="mt-3 flex gap-2">
				<button
					on:click={handlePreviewMargins}
					class="flex-1 bg-gray-100 text-gray-800 py-2 px-4 rounded-md hover:bg-gray-200 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2"
					disabled={previewingMargins}
				>
					{previewingMargins ? 'Previewing...' : 'Preview Margins'}
				</button>
				<button
					on:click={handleExecuteOrders}
					class="flex-1 bg-blue-600 text-white py-2 px-4 rounded-md hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
					disabled={isExecutingOrders || previewingMargins}
				>
					Execute Orders
				</button>
			</div>
			{#if marginPreview}
				<div class="mt-3">
					<p class="text-sm text-gray-600">Margin preview:</p>
					<pre class="text-xs bg-gray-100 p-2 rounded overflow-x-auto">{JSON.stringify(
							marginPreview,
							null,
							2
						)}</pre>
				</div>
			{:else if marginPreviewError}
				<div class="mt-3 text-sm text-red-600">{marginPreviewError}</div>
			{/if}
		{:else}
			<button
				on:click={handleExecuteOrders}
				class="w-full bg-blue-600 text-white py-2 px-4 rounded-md hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
				disabled={isExecutingOrders}
			>
				Execute Orders
			</button>
		{/if}
	</div>
</div>
