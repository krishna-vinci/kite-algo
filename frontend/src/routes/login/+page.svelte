<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { apiFetch } from '$lib/api';

	let username = 'admin';
	let password = '';
	let loading = false;
	let error = '';

	async function login() {
		loading = true;
		error = '';
		try {
			const response = await apiFetch('/api/auth/login', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ username, password })
			});
			if (!response.ok) {
				error = 'Invalid username or password';
				return;
			}
			const next = $page.url.searchParams.get('next') || '/';
			await goto(next);
		} catch (e) {
			error = 'Login failed';
		} finally {
			loading = false;
		}
	}
</script>

<div class="flex min-h-screen items-center justify-center bg-muted/20 px-4">
	<div class="w-full max-w-md rounded-xl border bg-background p-6 shadow-sm">
		<div class="mb-6 space-y-1 text-center">
			<h1 class="text-2xl font-semibold">Sign in to Kite Algo</h1>
			<p class="text-sm text-muted-foreground">App login is separate from broker connection.</p>
		</div>

		<div class="space-y-4">
			<div>
				<label class="mb-1 block text-sm font-medium" for="app-username">Username</label>
				<input id="app-username" bind:value={username} class="w-full rounded-md border px-3 py-2" />
			</div>
			<div>
				<label class="mb-1 block text-sm font-medium" for="app-password">Password</label>
				<input id="app-password" bind:value={password} type="password" class="w-full rounded-md border px-3 py-2" />
			</div>
			<button
				on:click={login}
				disabled={loading}
				class="w-full rounded-md bg-primary px-3 py-2 text-primary-foreground"
			>
				{loading ? 'Signing in...' : 'Sign in'}
			</button>
			{#if error}
				<p class="text-sm text-destructive">{error}</p>
			{/if}
		</div>
	</div>
</div>
