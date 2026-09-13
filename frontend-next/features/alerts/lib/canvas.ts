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

/** Persistable namespaces. `advanced` is rendered read-only and never saved. */
export type CanvasNodeKind = "stage" | "alert" | "channel" | "advanced";

export type CanvasGraphNode = {
  nodeId: string;
  kind: CanvasNodeKind;
  label: string;
  sublabel: string;
  x: number;
  y: number;
  collapsed: boolean;
  /**
   * True when the node's definition uses a construct the property editor does
   * not model (a feature/breadth stage, a sequence, or the screener block).
   * Such nodes are shown, never hidden — but they cannot be edited here.
   */
  readOnly?: boolean;
  advancedNote?: string;
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
  advanced: 60,
};

/** Namespaces the layout store accepts; `advanced` nodes are never persisted. */
export const PERSISTED_NAMESPACES: CanvasNodeKind[] = ["stage", "alert", "channel"];

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
  const counters: Record<CanvasNodeKind, number> = { stage: 0, alert: 0, channel: 0, advanced: 0 };

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
    // A feature/breadth stage or one carrying a sequence/breadth block is shown
    // but cannot be edited by the property panel — hiding it would present a
    // partial graph as the whole workflow.
    const advanced =
      stage.type !== "signal" || stage.sequence != null || stage.breadth != null || stage.function != null;
    nodes.push({
      nodeId,
      kind: "stage",
      label: stageId,
      sublabel: [stage.type, stage.clock, stage.timeframe].filter(Boolean).join(" · "),
      x: position.x,
      y: position.y,
      collapsed: savedById.get(nodeId)?.collapsed ?? false,
      ...(advanced
        ? { readOnly: true, advancedNote: `stage type '${String(stage.type)}' is not editable here` }
        : {}),
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

  // The screener block is outside the visual subset; show it read-only rather
  // than omitting it and presenting the resulting graph as the whole workflow.
  const screener = document.screener;
  if (screener && typeof screener === "object") {
    const spec = screener as Record<string, unknown>;
    const nodeId = "advanced:screener";
    const position = place(nodeId, "advanced");
    const schedule = spec.schedule as Record<string, unknown> | undefined;
    nodes.push({
      nodeId,
      kind: "advanced",
      label: "screener",
      sublabel: schedule ? `every ${String(schedule.every ?? "?")}` : "screener block",
      x: position.x,
      y: position.y,
      collapsed: false,
      readOnly: true,
      advancedNote: "The screener block is not editable on the canvas. Use the screener editor.",
    });
  }

  return { nodes, edges };
}

export const NODE_DIMENSIONS = { width: NODE_WIDTH, height: 64 };

// ---------------------------------------------------------------------------
// semantic editing of the loaded document (never a second model)
// ---------------------------------------------------------------------------
//
// Every function below takes the LOADED document and returns a NEW document
// with only the modeled fields changed, so anything the canvas does not model
// survives a canvas edit — the same guarantee the form editor makes.

type Doc = Record<string, unknown>;

export function nodeKindOf(nodeId: string): CanvasNodeKind | null {
  const separator = nodeId.indexOf(":");
  if (separator === -1) return null;
  const namespace = nodeId.slice(0, separator);
  return namespace === "stage" || namespace === "alert" || namespace === "channel"
    ? namespace
    : null;
}

export function nodeLocalId(nodeId: string): string {
  const separator = nodeId.indexOf(":");
  return separator === -1 ? nodeId : nodeId.slice(separator + 1);
}

export function stagesOf(document: Doc | null): Array<Record<string, unknown>> {
  return Array.isArray(document?.stages)
    ? (document?.stages as Array<Record<string, unknown>>)
    : [];
}

export function alertsOf(document: Doc | null): Array<Record<string, unknown>> {
  return Array.isArray(document?.alerts)
    ? (document?.alerts as Array<Record<string, unknown>>)
    : [];
}

export function channelsOf(alert: Record<string, unknown>): string[] {
  return Array.isArray(alert.channels) ? alert.channels.map(String) : [];
}

export function stageIds(document: Doc | null): string[] {
  return stagesOf(document).map((stage) => String(stage.id ?? "")).filter(Boolean);
}

export function alertIds(document: Doc | null): string[] {
  return alertsOf(document).map((alert) => String(alert.id ?? "")).filter(Boolean);
}

export function channelNames(document: Doc | null): string[] {
  const names = new Set<string>();
  for (const alert of alertsOf(document)) for (const name of channelsOf(alert)) names.add(name);
  return [...names].sort();
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** Where a channel name is referenced, and whether the canvas may edit it. */
export type ChannelReference = { editable: boolean; where: string };

export function channelReferenceLocations(document: Doc | null, name: string): ChannelReference[] {
  const locations: ChannelReference[] = [];
  for (const alert of alertsOf(document)) {
    if (channelsOf(alert).includes(name)) {
      locations.push({ editable: true, where: `alert ${String(alert.id ?? "")}` });
    }
  }
  const screener = document?.screener as Record<string, unknown> | undefined;
  const attachments = Array.isArray(screener?.attachments)
    ? (screener?.attachments as Array<Record<string, unknown>>)
    : [];
  for (const attachment of attachments) {
    const channels = Array.isArray(attachment.channels) ? attachment.channels.map(String) : [];
    if (channels.includes(name)) {
      locations.push({ editable: false, where: `screener attachment ${String(attachment.id ?? "")}` });
    }
  }
  return locations;
}

/** Nodes that depend on `nodeId` (stage input, alert source, channel binding). */
export function dependentsOf(document: Doc | null, nodeId: string): string[] {
  const kind = nodeKindOf(nodeId);
  const id = nodeLocalId(nodeId);
  const dependents: string[] = [];
  if (kind === "stage") {
    for (const alert of alertsOf(document)) {
      if (String(alert.source ?? "") === id) dependents.push(alertNodeId(String(alert.id ?? "")));
    }
    for (const stage of stagesOf(document)) {
      if (String(stage.input ?? "") === id) dependents.push(stageNodeId(String(stage.id ?? "")));
    }
  }
  return dependents;
}

export type EditCheck = { ok: true } | { ok: false; reason: string };

/**
 * Whether a node may be removed without silently damaging a dependency.
 *
 * A stage with dependents is blocked (the operator must re-point or remove the
 * dependents first); a channel referenced from a construct the canvas does not
 * model (a screener attachment) is blocked rather than half-removed.
 */
export function canDeleteNode(document: Doc | null, nodeId: string): EditCheck {
  const kind = nodeKindOf(nodeId);
  if (!kind) return { ok: false, reason: "Unrecognised node." };
  if (kind === "stage") {
    const dependents = dependentsOf(document, nodeId);
    if (dependents.length > 0) {
      return {
        ok: false,
        reason: `This stage is referenced by ${dependents.join(", ")}. Re-point or remove those first.`,
      };
    }
  }
  if (kind === "channel") {
    const hidden = channelReferenceLocations(document, nodeLocalId(nodeId)).filter(
      (reference) => !reference.editable,
    );
    if (hidden.length > 0) {
      return {
        ok: false,
        reason: `This channel is referenced by ${hidden
          .map((item) => item.where)
          .join(", ")}, which the canvas does not model. Use the advanced editor.`,
      };
    }
  }
  return { ok: true };
}

/**
 * Remove a node from the document.
 *
 * A channel node is a REFERENCE here: removing it deletes the binding from
 * every alert, never the configured channel itself (which lives in the API's
 * channel store, not the document). Preconditions are checked with
 * `canDeleteNode` by the caller.
 */
export function deleteNode(document: Doc, nodeId: string): Doc {
  const kind = nodeKindOf(nodeId);
  const id = nodeLocalId(nodeId);
  const next = clone(document);
  if (kind === "stage") {
    next.stages = stagesOf(next).filter((stage) => String(stage.id ?? "") !== id);
  } else if (kind === "alert") {
    next.alerts = alertsOf(next).filter((alert) => String(alert.id ?? "") !== id);
  } else if (kind === "channel") {
    next.alerts = alertsOf(next).map((alert) => {
      const channels = channelsOf(alert).filter((name) => name !== id);
      return { ...alert, channels };
    });
  }
  return next;
}

export function uniqueNodeId(document: Doc | null, base: string, existing: string[]): string {
  const taken = new Set(existing);
  if (!taken.has(base)) return base;
  let index = 2;
  while (taken.has(`${base}${index}`)) index += 1;
  return `${base}${index}`;
}

export function addStage(document: Doc, id: string): Doc {
  const next = clone(document);
  const stages = stagesOf(next);
  stages.push({
    id,
    type: "signal",
    clock: "candle_close",
    timeframe: "15minute",
    conditions: [{ left: { field: "close" }, op: "crosses_above", right: 0 }],
  });
  next.stages = stages;
  return next;
}

export function addAlert(document: Doc, id: string, source: string): Doc {
  const next = clone(document);
  const alerts = alertsOf(next);
  alerts.push({ id, source, trigger: "on_transition", channels: [] });
  next.alerts = alerts;
  return next;
}

/**
 * Rename a stage or alert, updating every reference so connections survive.
 * (Namespaces are stable; a channel reference is renamed by removing and
 * re-adding, since its name IS the configured channel.)
 */
export function renameNode(document: Doc, nodeId: string, newId: string): Doc {
  const kind = nodeKindOf(nodeId);
  const oldId = nodeLocalId(nodeId);
  if (!kind || newId === "" || newId === oldId) return document;
  const next = clone(document);
  if (kind === "stage") {
    next.stages = stagesOf(next).map((stage) =>
      String(stage.id ?? "") === oldId ? { ...stage, id: newId } : stage,
    );
    next.alerts = alertsOf(next).map((alert) =>
      String(alert.source ?? "") === oldId ? { ...alert, source: newId } : alert,
    );
    next.stages = stagesOf(next).map((stage) =>
      String(stage.input ?? "") === oldId ? { ...stage, input: newId } : stage,
    );
  } else if (kind === "alert") {
    next.alerts = alertsOf(next).map((alert) =>
      String(alert.id ?? "") === oldId ? { ...alert, id: newId } : alert,
    );
  }
  return next;
}

/** True when making `inputId` the input of `stageId` would create a cycle. */
export function wouldCreateCycle(document: Doc | null, stageId: string, inputId: string): boolean {
  if (stageId === inputId) return true;
  const byId = new Map(stagesOf(document).map((stage) => [String(stage.id ?? ""), stage]));
  let cursor: string | undefined = inputId;
  const seen = new Set<string>();
  while (cursor) {
    if (cursor === stageId) return true;
    if (seen.has(cursor)) return true;
    seen.add(cursor);
    cursor = byId.get(cursor)?.input != null ? String(byId.get(cursor)?.input) : undefined;
  }
  return false;
}

export type StagePatch = { clock?: string; timeframe?: string; input?: string | null };
export type AlertPatch = { trigger?: string; source?: string; channels?: string[] };

/** Apply modeled stage fields onto the loaded document. */
export function updateStage(document: Doc, stageId: string, patch: StagePatch): Doc {
  const next = clone(document);
  next.stages = stagesOf(next).map((stage) => {
    if (String(stage.id ?? "") !== stageId) return stage;
    const updated = { ...stage };
    if (patch.clock !== undefined) updated.clock = patch.clock;
    if (patch.timeframe !== undefined) updated.timeframe = patch.timeframe;
    if (patch.input !== undefined) {
      if (patch.input === null || patch.input === "") delete updated.input;
      else updated.input = patch.input;
    }
    return updated;
  });
  return next;
}

/** Apply modeled alert fields onto the loaded document. */
export function updateAlert(document: Doc, alertId: string, patch: AlertPatch): Doc {
  const next = clone(document);
  next.alerts = alertsOf(next).map((alert) => {
    if (String(alert.id ?? "") !== alertId) return alert;
    const updated = { ...alert };
    if (patch.trigger !== undefined) updated.trigger = patch.trigger;
    if (patch.source !== undefined) updated.source = patch.source;
    if (patch.channels !== undefined) updated.channels = patch.channels;
    return updated;
  });
  return next;
}

/** Structural equality, used to state "unsaved changes" relative to a base. */
export function documentsEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

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
