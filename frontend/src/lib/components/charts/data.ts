import type { Candle, SeriesData } from './types';

/**
 * Computes the start and end time of a dataset.
 * @param data - An array of objects, each with a `time` property (Unix timestamp in seconds).
 * @returns An object with `start` and `end` times, or null if the data is empty.
 */
export function computeTimeRange(data: { time: number }[]): { start: number; end: number } | null {
	if (!data || data.length === 0) {
		return null;
	}
	const start = data.reduce((min, p) => (p.time < min ? p.time : min), data[0].time);
	const end = data.reduce((max, p) => (p.time > max ? p.time : max), data[0].time);
	return { start, end };
}

/**
 * Determines the data update mode for a series.
 * @param mode - The desired mode ('set', 'updateLast', or undefined).
 * @returns The resolved mode, defaulting to 'set'.
 */
export function updateModeForSeries(mode: 'set' | 'updateLast' | undefined): 'set' | 'updateLast' {
	return mode || 'set';
}

/**
 * Type guard to check if a SeriesData array is a Candle array.
 * @param d - The SeriesData to check.
 * @returns True if the data is a Candle array, false otherwise.
 */
export function isCandleArray(d: SeriesData): d is Candle[] {
	if (!d || d.length === 0) {
		return false;
	}
	const first = d[0];
	return 'open' in first && 'high' in first && 'low' in first && 'close' in first;
}

/**
 * Performs a shallow equality check on two objects.
 * @param a - The first object.
 * @param b - The second object.
 * @returns True if the objects are shallowly equal, false otherwise.
 */
export function shallowEqual<T extends object>(a: T, b: T): boolean {
	if (a === b) return true;

	const keysA = Object.keys(a) as (keyof T)[];
	const keysB = Object.keys(b);

	if (keysA.length !== keysB.length) return false;

	for (const key of keysA) {
		if (!Object.prototype.hasOwnProperty.call(b, key) || a[key] !== b[key]) {
			return false;
		}
	}

	return true;
}