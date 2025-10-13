<script lang="ts">
	import LightweightChart from './LightweightChart.svelte';
	import type { SeriesSpec, ChartProps, ChartEventMap } from './types';

	type $$Props = {
		id: string;
		series?: SeriesSpec[];
		height?: number | { ratio: number };
		yMargins?: { top: number; bottom: number };
		theme?: ChartProps['theme'];
		timeFormatter?: ChartProps['timeFormatter'];
		options?: ChartProps['options'];
		autoSize?: boolean;
		fitContentOnInit?: boolean;
		restoreRangeOnData?: boolean;
		containerClass?: string;
		syncGroup?: string | null;
		devicePixelRatio?: ChartProps['devicePixelRatio'];
		skeleton?: boolean;
		onTimeRangeChange?: (payload: ChartEventMap['timeRangeChange']) => void;
		onCrosshairMove?: (payload: ChartEventMap['crosshairMove']) => void;
		onClick?: (payload: ChartEventMap['click']) => void;
	};

	// Props
	let {
		id,
		series = [],
		height = { ratio: 1 },
		yMargins = { top: 0.1, bottom: 0.1 },
		theme = undefined,
		timeFormatter = undefined,
		options = undefined,
		autoSize = true,
		fitContentOnInit = true,
		restoreRangeOnData = true,
		containerClass = '',
		syncGroup = null,
		devicePixelRatio = 'auto',
		skeleton = false,
		onTimeRangeChange = undefined,
		onCrosshairMove = undefined,
		onClick = undefined
	}: $$Props = $props();

	const mergedOptions = $derived(() => {
		const baseOptions = options ? { ...options } : {};
		if (yMargins) {
			return {
				...baseOptions,
				rightPriceScale: {
					...baseOptions.rightPriceScale,
					scaleMargins: {
						top: yMargins.top,
						bottom: yMargins.bottom
					}
				}
			};
		}
		return baseOptions;
	});
</script>

<div class="indicator-pane-wrapper {containerClass}" style="width:100%; height:100%;">
	<LightweightChart
		{series}
		{theme}
		{timeFormatter}
		options={mergedOptions}
		{autoSize}
		{fitContentOnInit}
		{restoreRangeOnData}
		{devicePixelRatio}
		{skeleton}
		{syncGroup}
		{onTimeRangeChange}
		{onCrosshairMove}
		{onClick}
	/>
</div>

<style>
	.indicator-pane-wrapper {
		min-height: 0;
	}
</style>