import { writable } from 'svelte/store';
import { browser } from '$app/environment';

export type Theme = 'light' | 'dark';

const THEME_STORAGE_KEY = 'theme';
const THEME_CLASS_DARK = 'dark';

const createThemeStore = () => {
	const { subscribe, set } = writable<Theme>('light');

	// Helper function to apply theme to DOM
	const applyTheme = (theme: Theme) => {
		if (!browser) return;
		
		const html = document.documentElement;
		if (theme === 'dark') {
			html.classList.add(THEME_CLASS_DARK);
		} else {
			html.classList.remove(THEME_CLASS_DARK);
		}
	};

	// Helper function to get system theme preference
	const getSystemTheme = (): Theme => {
		if (!browser) return 'light';
		return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
	};

	// Initialize theme on client-side
	if (browser) {
		const storedTheme = localStorage.getItem(THEME_STORAGE_KEY) as Theme | null;
		const systemTheme = getSystemTheme();
		const initialTheme = storedTheme ?? systemTheme;
		
		applyTheme(initialTheme);
		set(initialTheme);

		// Listen for system theme changes
		const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
		const handleSystemThemeChange = (e: MediaQueryListEvent) => {
			// Only update if no theme is stored (user hasn't made a preference)
			if (!localStorage.getItem(THEME_STORAGE_KEY)) {
				const newSystemTheme = e.matches ? 'dark' : 'light';
				applyTheme(newSystemTheme);
				set(newSystemTheme);
			}
		};
		
		mediaQuery.addEventListener('change', handleSystemThemeChange);
	}

	return {
		subscribe,
		set: (value: Theme) => {
			if (browser) {
				applyTheme(value);
				localStorage.setItem(THEME_STORAGE_KEY, value);
			}
			set(value);
		},
		toggle: () => {
			if (browser) {
				const currentTheme = document.documentElement.classList.contains(THEME_CLASS_DARK) ? 'dark' : 'light';
				const newTheme: Theme = currentTheme === 'dark' ? 'light' : 'dark';
				
				applyTheme(newTheme);
				localStorage.setItem(THEME_STORAGE_KEY, newTheme);
				set(newTheme);
			}
		},
		// Reset to system preference
		resetToSystem: () => {
			if (browser) {
				localStorage.removeItem(THEME_STORAGE_KEY);
				const systemTheme = getSystemTheme();
				applyTheme(systemTheme);
				set(systemTheme);
			}
		}
	};
};

export const theme = createThemeStore();