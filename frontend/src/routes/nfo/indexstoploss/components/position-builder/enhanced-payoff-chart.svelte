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
	
	// Handler to update target price offset when user enters a target price directly
	function handleTargetPriceChange(newPrice: number) {
		if (spotPrice > 0) {
			targetPriceOffset = ((newPrice - spotPrice) / spotPrice) * 100;
		}
	}

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
			const callOI = s.ce?.oi ?? 0;
			const putOI = s.pe?.oi ?? 0;
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
		if (payoffExpiryData.length === 0)
			return { min: -10000, max: 10000, range: 20000, step: 5000, ticks: [-10000, -5000, 0, 5000, 10000] };
		const all = [...payoffExpiryData.map(d => d.pnl), ...payoffTargetData.map(d => d.pnl), 0];
		let min = Math.min(...all);
		let max = Math.max(...all);
		if (min === max) {
			min = min - 1;
			max = max + 1;
		}
		
		if (max > 0 && Math.abs(min) > max * 3) {
			min = -max * 3;
		}
		
		const dataRange = Math.abs(max - min);
		let step = 10000;
		if (dataRange < 30000) {
			step = 5000;
		} else if (dataRange < 60000) {
			step = 10000;
		} else if (dataRange < 120000) {
			step = 20000;
		} else if (dataRange < 300000) {
			step = 50000;
		} else {
			step = 100000;
		}
		
		if (max > 0) {
			if (max < step * 3) {
				max = step * 3;
			}
			if (Math.abs(min) < max * 0.3) {
				min = -max * 0.3;
			}
		} else {
			const absMax = Math.max(Math.abs(min), Math.abs(max));
			const profitRatio = max / absMax;
			if (profitRatio > 0 && profitRatio < 0.3) {
				max = absMax * 0.3;
			}
			const lossRatio = Math.abs(min) / absMax;
			if (lossRatio > 0 && lossRatio < 0.3) {
				min = -absMax * 0.3;
			}
		}
		
		const niceMin = Math.floor(min / step) * step;
		const niceMax = Math.ceil(max / step) * step;
		const ticks: number[] = [];
		for (let v = niceMin; v <= niceMax + 1e-9; v += step) ticks.push(v);
		return { min: niceMin, max: niceMax, range: niceMax - niceMin, step, ticks };
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

	const oiBaseY = $derived(padding.top + chartHeight * 0.5);

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

	const yTicks = $derived.by(() => yScale.ticks);

	const xTicks = $derived.by(() => {
		const tickCount = 9;
		const step = xScale.range / (tickCount - 1);
		return Array.from({ length: tickCount }, (_, i) => xScale.min + i * step);
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

	function formatINRShort(n: number): string {
		const abs = Math.abs(n);
		const sign = n < 0 ? '-' : '';
		if (abs >= 1e7) return `${sign}${(abs / 1e7).toFixed(1)}Cr`;
		if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(1)}L`;
		if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}k`;
		return `${sign}${abs.toFixed(0)}`;
	}
</script>

<div class="space-y-3">
	<!-- OI Summary & Legend -->
	<div class="flex items-center justify-between px-2">
		<div class="flex items-center gap-6 text-sm">
			<div class="flex items-center gap-2">
				<span class="text-muted-foreground">OI data at {chainData?.spot_price?.toFixed(0) || 0}</span>
			</div>
			<div class="flex items-center gap-2">
				<div class="w-3 h-3 rounded-sm bg-pink-400"></div>
				<span class="font-medium">Call OI {(oiData.totalCallOI / 10000000).toFixed(2)}Cr</span>
			</div>
			<div class="flex items-center gap-2">
				<div class="w-3 h-3 rounded-sm bg-green-400"></div>
				<span class="font-medium">Put OI {(oiData.totalPutOI / 10000000).toFixed(2)}Cr</span>
			</div>
		</div>
		<div class="flex items-center gap-4 text-xs">
			<div class="flex items-center gap-2">
				<div class="w-8 h-0.5 bg-red-500"></div>
				<span>On Expiry</span>
			</div>
			<div class="flex items-center gap-2">
				<div class="w-8 h-0.5 bg-blue-500"></div>
				<span>On Target Date</span>
			</div>
		</div>
	</div>

	<div class="px-2 -mt-1 text-xs text-muted-foreground">
		<div class="flex gap-6">
			<div>Δ {positionGreeks.delta.toFixed(2)}</div>
			<div>Θ {positionGreeks.theta.toFixed(2)}</div>
			<div>Γ {positionGreeks.gamma.toFixed(4)}</div>
			<div>V {positionGreeks.vega.toFixed(2)}</div>
		</div>
	</div>
	
	<!-- Current Price Indicator -->
	<div class="text-center py-2">
		<div class="inline-flex items-center gap-2 px-4 py-1.5 bg-gray-50 border rounded-md">
			<span class="text-xs text-muted-foreground">Current price:</span>
			<span class="font-mono font-semibold">{projectedSpot.toFixed(2)}</span>
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
			
			<rect x="{padding.left}" y="{padding.top}" width="{chartWidth}" height="{Math.max(0, zeroY - padding.top)}" fill="#10b981" opacity="0.08" />
			<rect x="{padding.left}" y="{zeroY}" width="{chartWidth}" height="{Math.max(0, height - padding.bottom - zeroY)}" fill="#ef4444" opacity="0.08" />

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
								{formatCurrency(projectedPnL())}
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

			<line x1={padding.left} x2={width - padding.right} y1={oiBaseY} y2={oiBaseY} stroke="#e5e7eb" stroke-width="1" stroke-dasharray="4 4" />
			
			{#each oiData.strikeOI as { strike, callOI, putOI }}
				{@const x = toSVGX(strike)}
				{@const callHeight = toOIBarHeight(callOI)}
				{@const putHeight = toOIBarHeight(putOI)}
				{#if callOI > 0}
					<rect x={x - 3} y={oiBaseY - callHeight} width="6" height={callHeight} fill="#fb7185" opacity="0.6" />
				{/if}
				{#if putOI > 0}
					<rect x={x - 3} y={oiBaseY} width="6" height={putHeight} fill="#6ee7b7" opacity="0.6" />
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
			<!-- Expiry line (red) -->
			{#if pathExpiry}
				<path
					d={pathExpiry}
					fill="none"
					stroke="#ef4444"
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
					{formatINRShort(tick)}
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
			
			<!-- Projected Profit Display (inside chart) -->
			{#if metrics && projectedPnL()}
				{@const pnl = projectedPnL()}
				{@const pnlPercent = metrics.netPremium !== 0 ? ((pnl / Math.abs(metrics.netPremium)) * 100) : 0}
				<foreignObject x={(width - 300) / 2} y={height - padding.bottom + 30} width="300" height="40">
					<div class="flex justify-center font-sans">
						<div class="inline-flex items-center gap-2 px-4 py-1.5 rounded-md {pnl >= 0 ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'}">
							<span class="text-xs text-muted-foreground">Projected profit:</span>
							<span class="text-sm font-bold {pnl >= 0 ? 'text-green-600' : 'text-red-600'}">
								{formatCurrency(pnl)} ({pnl >= 0 ? '+' : ''}{pnlPercent.toFixed(1)}%)
							</span>
						</div>
					</div>
				</foreignObject>
			{/if}
		</svg>
	</div>

	<!-- Controls Row -->
	<div class="grid grid-cols-2 gap-6 px-2 py-3 border-t">
		<!-- Target Control -->
		<div class="space-y-2">
			<div class="flex items-center justify-between">
				<span class="text-sm font-medium">{underlying} Target</span>
				<div class="flex items-center gap-2">
					<span class="text-sm font-semibold text-orange-600">{targetPriceOffset.toFixed(1)}%</span>
					<button class="text-xs text-blue-600 hover:underline" onclick={() => (targetPriceOffset = 0)}>Reset</button>
				</div>
			</div>
			<div class="flex items-center gap-2">
				<button
					onclick={() => targetPriceOffset = Math.max(-15, targetPriceOffset - 0.5)}
					class="px-2 py-1 border rounded hover:bg-gray-50 text-sm"
				>−</button>
				<input
					type="number"
					value={projectedSpot.toFixed(0)}
					oninput={(e) => handleTargetPriceChange(parseFloat(e.currentTarget.value) || spotPrice)}
					class="w-full px-2 py-1 text-center text-sm border rounded"
					step="10"
				/>
				<button
					onclick={() => targetPriceOffset = Math.min(15, targetPriceOffset + 0.5)}
					class="px-2 py-1 border rounded hover:bg-gray-50 text-sm"
				>+</button>
			</div>
			<input type="range" min="-15" max="15" step="0.1" bind:value={targetPriceOffset} class="w-full accent-blue-600" />
		</div>

		<!-- Date Control -->
		<div class="space-y-2">
			<div class="flex items-center justify-between">
				<span class="text-sm font-medium">Date: <strong>{daysToExpiry}D</strong> to expiry</span>
				<button class="text-xs text-blue-600 hover:underline" onclick={() => daysToExpiry = 0}>Reset</button>
			</div>
			<div class="flex items-center gap-2">
				<button
					onclick={() => daysToExpiry = Math.max(0, daysToExpiry - 1)}
					class="px-2 py-1 border rounded hover:bg-gray-50 text-sm"
				>‹</button>
				<span class="flex-1 text-sm text-muted-foreground text-center">
					{new Date(Date.now() + daysToExpiry * 86400000).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })}
				</span>
				<button
					onclick={() => daysToExpiry = Math.min(maxDaysToExpiry, daysToExpiry + 1)}
					class="px-2 py-1 border rounded hover:bg-gray-50 text-sm"
				>›</button>
			</div>
			<input type="range" min="0" max={maxDaysToExpiry} step="1" bind:value={daysToExpiry} class="w-full accent-indigo-600" />
		</div>
	</div>
</div>
