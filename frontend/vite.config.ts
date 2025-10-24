import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	server: {
		proxy: {
			'/broker': {
				target: 'http://127.0.0.1:8777',
				changeOrigin: true,
				ws: true,
				rewrite: (path) => path.replace(/^\/broker/, '/broker')
			}
		}
	},
	ssr: {
		noExternal: ['svelte-sonner']
	}
});
