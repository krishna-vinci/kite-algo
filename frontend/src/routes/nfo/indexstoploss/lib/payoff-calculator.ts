/**
 * Payoff Calculator for Options Strategies
 * Calculates P&L, breakevens, max profit/loss, Greeks
 */

import type { SelectedStrike } from '../types';

export interface PayoffMetrics {
	maxProfit: number | 'unlimited';
	maxProfitPercent: number;
	maxLoss: number | 'unlimited';
	maxLossPercent: number;
	breakevens: number[];
	rewardRiskRatio: number | string;
	probabilityOfProfit: number; // POP
	netPremium: number;
	isCredit: boolean;
	timeValue: number;
	intrinsicValue: number;
	fundsNeeded: number;
	marginNeeded: number;
	marginAvailable: number;
}

export interface PayoffPoint {
	price: number;
	pnl: number;
	atExpiry: boolean;
}

/**
 * Calculate option payoff at expiry
 */
function calculateOptionPayoff(
	strike: number,
	optionType: 'CE' | 'PE',
	transactionType: 'BUY' | 'SELL',
	premium: number,
	spotPrice: number,
	quantity: number
): number {
	let intrinsicValue = 0;
	
	if (optionType === 'CE') {
		// Call option
		intrinsicValue = Math.max(0, spotPrice - strike);
	} else {
		// Put option
		intrinsicValue = Math.max(0, strike - spotPrice);
	}
	
	const payoff = transactionType === 'BUY'
		? (intrinsicValue - premium) * quantity
		: (premium - intrinsicValue) * quantity;
	
	return payoff;
}

/**
 * Calculate net P&L for all strikes at a given spot price
 */
export function calculateNetPnL(
	strikes: SelectedStrike[],
	spotPrice: number,
	atExpiry: boolean = true,
	multiplier: number = 1
): number {
	let totalPnL = 0;
	
	for (const strike of strikes) {
		const quantity = strike.lots * strike.lot_size * multiplier;
		const premium = strike.ltp || 0;
		
		if (atExpiry) {
			totalPnL += calculateOptionPayoff(
				strike.strike,
				strike.option_type,
				strike.transaction_type,
				premium,
				spotPrice,
				quantity
			);
		} else {
			// Current P&L (simplified - would need Black-Scholes for accuracy)
			const sign = strike.transaction_type === 'SELL' ? -1 : 1;
			totalPnL += sign * premium * quantity;
		}
	}
	
	return totalPnL;
}

/**
 * Generate payoff chart data points
 */
export function generatePayoffData(
	strikes: SelectedStrike[],
	currentSpot: number,
	range: number = 0.15, // ±15% range
	multiplier: number = 1
): PayoffPoint[] {
	const points: PayoffPoint[] = [];
	const minPrice = currentSpot * (1 - range);
	const maxPrice = currentSpot * (1 + range);
	const step = (maxPrice - minPrice) / 100; // 100 points
	
	for (let price = minPrice; price <= maxPrice; price += step) {
		const roundedPrice = Math.round(price);
		points.push({
			price: roundedPrice,
			pnl: calculateNetPnL(strikes, roundedPrice, true, multiplier),
			atExpiry: true
		});
	}
	
	return points;
}

/**
 * Detect if strategy has unlimited profit or loss
 */
function detectUnlimitedRisk(strikes: SelectedStrike[]): { unlimitedProfit: boolean; unlimitedLoss: boolean } {
	let netCE = 0;
	let netPE = 0;
	
	for (const strike of strikes) {
		const sign = strike.transaction_type === 'BUY' ? 1 : -1;
		if (strike.option_type === 'CE') {
			netCE += sign;
		} else {
			netPE += sign;
		}
	}
	
	// If net short on CE or PE, unlimited loss
	const unlimitedLoss = (netCE < 0 || netPE < 0) && strikes.length > 0;
	
	// If net long on CE or PE, unlimited profit
	const unlimitedProfit = (netCE > 0 || netPE > 0) && strikes.length > 0;
	
	return { unlimitedProfit, unlimitedLoss };
}

/**
 * Calculate strategy metrics
 */
