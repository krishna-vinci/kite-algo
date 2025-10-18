<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Activity, AlertCircle } from '@lucide/svelte';
	import { getEngineHealth } from '../../lib/api';
	import type { EngineHealthResponse } from '../../types';
	import { getHealthStatusColor, getWsStatusColor } from '../../lib/utils';
	
	interface Props {
		initialHealth?: EngineHealthResponse;
		pollInterval?: number;
		class?: string;
	}
	
	let { initialHealth, pollInterval = 5000, class: className }: Props = $props();
	
	let health = $state<EngineHealthResponse | null>(initialHealth || null);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let pollTimer: ReturnType<typeof setInterval> | null = null;
	
	async function fetchHealth() {
		if (loading) return;
		
		loading = true;
		error = null;
		
		try {
			health = await getEngineHealth();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to fetch health';
			console.error('Failed to fetch engine health:', e);
		} finally {
			loading = false;
		}
	}
	
	onMount(() => {
		// Fetch immediately if no initial data
		if (!health) {
			fetchHealth();
		}
		
		// Start polling
		pollTimer = setInterval(fetchHealth, pollInterval);
	});
	
	onDestroy(() => {
		if (pollTimer) {
			clearInterval(pollTimer);
		}
	});
	
	const healthIcon = $derived(
		health?.status === 'healthy' ? '🟢' : 
		health?.status === 'degraded' ? '🟡' : '🔴'
	);
	
	const wsIcon = $derived(
		health?.websocket_status === 'connected' ? '🔗' : '⛓️‍💥'
	);
</script>

{#if error}
	<Badge variant="destructive" class={`${className || ''} gap-1`}>
		<AlertCircle class="h-3 w-3" />
		<span class="text-xs">Engine Error</span>
	</Badge>
{:else if health}
	<Badge 
		variant="outline" 
		class={`${getHealthStatusColor(health.status)} ${className || ''} gap-2`}
		title={`Engine: ${health.status} | WebSocket: ${health.websocket_status} | Active: ${health.active_strategies} | Interval: ${health.evaluation_interval_ms}ms`}
	>
		<Activity class="h-3 w-3 animate-pulse" />
		<span class="text-xs font-mono">{healthIcon} {health.active_strategies} active</span>
		<span class={`text-xs ${getWsStatusColor(health.websocket_status)}`}>{wsIcon}</span>
	</Badge>
{:else}
	<Badge variant="outline" class={`${className || ''} gap-1`}>
		<Activity class="h-3 w-3 animate-spin" />
		<span class="text-xs">Loading...</span>
	</Badge>
{/if}
