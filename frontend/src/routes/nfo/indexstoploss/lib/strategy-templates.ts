/**
 * Strategy Templates for Strategy Builder V2
 * Defines all pre-built option strategies with auto-strike selection
 * 
 * ═══════════════════════════════════════════════════════════════════════════════
 * HOW TO ADJUST STRIKE OFFSETS:
 * ═══════════════════════════════════════════════════════════════════════════════
 * 
 * Each strategy has an `instruments` section where you can configure strike offsets
 * separately for NIFTY and BANKNIFTY:
 * 
 * instruments: {
 *   nifty: { 
 *     strikeGap: 50,              // Strike price gap (usually 50 for NIFTY)
 *     strikeOffsets: [0, 4, -4]   // Offset for each leg (in number of strikes)
 *   },
 *   banknifty: { 
 *     strikeGap: 100,             // Strike price gap (usually 100 for BANKNIFTY)
 *     strikeOffsets: [0, 2, -2]   // Offset for each leg (in number of strikes)
 *   }
 * }
 * 
 * - strikeOffsets array corresponds to legs array (same order)
 * - Positive offset = strikes above ATM (OTM for CE, ITM for PE)
 * - Negative offset = strikes below ATM (ITM for CE, OTM for PE)
 * - 0 = ATM strike
 * 
 * Example: Bull Call Spread with 2 legs
 *   legs: [
 *     { optionType: 'CE', transactionType: 'BUY', ... },   // Leg 0
 *     { optionType: 'CE', transactionType: 'SELL', ... }   // Leg 1
 *   ]
 *   instruments.nifty.strikeOffsets: [0, 4]
 *     → Leg 0 (BUY CE): ATM strike
 *     → Leg 1 (SELL CE): ATM + (4 × 50) = ATM + 200 points
 * 
 * ═══════════════════════════════════════════════════════════════════════════════
 */

export type StrategyCategory = 'bullish' | 'bearish' | 'neutral' | 'others';
export type OptionType = 'CE' | 'PE';
export type TransactionType = 'BUY' | 'SELL';
export type StrikeSelection = 'atm' | 'itm' | 'otm';

export interface StrategyLeg {
	optionType: OptionType;
	transactionType: TransactionType;
	strikeOffset: number; // Number of strikes from ATM (will be multiplied by instrument's strikeGap)
	strikeSelection: StrikeSelection;
}

export interface InstrumentConfig {
	strikeGap: number; // Strike price gap (e.g., 50 for NIFTY, 100 for BANKNIFTY)
	strikeOffsets: number[]; // Strike offsets for each leg (in number of strikes from ATM)
}

export interface StrategyTemplate {
	id: string;
	name: string;
	category: StrategyCategory;
	description: string;
	shortDesc: string; // For card display
	legs: StrategyLeg[]; // Base leg structure (optionType, transactionType, strikeSelection)
	defaultLots: number;
	// ═══ INSTRUMENT CONFIGURATION - EASY TO ADJUST ═══
	instruments: {
		nifty: InstrumentConfig;
		banknifty: InstrumentConfig;
	};
	// ═════════════════════════════════════════════════
	payoffType: 'limited' | 'unlimited'; // For chart rendering
	riskProfile: 'high' | 'medium' | 'low';
	minPayoffSVG?: string; // Mini payoff chart for card
}

// ═══════════════════════════════════════════════════════════════════════════════
// NEUTRAL STRATEGIES
// ═══════════════════════════════════════════════════════════════════════════════

