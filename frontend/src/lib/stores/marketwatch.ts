import { writable } from 'svelte/store';
import type { Instrument } from '$lib/types';

const WS_URL = 'ws://localhost:8080/ws';

export interface MarketwatchStore {
	instruments: Record<string, Instrument>;
	connection: WebSocket | null;
}

function createMarketwatchStore() {
	const { subscribe, set, update } = writable<MarketwatchStore>({
		instruments: {},
		connection: null
	});

	function connect() {
		const ws = new WebSocket(WS_URL);

		ws.onopen = () => {
			console.log('WebSocket connected');
			update((state) => ({ ...state, connection: ws }));
		};

		ws.onmessage = (event) => {
			const tick = JSON.parse(event.data);
			update((state) => {
				const instrument = state.instruments[tick.instrument_token];
				if (instrument) {
					instrument.last_price = tick.last_price;
					instrument.change = tick.change;
				}
				return state;
			});
		};

		ws.onclose = () => {
			console.log('WebSocket disconnected');
			update((state) => ({ ...state, connection: null }));
		};

		ws.onerror = (error) => {
			console.error('WebSocket error:', error);
		};
	}

	function subscribeToInstruments(tokens: number[]) {
		update((state) => {
			if (state.connection?.readyState === WebSocket.OPEN) {
				state.connection.send(JSON.stringify({ type: 'subscribe', tokens }));
			}
			return state;
		});
	}

	function unsubscribeFromInstruments(tokens: number[]) {
		update((state) => {
			if (state.connection?.readyState === WebSocket.OPEN) {
				state.connection.send(JSON.stringify({ type: 'unsubscribe', tokens }));
			}
			return state;
		});
	}

	return {
		subscribe,
		connect,
		subscribeToInstruments,
		unsubscribeFromInstruments,
		setInstruments: (instruments: Instrument[]) => {
			const instrumentMap = instruments.reduce(
				(acc, inst) => {
					acc[inst.instrument_token] = inst;
					return acc;
				},
				{} as Record<string, Instrument>
			);
			update((state) => ({ ...state, instruments: instrumentMap }));
		}
	};
}

export const marketwatch = createMarketwatchStore();