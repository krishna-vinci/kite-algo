<script lang="ts">
	import '../app.css';
	import { ModeWatcher } from "mode-watcher";
	import favicon from '$lib/assets/favicon.svg';
	import Navbar from '$lib/components/Navbar.svelte'; // Import the new Navbar component
	import AppSidebar from '$lib/components/AppSidebar.svelte';
	import Toast from '$lib/components/Toast.svelte';

	let { children } = $props();
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

<div class="app-layout">
	<AppSidebar />
	<main class="main-content">
		<Navbar />
		<div class="content-wrapper">
			{@render children?.()}
		</div>
	</main>
</div>

<ModeWatcher />
<Toast />

<style>
	.app-layout {
		display: flex;
		height: 100vh;
		overflow: hidden;
	}
	.main-content {
		flex-grow: 1;
		display: flex;
		flex-direction: column;
		overflow-y: auto;
	}
	.content-wrapper {
		padding: 1.5rem; /* 24px */
		flex-grow: 1;
	}
</style>
