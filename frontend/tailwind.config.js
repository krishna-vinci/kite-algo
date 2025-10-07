/** @type {import('tailwindcss').Config} */
export default {
	content: [
		'./src/**/*.{html,svelte}',
		'./node_modules/svelte-ux/**/*.{svelte,js}',
		'./node_modules/layerchart/**/*.{svelte,js}'
	],
	darkMode: 'class',
	theme: {
		extend: {}
	},
	plugins: []
};
