type WorkspaceTabsProps = Readonly<{
  activeTab: "chain" | "builder" | "impact";
  onTabChange: (tab: "chain" | "builder" | "impact") => void;
}>;

const tabs = [
  { key: "chain", label: "Option Chain" },
  { key: "builder", label: "Strategy Builder" },
  { key: "impact", label: "Nifty 50 Impact" },
] as const;

export function WorkspaceTabs({ activeTab, onTabChange }: WorkspaceTabsProps) {
  return (
    <nav aria-label="options workspace tabs" className="flex items-center gap-2 border-b border-[var(--border)] bg-[var(--panel)] px-3 py-2">
      {tabs.map((tab) => {
        const active = activeTab === tab.key;
        return (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onTabChange(tab.key)}
            className={`rounded-md border px-3 py-1.5 text-[11px] uppercase tracking-[0.16em] transition ${
              active
                ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]"
                : "border-transparent text-[var(--muted)] hover:border-[var(--border)] hover:text-[var(--text)]"
            }`}
          >
            {tab.label}
          </button>
        );
      })}
      {/* spacer — keeps tab row compact */}
    </nav>
  );
}
