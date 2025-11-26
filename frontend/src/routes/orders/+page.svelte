<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { getWsOrderEvents, buildOrderEventsSseUrl, type WebhookEvent } from '$lib/api';
	import { Button } from '$lib/components/ui/button';
	import {
		Table,
		TableBody,
		TableCell,
		TableHead,
		TableHeader,
		TableRow
	} from '$lib/components/ui/table';
	import { Badge } from '$lib/components/ui/badge';

	let openOrders: WebhookEvent[] = [];
	let executedOrders: WebhookEvent[] = [];
	let loading = false;
	let error = '';
	let streaming = false;
	let streamError = '';
	let es: EventSource | null = null;

	// IST offset: UTC+5:30
	function formatTimeIST(ts: string): string {
		try {
			const date = new Date(ts);
			return date.toLocaleTimeString('en-IN', { 
				timeZone: 'Asia/Kolkata',
				hour: '2-digit',
				minute: '2-digit',
				second: '2-digit'
			});
		} catch {
			return ts;
		}
	}

	function isOpenStatus(status: string): boolean {
		const s = (status || '').toUpperCase();
		return s === 'OPEN' || s === 'TRIGGER PENDING' || s === 'PENDING';
	}

	function isExecutedStatus(status: string): boolean {
		const s = (status || '').toUpperCase();
		return s === 'COMPLETE' || s === 'CANCELLED' || s === 'REJECTED';
	}

	function getStartOfDayIST(): string {
		const date = new Date();
		// Add 5h 30m to convert current UTC to IST "value"
		const istOffset = 5.5 * 60 * 60 * 1000;
		const istTime = new Date(date.getTime() + istOffset);
		// Set to midnight of that IST day (using UTC methods on the shifted time)
		istTime.setUTCHours(0, 0, 0, 0);
		// Subtract offset to get back to the true UTC timestamp of IST midnight
		const startOfDay = new Date(istTime.getTime() - istOffset);
		return startOfDay.toISOString();
	}

	async function loadEvents() {
		loading = true;
		error = '';
		try {
			// Fetch only today's events
			const allEvents = await getWsOrderEvents({ 
				limit: 200, 
				offset: 0,
				start_date: getStartOfDayIST()
			});

			// Deduplicate: keep the most terminal status for each order_id
			// Status priority ensures COMPLETE/CANCELLED/REJECTED take precedence over OPEN/PENDING
			const statusPriority: Record<string, number> = {
				'COMPLETE': 5,
				'CANCELLED': 4,
				'REJECTED': 3,
				'OPEN': 2,
				'PENDING': 1
			};
			const uniqueOrders = new Map<string, WebhookEvent>();
			for (const ev of allEvents) {
				const existing = uniqueOrders.get(ev.order_id);
				if (!existing) {
					uniqueOrders.set(ev.order_id, ev);
				} else {
					// Keep the event with higher status priority (more terminal state wins)
					const existingPriority = statusPriority[existing.status] ?? 0;
					const newPriority = statusPriority[ev.status] ?? 0;
					if (newPriority > existingPriority) {
						uniqueOrders.set(ev.order_id, ev);
					}
				}
			}
			const latestEvents = Array.from(uniqueOrders.values());

			openOrders = latestEvents.filter(e => isOpenStatus(e.status));
			executedOrders = latestEvents.filter(e => isExecutedStatus(e.status));
		} catch (e: any) {
			error = e.message || 'Failed to load orders';
			console.error('Failed to load orders:', e);
		} finally {
			loading = false;
		}
	}

	function appendEvent(ev: any) {
		try {
			const item: WebhookEvent = {
				id: ev.id,
				order_id: ev.order_id,
				user_id: ev.user_id,
				status: ev.status,
				event_timestamp: ev.event_timestamp,
				received_at: ev.received_at ?? new Date().toISOString(),
				exchange: ev.exchange ?? null,
				tradingsymbol: ev.tradingsymbol ?? null,
				instrument_token: ev.instrument_token ?? null,
				transaction_type: ev.transaction_type ?? null,
				quantity: ev.quantity ?? null,
				filled_quantity: ev.filled_quantity ?? null,
				average_price: typeof ev.average_price === 'number' ? ev.average_price : (ev.average_price ? Number(ev.average_price) : null),
				payload: ev.payload ?? {}
			};
			if (!item.id) return;

			// Remove from both lists first (in case status changed)
			openOrders = openOrders.filter(x => x.order_id !== item.order_id);
			executedOrders = executedOrders.filter(x => x.order_id !== item.order_id);

			// Add to appropriate list
			if (isOpenStatus(item.status)) {
				const exists = openOrders.findIndex(x => x.id === item.id);
				if (exists === -1) {
					openOrders = [item, ...openOrders].slice(0, 100);
				}
			} else if (isExecutedStatus(item.status)) {
				const exists = executedOrders.findIndex(x => x.id === item.id);
				if (exists === -1) {
					executedOrders = [item, ...executedOrders].slice(0, 100);
				}
			}
		} catch {}
	}

	function startStream() {
		try {
			streamError = '';
			if (es) {
				es.close();
				es = null;
			}
			// Do not clear tables, just attach to stream for updates
			
			const url = buildOrderEventsSseUrl('ws');
			es = new EventSource(url);
			const handler = (e: MessageEvent) => {
				try { appendEvent(JSON.parse(e.data)); } catch {}
			};
			es.onopen = () => { streaming = true; };
			es.onerror = () => { streamError = 'SSE connection error'; streaming = false; };
			es.onmessage = handler;
			es.addEventListener('ws', handler as any);
		} catch (e: any) {
			streamError = e?.message || 'Failed to start stream';
			streaming = false;
		}
	}

	function stopStream() {
		if (es) {
			es.close();
			es = null;
		}
		streaming = false;
	}

	function getStatusColor(status: string): string {
		const s = status.toUpperCase();
		if (s === 'COMPLETE') return 'bg-green-50 text-green-700 border-green-200';
		if (s === 'CANCELLED' || s === 'REJECTED') return 'bg-red-50 text-red-700 border-red-200';
		return 'bg-gray-50 text-gray-700 border-gray-200';
	}

	onMount(() => {
		loadEvents();
		startStream();
	});

	onDestroy(() => {
		if (es) {
			es.close();
			es = null;
		}
	});
