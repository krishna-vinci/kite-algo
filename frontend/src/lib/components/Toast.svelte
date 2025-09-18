<script lang="ts">
	import { toast, type ToastMessage } from '$lib/stores/toast';
	import { fly } from 'svelte/transition';

	let toasts: ToastMessage[] = [];
	toast.subscribe((value) => {
		toasts = value;
	});
</script>

<div class="fixed bottom-4 right-4 z-50 space-y-2">
	{#each toasts as { id, message, type } (id)}
		<div
			in:fly={{ y: 20, duration: 300 }}
			out:fly={{ y: 20, duration: 300 }}
			class="px-4 py-3 rounded-md text-white shadow-lg"
			class:bg-green-500={type === 'success'}
			class:bg-red-500={type === 'error'}
		>
			{message}
		</div>
	{/each}
</div>
