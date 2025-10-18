<script lang="ts">
	import { toast } from 'svelte-sonner';
	import Stepper from './stepper.svelte';
	import Step1SelectMarket from './step1-select-market.svelte';
	import Step2MiniChain from './step2-mini-chain.svelte';
	import Step3ProtectionConfig from './step3-protection-config.svelte';
	import Step4Review from './step4-review.svelte';
	import PositionPlanPreview from './position-plan-preview.svelte';
	import type { SelectedStrike, BuildPositionResponse } from '../../types';

	interface Props {
		onComplete?: (response: BuildPositionResponse) => void;
	}

	let { onComplete }: Props = $props();

	// Wizard state
	let currentStep = $state(1);

	// Step 1: Market selection
	let underlying = $state('');
	let expiry = $state('');
	let strategyType = $state('');
	let targetDelta = $state(0.30);

	// Step 2: Strike selection
	let selectedStrikes = $state<SelectedStrike[]>([]);

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
		{ id: 1, title: 'Select Market', description: 'Choose underlying & expiry' },
		{ id: 2, title: 'Select Strikes', description: 'View chain & select options' },
		{ id: 3, title: 'Protection', description: 'Configure stop-loss' },
		{ id: 4, title: 'Review & Execute', description: 'Confirm and place orders' }
	];

	function updateMarketData(data: any) {
		if (data.underlying !== undefined) underlying = data.underlying;
		if (data.expiry !== undefined) expiry = data.expiry;
		if (data.strategyType !== undefined) strategyType = data.strategyType;
		if (data.targetDelta !== undefined) targetDelta = data.targetDelta;
	}

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

	<!-- Main Content with Sidebar -->
	<div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
		<!-- Steps Content -->
		<div class="lg:col-span-2">
			{#if currentStep === 1}
				<Step1SelectMarket
					underlying={underlying}
					expiry={expiry}
					strategyType={strategyType}
					targetDelta={targetDelta}
					onUpdate={updateMarketData}
					onNext={() => currentStep = 2}
				/>
			{:else if currentStep === 2}
				<Step2MiniChain
					underlying={underlying}
					expiry={expiry}
					targetDelta={targetDelta}
					selectedStrikes={selectedStrikes}
					onUpdateSelectedStrikes={(strikes) => selectedStrikes = strikes}
					onNext={() => currentStep = 3}
					onBack={() => currentStep = 1}
				/>
			{:else if currentStep === 3}
				<Step3ProtectionConfig
					underlying={underlying}
					selectedStrikes={selectedStrikes}
					protectionConfig={protectionConfig}
					onUpdateProtection={updateProtectionConfig}
					onNext={() => currentStep = 4}
					onBack={() => currentStep = 2}
				/>
			{:else if currentStep === 4}
				<Step4Review
					underlying={underlying}
					expiry={expiry}
					strategyType={strategyType}
					selectedStrikes={selectedStrikes}
					protectionConfig={protectionConfig}
					onBack={() => currentStep = 3}
					onComplete={handleComplete}
				/>
			{/if}
		</div>

		<!-- Sticky Preview Sidebar -->
		<div class="lg:col-span-1">
			<PositionPlanPreview
				underlying={underlying}
				expiry={expiry}
				strategyType={strategyType}
				selectedStrikes={selectedStrikes}
				protectionEnabled={protectionConfig.enabled}
			/>
		</div>
	</div>
</div>
