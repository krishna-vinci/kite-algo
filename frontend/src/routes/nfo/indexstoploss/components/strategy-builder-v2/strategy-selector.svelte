<script lang="ts">
	import { BookOpen } from '@lucide/svelte';
	import {
		type StrategyCategory,
		neutralStrategies,
		bullishStrategies,
		bearishStrategies,
		type StrategyTemplate
	} from '../../lib/strategy-templates';
	
	interface Props {
		onTemplateSelected: (template: StrategyTemplate) => void;
		selectedTemplateId?: string;
	}
	
	let { onTemplateSelected, selectedTemplateId }: Props = $props();
	
	let activeCategory = $state<StrategyCategory>('neutral');
	
	const categories = [
		{ id: 'bullish' as const, label: 'Bullish', icon: '📈', color: 'text-green-600' },
		{ id: 'bearish' as const, label: 'Bearish', icon: '📉', color: 'text-red-600' },
		{ id: 'neutral' as const, label: 'Neutral', icon: '📊', color: 'text-blue-600' },
		{ id: 'others' as const, label: 'Others', icon: '🔧', color: 'text-gray-600' }
	];
	
	const strategiesByCategory = $derived(() => {
		switch (activeCategory) {
			case 'bullish':
				return bullishStrategies;
			case 'bearish':
				return bearishStrategies;
			case 'neutral':
				return neutralStrategies;
			default:
				return [];
		}
	});
	
	function getRiskColor(risk: string): string {
		switch (risk) {
			case 'low':
				return 'text-green-600 bg-green-50 border-green-200';
			case 'medium':
				return 'text-yellow-600 bg-yellow-50 border-yellow-200';
			case 'high':
				return 'text-red-600 bg-red-50 border-red-200';
			default:
				return 'text-gray-600 bg-gray-50 border-gray-200';
		}
	}
</script>

<div class="space-y-4">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<h3 class="text-lg font-semibold">Select Strategy Template</h3>
		<a
			href="https://zerodha.com/varsity/module/option-strategies/"
			target="_blank"
			class="flex items-center gap-2 text-sm text-primary hover:underline"
		>
			<BookOpen class="h-4 w-4" />
			Learn Options Strategies
		</a>
	</div>
	
	<!-- Category Tabs -->
	<div class="flex gap-2">
		{#each categories as category}
			<button
				onclick={() => (activeCategory = category.id)}
				class={`
					px-6 py-3 rounded-full font-medium transition-all duration-200
					${activeCategory === category.id
						? 'bg-primary text-primary-foreground shadow-md scale-105'
						: 'bg-muted text-muted-foreground hover:bg-muted/80'
					}
				`}
			>
				<span class="mr-2">{category.icon}</span>
				{category.label}
			</button>
		{/each}
	</div>
	
	<!-- Strategy Cards Grid -->
	<div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
		{#each strategiesByCategory as strategy}
			<button
				onclick={() => onTemplateSelected(strategy)}
				class={`
					p-4 rounded-lg border-2 transition-all duration-200 text-left
					hover:shadow-lg hover:scale-105
					${selectedTemplateId === strategy.id
						? 'border-primary bg-primary/5 shadow-md'
						: 'border-border bg-card hover:border-primary/50'
					}
				`}
			>
				<!-- Strategy Icon/Mini Chart Placeholder -->
				<div class="mb-3 h-20 bg-muted rounded flex items-center justify-center">
					{#if strategy.payoffType === 'limited'}
						<!-- Limited risk payoff shape -->
						<svg class="w-16 h-16" viewBox="0 0 100 50">
							<path
								d="M 10 40 L 30 40 L 50 10 L 70 10 L 90 40"
								fill="none"
								stroke="currentColor"
								stroke-width="2"
								class={activeCategory === 'bullish' ? 'text-green-500' : activeCategory === 'bearish' ? 'text-red-500' : 'text-blue-500'}
							/>
						</svg>
					{:else}
						<!-- Unlimited risk payoff shape -->
						<svg class="w-16 h-16" viewBox="0 0 100 50">
							<path
								d="M 10 45 L 50 25 L 90 5"
								fill="none"
								stroke="currentColor"
								stroke-width="2"
								class={activeCategory === 'bullish' ? 'text-green-500' : activeCategory === 'bearish' ? 'text-red-500' : 'text-blue-500'}
							/>
						</svg>
					{/if}
				</div>
				
				<!-- Strategy Name -->
				<h4 class="font-semibold text-sm mb-1 line-clamp-2">{strategy.name}</h4>
				
				<!-- Short Description -->
				<p class="text-xs text-muted-foreground mb-3 line-clamp-2">
					{strategy.shortDesc}
				</p>
				
				<!-- Risk Badge -->
				<div class="flex items-center gap-2">
					<span class={`
						text-xs px-2 py-1 rounded-full border font-medium
						${getRiskColor(strategy.riskProfile)}
					`}>
						{strategy.riskProfile.toUpperCase()} RISK
					</span>
					
					{#if selectedTemplateId === strategy.id}
						<span class="text-xs text-primary font-medium">✓ Selected</span>
					{/if}
				</div>
			</button>
		{/each}
	</div>
	
	{#if strategiesByCategory.length === 0}
		<div class="text-center py-8 text-muted-foreground">
			No strategies available in this category yet.
		</div>
	{/if}
</div>
