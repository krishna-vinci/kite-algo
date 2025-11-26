<script lang="ts">
	import * as Card from '$lib/components/ui/card';
	import * as Table from '$lib/components/ui/table';
	import { Badge } from '$lib/components/ui/badge';

	export let investableCapital: number;
	export let totalAllocatedValue: number;
	export let unallocatedCapital: number;
	export let allocations: any[] = [];
</script>

<Card.Root>
	<Card.Header>
		<Card.Title>Allocation Summary</Card.Title>
	</Card.Header>
	<Card.Content>
		<div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
			<div class="p-4 bg-gray-50 rounded-md">
				<div class="text-sm text-gray-500 mb-1">Total Investable Capital</div>
				<div class="text-lg font-semibold">₹{investableCapital.toFixed(2)}</div>
			</div>
			<div class="p-4 bg-gray-50 rounded-md">
				<div class="text-sm text-gray-500 mb-1">Total Allocated Value</div>
				<div class="text-lg font-semibold text-green-600">₹{totalAllocatedValue.toFixed(2)}</div>
			</div>
			<div class="p-4 bg-gray-50 rounded-md">
				<div class="text-sm text-gray-500 mb-1">Unallocated Capital</div>
				<div class="text-lg font-semibold text-amber-600">₹{unallocatedCapital.toFixed(2)}</div>
			</div>
		</div>

		{#if allocations.length > 0}
			<h3 class="text-lg font-medium mb-2">Detailed Allocations</h3>
			<div class="rounded-md border">
				<Table.Root>
					<Table.Header>
						<Table.Row>
							<Table.Head>Symbol</Table.Head>
							<Table.Head>Shares</Table.Head>
							<Table.Head>Allocated Value (₹)</Table.Head>
							<Table.Head>Status</Table.Head>
						</Table.Row>
					</Table.Header>
					<Table.Body>
						{#each allocations as allocation (allocation.symbol)}
							<Table.Row class={allocation.status !== 'ALLOCATED' ? 'bg-red-50 hover:bg-red-100' : ''}>
								<Table.Cell class="font-medium">{allocation.symbol}</Table.Cell>
								<Table.Cell>{allocation.quantity}</Table.Cell>
								<Table.Cell>{allocation.allocated_value.toFixed(2)}</Table.Cell>
								<Table.Cell>
									<Badge variant={allocation.status === 'ALLOCATED' ? 'default' : 'destructive'}>
										{allocation.status}
									</Badge>
									{#if allocation.reason}
										<span class="ml-2 text-red-500 text-xs">({allocation.reason})</span>
									{/if}
								</Table.Cell>
							</Table.Row>
						{/each}
					</Table.Body>
				</Table.Root>
			</div>
		{:else}
			<p class="text-muted-foreground text-center py-4">No allocations calculated yet.</p>
		{/if}
	</Card.Content>
</Card.Root>
