<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import type {
		IChartApi,
		ISeriesApi,
		ChartOptions,
		DeepPartial,
		LogicalRange
	} from 'lightweight-charts';

	// Phase 1 Imports
	import type {
		ChartProps,
		ChartHandle,
		SeriesSpec,
		SeriesData,
		UTCSec,
		ChartEventMap,
		ThemeTokens
	} from './types';
	import { resolveThemeTokens, toChartOptions } from './theme';
	import { toUnixSeconds } from './time';
	import { autosize as autosizeAction } from './resize';
	import { getOrCreateSyncGroup } from './sync';
	import { computeTimeRange, updateModeForSeries } from './data';

	// --- PROPS ---
	type $$Props = ChartProps & {
		onTimeRangeChange?: (payload: ChartEventMap['timeRangeChange']) => void;
		onCrosshairMove?: (payload: ChartEventMap['crosshairMove']) => void;
		onClick?: (payload: ChartEventMap['click']) => void;
	};

	let {
		containerClass = '',
		options = {},
		autoSize = true,
		devicePixelRatio = 'auto',
		theme = 'light',
		locale = 'en-US',
		timeFormatter = undefined,
		series = [],
		syncGroup = null,
		fitContentOnInit = true,
		restoreRangeOnData = true,
		skeleton = false,
		onTimeRangeChange = undefined,
		onCrosshairMove = undefined,
		onClick = undefined
	}: $$Props = $props();

	// --- STATE ---
	let container: HTMLDivElement;
	let chart: IChartApi | null = null;
	let unregisterFromSync: (() => void) | null = null;

	// State tracking for each series
	interface SeriesState {
		api: ISeriesApi<any>;
		spec: SeriesSpec; // Keep a copy of the spec for comparison
	}
	const seriesStateMap = new Map<string, SeriesState>();
	let hasPerformedInitialFit = false;

	// --- LIFECYCLE ---
	onMount(() => {
		let unmounted = false;
		(async () => {
			const { createChart } = await import('lightweight-charts');
			if (unmounted) return;

			const themeTokens = resolveThemeTokens(theme);
			const baseOptions = toChartOptions(themeTokens);
			const mergedOptions: DeepPartial<ChartOptions> = {
				...baseOptions,
				...options,
				localization: {
					locale,
					...(options?.localization ?? {})
				}
			};

			chart = createChart(container, mergedOptions);

			if (timeFormatter) {
				chart.applyOptions({ localization: { timeFormatter } });
			}

			// Event Handlers
			chart
				.timeScale()
				.subscribeVisibleLogicalRangeChange((lr) => onTimeRangeChange?.({ logicalRange: lr }));
			chart.subscribeCrosshairMove((param) => {
				const time = param.time ? toUnixSeconds(param.time as number) : null;
				onCrosshairMove?.({ time });
			});
			chart.subscribeClick((param) => {
				const time = param.time ? toUnixSeconds(param.time as number) : null;
				onClick?.({ x: param.point?.x ?? 0, y: param.point?.y ?? 0, time });
			});

			// Sync Group
			if (syncGroup) {
				unregisterFromSync = getOrCreateSyncGroup(syncGroup).register(chart);
			}

			// Initial Series Sync
			syncSeries(series);
		})();

		return () => {
			unmounted = true;
		};
	});

	onDestroy(() => {
		if (unregisterFromSync) {
			unregisterFromSync();
		}
		if (chart) {
			seriesStateMap.forEach((state, id) => removeSeriesById(id));
			chart.remove();
			chart = null;
		}
		seriesStateMap.clear();
		hasPerformedInitialFit = false;
	});

	// --- SERIES MANAGEMENT ---
	async function createSeriesForSpec(spec: SeriesSpec): Promise<ISeriesApi<any> | null> {
		if (!chart) return null;

		const {
			CandlestickSeries,
			LineSeries,
			AreaSeries,
			HistogramSeries,
			BaselineSeries
		} = await import('lightweight-charts');

		const { pane, ...restOptions } = spec.options || {};

		const seriesOptions = {
			...restOptions,
			priceScaleId: spec.priceScaleId,
			visible: spec.visible
		};

		let newSeries: ISeriesApi<any>;
		const paneIndex = pane as number | undefined;

		switch (spec.type) {
			case 'candlestick':
				newSeries = chart.addSeries(CandlestickSeries, seriesOptions, paneIndex);
				break;
			case 'line':
				newSeries = chart.addSeries(LineSeries, seriesOptions, paneIndex);
				break;
			case 'area':
				newSeries = chart.addSeries(AreaSeries, seriesOptions, paneIndex);
				break;
			case 'histogram':
				newSeries = chart.addSeries(HistogramSeries, seriesOptions, paneIndex);
				break;
			case 'baseline':
				newSeries = chart.addSeries(BaselineSeries, seriesOptions, paneIndex);
				break;
		}

		seriesStateMap.set(spec.id, { api: newSeries, spec });
		return newSeries;
	}

	function removeSeriesById(id: string) {
		if (!chart) return;
		const state = seriesStateMap.get(id);
		if (state) {
			chart.removeSeries(state.api);
			seriesStateMap.delete(id);
		}
	}

	// --- REACTIVE SYNC LOGIC ---
	async function syncSeries(newSeriesSpecs: SeriesSpec[]) {
		if (!chart) return;

		const currentIds = new Set(newSeriesSpecs.map((s) => s.id));
		const existingIds = new Set(seriesStateMap.keys());
		let shouldFitContent = false;

		// Remove series that are no longer needed
		for (const id of existingIds) {
			if (!currentIds.has(id)) {
				removeSeriesById(id);
			}
		}

		// Add or update series
		for (const spec of newSeriesSpecs) {
			let state = seriesStateMap.get(spec.id);
			let seriesApi = state?.api;

			// Create series if it doesn't exist
			if (!state || !seriesApi) {
				const newSeriesApi = await createSeriesForSpec(spec);
				if (!newSeriesApi) continue;
				seriesApi = newSeriesApi;
				state = seriesStateMap.get(spec.id)!;
				shouldFitContent = true; // Fit content when a new series is added
			} else {
				// Apply options to existing series
				seriesApi.applyOptions({ ...spec.options, visible: spec.visible });
			}

			// Now, handle data updates
			const data = spec.data || [];
			const oldData = state.spec.data || [];
			const dataMode = updateModeForSeries(spec.dataMode);

			if (dataMode === 'updateLast' && oldData.length > 0 && data.length > 0) {
				// Incremental update: only push the last point
				const lastPoint = data[data.length - 1];
				seriesApi.update(lastPoint as any);
			} else if (data !== oldData) {
				// Full data replacement if data arrays are different
				seriesApi.setData(data as any);
			}

			// Update the stored spec for the next comparison
			state.spec = spec;
		}

		// Fit content only once on initial load
		if (shouldFitContent && fitContentOnInit && !hasPerformedInitialFit) {
			chart.timeScale().fitContent();
			hasPerformedInitialFit = true;
		}
	}

	// --- REACTIVE EFFECTS ---
	$effect(() => {
		if (chart) {
			const themeTokens = resolveThemeTokens(theme);
			const baseOptions = toChartOptions(themeTokens);
			const mergedOptions: DeepPartial<ChartOptions> = {
				...baseOptions,
				...options,
				localization: {
					locale,
					...(options?.localization ?? {})
				}
			};
			chart.applyOptions(mergedOptions);
		}
	});

	$effect(() => {
		if (chart && timeFormatter) {
			chart.applyOptions({ localization: { timeFormatter } });
		}
	});

	$effect(() => {
		syncSeries(series);
	});

	// --- AUTOSIZE ---
	function handleResize({ width, height }: { width: number; height: number }) {
		chart?.resize(width, height);
	}

	// --- IMPERATIVE HANDLE ---
	export function getChart(): IChartApi | null {
		return chart;
	}
	export function getSeries(id: string): ISeriesApi<any> | undefined {
		return seriesStateMap.get(id)?.api;
	}
</script>

<div
	bind:this={container}
	class="lw-chart {containerClass}"
	style="width:100%;height:100%;min-height:300px;position:relative;"
	use:autosizeAction={autoSize ? { onResize: handleResize } : undefined}
>
	{#if skeleton}
		<div class="skeleton-overlay"></div>
	{/if}
</div>

<style>
	.lw-chart {
		/* Ensures the container has a size for the chart to attach to */
		display: block;
	}
	.skeleton-overlay {
		position: absolute;
		top: 0;
		left: 0;
		width: 100%;
		height: 100%;
		background-color: rgba(200, 200, 200, 0.2);
		z-index: 10;
		pointer-events: none;
		animation: pulse 1.5s cubic-bezier(0.4, 0, 0.6, 1) infinite;
	}

	@keyframes pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.5;
		}
	}
</style>