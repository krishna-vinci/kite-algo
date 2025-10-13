<script lang="ts">
	import { onMount } from 'svelte';
	import LightweightChart from './LightweightChart.svelte';
	import type {
		PaneSpec,
		ChartProps,
		LogicalRange,
		UTCSec
	} from './types';

	type $$Props = {
		panes?: PaneSpec[];
		sharedTimeScale?: boolean;
		syncGroup?: string;
		gap?: number;
		theme?: ChartProps['theme'];
		timeFormatter?: ChartProps['timeFormatter'];
		options?: ChartProps['options'];
		autoSize?: boolean;
		fitContentOnInit?: boolean;
		restoreRangeOnData?: boolean;
		containerClass?: string;
		devicePixelRatio?: ChartProps['devicePixelRatio'];
		skeleton?: boolean;
		onTimeRangeChange?: (payload: { logicalRange: LogicalRange | null; paneId: string }) => void;
		onCrosshairMove?: (payload: { time: UTCSec | null; paneId: string }) => void;
		onClick?: (payload: { x: number; y: number; time: UTCSec | null; paneId: string }) => void;
	};

	let {
		panes = [],
		sharedTimeScale = true,
		syncGroup = '',
		gap = 8,
		theme = undefined,
		timeFormatter = undefined,
		options = undefined,
		autoSize = true,
		fitContentOnInit = true,
		restoreRangeOnData = true,
		containerClass = '',
		devicePixelRatio = 'auto',
		skeleton = false,
		onTimeRangeChange = undefined,
		onCrosshairMove = undefined,
		onClick = undefined
	}: $$Props = $props();

	let internalSyncGroup = $state<string | null>(null);

	const effectiveSyncGroup = $derived(() => {
		if (sharedTimeScale) {
			return syncGroup || internalSyncGroup;
		}
		return null;
	});

	onMount(() => {
		if (sharedTimeScale && !syncGroup) {
			internalSyncGroup = `mp-${Math.random().toString(36).slice(2)}`;
		}
	});

	function calculateTotalRatio(panes: PaneSpec[]): number {
		const ratio = panes.reduce((acc: number, pane: PaneSpec) => {
			if (typeof pane.height === 'object' && 'ratio' in pane.height) {
				return acc + pane.height.ratio;
			}
			if (!pane.height) {
				return acc + 1;
			}
			return acc;
		}, 0);
		return ratio === 0 ? 1 : ratio;
	}

	function getPaneStyle(pane: PaneSpec): string {
		if (typeof pane.height === 'number') {
			return `height: ${pane.height}px; flex: 0 0 auto;`;
		}
		const totalRatio = calculateTotalRatio(panes);
		const ratio =
			(typeof pane.height === 'object' && 'ratio' in pane.height ? pane.height.ratio : 1) /
			totalRatio;
		return `flex: ${ratio} 1 0;`;
	}

	function getPaneOptions(pane: PaneSpec): ChartProps['options'] {
		if (pane.yMargins) {
			const baseOptions = options ? { ...options } : {};
			return {
				...baseOptions,
				rightPriceScale: {
					...baseOptions.rightPriceScale,
					scaleMargins: {
						top: pane.yMargins.top,
						bottom: pane.yMargins.bottom
					}
				}
			};
		}
		return options;
	}

</script>

<div
	class="multipane-container {containerClass}"
	style="display:flex; flex-direction:column; height:100%; width:100%; row-gap:{gap}px;"
>
	{#each panes as pane (pane.id)}
		<div class="pane-wrapper" style={getPaneStyle(pane)}>
			<LightweightChart
				series={pane.series}
				{theme}
				{timeFormatter}
				options={getPaneOptions(pane)}
				{autoSize}
				{fitContentOnInit}
				{restoreRangeOnData}
				{devicePixelRatio}
				{skeleton}
				syncGroup={effectiveSyncGroup()}
				onTimeRangeChange={(detail) => onTimeRangeChange?.({ ...detail, paneId: pane.id })}
				onCrosshairMove={(detail) => onCrosshairMove?.({ ...detail, paneId: pane.id })}
				onClick={(detail) => onClick?.({ ...detail, paneId: pane.id })}
			/>
		</div>
	{/each}
</div>

<style>
	.pane-wrapper {
		width: 100%;
		min-height: 0; /* Prevent flexbox overflow */
	}
</style>