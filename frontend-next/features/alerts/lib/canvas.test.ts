import { describe, expect, it } from "vitest";

import {
  alertNodeId,
  buildCanvasGraph,
  channelNodeId,
  orphanedNodeIds,
  stageNodeId,
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
