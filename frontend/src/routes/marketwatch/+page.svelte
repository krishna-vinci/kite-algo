<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { browser } from '$app/environment';
  import { getApiBase } from '$lib/api';

  interface Instrument {
    instrument_token: number;
    tradingsymbol: string;
    name: string;
    exchange: string;
    instrument_type: string;
  }

  type Mode = 'ltp' | 'quote' | 'full';

  interface OHLC {
    open?: number;
    high?: number;
    low?: number;
    close?: number;
  }

  interface DepthLevel {
    price: number;
    orders: number;
    quantity: number;
  }

  interface Tick {
    instrument_token: number;
    last_price?: number;
    change?: number;
    exchange_timestamp?: string;
    ohlc?: OHLC;
    volume_traded?: number;
    total_buy_quantity?: number;
    total_sell_quantity?: number;
    depth?: {
      buy: DepthLevel[];
      sell: DepthLevel[];
    };
    oi?: number;
    oi_day_high?: number;
    oi_day_low?: number;
    last_trade_time?: string;
  }

  let socket: WebSocket | null = null;
  let searchInput: string = '';
  let searchResults: Instrument[] = [];

  // Subscribed instruments and desired modes per instrument
  let subscribedInstruments: Map<number, Instrument> = new Map();
  let desiredModes: Map<number, Mode> = new Map(); // default 'quote' when subscribing

  // Live ticks keyed by instrument_token
  let liveTicks: Map<number, Tick> = new Map();

  // CONNECTING, CONNECTED, DISCONNECTED, ERROR, RECONNECTING
  let websocketStatus: string = 'DISCONNECTED';

  // Resolve backend base and ws URL
  function buildWsUrl(): string {
    const base = getApiBase(); // e.g. http://host:8777
    const wsProto = base.startsWith('https') ? 'wss' : 'ws';
    return base.replace(/^http/, wsProto) + '/broker/ws/marketwatch';
  }

  // Fetch instruments search results
  async function fetchSearchResults(query: string) {
    if (query.length < 2) {
      searchResults = [];
      return;
    }
    try {
      const url = `${getApiBase()}/broker/instruments/fuzzy-search?query=${encodeURIComponent(query)}`;
      const response = await fetch(url, { credentials: 'include' });
      if (response.ok) {
        searchResults = await response.json();
      } else {
        console.error('Failed to fetch search results:', response.status, response.statusText);
        searchResults = [];
      }
    } catch (error) {
      console.error('Error fetching search results:', error);
      searchResults = [];
    }
  }

  // Debounce search
  let searchTimeout: ReturnType<typeof setTimeout>;
  $: {
    clearTimeout(searchTimeout);
    if (searchInput) {
      searchTimeout = setTimeout(() => {
        fetchSearchResults(searchInput);
      }, 300);
    } else {
      searchResults = [];
    }
  }

  // Persist subscriptions to localStorage
  function saveSubscriptions() {
    if (!browser) return;
    try {
      const subs = Array.from(subscribedInstruments.entries());
      const modes = Array.from(desiredModes.entries());
      localStorage.setItem('marketwatch_subscriptions', JSON.stringify(subs));
      localStorage.setItem('marketwatch_modes', JSON.stringify(modes));
    } catch (e) {
      console.error('Failed to save subscriptions to localStorage:', e);
    }
  }

  function subscribe(instrument: Instrument) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      console.warn('WebSocket not connected. Cannot subscribe.');
      return;
    }
    const token = instrument.instrument_token;
    // Default mode is quote
    desiredModes.set(token, 'quote');
    subscribedInstruments.set(token, instrument);
    subscribedInstruments = new Map(subscribedInstruments); // Trigger reactivity
    saveSubscriptions();
    socket.send(JSON.stringify({ action: 'subscribe', tokens: [token], mode: 'quote' }));
  }

  function unsubscribe(instrument_token: number) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      console.warn('WebSocket not connected. Cannot unsubscribe.');
      return;
    }
    socket.send(JSON.stringify({ action: 'unsubscribe', tokens: [instrument_token] }));
    subscribedInstruments.delete(instrument_token);
    desiredModes.delete(instrument_token);
    liveTicks.delete(instrument_token);
    saveSubscriptions();
    // trigger reactivity
    liveTicks = new Map(liveTicks);
    subscribedInstruments = new Map(subscribedInstruments);
    desiredModes = new Map(desiredModes);
  }

  function setMode(instrument_token: number, mode: Mode) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      console.warn('WebSocket not connected. Cannot set mode.');
      return;
    }
    if (!subscribedInstruments.has(instrument_token)) {
      console.warn('Instrument not subscribed; subscribe first.');
      return;
    }
    desiredModes.set(instrument_token, mode);
    desiredModes = new Map(desiredModes);
    saveSubscriptions();
    socket.send(JSON.stringify({ action: 'set_mode', tokens: [instrument_token], mode }));
  }

  function resubscribeAll() {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const tokens = Array.from(subscribedInstruments.keys());
    if (tokens.length === 0) return;

    // Subscribe all with default quote first
    socket.send(JSON.stringify({ action: 'subscribe', tokens, mode: 'quote' }));

    // Then upgrade those that desire full
    const fullTokens = tokens.filter((t) => desiredModes.get(t) === 'full');
    if (fullTokens.length > 0) {
      socket.send(JSON.stringify({ action: 'set_mode', tokens: fullTokens, mode: 'full' }));
    }
  }

  function handleIncomingMessage(ev: MessageEvent) {
    try {
      const msg = JSON.parse(ev.data);
      if (!msg) {
        console.warn('Received empty message');
        return;
      }

      const type = msg.type;
      if (!type) {
        // Backward compatibility: if server sent raw array of ticks
        if (Array.isArray(msg)) {
          const arr: Tick[] = msg;
          for (const tick of arr) {
            if (tick && typeof tick.instrument_token === 'number') {
              liveTicks.set(tick.instrument_token, tick);
            }
          }
          liveTicks = new Map(liveTicks);
        }
        return;
      }

      if (type === 'status') {
        websocketStatus = msg.state ?? websocketStatus;
        return;
      }

      if (type === 'ack') {
        // Could reflect UI state; for now just log
        console.debug('ACK:', msg);
        return;
      }

      if (type === 'error') {
        console.error('WS error:', msg.message);
        return;
      }

      if (type === 'ticks' && Array.isArray(msg.data)) {
        const arr: Tick[] = msg.data;
        for (const tick of arr) {
          if (tick && typeof tick.instrument_token === 'number') {
            liveTicks.set(tick.instrument_token, tick);
          }
        }
        liveTicks = new Map(liveTicks);
        return;
      }
      console.warn('Unhandled message type:', type, msg);
    } catch (e) {
      console.error('Failed to parse WS message:', e);
    }
  }

  onMount(() => {
    if (!browser) return;

    // Load subscriptions from localStorage
    try {
      const storedSubs = localStorage.getItem('marketwatch_subscriptions');
      const storedModes = localStorage.getItem('marketwatch_modes');
      if (storedSubs) {
        subscribedInstruments = new Map(JSON.parse(storedSubs));
      }
      if (storedModes) {
        desiredModes = new Map(JSON.parse(storedModes));
      }
    } catch (e) {
      console.error('Failed to load subscriptions from localStorage:', e);
    }

    const wsUrl = buildWsUrl();
    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
      console.log('WebSocket connection established');
      websocketStatus = 'CONNECTED';
      resubscribeAll();
    }

    socket.onmessage = handleIncomingMessage;

    socket.onclose = (event) => {
      console.log('WebSocket connection closed:', event.code, event.reason);
      websocketStatus = 'DISCONNECTED';
    };

    socket.onerror = (error) => {
      console.error('WebSocket error:', error);
      websocketStatus = 'ERROR';
    };
  });

  onDestroy(() => {
    if (socket) {
      try {
        socket.close();
      } catch {
        // ignore
      }
    }
  });

  // Helper to format change percentage
  function formatChange(change: number | undefined): string {
    if (change === undefined || change === null) return 'N/A';
    const sign = change >= 0 ? '+' : '';
    return `${sign}${change.toFixed(2)}%`;
  }
