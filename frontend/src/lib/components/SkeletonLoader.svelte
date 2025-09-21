<script lang="ts">
	export let width: string = '100%';
	export let height: string = '1em';
	export let borderRadius: string = '0.25rem';
	export let className: string = '';
</script>

<div
	class="animate-pulse bg-gray-200 {className}"
	style="width: {width}; height: {height}; border-radius: {borderRadius};"
></div>

<style>
	/* You can add more specific styles here if needed */
</style>
