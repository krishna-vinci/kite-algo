<script lang="ts">
	import * as Resizable from "$lib/components/ui/resizable";
	import * as Tabs from "$lib/components/ui/tabs";
	import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "$lib/components/ui/card";
	import { Button } from "$lib/components/ui/button";
	import { Badge } from "$lib/components/ui/badge";

	// Tab states
	let selectedChartTab = $state("price-chart");
	let selectedStockTab = $state("top-stocks");
</script>

<div class="h-screen p-4 bg-background">
	<!-- Main horizontal split: 40% left panel, 60% right panel -->
	<Resizable.PaneGroup direction="horizontal" class="h-full">
		<!-- Left Panel (40%) - Trade Management -->
		<Resizable.Pane defaultSize={40} collapsedSize={5} collapsible minSize={20} class="p-4">
			<div class="h-full flex flex-col gap-4 overflow-y-auto">
				<!-- Header -->
				<Card>
					<CardHeader class="pb-3">
						<CardTitle class="text-lg">Trade Management</CardTitle>
						<CardDescription>Manage your NFO positions and orders</CardDescription>
					</CardHeader>
				</Card>

				<!-- Position List -->
				<Card class="flex-1">
					<CardHeader class="pb-3">
						<CardTitle class="text-base">Active Positions</CardTitle>
					</CardHeader>
					<CardContent class="p-0">
						<div class="divide-y">
							<div class="p-3 hover:bg-muted/50">
								<div class="flex justify-between items-center">
									<div>
										<div class="font-medium">BANKNIFTY 25DEC 48000 CE</div>
										<div class="text-sm text-muted-foreground">Long • 5 lots</div>
									</div>
									<div class="text-right">
										<Badge variant="default" class="mb-1">+2,450</Badge>
										<div class="text-sm text-muted-foreground">+12.5%</div>
									</div>
								</div>
							</div>
							<div class="p-3 hover:bg-muted/50">
								<div class="flex justify-between items-center">
									<div>
										<div class="font-medium">NIFTY 25DEC 24800 PE</div>
										<div class="text-sm text-muted-foreground">Short • 10 lots</div>
									</div>
									<div class="text-right">
										<Badge variant="destructive" class="mb-1">-1,200</Badge>
										<div class="text-sm text-muted-foreground">-8.3%</div>
									</div>
								</div>
							</div>
							<div class="p-3 hover:bg-muted/50">
								<div class="flex justify-between items-center">
									<div>
										<div class="font-medium">FINNIFTY 25DEC 20000 CE</div>
										<div class="text-sm text-muted-foreground">Long • 8 lots</div>
									</div>
									<div class="text-right">
										<Badge variant="default" class="mb-1">+3,100</Badge>
										<div class="text-sm text-muted-foreground">+15.2%</div>
									</div>
								</div>
							</div>
						</div>
					</CardContent>
				</Card>

				<!-- Order Entry Controls -->
				<Card>
					<CardHeader class="pb-3">
						<CardTitle class="text-base">Quick Order Entry</CardTitle>
					</CardHeader>
					<CardContent>
						<div class="grid grid-cols-2 gap-3">
							<div>
								<label class="text-sm font-medium mb-1 block">Symbol</label>
								<select class="w-full p-2 border rounded-md text-sm">
									<option>BANKNIFTY</option>
									<option>NIFTY</option>
									<option>FINNIFTY</option>
								</select>
							</div>
							<div>
								<label class="text-sm font-medium mb-1 block">Expiry</label>
								<select class="w-full p-2 border rounded-md text-sm">
									<option>25DEC</option>
									<option>01JAN</option>
									<option>08JAN</option>
								</select>
							</div>
							<div>
								<label class="text-sm font-medium mb-1 block">Strike</label>
								<input
									type="number"
									placeholder="48000"
									class="w-full p-2 border rounded-md text-sm"
								/>
							</div>
							<div>
								<label class="text-sm font-medium mb-1 block">Type</label>
								<select class="w-full p-2 border rounded-md text-sm">
									<option>CE</option>
									<option>PE</option>
								</select>
							</div>
							<div>
								<label class="text-sm font-medium mb-1 block">Lots</label>
								<input type="number" placeholder="5" class="w-full p-2 border rounded-md text-sm" />
							</div>
							<div>
								<label class="text-sm font-medium mb-1 block">Price</label>
								<input
									type="number"
									placeholder="150.25"
									class="w-full p-2 border rounded-md text-sm"
								/>
							</div>
						</div>
					</CardContent>
				</Card>

				<!-- Trade Modification Buttons -->
				<div class="grid grid-cols-3 gap-2">
					<Button variant="default" size="sm">Buy</Button>
					<Button variant="destructive" size="sm">Sell</Button>
					<Button variant="outline" size="sm">Modify</Button>
					<Button variant="outline" size="sm">Cancel</Button>
					<Button variant="outline" size="sm">Square Off</Button>
					<Button variant="outline" size="sm">Convert</Button>
				</div>
			</div>
		</Resizable.Pane>

		<Resizable.Handle />

		<!-- Right Panel (60%) - Charts and Stock Screening -->
		<Resizable.Pane defaultSize={60} class="p-4">
			<!-- Vertical split: 60% top (charts), 40% bottom (stock screening) -->
			<Resizable.PaneGroup direction="vertical" class="h-full">
				<!-- Chart Area (60%) -->
				<Resizable.Pane defaultSize={60} class="mb-2">
					<Card class="h-full">
						<CardHeader class="pb-3">
							<CardTitle class="text-base">Chart Analysis</CardTitle>
						</CardHeader>
						<CardContent class="p-0 overflow-y-auto">
							<Tabs.Root bind:value={selectedChartTab}>
								<Tabs.List class="grid w-full grid-cols-3 mx-4 mb-4">
									<Tabs.Trigger value="price-chart">Price Chart</Tabs.Trigger>
									<Tabs.Trigger value="technical-analysis">Technical Analysis</Tabs.Trigger>
									<Tabs.Trigger value="volume-chart">Volume Chart</Tabs.Trigger>
								</Tabs.List>

								<Tabs.Content value="price-chart" class="p-4">
									<div
										class="bg-muted/20 rounded-lg flex items-center justify-center border-2 border-dashed border-muted"
									>
										<div class="text-center">
											<div class="text-6xl mb-4">📈</div>
											<div class="text-lg font-medium">Price Chart</div>
											<div class="text-sm text-muted-foreground">
												Interactive price chart will be displayed here
											</div>
										</div>
									</div>
								</Tabs.Content>

								<Tabs.Content value="technical-analysis" class="p-4">
									<div
										class="bg-muted/20 rounded-lg flex items-center justify-center border-2 border-dashed border-muted"
									>
										<div class="text-center">
											<div class="text-6xl mb-4">📊</div>
											<div class="text-lg font-medium">Technical Analysis</div>
											<div class="text-sm text-muted-foreground">
												Technical indicators and analysis tools
											</div>
										</div>
									</div>
								</Tabs.Content>

								<Tabs.Content value="volume-chart" class="p-4">
									<div
										class="bg-muted/20 rounded-lg flex items-center justify-center border-2 border-dashed border-muted"
									>
										<div class="text-center">
											<div class="text-6xl mb-4">📉</div>
											<div class="text-lg font-medium">Volume Chart</div>
											<div class="text-sm text-muted-foreground">
												Volume analysis and trading patterns
											</div>
										</div>
									</div>
								</Tabs.Content>
							</Tabs.Root>
						</CardContent>
					</Card>
				</Resizable.Pane>

				<Resizable.Handle />

				<!-- Stock Screening Area (40%) -->
				<Resizable.Pane defaultSize={40}>
					<Card class="h-full">
						<CardHeader class="pb-3">
							<CardTitle class="text-base">Stock Screening</CardTitle>
						</CardHeader>
						<CardContent class="p-0 overflow-y-auto">
							<Tabs.Root bind:value={selectedStockTab}>
								<Tabs.List class="grid w-full grid-cols-3 mx-4 mb-4">
									<Tabs.Trigger value="top-stocks">Top Stocks</Tabs.Trigger>
									<Tabs.Trigger value="momentum-stocks">Momentum Stocks</Tabs.Trigger>
									<Tabs.Trigger value="custom-filters">Custom Filters</Tabs.Trigger>
								</Tabs.List>

								<Tabs.Content value="top-stocks" class="p-4">
									<div class="space-y-2">
										<div class="flex justify-between items-center p-2 border rounded">
											<div>
												<div class="font-medium">RELIANCE</div>
												<div class="text-sm text-muted-foreground">Large Cap</div>
											</div>
											<div class="text-right">
												<div class="font-medium">₹2,845.30</div>
												<Badge variant="default" class="text-xs">+1.2%</Badge>
											</div>
										</div>
										<div class="flex justify-between items-center p-2 border rounded">
											<div>
												<div class="font-medium">TCS</div>
												<div class="text-sm text-muted-foreground">Large Cap</div>
											</div>
											<div class="text-right">
												<div class="font-medium">₹4,125.80</div>
												<Badge variant="destructive" class="text-xs">-0.8%</Badge>
											</div>
										</div>
										<div class="flex justify-between items-center p-2 border rounded">
											<div>
												<div class="font-medium">HDFC BANK</div>
												<div class="text-sm text-muted-foreground">Large Cap</div>
											</div>
											<div class="text-right">
												<div class="font-medium">₹1,678.45</div>
												<Badge variant="default" class="text-xs">+2.1%</Badge>
											</div>
										</div>
									</div>
								</Tabs.Content>

								<Tabs.Content value="momentum-stocks" class="p-4">
									<div class="space-y-2">
										<div class="flex justify-between items-center p-2 border rounded">
											<div>
												<div class="font-medium">INFY</div>
												<div class="text-sm text-muted-foreground">IT Services</div>
											</div>
											<div class="text-right">
												<div class="font-medium">₹1,523.60</div>
												<Badge variant="default" class="text-xs">+5.8%</Badge>
											</div>
										</div>
										<div class="flex justify-between items-center p-2 border rounded">
											<div>
												<div class="font-medium">WIPRO</div>
												<div class="text-sm text-muted-foreground">IT Services</div>
											</div>
											<div class="text-right">
												<div class="font-medium">₹485.25</div>
												<Badge variant="default" class="text-xs">+4.2%</Badge>
											</div>
										</div>
										<div class="flex justify-between items-center p-2 border rounded">
											<div>
												<div class="font-medium">TECHM</div>
												<div class="text-sm text-muted-foreground">IT Services</div>
											</div>
											<div class="text-right">
												<div class="font-medium">₹1,245.80</div>
												<Badge variant="default" class="text-xs">+3.9%</Badge>
											</div>
										</div>
									</div>
								</Tabs.Content>

								<Tabs.Content value="custom-filters" class="p-4">
									<div class="space-y-4">
										<div class="grid grid-cols-2 gap-4">
											<div>
												<label class="text-sm font-medium mb-1 block">Sector</label>
												<select class="w-full p-2 border rounded-md text-sm">
													<option>All Sectors</option>
													<option>Banking</option>
													<option>IT</option>
													<option>Pharma</option>
													<option>Auto</option>
												</select>
											</div>
											<div>
												<label class="text-sm font-medium mb-1 block">Market Cap</label>
												<select class="w-full p-2 border rounded-md text-sm">
													<option>All</option>
													<option>Large Cap</option>
													<option>Mid Cap</option>
													<option>Small Cap</option>
												</select>
											</div>
											<div>
												<label class="text-sm font-medium mb-1 block">Min Price</label>
												<input
													type="number"
													placeholder="100"
													class="w-full p-2 border rounded-md text-sm"
												/>
											</div>
											<div>
												<label class="text-sm font-medium mb-1 block">Max Price</label>
												<input
													type="number"
													placeholder="5000"
													class="w-full p-2 border rounded-md text-sm"
												/>
											</div>
										</div>
										<Button class="w-full">Apply Filters</Button>
										<div class="text-center text-sm text-muted-foreground py-8">
											Configure filters and click Apply to see results
										</div>
									</div>
								</Tabs.Content>
							</Tabs.Root>
						</CardContent>
					</Card>
				</Resizable.Pane>
			</Resizable.PaneGroup>
		</Resizable.Pane>
	</Resizable.PaneGroup>
</div>

<style>
	:global(body) {
		overflow: hidden;
	}
</style>