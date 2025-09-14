<script>
	import { onMount } from 'svelte';

	// State variables
	let isLoggedIn = false;
	let userName = '';
	let isLoading = false;

	// API base URL - using the backend port from the Docker configuration
	const API_BASE_URL = 'http://localhost:8777';

	// Check login status on component mount
	onMount(async () => {
		checkLoginStatus();
	});

	// Check if user is logged in by checking for session cookie
	async function checkLoginStatus() {
		try {
			const response = await fetch(`${API_BASE_URL}/broker/profile_kite`, {
				credentials: 'include'
			});
			
			if (response.ok) {
				const profile = await response.json();
				isLoggedIn = true;
				userName = profile.user_shortname || profile.user_name || profile.email || 'User';
			} else {
				isLoggedIn = false;
				userName = '';
			}
		} catch (error) {
			isLoggedIn = false;
			userName = '';
		}
	}

	// Login function
	async function login() {
		isLoading = true;
		try {
			const response = await fetch(`${API_BASE_URL}/broker/login_kite`, {
				method: 'POST',
				credentials: 'include'
			});

			if (response.ok) {
				const data = await response.json();
				isLoggedIn = true;
				userName = data.profile.user_shortname || data.profile.user_name || data.profile.email || 'User';
			} else {
				console.error('Login failed:', response.status);
			}
		} catch (error) {
			console.error('Login error:', error);
		} finally {
			isLoading = false;
		}
	}

	// Logout function
	async function logout() {
		isLoading = true;
		try {
			const response = await fetch(`${API_BASE_URL}/broker/logout_kite`, {
				method: 'POST',
				credentials: 'include'
			});

			if (response.ok) {
				isLoggedIn = false;
				userName = '';
			} else {
				console.error('Logout failed:', response.status);
			}
		} catch (error) {
			console.error('Logout error:', error);
		} finally {
			isLoading = false;
		}
	}
</script>

<div class="absolute top-4 right-4 flex items-center space-x-2">
	{#if isLoggedIn}
		<div class="flex items-center space-x-2">
			<span class="text-white font-medium">Hello, {userName}</span>
			<button 
				on:click={logout}
				class="bg-red-500 hover:bg-red-600 text-white px-4 py-2 rounded-md transition duration-200"
				disabled={isLoading}
			>
				{isLoading ? 'Logging out...' : 'Logout'}
			</button>
		</div>
	{:else}
		<button 
			on:click={login}
			class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded-md transition duration-200"
			disabled={isLoading}
		>
			{isLoading ? 'Logging in...' : 'Login'}
		</button>
	{/if}
</div>

<style>
	/* Add any additional styling here if needed */
</style>