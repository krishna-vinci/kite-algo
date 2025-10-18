/**
 * Server load function for Position Protection System
 * Phase 1: Fetch initial data for SSR
 */

import { getApiBase } from '$lib/api';
import type { PageServerLoad } from './$types';
import type { StrategyListResponse, EngineHealthResponse, RealtimePositionsResponse } from './types';

export const load: PageServerLoad = async ({ fetch, cookies }) => {
	const base = getApiBase();
	
	// Get session ID from cookies for authentication
	const sessionId = cookies.get('kite_session_id');
	const headers: HeadersInit = {};
	if (sessionId) {
		headers['X-Session-ID'] = sessionId;
	}
	
	// Fetch strategies, health, and positions in parallel
	const [strategiesRes, healthRes, positionsRes] = await Promise.allSettled([
		fetch(`${base}/strategies/?limit=50`, { credentials: 'include', headers }),
		fetch(`${base}/strategies/health`, { credentials: 'include', headers }),
		fetch(`${base}/broker/positions/realtime`, { credentials: 'include', headers })
	]);
	
	// Parse strategies
	let strategies: StrategyListResponse = { total: 0, strategies: [] };
	if (strategiesRes.status === 'fulfilled' && strategiesRes.value.ok) {
		strategies = await strategiesRes.value.json();
	} else {
		console.error('Failed to fetch strategies:', strategiesRes);
	}
	
	// Parse health
	let health: EngineHealthResponse | null = null;
	if (healthRes.status === 'fulfilled' && healthRes.value.ok) {
		health = await healthRes.value.json();
	} else {
		console.error('Failed to fetch health:', healthRes);
	}
	
	// Parse positions
	let positions: RealtimePositionsResponse = { net: [], day: [] };
	if (positionsRes.status === 'fulfilled' && positionsRes.value.ok) {
		positions = await positionsRes.value.json();
	} else {
		console.error('Failed to fetch positions:', positionsRes);
	}
	
	return {
		strategies,
		health,
		positions
	};
};
