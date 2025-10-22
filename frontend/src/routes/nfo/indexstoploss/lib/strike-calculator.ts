/**
 * Strike Calculator for Strategy Builder V2
 * Calculates actual strikes based on ATM and strategy template
 */

import type { StrategyTemplate, StrategyLeg } from './strategy-templates';
import type { OptionChainStrike } from '../types';

export interface CalculatedStrike {
	strike: number;
	optionType: 'CE' | 'PE';
	transactionType: 'BUY' | 'SELL';
	strikeOffset: number; // For display/adjustment
	ltp?: number;
	delta?: number;
	iv?: number;
}

/**
 * Find ATM strike from option chain
 */
export function findATMStrike(
	strikes: OptionChainStrike[],
	spotPrice: number
): number {
	if (strikes.length === 0) return 0;

	// Find strike closest to spot price
	let closestStrike = strikes[0].strike;
	let minDiff = Math.abs(strikes[0].strike - spotPrice);

	for (const s of strikes) {
		const diff = Math.abs(s.strike - spotPrice);
		if (diff < minDiff) {
			minDiff = diff;
			closestStrike = s.strike;
		}
	}

	return closestStrike;
}

/**
 * Calculate strikes for a strategy template
 */
export function calculateStrategyStrikes(
	template: StrategyTemplate,
	atmStrike: number,
	underlying: string = 'NIFTY'
): CalculatedStrike[] {
	// Determine if it's BANKNIFTY or NIFTY
	const isBankNifty = underlying.toUpperCase().includes('BANK');
	
	// Get instrument config based on underlying
	const instrumentConfig = isBankNifty 
		? template.instruments.banknifty 
		: template.instruments.nifty;

	const calculatedStrikes: CalculatedStrike[] = [];

	// Map each leg with its corresponding offset from the instruments config
	template.legs.forEach((leg, index) => {
		const legStrikeOffset = instrumentConfig.strikeOffsets[index] || 0;
		const actualStrike = atmStrike + legStrikeOffset * instrumentConfig.strikeGap;

		calculatedStrikes.push({
			strike: actualStrike,
			optionType: leg.optionType,
			transactionType: leg.transactionType,
			strikeOffset: legStrikeOffset
		});
	});

	return calculatedStrikes;
}

/**
 * Adjust strike by step (for +/- buttons)
 */
export function adjustStrike(
	currentStrike: number,
	step: number,
	underlying: string = 'NIFTY'
): number {
	// Determine strike gap
	const strikeGap = underlying.toUpperCase().includes('BANK') ? 100 : 50;
	return currentStrike + step * strikeGap;
}

/**
 * Enrich calculated strikes with chain data
 */
export function enrichStrikesWithChainData(
	calculatedStrikes: CalculatedStrike[],
	chainStrikes: OptionChainStrike[]
): CalculatedStrike[] {
	return calculatedStrikes.map((calc) => {
		const chainStrike = chainStrikes.find((cs) => cs.strike === calc.strike);

		if (!chainStrike) return calc;

		const optionSide = calc.optionType === 'CE' ? chainStrike.ce : chainStrike.pe;

		return {
			...calc,
			ltp: optionSide?.ltp,
			delta: optionSide?.greeks?.delta,
			iv: optionSide?.greeks?.iv
		};
	});
}

/**
 * Calculate net premium for selected strikes
 */
export function calculateNetPremium(
	strikes: CalculatedStrike[],
	lots: number = 1,
	lotSize: number = 25
): {
	netPremium: number;
	creditDebit: 'CREDIT' | 'DEBIT';
	totalCost: number;
} {
	let totalPremium = 0;

	for (const strike of strikes) {
		const premium = strike.ltp || 0;
		const sign = strike.transactionType === 'SELL' ? 1 : -1;
		totalPremium += sign * premium * lots * lotSize;
	}

	return {
		netPremium: totalPremium,
		creditDebit: totalPremium >= 0 ? 'CREDIT' : 'DEBIT',
		totalCost: Math.abs(totalPremium)
	};
}

/**
 * Validate if strikes are available in chain
 */
export function validateStrikes(
	calculatedStrikes: CalculatedStrike[],
	chainStrikes: OptionChainStrike[]
): { valid: boolean; missingStrikes: number[] } {
	const chainStrikeValues = chainStrikes.map((cs) => cs.strike);
	const missingStrikes: number[] = [];

	for (const calc of calculatedStrikes) {
		if (!chainStrikeValues.includes(calc.strike)) {
			missingStrikes.push(calc.strike);
		}
	}

	return {
		valid: missingStrikes.length === 0,
		missingStrikes
	};
}

/**
 * Get strike display label
 */
export function getStrikeLabel(
	strike: number,
	atmStrike: number,
	optionType: 'CE' | 'PE',
	transactionType: 'BUY' | 'SELL'
): string {
	const isATM = strike === atmStrike;
	const moneyness = strike > atmStrike ? 'OTM' : strike < atmStrike ? 'ITM' : 'ATM';
	const action = transactionType === 'BUY' ? 'B' : 'S';

	if (optionType === 'CE') {
		// For CE: Above ATM is OTM, Below ATM is ITM
		const label = strike > atmStrike ? 'OTM' : strike < atmStrike ? 'ITM' : 'ATM';
		return `${strike} CE ${action} (${label})`;
	} else {
		// For PE: Below ATM is OTM, Above ATM is ITM
		const label = strike < atmStrike ? 'OTM' : strike > atmStrike ? 'ITM' : 'ATM';
		return `${strike} PE ${action} (${label})`;
	}
}
