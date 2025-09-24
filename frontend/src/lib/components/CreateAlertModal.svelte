<script lang="ts">
	import { createEventDispatcher, onMount, onDestroy } from 'svelte';
	import { apiFetch, getLtp } from '$lib/api';
	import InstrumentPicker from '$lib/components/InstrumentPicker.svelte';
	import type { InstrumentRow } from '$lib/types';
	import { toast } from '$lib/stores/toast';
	import { marketwatch } from '$lib/stores/marketwatch';

	const dispatch = createEventDispatcher();

	// Form state
	let name = '';
	let lhs_exchange = 'NSE';
	let lhs_tradingsymbol = '';
	let lhs_attribute = 'LastTradedPrice';
	let operator = '>=';
	let rhs_constant: number | null = null;
	let cooldown_sec = 120;
	let creating = false;

	// Instrument picker state
	let selectedInstrument: InstrumentRow | null = null;
	let lastPrice: number | null = null;
	let percentOfLast: number | null = null;

	// WS LTP integration state
	let wsSubscribedToken: number | null = null;
	let ltpFallbackTimer: any = null;
	let fallbackPrice: number | null = null;

	// Derived live price from WS store
	$: livePrice = selectedInstrument
		? $marketwatch.instruments[selectedInstrument.instrument_token]?.last_price ?? null
		: null;
	$: baselineCandidate = livePrice ?? fallbackPrice;

	onMount(() => {
		// Ensure WS is connected while modal is open
		marketwatch.connect();
	});

	onDestroy(() => {
		// Cleanup ephemeral subscription and timers
		if (wsSubscribedToken != null) {
			marketwatch.unsubscribeFromInstruments([wsSubscribedToken]);
			wsSubscribedToken = null;
		}
		if (ltpFallbackTimer) {
			clearTimeout(ltpFallbackTimer);
			ltpFallbackTimer = null;
		}
	});

	// If WS price arrives, cancel fallback timer
	$: if (livePrice != null && ltpFallbackTimer) {
		clearTimeout(ltpFallbackTimer);
		ltpFallbackTimer = null;
	}

	function subscribeLtpFor(token: number) {
		marketwatch.connect();
		marketwatch.subscribeToInstruments([token], 'ltp');
		wsSubscribedToken = token;

		// Start short fallback timer to REST LTP if WS doesn't arrive quickly
		if (ltpFallbackTimer) clearTimeout(ltpFallbackTimer);
		ltpFallbackTimer = setTimeout(() => {
			if (livePrice == null) {
				void fetchLtpForSelected();
			}
		}, 1000);
	}

	function unsubscribePrev() {
		if (wsSubscribedToken != null) {
			marketwatch.unsubscribeFromInstruments([wsSubscribedToken]);
			wsSubscribedToken = null;
		}
	}

	function clearSelectedInstrument() {
		unsubscribePrev();
		selectedInstrument = null;
		fallbackPrice = null;
		percentOfLast = null;
	}

	// Keep threshold in sync when user has entered a percent and baseline updates
	$: if (percentOfLast !== null) {
		recomputeFromPercent();
	}

	const operators = [
		{ value: '>=', label: 'Greater than or equal to' },
		{ value: '<=', label: 'Less than or equal to' },
		{ value: '>', label: 'Greater than' },
		{ value: '<', label: 'Less than' },
		{ value: '==', label: 'Equal to' },
		{ value: '!=', label: 'Not equal to' }
	];

	const attributes = [
		{ value: 'LastTradedPrice', label: 'Last price' },
		{ value: 'HighPrice', label: 'High price' },
		{ value: 'LowPrice', label: 'Low price' },
		{ value: 'OpenPrice', label: 'Open price' },
		{ value: 'ClosePrice', label: 'Close price' },
		{ value: 'DayChange', label: 'Day change' },
		{ value: 'DayChangePercent', label: 'Day change %' },
		{ value: 'IntradayChange', label: 'Intraday change' },
		{ value: 'IntradayChangePercent', label: 'Intraday change %' },
		{ value: 'LastTradedQuantity', label: 'Last traded qty.' },
		{ value: 'AverageTradedPrice', label: 'Avg. traded price' },
		{ value: 'Volume', label: 'Volume' },
		{ value: 'TotalBuyQuantity', label: 'Total buy qty.' },
		{ value: 'TotalSellQuantity', label: 'Total sell qty.' },
		{ value: 'OI', label: 'OI' },
		{ value: 'OIDayHigh', label: 'OI day high' },
		{ value: 'OIDayLow', label: 'OI day low' }
	];

	async function fetchLtpForSelected() {
		try {
			const ex = (selectedInstrument?.exchange || lhs_exchange || '').trim();
			const ts = (selectedInstrument?.tradingsymbol || lhs_tradingsymbol || '').trim();
			if (!ex || !ts) {
				fallbackPrice = null;
				return;
			}
			// Use typed helper; resolves one-shot LTP
			const price = await getLtp(ex, ts);
			fallbackPrice = price;
		} catch {
			fallbackPrice = null;
		}
	}

	function handleInstrumentSelect(e: CustomEvent<{ instrument: InstrumentRow }>) {
		const instrument = e.detail.instrument;

		// Unsubscribe previous token if any
		unsubscribePrev();

		selectedInstrument = instrument;
		lhs_exchange = instrument.exchange || lhs_exchange;
		lhs_tradingsymbol = instrument.tradingsymbol || lhs_tradingsymbol;

		// Reset local baselines
		fallbackPrice = null;
		lastPrice = null;
		percentOfLast = null;

		// Ephemeral subscribe WS in ltp mode for this token
		if (instrument.instrument_token) {
			subscribeLtpFor(instrument.instrument_token);
		}
	}

	function recomputeFromPercent() {
		const base = baselineCandidate;
		if (base === null || percentOfLast === null || isNaN(percentOfLast)) return;
		const p = Number(percentOfLast) / 100;
		let v = base;
		if (operator === '>=' || operator === '>') v = base * (1 + p);
		else if (operator === '<=' || operator === '<') v = base * (1 - p);
		else v = base;
		rhs_constant = Math.round(v * 100) / 100;
	}

	async function createAlert() {
		creating = true;
		try {
			if (!name || !lhs_exchange || !lhs_tradingsymbol || rhs_constant === null) {
				throw new Error('Please fill name, instrument, and threshold.');
			}

			// Resolve baseline at submit time (prefer WS, fallback to REST)
			let baseline_price: number | null = baselineCandidate;
			if (baseline_price === null) {
				await fetchLtpForSelected();
				baseline_price = baselineCandidate;
			}
			if (baseline_price === null) {
				throw new Error('Waiting for live price to anchor baseline…');
			}

			const validateRes = await apiFetch('/broker/alerts/validate', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					lhs_exchange,
					lhs_tradingsymbol,
					operator,
					rhs_type: 'constant',
					rhs_constant
				})
			});
			const v = await validateRes.json().catch(() => null);
			if (!validateRes.ok || v?.valid === false) {
				throw new Error(
					v?.reason ? `Validation failed: ${v.reason}` : `Validation failed (${validateRes.status})`
				);
			}

			const body = {
				name,
				lhs_exchange,
				lhs_tradingsymbol,
				lhs_attribute,
				operator,
				rhs_type: 'constant',
				type: 'simple',
				rhs_constant,
				cooldown_sec,
				baseline_price // snake_case to match backend
			};
			const res = await apiFetch('/broker/alerts', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(body)
			});
			if (!res.ok) {
				const t = await res.text();
				throw new Error(`Create failed: ${res.status} ${t}`);
			}
			toast.success('Alert created');
			dispatch('alertCreated');
		} catch (e: any) {
			toast.error(e?.message ?? 'Unknown error');
		} finally {
			creating = false;
		}
	}
