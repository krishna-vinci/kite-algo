import type { PageLoad } from './$types';
import { getApiBase } from '$lib/api';

export const load: PageLoad = async ({ fetch }) => {
	const response = await fetch(`${getApiBase()}/api/nifty50`);
	const instruments = await response.json();
	return { instruments };
};