import type { Handle } from '@sveltejs/kit';

// Internal backend URL (within Docker network)
const API_BASE_URL = process.env.API_BASE_URL || 'http://finance-app:8777';

export const handle: Handle = async ({ event, resolve }) => {
	// Proxy /broker requests to the backend
	if (event.url.pathname.startsWith('/broker')) {
		const targetUrl = `${API_BASE_URL}${event.url.pathname}${event.url.search}`;
		
		try {
			// Create a new request with the same method, headers, and body
			// We strip origin/host headers to avoid CORS issues at the backend if needed,
			// but usually forwarding everything is fine or better.
			// However, 'host' header might need to be updated to the backend's host if backend checks it.
			// For Docker internal comms, usually just forwarding is okay.
			
			const response = await fetch(targetUrl, {
				method: event.request.method,
				headers: event.request.headers,
				body: event.request.body,
				// @ts-ignore - duplex option needed for streaming/body forwarding in some node fetch versions
				duplex: 'half' 
			});

			return new Response(response.body, {
				status: response.status,
				headers: response.headers
			});
		} catch (err) {
			console.error('Proxy error:', err);
			return new Response(JSON.stringify({ error: 'Backend unavailable' }), { status: 503 });
		}
	}

	return resolve(event);
};
