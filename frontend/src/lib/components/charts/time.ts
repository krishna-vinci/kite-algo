/**
 * Converts various date/time representations to a Unix timestamp in seconds (UTC).
 * @param input - The date/time to convert. Can be a number (ms or s), string (ISO, Y-M-D), Date object, or an object with date components.
 * @returns The Unix timestamp in seconds.
 */
export function toUnixSeconds(
	input: number | string | Date | { year: number; month: number; day: number; hour?: number; minute?: number }
): number {
	if (typeof input === 'number') {
		// Assume seconds if it's a typical Unix timestamp, otherwise milliseconds.
		return input > 10000000000 ? Math.floor(input / 1000) : input;
	}
	if (input instanceof Date) {
		return Math.floor(input.getTime() / 1000);
	}
	if (typeof input === 'string') {
		return Math.floor(new Date(input).getTime() / 1000);
	}
	if (typeof input === 'object') {
		const { year, month, day, hour = 0, minute = 0 } = input;
		const date = new Date(Date.UTC(year, month - 1, day, hour, minute));
		return Math.floor(date.getTime() / 1000);
	}
	throw new Error('Invalid input for toUnixSeconds');
}

/**
 * Creates a time formatter for a specific timezone.
 * @param tz - The IANA timezone name (e.g., 'Asia/Kolkata', 'America/New_York').
 * @param opts - Formatting options.
 * @returns A function that formats a Unix timestamp (seconds) into a time string.
 */
export function makeTzFormatter(
	tz: string,
	opts: Intl.DateTimeFormatOptions = {
		hour: '2-digit',
		minute: '2-digit',
		second: '2-digit',
		hour12: false,
		timeZone: tz
	}
): (tsSeconds: number) => string {
	const formatter = new Intl.DateTimeFormat('en-US', opts);
	return (tsSeconds: number) => formatter.format(tsSeconds * 1000);
}

/**
 * A pre-configured time formatter for Indian Standard Time (IST).
 */
export const istTimeFormatter = makeTzFormatter('Asia/Kolkata');

/**
 * Creates a date formatter for a specific timezone and locale.
 * @param tz - The IANA timezone name.
 * @param locale - The locale to use for formatting (e.g., 'en-US', 'en-GB').
 * @returns A function that formats a Unix timestamp (seconds) into a localized date string.
 */
export function makeLocalizedDateFormatter(
	tz: string,
	locale = 'en-US'
): (tsSeconds: number) => string {
	const formatter = new Intl.DateTimeFormat(locale, {
		year: 'numeric',
		month: 'short',
		day: 'numeric',
		timeZone: tz
	});
	return (tsSeconds: number) => formatter.format(tsSeconds * 1000);
}