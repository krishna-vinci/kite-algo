import type { DeepPartial, ChartOptions, ColorType, LineWidth } from 'lightweight-charts';
import type { ThemeTokens } from './types';

/**
 * A theme preset with light colors.
 */
export const lightTheme: ThemeTokens = {
	layout: {
		background: '#ffffff',
		textColor: '#191919'
	},
	grid: {
		vertLines: '#e0e0e0',
		horzLines: '#e0e0e0'
	},
	priceScaleBorder: '#cccccc',
	timeScaleBorder: '#cccccc',
	crosshair: {
		color: '#555555'
	},
	candle: {
		up: '#26a69a',
		down: '#ef5350',
		wickUp: '#26a69a',
		wickDown: '#ef5350',
		borderVisible: false
	},
	line: {
		color: '#2196f3'
	}
};

/**
 * A theme preset with dark colors.
 */
export const darkTheme: ThemeTokens = {
	layout: {
		background: '#1e1e1e',
		textColor: '#d1d4dc'
	},
	grid: {
		vertLines: '#2b2b43',
		horzLines: '#2b2b43'
	},
	priceScaleBorder: '#2b2b43',
	timeScaleBorder: '#2b2b43',
	crosshair: {
		color: '#a0a0a0'
	},
	candle: {
		up: '#26a69a',
		down: '#ef5350',
		wickUp: '#26a69a',
		wickDown: '#ef5350',
		borderVisible: false
	},
	line: {
		color: '#2196f3'
	}
};

/**
 * Maps theme tokens to lightweight-charts ChartOptions.
 * @param tokens - The theme tokens to apply.
 * @returns A DeepPartial<ChartOptions> object.
 */
export function toChartOptions(tokens: ThemeTokens): DeepPartial<ChartOptions> {
	return {
		layout: {
			background: {
				type: 'solid' as ColorType,
				color: tokens.layout.background
			},
			textColor: tokens.layout.textColor,
			fontFamily: tokens.layout.fontFamily
		},
		grid: {
			vertLines: {
				color: tokens.grid.vertLines
			},
			horzLines: {
				color: tokens.grid.horzLines
			}
		},
		crosshair: {
			vertLine: {
				color: tokens.crosshair.color,
				width: tokens.crosshair.width as LineWidth | undefined
			},
			horzLine: {
				color: tokens.crosshair.color,
				width: tokens.crosshair.width as LineWidth | undefined
			}
		},
		rightPriceScale: {
			borderColor: tokens.priceScaleBorder
		},
		timeScale: {
			borderColor: tokens.timeScaleBorder
		}
	};
}

/**
 * Resolves a theme string or a theme object into a ThemeTokens object.
 * @param theme - The theme to resolve. Can be 'light', 'dark', or a custom ThemeTokens object.
 * @returns The resolved ThemeTokens object.
 */
export function resolveThemeTokens(theme?: 'light' | 'dark' | ThemeTokens): ThemeTokens {
	if (!theme || theme === 'light') {
		return lightTheme;
	}
	if (theme === 'dark') {
		return darkTheme;
	}
	return theme;
}