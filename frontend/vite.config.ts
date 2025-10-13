import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	server: {
		proxy: {
			'/broker': {
				target: 'http://finance-app:8777',
				changeOrigin: true,
				rewrite: (path) => path.replace(/^\/broker/, '/broker')
			}
		}
	}
});
