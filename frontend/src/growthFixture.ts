import type { GraphSnapshot } from "./graphTypes";

/** Synthetic, public fixture data; tests never contact a model provider. */
export const growthFixture = (): GraphSnapshot => ({
  run: { id: "graph-1", title: "图世界实验", provider: "fixture", mode: "growth_reading", revision: 2, status: "idle",
    limits: { max_calls: 24, max_tokens: 128000, max_parallel: 3, max_windows: 8 },
    usage: { calls: 2, input_tokens: 100, output_tokens: 40, windows: 1 } },
  graph_commit: 2,
  active_scopes: [],
  windows: [
    { id: "a", label: "写信人", revision: 1, entry_node_ids: ["node-a", "node-common"], working_node_id: "node-grown", frontier_node_ids: ["node-grown"], direction: "", method_ref: null, status: "ready" },
    { id: "b", label: "读信人", revision: 1, entry_node_ids: ["node-b", "node-common"], working_node_id: "node-grown", frontier_node_ids: ["node-grown"], direction: "", method_ref: null, status: "ready" },
  ],
  nodes: [
    { id: "node-a", version: "version-a", text_ref: { sha256: "text-a", length: 12 }, created_commit: 0, scope_id: "window:a", source_spans: [] },
    { id: "node-b", version: "version-b", text_ref: { sha256: "text-b", length: 20 }, created_commit: 0, scope_id: "window:b", source_spans: [] },
    { id: "node-common", version: "version-common", text_ref: { sha256: "text-common", length: 25 }, created_commit: 0, scope_id: "world", source_spans: [] },
    { id: "node-grown", version: "version-grown", text_ref: { sha256: "text-grown", length: 30 }, created_commit: 2, scope_id: "window:a", source_spans: [] },
  ],
  links: [
    { id: "growth-a", version: "v1", source_id: "node-a", target_id: "node-grown", kind: "growth", relation: "走向街道", created_commit: 1, scope_id: "window:a" },
    { id: "growth-b", version: "v2", source_id: "node-b", target_id: "node-grown", kind: "growth", relation: "共同来路", created_commit: 2, scope_id: "window:b" },
    { id: "cycle", version: "v3", source_id: "node-grown", target_id: "node-a", kind: "association", relation: "回看旧信", created_commit: 2, scope_id: "window:a" },
    { id: "self", version: "v4", source_id: "node-grown", target_id: "node-grown", kind: "association", relation: "再次进入", created_commit: 2, scope_id: "window:a" },
  ],
  readings: [{ id: "reader-1", label: "第一条阅读", revision: 0, read_seq: 0, observed_graph_commit: 2, entry_node_ids: ["node-a"], cursor_node_id: "node-a", direction: "", method_ref: null, status: "ready", visits: [], passages: [] }],
  sessions: [], methods: [], trials: [], responses: [], receipts: [], events: [], counts: { nodes: 4, links: 4 },
});
