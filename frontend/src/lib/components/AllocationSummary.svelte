<script lang="ts">
    export let investableCapital: number;
    export let totalAllocatedValue: number;
    export let unallocatedCapital: number;
    export let allocations: any[] = [];
</script>

<div class="bg-white p-6 rounded-lg shadow-md">
    <h2 class="text-xl font-semibold mb-4">Allocation Summary</h2>
    <div class="mb-4">
        <p class="text-sm text-gray-600">Total Investable Capital: ₹{investableCapital.toFixed(2)}</p>
        <p class="text-sm text-gray-600">Total Allocated Value: ₹{totalAllocatedValue.toFixed(2)}</p>
        <p class="text-sm text-gray-600">Unallocated Capital: ₹{unallocatedCapital.toFixed(2)}</p>
    </div>

    {#if allocations.length > 0}
        <h3 class="text-lg font-medium mb-2">Detailed Allocations:</h3>
        <div class="overflow-x-auto">
            <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                    <tr>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                            Symbol
                        </th>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                            Shares
                        </th>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                            Allocated Value (₹)
                        </th>
                        <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                            Status
                        </th>
                    </tr>
                </thead>
                <tbody class="bg-white divide-y divide-gray-200">
                    {#each allocations as allocation (allocation.symbol)}
                        <tr class={allocation.status !== 'ALLOCATED' ? 'bg-red-50' : ''}>
                            <td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">
                                {allocation.symbol}
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                {allocation.quantity}
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                {allocation.allocated_value.toFixed(2)}
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                {allocation.status}
                                {#if allocation.reason}
                                    <span class="ml-2 text-red-500 text-xs">({allocation.reason})</span>
                                {/if}
                            </td>
                        </tr>
                    {/each}
                </tbody>
            </table>
        </div>
    {:else}
        <p class="text-gray-500">No allocations calculated yet.</p>
    {/if}
</div>