<script lang="ts">
	import { Check } from '@lucide/svelte';
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
</script>

<nav aria-label="Progress">
	<ol class="flex items-center w-full">
		{#each steps as step, index (step.id)}
			{@const status = getStepStatus(step.id)}
			{@const isLast = index === steps.length - 1}
			
			<li class="flex items-center {isLast ? '' : 'flex-1'}">
				<!-- Step Circle -->
				<div class="flex flex-col items-center gap-2">
					<div class="flex items-center gap-4">
						<div
							class="
								flex items-center justify-center
								w-10 h-10 rounded-full border-2
								{status === 'completed' ? 'bg-primary border-primary text-primary-foreground' :
								status === 'current' ? 'border-primary bg-background text-primary' :
								'border-muted bg-background text-muted-foreground'}
							"
						>
							{#if status === 'completed'}
								<Check class="h-5 w-5" />
							{:else}
								<span class="font-semibold">{step.id}</span>
							{/if}
						</div>
					</div>
					
					<!-- Step Label -->
					<div class="flex flex-col items-center text-center">
						<p class="
							text-sm font-medium
							{status === 'current' ? 'text-foreground' : 'text-muted-foreground'}
						">
							{step.title}
						</p>
						<p class="text-xs text-muted-foreground hidden sm:block max-w-[120px]">
							{step.description}
						</p>
					</div>
				</div>

				<!-- Separator Line -->
				{#if !isLast}
					<Separator 
						class="
							flex-1 mx-4 
							{status === 'completed' ? 'bg-primary' : 'bg-muted'}
						" 
					/>
				{/if}
			</li>
		{/each}
	</ol>
</nav>
