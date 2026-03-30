/**
 * API Client for Position Protection System
 * Phase 1: All API endpoint functions
 */

import { getApiBase, apiFetch } from '$lib/api';
import type {
	StrategyListResponse,
	ProtectionStrategyResponse,
	EngineHealthResponse,
	EventsResponse,
	RealtimePositionsResponse,
	MiniChainResponse,
	StrikeSuggestion,
	BuildPositionRequest,
	BuildPositionResponse
} from '../types';

const API_PREFIX = '/api/strategies';

/**
 * Get list of all strategies with optional filters
 */
export async function listStrategies(
	status?: string,
	monitoringMode?: string,
	limit: number = 50
): Promise<StrategyListResponse> {
	const params = new URLSearchParams();

	if (status) params.append('status', status);
	if (monitoringMode) params.append('monitoring_mode', monitoringMode);
	params.append('limit', limit.toString());

	const response = await apiFetch(`${API_PREFIX}/?${params.toString()}`);

	if (!response.ok) {
		throw new Error(`Failed to fetch strategies: ${response.statusText}`);
	}

	return response.json();
}

/**
 * Get detailed information about a specific strategy
 */
export async function getStrategy(strategyId: string): Promise<ProtectionStrategyResponse> {
	const response = await apiFetch(`${API_PREFIX}/${strategyId}`);

	if (!response.ok) {
		throw new Error(`Failed to fetch strategy: ${response.statusText}`);
	}

	return response.json();
}

/**
 * Get engine health status
 */
export async function getEngineHealth(): Promise<EngineHealthResponse> {
	const response = await apiFetch(`${API_PREFIX}/health`);

	if (!response.ok) {
		throw new Error(`Failed to fetch engine health: ${response.statusText}`);
	}

	return response.json();
}

/**
 * Get event history for a strategy
 */
export async function getStrategyEvents(
	strategyId: string,
	limit: number = 50
): Promise<EventsResponse> {
	const response = await apiFetch(`${API_PREFIX}/${strategyId}/events?limit=${limit}`);

	if (!response.ok) {
		throw new Error(`Failed to fetch events: ${response.statusText}`);
	}

	return response.json();
}

/**
 * Update strategy status (pause/resume)
 */
export async function updateStrategyStatus(
	strategyId: string,
	status: 'active' | 'paused',
	reason?: string
): Promise<{ status: string; strategy_id: string; message: string }> {
	const response = await apiFetch(`${API_PREFIX}/${strategyId}/status`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ status, reason })
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: response.statusText }));
		throw new Error(error.detail || 'Failed to update strategy status');
	}

	return response.json();
}

/**
 * Update strategy parameters (stoploss levels, trailing config, name)
 */
export async function updateStrategy(
	strategyId: string,
	updates: {
		name?: string;
		index_upper_stoploss?: number;
		index_lower_stoploss?: number;
		trailing_mode?: string;
		trailing_distance?: number;
		trailing_lock_profit?: number;
		premium_thresholds?: Record<string, any>;
	}
): Promise<ProtectionStrategyResponse> {
	const response = await apiFetch(`${API_PREFIX}/${strategyId}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(updates)
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: response.statusText }));
		throw new Error(error.detail || 'Failed to update strategy');
	}

	return response.json();
}

/**
 * Delete a strategy (must be paused first)
 */
export async function deleteStrategy(
	strategyId: string
): Promise<{ status: string; strategy_id: string }> {
	const response = await apiFetch(`${API_PREFIX}/${strategyId}`, {
		method: 'DELETE'
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: response.statusText }));
		throw new Error(error.detail || 'Failed to delete strategy');
	}

	return response.json();
}

/**
 * Get real-time positions snapshot
 */
export async function getRealtimePositions(): Promise<RealtimePositionsResponse> {
	const response = await apiFetch('/api/positions/realtime');

	if (!response.ok) {
		throw new Error(`Failed to fetch positions: ${response.statusText}`);
	}

	return response.json();
}

/**
 * Build SSE URL for position stream
 */
export function buildPositionStreamUrl(): string {
	const base = getApiBase();
	return `${base}/api/positions/stream`;
}

// ═══════════════════════════════════════════════════════════════════════════════
// PHASE 3: POSITION BUILDER API FUNCTIONS
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * Get available expiry dates for an underlying from active options session
 */
export async function getAvailableExpiries(
	underlying: string
): Promise<{ underlying: string; expiries: string[]; spot_ltp: number; timestamp: string }> {
	const response = await apiFetch(`${API_PREFIX}/available-expiries/${underlying}`);

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: response.statusText }));
		throw new Error(error.detail || 'Failed to fetch available expiries');
	}

	return response.json();
}

/**
 * Get mini option chain with live Greeks
 */
export async function getMiniChain(
	underlying: string,
	expiry: string,
	centerStrike?: number,
	count: number = 11
): Promise<MiniChainResponse> {
	const params = new URLSearchParams({ count: count.toString() });
	if (centerStrike) params.append('center_strike', centerStrike.toString());

	const response = await apiFetch(
		`${API_PREFIX}/mini-chain/${underlying}/${expiry}?${params.toString()}`
	);

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: response.statusText }));
		throw new Error(error.detail || 'Failed to fetch mini chain');
	}

	return response.json();
}

/**
 * Get strike suggestions based on delta and strategy type
 */
export async function suggestStrikes(
	underlying: string,
	expiry: string,
	strategyType: string,
	targetDelta: number = 0.3,
	riskAmount?: number
): Promise<StrikeSuggestion> {
	const response = await apiFetch(`${API_PREFIX}/suggest-strikes`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			underlying,
			expiry,
			strategy_type: strategyType,
			target_delta: targetDelta,
			risk_amount: riskAmount
		})
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: response.statusText }));
		throw new Error(error.detail || 'Failed to suggest strikes');
	}

	return response.json();
}

/**
 * Build position with optional protection strategy
 * If placeOrders is false, returns dry run plan
 * If placeOrders is true, executes orders and creates protection
 */
export async function buildPosition(request: BuildPositionRequest): Promise<BuildPositionResponse> {
	const response = await apiFetch(`${API_PREFIX}/build-position`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request)
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: response.statusText }));
		throw new Error(error.detail || 'Failed to build position');
	}

	return response.json();
}

// ═══════════════════════════════════════════════════════════════════════════════
// PHASE 4: CREATE PROTECTION STRATEGY
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * Create a new protection strategy for existing positions
 */
export async function createProtectionStrategy(request: any): Promise<ProtectionStrategyResponse> {
	const response = await apiFetch(`${API_PREFIX}/protection`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request)
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: response.statusText }));
		throw new Error(error.detail || 'Failed to create protection strategy');
	}

	return response.json();
}
