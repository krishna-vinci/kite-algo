import { describe, expect, it } from "vitest";

import {
  addAlert,
  addStage,
  alertNodeId,
  buildCanvasGraph,
  canDeleteNode,
  channelNodeId,
  deleteNode,
  dependentsOf,
  documentsEqual,
  nodeKindOf,
  nodeLocalId,
  orphanedNodeIds,
  renameNode,
  stageNodeId,
  updateAlert,
  updateStage,
  wouldCreateCycle,
} from "./canvas";

const DOCUMENT = {
  version: 1,
  name: "demo",
  stages: [
    { id: "px", type: "signal", clock: "candle_close", timeframe: "15minute" },
    { id: "trend", type: "feature", clock: "candle_close", timeframe: "15minute", function: "ema" },
  ],
  alerts: [{ id: "a1", source: "px", trigger: "on_transition", channels: ["ops"] }],
};

describe("buildCanvasGraph", () => {
  it("namespaces every node id by kind", () => {
    const graph = buildCanvasGraph(DOCUMENT);
    const ids = graph.nodes.map((node) => node.nodeId).sort();
    expect(ids).toEqual(
      [stageNodeId("px"), stageNodeId("trend"), alertNodeId("a1"), channelNodeId("ops")].sort(),
    );
  });

  it("derives edges from stage input, alert source and channel bindings", () => {
    const graph = buildCanvasGraph({
      ...DOCUMENT,
      stages: [
        { id: "trend", type: "feature", clock: "candle_close", timeframe: "15minute" },
        { id: "px", type: "signal", clock: "candle_close", timeframe: "15minute", input: "trend" },
      ],
    });
    expect(graph.edges).toContainEqual({ from: stageNodeId("trend"), to: stageNodeId("px") });
    expect(graph.edges).toContainEqual({ from: stageNodeId("px"), to: alertNodeId("a1") });
    expect(graph.edges).toContainEqual({ from: alertNodeId("a1"), to: channelNodeId("ops") });
  });

  it("does NOT collide a stage and a channel that share a name", () => {
    // The case namespacing exists for: `telegram_primary` is a legal stage id
    // AND a legal channel name. Without a namespace both nodes would resolve to
    // one key and inherit each other's saved position.
    const document = {
      stages: [{ id: "telegram_primary", type: "signal", clock: "candle_close", timeframe: "day" }],
      alerts: [{ id: "a1", source: "telegram_primary", trigger: "once", channels: ["telegram_primary"] }],
    };
    const graph = buildCanvasGraph(document);
    const stage = graph.nodes.find((node) => node.kind === "stage");
    const channel = graph.nodes.find((node) => node.kind === "channel");
    expect(stage?.nodeId).toBe("stage:telegram_primary");
    expect(channel?.nodeId).toBe("channel:telegram_primary");
    expect(stage?.nodeId).not.toBe(channel?.nodeId);
  });

  it("keeps each node's own saved position, so colliding names cannot cross-assign", () => {
    const layout = [
      { node_id: "stage:telegram_primary", x: 11, y: 22 },
      { node_id: "channel:telegram_primary", x: 333, y: 444 },
    ];
    const document = {
      stages: [{ id: "telegram_primary", type: "signal", clock: "candle_close", timeframe: "day" }],
      alerts: [{ id: "a1", source: "telegram_primary", trigger: "once", channels: ["telegram_primary"] }],
    };
    const graph = buildCanvasGraph(document, layout);
    const stage = graph.nodes.find((node) => node.kind === "stage");
    const channel = graph.nodes.find((node) => node.kind === "channel");
    expect({ x: stage?.x, y: stage?.y }).toEqual({ x: 11, y: 22 });
    expect({ x: channel?.x, y: channel?.y }).toEqual({ x: 333, y: 444 });
  });

  it("auto-lays out nodes that have no saved position", () => {
    const graph = buildCanvasGraph(DOCUMENT);
    const stages = graph.nodes.filter((node) => node.kind === "stage");
    // Same column, different rows: a column auto-layout, not an overlap.
    expect(stages[0].x).toBe(stages[1].x);
    expect(stages[0].y).not.toBe(stages[1].y);
  });

  it("returns an empty graph for a null document", () => {
    expect(buildCanvasGraph(null)).toEqual({ nodes: [], edges: [] });
  });
});

