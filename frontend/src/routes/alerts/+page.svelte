<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch, getApiBase } from '$lib/api';
	import InstrumentPicker, { type InstrumentRow } from '$lib/components/InstrumentPicker.svelte';
	import CreateAlertModal from '$lib/components/CreateAlertModal.svelte';
	import { toast } from '$lib/stores/toast';

	type AlertRow = {
		uuid: string;
		name: string;
		status: string;
		alert_type: string;
		lhs_exchange?: string;
		lhs_tradingsymbol?: string;
		lhs_attribute?: string;
		operator?: string;
		rhs_type?: string;
		rhs_constant?: number;
		alert_count?: number;
		last_notified_at?: string | null;
		updated_at?: string;
	};

	let loading = false;
	let creating = false;
	let testing = false;
	let alerts: AlertRow[] = [];

	// Modal state
	let showCreateModal = false;

	// Helper to get label from value
	function getOperatorLabel(value: string | undefined) {
		if (!value) return '';
		// This will be moved to the modal, but keep it here for now to avoid breaking the UI
		const operators = [
			{ value: '>=', label: 'Greater than or equal to' },
			{ value: '<=', label: 'Less than or equal to' },
			{ value: '>', label: 'Greater than' },
			{ value: '<', label: 'Less than' },
			{ value: '==', label: 'Equal to' },
			{ value: '!=', label: 'Not equal to' }
		];
		return operators.find((op) => op.value === value)?.label ?? value;
	}
	function getAttributeLabel(value: string | undefined) {
		if (!value) return '';
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
		return attributes.find((a) => a.value === value)?.label ?? value;
	}

	async function loadAlerts(refresh = false) {
		loading = true;
		try {
			const res = await apiFetch(`/broker/alerts${refresh ? '?refresh=true' : ''}`);
			if (!res.ok) {
				const t = await res.text();
				throw new Error(`Failed to load alerts: ${res.status} ${t}`);
			}
			const data = await res.json();
			alerts = (data?.data ?? []) as AlertRow[];
		} catch (e: any) {
			toast.error(e?.message ?? 'Unknown error');
		} finally {
			loading = false;
		}
	}

	async function createAlert() {
		creating = true;
		errorMsg = null;
		successMsg = null;
		try {
			if (!name || !lhs_exchange || !lhs_tradingsymbol || rhs_constant === null) {
				throw new Error('Please fill name, instrument, and threshold.');
			}

			// Validate against backend
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
				// Non-Kite fields we store in DB mirror (cooldown)
				cooldown_sec
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
			await loadAlerts(true);
			successMsg = 'Alert created';
			// Reset minimal inputs
			// keep exchange populated for next create
			name = '';
			lhs_tradingsymbol = '';
			rhs_constant = null;
			percentOfLast = null;
		} catch (e: any) {
			errorMsg = e?.message ?? 'Unknown error';
		} finally {
			creating = false;
		}
	}

	async function deleteAlert(uuid: string) {
		if (!uuid) return;
		if (!confirm('Delete this alert?')) return;
		try {
			const res = await apiFetch(`/broker/alerts/${uuid}`, { method: 'DELETE' });
			if (!res.ok) {
				const t = await res.text();
				throw new Error(`Delete failed: ${res.status} ${t}`);
			}
			alerts = alerts.filter((a) => a.uuid !== uuid);
			toast.success('Alert deleted');
		} catch (e: any) {
			toast.error(e?.message ?? 'Unknown error');
		}
	}

	async function testNotification() {
		testing = true;
		try {
			const res = await apiFetch('/broker/alerts/test-notification', { method: 'POST' });
			if (!res.ok) {
				const t = await res.text();
				throw new Error(`Test notification failed: ${res.status} ${t}`);
			}
			toast.success('Test notification sent (check ntfy: kite-alerts)');
		} catch (e: any) {
			toast.error(e?.message ?? 'Unknown error');
		} finally {
			testing = false;
		}
	}

	onMount(() => {
		loadAlerts(false);
	});
</script>

