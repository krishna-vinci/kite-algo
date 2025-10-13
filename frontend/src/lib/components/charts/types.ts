import type {
	ChartOptions,
	DeepPartial,
	IChartApi,
	ISeriesApi,
	LogicalRange
} from 'lightweight-charts';

export type { LogicalRange };

/** Seconds since epoch (UTC) */
export type UTCSec = number;

/** Represents a single candlestick data point. */
export interface Candle {
	time: UTCSec;
	open: number;
	high: number;
	low: number;
	close: number;
	volume?: number;
}

/** Represents a single data point for line, area, histogram, and baseline series. */
export interface LinePoint {
	time: UTCSec;
	value: number;
}

export type AreaPoint = LinePoint;
export type HistogramPoint = LinePoint;
export type BaselinePoint = LinePoint;

/** A union of all possible series data types. */
export type SeriesData = Candle[] | LinePoint[] | AreaPoint[] | HistogramPoint[] | BaselinePoint[];

/** The kind of series to be rendered. */
export type SeriesKind = 'candlestick' | 'line' | 'area' | 'histogram' | 'baseline';

/** Base interface for all series specifications. */
export interface SeriesBase {
	id: string;
	options?: Record<string, unknown>;
	priceScaleId?: string;
	pane?: string | number;
	visible?: boolean;
	dataMode?: 'set' | 'updateLast';
}

/** A discriminated union representing the specification for a single series. */
export type SeriesSpec =
	| ({ type: 'candlestick'; data: Candle[] } & SeriesBase)
	| ({ type: 'line'; data: LinePoint[] } & SeriesBase)
	| ({ type: 'area'; data: AreaPoint[] } & SeriesBase)
	| ({ type: 'histogram'; data: HistogramPoint[] } & SeriesBase)
	| ({ type: 'baseline'; data: BaselinePoint[] } & SeriesBase);

/** Properties for a single chart component. */
export interface ChartProps {
	containerClass?: string;
	options?: DeepPartial<ChartOptions>;
	autoSize?: boolean; // default true
	devicePixelRatio?: number | 'auto';
	theme?: 'light' | 'dark' | ThemeTokens;
	locale?: string;
	timeFormatter?: (tsSeconds: UTCSec) => string;
	series: SeriesSpec[];
	syncGroup?: string | null;
	fitContentOnInit?: boolean; // default true
	restoreRangeOnData?: boolean; // default true
	skeleton?: boolean; // default false
}

/** Map of chart events to their payload types. */
export interface ChartEventMap {
	crosshairMove: { time: UTCSec | null };
	click: { x: number; y: number; time: UTCSec | null };
	timeRangeChange: { logicalRange: LogicalRange | null };
}

/** Handle for interacting with a chart instance. */
export interface ChartHandle {
	getChart(): IChartApi | null;
	getSeries(id: string): ISeriesApi<any> | undefined;
	addSeries(spec: SeriesSpec): void;
	removeSeries(id: string): void;
	setSeriesData(id: string, data: SeriesData, mode?: 'set' | 'updateLast'): void;
}

/** Specification for a single pane in a multi-pane chart layout. */
export interface PaneSpec {
	id: string;
	height?: number | { ratio: number };
	yMargins?: { top: number; bottom: number };
	series: SeriesSpec[];
}

/** Properties for a multi-pane chart component. */
export interface MultiPaneProps {
	panes: PaneSpec[];
	sharedTimeScale?: boolean;
	syncGroup?: string;
	gap?: number;
}

// Forward-declare ThemeTokens to avoid circular dependency
export interface ThemeTokens {
	layout: { background: string; textColor: string; fontFamily?: string };
	grid: { vertLines: string; horzLines: string };
	priceScaleBorder: string;
	timeScaleBorder: string;
	crosshair: { color: string; width?: number };
	candle: { up: string; down: string; wickUp?: string; wickDown?: string; borderVisible?: boolean };
	line: { color: string; width?: number };
}