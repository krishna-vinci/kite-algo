/**
 * Canvas graph derivation — pure, so the layout contract is testable without
 * rendering.
 *
 * Node identities are NAMESPACED (`stage:` / `alert:` / `channel:`). That is
 * not cosmetic: stage ids, alert ids and channel names are separate id spaces
 * that can legally collide (a stage called `telegram_primary` and a channel of
 * the same name are both valid), and a bare id would let two different nodes
 * share one saved position — a silent visual corruption.
 *
 * The graph is a VIEW of the canonical document. Nothing here writes to the
 * document, which is why a layout change cannot move a canonical hash.
 */

export type CanvasNodeKind = "stage" | "alert" | "channel";

export type CanvasGraphNode = {
  nodeId: string;
  kind: CanvasNodeKind;
  label: string;
  sublabel: string;
  x: number;
  y: number;
  collapsed: boolean;
};

export type CanvasGraphEdge = { from: string; to: string };

export type CanvasGraph = {
  nodes: CanvasGraphNode[];
  edges: CanvasGraphEdge[];
};

export const NODE_NAMESPACES: CanvasNodeKind[] = ["stage", "alert", "channel"];

export function stageNodeId(stageId: string): string {
  return `stage:${stageId}`;
}

export function alertNodeId(alertId: string): string {
  return `alert:${alertId}`;
}

export function channelNodeId(channelName: string): string {
  return `channel:${channelName}`;
}

/** Column per kind, so an auto-layout reads left to right. */
const COLUMN_X: Record<CanvasNodeKind, number> = {
  stage: 60,
  alert: 420,
  channel: 760,
};

const ROW_HEIGHT = 96;
const NODE_WIDTH = 240;

/** A saved layout row, exactly as `GET .../layout` returns it. */
export type CanvasLayoutEntry = {
  node_id: string;
  x: number;
  y: number;
  collapsed?: boolean;
};

/**
 * Build the graph, preferring saved positions and falling back to a column
 * auto-layout for nodes that have none.
 */
export function buildCanvasGraph(
  document: Record<string, unknown> | null,
  layout: CanvasLayoutEntry[] = [],
): CanvasGraph {
  const savedById = new Map<string, CanvasLayoutEntry>();
  for (const entry of layout) {
    if (entry.node_id) savedById.set(entry.node_id, entry);
  }

  if (!document) return { nodes: [], edges: [] };

  const stages = Array.isArray(document.stages) ? (document.stages as Array<Record<string, unknown>>) : [];
  const alerts = Array.isArray(document.alerts) ? (document.alerts as Array<Record<string, unknown>>) : [];

  const nodes: CanvasGraphNode[] = [];
  const edges: CanvasGraphEdge[] = [];
  const counters: Record<CanvasNodeKind, number> = { stage: 0, alert: 0, channel: 0 };

  const place = (nodeId: string, kind: CanvasNodeKind): { x: number; y: number } => {
    const entry = savedById.get(nodeId);
    if (entry) return { x: entry.x, y: entry.y };
    const index = counters[kind];
    counters[kind] += 1;
    return { x: COLUMN_X[kind], y: 60 + index * ROW_HEIGHT };
  };

  for (const stage of stages) {
    const stageId = String(stage.id ?? "");
    if (!stageId) continue;
    const nodeId = stageNodeId(stageId);
    const position = place(nodeId, "stage");
    nodes.push({
      nodeId,
      kind: "stage",
      label: stageId,
      sublabel: [stage.type, stage.clock, stage.timeframe].filter(Boolean).join(" · "),
      x: position.x,
      y: position.y,
      collapsed: savedById.get(nodeId)?.collapsed ?? false,
    });
    // Layered stage chains: an explicit `input` is a real dependency.
    if (typeof stage.input === "string" && stage.input) {
      edges.push({ from: stageNodeId(stage.input), to: nodeId });
    }
  }

  const channelNames = new Set<string>();
  for (const alert of alerts) {
    const alertId = String(alert.id ?? "");
    if (!alertId) continue;
    const nodeId = alertNodeId(alertId);
    const position = place(nodeId, "alert");
    nodes.push({
      nodeId,
      kind: "alert",
      label: alertId,
      sublabel: `trigger: ${String(alert.trigger ?? "—")}`,
      x: position.x,
      y: position.y,
      collapsed: savedById.get(nodeId)?.collapsed ?? false,
    });
    if (typeof alert.source === "string" && alert.source) {
      edges.push({ from: stageNodeId(alert.source), to: nodeId });
    }
    if (Array.isArray(alert.channels)) {
      for (const channel of alert.channels) channelNames.add(String(channel));
    }
  }

  for (const channel of [...channelNames].sort()) {
    const nodeId = channelNodeId(channel);
    const position = place(nodeId, "channel");
    nodes.push({
      nodeId,
      kind: "channel",
      label: channel,
      sublabel: "channel",
      x: position.x,
      y: position.y,
      collapsed: savedById.get(nodeId)?.collapsed ?? false,
    });
    for (const alert of alerts) {
      const channels = Array.isArray(alert.channels) ? alert.channels.map(String) : [];
      if (channels.includes(channel)) {
        edges.push({ from: alertNodeId(String(alert.id ?? "")), to: nodeId });
      }
    }
  }

  return { nodes, edges };
}

export const NODE_DIMENSIONS = { width: NODE_WIDTH, height: 64 };

/**
 * Which nodes left the document since the layout was saved.
 *
 * Called out explicitly because a stale layout row is harmless while a stale
 * NODE is not: this is what the UI offers to forget.
 */
export function orphanedNodeIds(graph: CanvasGraph, layoutNodeIds: string[]): string[] {
  const live = new Set(graph.nodes.map((node) => node.nodeId));
  return layoutNodeIds.filter((nodeId) => !live.has(nodeId));
}
