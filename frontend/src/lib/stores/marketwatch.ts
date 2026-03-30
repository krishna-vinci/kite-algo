import { writable } from 'svelte/store';
import type { Instrument } from '$lib/types';
import { getApiBase } from '$lib/api';

// Extended interface to support live tick data on-the-fly
interface TickData {
	instrument_token: number;
	last_price?: number;
	change?: number;
	exchange_timestamp?: string;
	ohlc?: {
		open?: number;
		high?: number;
		low?: number;
		close?: number;
	};
	volume_traded?: number;
	total_buy_quantity?: number;
	total_sell_quantity?: number;
	depth?: {
		buy: Array<{ price: number; orders: number; quantity: number }>;
		sell: Array<{ price: number; orders: number; quantity: number }>;
	};
	oi?: number;
	oi_day_high?: number;
	oi_day_low?: number;
	last_trade_time?: string;
}

export interface MarketwatchStore {
	instruments: Record<number, TickData>; // Changed to number key and TickData type
	connection: WebSocket | null;
}

function createMarketwatchStore() {
	const { subscribe, set, update } = writable<MarketwatchStore>({
		instruments: {},
		connection: null
	});

	// Singleton connection state
	let ws: WebSocket | null = null;
	let connecting: boolean = false;
	let connected: boolean = false;
	let subscribedTokens: Set<number> = new Set();
	let desiredMode: string | null = null;
	let messageQueue: any[] = []; // Queue messages until connection is open

	// Build WebSocket URL from API base
	function buildWsUrl(): string {
		if (typeof window === 'undefined') {
			// SSR fallback - should not be called in SSR
			return 'ws://localhost:8777/api/ws/marketwatch';
		}
		const base = getApiBase(); // '' or http(s)://host
		// If base is empty or relative, derive from current location
		if (!base || base.startsWith('/')) {
			const loc = window.location;
			const wsProto = loc.protocol === 'https:' ? 'wss' : 'ws';
			return `${wsProto}://${loc.host}/api/ws/marketwatch`;
		}
		// Absolute base provided: convert scheme and construct ws URL
		const wsProto = base.startsWith('https') ? 'wss' : 'ws';
		const wsHost = base.replace(/^https?:\/\//, '');
		return `${wsProto}://${wsHost}/api/ws/marketwatch`;
	}

	// Send action safely when connected, or queue until connection opens
	function sendAction(action: string, payload: any) {
		const message = { action, ...payload };
		if (ws && ws.readyState === WebSocket.OPEN) {
			ws.send(JSON.stringify(message));
		} else {
			// Queue for sending when connection opens
			messageQueue.push(message);
		}
	}

	// Process queued messages when connection opens
	function processMessageQueue() {
		while (messageQueue.length > 0) {
			const message = messageQueue.shift();
			if (ws && ws.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify(message));
			}
		}
	}

	function connect() {
		// Singleton guard: if already connected or connecting, return
		if (
			(ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) ||
			connecting
		) {
			return;
		}

		// Browser-only check
		if (typeof window === 'undefined') {
			return;
		}

		connecting = true;
		connected = false;

		const wsUrl = buildWsUrl();
		ws = new WebSocket(wsUrl);

		ws.onopen = () => {
			console.log('Marketwatch WebSocket connected to', wsUrl);
			connecting = false;
			connected = true;
			update((state) => ({ ...state, connection: ws }));

			// Process any queued messages
			processMessageQueue();

			// Resubscribe to all previously subscribed tokens if any
			if (subscribedTokens.size > 0) {
				const tokens = Array.from(subscribedTokens);
				sendAction('subscribe', { tokens, mode: desiredMode || 'quote' });
			}
		};

		ws.onmessage = (event) => {
			try {
				const msg = JSON.parse(event.data);

				// Handle different message types per backend contract
				if (msg.type === 'ticks' && Array.isArray(msg.data)) {
					// Batch tick data - create/update instrument entries on-the-fly
					update((state) => {
						const newInstruments = { ...state.instruments };
						for (const tick of msg.data) {
							if (tick && typeof tick.instrument_token === 'number') {
								// Create or update instrument entry with tick data
								newInstruments[tick.instrument_token] = tick;
							}
						}
						return { ...state, instruments: newInstruments };
					});
				} else if (msg.type === 'ack') {
					console.debug('WebSocket ACK:', msg);
				} else if (msg.type === 'status') {
					console.debug('WebSocket status:', msg.state);
				} else if (msg.type === 'error') {
					console.error('WebSocket error:', msg.message);
				} else {
					// Legacy: handle raw tick or array of ticks for backward compatibility
					if (Array.isArray(msg)) {
						update((state) => {
							const newInstruments = { ...state.instruments };
							for (const tick of msg) {
								if (tick && typeof tick.instrument_token === 'number') {
									newInstruments[tick.instrument_token] = tick;
								}
							}
							return { ...state, instruments: newInstruments };
						});
					} else if (msg.instrument_token) {
						// Single tick
						update((state) => ({
							...state,
							instruments: {
								...state.instruments,
								[msg.instrument_token]: msg
							}
						}));
					}
				}
			} catch (e) {
				console.error('Failed to parse WebSocket message:', e);
			}
		};

		ws.onclose = (event) => {
			console.log('❌ MARKETWATCH STORE DEBUG: WebSocket disconnected', {
				code: event.code,
				reason: event.reason,
				wasClean: event.wasClean
			});
			connecting = false;
			connected = false;
			ws = null;
			update((state) => ({ ...state, connection: null }));
		};

		ws.onerror = (error) => {
			console.error('❌ MARKETWATCH STORE DEBUG: WebSocket error', {
				error,
				wsUrl,
				readyState: ws?.readyState,
				connecting,
				connected
			});
			connecting = false;
			connected = false;
		};
	}

	function subscribeToInstruments(tokens: number[], mode?: string) {
		if (!tokens || tokens.length === 0) return;

		// Update internal state
		tokens.forEach((token) => subscribedTokens.add(token));
		if (mode) desiredMode = mode;

		// Send subscription message (queued if not connected)
		sendAction('subscribe', {
			tokens,
			mode: desiredMode || 'quote'
		});
	}

	function unsubscribeFromInstruments(tokens: number[]) {
		if (!tokens || tokens.length === 0) return;

		// Update internal state
		tokens.forEach((token) => subscribedTokens.delete(token));

		// Send unsubscription message (queued if not connected)
		sendAction('unsubscribe', { tokens });

		// Remove from local instruments map
		update((state) => {
			const newInstruments = { ...state.instruments };
			tokens.forEach((token) => delete newInstruments[token]);
			return { ...state, instruments: newInstruments };
		});
	}

	function setMode(mode: string) {
		desiredMode = mode;
		if (subscribedTokens.size > 0) {
			const tokens = Array.from(subscribedTokens);
			sendAction('set_mode', { mode, tokens });
			// Optionally resubscribe with new mode
			// sendAction('subscribe', { tokens, mode });
		}
	}

	return {
		subscribe,
		connect,
		subscribeToInstruments,
		unsubscribeFromInstruments,
		setMode,
		// Preserve backward compatibility: allow setting initial instrument data
		setInstruments: (instruments: Instrument[]) => {
			const instrumentMap = instruments.reduce(
				(acc, inst) => {
					// Convert Instrument to TickData format, preserving existing live data if any
					acc[inst.instrument_token] = {
						...acc[inst.instrument_token], // Preserve any existing live data first
						instrument_token: inst.instrument_token,
						last_price: inst.price || 0, // Map price to last_price
						change: inst.change || 0
					};
					return acc;
				},
				{} as Record<number, TickData>
			);
			update((state) => ({
				...state,
				instruments: { ...state.instruments, ...instrumentMap }
			}));
		}
	};
}

export const marketwatch = createMarketwatchStore();