</script>

<div class="space-y-6">
	<div class="flex items-center justify-between">
		<div></div>
		<div class="flex gap-2">
			{#if !streaming}
				<Button variant="outline" on:click={startStream}>Start Live</Button>
			{:else}
				<Button variant="destructive" on:click={stopStream}>Stop Live</Button>
			{/if}
		</div>
	</div>

	{#if streamError}
		<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 text-red-800 dark:text-red-200 px-4 py-3 rounded">
			{streamError}
		</div>
	{/if}

	{#if error}
		<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 text-red-800 dark:text-red-200 px-4 py-3 rounded">
			{error}
		</div>
	{/if}

	<!-- Open Orders Section -->
	<div class="space-y-4">
		<h2 class="text-xl font-semibold">Open orders ({openOrders.length})</h2>
		<div class="rounded-md border">
			<Table>
				<TableHeader>
					<TableRow>
						<TableHead>Time</TableHead>
						<TableHead>Type</TableHead>
						<TableHead>Instrument</TableHead>
						<TableHead>Product</TableHead>
						<TableHead>Qty.</TableHead>
						<TableHead>LTP</TableHead>
						<TableHead>Price</TableHead>
						<TableHead>Status</TableHead>
					</TableRow>
				</TableHeader>
				<TableBody>
					{#if loading}
						<TableRow>
							<TableCell colspan={8} class="text-center py-8 text-muted-foreground">
								Loading...
							</TableCell>
						</TableRow>
					{:else if openOrders.length === 0}
						<TableRow>
							<TableCell colspan={8} class="text-center py-8 text-muted-foreground">
								No open orders
							</TableCell>
						</TableRow>
					{:else}
						{#each openOrders as event}
							<TableRow>
								<TableCell class="font-mono text-sm">{formatTimeIST(event.event_timestamp)}</TableCell>
								<TableCell>
									{#if event.transaction_type === 'BUY'}
										<Badge variant="default" class="bg-blue-500">BUY</Badge>
									{:else if event.transaction_type === 'SELL'}
										<Badge variant="destructive">SELL</Badge>
									{:else}
										<span class="text-muted-foreground">-</span>
									{/if}
								</TableCell>
								<TableCell>
									<div class="font-medium">{event.tradingsymbol || '-'}</div>
									<div class="text-xs text-muted-foreground">{event.exchange || ''}</div>
								</TableCell>
								<TableCell class="text-sm">{event.payload?.product || '-'}</TableCell>
								<TableCell class="font-mono text-sm">
									{event.filled_quantity || 0} / {event.quantity || 0}
								</TableCell>
								<TableCell class="font-mono text-sm">-</TableCell>
								<TableCell class="font-mono text-sm">{event.average_price?.toFixed(2) || event.payload?.price?.toFixed(2) || '-'}</TableCell>
								<TableCell>
									<Badge variant="outline" class="bg-blue-50 text-blue-700 border-blue-200">
										{event.status}
									</Badge>
								</TableCell>
							</TableRow>
						{/each}
					{/if}
				</TableBody>
			</Table>
		</div>
	</div>

	<!-- Executed Orders Section -->
	<div class="space-y-4">
		<h2 class="text-xl font-semibold">Executed orders ({executedOrders.length})</h2>
		<div class="rounded-md border">
			<Table>
				<TableHeader>
					<TableRow>
						<TableHead>Time</TableHead>
						<TableHead>Type</TableHead>
						<TableHead>Instrument</TableHead>
						<TableHead>Product</TableHead>
						<TableHead>Qty.</TableHead>
						<TableHead>Avg. price</TableHead>
						<TableHead>Status</TableHead>
					</TableRow>
				</TableHeader>
				<TableBody>
					{#if loading}
						<TableRow>
							<TableCell colspan={7} class="text-center py-8 text-muted-foreground">
								Loading...
							</TableCell>
						</TableRow>
					{:else if executedOrders.length === 0}
						<TableRow>
							<TableCell colspan={7} class="text-center py-8 text-muted-foreground">
								No executed orders
							</TableCell>
						</TableRow>
					{:else}
						{#each executedOrders as event}
							<TableRow>
								<TableCell class="font-mono text-sm">{formatTimeIST(event.event_timestamp)}</TableCell>
								<TableCell>
									{#if event.transaction_type === 'BUY'}
										<Badge variant="default" class="bg-blue-500">BUY</Badge>
									{:else if event.transaction_type === 'SELL'}
										<Badge variant="destructive">SELL</Badge>
									{:else}
										<span class="text-muted-foreground">-</span>
									{/if}
								</TableCell>
								<TableCell>
									<div class="font-medium">{event.tradingsymbol || '-'}</div>
									<div class="text-xs text-muted-foreground">{event.exchange || ''}</div>
								</TableCell>
								<TableCell class="text-sm">{event.payload?.product || '-'}</TableCell>
								<TableCell class="font-mono text-sm">
									{event.filled_quantity || 0} / {event.quantity || 0}
								</TableCell>
								<TableCell class="font-mono text-sm">{event.average_price?.toFixed(2) || '-'}</TableCell>
								<TableCell>
									<Badge variant="outline" class={getStatusColor(event.status)}>
										{event.status}
									</Badge>
								</TableCell>
							</TableRow>
						{/each}
					{/if}
				</TableBody>
			</Table>
		</div>
	</div>
</div>
