<script lang="ts">
	import { page } from '$app/stores';
	const subNavItems = [
		{ name: 'Favorites', href: '/marketwatch/favorites' },
		{ name: 'Nifty 50', href: '/marketwatch/nifty-50' },
		{ name: 'Charts', href: '/marketwatch/charts' },
		{ name: 'Performance', href: '/marketwatch/performance', preload: 'off' }
	];
</script>

<div class="sub-nav-container border-b">
	<nav class="sub-nav container flex gap-4">
		{#each subNavItems as item}
			<a
				href={item.href}
				data-sveltekit-preload-data={item.preload}
				class="py-4 px-1 text-sm font-medium {$page.url.pathname.startsWith(item.href)
					? 'border-b-2 border-primary text-primary'
					: 'border-b-2 border-transparent text-muted-foreground hover:text-foreground'}"
			>
				{item.name}
			</a>
		{/each}
	</nav>
</div>

<main class="px-6 py-6">
	<slot />
</main>