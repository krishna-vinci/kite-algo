export type NavigationItem = {
  label: string;
  short: string;
  href: string;
  description: string;
  tag?: string;
};

export const navigation: NavigationItem[] = [
  { label: "Dashboard", short: "D", href: "/dashboard", description: "operator overview" },
  { label: "Alerts", short: "A", href: "/alerts", description: "rules, screeners and evaluation health" },
  { label: "Strategies", short: "S", href: "/strategies", description: "live paper and operator controls" },
  { label: "Options", short: "O", href: "/options", description: "chain, analytics and payoff builder" },
  { label: "Journal", short: "J", href: "/journal", description: "review and analytics" },
  { label: "Settings", short: "⚙", href: "/settings", description: "defaults and sessions" },
];
