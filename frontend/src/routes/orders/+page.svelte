<script lang="ts">
	import { onMount } from 'svelte';
	import { getWebhookEvents, type WebhookEvent } from '$lib/api';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import {
		Table,
		TableBody,
		TableCell,
		TableHead,
		TableHeader,
		TableRow
	} from '$lib/components/ui/table';
	import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card';
	import { Badge } from '$lib/components/ui/badge';
	import { RefreshCw, Search, ChevronLeft, ChevronRight } from '@lucide/svelte';

	let events: WebhookEvent[] = [];
	let loading = false;
	let error = '';

	// Filters
	let orderIdFilter = '';
	let statusFilter = '';
	let limit = 50;
	let offset = 0;

	// Expanded row for payload view
	let expandedEventId: string | null = null;

	async function loadEvents() {
		loading = true;
		error = '';
		try {
			events = await getWebhookEvents({
				order_id: orderIdFilter || undefined,
				status: statusFilter || undefined,
				limit,
				offset
			});
		} catch (e: any) {
			error = e.message || 'Failed to load webhook events';
			console.error('Failed to load webhook events:', e);
		} finally {
			loading = false;
		}
	}

	function formatTimestamp(ts: string): string {
		try {
			return new Date(ts).toLocaleString();
		} catch {
			return ts;
		}
	}

	function getStatusColor(status: string): string {
		const s = status.toUpperCase();
		if (s === 'COMPLETE') return 'bg-green-500';
		if (s === 'CANCELLED' || s === 'REJECTED') return 'bg-red-500';
		if (s === 'OPEN' || s === 'TRIGGER PENDING') return 'bg-blue-500';
		return 'bg-gray-500';
	}

	function togglePayload(eventId: string) {
		expandedEventId = expandedEventId === eventId ? null : eventId;
	}

	function nextPage() {
		offset += limit;
		loadEvents();
	}

	function prevPage() {
		if (offset >= limit) {
			offset -= limit;
			loadEvents();
		}
	}

	onMount(() => {
		loadEvents();
	});
</script>

<div class="container mx-auto py-6 space-y-6">
	<Card>
		<CardHeader>
			<CardTitle>Webhook Order Events</CardTitle>
			<CardDescription>
				Real-time postback events received from your broker for order updates
			</CardDescription>
		</CardHeader>
		<CardContent class="space-y-4">
			<!-- Filters -->
			<div class="flex flex-wrap gap-4 items-end">
				<div class="flex-1 min-w-[200px]">
					<Label for="order-id-filter">Order ID</Label>
					<Input
						id="order-id-filter"
						bind:value={orderIdFilter}
						placeholder="Filter by order ID"
						on:keydown={(e) => e.key === 'Enter' && loadEvents()}
					/>
				</div>
				<div class="flex-1 min-w-[200px]">
					<Label for="status-filter">Status</Label>
					<Input
						id="status-filter"
						bind:value={statusFilter}
						placeholder="e.g. COMPLETE, OPEN"
						on:keydown={(e) => e.key === 'Enter' && loadEvents()}
					/>
				</div>
				<Button on:click={loadEvents} disabled={loading}>
					<Search class="w-4 h-4 mr-2" />
					Search
				</Button>
				<Button on:click={loadEvents} variant="outline" disabled={loading}>
					<RefreshCw class={`w-4 h-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
					Refresh
				</Button>
			</div>

			{#if error}
				<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 text-red-800 dark:text-red-200 px-4 py-3 rounded">
					{error}
				</div>
			{/if}

			<!-- Events Table -->
			<div class="rounded-md border">
				<Table>
					<TableHeader>
						<TableRow>
							<TableHead>Event Time</TableHead>
							<TableHead>Order ID</TableHead>
							<TableHead>Symbol</TableHead>
							<TableHead>Type</TableHead>
							<TableHead>Status</TableHead>
							<TableHead>Qty</TableHead>
							<TableHead>Filled</TableHead>
							<TableHead>Avg Price</TableHead>
							<TableHead>Actions</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{#if loading && events.length === 0}
							<TableRow>
								<TableCell colspan={9} class="text-center py-8 text-muted-foreground">
									Loading events...
								</TableCell>
							</TableRow>
						{:else if events.length === 0}
							<TableRow>
								<TableCell colspan={9} class="text-center py-8 text-muted-foreground">
									No webhook events found
								</TableCell>
							</TableRow>
						{:else}
							{#each events as event (event.id)}
								<TableRow>
									<TableCell class="font-mono text-xs">
										{formatTimestamp(event.event_timestamp)}
									</TableCell>
									<TableCell class="font-mono text-xs">{event.order_id}</TableCell>
									<TableCell>
										{event.tradingsymbol || '-'}
										{#if event.exchange}
											<span class="text-xs text-muted-foreground">({event.exchange})</span>
										{/if}
									</TableCell>
									<TableCell>
										{#if event.transaction_type}
											<Badge variant={event.transaction_type === 'BUY' ? 'default' : 'secondary'}>
												{event.transaction_type}
											</Badge>
										{:else}
											-
										{/if}
									</TableCell>
									<TableCell>
										<Badge class={getStatusColor(event.status)}>
											{event.status}
										</Badge>
									</TableCell>
									<TableCell>{event.quantity ?? '-'}</TableCell>
									<TableCell>{event.filled_quantity ?? '-'}</TableCell>
									<TableCell>
										{event.average_price ? `₹${event.average_price.toFixed(2)}` : '-'}
									</TableCell>
									<TableCell>
										<Button
											variant="ghost"
											size="sm"
											on:click={() => togglePayload(event.id)}
										>
											{expandedEventId === event.id ? 'Hide' : 'View'} Payload
										</Button>
									</TableCell>
								</TableRow>
								{#if expandedEventId === event.id}
									<TableRow>
										<TableCell colspan={9} class="bg-muted/50">
											<div class="p-4">
												<h4 class="font-semibold mb-2">Full Payload</h4>
												<pre class="bg-background p-3 rounded text-xs overflow-auto max-h-96">{JSON.stringify(
														event.payload,
														null,
														2
													)}</pre>
											</div>
										</TableCell>
									</TableRow>
								{/if}
							{/each}
						{/if}
					</TableBody>
				</Table>
			</div>

			<!-- Pagination -->
			<div class="flex items-center justify-between">
				<div class="text-sm text-muted-foreground">
					Showing {offset + 1} - {offset + events.length} events
				</div>
				<div class="flex gap-2">
					<Button variant="outline" size="sm" on:click={prevPage} disabled={offset === 0 || loading}>
						<ChevronLeft class="w-4 h-4 mr-1" />
						Previous
					</Button>
					<Button variant="outline" size="sm" on:click={nextPage} disabled={events.length < limit || loading}>
						Next
						<ChevronRight class="w-4 h-4 ml-1" />
					</Button>
				</div>
			</div>
		</CardContent>
	</Card>
</div>
