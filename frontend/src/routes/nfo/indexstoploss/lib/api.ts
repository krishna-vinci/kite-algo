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
	RealtimePositionsResponse
} from '../types';

const API_PREFIX = '/strategies';

/**
 * Get list of all strategies with optional filters
 */
export async function listStrategies(
	status?: string,
	monitoringMode?: string,
	limit: number = 50
): Promise<StrategyListResponse> {
	const base = getApiBase();
	const params = new URLSearchParams();
	
	if (status) params.append('status', status);
	if (monitoringMode) params.append('monitoring_mode', monitoringMode);
	params.append('limit', limit.toString());
	
	const url = `${base}${API_PREFIX}/?${params.toString()}`;
	const response = await fetch(url);
	
	if (!response.ok) {
		throw new Error(`Failed to fetch strategies: ${response.statusText}`);
	}
	
	return response.json();
}

/**
 * Get detailed information about a specific strategy
 */
export async function getStrategy(strategyId: string): Promise<ProtectionStrategyResponse> {
	const base = getApiBase();
	const url = `${base}${API_PREFIX}/${strategyId}`;
	const response = await fetch(url);
	
	if (!response.ok) {
		throw new Error(`Failed to fetch strategy: ${response.statusText}`);
	}
	
	return response.json();
}

/**
 * Get engine health status
 */
export async function getEngineHealth(): Promise<EngineHealthResponse> {
	const base = getApiBase();
	const url = `${base}${API_PREFIX}/health`;
	const response = await fetch(url);
	
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
	const base = getApiBase();
	const url = `${base}${API_PREFIX}/${strategyId}/events?limit=${limit}`;
	const response = await fetch(url);
	
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
	const base = getApiBase();
	const url = `${base}${API_PREFIX}/${strategyId}/status`;
	
	const response = await fetch(url, {
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
 * Delete a strategy (must be paused first)
 */
export async function deleteStrategy(
	strategyId: string
): Promise<{ status: string; strategy_id: string }> {
	const base = getApiBase();
	const url = `${base}${API_PREFIX}/${strategyId}`;
	
	const response = await fetch(url, {
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
	const response = await apiFetch('/broker/positions/realtime');
	
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
	return `${base}/broker/positions/stream`;
}
