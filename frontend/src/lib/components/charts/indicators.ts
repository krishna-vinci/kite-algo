import type { SeriesSpec } from './types';
import { LineStyle } from 'lightweight-charts';

/**
 * Parameters for creating a constant line series.
 */
interface ConstantLineParams {
	id: string;
	value: number;
	timeRange: { start: number; end: number };
	color?: string;
	lineWidth?: 1 | 2 | 3 | 4;
	lineStyle?: LineStyle;
	priceLineVisible?: boolean;
}

/**
 * Creates a SeriesSpec for a horizontal line spanning a given time range.
 * This is useful for indicators like support/resistance, pivot points, etc.
 * @param params - The parameters for the constant line.
 * @returns A Line series specification.
 */
export function constantLineSeries({
	id,
	value,
	timeRange,
	color = 'rgba(41, 98, 255, 0.3)',
	lineWidth = 1,
	lineStyle = LineStyle.Dashed,
	priceLineVisible = false
}: ConstantLineParams): SeriesSpec {
	return {
		id,
		type: 'line',
		data: [
			{ time: timeRange.start, value },
			{ time: timeRange.end, value }
		],
		options: {
			color,
			lineWidth,
			lineStyle,
			priceLineVisible,
			lastValueVisible: false,
			crosshairMarkerVisible: false
		}
	};
}