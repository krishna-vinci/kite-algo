import { AlgoWorkerAccessPanel } from "@/components/settings/algo-worker-access-panel";
import { IndexBaselinesPanel } from "@/components/settings/index-baselines-panel";
import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
import { StatusBadge } from "@/components/operator/status-badge";

const sidebarItems = [
  ["Index baselines", "live"],
  ["Algo worker access", "paper"],
  ["Configuration APIs", "planned"],
];

export default function SettingsPage() {
  return (
    <div className="grid gap-4 xl:grid-cols-[280px_minmax(0,1fr)] pb-4">
      <aside className="rounded-[1.5rem] border border-border/70 bg-card/70 p-4 backdrop-blur">
        <SectionLabel eyebrow="settings" title="Workspace sections" description="Local navigation stays page-specific." />
        <nav aria-label="Settings sections" className="mt-4 space-y-2">
          {sidebarItems.map(([label, tag], index) => (
            <a
              key={label}
              href={`#${label!.toLowerCase().replace(/\s+/g, "-")}`}
              aria-current={index === 0 ? "page" : undefined}
              className={[
                "flex items-center justify-between rounded-2xl border px-3 py-3 text-sm transition-colors",
                index === 0
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "border-border/70 bg-background/60 text-foreground/75 hover:border-primary/25 hover:text-foreground",
              ].join(" ")}
            >
              <span className="font-medium tracking-tight">{label}</span>
              <StatusBadge tone={index === 0 ? "positive" : "neutral"}>{tag}</StatusBadge>
            </a>
          ))}
        </nav>
      </aside>

      <div className="space-y-4">
        {/* Index baselines — live operator section */}
        <IndexBaselinesPanel />

        <AlgoWorkerAccessPanel />

        <Panel id="configuration-apis" eyebrow="configuration" title="Settings APIs" action={<StatusBadge tone="neutral">planned</StatusBadge>}>
          <div className="rounded-[1.2rem] border border-dashed border-border/70 bg-background/40 p-5">
            <p className="max-w-3xl text-sm leading-7 text-foreground/65">
              Trading defaults, protection policies, schedules, and notification routing will move here once the backend settings contracts are exposed. Static placeholder values have been removed to keep this workspace trustworthy.
            </p>
          </div>
        </Panel>
      </div>
    </div>
  );
}