describe("semantic editing", () => {
  it("parses namespaced ids", () => {
    expect(nodeKindOf("stage:px")).toBe("stage");
    expect(nodeKindOf("channel:ops")).toBe("channel");
    expect(nodeKindOf("bare")).toBeNull();
    expect(nodeLocalId("alert:a1")).toBe("a1");
  });

  it("finds dependents of a stage", () => {
    expect(dependentsOf(DOCUMENT, "stage:px")).toEqual(["alert:a1"]);
  });

  it("blocks deleting a stage that still has dependents", () => {
    const check = canDeleteNode(DOCUMENT, "stage:px");
    expect(check.ok).toBe(false);
    if (!check.ok) expect(check.reason).toMatch(/alert:a1/);
  });

  it("allows deleting an unreferenced stage and an alert", () => {
    expect(canDeleteNode(DOCUMENT, "stage:trend").ok).toBe(true);
    expect(canDeleteNode(DOCUMENT, "alert:a1").ok).toBe(true);
  });

  it("removes an alert and its channel reference when deleting a channel", () => {
    const removedAlert = deleteNode(DOCUMENT, "alert:a1");
    expect((removedAlert.alerts as unknown[]).length).toBe(0);

    const removedChannel = deleteNode(DOCUMENT, "channel:ops");
    const alert = (removedChannel.alerts as Array<Record<string, unknown>>)[0];
    expect(alert.channels).toEqual([]);
    expect((removedChannel.stages as unknown[]).length).toBe(2);
  });

  it("blocks deleting a channel referenced from an unmodeled construct", () => {
    const withAttachment = {
      ...DOCUMENT,
      screener: {
        schedule: { every: "1d" },
        attachments: [{ id: "entry", trigger: "entry", channels: ["ops"] }],
      },
    };
    const check = canDeleteNode(withAttachment, "channel:ops");
    expect(check.ok).toBe(false);
    if (!check.ok) expect(check.reason).toMatch(/attachment/i);
  });

  it("renames a stage and rewrites every reference so connections survive", () => {
    const renamed = renameNode(DOCUMENT, "stage:px", "price");
    const stages = renamed.stages as Array<Record<string, unknown>>;
    const alert = (renamed.alerts as Array<Record<string, unknown>>)[0];
    expect(stages.map((stage) => stage.id)).toContain("price");
    expect(stages.map((stage) => stage.id)).not.toContain("px");
    expect(alert.source).toBe("price");
  });

  it("detects a stage input cycle", () => {
    const doc = {
      stages: [
        { id: "a", type: "signal", clock: "candle_close", timeframe: "day", input: "b" },
        { id: "b", type: "signal", clock: "candle_close", timeframe: "day" },
      ],
      alerts: [],
    };
    expect(wouldCreateCycle(doc, "b", "a")).toBe(true);
    expect(wouldCreateCycle(doc, "b", "c_missing")).toBe(false);
  });

  it("applies modeled stage and alert fields without touching the rest", () => {
    const updated = updateAlert(updateStage(DOCUMENT, "px", { clock: "ltp" }), "a1", {
      trigger: "once",
      channels: ["ops", "email"],
    });
    const stage = (updated.stages as Array<Record<string, unknown>>)[0];
    const alert = (updated.alerts as Array<Record<string, unknown>>)[0];
    expect(stage.clock).toBe("ltp");
    expect(alert.trigger).toBe("once");
    expect(alert.channels).toEqual(["ops", "email"]);
  });

  it("adds a stage and an alert", () => {
    const withStage = addStage(DOCUMENT, "new");
    expect((withStage.stages as unknown[]).length).toBe(3);
    const withAlert = addAlert(DOCUMENT, "a2", "px");
    expect((withAlert.alerts as unknown[]).length).toBe(2);
  });

  it("compares documents structurally", () => {
    expect(documentsEqual(DOCUMENT, JSON.parse(JSON.stringify(DOCUMENT)))).toBe(true);
    expect(documentsEqual(DOCUMENT, deleteNode(DOCUMENT, "alert:a1"))).toBe(false);
  });

  it("shows unmodeled constructs as read-only advanced nodes", () => {
    const graph = buildCanvasGraph({
      ...DOCUMENT,
      stages: [{ id: "f", type: "feature", clock: "candle_close", timeframe: "day", function: "ema" }],
      screener: { schedule: { every: "1d" } },
    });
    const feature = graph.nodes.find((node) => node.nodeId === "stage:f");
    const screener = graph.nodes.find((node) => node.kind === "advanced");
    expect(feature?.readOnly).toBe(true);
    expect(screener?.nodeId).toBe("advanced:screener");
    expect(screener?.readOnly).toBe(true);
  });
});

describe("orphanedNodeIds", () => {
  it("reports saved positions whose node left the document", () => {
    const graph = buildCanvasGraph(DOCUMENT);
    const orphans = orphanedNodeIds(graph, ["stage:px", "stage:deleted"]);
    expect(orphans).toEqual(["stage:deleted"]);
  });

  it("reports nothing when every saved position still matches", () => {
    const graph = buildCanvasGraph(DOCUMENT);
    expect(orphanedNodeIds(graph, graph.nodes.map((node) => node.nodeId))).toEqual([]);
  });
});
