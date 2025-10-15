/**
 * Candle Stream Manager - Handles SSE connections for real-time candle updates
 * Features:
 * - Automatic reconnection with exponential backoff
 * - Multiple concurrent streams
 * - Snapshot + incremental updates
 * - Connection state management
 * - Error recovery
 */

import { buildCandleStreamUrl, normalizeTimeframe } from '$lib/api';
import type { Candle } from '$lib/api';

export type StreamState = 'connecting' | 'connected' | 'disconnected' | 'error';

export interface StreamSnapshot {
	instrument_token: number;
	interval: string;
	candles: any[]; // Raw candle arrays from SSE
}

export interface StreamCandle {
	event: 'candle';
	instrument_token: number;
	interval: string;
	candle: any[]; // Raw candle array [time, open, high, low, close, volume, oi?]
}

interface StreamCallbacks {
	onSnapshot?: (snapshot: StreamSnapshot) => void;
	onCandle?: (candle: StreamCandle) => void;
	onTick?: (tick: StreamCandle) => void; // For forming candle updates
	onStateChange?: (state: StreamState) => void;
	onError?: (error: Error) => void;
}

class CandleStream {
	private eventSource: EventSource | null = null;
	private state: StreamState = 'disconnected';
	private callbacks: StreamCallbacks;
	private reconnectAttempts = 0;
	private maxReconnectAttempts = 10;
	private reconnectDelay = 1000; // Start with 1 second
	private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	private manualClose = false;

	constructor(
		private identifier: string | number,
		private timeframe: string,
		callbacks: StreamCallbacks
	) {
		this.callbacks = callbacks;
	}

	/**
	 * Start the stream
	 */
	start(): void {
		if (this.eventSource) {
			console.warn('Stream already started');
			return;
		}

		this.manualClose = false;
		this.connect();
	}

	/**
	 * Stop the stream
	 */
	stop(): void {
		this.manualClose = true;
		this.cleanup();
		this.setState('disconnected');
	}

	/**
	 * Get current state
	 */
	getState(): StreamState {
		return this.state;
	}

	private connect(): void {
		try {
			this.setState('connecting');

			const url = buildCandleStreamUrl(this.identifier, this.timeframe);
			this.eventSource = new EventSource(url, { withCredentials: true });

			this.eventSource.addEventListener('open', () => {
				console.log(`[CandleStream] Connected: ${this.identifier}|${this.timeframe}`);
				this.setState('connected');
				this.reconnectAttempts = 0;
				this.reconnectDelay = 1000;
			});

			this.eventSource.addEventListener('snapshot', (event) => {
				try {
					const data = JSON.parse(event.data) as StreamSnapshot;
					this.callbacks.onSnapshot?.(data);
				} catch (error) {
					console.error('[CandleStream] Failed to parse snapshot:', error);
					this.callbacks.onError?.(error as Error);
				}
			});

			this.eventSource.addEventListener('candle', (event) => {
				try {
					const data = JSON.parse(event.data) as StreamCandle;
					this.callbacks.onCandle?.(data);
				} catch (error) {
					console.error('[CandleStream] Failed to parse candle:', error);
					this.callbacks.onError?.(error as Error);
				}
			});

			this.eventSource.addEventListener('tick', (event) => {
				try {
					const data = JSON.parse(event.data) as StreamCandle;
					this.callbacks.onTick?.(data);
				} catch (error) {
					console.error('[CandleStream] Failed to parse tick:', error);
					this.callbacks.onError?.(error as Error);
				}
			});

			this.eventSource.addEventListener('error', (event) => {
				console.error('[CandleStream] Connection error:', event);
				
				if (this.eventSource?.readyState === EventSource.CLOSED) {
					this.setState('disconnected');
					this.cleanup();

					// Attempt reconnection if not manually closed
					if (!this.manualClose) {
						this.scheduleReconnect();
					}
				} else {
					this.setState('error');
				}
			});
		} catch (error) {
			console.error('[CandleStream] Failed to connect:', error);
			this.setState('error');
			this.callbacks.onError?.(error as Error);

			if (!this.manualClose) {
				this.scheduleReconnect();
			}
		}
	}

	private scheduleReconnect(): void {
		if (this.reconnectAttempts >= this.maxReconnectAttempts) {
			console.error('[CandleStream] Max reconnect attempts reached');
			this.setState('error');
			this.callbacks.onError?.(new Error('Max reconnection attempts reached'));
			return;
		}

		this.reconnectAttempts++;
		const delay = Math.min(this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1), 30000);

		console.log(
			`[CandleStream] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`
		);

		this.reconnectTimer = setTimeout(() => {
			this.connect();
		}, delay);
	}

	private cleanup(): void {
		if (this.reconnectTimer) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}

		if (this.eventSource) {
			this.eventSource.close();
			this.eventSource = null;
		}
	}

	private setState(newState: StreamState): void {
		if (this.state !== newState) {
			this.state = newState;
			this.callbacks.onStateChange?.(newState);
		}
	}
}

/**
 * Candle Stream Manager - Manages multiple concurrent streams
 */
export class CandleStreamManager {
	private streams = new Map<string, CandleStream>();

	/**
	 * Create a unique key for a stream
	 */
	private getKey(identifier: string | number, timeframe: string): string {
		const normalized = normalizeTimeframe(timeframe);
		return `${identifier}|${normalized}`;
	}

	/**
	 * Subscribe to a candle stream
	 */
	subscribe(
		identifier: string | number,
		timeframe: string,
		callbacks: StreamCallbacks
	): () => void {
		const key = this.getKey(identifier, timeframe);

		// If already subscribed, stop the old stream
		if (this.streams.has(key)) {
			this.unsubscribe(identifier, timeframe);
		}

		const stream = new CandleStream(identifier, timeframe, callbacks);
		this.streams.set(key, stream);
		stream.start();

		// Return unsubscribe function
		return () => this.unsubscribe(identifier, timeframe);
	}

	/**
	 * Unsubscribe from a candle stream
	 */
	unsubscribe(identifier: string | number, timeframe: string): void {
		const key = this.getKey(identifier, timeframe);
		const stream = this.streams.get(key);

		if (stream) {
			stream.stop();
			this.streams.delete(key);
		}
	}

	/**
	 * Get stream state
	 */
	getStreamState(identifier: string | number, timeframe: string): StreamState | null {
		const key = this.getKey(identifier, timeframe);
		const stream = this.streams.get(key);
		return stream ? stream.getState() : null;
	}

	/**
	 * Check if subscribed to a stream
	 */
	isSubscribed(identifier: string | number, timeframe: string): boolean {
		const key = this.getKey(identifier, timeframe);
		return this.streams.has(key);
	}

	/**
	 * Unsubscribe from all streams
	 */
	unsubscribeAll(): void {
		for (const stream of this.streams.values()) {
			stream.stop();
		}
		this.streams.clear();
	}

	/**
	 * Get active stream count
	 */
	getActiveStreamCount(): number {
		return this.streams.size;
	}
}

/**
 * Parse raw candle array from SSE into Candle object
 */
export function parseRawCandle(raw: any[]): Candle {
	return {
		time: new Date(raw[0]).getTime() / 1000, // Convert ISO string to epoch seconds
		open: Number(raw[1]),
		high: Number(raw[2]),
		low: Number(raw[3]),
		close: Number(raw[4]),
		volume: Number(raw[5]),
		oi: raw[6] ? Number(raw[6]) : undefined
	};
}

/**
 * Parse multiple raw candles from snapshot
 */
export function parseRawCandles(rawCandles: any[]): Candle[] {
	return rawCandles.map(parseRawCandle);
}
