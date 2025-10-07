<script lang="ts">
	import { page } from '$app/stores';
	const subNavItems = [
		{ name: 'All', href: '/holdings/all' },
		{ name: 'Equity', href: '/holdings/equity' },
		{ name: 'Mutual funds', href: '/holdings/mutual-funds' }
	];
</script>

<div class="sub-nav-container border-b">
	<nav class="sub-nav container flex gap-4">
		{#each subNavItems as item}
			<a
				href={item.href}
				class="py-4 px-1 text-sm font-medium {$page.url.pathname.startsWith(item.href)
					? 'border-b-2 border-primary text-primary'
					: 'border-b-2 border-transparent text-muted-foreground hover:text-foreground'}"
			>
				{item.name}
			</a>
		{/each}
	</nav>
</div>

<main class="container py-6">
	<slot />
</main>