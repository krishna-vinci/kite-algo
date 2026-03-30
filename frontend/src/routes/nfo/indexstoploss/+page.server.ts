/**
 * Server load function for Position Protection System
 * Phase 1: Fetch initial data for SSR
 */

import type { PageServerLoad } from './$types';
import type {
	StrategyListResponse,
	EngineHealthResponse,
	RealtimePositionsResponse
} from './types';

export const load: PageServerLoad = async ({ fetch, cookies }) => {
	// Get session ID from cookies for authentication
	const sessionId = cookies.get('kite_session_id');
	const headers: HeadersInit = {};
	if (sessionId) {
		headers['X-Session-ID'] = sessionId;
	}

	// Fetch strategies, health, and positions in parallel
	const [strategiesRes, healthRes, positionsRes] = await Promise.allSettled([
		fetch('/api/strategies/?limit=50', { credentials: 'include', headers }),
		fetch('/api/strategies/health', { credentials: 'include', headers }),
		fetch('/api/positions/realtime', { credentials: 'include', headers })
	]);

	// Parse strategies
	let strategies: StrategyListResponse = { total: 0, strategies: [] };
	if (strategiesRes.status === 'fulfilled' && strategiesRes.value.ok) {
		strategies = await strategiesRes.value.json();
	} else if (strategiesRes.status === 'fulfilled' && strategiesRes.value.status === 401) {
		console.warn(
			'Strategies API returned 401 - User may not be authenticated. Using empty strategies.'
		);
	} else {
		console.error(
			'Failed to fetch strategies:',
			strategiesRes.status === 'fulfilled' ? strategiesRes.value.statusText : strategiesRes
		);
	}

	// Parse health
	let health: EngineHealthResponse | null = null;
	if (healthRes.status === 'fulfilled' && healthRes.value.ok) {
		health = await healthRes.value.json();
	} else if (healthRes.status === 'fulfilled' && healthRes.value.status === 401) {
		console.warn('Health API returned 401 - User may not be authenticated.');
	} else {
		console.error(
			'Failed to fetch health:',
			healthRes.status === 'fulfilled' ? healthRes.value.statusText : healthRes
		);
	}

	// Parse positions
	let positions: RealtimePositionsResponse = { net: [], day: [] };
	if (positionsRes.status === 'fulfilled' && positionsRes.value.ok) {
		positions = await positionsRes.value.json();
	} else if (positionsRes.status === 'fulfilled' && positionsRes.value.status === 401) {
		console.warn(
			'Positions API returned 401 - User may not be authenticated. Using empty positions.'
		);
	} else {
		console.error(
			'Failed to fetch positions:',
			positionsRes.status === 'fulfilled' ? positionsRes.value.statusText : positionsRes
		);
	}

	return {
		strategies,
		health,
		positions
	};
};
