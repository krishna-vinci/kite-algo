import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";

const processes = [
  { pid: "2184", name: "straddle-watcher", state: "running", cpu: "8%", mem: "142MB" },
  { pid: "2241", name: "risk-guardian", state: "sleeping", cpu: "1%", mem: "88MB" },
  { pid: "2299", name: "exit-router", state: "running", cpu: "6%", mem: "116MB" },
];

const logs = [
  "09:12:01  boot sequence complete",
  "09:12:07  worker shard 3 attached",
  "09:12:12  trailing stop recalculated",
  "09:12:19  queue depth stable",
  "09:12:24  awaiting next signal",
];

export default function AlgosPage() {
  return (
    <div className="grid gap-4 pb-4 xl:grid-cols-[1.05fr_0.95fr]">
      <Panel eyebrow="tmux" title="Process manager">
        <div className="rounded-2xl border border-border/60 bg-background/60 p-3 font-mono text-sm">
          <div className="flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-[0.35em] text-foreground/40">
            <span className="rounded-full border border-primary/30 bg-primary/10 px-2 py-1 text-primary">session 01</span>
            <span>window 3</span>
            <span>layout: monitor</span>
          </div>
          <div className="mt-4 space-y-2">
            {processes.map((process) => (
              <div key={process.pid} className="grid grid-cols-[72px_1fr_96px_72px_72px] gap-2 rounded-xl border border-border/60 px-3 py-2">
                <span className="text-foreground/40">[{process.pid}]</span>
                <span className="text-primary">{process.name}</span>
                <span>{process.state}</span>
                <span>{process.cpu}</span>
                <span>{process.mem}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="mt-4 flex flex-wrap gap-2 text-xs">
          <StatusBadge tone="positive">attach</StatusBadge>
          <StatusBadge tone="neutral">split pane</StatusBadge>
          <StatusBadge tone="warning">hot reload pending</StatusBadge>
        </div>
      </Panel>

      <Panel eyebrow="logs" title="Live task stream">
        <div className="rounded-2xl border border-border/60 bg-background/60 p-4 font-mono text-sm leading-7">
          {logs.map((line) => (
            <div key={line} className="flex gap-3">
              <span className="text-foreground/40">$</span>
              <span className="text-foreground/80">{line}</span>
            </div>
          ))}
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
            <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">keybinding</p>
            <p className="mt-2 font-mono text-sm text-primary">Ctrl+b split</p>
          </div>
          <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
            <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">worker</p>
            <p className="mt-2 font-mono text-sm text-primary">3 active</p>
          </div>
          <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
            <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">queue</p>
            <p className="mt-2 font-mono text-sm text-primary">2 pending</p>
          </div>
        </div>
      </Panel>
    </div>
  );
}
