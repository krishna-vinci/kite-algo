<script lang="ts">
	import { toast } from 'svelte-sonner';
	import Stepper from './stepper.svelte';
	import MergedBuilder from './merged-builder.svelte';
	import Step3ProtectionConfig from './step3-protection-config.svelte';
	import Step4Review from './step4-review.svelte';
	import type { SelectedStrike, BuildPositionResponse, MiniChainResponse } from '../../types';
	import { getMiniChain } from '../../lib/api';

	interface Props {
		onComplete?: (response: BuildPositionResponse) => void;
	}

	let { onComplete }: Props = $props();

	// Wizard state (now step 1 merges old step 1 & 2)
	let currentStep = $state(1);

	// Step 1: Market selection
	let underlying = $state('');
	let expiry = $state('');
	let strategyType = $state('');
	let targetDelta = $state(0.30);
	let selectedTemplate = $state<any>(null); // Strategy template for auto-population

	// Strike selection (handled in merged builder)
	let selectedStrikes = $state<SelectedStrike[]>([]);
	let chainData = $state<MiniChainResponse | null>(null);
	let chainLoading = $state(false);
	let multiplier = $state(1);

	// Step 3: Protection config
	let protectionConfig = $state({
		enabled: true,
		monitoring_mode: 'index' as 'index' | 'premium' | 'hybrid',
		index_tradingsymbol: '',
		index_upper_stoploss: undefined as number | undefined,
		index_lower_stoploss: undefined as number | undefined,
		trailing_enabled: false,
		trailing_distance: undefined as number | undefined
	});

	const steps = [
		{ id: 1, title: 'Build Strategy', description: 'Market, template & strikes' },
		{ id: 2, title: 'Protection', description: 'Configure stop-loss' },
		{ id: 3, title: 'Review & Execute', description: 'Confirm and place orders' }
	];

	async function loadChainData() {
		if (!underlying || !expiry) return;

		chainLoading = true;
		chainData = null;
		try {
			chainData = await getMiniChain(underlying, expiry);
		} catch (e) {
			console.error('Failed to load chain in wizard:', e);
			toast.error('Failed to load option chain. Please try again.');
		} finally {
			chainLoading = false;
		}
	}

	function updateMarketData(data: any) {
		const isContextChanging = data.underlying !== undefined || data.expiry !== undefined || data.selectedTemplate !== undefined;

		if (isContextChanging && selectedStrikes.length > 0) {
			selectedStrikes = [];
		}

		if (data.underlying !== undefined) underlying = data.underlying;
		if (data.expiry !== undefined) expiry = data.expiry;
		if (data.strategyType !== undefined) strategyType = data.strategyType;
		if (data.targetDelta !== undefined) targetDelta = data.targetDelta;
		if (data.selectedTemplate !== undefined) selectedTemplate = data.selectedTemplate;
	}

	$effect(() => {
		if (underlying && expiry) {
			loadChainData();
		}
	});

	function updateProtectionConfig(data: any) {
		protectionConfig = { ...protectionConfig, ...data };
	}

	function handleComplete(response: BuildPositionResponse) {
		toast.success('Position builder complete!');
		
		// Reset wizard
		currentStep = 1;
		underlying = '';
		expiry = '';
		strategyType = '';
		selectedStrikes = [];
		protectionConfig.enabled = true;
		
		if (onComplete) {
			onComplete(response);
		}
	}
</script>

<div class="space-y-6">
	<!-- Stepper -->
	<Stepper steps={steps} currentStep={currentStep} />

	<!-- Main Content -->
	{#if currentStep === 1}
		<!-- Merged Builder: Stage 1 & 2 combined -->
		<MergedBuilder
			underlying={underlying}
			expiry={expiry}
			strategyType={strategyType}
			targetDelta={targetDelta}
			selectedStrikes={selectedStrikes}
			chainData={chainData}
			chainLoading={chainLoading}
			bind:multiplier
			onUpdate={updateMarketData}
			onUpdateSelectedStrikes={(strikes) => selectedStrikes = strikes}
			onNext={() => currentStep = 2}
			onReloadChain={loadChainData}
		/>
	{:else if currentStep === 2}
		<!-- Step 2: Protection Config (old step 3) -->
		<Step3ProtectionConfig
			underlying={underlying}
			selectedStrikes={selectedStrikes}
			protectionConfig={protectionConfig}
			onUpdateProtection={updateProtectionConfig}
			onNext={() => currentStep = 3}
			onBack={() => currentStep = 1}
		/>
	{:else if currentStep === 3}
		<!-- Step 3: Review & Execute (old step 4) -->
		<Step4Review
			underlying={underlying}
			expiry={expiry}
			strategyType={strategyType}
			selectedStrikes={selectedStrikes}
			protectionConfig={protectionConfig}
			multiplier={multiplier}
			onBack={() => currentStep = 2}
			onComplete={handleComplete}
		/>
	{/if}
</div>
