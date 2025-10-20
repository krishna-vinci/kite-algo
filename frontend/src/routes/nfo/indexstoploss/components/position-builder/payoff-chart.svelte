<script lang="ts">
	import type { SelectedStrike } from '../../types';
	import { generatePayoffData, calculateMetrics, formatCurrency } from '../../lib/payoff-calculator';
	
	interface Props {
		strikes: SelectedStrike[];
		spotPrice: number;
		underlying: string;
	}
	
	let { strikes, spotPrice, underlying }: Props = $props();
	
	// Calculate payoff data
	const payoffData = $derived(
		strikes.length === 0 || !spotPrice
			? []
			: generatePayoffData(strikes, spotPrice, 0.15, 1)
	);
	
	const metrics = $derived(
		strikes.length === 0 || !spotPrice
			? null
			: calculateMetrics(strikes, spotPrice, 1)
	);
	
	// SVG dimensions
	const width = 700;
	const height = 400;
	const padding = { top: 40, right: 60, bottom: 60, left: 80 };
	const chartWidth = width - padding.left - padding.right;
	const chartHeight = height - padding.top - padding.bottom;
	
	// Scales
	const xScale = $derived.by(() => {
		if (payoffData.length === 0) return { min: 0, max: 1, range: 1 };
		const prices = payoffData.map(d => d.price);
		const min = Math.min(...prices);
		const max = Math.max(...prices);
		return { min, max, range: max - min };
	});
	
	const yScale = $derived.by(() => {
		if (payoffData.length === 0) return { min: -1000, max: 1000, range: 2000 };
		const pnls = payoffData.map(d => d.pnl);
		const min = Math.min(...pnls, 0);
		const max = Math.max(...pnls, 0);
		const padding = (max - min) * 0.1;
		return { min: min - padding, max: max + padding, range: max - min + 2 * padding };
	});
	
	// Convert data point to SVG coordinates
	function toSVGX(price: number): number {
		return padding.left + ((price - xScale.min) / xScale.range) * chartWidth;
	}
	
	function toSVGY(pnl: number): number {
		return padding.top + chartHeight - ((pnl - yScale.min) / yScale.range) * chartHeight;
	}
	
	// Generate path for payoff line
	const pathD = $derived(
		payoffData.length === 0
			? ''
			: payoffData.map((d, i) => {
					const x = toSVGX(d.price);
					const y = toSVGY(d.pnl);
					return `${i === 0 ? 'M' : 'L'} ${x},${y}`;
			  }).join(' ')
	);
	
	// Zero line (break-even)
	const zeroY = $derived(toSVGY(0));
	
	// Current spot price line
	const spotX = $derived(toSVGX(spotPrice));
	
	// Y-axis ticks
	const yTicks = $derived.by(() => {
		const tickCount = 5;
		const step = yScale.range / (tickCount - 1);
		return Array.from({ length: tickCount }, (_, i) => yScale.min + i * step);
	});
	
	// X-axis ticks
	const xTicks = $derived.by(() => {
		const tickCount = 7;
		const step = xScale.range / (tickCount - 1);
		return Array.from({ length: tickCount }, (_, i) => xScale.min + i * step);
	});
</script>