</script>

<div class="container mx-auto p-4">
  <h1 class="text-2xl font-bold mb-4">Market Watch</h1>

  <div class="mb-4">
    <p>
      WebSocket Status:
      <span class="font-semibold {websocketStatus === 'CONNECTED' ? 'text-green-500' : websocketStatus === 'DISCONNECTED' ? 'text-red-500' : 'text-yellow-500'}">
        {websocketStatus}
      </span>
    </p>
  </div>

  <div class="mb-6">
    <h2 class="text-xl font-semibold mb-2">Search Instruments</h2>
    <input
      type="text"
      bind:value={searchInput}
      placeholder="Search by symbol or name (e.g., RELIANCE, NIFTY)"
      class="border p-2 rounded w-full md:w-1/2"
    />

    {#if searchResults.length > 0}
      <div class="mt-4 border rounded shadow-md max-h-60 overflow-y-auto">
        {#each searchResults as instrument (instrument.instrument_token)}
          <div class="flex justify-between items-center p-2 border-b last:border-b-0 hover:bg-gray-50">
            <div>
              <p class="font-medium">{instrument.tradingsymbol} - {instrument.name}</p>
              <p class="text-sm text-gray-500">
                {instrument.exchange}:{instrument.instrument_type} (Token: {instrument.instrument_token})
              </p>
            </div>
            <button
              on:click={() => subscribe(instrument)}
              disabled={subscribedInstruments.has(instrument.instrument_token)}
              class="bg-blue-500 text-white p-2 rounded text-sm disabled:bg-gray-400"
            >
              {#if subscribedInstruments.has(instrument.instrument_token)}
                Subscribed
              {:else}
                Subscribe (quote)
              {/if}
            </button>
          </div>
        {/each}
      </div>
    {/if}
  </div>

  <div class="mb-6">
    <h2 class="text-xl font-semibold mb-2">Subscribed Instruments</h2>
    {#if subscribedInstruments.size === 0}
      <p class="text-gray-600">No instruments subscribed yet. Search and subscribe above!</p>
    {:else}
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {#each Array.from(subscribedInstruments.values()) as instrument (instrument.instrument_token)}
          <div class="border p-4 rounded shadow-md">
            <div class="flex justify-between items-center mb-2">
              <h3 class="font-bold text-lg">{instrument.tradingsymbol}</h3>
              <button
                on:click={() => unsubscribe(instrument.instrument_token)}
                class="bg-red-500 text-white p-1 px-2 rounded text-sm"
              >
                Unsubscribe
              </button>
            </div>
            <p class="text-gray-700">{instrument.name}</p>
            <p class="text-sm text-gray-500">{instrument.exchange}:{instrument.instrument_type}</p>

            <div class="mt-2 flex items-center gap-2">
              <span class="text-sm text-gray-600">Mode:</span>
              <button
                class="px-2 py-1 rounded text-xs border {desiredModes.get(instrument.instrument_token) === 'quote' ? 'bg-blue-500 text-white' : 'bg-white'}"
                on:click={() => setMode(instrument.instrument_token, 'quote')}
              >
                Quote
              </button>
              <button
                class="px-2 py-1 rounded text-xs border {desiredModes.get(instrument.instrument_token) === 'full' ? 'bg-blue-500 text-white' : 'bg-white'}"
                on:click={() => setMode(instrument.instrument_token, 'full')}
              >
                Full
              </button>
            </div>

            {#if liveTicks.has(instrument.instrument_token)}
              {@const tick = liveTicks.get(instrument.instrument_token)}
              <div class="mt-3 space-y-1">
                <p><strong>LTP:</strong> {tick?.last_price !== undefined ? tick.last_price.toFixed(2) : 'N/A'}</p>
                <p>
                  <strong>Change:</strong>
                  <span class="{tick?.change !== undefined && tick.change >= 0 ? 'text-green-600' : 'text-red-600'}">
                    {formatChange(tick?.change)}
                  </span>
                </p>
                <p><strong>Volume:</strong> {tick?.volume_traded ?? 'N/A'}</p>
                {#if tick?.ohlc}
                  <p class="text-sm text-gray-600">
                    <strong>OHLC:</strong>
                    O {tick.ohlc.open ?? '-'} H {tick.ohlc.high ?? '-'} L {tick.ohlc.low ?? '-'} C {tick.ohlc.close ?? '-'}
                  </p>
                {/if}
              </div>

              {#if tick?.depth}
                <div class="mt-3 grid grid-cols-2 gap-4">
                  <div>
                    <p class="font-semibold text-sm mb-1">Buy Depth</p>
                    <table class="text-xs w-full">
                      <thead>
                        <tr class="text-gray-500">
                          <th class="text-left">Qty</th>
                          <th class="text-left">Price</th>
                          <th class="text-left">Orders</th>
                        </tr>
                      </thead>
                      <tbody>
                        {#each tick.depth.buy.slice(0, 5) as lvl}
                          <tr>
                            <td>{lvl.quantity}</td>
                            <td>{lvl.price}</td>
                            <td>{lvl.orders}</td>
                          </tr>
                        {/each}
                      </tbody>
                    </table>
                  </div>
                  <div>
                    <p class="font-semibold text-sm mb-1">Sell Depth</p>
                    <table class="text-xs w-full">
                      <thead>
                        <tr class="text-gray-500">
                          <th class="text-left">Qty</th>
                          <th class="text-left">Price</th>
                          <th class="text-left">Orders</th>
                        </tr>
                      </thead>
                      <tbody>
                        {#each tick.depth.sell.slice(0, 5) as lvl}
                          <tr>
                            <td>{lvl.quantity}</td>
                            <td>{lvl.price}</td>
                            <td>{lvl.orders}</td>
                          </tr>
                        {/each}
                      </tbody>
                    </table>
                  </div>
                </div>
              {/if}
            {:else}
              <p class="mt-2 text-gray-500">Waiting for data...</p>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>
</div>