export function calculateMetrics(
	strikes: SelectedStrike[],
	currentSpot: number,
	multiplier: number = 1
): PayoffMetrics {
	// Detect unlimited scenarios
	const { unlimitedProfit, unlimitedLoss } = detectUnlimitedRisk(strikes);
	
	// Generate payoff data
	const payoffData = generatePayoffData(strikes, currentSpot, 0.15, multiplier);
	
	// Find max profit and loss from the range we calculated
	let maxProfit: number | 'unlimited' = -Infinity;
	let maxLoss: number | 'unlimited' = Infinity;
	
	if (!unlimitedProfit && !unlimitedLoss) {
		for (const point of payoffData) {
			if (point.pnl > maxProfit) maxProfit = point.pnl;
			if (point.pnl < maxLoss) maxLoss = point.pnl;
		}
	} else {
		// Calculate within range but mark as unlimited
		let rangeMax = -Infinity;
		let rangeMin = Infinity;
		for (const point of payoffData) {
			if (point.pnl > rangeMax) rangeMax = point.pnl;
			if (point.pnl < rangeMin) rangeMin = point.pnl;
		}
		maxProfit = unlimitedProfit ? 'unlimited' : rangeMax;
		maxLoss = unlimitedLoss ? 'unlimited' : rangeMin;
	}
	
	// Calculate net premium
	let netPremium = 0;
	let totalQuantity = 0;
	
	for (const strike of strikes) {
		const quantity = strike.lots * strike.lot_size * multiplier;
		const premium = strike.ltp || 0;
		const sign = strike.transaction_type === 'SELL' ? 1 : -1;
		netPremium += sign * premium * quantity;
		totalQuantity += quantity;
	}
	
	const isCredit = netPremium > 0;
	
	// Calculate breakevens (simplified)
	const breakevens: number[] = [];
	for (let i = 1; i < payoffData.length; i++) {
		const prev = payoffData[i - 1];
		const curr = payoffData[i];
		
		// Sign change indicates breakeven
		if ((prev.pnl < 0 && curr.pnl >= 0) || (prev.pnl >= 0 && curr.pnl < 0)) {
			breakevens.push(curr.price);
		}
	}
	
	// Reward/Risk ratio
	let rewardRiskRatio: number | string = 0;
	if (maxProfit === 'unlimited' || maxLoss === 'unlimited') {
		rewardRiskRatio = 'N/A';
	} else if (maxLoss !== 0) {
		rewardRiskRatio = parseFloat((Math.abs(maxProfit / maxLoss)).toFixed(2));
	}
	
	// Probability of Profit (simplified - count profitable prices)
	const profitablePoints = payoffData.filter(p => p.pnl > 0).length;
	const probabilityOfProfit = (profitablePoints / payoffData.length) * 100;
	
	// Time value and intrinsic value (simplified)
	const timeValue = Math.abs(netPremium);
	const intrinsicValue = 0; // Would need current spot vs strikes calculation
	
	// Margins (placeholder - these should be fetched from calculateBasketMargins API)
	const fundsNeeded = Math.abs(netPremium);
	const marginNeeded = typeof maxLoss === 'number' ? Math.abs(maxLoss) * 0.3 : Math.abs(netPremium) * 2;
	const marginAvailable = 100000; // TODO: Fetch from broker API
	
	// Calculate percentages
	const numericMaxLoss = typeof maxLoss === 'number' ? maxLoss : 0;
	const numericMaxProfit = typeof maxProfit === 'number' ? maxProfit : 0;
	const investment = isCredit ? Math.abs(numericMaxLoss) : Math.abs(netPremium);
	const maxProfitPercent = investment > 0 && typeof maxProfit === 'number' ? (maxProfit / investment) * 100 : 0;
	const maxLossPercent = investment > 0 && typeof maxLoss === 'number' ? (maxLoss / investment) * 100 : 0;
	
	return {
		maxProfit: typeof maxProfit === 'number' ? Math.round(maxProfit) : 'unlimited',
		maxProfitPercent: Math.round(maxProfitPercent),
		maxLoss: typeof maxLoss === 'number' ? Math.round(maxLoss) : 'unlimited',
		maxLossPercent: Math.round(maxLossPercent),
		breakevens,
		rewardRiskRatio,
		probabilityOfProfit: Math.round(probabilityOfProfit),
		netPremium: Math.round(netPremium),
		isCredit,
		timeValue: Math.round(timeValue),
		intrinsicValue: Math.round(intrinsicValue),
		fundsNeeded: Math.round(fundsNeeded),
		marginNeeded: Math.round(marginNeeded),
		marginAvailable
	};
}

/**
 * Format currency for display
 */
export function formatCurrency(amount: number, plusSign = true): string {
	if (amount === 0) return '₹0';
	const sign = amount > 0 ? (plusSign ? '+' : '') : '-';
	return `${sign}₹${Math.abs(amount).toLocaleString('en-IN', {
		minimumFractionDigits: 0,
		maximumFractionDigits: 0
	})}`;
}

/**
 * Format percentage for display
 */
export function formatPercent(percent: number): string {
	const sign = percent >= 0 ? '+' : '';
	return `${sign}${percent}%`;
}
