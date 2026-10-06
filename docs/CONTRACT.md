# OpenTale Demo v0.3 — module contract

## Architecture and ownership

The deterministic kernel owns expressions, scoped uses, continuations and batch installation. It never calls a model. Runner assembles the same revision's local window, persists proposals, installs all changes atomically, then realizes the updated continuations once and presents each enabled reading independently in parallel. Reading histories never become a universal fiction authority. A failed stage resumes without reinstalling a committed batch or repeating accepted readings. The provider is a replaceable async evaluator. UI displays prose separately from technical provenance.

An accepted batch must freshly rebind at least one shared position/expression/role from two distinct peers in this window. Existing joint bindings from prior windows do not suffice. Otherwise `JOINT_DEPENDENCY_MISSING` rejects the detached batch and its drafts are discarded before retry.

## Domain (Pydantic models, `opentale.domain`)

- `Binding`: `source_position_id`, `source_expression_id`, `value`, `anchor` (exact substring of proposal text), `revision`.
- `Use`: `id`, `position_id`, `expression_id`, `role` (open natural-language label), `bindings: list[Binding]`, `revision`.
- `Expression`: `id`, `text`, `created_revision`, `source_position_id: str | None`.
- `Position`: `id`, `name`, `seed` (initial MD text).
- `Continuation`: `position_id`, `frame_revision`, `status`, `seed_text`, `condition_fingerprint`, `dependencies: list[str]`, `surface`, `resume_count`.
- `Operation`: `op` is `rebind`, `target_position_id`, `expression_id`, `role`, `value`, `anchor`. All fields required. No hidden global truth normalization. Operators address an existing shared expression and scoped position. Future split/spawn operations are outside v0.1.
- `Proposal`: `position_id`, `based_on_revision`, `text` (unfinished narrative seed), `operations: list[Operation]`. Each anchor must occur in text. A proposal must change a peer's active condition; self-only appendages are rejected.
- `GenerationFrame`: `position_id`, `name`, `revision`, `seed_text`, `active_uses: list[dict]`, `condition_fingerprint`, `recent_surface: list[str]`, `peers: list[dict]`, `peer_interfaces: list[dict]`. Actor `active_uses` contain expression_id, expression text, role and binding values; exclude technical binding source provenance. Peer interfaces expose only position_id/expression_id/role writable handles, no private biography content. Seed profile is an active initial use, not a separate immutable prompt.
- `ReadingFrame`: `revision`, `candidates: list[dict]` (position_id, name, text, shared active uses), `previous_segments: list[dict]`, `open_relations: list[dict]`, `current_focus: str | None`, `start_position_id: str | None`, `required_focus_position_id: str | None`. History and reading state come only from this track; candidates come from the same completed world. No track IDs, titles or other tracks' prose enter model inputs. The initial position is required for the first paragraph; subsequent focus may move with relations.
- `ReadingTrack`: `id`, `title`, `start_position_id`, `created_revision`, `enabled`, independent `story`, independent `reading` (focus/open_relations/interpretations), operational `last_error`. Up to 64 tracks including `main`.
- `Presentation`: `text`, `focus_position_id`, `expression_ids: list[str]`, `reinterprets: list[str]` (existing story segment IDs). No technical references inside prose.
- `Limits`: `max_calls=24`, `max_tokens=128000`, `max_parallel=3`, `max_windows=8`; all positive and bounded by server (calls<=1000, tokens<=5120000, parallel<=8, windows<=1000). 5.12M means decimal 5,120,000 total experiment tokens shared by actors and all readings.

Application capacities share `opentale.capacity`: Proposal operations<=256, wire fragments<=128, Operation value/anchor<=16000 chars, Proposal text<=32000 chars, Binding value/anchor<=32000 chars, realization<=32000 chars and Presentation text<=64000 chars. Provider input<=512000 chars, response content<=256000 chars, response bytes<=2 MiB. The configured single-response output parameter permits 128..32768 tokens (default1600), independently of the experiment budget; existing local settings are never rewritten. Role/identifier and reference validity constraints remain unchanged.

## Provider adapter (`opentale.providers`)

`async propose(frame: GenerationFrame, *, repair: OutputFeedback|None=None) -> ModelResult[Proposal]`

`async realize(frame: GenerationFrame, *, repair: OutputFeedback|None=None) -> ModelResult[str]`

