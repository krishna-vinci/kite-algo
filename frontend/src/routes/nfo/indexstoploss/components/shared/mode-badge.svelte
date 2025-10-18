<script lang="ts">
	import { Badge } from '$lib/components/ui/badge';
	import type { MonitoringMode } from '../../types';
	import { getModeColor, getModeDisplayName } from '../../lib/utils';
	
	interface Props {
		mode: MonitoringMode;
		class?: string;
	}
	
	let { mode, class: className }: Props = $props();
</script>

<Badge 
	variant="outline" 
	class={`${getModeColor(mode)} ${className || ''} font-mono text-xs`}
>
	{getModeDisplayName(mode)}
</Badge>
