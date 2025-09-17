<script lang="ts">
  import { page } from '$app/stores';
  import LoginLogout from './LoginLogout.svelte';

  const navItems = [
    { name: 'Dashboard', href: '/' },
    { name: 'Market Watch', href: '/marketwatch' },
    { name: 'Orders', href: '/orders' },
    { name: 'Holdings', href: '/holdings' },
    { name: 'Positions', href: '/positions' },
    { name: 'Bids', href: '/bids' },
    { name: 'Funds', href: '/funds' },
    { name: 'Alerts', href: '/alerts' },
    { name: 'Strategies', href: '/strategies/momentum' },
  ];
</script>

<nav class="bg-white shadow-sm">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
    <div class="flex justify-between h-16">
      <div class="flex">
        <div class="flex-shrink-0 flex items-center">
          <!-- Logo -->
          <img class="h-8 w-auto" src="/favicon.svg" alt="Kite App Logo" />
        </div>
        <div class="hidden sm:-my-px sm:ml-6 sm:flex sm:space-x-8">
          {#each navItems as item}
            <a
              href={item.href}
              class="inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
              class:border-orange-500={$page.url.pathname === item.href}
              class:text-orange-600={($page.url.pathname === item.href)}
              class:border-transparent={!($page.url.pathname === item.href)}
              class:text-gray-500={!($page.url.pathname === item.href)}
              class:hover:border-gray-300={!($page.url.pathname === item.href)}
              class:hover:text-gray-700={!($page.url.pathname === item.href)}
              aria-current={$page.url.pathname === item.href ? 'page' : undefined}
            >
              {item.name}
            </a>
          {/each}
        </div>
      </div>
      <div class="hidden sm:ml-6 sm:flex sm:items-center">
        <!-- Notification Bell -->
        <button
          type="button"
          class="bg-white p-1 rounded-full text-gray-400 hover:text-gray-500 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-orange-500"
        >
          <span class="sr-only">View notifications</span>
          <!-- Heroicon name: outline/bell -->
          <svg
            class="h-6 w-6"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            aria-hidden="true"
          >
            <path
              stroke-linecap="round"
              stroke-linejoin="round"
              stroke-width="2"
              d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
            />
          </svg>
        </button>

        <!-- Shopping Cart -->
        <button
          type="button"
          class="ml-3 bg-white p-1 rounded-full text-gray-400 hover:text-gray-500 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-orange-500"
        >
          <span class="sr-only">View cart</span>
          <!-- Heroicon name: outline/shopping-cart -->
          <svg
            class="h-6 w-6"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            aria-hidden="true"
          >
            <path
              stroke-linecap="round"
              stroke-linejoin="round"
              stroke-width="2"
              d="M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 11-4 0 2 2 0 014 0z"
            />
          </svg>
        </button>

        <!-- Profile dropdown -->
        <LoginLogout />
      </div>
    </div>
  </div>
</nav>