`async present(frame: ReadingFrame, *, repair: OutputFeedback|None=None) -> ModelResult[Presentation]`

`ModelResult[T]` exposes `value`, `input_tokens`, `output_tokens` (defaults 0). Runner owns reservation, accounting and persisting partial results. A provider call is charged even on failure; failed token counts may be unknown.

Optional internal `ProviderError.feedback: OutputFeedback|None` is immutable and allowlisted (fixed categories, schema paths and error kinds only). It contains no original output, values, arbitrary field names or credential-bearing validation exceptions; `public()` remains three safe fields. JSON normalization accepts only whole BOM/fence wrappers and at most two JSON-string wrappers, with duplicate keys and non-finite values still rejected. Unknown or ambiguous structures are never guessed or salvaged by dropping operations.

Runner owns bounded format recovery: only retryable MODEL_OUTPUT_INVALID with JSON_SYNTAX/JSON_TYPE/SCHEMA_VALIDATION feedback receives up to two extra evaluations of the unchanged frame. Each evaluation passes normal call/Token admission and accounting. Funded in-flight peers are subtracted from future base work when checking remaining call slots, so repairs cannot starve the window. Manual serialization reserves only its own call, not future actor work. Pause/cancel, lack of spare budget or exhausted repair count immediately preserve valid checkpoints. Other references/prose/security/transport failures do not receive format repair. Provider has no hidden retries; system feedback never contains rejected prose.

`ProviderError.public()` exposes safe `code`, `message`, `retryable` only. Optional internal `usage: tuple[int,int]|None` carries validated input/output token counts for rejected but billed responses; Runner settles known counts exactly and conservatively retains estimates for unknown results. Never expose raw exception strings, HTTP bodies, prompts or headers. Timeouts, HTTP failures, malformed JSON, excessive outputs and schema errors are bounded, sanitized failures. No unbounded internal retries. Fixture provider has no network. DeepSeek reads Settings from config, uses non-thinking JSON generation by default and discards reasoning content. Do not impose CoT requirements.

DeepSeek proposal transport is provider-local: narrative fragments carry explicit target/expression/role assignments. Compilation concatenates the exact fragment text and uses it unchanged as value/anchor; current position/revision come from the frame. The prompt schema enumerates current writable targets and expressions per target. Compatible legacy Proposal responses still undergo exact validation. Stored `Proposal`, kernel semantics and executable replay remain unchanged; compilation never guesses targets, fabricates prose or drops invalid operations. An existing shared interface may be recommended for coordination, while additional roles stay open.

`make_provider(mode: str, settings: Settings)` constructs `fixture` or `deepseek`.

Config `load_settings(root: Path) -> Settings` remains compatible. `load_settings_with_diagnostics(root)` returns settings and safe provenance from the same read. `Settings` fields `api_key` (SecretStr), `base_url`, `model`, `timeout_seconds`, `max_output_tokens`, `port`. `Settings.public()` exposes only provider, configured boolean, model, timeout/max output, **no key and no base URL**. `.env.local` accepts UTF-8 with or without BOM and loads without mutating process environment. Environment overrides remain authoritative, including an explicitly empty key. Config errors are sanitized. Only HTTPS remote endpoints or HTTP loopback are accepted. Changing configuration requires UI reload (allowed only if no live task).

## HTTP API (root implements)

### Custom preset editor (v0.2)

`GET /api/presets` also reads private custom groups from `<data_dir>/presets`. Each preset includes `origin:builtin|custom`, `editable:boolean`, `version:string|null`; custom presets include `storage_path` pointing to their current version directory. `errors` lists safe diagnostics for malformed custom groups while other presets stay usable.

`POST /api/presets` creates a custom group. `PUT /api/presets/{id}` updates a custom group with required `expected_version` to reject lost updates. Both return `{preset: enrichedPreset}`. Input definition: `{id,name,description,positions:[{id,name,seed}],expressions:[{id,text,role}]}`. Positions: 3–8, IDs lowercase ASCII identifier/underscore up to 40 chars, name up to 80 chars, Markdown seed 1–12000 chars. Expressions: 1–6, safe slug IDs up to 64 chars excluding `seed-` and `expr-` prefixes, text 1–6000 chars, open role up to 80 chars (default `共同生活`). Preset ID safe slug up to 64, name up to 100, description up to 2000. IDs must be unique; all text fields trimmed/nonblank where required.

