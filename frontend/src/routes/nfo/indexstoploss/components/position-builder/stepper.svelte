<script lang="ts">
	import { Check, Hammer, Shield, Eye } from '@lucide/svelte';
	import { Separator } from '$lib/components/ui/separator';

	interface Step {
		id: number;
		title: string;
		description: string;
	}

	interface Props {
		steps: Step[];
		currentStep: number;
	}

	let { steps, currentStep }: Props = $props();

	function getStepStatus(stepId: number): 'completed' | 'current' | 'upcoming' {
		if (stepId < currentStep) return 'completed';
		if (stepId === currentStep) return 'current';
		return 'upcoming';
	}
	
	function getStepIcon(stepId: number) {
		switch(stepId) {
			case 1: return Hammer;
			case 2: return Shield;
			case 3: return Eye;
			default: return Hammer;
		}
	}
</script>

<nav aria-label="Progress">
	<ol class="flex items-center w-full justify-center">
		{#each steps as step, index (step.id)}
			{@const status = getStepStatus(step.id)}
			{@const isLast = index === steps.length - 1}
			{@const StepIcon = getStepIcon(step.id)}
			
			<li class="flex items-center">
				<!-- Step Circle - Minimized -->
				<div
					class="
						flex items-center justify-center
						w-7 h-7 rounded-full border-2 transition-all
						{status === 'completed' ? 'bg-primary border-primary text-primary-foreground' :
						status === 'current' ? 'border-primary bg-background text-primary' :
						'border-muted bg-background text-muted-foreground'}
					"
					title={step.title}
				>
					{#if status === 'completed'}
						<Check class="h-3.5 w-3.5" />
					{:else}
						<StepIcon class="h-3.5 w-3.5" />
					{/if}
				</div>

				<!-- Separator Line -->
				{#if !isLast}
					<Separator 
						class="
							w-8 mx-2
							{status === 'completed' ? 'bg-primary' : 'bg-muted'}
						" 
					/>
				{/if}
			</li>
		{/each}
	</ol>
</nav>
