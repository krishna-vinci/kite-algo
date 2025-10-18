<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		Table,
		TableBody,
		TableCell,
		TableHead,
		TableHeader,
		TableRow
	} from '$lib/components/ui/table';
	import { Badge } from '$lib/components/ui/badge';
	import { RefreshCw } from '@lucide/svelte';
	import type { RealtimePosition } from '../../types';
	import { buildPositionStreamUrl, getRealtimePositions } from '../../lib/api';
	import { createPositionStream } from '../../lib/sse';
	import { formatCurrency, formatNumber, getPnLColor } from '../../lib/utils';
	
	interface Props {
		initialPositions?: RealtimePosition[];
	}
	
	let { initialPositions = [] }: Props = $props();
	
	let positions = $state<RealtimePosition[]>(initialPositions);
	let loading = $state(false);
	let streamConnected = $state(false);
	let lastUpdate = $state<Date | null>(null);
	let stream: ReturnType<typeof createPositionStream> | null = null;
	
	async function fetchPositions() {
		loading = true;
		try {
			const data = await getRealtimePositions();
			// Combine net and day positions
			positions = [...(data.net || []), ...(data.day || [])];
			lastUpdate = new Date();
		} catch (e) {
			console.error('Failed to fetch positions:', e);
		} finally {
			loading = false;
		}
	}
	
	function handleSSEUpdate(update: any) {
		// Update positions from SSE
		positions = [...(update.net || []), ...(update.day || [])];
		lastUpdate = new Date();
		streamConnected = true;
	}
	
	function handleSSEError(error: Event) {
		console.error('SSE error:', error);
		streamConnected = false;
	}
	
	onMount(() => {
		// Fetch initial data if not provided
		if (positions.length === 0) {
			fetchPositions();
		}
		
		// Only start SSE connection if we have a session (authenticated)
		// Check for session cookie or session storage
		const hasSession = document.cookie.includes('kite_session_id') || 
		                   sessionStorage.getItem('kite_session_id');
		
		if (hasSession) {
			const url = buildPositionStreamUrl();
			stream = createPositionStream(url, handleSSEUpdate, handleSSEError);
			stream.connect();
		} else {
			console.log('No session found, skipping SSE connection for positions');
		}
	});
	
	onDestroy(() => {
		if (stream) {
			stream.disconnect();
		}
	});
	
	const netPositions = $derived(positions.filter(p => p.quantity !== 0));
	const totalPnL = $derived(netPositions.reduce((sum, p) => sum + p.pnl, 0));
</script>

<div class="space-y-2">
	<div class="flex items-center justify-between">
		<div class="flex items-center gap-2">
			<h3 class="text-sm font-semibold">Real-Time Positions</h3>
			<Badge variant={streamConnected ? 'default' : 'secondary'} class="text-xs">
				{streamConnected ? '🟢 Live' : '⚪ Static'}
			</Badge>
			{#if lastUpdate}
				<span class="text-xs text-muted-foreground">
					Updated {lastUpdate.toLocaleTimeString()}
				</span>
			{/if}
		</div>
		<button
			onclick={fetchPositions}
			disabled={loading}
			class="text-muted-foreground hover:text-foreground transition-colors"
			title="Refresh Positions"
		>
			<RefreshCw class={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
		</button>
	</div>
	
	<div class="rounded-md border">
		<Table>
			<TableHeader>
				<TableRow>
					<TableHead class="w-[150px]">Symbol</TableHead>
					<TableHead class="w-[80px]">Exchange</TableHead>
					<TableHead class="w-[80px]">Product</TableHead>
					<TableHead class="w-[100px] text-right">Qty</TableHead>
					<TableHead class="w-[100px] text-right">Avg Price</TableHead>
					<TableHead class="w-[100px] text-right">LTP</TableHead>
					<TableHead class="w-[120px] text-right">P&L</TableHead>
				</TableRow>
			</TableHeader>
			<TableBody>
				{#if netPositions.length === 0}
					<TableRow>
						<TableCell colspan={7} class="text-center text-muted-foreground py-8">
							{loading ? 'Loading positions...' : 'No open positions'}
						</TableCell>
					</TableRow>
				{:else}
					{#each netPositions as position (position.instrument_token)}
						<TableRow class="hover:bg-muted/50">
							<TableCell class="font-medium text-xs">
								{position.tradingsymbol}
							</TableCell>
							<TableCell class="text-xs">
								{position.exchange}
							</TableCell>
							<TableCell class="text-xs">
								{position.product}
							</TableCell>
							<TableCell class={`text-right font-mono text-xs ${
								position.quantity > 0 ? 'text-green-500' : 'text-red-500'
							}`}>
								{position.quantity > 0 ? '+' : ''}{formatNumber(position.quantity, 0)}
							</TableCell>
							<TableCell class="text-right font-mono text-xs">
								{formatNumber(position.average_price, 2)}
							</TableCell>
							<TableCell class="text-right font-mono text-xs">
								{formatNumber(position.last_price, 2)}
							</TableCell>
							<TableCell class={`text-right font-mono font-semibold ${getPnLColor(position.pnl)}`}>
								{formatCurrency(position.pnl, 0)}
							</TableCell>
						</TableRow>
					{/each}
					<!-- Total Row -->
					<TableRow class="bg-muted/50 font-semibold">
						<TableCell colspan={6} class="text-right">
							<span class="text-sm">Total P&L</span>
						</TableCell>
						<TableCell class={`text-right font-mono text-lg ${getPnLColor(totalPnL)}`}>
							{formatCurrency(totalPnL, 0)}
						</TableCell>
					</TableRow>
				{/if}
			</TableBody>
		</Table>
	</div>
</div>
