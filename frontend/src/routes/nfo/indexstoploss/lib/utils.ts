/**
 * Utility functions for Position Protection System
 * Phase 1: Formatting and helper functions
 */

import type { MonitoringMode, StrategyStatus } from '../types';

/**
 * Format currency value
 */
export function formatCurrency(value: number, decimals: number = 2): string {
	return `₹${value.toFixed(decimals).replace(/\B(?=(\d{3})+(?!\d))/g, ',')}`;
}

/**
 * Format number with commas
 */
export function formatNumber(value: number, decimals: number = 2): string {
	return value.toFixed(decimals).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/**
 * Format P&L with color and sign
 */
export function formatPnL(value: number, includeSign: boolean = true): string {
	const sign = includeSign && value > 0 ? '+' : '';
	return `${sign}${formatCurrency(value)}`;
}

/**
 * Get P&L color class
 */
export function getPnLColor(value: number): string {
	if (value > 0) return 'text-green-500';
	if (value < 0) return 'text-red-500';
	return 'text-muted-foreground';
}

/**
 * Get status badge variant
 */
export function getStatusVariant(status: StrategyStatus): 'default' | 'secondary' | 'destructive' | 'outline' {
	switch (status) {
		case 'active':
		case 'partial':
			return 'default';
		case 'paused':
			return 'secondary';
		case 'error':
		case 'triggered':
			return 'destructive';
		default:
			return 'outline';
	}
}

/**
 * Get status color
 */
export function getStatusColor(status: StrategyStatus): string {
	switch (status) {
		case 'active':
			return 'text-green-500';
		case 'partial':
			return 'text-blue-500';
		case 'paused':
			return 'text-yellow-500';
		case 'error':
		case 'triggered':
			return 'text-red-500';
		case 'completed':
			return 'text-gray-500';
		default:
			return 'text-muted-foreground';
	}
}

/**
 * Get mode badge color
 */
export function getModeColor(mode: MonitoringMode): string {
	switch (mode) {
		case 'index':
			return 'bg-blue-500/10 text-blue-500 border-blue-500/20';
		case 'premium':
			return 'bg-purple-500/10 text-purple-500 border-purple-500/20';
		case 'hybrid':
			return 'bg-orange-500/10 text-orange-500 border-orange-500/20';
		case 'combined_premium':
			return 'bg-green-500/10 text-green-500 border-green-500/20';
		default:
			return 'bg-gray-500/10 text-gray-500 border-gray-500/20';
	}
}

/**
 * Get mode display name
 */
export function getModeDisplayName(mode: MonitoringMode): string {
	switch (mode) {
		case 'index':
			return 'INDEX';
		case 'premium':
			return 'PREMIUM';
		case 'hybrid':
			return 'HYBRID';
		case 'combined_premium':
			return 'COMBINED';
		default:
			return mode.toUpperCase();
	}
}

/**
 * Format timestamp as relative time
 */
export function formatRelativeTime(timestamp: string): string {
	const date = new Date(timestamp);
	const now = new Date();
	const diffMs = now.getTime() - date.getTime();
	const diffSecs = Math.floor(diffMs / 1000);
	const diffMins = Math.floor(diffSecs / 60);
	const diffHours = Math.floor(diffMins / 60);
	const diffDays = Math.floor(diffHours / 24);
	
	if (diffSecs < 60) return `${diffSecs}s ago`;
	if (diffMins < 60) return `${diffMins}m ago`;
	if (diffHours < 24) return `${diffHours}h ago`;
	if (diffDays < 7) return `${diffDays}d ago`;
	
	return date.toLocaleDateString();
}

/**
 * Format timestamp as time
 */
export function formatTime(timestamp: string): string {
	const date = new Date(timestamp);
	return date.toLocaleTimeString('en-IN', { 
		hour: '2-digit', 
		minute: '2-digit',
		second: '2-digit'
	});
}

/**
 * Format timestamp as date
 */
export function formatDate(timestamp: string): string {
	const date = new Date(timestamp);
	return date.toLocaleDateString('en-IN', {
		day: '2-digit',
		month: 'short',
		year: 'numeric'
	});
}

/**
 * Get health status color
 */
export function getHealthStatusColor(status: string): string {
	switch (status.toLowerCase()) {
		case 'healthy':
			return 'text-green-500';
		case 'degraded':
			return 'text-yellow-500';
		case 'down':
		case 'stopped':
			return 'text-red-500';
		default:
			return 'text-muted-foreground';
	}
}

/**
 * Get WebSocket status color
 */
export function getWsStatusColor(status: string): string {
	switch (status.toLowerCase()) {
		case 'connected':
			return 'text-green-500';
		case 'connecting':
			return 'text-yellow-500';
		case 'disconnected':
			return 'text-red-500';
		default:
			return 'text-muted-foreground';
	}
}

/**
 * Calculate total P&L from positions
 */
export function calculateTotalPnL(positions: Array<{ pnl: number }>): number {
	return positions.reduce((sum, pos) => sum + pos.pnl, 0);
}

/**
 * Truncate text with ellipsis
 */
export function truncate(text: string, maxLength: number): string {
	if (text.length <= maxLength) return text;
	return text.substring(0, maxLength - 3) + '...';
}
