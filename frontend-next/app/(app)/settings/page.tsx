"use client";

import { useRef, useState } from "react";
import { AlgoWorkerAccessPanel } from "@/components/settings/algo-worker-access-panel";
import { IndexBaselinesPanel } from "@/components/settings/index-baselines-panel";
import { WorkspaceContextPanel } from "@/components/settings/workspace-context-panel";
import { Panel } from "@/components/operator/panel";

// ---------------------------------------------------------------------------
// Tab definitions
// ---------------------------------------------------------------------------

type TabId = "reference-data" | "worker-access" | "workspace" | "apis";

const TABS: { id: TabId; label: string; description: string }[] = [
  {
    id: "reference-data",
    label: "Reference data",
    description: "Index baselines, constituent freshness, and review workflow.",
  },
  {
    id: "worker-access",
    label: "Worker access",
    description: "Token issuance, scope controls, and run permissions.",
  },
  {
    id: "workspace",
    label: "Workspace",
    description: "Read-only view of active environments and mode context.",
  },
  {
    id: "apis",
    label: "APIs",
    description: "Future home for trading defaults and protection contracts.",
  },
];

// ---------------------------------------------------------------------------
// Page component
// ---------------------------------------------------------------------------

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<TabId>("reference-data");
  const tabRefs = useRef<Record<TabId, HTMLButtonElement | null>>({
    "reference-data": null,
    "worker-access": null,
    workspace: null,
    apis: null,
  });

  function moveTabFocus(currentId: TabId, direction: "next" | "prev" | "first" | "last") {
    const currentIndex = TABS.findIndex((tab) => tab.id === currentId);
    if (currentIndex === -1) return;

    let targetIndex = currentIndex;
    if (direction === "next") {
      targetIndex = (currentIndex + 1) % TABS.length;
    } else if (direction === "prev") {
      targetIndex = (currentIndex - 1 + TABS.length) % TABS.length;
    } else if (direction === "first") {
      targetIndex = 0;
    } else if (direction === "last") {
      targetIndex = TABS.length - 1;
    }

    const targetId = TABS[targetIndex]?.id;
    if (!targetId) return;
    setActiveTab(targetId);
    tabRefs.current[targetId]?.focus();
  }

  return (
    <div className="space-y-6 pb-6">
      {/* Page header */}
      <div className="space-y-1">
        <p className="text-[11px] uppercase tracking-[0.28em] text-foreground/40">settings</p>
        <h1 className="text-xl font-semibold tracking-tight text-foreground">Workspace configuration</h1>
        <p className="max-w-2xl text-sm leading-6 text-foreground/50">
          Manage reference data, operator access, environment context, and system defaults.
        </p>
      </div>

      {/* Tab bar */}
      <nav
        aria-label="Settings sections"
        className="flex gap-1 rounded-2xl border border-border/60 bg-card/50 p-1 backdrop-blur"
        role="tablist"
      >
        {TABS.map((tab) => {
          const isActive = activeTab === tab.id;
          return (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={isActive}
                aria-controls={`tabpanel-${tab.id}`}
                id={`tab-${tab.id}`}
                tabIndex={isActive ? 0 : -1}
                ref={(node) => {
                  tabRefs.current[tab.id] = node;
                }}
                onClick={() => setActiveTab(tab.id)}
                onKeyDown={(event) => {
                  if (event.key === "ArrowRight" || event.key === "ArrowDown") {
                    event.preventDefault();
                    moveTabFocus(tab.id, "next");
                  } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
                    event.preventDefault();
                    moveTabFocus(tab.id, "prev");
                  } else if (event.key === "Home") {
                    event.preventDefault();
                    moveTabFocus(tab.id, "first");
                  } else if (event.key === "End") {
                    event.preventDefault();
                    moveTabFocus(tab.id, "last");
                  }
                }}
                className={[
                  "flex-1 rounded-xl px-3 py-2 text-xs font-medium tracking-tight transition-colors",
                isActive
                  ? "bg-background text-foreground shadow-sm"
                  : "text-foreground/50 hover:text-foreground/80",
              ].join(" ")}
            >
              {tab.label}
            </button>
          );
        })}
      </nav>

      {/* Tab panels */}
      <div>
        {activeTab === "reference-data" && (
          <div
            id="tabpanel-reference-data"
            role="tabpanel"
            aria-labelledby="tab-reference-data"
          >
            <IndexBaselinesPanel />
          </div>
        )}

        {activeTab === "worker-access" && (
          <div
            id="tabpanel-worker-access"
            role="tabpanel"
            aria-labelledby="tab-worker-access"
          >
            <AlgoWorkerAccessPanel />
          </div>
        )}

        {activeTab === "workspace" && (
          <div
            id="tabpanel-workspace"
            role="tabpanel"
            aria-labelledby="tab-workspace"
          >
            <WorkspaceContextPanel />
          </div>
        )}

        {activeTab === "apis" && (
          <div
            id="tabpanel-apis"
            role="tabpanel"
            aria-labelledby="tab-apis"
          >
            <Panel
              eyebrow="configuration"
              title="Settings APIs"
              aria-label="Settings APIs"
            >
              <div className="rounded-[1.2rem] border border-dashed border-border/70 bg-background/40 p-5">
                <p className="max-w-3xl text-sm leading-7 text-foreground/65">
                  Trading defaults, protection policies, schedules, and notification routing will move here once the backend settings contracts are exposed. Static placeholder values have been removed to keep this workspace trustworthy.
                </p>
              </div>
            </Panel>
          </div>
        )}
      </div>
    </div>
  );
}
