<script lang="ts">
	import Navbar from './Navbar.svelte';
	import { page } from '$app/stores';

	let currentPath: string;

	$: {
		currentPath = $page.url.pathname;
	}

	const strategies = [
		{ name: 'Momentum', path: '/strategies/momentum' }
		// Add other strategies here in the future
	];
</script>

<div class="strategy-layout">
	<aside class="sidebar">
		<nav>
			<ul>
				{#each strategies as strategy}
					<li>
						<a href={strategy.path} class:active={currentPath === strategy.path}>
							{strategy.name}
						</a>
					</li>
				{/each}
			</ul>
		</nav>
	</aside>
	<main class="content">
		<slot />
	</main>
</div>

<style>
	.strategy-layout {
		display: flex;
		min-height: calc(100vh - 60px); /* Adjust based on Navbar height */
	}

	.sidebar {
		width: 200px;
		background-color: #f4f4f4;
		padding: 20px;
		border-right: 1px solid #eee;
	}

	.sidebar ul {
		list-style: none;
		padding: 0;
		margin: 0;
	}

	.sidebar li {
		margin-bottom: 10px;
	}

	.sidebar a {
		text-decoration: none;
		color: #333;
		font-weight: bold;
		display: block;
		padding: 8px 10px;
		border-radius: 4px;
		transition: background-color 0.2s ease-in-out;
	}

	.sidebar a:hover {
		background-color: #e0e0e0;
	}

	.sidebar a.active {
		background-color: #007bff;
		color: white;
	}

	.content {
		flex-grow: 1;
		padding: 20px;
	}
</style>
