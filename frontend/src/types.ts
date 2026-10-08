export type Provider = "fixture" | "deepseek";
export type ExperimentMode = "classic" | "reflexive" | "reflexive_v2";
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
  max_calls: number | null;
  max_tokens: number | null;
  max_parallel: number | null;
  max_windows: number | null;
}
export type FiniteLimits = { [Field in keyof Limits]: number };
export interface RunSummary {
  id: string;
  title: string;
  provider: Provider;
  mode?: ExperimentMode;
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
export interface ReadingState {
  focus_position_id: string | null;
  open_relations: unknown[];
  interpretations: unknown[];
}
export interface ReadingTrack {
  id: string;
  title: string;
  start_position_id: string | null;
  created_revision: number;
  enabled: boolean;
  story: Segment[];
  reading: ReadingState;
  last_error: SafeError | null;
}
export interface Event {
  id: string;
  type: string;
  revision: number;
  at: string;
  data: Record<string, unknown>;
}
export interface Snapshot {
  schema_version: 1 | 2 | 3 | 4 | 5;
  run: Run;
  positions: Position[];
  expressions: Expression[];
  uses: Use[];
  continuations: Continuation[];
  events: Event[];
  story: Segment[];
  reading: ReadingState;
  reading_tracks?: ReadingTrack[];
  reflection?: ReflectionState | null;
  requests?: RequestEvidence[];
  schedule?: SchedulePlan | null;
  pending: null | {
    base_revision: number;
    proposals: Record<string, unknown>;
    realizations: Record<string, unknown>;
    committed: boolean;
    presentations?: Record<string, unknown>;
    deferred_releases?: { scope_id: string; method_id: string }[];
  };
  audit: {
    shared_expressions: unknown[];
    effective_edges: unknown[];
    joint_targets: unknown[];
    last_batch: unknown[];
  };
}
export interface RequestEvidence {
  id: string;
  stage: string;
  scope_id: string;
  input_world_revision: number;
  policy_revision: number;
  frame_hash: string;
  request_hash: string | null;
  projection_version: string;
  evidence_level: "exact_projection" | "frame_only";
  attempts: {
    number: number; status: string; repair_category?: string;
    input_tokens?: number; output_tokens?: number;
  }[];
  accepted_result_hash?: string | null;
}
export interface SchedulePlan {
  epoch_id: string;
  mode: "shadow";
  jobs: {
    key: string; stage: string; scope_id: string;
    reason: string; changed: boolean; dependencies: string[];
  }[];
}
export interface NarrativeObject {
  id: string;
  kind: string;
  text?: string;
  title?: string;
  preview_truncated?: boolean;
  view?: NarrativeView;
  [field: string]: unknown;
}
export interface ObjectCatalog {
  scope_id: string;
  query: string;
  objects: NarrativeObject[];
  total: number;
}
export interface InterventionPair {
  control: Snapshot;
  intervention: Snapshot;
  manifest: {
    id: string; origin_run_id: string; origin_revision: number;
    origin_digest: string; scope_id: string; method_id: string;
    intervention: "release_method";
  };
}
export interface MethodRecord {
  id: string;
  title: string;
  instruction: string;
  inputs: string[];
  order: "listed" | "reverse";
  limit: number | null;
  revises: string | null;
  created_revision: number;
  source_scope: string;
  language_version?: 1 | 2;
  program?: { steps: Record<string, unknown>[]; output: string } | null;
}
export interface ObjectRef {
  object_id: string;
  mode: "live" | "version";
  version?: string | null;
  start?: number | null;
  end?: number | null;
}
export interface ViewGroup {
  id: string;
  label: string;
  material_ids: string[];
  children: ViewGroup[];
}
export interface NarrativeView {
  groups: ViewGroup[];
  relations: { source: string; target: string; label: string }[];
  material_ids: string[];
}
export interface ViewRecord {
  id: string; created_revision: number; scope_id: string; view: NarrativeView;
}
export interface ResponseRecord {
  id: string; created_revision: number; source_scope: string; text: string;
  about: string[]; evidence: string[]; visibility: "scope" | "shared";
}
export interface EffectRecord {
  id: string; revision: number; scope_id: string; stage: string;
  choice_id: string; method_ids: string[];
  before_fingerprint: string | null; after_fingerprint: string; changed: boolean;
  selected_added: string[]; selected_removed: string[]; response_ids: string[];
}
export interface SelectedMaterial {
  id: string;
  text: string;
  position_id: string | null;
  expression_id: string | null;
  role: string;
}
export interface ChoiceRecord {
  id: string;
  revision: number;
  scope_id: string;
  stage: "propose" | "realize" | "present";
  offered: string[];
  selected: string[];
  methods: string[];
  groups: string[][];
  materials: SelectedMaterial[];
  input_fingerprint: string;
  plan_fingerprint?: string;
  view?: NarrativeView | null;
  dependencies?: ObjectRef[];
}
export interface ReflectionState {
  methods: MethodRecord[];
  adoptions: Record<string, string[]>;
  choices: ChoiceRecord[];
  views?: ViewRecord[];
  responses?: ResponseRecord[];
  effects?: EffectRecord[];
}
export interface ReflectionAblation {
  before_fingerprint: string;
  after_fingerprint: string;
  before_selected: string[];
  after_selected: string[];
  structural_effect: boolean;
  note: string;
}
export interface Preset {
  id: string;
  name: string;
  description: string;
  positions: Position[];
  expressions: { id: string; text: string; role?: string }[];
  origin?: "builtin" | "custom";
  editable?: boolean;
  version?: string | null;
  storage_path?: string;
}
export interface PresetCatalog {
  presets: Preset[];
  errors?: { id: string; code: string; message: string }[];
}
export interface PublicConfig {
  provider: string;
  configured: boolean;
  model: string;
  timeout_seconds: number;
  max_output_tokens: number;
  budget_bounds?: Limits;
  diagnostics?: {
    config_file_path: string;
    config_file_exists: boolean;
    file_api_key_present: boolean;
    environment_api_key_override: boolean;
    environment_api_key_empty_override: boolean;
    credential_source: "environment" | "file" | "none";
  };
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
