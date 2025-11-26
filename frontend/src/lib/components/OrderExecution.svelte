<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import * as Card from '$lib/components/ui/card';
	import * as Alert from '$lib/components/ui/alert';
	import { Button } from '$lib/components/ui/button';
	import { Checkbox } from '$lib/components/ui/checkbox';
	import { Label } from '$lib/components/ui/label';
	import { Loader2 } from '@lucide/svelte';

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

<Card.Root>
	<Card.Header>
		<Card.Title>Order Execution</Card.Title>
	</Card.Header>
	<Card.Content>
		{#if isExecutingOrders}
			<Alert.Root class="mb-4">
				<Loader2 class="h-4 w-4 animate-spin" />
				<Alert.Title>Executing Orders...</Alert.Title>
				<Alert.Description>Please wait.</Alert.Description>
			</Alert.Root>
		{/if}

		{#if orderExecutionError}
			<Alert.Root variant="destructive" class="mb-4">
				<Alert.Title>Error</Alert.Title>
				<Alert.Description>{orderExecutionError}</Alert.Description>
			</Alert.Root>
		{/if}

		{#if !isExecutingOrders && (successfulOrdersCount > 0 || failedOrdersCount > 0)}
			<Alert.Root variant="default" class="mb-4 bg-green-50 border-green-200 text-green-800">
				<Alert.Title>Order Execution Complete</Alert.Title>
				<Alert.Description>
					{successfulOrdersCount} successful, {failedOrdersCount} failed.
				</Alert.Description>
			</Alert.Root>
		{/if}

		<div class="space-y-4">
			<div class="flex items-center space-x-2">
				<Checkbox id="useBasket" bind:checked={useBasket} />
				<Label for="useBasket">Use basket order</Label>
			</div>

			{#if useBasket}
				<div class="flex items-center space-x-2 ml-6">
					<Checkbox id="allOrNone" bind:checked={allOrNone} />
					<Label for="allOrNone">All-or-none basket (best-effort rollback)</Label>
				</div>

				<div class="flex gap-2 mt-4">
					<Button
						variant="secondary"
						class="w-full"
						on:click={handlePreviewMargins}
						disabled={previewingMargins}
					>
						{#if previewingMargins}
							<Loader2 class="mr-2 h-4 w-4 animate-spin" />
						{/if}
						Preview Margins
					</Button>
					<Button
						variant="default"
						class="w-full"
						on:click={handleExecuteOrders}
						disabled={isExecutingOrders || previewingMargins}
					>
						Execute Orders
					</Button>
				</div>

				{#if marginPreview}
					<div class="mt-4 rounded-md bg-muted p-4 overflow-x-auto">
						<p class="text-sm font-medium mb-2">Margin Preview:</p>
						<pre class="text-xs">{JSON.stringify(marginPreview, null, 2)}</pre>
					</div>
				{:else if marginPreviewError}
					<Alert.Root variant="destructive" class="mt-4">
						<Alert.Title>Preview Error</Alert.Title>
						<Alert.Description>{marginPreviewError}</Alert.Description>
					</Alert.Root>
				{/if}
			{:else}
				<Button
					variant="default"
					class="w-full mt-4"
					on:click={handleExecuteOrders}
					disabled={isExecutingOrders}
				>
					Execute Orders
				</Button>
			{/if}
		</div>
	</Card.Content>
</Card.Root>
