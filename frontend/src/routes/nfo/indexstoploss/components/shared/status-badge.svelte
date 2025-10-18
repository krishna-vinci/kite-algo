<script lang="ts">
	import { Badge } from '$lib/components/ui/badge';
	import type { StrategyStatus } from '../../types';
	import { getStatusVariant, getStatusColor } from '../../lib/utils';
	
	interface Props {
		status: StrategyStatus;
		class?: string;
	}
	
	let { status, class: className }: Props = $props();
	
	const statusIcons: Record<StrategyStatus, string> = {
		active: '🟢',
		paused: '🟡',
		completed: '⚪',
		triggered: '🔴',
		error: '⚠️',
		partial: '🔵'
	};
</script>

<Badge 
	variant={getStatusVariant(status)}
	class={`${getStatusColor(status)} ${className || ''} text-xs`}
>
	<span class="mr-1">{statusIcons[status] || ''}</span>
	{status.toUpperCase()}
</Badge>