export const neutralStrategies: StrategyTemplate[] = [
	{
		id: 'short_straddle',
		name: 'Short Straddle',
		category: 'neutral',
		description: 'Sell ATM Call and ATM Put to collect premium',
		shortDesc: 'Sell ATM CE + PE',
		legs: [
			{ optionType: 'CE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'atm' },
			{ optionType: 'PE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'atm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [0, 0] },
			banknifty: { strikeGap: 100, strikeOffsets: [0, 0] }
		},
		// ═══════════════════════════
		payoffType: 'limited',
		riskProfile: 'high'
	},
	{
		id: 'short_strangle',
		name: 'Short Strangle',
		category: 'neutral',
		description: 'Sell OTM Call and OTM Put to collect premium with wider range',
		shortDesc: 'Sell OTM CE + PE',
		legs: [
			{ optionType: 'CE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'otm' },
			{ optionType: 'PE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'otm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [4, -4] },
			banknifty: { strikeGap: 100, strikeOffsets: [10, -10] }
		},
		// ═══════════════════════════
		payoffType: 'limited',
		riskProfile: 'high'
	},
	{
		id: 'long_straddle',
		name: 'Long Straddle',
		category: 'neutral',
		description: 'Buy ATM Call and ATM Put for volatility play',
		shortDesc: 'Buy ATM CE + PE',
		legs: [
			{ optionType: 'CE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'atm' },
			{ optionType: 'PE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'atm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [0, 0] },
			banknifty: { strikeGap: 100, strikeOffsets: [0, 0] }
		},
		// ═══════════════════════════
		payoffType: 'unlimited',
		riskProfile: 'medium'
	},
	{
		id: 'long_strangle',
		name: 'Long Strangle',
		category: 'neutral',
		description: 'Buy OTM Call and OTM Put for large move expectation',
		shortDesc: 'Buy OTM CE + PE',
		legs: [
			{ optionType: 'CE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'otm' },
			{ optionType: 'PE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'otm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [4, -4] },
			banknifty: { strikeGap: 100, strikeOffsets: [10, -10] }
		},
		// ═══════════════════════════
		payoffType: 'unlimited',
		riskProfile: 'medium'
	},
	{
		id: 'iron_condor',
		name: 'Iron Condor',
		category: 'neutral',
		description: 'Defined risk neutral strategy with 4 legs',
		shortDesc: 'CE/PE spreads',
		legs: [
			{ optionType: 'CE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'otm' },
			{ optionType: 'CE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'otm' },
			{ optionType: 'PE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'otm' },
			{ optionType: 'PE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'otm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [2, 4, -2, -4] },
			banknifty: { strikeGap: 100, strikeOffsets: [2, 7, -2, -7] }
		},
		// ═══════════════════════════
		payoffType: 'limited',
		riskProfile: 'low'
	},
	{
		id: 'iron_butterfly',
		name: 'Iron Butterfly',
		category: 'neutral',
		description: 'Narrow range neutral strategy',
		shortDesc: 'ATM + wings',
		legs: [
			{ optionType: 'CE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'atm' },
			{ optionType: 'CE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'otm' },
			{ optionType: 'PE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'atm' },
			{ optionType: 'PE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'otm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [0, 4, 0, -4] },
			banknifty: { strikeGap: 100, strikeOffsets: [0, 5, 0, -5] }
		},
		// ═══════════════════════════
		payoffType: 'limited',
		riskProfile: 'low'
	}
];

// ═══════════════════════════════════════════════════════════════════════════════
// BULLISH STRATEGIES
// ═══════════════════════════════════════════════════════════════════════════════

export const bullishStrategies: StrategyTemplate[] = [
	{
		id: 'buy_call',
		name: 'Buy Call',
		category: 'bullish',
		description: 'Long call for upside potential',
		shortDesc: 'Buy ATM CE',
		legs: [
			{ optionType: 'CE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'atm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [0] },
			banknifty: { strikeGap: 100, strikeOffsets: [0] }
		},
		// ═══════════════════════════
		payoffType: 'unlimited',
		riskProfile: 'medium'
	},
	{
		id: 'sell_put',
		name: 'Sell Put',
		category: 'bullish',
		description: 'Short put to collect premium with bullish view',
		shortDesc: 'Sell ATM PE',
		legs: [
			{ optionType: 'PE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'atm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [0] },
			banknifty: { strikeGap: 100, strikeOffsets: [0] }
		},
		// ═══════════════════════════
		payoffType: 'limited',
		riskProfile: 'high'
	},
	{
		id: 'bull_call_spread',
		name: 'Bull Call Spread (Debit)',
		category: 'bullish',
		description: 'Buy ATM call, sell OTM call for defined risk upside',
		shortDesc: 'Buy ATM, Sell OTM CE',
		legs: [
			{ optionType: 'CE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'atm' },
			{ optionType: 'CE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'otm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [0, 4] },
			banknifty: { strikeGap: 100, strikeOffsets: [0, 5] }
		},
		// ═══════════════════════════
		payoffType: 'limited',
		riskProfile: 'low'
	},
	{
		id: 'bull_put_spread',
		name: 'Bull Put Spread (Credit)',
		category: 'bullish',
		description: 'Sell ATM put, buy OTM put for credit with defined risk',
		shortDesc: 'Sell ATM, Buy OTM PE',
		legs: [
			{ optionType: 'PE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'atm' },
			{ optionType: 'PE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'otm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [0, -4] },
			banknifty: { strikeGap: 100, strikeOffsets: [0, -5] }
		},
		// ═══════════════════════════
		payoffType: 'limited',
		riskProfile: 'low'
	}
];

// ═══════════════════════════════════════════════════════════════════════════════
// BEARISH STRATEGIES
// ═══════════════════════════════════════════════════════════════════════════════

export const bearishStrategies: StrategyTemplate[] = [
	{
		id: 'buy_put',
		name: 'Buy Put',
		category: 'bearish',
		description: 'Long put for downside potential',
		shortDesc: 'Buy ATM PE',
		legs: [
			{ optionType: 'PE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'atm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [0] },
			banknifty: { strikeGap: 100, strikeOffsets: [0] }
		},
		// ═══════════════════════════
		payoffType: 'unlimited',
		riskProfile: 'medium'
	},
	{
		id: 'sell_call',
		name: 'Sell Call',
		category: 'bearish',
		description: 'Short call to collect premium with bearish view',
		shortDesc: 'Sell ATM CE',
		legs: [
			{ optionType: 'CE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'atm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [0] },
			banknifty: { strikeGap: 100, strikeOffsets: [0] }
		},
		// ═══════════════════════════
		payoffType: 'limited',
		riskProfile: 'high'
	},
	{
		id: 'bear_call_spread',
		name: 'Bear Call Spread (Credit)',
		category: 'bearish',
		description: 'Sell ATM call, buy OTM call for credit with defined risk',
		shortDesc: 'Sell ATM, Buy OTM CE',
		legs: [
			{ optionType: 'CE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'atm' },
			{ optionType: 'CE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'otm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [0, 4] },
			banknifty: { strikeGap: 100, strikeOffsets: [0, 5] }
		},
		// ═══════════════════════════
		payoffType: 'limited',
		riskProfile: 'low'
	},
	{
		id: 'bear_put_spread',
		name: 'Bear Put Spread (Debit)',
		category: 'bearish',
		description: 'Buy ATM put, sell OTM put for defined risk downside',
		shortDesc: 'Buy ATM, Sell OTM PE',
		legs: [
			{ optionType: 'PE', transactionType: 'BUY', strikeOffset: 0, strikeSelection: 'atm' },
			{ optionType: 'PE', transactionType: 'SELL', strikeOffset: 0, strikeSelection: 'otm' }
		],
		defaultLots: 1,
		// ═══ ADJUST OFFSETS HERE ═══
		instruments: {
			nifty: { strikeGap: 50, strikeOffsets: [0, -4] },
			banknifty: { strikeGap: 100, strikeOffsets: [0, -5] }
		},
		// ═══════════════════════════
		payoffType: 'limited',
		riskProfile: 'low'
	}
];

// ═══════════════════════════════════════════════════════════════════════════════
// COMBINED EXPORT
// ═══════════════════════════════════════════════════════════════════════════════

export const allStrategies: StrategyTemplate[] = [
	...neutralStrategies,
	...bullishStrategies,
	...bearishStrategies
];

export function getStrategiesByCategory(category: StrategyCategory): StrategyTemplate[] {
	return allStrategies.filter((s) => s.category === category);
}

export function getStrategyById(id: string): StrategyTemplate | undefined {
	return allStrategies.find((s) => s.id === id);
}