Builtins are read-only in the web editor; copy to a new ID to customize. Save generates private `preset.json`, per-character `character.json` (ID/name only), and `character.md`. A version is fully written before an atomic `current.json` pointer replacement; failed or conflicting saves leave the prior version active. Old versions remain for recovery. Custom creation/editing does not call a model and never modifies already-created snapshots. All initial expressions are shared among the selected positions in v0.2; custom topology/spawn remains out of scope.

Preset writes accept up to 1 MiB of HTTP data; all other writes retain the smaller request bound. No credential-entry field is added; parsed preset values containing the configured credential are rejected before persistence.

`GET /api/health` -> `{ok:true, version:"0.3.1"}`

`GET /api/config`, `POST /api/config/reload` -> safe Settings.public, `budget_bounds:{max_calls:1000,max_tokens:5120000,max_parallel:8,max_windows:1000}`, plus `diagnostics`: `{config_file_path,config_file_exists,file_api_key_present,environment_api_key_override,environment_api_key_empty_override,credential_source}`. The source is `environment|file|none`. These fields describe only the actual file path, field presence and override state; never values, lengths, unknown field names, endpoint addresses or credentials. Reload failure preserves the last usable configuration and diagnostic snapshot. `configured` indicates that a nonempty key was loaded; it does not assert authentication or model availability, and loading/reloading never calls a model.

`POST /api/runs/{id}/fork` accepts optional `title` and `limits: Limits`. Omitted limits inherit the parent. Explicit limits must pass the ordinary budget bounds, apply only to the new branch and do not trigger generation. Pending accepted work and semantic evidence are retained; branch usage starts at zero while the parent's usage remains recorded.

`GET /api/presets` -> `{presets:[{id,name,description,positions:[{id,name,seed}],expressions:[...]}]}`

`POST /api/runs` body `{preset_id:"letters",provider:"fixture"|"deepseek",limits?:{...}}` -> full snapshot.

`GET /api/runs` -> `{runs:[{id,status,provider,revision,created_at,updated_at,title,usage:{calls,input_tokens,output_tokens},last_error}]}`

`GET /api/runs/{id}` -> full snapshot:

```
{schema_version:2, run:{id,title,provider,status,revision,created_at,updated_at,
 limits,usage:{calls,input_tokens,output_tokens,reserved_tokens,unknown_tokens,failed_calls,windows},last_error,phase,fork_of},
 positions:[], expressions:[], uses:[], continuations:[], events:[], story:[],
 reading:{focus_position_id,open_relations:[],interpretations:[]},
 reading_tracks:[{id,title,start_position_id,created_revision,enabled,story:[],reading:{},last_error}],
 pending:null|{base_revision,proposals:{},realizations:{},committed:boolean,presentations:{}},
 audit:{shared_expressions:[],effective_edges:[],joint_targets:[],last_batch:[]}}
```

`reading_tracks` is canonical. Top-level `story`/`reading` are computed compatibility projections of `main`; SQLite stores each history once. Version-1 snapshots load into a main track preserving prose, reading state, pending work, usage and old execution events. Unsupported future versions remain errors. There is no destructive bulk migration.

Events `{id,type,revision,at,data}`. Types include `created`, `window_started`, `proposal_ready`, `batch_installed`, `continuation_resumed`, `presented`, `window_completed`, `reading_created`, `reading_enabled`, `reading_error`, `paused`, `error`, `budget_exhausted`, `forked`, `limits_updated`. Each accepted presentation records `reading_id`; missing ID in legacy events means main. `window_completed` is emitted after enabled readings settle. Story segment `{id,revision,text,focus_position_id,expression_ids:[],reinterprets:[]}`. Status `idle|running|paused|error|budget_exhausted`; phase `idle|drafting|realizing|presenting`.

`POST /api/runs/{id}/readings` body `{title,start_position_id,enabled?:true}` creates an empty independent reading and returns Snapshot without generation. `PATCH /api/runs/{id}/readings/{reading_id}` body `{enabled:bool}` controls future automatic presentation, preserving history. Only stable runs permit configuration, including paused/failed pending windows. Newly created tracks start from the present; older primary prose is not copied into them.

