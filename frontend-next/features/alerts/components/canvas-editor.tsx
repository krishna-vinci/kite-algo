"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircleIcon,
  CheckIcon,
  InfoIcon,
  Redo2Icon,
  SaveIcon,
  Trash2Icon,
  Undo2Icon,
  ZoomInIcon,
  ZoomOutIcon,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { SectionLabel } from "@/components/operator/section-label";
import { OperatorIssueList } from "@/features/alerts/components/operator-issue-list";
import {
  addAlert,
  addStage,
  alertNodeId,
  alertsOf,
  buildCanvasGraph,
  canDeleteNode,
  deleteNode,
  documentsEqual,
  NODE_DIMENSIONS,
  nodeKindOf,
  nodeLocalId,
  orphanedNodeIds,
  PERSISTED_NAMESPACES,
  renameNode,
  stageIds,
  stageNodeId,
  stagesOf,
  updateAlert,
  updateStage,
  wouldCreateCycle,
  type CanvasGraphNode,
} from "@/features/alerts/lib/canvas";
import {
  useAlertsCanvasLayout,
  useAlertsCanvasMutations,
  useAlertsWorkflow,
} from "@/features/alerts/hooks/use-alerts-queries";
import { patchAlertsWorkflow, fetchAlertsChannels, validateAlertsWorkflow } from "@/features/alerts/api";
import { alertsKeys } from "@/features/alerts/hooks/keys";
import type { AlertsIssue } from "@/features/alerts/types";
import { ApiClientError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

const PADDING = 80;

const KIND_CLASS: Record<CanvasGraphNode["kind"], string> = {
  stage: "border-sky-400/40 bg-sky-400/10",
  alert: "border-amber-400/40 bg-amber-400/10",
  channel: "border-emerald-400/40 bg-emerald-400/10",
  advanced: "border-slate-400/40 bg-slate-400/10",
};

type Positions = Record<string, { x: number; y: number }>;

/**
 * Semantic canvas editor over the canonical document.
 *
 * Two save paths, kept visibly distinct:
 *   - layout-only moves write the layout table via the layout API: no revision,
 *     unchanged canonical hash;
 *   - semantic edits (nodes, connections, properties) go through the normal
 *     revisioned PATCH with `expected_revision`.
 *
 * The document is rebuilt from the LOADED document, never from the graph model,
 * so a field the canvas does not model survives. Constructs outside the subset
 * (feature/breadth stages, sequences, the screener block) appear as read-only
 * nodes instead of being omitted.
 */
export function CanvasEditor({
  workflowId,
  scope,
}: Readonly<{ workflowId: string; scope: string | null }>) {
  const queryClient = useQueryClient();
  const workflowQuery = useAlertsWorkflow(workflowId, scope, { includeYaml: false });
  const layoutQuery = useAlertsCanvasLayout(workflowId, scope);
  const { saveLayout, deleteLayout } = useAlertsCanvasMutations(workflowId, scope);
  const channelsQuery = useQuery({
    queryKey: alertsKeys.channels(scope),
    queryFn: () => fetchAlertsChannels(scope),
    enabled: Boolean(scope),
  });

  const baseDocument = workflowQuery.data?.document ?? null;
  const baseRevision =
    workflowQuery.data?.latest_revision?.revision ??
    workflowQuery.data?.active_revision?.revision ??
    1;

  const [document, setDocument] = useState<Record<string, unknown> | null>(baseDocument);
  const [positions, setPositions] = useState<Positions>({});
  const [layoutDirty, setLayoutDirty] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [past, setPast] = useState<Array<{ document: Record<string, unknown> | null; positions: Positions }>>([]);
  const [future, setFuture] = useState<Array<{ document: Record<string, unknown> | null; positions: Positions }>>([]);
  const [issues, setIssues] = useState<AlertsIssue[] | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });

  // Reset the working copy when the underlying REVISION changes. Keyed on the
  // revision, not the document object, so a background refetch of the same
  // revision cannot discard the operator's in-progress edit.
  const initializedRevision = useRef<number | null>(null);
  useEffect(() => {
    if (initializedRevision.current === baseRevision) return;
    initializedRevision.current = baseRevision;
    /* eslint-disable react-hooks/set-state-in-effect -- syncing the working
       copy to a genuinely new server revision; guarded so it runs once per
       revision rather than on every render. */
    setDocument(baseDocument);
    setPast([]);
    setFuture([]);
    setSelected(null);
    setIssues(null);
    setConflict(false);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [baseDocument, baseRevision]);

  const dragRef = useRef<{
    nodeId: string | null;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);

  const snapshot = useCallback(
    () => ({ document, positions }),
    [document, positions],
  );

  const commit = useCallback(
    (next: { document?: Record<string, unknown>; positions?: Positions }) => {
      setPast((history) => [...history, snapshot()]);
      setFuture([]);
      if (next.document !== undefined) setDocument(next.document);
      if (next.positions !== undefined) setPositions(next.positions);
    },
    [snapshot],
  );

  const semanticDirty = !documentsEqual(document, baseDocument);

  const graph = useMemo(() => {
    const built = buildCanvasGraph(document, layoutQuery.data?.nodes ?? []);
    return {
      ...built,
      nodes: built.nodes.map((node) => ({
        ...node,
        x: positions[node.nodeId]?.x ?? node.x,
        y: positions[node.nodeId]?.y ?? node.y,
      })),
    };
  }, [document, layoutQuery.data?.nodes, positions]);

  const nodeById = useMemo(() => new Map(graph.nodes.map((node) => [node.nodeId, node])), [graph.nodes]);
  const extent = useMemo(() => {
    const maxX = graph.nodes.reduce((acc, node) => Math.max(acc, node.x + NODE_DIMENSIONS.width), 0);
    const maxY = graph.nodes.reduce((acc, node) => Math.max(acc, node.y + NODE_DIMENSIONS.height), 0);
    return { width: maxX + PADDING, height: maxY + PADDING };
  }, [graph.nodes]);

  const issuesByNode = useMemo(() => {
    const map = new Map<string, AlertsIssue[]>();
    if (!issues) return map;
    for (const issue of issues) {
      for (const stage of stagesOf(document)) {
        if (issue.where.includes(`.${String(stage.id)}`)) {
          const key = stageNodeId(String(stage.id));
          map.set(key, [...(map.get(key) ?? []), issue]);
        }
      }
      for (const alert of alertsOf(document)) {
        if (issue.where.includes(`.${String(alert.id)}`)) {
          const key = alertNodeId(String(alert.id));
          map.set(key, [...(map.get(key) ?? []), issue]);
        }
      }
    }
    return map;
  }, [issues, document]);

  const channelOptions = useMemo(() => {
    const names = new Set<string>(channelsQuery.data?.channels.map((channel) => channel.name) ?? []);
    for (const alert of alertsOf(document)) {
      const channels = Array.isArray(alert.channels) ? alert.channels.map(String) : [];
      for (const name of channels) names.add(name);
    }
    return [...names].sort();
  }, [channelsQuery.data, document]);

  const validateMutation = useMutation({
    mutationFn: () => validateAlertsWorkflow({ document: document ?? {} }, scope),
    onSuccess: (response) => setIssues(response.issues),
  });

  const saveDefinition = useMutation({
    mutationFn: () =>
      patchAlertsWorkflow(workflowId, { document: document ?? {}, expected_revision: baseRevision }, scope),
    onMutate: () => {
      setConflict(false);
      setEditError(null);
    },
    onSuccess: async () => {
      setPast([]);
      setFuture([]);
      await queryClient.invalidateQueries({ queryKey: alertsKeys.workflow(workflowId, scope) });
      await queryClient.invalidateQueries({ queryKey: alertsKeys.workflows(scope, false) });
    },
    onError: (error) => {
      if (error instanceof ApiClientError && error.status === 409) setConflict(true);
      else setEditError(error instanceof Error ? error.message : "Could not save.");
    },
  });

  const onPointerDownNode = useCallback(
    (event: React.PointerEvent<HTMLDivElement>, node: CanvasGraphNode) => {
      event.preventDefault();
      event.stopPropagation();
      (event.target as HTMLElement).setPointerCapture(event.pointerId);
      setSelected(node.nodeId);
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

  const onPointerDownCanvas = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    setSelected(null);
    dragRef.current = {
      nodeId: null,
      startX: event.clientX,
      startY: event.clientY,
      originX: pan.x,
      originY: pan.y,
    };
  }, [pan]);

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (drag.nodeId) {
      setPositions((current) => ({
        ...current,
        [drag.nodeId as string]: {
          x: Math.max(0, drag.originX + dx),
          y: Math.max(0, drag.originY + dy),
        },
      }));
      setLayoutDirty(true);
    } else {
      setPan({ x: drag.originX + dx, y: drag.originY + dy });
    }
  }, []);

  const onPointerUp = useCallback(() => {
    const drag = dragRef.current;
    dragRef.current = null;
    if (drag?.nodeId) {
      setPast((history) => [...history, { document, positions }]);
      setFuture([]);
    }
  }, [document, positions]);

  const removeNode = useCallback(
    (nodeId: string) => {
      if (!document) return;
      const check = canDeleteNode(document, nodeId);
      if (!check.ok) {
        setEditError(check.reason);
        return;
      }
      setEditError(null);
      commit({ document: deleteNode(document, nodeId) });
      setSelected(null);
    },
    [document, commit],
  );

  const selectRelative = useCallback(
    (from: string | null, delta: number) => {
      const ids = graph.nodes.map((node) => node.nodeId);
      if (ids.length === 0) return;
      const index = from ? ids.indexOf(from) : -1;
      const next = (index + delta + ids.length) % ids.length;
      setSelected(ids[next]);
    },
    [graph.nodes],
  );

  const onNodeKeyDown = useCallback(
    (event: React.KeyboardEvent, node: CanvasGraphNode) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        setSelected(node.nodeId);
      } else if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault();
        removeNode(node.nodeId);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        selectRelative(node.nodeId, 1);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        selectRelative(node.nodeId, -1);
      }
    },
    [removeNode, selectRelative],
  );

  if (workflowQuery.isLoading || layoutQuery.isLoading) {
    return <Skeleton className="h-96 w-full rounded-xl" />;
  }

  const workflow = workflowQuery.data;
  if (!workflow || !document) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Alert not found</AlertTitle>
        <AlertDescription>This alert does not exist in the selected scope.</AlertDescription>
      </Alert>
    );
  }

  const orphans = orphanedNodeIds(graph, (layoutQuery.data?.nodes ?? []).map((node) => node.node_id));
  const selectedNode = selected ? nodeById.get(selected) ?? null : null;

  const savePositions = () => {
    saveLayout.mutate(
      graph.nodes
        .filter((node) => PERSISTED_NAMESPACES.includes(node.kind))
        .map((node) => ({
          node_id: node.nodeId,
          x: Math.round(node.x),
          y: Math.round(node.y),
          collapsed: node.collapsed,
        })),
      { onSuccess: () => setLayoutDirty(false) },
    );
  };

  return (
    <div className="flex flex-col gap-4 pb-8">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <SectionLabel eyebrow="Canvas" title={workflow.name} description="One definition, edited visually." />
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" disabled={past.length === 0} onClick={() => {
            const previous = past[past.length - 1];
            setPast((h) => h.slice(0, -1));
            setFuture((f) => [...f, snapshot()]);
            setDocument(previous.document);
            setPositions(previous.positions);
          }}>
            <Undo2Icon className="size-4" aria-hidden /> Undo
          </Button>
          <Button size="sm" variant="outline" disabled={future.length === 0} onClick={() => {
            const next = future[future.length - 1];
            setFuture((f) => f.slice(0, -1));
            setPast((h) => [...h, snapshot()]);
            setDocument(next.document);
            setPositions(next.positions);
          }}>
            <Redo2Icon className="size-4" aria-hidden /> Redo
          </Button>
          <Button size="sm" variant="outline" onClick={() => commit({ document: addStage(document, `s${stageIds(document).length + 1}`) })}>
            Add stage
          </Button>
          <Button size="sm" variant="outline" onClick={() => commit({ document: addAlert(document, `a${alertsOf(document).length + 1}`, stageIds(document)[0] ?? "") })}>
            Add alert
          </Button>
          <Button size="sm" variant="outline" disabled={validateMutation.isPending} onClick={() => validateMutation.mutate()}>
            Validate
          </Button>
          <Button size="sm" variant={layoutDirty ? "outline" : "ghost"} disabled={!layoutDirty || saveLayout.isPending} onClick={savePositions}>
            <SaveIcon className="size-4" aria-hidden />
            {saveLayout.isPending ? "Saving…" : layoutDirty ? "Save layout" : "Layout saved"}
          </Button>
          <Button size="sm" variant={semanticDirty ? "default" : "ghost"} disabled={!semanticDirty || saveDefinition.isPending} onClick={() => saveDefinition.mutate()}>
            {saveDefinition.isPending ? "Saving…" : "Save definition"}
          </Button>
        </div>
      </div>

      <Alert>
        <InfoIcon />
        <AlertTitle>Layout and semantics save differently</AlertTitle>
        <AlertDescription className="flex flex-wrap items-center gap-3">
          <span>
            Moving nodes saves to the layout table — no revision, unchanged hash. Changing nodes,
            connections or properties saves a new draft revision under{" "}
            <span className="font-mono">expected_revision {baseRevision}</span>.
          </span>
          {semanticDirty ? (
            <Badge variant="outline" data-testid="canvas-semantic-dirty">unsaved semantic changes</Badge>
          ) : (
            <Badge variant="outline" className="gap-1">
              <CheckIcon className="size-3" aria-hidden /> matches loaded revision
            </Badge>
          )}
        </AlertDescription>
      </Alert>

      {editError ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>That edit is blocked</AlertTitle>
          <AlertDescription>{editError}</AlertDescription>
        </Alert>
      ) : null}

      {conflict ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>This alert changed while you were editing</AlertTitle>
          <AlertDescription>
            Nothing was overwritten. Undo is still available; reload to pick up the newer revision.
          </AlertDescription>
        </Alert>
      ) : null}

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
            <Button size="xs" variant="outline" disabled={deleteLayout.isPending} onClick={() => deleteLayout.mutate(orphans)}>
              <Trash2Icon className="size-3" aria-hidden /> Forget them
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[1fr_20rem]">
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <Button size="icon-sm" variant="outline" aria-label="Zoom out" onClick={() => setZoom((z) => Math.max(0.4, z - 0.1))}>
              <ZoomOutIcon className="size-4" />
            </Button>
            <span className="text-xs text-muted-foreground">{Math.round(zoom * 100)}%</span>
            <Button size="icon-sm" variant="outline" aria-label="Zoom in" onClick={() => setZoom((z) => Math.min(1.6, z + 0.1))}>
              <ZoomInIcon className="size-4" />
            </Button>
          </div>

          <div
            className="relative overflow-hidden rounded-xl border border-border/70 bg-[#0b0d13]"
            style={{ height: 560 }}
            role="application"
            aria-label="Workflow canvas"
          >
            <div
              className="relative origin-top-left"
              style={{
                width: extent.width,
                height: extent.height,
                transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
              }}
              onPointerDown={onPointerDownCanvas}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
            >
              <svg className="pointer-events-none absolute inset-0" width={extent.width} height={extent.height} aria-hidden>
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

              {graph.nodes.map((node) => {
                const nodeIssues = issuesByNode.get(node.nodeId) ?? [];
                const hasError = nodeIssues.some((issue) => issue.severity === "error");
                return (
                  <div
                    key={node.nodeId}
                    role="button"
                    tabIndex={0}
                    aria-pressed={selected === node.nodeId}
                    aria-label={`${node.kind} ${node.label}${node.readOnly ? " (read-only)" : ""}`}
                    onPointerDown={(event) => onPointerDownNode(event, node)}
                    onKeyDown={(event) => onNodeKeyDown(event, node)}
                    className={cn(
                      "absolute cursor-grab select-none rounded-lg border p-3 shadow-lg active:cursor-grabbing focus:outline-none focus:ring-2 focus:ring-primary/60",
                      KIND_CLASS[node.kind],
                      selected === node.nodeId && "ring-2 ring-primary/70",
                      hasError && "border-rose-400/70",
                    )}
                    style={{ left: node.x, top: node.y, width: NODE_DIMENSIONS.width }}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-mono text-sm">{node.label}</span>
                      <Badge variant="outline">{node.readOnly ? "advanced" : node.kind}</Badge>
                    </div>
                    <p className="mt-1 truncate text-xs text-muted-foreground">{node.sublabel}</p>
                    <p className="mt-1 font-mono text-[9px] text-muted-foreground/60">{node.nodeId}</p>
                    {nodeIssues.length > 0 ? (
                      <p className="mt-1 text-[10px] text-rose-300">{nodeIssues.length} issue(s)</p>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        <aside className="flex flex-col gap-3 rounded-xl border border-border/70 bg-card/60 p-4" aria-label="Node inspector">
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">Inspector</p>
          {!selectedNode ? (
            <p className="text-sm text-muted-foreground">
              Select a node. Tab/arrow keys move between nodes; Delete removes the selected node.
            </p>
          ) : (
            <NodeInspector
              key={selectedNode.nodeId}
              node={selectedNode}
              document={document}
              workflowId={workflowId}
              channelOptions={channelOptions}
              onError={setEditError}
              onCommit={(next) => commit({ document: next })}
              onRemove={() => removeNode(selectedNode.nodeId)}
            />
          )}

          {issues ? (
            <div className="mt-2 flex flex-col gap-2">
              <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">Compiler issues</p>
              {issues.length === 0 ? (
                <p className="text-sm text-emerald-300">Valid, no issues.</p>
              ) : (
                <OperatorIssueList issues={issues} bare />
              )}
            </div>
          ) : null}
        </aside>
      </div>
    </div>
  );
}

type NodeInspectorProps = Readonly<{
  node: CanvasGraphNode;
  document: Record<string, unknown>;
  workflowId: string;
  channelOptions: string[];
  onError: (message: string | null) => void;
  onCommit: (next: Record<string, unknown>) => void;
  onRemove: () => void;
}>;

function NodeInspector({ node, document, workflowId, channelOptions, onError, onCommit, onRemove }: NodeInspectorProps) {
  const kind = nodeKindOf(node.nodeId);
  const localId = nodeLocalId(node.nodeId);
  // The inspector is keyed by node id, so this local edit buffers a rename and
  // resets naturally when the selection changes — no sync effect needed.
  const [renameValue, setRenameValue] = useState(localId);

  if (node.readOnly) {
    return (
      <div className="flex flex-col gap-3">
        <p className="font-mono text-sm">{node.label}</p>
        <p className="text-xs text-muted-foreground">{node.advancedNote}</p>
        <Button asChild size="sm" variant="outline">
          <Link href={`/alerts/${workflowId}/edit?advanced=1`}>Open the advanced editor</Link>
        </Button>
      </div>
    );
  }

  if (kind === "stage") {
    const stage = stagesOf(document).find((candidate) => String(candidate.id ?? "") === localId) ?? {};
    const otherStages = stageIds(document).filter((id) => id !== localId);
    const currentInput = stage.input != null ? String(stage.input) : "";
    return (
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor="inspect-stage-id">Stage id</Label>
          <div className="flex gap-2">
            <Input id="inspect-stage-id" value={renameValue} onChange={(event) => setRenameValue(event.target.value)} />
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                if (stageIds(document).includes(renameValue) && renameValue !== localId) {
                  onError(`A stage '${renameValue}' already exists.`);
                  return;
                }
                onError(null);
                onCommit(renameNode(document, node.nodeId, renameValue));
              }}
            >
              Rename
            </Button>
          </div>
        </div>

        <div className="flex flex-col gap-1">
          <Label htmlFor="inspect-clock">Clock</Label>
          <Select
            value={String(stage.clock ?? "candle_close")}
            onValueChange={(clock) => onCommit(updateStage(document, localId, { clock }))}
          >
            <SelectTrigger id="inspect-clock"><SelectValue /></SelectTrigger>
            <SelectContent>
              {["candle_close", "ltp"].map((clock) => (
                <SelectItem key={clock} value={clock}>{clock}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1">
          <Label htmlFor="inspect-timeframe">Timeframe</Label>
          <Input
            id="inspect-timeframe"
            value={String(stage.timeframe ?? "")}
            onChange={(event) => onCommit(updateStage(document, localId, { timeframe: event.target.value }))}
          />
        </div>

        <div className="flex flex-col gap-1">
          <Label htmlFor="inspect-input">Input stage (layer dependency)</Label>
          <Select
            value={currentInput || "none"}
            onValueChange={(value) => {
              const input = value === "none" ? null : value;
              if (input && wouldCreateCycle(document, localId, input)) {
                onError("That input would create a cycle.");
                return;
              }
              onError(null);
              onCommit(updateStage(document, localId, { input }));
            }}
          >
            <SelectTrigger id="inspect-input"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="none">(none)</SelectItem>
              {otherStages.map((id) => (
                <SelectItem key={id} value={id}>{id}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <DeleteButton node={node} document={document} onRemove={onRemove} />
      </div>
    );
  }

  if (kind === "alert") {
    const alert = alertsOf(document).find((candidate) => String(candidate.id ?? "") === localId) ?? {};
    const channels = Array.isArray(alert.channels) ? alert.channels.map(String) : [];
    return (
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor="inspect-alert-id">Alert id</Label>
          <div className="flex gap-2">
            <Input id="inspect-alert-id" value={renameValue} onChange={(event) => setRenameValue(event.target.value)} />
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                if (alertNodeId(renameValue) === node.nodeId) return;
                if (alertsOf(document).some((a) => String(a.id ?? "") === renameValue)) {
                  onError(`An alert '${renameValue}' already exists.`);
                  return;
                }
                onError(null);
                onCommit(renameNode(document, node.nodeId, renameValue));
              }}
            >
              Rename
            </Button>
          </div>
        </div>

        <div className="flex flex-col gap-1">
          <Label htmlFor="inspect-source">Source stage</Label>
          <Select
            value={String(alert.source ?? "") || undefined}
            onValueChange={(source) => onCommit(updateAlert(document, localId, { source }))}
          >
            <SelectTrigger id="inspect-source"><SelectValue placeholder="Select a stage" /></SelectTrigger>
            <SelectContent>
              {stageIds(document).map((id) => (
                <SelectItem key={id} value={id}>{id}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1">
          <Label htmlFor="inspect-trigger">Trigger</Label>
          <Select
            value={String(alert.trigger ?? "on_transition")}
            onValueChange={(trigger) => onCommit(updateAlert(document, localId, { trigger }))}
          >
            <SelectTrigger id="inspect-trigger"><SelectValue /></SelectTrigger>
            <SelectContent>
              {["once", "on_transition", "once_per_session", "reminder"].map((trigger) => (
                <SelectItem key={trigger} value={trigger}>{trigger}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1">
          <Label>Channels</Label>
          {channelOptions.length === 0 ? (
            <p className="text-xs text-muted-foreground">No channels configured.</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {channelOptions.map((name) => (
                <li key={name} className="flex items-center gap-2">
                  <Checkbox
                    id={`inspect-channel-${name}`}
                    checked={channels.includes(name)}
                    onCheckedChange={(checked) =>
                      onCommit(
                        updateAlert(document, localId, {
                          channels:
                            checked === true ? [...channels, name] : channels.filter((item) => item !== name),
                        }),
                      )
                    }
                  />
                  <label htmlFor={`inspect-channel-${name}`} className="text-sm">{name}</label>
                </li>
              ))}
            </ul>
          )}
        </div>

        <DeleteButton node={node} document={document} onRemove={onRemove} />
      </div>
    );
  }

  // channel reference
  return (
    <div className="flex flex-col gap-3">
      <p className="font-mono text-sm">{localId}</p>
      <p className="text-xs text-muted-foreground">
        This is a channel <strong>reference</strong>. Removing it deletes the binding from every
        alert; it does not delete the configured channel.
      </p>
      <Button size="sm" variant="outline" onClick={onRemove}>
        <Trash2Icon className="size-4" aria-hidden /> Remove reference
      </Button>
    </div>
  );
}

function DeleteButton({
  node,
  document,
  onRemove,
}: Readonly<{ node: CanvasGraphNode; document: Record<string, unknown>; onRemove: () => void }>) {
  const check = canDeleteNode(document, node.nodeId);
  return (
    <div className="flex flex-col gap-1">
      <Button size="sm" variant="outline" disabled={!check.ok} onClick={onRemove}>
        <Trash2Icon className="size-4" aria-hidden /> Delete node
      </Button>
      {!check.ok ? <p className="text-xs text-amber-300">{check.reason}</p> : null}
    </div>
  );
}
