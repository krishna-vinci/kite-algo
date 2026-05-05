export type NavigationItem = {
  label: string;
  short: string;
  href: string;
  description: string;
  tag?: string;
};

export const navigation: NavigationItem[] = [
  { label: "Dashboard", short: "D", href: "/dashboard", description: "operator overview" },
  { label: "Strategies", short: "S", href: "/strategies", description: "live paper and operator controls" },
  { label: "Journal", short: "J", href: "/journal", description: "review and analytics" },
  { label: "Analytics", short: "A", href: "/analytics", description: "performance analytics" },
  { label: "Settings", short: "⚙", href: "/settings", description: "defaults and sessions" },
];