<section class="max-w-6xl mx-auto p-4 space-y-6">
	<header class="flex items-center justify-between">
		<h1 class="text-2xl font-semibold">Alerts</h1>
		<div class="flex gap-2">
			<button
				class="btn btn-primary flex items-center gap-2"
				on:click={() => (showCreateModal = true)}
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					class="h-5 w-5"
					viewBox="0 0 20 20"
					fill="currentColor"
				>
					<path
						fill-rule="evenodd"
						d="M10 3a1 1 0 011 1v5h5a1 1 0 110 2h-5v5a1 1 0 11-2 0v-5H4a1 1 0 110-2h5V4a1 1 0 011-1z"
						clip-rule="evenodd"
					/>
				</svg>
				Create New Alert
			</button>
			<button
				class="btn btn-secondary flex items-center gap-2"
				on:click={() => loadAlerts(true)}
				disabled={loading}
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					class="h-5 w-5"
					class:animate-spin={loading}
					viewBox="0 0 20 20"
					fill="currentColor"
				>
					<path
						fill-rule="evenodd"
						d="M4 2a1 1 0 011 1v2.101a7.002 7.002 0 0111.601 2.566 1 1 0 11-1.885.666A5.002 5.002 0 005.999 7H9a1 1 0 110 2H4a1 1 0 01-1-1V3a1 1 0 011-1zm.008 9.057a1 1 0 011.276.61A5.002 5.002 0 0014.001 13H11a1 1 0 110-2h5a1 1 0 011 1v5a1 1 0 11-2 0v-2.101a7.002 7.002 0 01-11.601-2.566 1 1 0 01.61-1.276z"
						clip-rule="evenodd"
					/>
				</svg>
				{loading ? 'Refreshing...' : 'Refresh'}
			</button>
			<button class="btn-secondary" on:click={testNotification} disabled={testing}>
				{testing ? 'Testing...' : 'Test Notification'}
			</button>
		</div>
	</header>

	<div class="card">
		<h2 class="text-lg font-medium mb-3">Existing Alerts</h2>
		{#if alerts.length === 0}
			<div class="text-gray-600">No alerts found.</div>
		{:else}
			<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
				{#each alerts as a}
					<div class="card flex flex-col justify-between">
						<div>
							<div class="flex items-center justify-between mb-2">
								<h3 class="font-semibold">{a.name}</h3>
								<span
									class="text-xs px-2 py-1 rounded-full"
									class:bg-green-100={a.status === 'enabled'}
									class:text-green-800={a.status === 'enabled'}
									class:bg-gray-100={a.status !== 'enabled'}
									class:text-gray-800={a.status !== 'enabled'}
								>
									{a.status}
								</span>
							</div>
							<p class="text-sm text-gray-600">
								{getAttributeLabel(a.lhs_attribute)} of {a.lhs_exchange}:{a.lhs_tradingsymbol}
								<br />
								is {getOperatorLabel(a.operator)}
								{a.rhs_constant}
							</p>
							<div class="text-xs text-gray-500 mt-2">
								Triggered: {a.alert_count ?? 0} times
								<br />
								Last notified: {a.last_notified_at ?? '-'}
							</div>
						</div>
						<div class="mt-4 flex justify-end">
							<button
								class="btn btn-danger flex items-center gap-1"
								on:click={() => deleteAlert(a.uuid)}
							>
								<svg
									xmlns="http://www.w3.org/2000/svg"
									class="h-4 w-4"
									viewBox="0 0 20 20"
									fill="currentColor"
								>
									<path
										fill-rule="evenodd"
										d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z"
										clip-rule="evenodd"
									/>
								</svg>
								Delete
							</button>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</div>
</section>

{#if showCreateModal}
	<CreateAlertModal
		on:close={() => (showCreateModal = false)}
		on:alertCreated={() => {
			showCreateModal = false;
			loadAlerts(true);
		}}
	/>
{/if}

<style>
	.btn {
		@apply px-4 py-2 rounded-md font-semibold transition-colors duration-200;
	}
	.btn-primary {
		@apply bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50;
	}
	.btn-secondary {
		@apply bg-gray-200 text-gray-800 hover:bg-gray-300 disabled:opacity-50;
	}
	.btn-danger {
		@apply bg-red-600 text-white hover:bg-red-700 text-sm px-3 py-1;
	}
	.card {
		@apply p-4 border border-gray-200 rounded-lg bg-white shadow-md hover:shadow-lg transition-shadow duration-200;
	}
	.field {
		@apply flex flex-col gap-1;
	}
	.label {
		@apply text-sm font-medium text-gray-700;
	}
	.input {
		@apply border border-gray-300 rounded-md px-3 py-2 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-shadow duration-200;
	}
</style>
