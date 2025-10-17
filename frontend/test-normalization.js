/**
 * Test script to verify interval normalization consistency
 * Run in browser console on the NFO charts page
 */

// Test normalization function
const TIMEFRAME_ALIASES = {
	'1m': '1minute', min: '1minute', minute: '1minute',
	'3m': '3minute', '3minute': '3minute',
	'5m': '5minute', '5minute': '5minute',
	'10m': '10minute', '10minute': '10minute',
	'15m': '15minute', '15minute': '15minute',
	'30m': '30minute', '30minute': '30minute',
	'60m': '60minute', '1h': '60minute', '60minute': '60minute',
	'1d': 'day', day: 'day'
};

function normalizeTimeframe(timeframe) {
	const normalized = TIMEFRAME_ALIASES[timeframe.toLowerCase()];
	if (!normalized) {
		throw new Error(`Invalid timeframe alias: "${timeframe}"`);
	}
	return normalized;
}

// Run tests
console.log('=== Interval Normalization Tests ===\n');

const testCases = [
	{ input: '5m', expected: '5minute' },
	{ input: '1h', expected: '60minute' },
	{ input: '1d', expected: 'day' },
	{ input: '15m', expected: '15minute' },
	{ input: '5minute', expected: '5minute' }, // Already normalized
];

let passed = 0;
let failed = 0;

testCases.forEach(({ input, expected }) => {
	const result = normalizeTimeframe(input);
	const status = result === expected ? '✅ PASS' : '❌ FAIL';
	console.log(`${status}: "${input}" → "${result}" (expected: "${expected}")`);
	
	if (result === expected) passed++;
	else failed++;
});

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);

// Test key generation
console.log('=== Key Generation Test ===\n');

const token = 256265;
const intervals = ['5m', '1h', '15m'];

intervals.forEach(interval => {
	const normalized = normalizeTimeframe(interval);
	const key = `${token}|${normalized}`;
	console.log(`Token: ${token}, Interval: "${interval}" → Key: "${key}"`);
});

console.log('\n=== Data Structure Test ===\n');

// Simulate the data structure
const candlesByTokenAndInterval = new Map();
candlesByTokenAndInterval.set(token, new Map());

// Store with normalized key
const normalizedInterval = normalizeTimeframe('5m');
candlesByTokenAndInterval.get(token).set(normalizedInterval, [{time: 123, open: 100}]);

// Try to retrieve with different formats
console.log('Stored with key: "5minute"');
console.log('Retrieve with "5m":', candlesByTokenAndInterval.get(token).get(normalizeTimeframe('5m')) ? '✅ FOUND' : '❌ NOT FOUND');
console.log('Retrieve with "5minute":', candlesByTokenAndInterval.get(token).get('5minute') ? '✅ FOUND' : '❌ NOT FOUND');
console.log('Retrieve with "5min":', candlesByTokenAndInterval.get(token).get('5min') ? '✅ FOUND' : '❌ NOT FOUND (expected - invalid alias)');

console.log('\n=== Test Complete ===');
console.log('All intervals must normalize consistently for streaming to work!');
