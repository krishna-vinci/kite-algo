<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { apiFetch, clearSessionId, setSessionId } from '$lib/api';

	type SessionStatus = {
		app?: { authenticated?: boolean; user?: { username: string; role: string } | null };
		broker?: { connected?: boolean };
	};

	let appAuthenticated = false;
	let brokerConnected = false;
	let userName = '';
	let brokerName = '';
	let isLoading = false;
	let errorMessage = '';

	onMount(async () => {
		await refreshStatus();
	});

	async function refreshStatus() {
		try {
			const res = await apiFetch('/api/auth/session-status');
			if (res.ok) {
				const data = (await res.json()) as SessionStatus;
				appAuthenticated = !!data?.app?.authenticated;
				userName = data?.app?.user?.username || '';
				brokerConnected = !!data?.broker?.connected;
				if (appAuthenticated) {
					await refreshBrokerProfile();
				}
				return;
			}
		} catch (error) {
			console.error('Failed to refresh auth status', error);
		}
		appAuthenticated = false;
		brokerConnected = false;
		userName = '';
		brokerName = '';
	}

	async function refreshBrokerProfile() {
		try {
			const response = await apiFetch('/api/profile_kite');
			if (response.ok) {
				const profile = await response.json();
				brokerConnected = true;
				brokerName = profile.user_shortname || profile.user_name || profile.email || 'Broker connected';
			} else {
				brokerConnected = false;
				brokerName = '';
			}
		} catch {
			brokerConnected = false;
			brokerName = '';
		}
	}

	async function appLogout() {
		isLoading = true;
		try {
			await apiFetch('/api/auth/logout', { method: 'POST' });
			clearSessionId();
			appAuthenticated = false;
			brokerConnected = false;
			userName = '';
			brokerName = '';
			await goto('/login');
		} finally {
			isLoading = false;
		}
	}

	async function brokerLogin() {
		isLoading = true;
		errorMessage = '';
		try {
			const response = await apiFetch('/api/login_kite', { method: 'POST' });
			if (!response.ok) {
				errorMessage = 'Broker connect failed';
				return;
			}
			const data = await response.json();
			if (data?.session_id) {
				setSessionId(data.session_id);
			}
			brokerConnected = true;
			brokerName =
				data.profile?.user_shortname || data.profile?.user_name || data.profile?.email || 'Broker connected';
		} catch (error) {
			console.error('Broker login error', error);
			errorMessage = 'Broker connect failed';
		} finally {
			isLoading = false;
		}
	}

	async function brokerLogout() {
		isLoading = true;
		try {
			await apiFetch('/api/logout_kite', { method: 'POST' });
			clearSessionId();
			brokerConnected = false;
			brokerName = '';
		} finally {
			isLoading = false;
		}
	}
</script>

<div class="flex items-center gap-2 text-sm">
	{#if appAuthenticated}
		<div class="flex flex-col items-end leading-tight">
			<span class="font-medium text-foreground">App: {userName}</span>
			<span class={brokerConnected ? 'text-emerald-600' : 'text-amber-600'}>
				Broker: {brokerConnected ? brokerName || 'connected' : 'not connected'}
			</span>
		</div>
		{#if brokerConnected}
			<button
				on:click={brokerLogout}
				class="rounded-md border px-3 py-1.5"
				disabled={isLoading}
			>
				Disconnect Broker
			</button>
		{:else}
			<button
				on:click={brokerLogin}
				class="rounded-md bg-primary px-3 py-1.5 text-primary-foreground"
				disabled={isLoading}
			>
				Connect Broker
			</button>
		{/if}
		<button
			on:click={appLogout}
			class="rounded-md bg-destructive px-3 py-1.5 text-white"
			disabled={isLoading}
		>
			App Logout
		</button>
	{/if}
</div>

{#if errorMessage}
	<div class="mt-1 text-xs text-destructive">{errorMessage}</div>
{/if}
