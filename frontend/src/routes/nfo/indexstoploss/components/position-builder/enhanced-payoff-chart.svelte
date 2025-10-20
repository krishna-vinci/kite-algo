<script lang="ts">
	import type { SelectedStrike, MiniChainResponse } from '../../types';
	import { generatePayoffData, formatCurrency, calculateMetrics } from '../../lib/payoff-calculator';
	import { Button } from '$lib/components/ui/button';
	import { ZoomOut, Info, ChevronLeft, ChevronRight } from '@lucide/svelte';

	interface Props {
		strikes: SelectedStrike[];
		spotPrice: number;
		underlying: string;
		targetPriceOffset: number;
		daysToExpiry: number;
		maxDaysToExpiry: number;
		expiry: string;
		chainData?: MiniChainResponse | null;
		positionGreeks: { delta: number; gamma: number; theta: number; vega: number };
	}

	let {
		strikes,
		spotPrice,
		underlying,
		targetPriceOffset = $bindable(),
		daysToExpiry = $bindable(),
		maxDaysToExpiry,
		expiry,
		chainData = null,
		positionGreeks
	}: Props = $props();

	const projectedSpot = $derived(spotPrice * (1 + targetPriceOffset / 100));

	const metrics = $derived(
		strikes.length === 0
			? null
			: calculateMetrics(strikes, spotPrice, 1) // Using spotPrice for expiry breakeven
	);
	
	// Generate payoff data for two scenarios
	// Use spotPrice (not projectedSpot) as the center point for the price range
	const payoffExpiryData = $derived(generatePayoffData(strikes, spotPrice, 0.15, 1)); // At expiry
	
	// For target date, approximate by interpolating between current and expiry
	// This is simplified - a full implementation would use Black-Scholes
	const payoffTargetData = $derived.by(() => {
		// NOTE: Removing flawed time decay logic to fix "half profit" bug.
		// The target date line will now track the expiry line. A more sophisticated
		// time decay model can be implemented if needed.
		const expiryData = generatePayoffData(strikes, spotPrice, 0.15, 1);
		
		return expiryData.map(point => ({
			price: point.price,
			pnl: point.pnl,
			atExpiry: false
		}));
	});

	const width = 1000;
	const height = 400;
	const padding = { top: 60, right: 60, bottom: 80, left: 70 };
	const chartWidth = width - padding.left - padding.right;
	const chartHeight = height - padding.top - padding.bottom;
	
	// Calculate total Call and Put OI
	const oiData = $derived.by(() => {
		if (!chainData?.strikes) return { totalCallOI: 0, totalPutOI: 0, strikeOI: [] };
		
		let totalCallOI = 0;
		let totalPutOI = 0;
		const strikeOI = chainData.strikes.map(s => {
			const callOI = s.ce?.oi || 0;
			const putOI = s.pe?.oi || 0;
			totalCallOI += callOI;
			totalPutOI += putOI;
			return { strike: s.strike, callOI, putOI };
		});
		
		return { totalCallOI, totalPutOI, strikeOI };
	});

	const xScale = $derived.by(() => {
		if (payoffExpiryData.length === 0) return { min: 0, max: 1, range: 1 };
		const prices = payoffExpiryData.map(d => d.price);
		const min = Math.min(...prices);
		const max = Math.max(...prices);
		return { min, max, range: max - min };
	});

	const yScale = $derived.by(() => {
		if (payoffExpiryData.length === 0) return { min: -1000, max: 1000, range: 2000 };
		const allPnls = [...payoffExpiryData.map(d => d.pnl), ...payoffTargetData.map(d => d.pnl)];
		const min = Math.min(...allPnls, 0);
		const max = Math.max(...allPnls, 0);
		const pad = (max - min) * 0.15;
		return { min: min - pad, max: max + pad, range: max - min + 2 * pad };
	});
	
	// OI scale for bars
	const maxOI = $derived.by(() => {
		if (oiData.strikeOI.length === 0) return 1;
		return Math.max(...oiData.strikeOI.map(s => Math.max(s.callOI, s.putOI)));
	});

	function toSVGX(price: number): number {
		return padding.left + ((price - xScale.min) / xScale.range) * chartWidth;
	}

	function toSVGY(pnl: number): number {
		return padding.top + chartHeight - ((pnl - yScale.min) / yScale.range) * chartHeight;
	}

	// Path for expiry line (dark purple)
	const pathExpiry = $derived(
		payoffExpiryData.length === 0
			? ''
			: payoffExpiryData.map((d, i) => {
					const x = toSVGX(d.price);
					const y = toSVGY(d.pnl);
					return `${i === 0 ? 'M' : 'L'} ${x},${y}`;
			  }).join(' ')
	);
	
	// Path for target date line (blue)
	const pathTarget = $derived(
		payoffTargetData.length === 0
			? ''
			: payoffTargetData.map((d, i) => {
					const x = toSVGX(d.price);
					const y = toSVGY(d.pnl);
					return `${i === 0 ? 'M' : 'L'} ${x},${y}`;
			  }).join(' ')
	);

	const zeroY = $derived(toSVGY(0));
	const spotX = $derived(toSVGX(spotPrice));
	const targetX = $derived(toSVGX(projectedSpot));

	const yTicks = $derived.by(() => {
		const tickCount = 6;
		const step = yScale.range / (tickCount - 1);
		return Array.from({ length: tickCount }, (_, i) => yScale.min + i * step);
	});

	const xTicks = $derived.by(() => {
		const tickCount = 9;
		const step = xScale.range / (tickCount - 1);
		return Array.from({ length: tickCount }, (_, i) => xScale.min + i * step);
	});
	
	// SD markers (for display purposes)
	const sdMarkers = $derived.by(() => {
		const sd = xScale.range * 0.15; // Approximate SD
		return [
			{ label: '-2SD', value: spotPrice - 2 * sd },
			{ label: '-1SD', value: spotPrice - sd },
			{ label: '1SD', value: spotPrice + sd },
			{ label: '2SD', value: spotPrice + 2 * sd }
		].filter(m => m.value >= xScale.min && m.value <= xScale.max);
	});

	const projectedPnL = $derived(() => {
		if (payoffTargetData.length === 0) return 0;
		const closest = payoffTargetData.reduce((prev, curr) =>
			Math.abs(curr.price - projectedSpot) < Math.abs(prev.price - projectedSpot) ? curr : prev
		);
		return closest.pnl;
	});
	
	function formatOI(oi: number): string {
		if (oi >= 10000000) return `${(oi / 10000000).toFixed(2)}Cr`;
		if (oi >= 100000) return `${(oi / 100000).toFixed(2)}L`;
		return oi.toString();
	}
	
	function toOIBarHeight(oi: number): number {
		return (oi / maxOI) * (chartHeight * 0.3); // Max 30% of chart height
	}
