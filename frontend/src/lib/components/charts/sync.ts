import type { IChartApi, LogicalRange } from 'lightweight-charts';

/**
 * Represents a group of charts that are synchronized.
 */
export interface SyncGroup {
	id: string;
	register(chart: IChartApi): () => void;
	unregister(chart: IChartApi): void;
}

const syncGroups = new Map<string, Set<IChartApi>>();
let isSyncing = false;

/**
 * Retrieves an existing sync group or creates a new one if it doesn't exist.
 * @param id - The unique identifier for the sync group.
 * @returns The SyncGroup instance.
 */
export function getOrCreateSyncGroup(id: string): SyncGroup {
	if (!syncGroups.has(id)) {
		syncGroups.set(id, new Set());
	}
	const group = syncGroups.get(id)!;

	const handleTimeRangeChange = (
		logicalRange: LogicalRange | null,
		sourceChart: IChartApi
	) => {
		if (isSyncing || !logicalRange) return;

		isSyncing = true;
		for (const chart of group) {
			if (chart !== sourceChart) {
				chart.timeScale().setVisibleLogicalRange(logicalRange);
			}
		}
		isSyncing = false;
	};

	return {
		id,
		register(chart: IChartApi): () => void {
			group.add(chart);
			const subscription = (logicalRange: LogicalRange | null) =>
				handleTimeRangeChange(logicalRange, chart);
			chart.timeScale().subscribeVisibleLogicalRangeChange(subscription);

			return () => {
				chart.timeScale().unsubscribeVisibleLogicalRangeChange(subscription);
				this.unregister(chart);
			};
		},
		unregister(chart: IChartApi): void {
			group.delete(chart);
			if (group.size === 0) {
				syncGroups.delete(id);
			}
		}
	};
}