/**
 * SSE (Server-Sent Events) helper for real-time position updates
 * Phase 1: Position streaming integration
 */

import type { RealtimePosition } from '../types';

export interface SSEPositionUpdate {
	net: RealtimePosition[];
	day: RealtimePosition[];
	timestamp: string;
}

export type SSEEventHandler = (update: SSEPositionUpdate) => void;
export type SSEErrorHandler = (error: Event) => void;

/**
 * Create and manage SSE connection for position updates
 */
export class PositionStreamManager {
	private eventSource: EventSource | null = null;
	private reconnectAttempts = 0;
	private maxReconnectAttempts = 5;
	private reconnectDelay = 1000; // Start with 1 second
	private maxReconnectDelay = 30000; // Max 30 seconds
	
	constructor(
		private url: string,
		private onUpdate: SSEEventHandler,
		private onError?: SSEErrorHandler
	) {}
	
	/**
	 * Connect to SSE stream
	 */
	connect(): void {
		if (this.eventSource) {
			console.warn('SSE connection already exists');
			return;
		}
		
		console.log('Connecting to position stream:', this.url);
		
		this.eventSource = new EventSource(this.url);
		
		// Handle incoming messages
		this.eventSource.onmessage = (event) => {
			try {
				const data = JSON.parse(event.data);
				this.onUpdate(data);
				this.reconnectAttempts = 0; // Reset on successful message
			} catch (error) {
				console.error('Failed to parse SSE message:', error);
			}
		};
		
		// Handle errors
		this.eventSource.onerror = (error) => {
			console.error('SSE connection error:', error);
			
			if (this.onError) {
				this.onError(error);
			}
			
			// Attempt reconnection with exponential backoff
			this.handleReconnect();
		};
		
		// Handle connection open
		this.eventSource.onopen = () => {
			console.log('SSE connection established');
			this.reconnectAttempts = 0;
		};
	}
	
	/**
	 * Handle reconnection with exponential backoff
	 */
	private handleReconnect(): void {
		if (this.reconnectAttempts >= this.maxReconnectAttempts) {
			console.error('Max reconnection attempts reached');
			this.disconnect();
			return;
		}
		
		this.reconnectAttempts++;
		const delay = Math.min(
			this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1),
			this.maxReconnectDelay
		);
		
		console.log(`Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`);
		
		setTimeout(() => {
			if (this.eventSource) {
				this.eventSource.close();
				this.eventSource = null;
			}
			this.connect();
		}, delay);
	}
	
	/**
	 * Disconnect from SSE stream
	 */
	disconnect(): void {
		if (this.eventSource) {
			console.log('Disconnecting from position stream');
			this.eventSource.close();
			this.eventSource = null;
		}
	}
	
	/**
	 * Check if connected
	 */
	isConnected(): boolean {
		return this.eventSource !== null && this.eventSource.readyState === EventSource.OPEN;
	}
	
	/**
	 * Get connection state
	 */
	getState(): number {
		return this.eventSource?.readyState ?? EventSource.CLOSED;
	}
}

/**
 * Create a position stream manager
 */
export function createPositionStream(
	url: string,
	onUpdate: SSEEventHandler,
	onError?: SSEErrorHandler
): PositionStreamManager {
	return new PositionStreamManager(url, onUpdate, onError);
}
