import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8777';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	server: {
		proxy: {
			'/api': {
				target: apiTarget,
				changeOrigin: true,
				ws: true,
				rewrite: (path) => path.replace(/^\/api/, '/api')
			},
			'/mcp': {
				target: apiTarget,
				changeOrigin: true,
				ws: true,
				rewrite: (path) => path.replace(/^\/mcp/, '/mcp')
			},
			'/llm': {
				target: apiTarget,
				changeOrigin: true,
				ws: true,
				rewrite: (path) => path.replace(/^\/llm/, '/llm')
			}
		}
	},
	ssr: {
		noExternal: ['svelte-sonner']
	}
});
