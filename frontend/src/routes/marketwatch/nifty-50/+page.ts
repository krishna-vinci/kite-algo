import type { PageLoad } from './$types';
import { getApiBase } from '$lib/api';

export const load: PageLoad = async ({ fetch }) => {
    const response = await fetch(`${getApiBase()}/broker/nifty50`);
    const sectors = await response.json();
    return { sectors };
};