<script lang="ts">
	import { page } from '$app/stores';
	import LoginLogout from './LoginLogout.svelte';
	import favicon from '$lib/assets/favicon.svg';
	import SunIcon from "@lucide/svelte/icons/sun";
	import MoonIcon from "@lucide/svelte/icons/moon";

	import { resetMode, setMode } from "mode-watcher";
	import * as DropdownMenu from "$lib/components/ui/dropdown-menu/index.js";
	import { buttonVariants } from "$lib/components/ui/button/index.js";

	import { Button } from '$lib/components/ui/button/index.js';
	import {
		Root as NavigationMenu,
		NavigationMenuList,
		NavigationMenuItem,
		NavigationMenuLink,
		NavigationMenuTrigger,
		NavigationMenuContent,
		NavigationMenuViewport,
		NavigationMenuIndicator
	} from '$lib/components/ui/navigation-menu/index.js';
	import {
		Sheet,
		SheetContent,
		SheetHeader,
		SheetTitle,
		SheetTrigger
	} from '$lib/components/ui/sheet/index.js';

	const navItems = [
		{ name: 'Dashboard', href: '/' },
		{ name: 'Market Watch', href: '/marketwatch' },
		{ name: 'Orders', href: '/orders' },
		{ name: 'Investing', href: '/holdings' },
		{ name: 'Trades', href: '/positions' },
		{ name: 'Alerts', href: '/alerts' },
		{ name: 'Strategies', href: '/strategies/momentum' }
	];
</script>

<div class="border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
	<div class="container flex h-14 items-center">
		<!-- Brand/Logo -->
		<a href="/" class="mr-4 hidden md:flex">
			<img src={favicon} alt="Kite App Logo" class="h-8 w-auto" />
		</a>

		<!-- Desktop Navigation -->
		<div class="flex flex-1 items-center justify-center md:justify-center">
			<NavigationMenu class="hidden md:flex">
				<NavigationMenuList class="mx-auto">
					{#each navItems as item}
						<NavigationMenuItem>
							<NavigationMenuLink
								href={item.href}
								class="group inline-flex h-9 w-max items-center justify-center rounded-md bg-background px-4 py-2 text-sm font-medium transition-colors hover:bg-accent focus:bg-accent focus:outline-none focus:ring-1 focus:ring-ring {$page
									.url
									.pathname === item.href || ($page.url.pathname.startsWith(item.href) && item.href !== '/')
									? 'bg-primary text-primary-foreground'
									: 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'}"
							>
								{item.name}
							</NavigationMenuLink>
						</NavigationMenuItem>
					{/each}
				</NavigationMenuList>
			</NavigationMenu>
		</div>


		<!-- Right side: User controls -->
		<div class="flex items-center space-x-2">
		<DropdownMenu.Root>
		 <DropdownMenu.Trigger
		  class={buttonVariants({ variant: "outline", size: "icon" })}
		 >
		  <SunIcon
		   class="h-[1.2rem] w-[1.2rem] rotate-0 scale-100 !transition-all dark:-rotate-90 dark:scale-0"
		  />
		  <MoonIcon
		   class="absolute h-[1.2rem] w-[1.2rem] rotate-90 scale-0 !transition-all dark:rotate-0 dark:scale-100"
		  />
		  <span class="sr-only">Toggle theme</span>
		 </DropdownMenu.Trigger>
		 <DropdownMenu.Content align="end">
		  <DropdownMenu.Item onclick={() => setMode("light")}>Light</DropdownMenu.Item
		  >
		  <DropdownMenu.Item onclick={() => setMode("dark")}>Dark</DropdownMenu.Item>
		  <DropdownMenu.Item onclick={() => resetMode()}>System</DropdownMenu.Item>
		 </DropdownMenu.Content>
		</DropdownMenu.Root>
			<LoginLogout />
		</div>

		<!-- Mobile menu trigger -->
		<Sheet>
			<SheetTrigger class="mr-2 md:hidden">
				<Button variant="ghost" size="icon" aria-label="Open menu" type="button">
					<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true">
						<path stroke-linecap="round" d="M4 6h16M4 12h16M4 18h16" />
					</svg>
					<span class="sr-only">Open menu</span>
				</Button>
			</SheetTrigger>
			<SheetContent side="right">
				<SheetHeader class="space-y-2">
					<SheetTitle>Menu</SheetTitle>
				</SheetHeader>
				<div class="py-4">
					<ul class="space-y-2">
						{#each navItems as item}
							<li>
								<a
									href={item.href}
									class="block select-none space-y-1 rounded-md px-2 py-1.5 text-sm font-medium leading-none {$page
										.url
										.pathname === item.href || ($page.url.pathname.startsWith(item.href) && item.href !== '/')
										? 'bg-primary text-primary-foreground hover:bg-primary'
										: 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'}"
								>
									{item.name}
								</a>
							</li>
						{/each}
					</ul>
				</div>
			</SheetContent>
		</Sheet>
	</div>
</div>
