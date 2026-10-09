import type { Limits, Provider, SafeError, Usage } from "./types";

export interface GraphTextRef { sha256: string; length: number }
export interface GraphObjectRef { kind: string; object_id: string; version: string }
export interface GraphSourceSpan {
  node_id: string;
  node_version: string;
  text_ref: GraphTextRef;
  start: number;
  end: number;
  passage_start?: number | null;
  passage_end?: number | null;
}
export interface GraphNode {
  id: string;
  version: string;
  text_ref: GraphTextRef;
  created_commit: number;
  scope_id: string;
  source_spans: GraphSourceSpan[];
  private_scope?: string | null;
}
export interface GraphLink {
  id: string;
  version: string;
  source_id: string;
  target_id: string;
  kind: "growth" | "association";
  relation: string;
  created_commit: number;
  scope_id: string;
  source_spans?: GraphSourceSpan[];
}
export interface GenerationWindow {
  id: string;
  label: string;
  revision: number;
  entry_node_ids: string[];
  working_node_id: string | null;
  frontier_node_ids: string[];
  direction: string;
  method_ref: GraphObjectRef | null;
  status: string;
}
export interface GraphVisit {
  id: string;
  track_id: string;
  read_seq: number;
  node_id: string;
  node_version: string;
  observed_graph_commit: number;
  via_link_id: string | null;
  from_node_id: string | null;
  transition: string;
  source_spans: GraphSourceSpan[];
}
export interface GraphPassage {
  id: string;
  track_id: string;
  read_seq: number;
  text_ref: GraphTextRef;
  text?: string;
  visit_ids: string[];
  source_spans: GraphSourceSpan[];
}
export interface GraphReading {
  id: string;
  label: string;
  revision?: number;
  read_seq: number;
  observed_graph_commit: number;
  entry_node_ids: string[];
  cursor_node_id: string | null;
  bookmark_node_ids?: string[];
  direction: string;
  method_ref?: GraphObjectRef | null;
  status: string;
  visits: GraphVisit[];
  passages: GraphPassage[];
}
export interface GraphSession {
  id: string;
  scope_id: string;
  work_seq: number;
  last_action: string;
  status: string;
  error?: SafeError | null;
}
export interface GraphRecord {
  id: string;
  title?: string;
  label?: string;
  kind?: string;
  version?: string;
  scope_id?: string;
  text?: string;
  status?: string;
  purpose?: string;
  created_commit?: number;
  graph_commit?: number;
  definition?: unknown;
  program?: unknown;
  method_ref?: GraphObjectRef | null;
  evaluation_method_ref?: GraphObjectRef | null;
  source_spans?: GraphSourceSpan[];
  [field: string]: unknown;
}
export interface GraphRunSummary {
  id: string;
  title: string;
  provider: Provider;
  mode: "growth_reading";
  revision: number;
  status: string;
  limits: Limits;
  usage: Usage;
  last_error?: SafeError | null;
}
export interface GraphRunArchive {
  id: string;
  title: string;
  revision: number;
  status: string;
  preset_id?: string;
}
export interface GraphPreset {
  id: string;
  name: string;
  version: string;
  windows: { id: string; label: string; seed: string; direction?: string }[];
  shared_seed: string;
}
export interface GraphPresetInput {
  id: string;
  name: string;
  expected_version?: string;
  windows: GraphPreset["windows"];
  shared_seed: string;
}
export const GROWTH_DEFAULT_LIMITS: Limits = { max_calls: null, max_tokens: null, max_parallel: 3, max_windows: null };
export interface GraphSnapshot {
  run: GraphRunSummary;
  graph_commit: number;
  windows: GenerationWindow[];
  nodes: GraphNode[];
  links: GraphLink[];
  readings: GraphReading[];
  sessions: GraphSession[];
  active_scopes: string[];
  usage?: Usage;
  methods: GraphRecord[];
  trials: GraphRecord[];
  responses: GraphRecord[];
  receipts: GraphRecord[];
  events: GraphRecord[];
  counts?: Record<string, number>;
}
export interface GraphProjection {
  nodes: GraphNode[];
  links: GraphLink[];
  graph_commit: number;
  next_offset?: number | null;
}
export interface GraphCreateInput {
  title: string;
  provider: Provider;
  preset_id?: string;
  windows?: { id: string; label: string; seed: string; direction?: string }[];
  shared_seed?: string;
  limits: Limits;
}
export interface GraphPage<T> { items: T[]; next_offset?: number | null }
export type GraphRecordPage = GraphPage<GraphRecord>;
