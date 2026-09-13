"use client";

import Link from "next/link";
import { InfoIcon, SaveIcon, Trash2Icon } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SectionLabel } from "@/components/operator/section-label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  buildCanvasGraph,
  channelNodeId,
  NODE_DIMENSIONS,
  orphanedNodeIds,
  type CanvasGraphNode,
} from "@/features/alerts/lib/canvas";
import {
  useAlertsCanvasLayout,
  useAlertsCanvasMutations,
  useAlertsWorkflow,
} from "@/features/alerts/hooks/use-alerts-queries";
import { cn } from "@/lib/utils";

const PADDING = 80;

const KIND_CLASS: Record<CanvasGraphNode["kind"], string> = {
  stage: "border-sky-400/40 bg-sky-400/10",
  alert: "border-amber-400/40 bg-amber-400/10",
  channel: "border-emerald-400/40 bg-emerald-400/10",
};

export function CanvasEditor({
  workflowId,
  scope,
}: Readonly<{ workflowId: string; scope: string | null }>) {
  const workflowQuery = useAlertsWorkflow(workflowId, scope, { includeYaml: false });
  const layoutQuery = useAlertsCanvasLayout(workflowId, scope);
  const { saveLayout, deleteLayout } = useAlertsCanvasMutations(workflowId, scope);

  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [dirty, setDirty] = useState(false);
  const dragRef = useRef<{ nodeId: string; startX: number; startY: number; originX: number; originY: number } | null>(
    null,
  );
  const containerRef = useRef<HTMLDivElement | null>(null);

  const graph = useMemo(() => {
    const document = workflowQuery.data?.document ?? null;
    const layout = layoutQuery.data?.nodes ?? [];
    const built = buildCanvasGraph(document, layout);
    return {
      ...built,
      nodes: built.nodes.map((node) => ({
        ...node,
        x: positions[node.nodeId]?.x ?? node.x,
        y: positions[node.nodeId]?.y ?? node.y,
      })),
    };
  }, [workflowQuery.data?.document, layoutQuery.data?.nodes, positions]);

  const extent = useMemo(() => {
    const maxX = graph.nodes.reduce((acc, node) => Math.max(acc, node.x + NODE_DIMENSIONS.width), 0);
    const maxY = graph.nodes.reduce((acc, node) => Math.max(acc, node.y + NODE_DIMENSIONS.height), 0);
    return { width: maxX + PADDING, height: maxY + PADDING };
  }, [graph.nodes]);

  const nodeById = useMemo(
    () => new Map(graph.nodes.map((node) => [node.nodeId, node])),
    [graph.nodes],
  );

  const onPointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>, node: CanvasGraphNode) => {
      event.preventDefault();
      (event.target as HTMLElement).setPointerCapture(event.pointerId);
      dragRef.current = {
        nodeId: node.nodeId,
        startX: event.clientX,
        startY: event.clientY,
        originX: node.x,
        originY: node.y,
      };
    },
    [],
  );

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    setPositions((current) => ({
      ...current,
      [drag.nodeId]: { x: Math.max(0, drag.originX + dx), y: Math.max(0, drag.originY + dy) },
    }));
    setDirty(true);
  }, []);

  const onPointerUp = useCallback(() => {
    dragRef.current = null;
  }, []);

  if (workflowQuery.isLoading || layoutQuery.isLoading) {
    return <Skeleton className="h-96 w-full rounded-xl" />;
  }

  const workflow = workflowQuery.data;
  if (!workflow) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Alert not found</AlertTitle>
        <AlertDescription>This alert does not exist in the selected scope.</AlertDescription>
      </Alert>
    );
  }

  const orphans = orphanedNodeIds(graph, (layoutQuery.data?.nodes ?? []).map((node) => node.node_id));

  const savePositions = () => {
    // Cosmetic only: this writes the layout table, never the document, so no
    // revision is created and the canonical hash is unchanged.
    saveLayout.mutate(
      graph.nodes.map((node) => ({
        node_id: node.nodeId,
        x: Math.round(node.x),
        y: Math.round(node.y),
        collapsed: node.collapsed,
      })),
      { onSuccess: () => setDirty(false) },
    );
  };

  return (
    <div className="flex flex-col gap-6 pb-8">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <SectionTitle name={workflow.name} />
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant={dirty ? "default" : "outline"}
            disabled={!dirty || saveLayout.isPending}
            onClick={savePositions}
          >
            <SaveIcon className="size-4" aria-hidden />
            {saveLayout.isPending ? "Saving…" : dirty ? "Save layout" : "Layout saved"}
          </Button>
          <Button asChild size="sm" variant="outline">
            <Link href={`/alerts/${workflowId}/edit`}>Edit conditions</Link>
          </Button>
        </div>
      </div>

      {/* The whole point of the split, stated where the operator acts on it. */}
      <Alert>
        <InfoIcon />
        <AlertTitle>Moving nodes does not change the alert</AlertTitle>
        <AlertDescription>
          Positions are stored separately from the definition, so saving a layout creates{" "}
          <strong>no revision</strong> and leaves the canonical hash untouched. Changing conditions,
          instruments, the session or the trigger is a semantic edit and goes through the editor,
          which does create a revision.
        </AlertDescription>
      </Alert>

      {saveLayout.error ? (
        <Alert variant="destructive">
          <AlertTitle>Could not save the layout</AlertTitle>
          <AlertDescription>
            {saveLayout.error instanceof Error ? saveLayout.error.message : "Unknown error"}
          </AlertDescription>
        </Alert>
      ) : null}

      {orphans.length > 0 ? (
        <Alert>
          <AlertTitle>Some saved positions no longer match a node</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center gap-3">
            <span>{orphans.length} saved position(s) refer to nodes that left the document.</span>
            <Button
              size="xs"
              variant="outline"
              disabled={deleteLayout.isPending}
              onClick={() => deleteLayout.mutate(orphans)}
            >
              <Trash2Icon className="size-3" aria-hidden />
              Forget them
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}

      <div
        ref={containerRef}
        className="relative overflow-auto rounded-xl border border-border/70 bg-[#0b0d13]"
        style={{ height: 560 }}
      >
        <div className="relative" style={{ width: extent.width, height: extent.height }}>
          <svg
            className="pointer-events-none absolute inset-0"
            width={extent.width}
            height={extent.height}
            aria-hidden
          >
            {graph.edges.map((edge) => {
              const from = nodeById.get(edge.from);
              const to = nodeById.get(edge.to);
              if (!from || !to) return null;
              const x1 = from.x + NODE_DIMENSIONS.width;
              const y1 = from.y + NODE_DIMENSIONS.height / 2;
              const x2 = to.x;
              const y2 = to.y + NODE_DIMENSIONS.height / 2;
              const mid = (x1 + x2) / 2;
              return (
                <path
                  key={`${edge.from}->${edge.to}`}
                  d={`M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`}
                  fill="none"
                  stroke="rgba(148,163,184,0.35)"
                  strokeWidth={1.5}
                />
              );
            })}
          </svg>

          {graph.nodes.map((node) => (
            <div
              key={node.nodeId}
              role="group"
              aria-label={`${node.kind} ${node.label}`}
              onPointerDown={(event) => onPointerDown(event, node)}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              className={cn(
                "absolute cursor-grab select-none rounded-lg border p-3 shadow-lg active:cursor-grabbing",
                KIND_CLASS[node.kind],
              )}
              style={{ left: node.x, top: node.y, width: NODE_DIMENSIONS.width }}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-mono text-sm">{node.label}</span>
                <Badge variant="outline">{node.kind}</Badge>
              </div>
              <p className="mt-1 truncate text-xs text-muted-foreground">{node.sublabel}</p>
              <p className="mt-1 font-mono text-[9px] text-muted-foreground/60">{node.nodeId}</p>
            </div>
          ))}
        </div>
      </div>

      {graph.nodes.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          This definition has nothing to lay out.{" "}
          <Link
            href={`/alerts/${workflowId}/edit`}
            className="underline"
          >
            Edit it
          </Link>{" "}
          first.
        </p>
      ) : null}

      <p className="text-xs text-muted-foreground">
        Node ids are namespaced ({channelNodeId("name")} vs stage ids) because stage ids, alert ids
        and channel names are separate id spaces that can collide — a bare id would let two
        different nodes share one saved position.
      </p>
    </div>
  );
}

function SectionTitle({ name }: Readonly<{ name: string }>) {
  return <SectionLabel eyebrow="Canvas" title={name} description="Layout is independent of the definition." />;
}
