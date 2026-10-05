export type Provider = "fixture" | "deepseek";
export interface SafeError {
  code: string;
  message: string;
  retryable: boolean;
}
export interface Usage {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  reserved_tokens?: number;
  unknown_tokens?: number;
  failed_calls?: number;
  windows?: number;
}
export interface Limits {
  max_calls: number;
  max_tokens: number;
  max_parallel: number;
  max_windows: number;
}
export interface RunSummary {
  id: string;
  title: string;
  provider: Provider;
  status: "idle" | "running" | "paused" | "error" | "budget_exhausted";
  revision: number;
  created_at: string;
  updated_at: string;
  usage: Usage;
  last_error: SafeError | null;
}
export interface Run extends RunSummary {
  limits: Limits;
  phase: "idle" | "drafting" | "realizing" | "presenting";
  fork_of: string | null;
}
export interface Position {
  id: string;
  name: string;
  seed: string;
}
export interface Expression {
  id: string;
  text: string;
  created_revision: number;
  source_position_id: string | null;
}
export interface Binding {
  source_position_id: string;
  source_expression_id: string;
  value: string;
  anchor: string;
  revision: number;
}
export interface Use {
  id: string;
  position_id: string;
  expression_id: string;
  role: string;
  bindings: Binding[];
  revision: number;
}
export interface Continuation {
  position_id: string;
  frame_revision: number;
  status: string;
  seed_text: string;
  condition_fingerprint: string;
  dependencies: string[];
  surface: string;
  resume_count: number;
}
export interface Segment {
  id: string;
  revision: number;
  text: string;
  focus_position_id: string;
  expression_ids: string[];
  reinterprets: string[];
}
export interface Event {
  id: string;
  type: string;
  revision: number;
  at: string;
  data: Record<string, unknown>;
}
export interface Snapshot {
  schema_version: number;
  run: Run;
  positions: Position[];
  expressions: Expression[];
  uses: Use[];
  continuations: Continuation[];
  events: Event[];
  story: Segment[];
  reading: {
    focus_position_id: string | null;
    open_relations: unknown[];
    interpretations: unknown[];
  };
  pending: null | {
    base_revision: number;
    proposals: Record<string, unknown>;
    realizations: Record<string, unknown>;
    committed: boolean;
  };
  audit: {
    shared_expressions: unknown[];
    effective_edges: unknown[];
    joint_targets: unknown[];
    last_batch: unknown[];
  };
}
export interface Preset {
  id: string;
  name: string;
  description: string;
  positions: Position[];
  expressions: Expression[];
}
export interface PublicConfig {
  provider: string;
  configured: boolean;
  model: string;
  timeout_seconds: number;
  max_output_tokens: number;
}
export interface Verification {
  ok: boolean;
  checks: { name: string; passed: boolean; detail: string }[];
  note: string;
}
export interface ReplayVerification {
  ok: boolean;
  digest: string | null;
  checks: { name: string; passed: boolean }[];
  note: string;
}
export interface Counterfactual {
  baseline_fingerprint: string;
  changed_fingerprint: string;
  changed_use_ids: string[];
  baseline_text: string;
  changed_text: string;
  structural_effect: boolean;
  semantic_fixture_effect: boolean;
  note: string;
}
