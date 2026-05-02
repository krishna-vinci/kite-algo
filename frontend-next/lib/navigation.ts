export type NavigationItem = {
  label: string;
  short: string;
  href: string;
  description: string;
  tag?: string;
};

export const navigation: NavigationItem[] = [
  { label: "Dashboard", short: "D", href: "/dashboard", description: "operator overview" },
  { label: "Trading", short: "T", href: "/trading", description: "orders positions and risk" },
  { label: "Options", short: "O", href: "/options", description: "builder and chain", tag: "hot" },
  { label: "Journal", short: "J", href: "/journal", description: "review and analytics" },
  { label: "Paper", short: "P", href: "/paper", description: "accounts and blotter" },
  { label: "Settings", short: "⚙", href: "/settings", description: "defaults and sessions" },
];