<div class="space-y-4">
	{#if strikes.length === 0}
		<div class="flex items-center justify-center h-96 text-muted-foreground">
			<div class="text-center">
				<p class="text-lg font-medium">No strikes selected</p>
				<p class="text-sm">Select strikes from the chain to see payoff diagram</p>
			</div>
		</div>
	{:else}
		<!-- Metrics Cards -->
		{#if metrics}
			<div class="grid grid-cols-2 md:grid-cols-4 gap-3">
				<div class="p-3 rounded-lg border bg-card">
					<div class="text-xs text-muted-foreground">Net Premium</div>
					<div class="text-lg font-bold {metrics.netPremium >= 0 ? 'text-green-600' : 'text-red-600'}">
						{formatCurrency(metrics.netPremium)}
					</div>
					<div class="text-xs text-muted-foreground">{metrics.isCredit ? 'Credit' : 'Debit'}</div>
				</div>
				
				<div class="p-3 rounded-lg border bg-card">
					<div class="text-xs text-muted-foreground">Max Profit</div>
					<div class="text-lg font-bold text-green-600">
						{metrics.maxProfit === 'unlimited' ? '∞' : formatCurrency(metrics.maxProfit)}
					</div>
					<div class="text-xs text-muted-foreground">
						{metrics.maxProfit === 'unlimited' ? 'Unlimited' : `${metrics.maxProfitPercent}%`}
					</div>
				</div>
				
				<div class="p-3 rounded-lg border bg-card">
					<div class="text-xs text-muted-foreground">Max Loss</div>
					<div class="text-lg font-bold text-red-600">
						{metrics.maxLoss === 'unlimited' ? '∞' : formatCurrency(metrics.maxLoss)}
					</div>
					<div class="text-xs text-muted-foreground">
						{metrics.maxLoss === 'unlimited' ? 'Unlimited' : `${metrics.maxLossPercent}%`}
					</div>
				</div>
				
				<div class="p-3 rounded-lg border bg-card">
					<div class="text-xs text-muted-foreground">Breakeven</div>
					<div class="text-lg font-bold">
						{#if metrics.breakevens.length === 0}
							—
						{:else if metrics.breakevens.length === 1}
							{metrics.breakevens[0].toFixed(0)}
						{:else}
							{metrics.breakevens.length} points
						{/if}
					</div>
					<div class="text-xs text-muted-foreground">POP: {metrics.probabilityOfProfit}%</div>
				</div>
			</div>
		{/if}
		
		<!-- Payoff Chart -->
		<div class="p-4 rounded-lg border bg-card">
			<h3 class="text-sm font-semibold mb-3">Payoff Diagram at Expiry</h3>
			<svg viewBox="0 0 {width} {height}" class="w-full h-auto">
				<!-- Grid lines -->
				{#each yTicks as tick}
					<line
						x1={padding.left}
						y1={toSVGY(tick)}
						x2={width - padding.right}
						y2={toSVGY(tick)}
						stroke="#e5e7eb"
						stroke-width="1"
						stroke-dasharray="4"
					/>
				{/each}
				
				<!-- Zero line (thicker) -->
				<line
					x1={padding.left}
					y1={zeroY}
					x2={width - padding.right}
					y2={zeroY}
					stroke="#6b7280"
					stroke-width="2"
				/>
				
				<!-- Current spot line -->
				<line
					x1={spotX}
					y1={padding.top}
					x2={spotX}
					y2={height - padding.bottom}
					stroke="#3b82f6"
					stroke-width="2"
					stroke-dasharray="6"
				/>
				<text
					x={spotX}
					y={padding.top - 10}
					text-anchor="middle"
					fill="#3b82f6"
					font-size="12"
					font-weight="600"
				>
					Spot: {spotPrice.toFixed(0)}
				</text>
				
				<!-- Payoff line -->
				{#if pathD && metrics}
					<path
						d={pathD}
						fill="none"
						stroke={metrics.netPremium >= 0 ? '#10b981' : '#ef4444'}
						stroke-width="3"
					/>
				{/if}
				
				<!-- Y-axis -->
				<line
					x1={padding.left}
					y1={padding.top}
					x2={padding.left}
					y2={height - padding.bottom}
					stroke="#374151"
					stroke-width="2"
				/>
				
				<!-- Y-axis labels -->
				{#each yTicks as tick}
					<text
						x={padding.left - 10}
						y={toSVGY(tick)}
						text-anchor="end"
						dominant-baseline="middle"
						fill="#6b7280"
						font-size="11"
					>
						{tick >= 0 ? '+' : ''}{(tick / 1000).toFixed(1)}k
					</text>
				{/each}
				
				<!-- X-axis -->
				<line
					x1={padding.left}
					y1={height - padding.bottom}
					x2={width - padding.right}
					y2={height - padding.bottom}
					stroke="#374151"
					stroke-width="2"
				/>
				
				<!-- X-axis labels -->
				{#each xTicks as tick}
					<text
						x={toSVGX(tick)}
						y={height - padding.bottom + 20}
						text-anchor="middle"
						fill="#6b7280"
						font-size="11"
					>
						{tick.toFixed(0)}
					</text>
				{/each}
				
				<!-- Axis labels -->
				<text
					x={padding.left - 60}
					y={height / 2}
					transform="rotate(-90, {padding.left - 60}, {height / 2})"
					text-anchor="middle"
					fill="#374151"
					font-size="13"
					font-weight="600"
				>
					P&L (₹)
				</text>
				
				<text
					x={width / 2}
					y={height - 10}
					text-anchor="middle"
					fill="#374151"
					font-size="13"
					font-weight="600"
				>
					{underlying} Spot Price
				</text>
			</svg>
		</div>
	{/if}
</div>