</script>

<div
	class="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50"
	on:click={() => dispatch('close')}
>
	<div class="w-full max-w-3xl rounded-lg bg-white p-8 shadow-xl" on:click|stopPropagation>
		<header class="mb-6 flex items-center justify-between">
			<h2 class="text-xl font-semibold text-gray-800">Create New Alert</h2>
			<button on:click={() => dispatch('close')} class="text-gray-500 hover:text-gray-800" aria-label="Close dialog">
				<svg
					xmlns="http://www.w3.org/2000/svg"
					class="h-6 w-6"
					fill="none"
					viewBox="0 0 24 24"
					stroke="currentColor"
				>
					<path
						stroke-linecap="round"
						stroke-linejoin="round"
						stroke-width="2"
						d="M6 18L18 6M6 6l12 12"
					/>
				</svg>
			</button>
		</header>

		<div class="space-y-6">
			<input
				class="input w-full"
				bind:value={name}
				placeholder="Alert name, e.g., NIFTY crossing 25000"
			/>

			<div class="flex items-end gap-4">
				<div class="field w-1/4">
					<span class="label">If</span>
					<select class="input" bind:value={lhs_attribute}>
						{#each attributes as a}
							<option value={a.value}>{a.label}</option>
						{/each}
					</select>
				</div>

				<div class="field w-1/4">
					<span class="label">of</span>
					{#if !selectedInstrument}
						<InstrumentPicker on:select={handleInstrumentSelect} />
					{:else}
						<div class="flex h-[42px] items-end gap-3">
							<div>
								<div class="font-semibold leading-tight">{selectedInstrument.tradingsymbol}</div>
								<div class="text-xs text-gray-500">({selectedInstrument.exchange})</div>
							</div>
							<div class="text-xs text-gray-700">
								LTP:
								{#if livePrice !== null}
									<span class="font-semibold">{livePrice.toFixed(2)}</span>
									<span class="ml-1 text-[10px] text-green-700">(WS)</span>
								{:else if fallbackPrice !== null}
									<span class="font-semibold">{fallbackPrice.toFixed(2)}</span>
									<span class="ml-1 text-[10px] text-amber-700">(REST)</span>
								{:else}
									<span class="text-gray-400">—</span>
								{/if}
							</div>
							<button
								class="text-xs text-blue-600 hover:underline"
								on:click={clearSelectedInstrument}>Change</button
							>
						</div>
					{/if}
				</div>

				<div class="field w-1/4">
					<span class="label">is</span>
					<select class="input" bind:value={operator} on:change={recomputeFromPercent}>
						{#each operators as op}
							<option value={op.value}>{op.label}</option>
						{/each}
					</select>
				</div>

				<div class="field w-1/4">
					<input
						class="input"
						type="number"
						step="any"
						bind:value={rhs_constant}
						placeholder="e.g., 25000"
					/>
				</div>
			</div>

			<div class="grid grid-cols-2 gap-4 pt-4">
				<label class="field">
					<span class="label">Cooldown (seconds)</span>
					<input class="input" type="number" bind:value={cooldown_sec} min="0" />
				</label>
				<label class="field">
					<span class="label">% of Last price (optional)</span>
					<input
						class="input"
						type="number"
						step="any"
						bind:value={percentOfLast}
						on:input={recomputeFromPercent}
						placeholder="e.g., 1.5"
					/>
				</label>
			</div>
		</div>

		<footer class="mt-8 flex justify-end gap-4 border-t pt-6">
			<button class="btn btn-secondary" on:click={() => dispatch('close')}>Cancel</button>
			<button class="btn btn-primary" on:click={createAlert} disabled={creating || !selectedInstrument || rhs_constant === null || baselineCandidate === null}>
				{creating ? 'Creating...' : 'Create'}
			</button>
		</footer>
	</div>
</div>

<style>
	@reference "tailwindcss";
	.btn {
		@apply rounded-md px-6 py-2 text-sm font-semibold shadow-sm transition-all duration-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2;
	}
	.btn-primary {
		@apply bg-blue-600 text-white hover:bg-blue-700 focus-visible:outline-blue-600 disabled:cursor-not-allowed disabled:opacity-50;
	}
	.btn-secondary {
		@apply border-0 bg-transparent text-gray-700 hover:bg-gray-100;
	}
	.field {
		@apply flex flex-col gap-1;
	}
	.label {
		@apply text-sm font-medium text-gray-600;
	}
	.input {
		@apply block w-full rounded-md border-gray-300 px-3 py-2 text-gray-900 shadow-sm ring-1 ring-inset ring-gray-300 transition-shadow duration-200 placeholder:text-gray-400 focus:ring-2 focus:ring-inset focus:ring-blue-600 sm:text-sm sm:leading-6;
	}
</style>
