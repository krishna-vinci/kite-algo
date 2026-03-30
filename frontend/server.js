import { handler } from './build/handler.js';
import express from 'express';
import { createProxyMiddleware } from 'http-proxy-middleware';

const app = express();

// Configuration
const PORT = process.env.PORT || 80;
const API_BASE_URL = process.env.API_BASE_URL || 'http://finance-app:8777';

console.log(`Starting custom server on port ${PORT}`);
console.log(`Proxying /api, /mcp, /llm to ${API_BASE_URL}`);

// Proxy /api, /mcp, /llm requests (handles both HTTP API and WebSockets)
// We use a single middleware mounted at root with pathFilter to avoid Express stripping prefixes.
// This ensures both HTTP and WebSocket requests preserve their full paths.
app.use(
	createProxyMiddleware({
		target: API_BASE_URL,
		changeOrigin: true,
		ws: true, // Enable WebSocket proxying
		pathFilter: ['/api', '/mcp', '/llm'],
		logLevel: 'debug'
	})
);

// Let SvelteKit handle everything else
app.use(handler);

app.listen(PORT, '0.0.0.0', () => {
	console.log(`Listening on port ${PORT}`);
});