</script>

<div class="space-y-3">
	<!-- Header with OI Info -->
	<div class="flex items-center justify-between px-2">
		<div class="text-xs text-muted-foreground">
			<div class="flex items-center gap-4">
				<span>OI @ {spotPrice.toFixed(0)}:</span>
				<span class="inline-flex items-center gap-1">
					<span class="w-2 h-2 bg-rose-400 rounded-sm"></span>
					Call {formatOI(oiData.totalCallOI)}
				</span>
				<span class="inline-flex items-center gap-1">
					<span class="w-2 h-2 bg-emerald-400 rounded-sm"></span>
					Put {formatOI(oiData.totalPutOI)}
				</span>
				{#if positionGreeks}
					<span class="font-mono text-gray-500 border-l pl-3">
						<span class="text-blue-600">Δ</span> {positionGreeks.delta.toFixed(2)}
						<span class="text-purple-600 ml-2">Γ</span> {positionGreeks.gamma.toFixed(4)}
						<span class="text-red-600 ml-2">θ</span> {positionGreeks.theta.toFixed(2)}
						<span class="text-green-600 ml-2">ν</span> {positionGreeks.vega.toFixed(2)}
					</span>
				{/if}
			</div>
		</div>
		<div class="flex items-center gap-2">
			<div class="flex items-center gap-3 text-xs">
				<span class="flex items-center gap-1">
					<div class="w-4 h-0.5 bg-purple-800"></div>
					On Expiry
				</span>
				<span class="flex items-center gap-1">
					<div class="w-4 h-0.5 bg-blue-500"></div>
					On Target Date
				</span>
			</div>
			<Button variant="outline" size="sm" class="h-7">
				<ZoomOut class="h-3.5 w-3.5 mr-1" />
				Zoom Out
			</Button>
		</div>
	</div>
	
	<!-- Chart -->
	<div class="relative bg-white border rounded-lg overflow-hidden">
		<svg viewBox="0 0 {width} {height}" class="w-full h-auto">
			<!-- Hatched background pattern -->
			<defs>
				<pattern id="hatch" width="10" height="10" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
					<line x1="0" y1="0" x2="0" y2="10" stroke="#f3f4f6" stroke-width="8" />
				</pattern>
			</defs>
			
			<!-- Breakeven/Target Info Box -->
			{#if metrics}
				<foreignObject x={padding.left + 10} y={padding.top + 10} width="220" height="80">
					<div class="bg-white/80 backdrop-blur-sm p-2 rounded-md border border-gray-200/80 text-xs space-y-1 font-sans">
						<div class="flex justify-between items-center">
							<span class="text-muted-foreground">Breakevens (Expiry):</span>
							<span class="font-semibold text-right">
								{#if metrics.breakevens.length === 0}
									N/A
								{:else}
									{metrics.breakevens.map(b => b.toFixed(0)).join(' & ')}
								{/if}
							</span>
						</div>
						<div class="flex justify-between items-center">
							<span class="text-muted-foreground">PnL at Target:</span>
							<span class="font-semibold {projectedPnL() >= 0 ? 'text-green-600' : 'text-red-600'}">
								{projectedPnL() >= 0 ? '+' : ''}{formatCurrency(projectedPnL())}
							</span>
						</div>
						<div class="flex justify-between items-center">
							<span class="text-muted-foreground">Expiry Date:</span>
							<span class="font-semibold">
								{new Date(expiry).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })} ({daysToExpiry}d)
							</span>
						</div>
					</div>
				</foreignObject>
			{/if}

			<!-- Hatched background -->
			<rect x="{padding.left}" y="{padding.top}" width="{chartWidth}" height="{chartHeight}" fill="url(#hatch)" />
			
			<!-- OI Bars -->
			{#each oiData.strikeOI as { strike, callOI, putOI }}
				{@const x = toSVGX(strike)}
				{@const callHeight = toOIBarHeight(callOI)}
				{@const putHeight = toOIBarHeight(putOI)}
				
				<!-- Call OI bar (pink/red, upward) -->
				{#if callOI > 0}
					<rect
						x={x - 3}
						y={padding.top}
						width="6"
						height={callHeight}
						fill="#fb7185"
						opacity="0.6"
					/>
				{/if}
				
				<!-- Put OI bar (green, upward) -->
				{#if putOI > 0}
					<rect
						x={x - 3}
						y={padding.top}
						width="6"
						height={putHeight}
						fill="#6ee7b7"
						opacity="0.6"
					/>
				{/if}
			{/each}
			
			<!-- Zero line -->
			<line
				x1={padding.left}
				y1={zeroY}
				x2={width - padding.right}
				y2={zeroY}
				stroke="#9ca3af"
				stroke-width="1.5"
			/>

			
			<!-- Current price vertical line (STATIC) -->
			<line
				x1={spotX}
				y1={padding.top}
				x2={spotX}
				y2={height - padding.bottom}
				stroke="#6b7280"
				stroke-width="1.5"
			/>
			
			<!-- Current price label at top (STATIC) -->
			<g>
				<rect
					x={spotX - 50}
					y={padding.top - 35}
					width="100"
					height="20"
					fill="white"
					stroke="#d1d5db"
					rx="3"
				/>
				<text
					x={spotX}
					y={padding.top - 20}
					text-anchor="middle"
					fill="#374151"
					font-size="11"
					font-weight="600"
				>
					Current price: {spotPrice.toFixed(2)}
				</text>
			</g>
			
			<!-- Target price vertical line (MOVES with slider) -->
			{#if Math.abs(targetPriceOffset) > 0.1}
				<line
					x1={targetX}
					y1={padding.top}
					x2={targetX}
					y2={height - padding.bottom}
					stroke="#3b82f6"
					stroke-width="2"
					stroke-dasharray="4 4"
				/>
				
				<!-- Target price marker on the payoff line -->
				{@const targetPnL = projectedPnL()}
				{@const targetY = toSVGY(targetPnL)}
				<circle
					cx={targetX}
					cy={targetY}
					r="5"
					fill={targetPnL >= 0 ? '#10b981' : '#ef4444'}
					stroke="white"
					stroke-width="2"
				/>
				
				<!-- Small label showing target value -->
				<g>
					<rect
						x={targetX - 35}
						y={padding.top - 35}
						width="70"
						height="20"
						fill="#3b82f6"
						rx="3"
					/>
					<text
						x={targetX}
						y={padding.top - 20}
						text-anchor="middle"
						fill="white"
						font-size="10"
						font-weight="600"
					>
						Target: {projectedSpot.toFixed(0)}
					</text>
				</g>
			{/if}
			<!-- Payoff Lines -->
			<!-- Expiry line (dark purple) -->
			{#if pathExpiry}
				<path
					d={pathExpiry}
					fill="none"
					stroke="#6b21a8"
					stroke-width="2.5"
				/>
			{/if}
			
			<!-- Target date line (blue) -->
			{#if pathTarget}
				<path
					d={pathTarget}
					fill="none"
					stroke="#3b82f6"
					stroke-width="2.5"
				/>
			{/if}

			
			<!-- Y-axis -->
			<line
				x1={padding.left}
				y1={padding.top}
				x2={padding.left}
				y2={height - padding.bottom}
				stroke="#9ca3af"
				stroke-width="1.5"
			/>

			<!-- Y-axis labels (left side - Profit/Loss) -->
			{#each yTicks as tick}
				<text
					x={padding.left - 12}
					y={toSVGY(tick)}
					text-anchor="end"
					dominant-baseline="middle"
					fill="#6b7280"
					font-size="10"
				>
					{tick >= 0 ? '' : ''}{(Math.abs(tick) / 1000).toFixed(0)},{((Math.abs(tick) % 1000) / 10).toFixed(0).padStart(3, '0')}
				</text>
			{/each}
			
			<!-- Y-axis label -->
			<text
				x="10"
				y={height / 2}
				transform="rotate(-90, 10, {height / 2})"
				text-anchor="middle"
				fill="#6b7280"
				font-size="11"
				font-weight="500"
			>
				Profit / Loss
			</text>
			
			<!-- OI axis label (right side) -->
			<text
				x={width - 10}
				y={height / 2}
				transform="rotate(90, {width - 10}, {height / 2})"
				text-anchor="middle"
				fill="#6b7280"
				font-size="11"
				font-weight="500"
			>
				OI in Contracts
			</text>

			<!-- X-axis -->
			<line
				x1={padding.left}
				y1={height - padding.bottom}
				x2={width - padding.right}
				y2={height - padding.bottom}
				stroke="#9ca3af"
				stroke-width="1.5"
			/>

			
			<!-- X-axis labels -->
			{#each xTicks as tick}
				<text
					x={toSVGX(tick)}
					y={height - padding.bottom + 18}
					text-anchor="middle"
					fill="#6b7280"
					font-size="10"
				>
					{tick.toFixed(0)}
				</text>
			{/each}
			
			<!-- SD markers on X-axis -->
			{#each sdMarkers as { label, value }}
				<text
					x={toSVGX(value)}
					y={height - padding.bottom + 35}
					text-anchor="middle"
					fill="#9ca3af"
					font-size="9"
				>
					{label}
				</text>
			{/each}
		</svg>
	</div>

	
	<!-- Controls Row -->
	<div class="grid grid-cols-2 gap-4 px-2">
		<!-- Target Control -->
		<div class="space-y-1.5">
			<div class="flex items-center justify-between">
				<span class="text-xs font-medium text-muted-foreground">{underlying} Target</span>
				<div class="flex items-center gap-1.5">
					<Button
						variant="outline"
						size="sm"
						class="h-6 w-6 p-0"
						onclick={() => (targetPriceOffset = Math.max(targetPriceOffset - 0.5, -10))}
					>
						−
					</Button>
					<span class="font-mono text-xs font-semibold min-w-[70px] text-center">
						{projectedSpot.toFixed(0)}
					</span>
					<Button
						variant="outline"
						size="sm"
						class="h-6 w-6 p-0"
						onclick={() => (targetPriceOffset = Math.min(targetPriceOffset + 0.5, 10))}
					>
						+
					</Button>
				</div>
			</div>
			<input
				type="range"
				min="-10"
				max="10"
				step="0.1"
				bind:value={targetPriceOffset}
				class="w-full h-1.5"
			/>
			<div class="flex justify-between text-[10px] text-muted-foreground">
				<Button variant="link" class="h-auto p-0 text-[10px]" onclick={() => targetPriceOffset = 0}>Reset</Button>
				<span class="font-semibold">{targetPriceOffset > 0 ? '+' : ''}{targetPriceOffset.toFixed(1)}%</span>
			</div>
		</div>

		<!-- Date Control -->
		<div class="space-y-1.5">
			<div class="flex items-center justify-between">
				<span class="text-xs font-medium text-muted-foreground">Date: {daysToExpiry}D to expiry</span>
				<div class="flex items-center gap-1.5">
					<Button
						variant="outline"
						size="sm"
						class="h-6 w-6 p-0"
						onclick={() => (daysToExpiry = Math.max(daysToExpiry - 1, 0))}
						disabled={daysToExpiry <= 0}
					>
						<ChevronLeft class="h-3 w-3" />
					</Button>
					<span class="font-mono text-xs font-semibold min-w-[120px] text-center">
						{new Date(Date.now() + daysToExpiry * 24 * 60 * 60 * 1000).toLocaleDateString('en-GB', {
							weekday: 'short',
							day: '2-digit',
							month: 'short'
						})}
					</span>
					<Button
						variant="outline"
						size="sm"
						class="h-6 w-6 p-0"
						onclick={() => (daysToExpiry = Math.min(daysToExpiry + 1, maxDaysToExpiry))}
						disabled={daysToExpiry >= maxDaysToExpiry}
					>
						<ChevronRight class="h-3 w-3" />
					</Button>
				</div>
			</div>
			<input
				type="range"
				min="0"
				max={maxDaysToExpiry}
				step="1"
				bind:value={daysToExpiry}
				class="w-full h-1.5"
			/>
			<div class="flex justify-between text-[10px] text-muted-foreground">
				<Button variant="link" class="h-auto p-0 text-[10px]" onclick={() => daysToExpiry = maxDaysToExpiry}>Reset to Today</Button>
				<span class="flex items-center gap-1"><Info class="h-2.5 w-2.5" />Indicative</span>
				<span>{new Date(expiry).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })}</span>
			</div>
		</div>
	</div>
</div>
