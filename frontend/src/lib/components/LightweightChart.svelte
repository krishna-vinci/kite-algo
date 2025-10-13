<script lang="ts">
	import { dev } from '$app/environment';
	import { onMount } from 'svelte';
	import MultiPaneChart from './charts/MultiPaneChart.svelte';
	import type { ChartProps, PaneSpec, Candle, LinePoint } from './charts/types';

	type $$Props = {
		// Legacy Props (Deprecated)
		symbol?: string;
		exchange?: string;
		timeframe?: string;
		emaPeriod?: number;
		rsiPeriod?: number;

		// New Data Props
		candles?: Candle[];
		ema?: LinePoint[];
		rsi?: LinePoint[];

		// Passthrough ChartProps
		theme?: ChartProps['theme'];
		options?: ChartProps['options'];
		timeFormatter?: ChartProps['timeFormatter'];
		autoSize?: ChartProps['autoSize'];
		fitContentOnInit?: ChartProps['fitContentOnInit'];
		restoreRangeOnData?: ChartProps['restoreRangeOnData'];
		syncGroup?: string;
		devicePixelRatio?: ChartProps['devicePixelRatio'];
		skeleton?: ChartProps['skeleton'];
	};

	let {
		symbol = undefined,
		exchange = undefined,
		timeframe = undefined,
		emaPeriod = undefined,
		rsiPeriod = undefined,
		candles = [],
		ema = [],
		rsi = [],
		theme = 'light',
		options = undefined,
		timeFormatter = undefined,
		autoSize = true,
		fitContentOnInit = true,
		restoreRangeOnData = false,
		syncGroup = undefined,
		devicePixelRatio = undefined,
		skeleton = undefined
	}: $$Props = $props();

	$effect(() => {
		console.log(`[Wrapper] candles prop updated. Length: ${candles.length}`);
	});

	const panes: PaneSpec[] = $derived.by(() => {
		// This wrapper maintains the legacy two-pane (Price + RSI) structure.
		return [
			{
				id: 'price',
				series: [
					{ id: 'candles', type: 'candlestick' as const, data: candles },
					{
						id: 'ema',
						type: 'line' as const,
						data: ema,
						options: { color: '#2962FF', lineWidth: 2 }
					}
				]
			},
			{
				id: 'rsi',
				height: 150, // Preserve legacy height
				series: [
					{
						id: 'rsi',
						type: 'line' as const,
						data: rsi,
						options: { color: '#f44336', lineWidth: 2 }
					}
				]
			}
		];
	});

	onMount(() => {
		if (dev && (symbol || exchange || timeframe || emaPeriod || rsiPeriod)) {
			console.warn(
				`[DEPRECATED] The legacy <LightweightChart> component at 'frontend/src/lib/components/LightweightChart.svelte' no longer fetches its own data. The props 'symbol', 'exchange', 'timeframe', 'emaPeriod', and 'rsiPeriod' are ignored. Please fetch data in your route and pass it directly via the 'candles', 'ema', and 'rsi' props.`
			);
		}
	});
</script>

<MultiPaneChart
	{panes}
	{theme}
	{options}
	{timeFormatter}
	{autoSize}
	{fitContentOnInit}
	{restoreRangeOnData}
	{syncGroup}
	{devicePixelRatio}
	{skeleton}
/>