export type NavigationItem = {
  label: string;
  short: string;
  href: string;
  description: string;
  tag?: string;
};

export const navigation: NavigationItem[] = [
  { label: "Dashboard", short: "D", href: "/dashboard", description: "operator overview" },
  { label: "Options", short: "O", href: "/options", description: "builder and chain", tag: "hot" },
  { label: "Algos", short: "A", href: "/algos", description: "process manager" },
  { label: "Journal", short: "J", href: "/journal", description: "review and analytics" },
  { label: "Alerts", short: "!", href: "/alerts", description: "triggers and history" },
  { label: "Screeners", short: "S", href: "/screeners", description: "filters and results" },
  { label: "Paper", short: "P", href: "/paper", description: "accounts and blotter" },
  { label: "Charts", short: "C", href: "/charts", description: "market context" },
  { label: "Custom Display", short: "X", href: "/custom-display", description: "workspace layouts" },
  { label: "Settings", short: "⚙", href: "/settings", description: "defaults and sessions" },
];