`POST /api/runs/{id}/readings/{reading_id}/present` presents the current completed world only, returning Snapshot. It holds the shared run lock, never proposes/realizes actors and consumes the same call/Token budget. It works after the world-window cap is exhausted if call/Token budget remains. One paragraph per track/world revision is idempotent. READY requires all positions' continuation status=ready, matching current revision, nonempty surfaces, and (if a batch is committed but pending) every realization accepted. A future uncommitted draft does not invalidate the last completed world. No call occurs on READING_NOT_READY/RUN_BUSY guards. Each track may reference only its own history and its selected focus's currently active expressions, with at least one shared expression.

Automatic windows normally cost `2*N + M` calls for N actors and M enabled readings, plus failed attempts. Presentations run concurrently under the same max_parallel cap; accepted results are installed and persisted independently, including `pending.presentations`. Retry issues only missing actors/continuations/readings. Enabling/disabling does not add paid calls; all readings may be disabled to generate world state without automatic serialization.

`PATCH /api/runs/{id}/limits` body `Limits` -> updated full Snapshot. Only a stable run may be changed; RUN_BUSY rejects active execution. The budget must not fall below used calls, input/output/unknown/reserved token occupancy or completed windows (BUDGET_BELOW_USAGE). It preserves pending results, paid usage, semantic state and the original run ID, records limits_updated, and never invokes a provider. A budget-exhausted run becomes paused and clears only its old BUDGET_EXHAUSTED warning; other model errors remain as historical recovery context. Widened schemas accept existing snapshots without migration.

`POST /api/runs/{id}/step` -> full snapshot after one complete/recovered window. On safe errors, return 422 `{error:{code,message,retryable}}`; stored snapshot retains recoverable state.

`POST /api/runs/{id}/start` -> snapshot, background loop; `POST /pause` -> snapshot, cooperative pause at operation boundary. Resuming uses pending results. Same run cannot execute overlapping steps.

`POST /api/runs/{id}/fork` body `{title?:str,limits?:Limits}` -> independent snapshot; no merge/global-history authority.

`DELETE /api/runs/{id}` -> `{deleted:true}`, forbidden while active.

`GET /api/runs/{id}/export` -> sanitized JSON file; no provider config/secrets/reasoning.

`GET /api/runs/{id}/replay` -> `{ok,digest,checks:[{name,passed}],note}`. Re-executes recorded initial snapshot, batch proposals, continuation surfaces, reading configuration and presentations with no model calls. Compares positions/expressions/uses/continuations/story/reading/all canonical reading tracks. Track ordering is canonicalized; diagnostic last_error is not semantic truth. Missing or corrupt evidence fails safely. This is semantic-state replay, not reproduction of provider billing, wall-clock times or unpublished in-flight requests.

`POST /api/runs/{id}/counterfactual` body `{target_position_id,source_position_id}` -> OFFLINE only `{baseline_fingerprint,changed_fingerprint,changed_use_ids:[],baseline_text,changed_text,structural_effect:boolean,semantic_fixture_effect:boolean,note}`. Uses a detached copy, never changes original or invokes a paid provider.

`GET /api/verify` -> actual offline runtime checks `{ok,checks:[{name,passed,detail}],note}`. These demonstrate mechanics only.

Usage reserves a conservative request token estimate before a remote call. `reserved_tokens` are in-flight reservations; failures/restarts move unavailable token accounting to `unknown_tokens`; `failed_calls` and completed `windows` remain inspectable. Fixture reserves no tokens because it performs no model inference, while call and window admission still applies. Missing remote usage cannot prove an exact billing cap.

Error status 400/404/409/422/503 with safe error envelope. No permissive CORS; default localhost single origin.

## Frontend

React + TypeScript + Vite. Dev port 5196, proxy /api to 127.0.0.1:8231; production served by backend at 8231. Chinese UI: run creation, provider/config status, budgets, start/step/pause, phase display, selectable scoped positions, shared expression/uses graph, story reader, diagnostics/verification, offline counterfactual, fork/export/delete. No key-entry field. Poll active runs with backoff; display pending state and preserve partial story on safe errors. Tests verify control behavior and separation of technical provenance from prose.

## Verification contract

Demonstrate order-independent batch installation, two inputs affecting the same active target, scoped contradictions, stale-batch rejection, live expression reuse, changed condition fingerprints and fixture realization after source ablation, replay/snapshot recovery, budget admission before calls, partial-stage resume, safe provider failures and secret exclusion. No claim of actual LLM semantic/literary performance without a separately authorized live experiment.
