import { redirect } from '@sveltejs/kit';
import type { LayoutServerLoad } from './$types';

export const load: LayoutServerLoad = async ({ fetch, url }) => {
	const pathname = url.pathname;
	const isLoginPage = pathname === '/login';

	let sessionStatus: any = null;
	try {
		const response = await fetch('/api/auth/session-status', { headers: { accept: 'application/json' } });
		if (response.ok) {
			sessionStatus = await response.json();
		}
	} catch {
		sessionStatus = null;
	}

	const authenticated = !!sessionStatus?.app?.authenticated;

	if (!authenticated && !isLoginPage) {
		throw redirect(302, `/login?next=${encodeURIComponent(pathname + url.search)}`);
	}

	if (authenticated && isLoginPage) {
		throw redirect(302, '/');
	}

	return {
		authenticated,
		sessionStatus
	};
};
