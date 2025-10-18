<script lang="ts">
	import { Badge } from '$lib/components/ui/badge';
	import { 
		CheckCircle2, 
		XCircle, 
		AlertCircle, 
		Play, 
		Pause, 
		TrendingUp, 
		TrendingDown,
		Activity,
		Clock
	} from '@lucide/svelte';
	
	import { formatRelativeTime, formatCurrency } from '../lib/utils';
	import type { StrategyEvent } from '../types';
	
	interface Props {
		events: StrategyEvent[];
	}
	
	let { events }: Props = $props();
	
	// Event type configuration
	const eventConfig: Record<string, { icon: any; color: string; label: string }> = {
		created: { icon: CheckCircle2, color: 'text-green-500', label: 'Created' },
		paused: { icon: Pause, color: 'text-yellow-500', label: 'Paused' },
		resumed: { icon: Play, color: 'text-blue-500', label: 'Resumed' },
		triggered: { icon: AlertCircle, color: 'text-red-500', label: 'Triggered' },
		order_placed: { icon: TrendingDown, color: 'text-orange-500', label: 'Order Placed' },
		order_success: { icon: CheckCircle2, color: 'text-green-500', label: 'Order Success' },
		order_failed: { icon: XCircle, color: 'text-red-500', label: 'Order Failed' },
		trailing_activated: { icon: TrendingUp, color: 'text-purple-500', label: 'Trailing Activated' },
		trailing_updated: { icon: Activity, color: 'text-blue-500', label: 'Trailing Updated' },
		level_executed: { icon: CheckCircle2, color: 'text-green-500', label: 'Level Executed' },
		error: { icon: XCircle, color: 'text-red-500', label: 'Error' }
	};
	
	function getEventConfig(eventType: string) {
		return eventConfig[eventType] || { 
			icon: Clock, 
			color: 'text-gray-500', 
			label: eventType.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())
		};
	}
</script>

<div class="relative space-y-4">
	{#each events as event, index}
		{@const config = getEventConfig(event.event_type)}
		{@const Icon = config.icon}
		
		<div class="relative flex gap-4">
			<!-- Timeline line -->
			{#if index < events.length - 1}
				<div class="absolute left-[11px] top-8 bottom-0 w-px bg-border"></div>
			{/if}
			
			<!-- Icon -->
			<div class="relative flex-shrink-0">
				<div class="flex h-6 w-6 items-center justify-center rounded-full border-2 border-background bg-card">
					<Icon class="h-3 w-3 {config.color}" />
				</div>
			</div>
			
			<!-- Content -->
			<div class="flex-1 space-y-1 pb-4">
				<div class="flex items-center justify-between">
					<div class="flex items-center gap-2">
						<span class="font-medium text-sm">{config.label}</span>
						{#if event.order_status}
							<Badge variant="outline" class="text-xs">
								{event.order_status}
							</Badge>
						{/if}
					</div>
					<span class="text-xs text-muted-foreground">
						{formatRelativeTime(event.created_at)}
					</span>
				</div>
				
				<!-- Event details -->
				<div class="text-sm text-muted-foreground space-y-1">
					{#if event.trigger_price}
						<div>
							Trigger Price: <span class="font-mono">{formatCurrency(event.trigger_price)}</span>
							{#if event.trigger_type}
								<span class="ml-2">({event.trigger_type})</span>
							{/if}
						</div>
					{/if}
					
					{#if event.level_name}
						<div>Level: <span class="font-medium">{event.level_name}</span></div>
					{/if}
					
					{#if event.quantity_affected}
						<div>
							Quantity: <span class="font-mono">{event.quantity_affected}</span>
							{#if event.lots_affected}
								<span class="ml-2">({event.lots_affected} lots)</span>
							{/if}
						</div>
					{/if}
					
					{#if event.order_id}
						<div>Order ID: <span class="font-mono text-xs">{event.order_id}</span></div>
					{/if}
					
					{#if event.error_message}
						<div class="text-red-500">
							Error: {event.error_message}
						</div>
					{/if}
					
					{#if event.meta && Object.keys(event.meta).length > 0}
						<details class="mt-2">
							<summary class="cursor-pointer text-xs hover:text-foreground">
								View metadata
							</summary>
							<pre class="mt-2 rounded bg-muted p-2 text-xs overflow-auto">{JSON.stringify(event.meta, null, 2)}</pre>
						</details>
					{/if}
				</div>
			</div>
		</div>
	{/each}
</div>
