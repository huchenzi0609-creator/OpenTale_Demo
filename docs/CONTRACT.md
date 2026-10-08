# OpenTale Demo v0.5 — module contract

## Context projections (v0.5.1)

New DeepSeek v2 requests use `projection_version:provider-json-v2-context`. The
adapter shares duplicate text/metadata while retaining every material occurrence,
the selected View and all original integer capabilities. Candidate structures are
previews. If required, deterministic excerpts retain exact source hashes/ranges and
explicit omissions; original frozen frames and archives are unchanged. No additional
model calls are made. `Settings.context_target_chars` defaults to 96000 and is a
soft per-request target, independent of nullable cumulative experiment limits.

`RequestEnvelope.context_compaction` optionally records the allowlisted compiler
statistics; `RequestAttempt.request_chars` records each actual system+user size.
The base manifest is stable across format repair, while the attempt size may change.
Source verification checks text pool intervals/metadata against the frozen frame.
Previously frozen `provider-json-v1` requests use a separate legacy adapter sharing
the same settings/client; successful stages are never re-generated. Pure preflight
validation runs before call/token accounting in all modes. Projection and actual
transport use the same deterministic implementation. See [context guide](CONTEXT_COMPACTION.md).

## Opt-in reflective programs (schema 5)

`mode:reflexive_v2` selects the versioned View/program/response engine. Classic and
reflexive v1 retain their original execution semantics. Schema 5 is required for v2;
schema 1–4 saves remain readable and are never silently promoted into v2.

Root owns domain/runtime integration. The engine owner extends reflection types and
projection/compiler; provider owns request-local wire compilation; UI consumes these
contracts. `ReflectionFrame.language_version=2` selects new provider behavior.

`Snapshot` gains `policy_revision`, `requests:RequestEnvelope[]`, optional `schedule`
and `experiment`. Request envelopes freeze stage/scope/epoch/world/policy revisions,
full frame, its hash and exact safe provider projection hash before evaluation. Each
attempt has its own projection/hash/usage/status. Authorization, endpoint configuration
and hidden reasoning are excluded. Providers without a projection expose `frame_only`
evidence. `Pending.proposal_frames` freezes proposal inputs even in v1; installation
and replay use these original frames rather than reconstructing accepted evidence.

v2 source contributions are updated by `(writer,target,expression,role)` slot. An
unscheduled or unmodified writer's contribution survives. `withdraw` explicitly removes
the submitting writer's slot; legacy modes reject it. V2 method releases requested
inside pending epochs are queued until the epoch completes.

`GET /api/runs/{id}/objects?scope_id=...&query=...` returns scoped preview catalog
`{scope_id,query,objects,total}`. `GET /api/runs/{id}/object?scope_id=...&object_id=...`
returns `{scope_id,object}` with full visible material. Both are read-only and make no
model calls. `POST /api/runs/{id}/experiments {scope_id,method_id}` creates control and
method-release branches at a complete stable checkpoint, returns `{control,intervention,
manifest}`, and does not generate. The manifest records common origin digest and versions.

SchedulePlan initially runs in `shadow` mode: it records reasons, effective input changes
and dependencies while honoring all explicit continuation requests. It does not claim
reduced model calls. Unbounded (`null`) budgets retain their established behavior.

V2 HTTP snapshots return request hashes/status/usage without stored frames or attempt
projections, and omit large frozen event inputs. Explicit export preserves full evidence;
internal replay never uses the abbreviated workbench projection. SQLite schema-5 saves
use content-addressed immutable nodes plus separate append-only events and sealed request
records. Only new events/unsealed requests are packed on each checkpoint. Completed-window
and deletion transactions collect unreachable nodes without removing other branches' roots.
Decoding preserves JSON value semantics: deduplicated physical nodes do not alias mutable
domain values. Manual presentation retries install any accepted-but-missing feedback without
another model call. Paired branches verify a code/non-secret-config signature before calls.

Schema 4 identifies nullable/unbounded or larger-than-legacy budget/method data. Existing finite schema 1/2/3 data remains readable without changing its limits or fiction. Saving extended data marks its format version explicitly; canonical reading histories and mode requirements still apply.

The optional reflexive experiment extends this contract as described in [REFLEXIVE_CONTRACT](REFLEXIVE_CONTRACT.md) and [the execution guide](REFLEXIVE_EXPERIMENT.md). Classic remains the default and retains the contract below; reflexive snapshots use schema 3. `POST /api/runs` additionally accepts `mode:classic|reflexive`. `GET /api/runs/{id}/reflection` exposes runtime method/choice evidence; POST `reflection/ablate` compares a detached plan and POST `reflection/release` explicitly releases an adopted scoped method at a stable checkpoint, recording `method_released`. Release preserves any already frozen pending inputs. Neither action invokes a model. Choice/reflection evidence is included in semantic replay. New events `reading_inputs_frozen` and `reflection_feedback` pin concurrent reading inputs and apply accepted method feedback collectively. The development profile isolates experimental storage and assets.

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
- `Limits`: `max_calls=24`, `max_tokens=128000`, `max_parallel=3`, `max_windows=8`; positive integers without software ceilings, or null for unlimited. Defaults and saved limits remain finite unless explicitly changed. Parallel null permits all available stage jobs; it does not create extra jobs. All accounting remains shared by actors and readings.

Application capacities share `opentale.capacity`: Proposal operations<=256, wire fragments<=128, Operation value/anchor<=16000 chars, Proposal text<=32000 chars, Binding value/anchor<=32000 chars, realization<=32000 chars and Presentation text<=64000 chars. Provider input<=512000 chars, response content<=256000 chars, response bytes<=2 MiB. The configured single-response output parameter permits positive values >=128 without a software ceiling (default1600), independently of the experiment budget; existing local settings are never rewritten. Role/identifier and reference validity constraints remain unchanged.

## Provider adapter (`opentale.providers`)

`async propose(frame: GenerationFrame, *, repair: OutputFeedback|None=None) -> ModelResult[Proposal]`

`async realize(frame: GenerationFrame, *, repair: OutputFeedback|None=None) -> ModelResult[str]`

`async present(frame: ReadingFrame, *, repair: OutputFeedback|None=None) -> ModelResult[Presentation]`

`ModelResult[T]` exposes `value`, `input_tokens`, `output_tokens` (defaults 0). Runner owns reservation, accounting and persisting partial results. A provider call is charged even on failure; failed token counts may be unknown.

Optional internal `ProviderError.feedback: OutputFeedback|None` is immutable and allowlisted (fixed categories, schema paths and error kinds only). It contains no original output, values, arbitrary field names or credential-bearing validation exceptions; `public()` remains three safe fields. JSON normalization accepts only whole BOM/fence wrappers and at most two JSON-string wrappers, with duplicate keys and non-finite values still rejected. Unknown or ambiguous structures are never guessed or salvaged by dropping operations.

Runner owns bounded format recovery: only retryable MODEL_OUTPUT_INVALID with JSON_SYNTAX/JSON_TYPE/SCHEMA_VALIDATION feedback receives up to two extra evaluations of the unchanged frame. Each evaluation passes normal call/Token admission and accounting. Funded in-flight peers are subtracted from future base work when checking remaining call slots, so repairs cannot starve the window. Manual serialization reserves only its own call, not future actor work. Pause/cancel, lack of spare budget or exhausted repair count immediately preserve valid checkpoints. Only propose may also retry PROPOSAL_REFERENCE with one fixed TARGET_UNKNOWN/EXPRESSION_UNAVAILABLE/INTERFACE_UNKNOWN issue, sharing the same two-attempt ceiling. Presentation READING_INVALID (READING_SCOPE_INVALID, READING_FOCUS_INVALID, READING_EXPRESSION_INVALID, READING_HISTORY_INVALID or READING_SHARED_REQUIRED) also uses the same two-extra-evaluation ceiling and exact frozen frame. All other prose/security/transport failures remain non-repairable; a fresh response must pass unchanged permissions before acceptance. Provider has no hidden retries; system feedback never contains rejected prose.

`ProviderError.public()` exposes safe `code`, `message`, `retryable` only. Optional internal `usage: tuple[int,int]|None` carries validated input/output token counts for rejected but billed responses; Runner settles known counts exactly and conservatively retains estimates for unknown results. Never expose raw exception strings, HTTP bodies, prompts or headers. Timeouts, HTTP failures, malformed JSON, excessive outputs and schema errors are bounded, sanitized failures. No unbounded internal retries. Fixture provider has no network. DeepSeek reads Settings from config, uses non-thinking JSON generation by default and discards reasoning content. Do not impose CoT requirements.

DeepSeek proposal transport is provider-local: narrative fragments carry `{interface_index:int,role:str}` target assignments. A deterministic request-local routing table enumerates the same writable `(peer, expression)` pairs as before: local active expressions plus the selected peer's exposed interfaces. The model selects an enumerated integer, and compilation mechanically fills persistent IDs, concatenates exact fragment text and uses it unchanged as value/anchor; position/revision come from the frame. Index values do not become persistent identities. Compatible named-fragment and legacy Proposal responses still undergo exact validation, with no fuzzy aliases. Stored `Proposal`, kernel semantics and replay remain unchanged; compilation never guesses targets, fabricates prose or drops invalid operations. Existing common scopes recommend matching indices; additional roles stay open.

`make_provider(mode: str, settings: Settings)` constructs `fixture` or `deepseek`.

Config `load_settings(root: Path) -> Settings` remains compatible. `load_settings_with_diagnostics(root)` returns settings and safe provenance from the same read. `Settings` fields `api_key` (SecretStr), `base_url`, `model`, `timeout_seconds`, `max_output_tokens`, `port`. `Settings.public()` exposes only provider, configured boolean, model, timeout/max output, **no key and no base URL**. `.env.local` accepts UTF-8 with or without BOM and loads without mutating process environment. Environment overrides remain authoritative, including an explicitly empty key. Config errors are sanitized. Only HTTPS remote endpoints or HTTP loopback are accepted. Changing configuration requires UI reload (allowed only if no live task).

## HTTP API (root implements)

### Custom preset editor (v0.2)

`GET /api/presets` also reads private custom groups from `<data_dir>/presets`. Each preset includes `origin:builtin|custom`, `editable:boolean`, `version:string|null`; custom presets include `storage_path` pointing to their current version directory. `errors` lists safe diagnostics for malformed custom groups while other presets stay usable.

`POST /api/presets` creates a custom group. `PUT /api/presets/{id}` updates a custom group with required `expected_version` to reject lost updates. Both return `{preset: enrichedPreset}`. Input definition: `{id,name,description,positions:[{id,name,seed}],expressions:[{id,text,role}]}`. Positions: 3–8, IDs lowercase ASCII identifier/underscore up to 40 chars, name up to 80 chars, Markdown seed 1–12000 chars. Expressions: 1–6, safe slug IDs up to 64 chars excluding `seed-` and `expr-` prefixes, text 1–6000 chars, open role up to 80 chars (default `共同生活`). Preset ID safe slug up to 64, name up to 100, description up to 2000. IDs must be unique; all text fields trimmed/nonblank where required.

Builtins are read-only in the web editor; copy to a new ID to customize. Save generates private `preset.json`, per-character `character.json` (ID/name only), and `character.md`. A version is fully written before an atomic `current.json` pointer replacement; failed or conflicting saves leave the prior version active. Old versions remain for recovery. Custom creation/editing does not call a model and never modifies already-created snapshots. All initial expressions are shared among the selected positions in v0.2; custom topology/spawn remains out of scope.

Preset writes accept up to 1 MiB of HTTP data; all other writes retain the smaller request bound. No credential-entry field is added; parsed preset values containing the configured credential are rejected before persistence.

`GET /api/health` -> `{ok:true, version:"0.5.1"}`

`GET /api/config`, `POST /api/config/reload` -> safe Settings.public, `budget_bounds:{max_calls:null,max_tokens:null,max_parallel:null,max_windows:null}`, plus `diagnostics`: `{config_file_path,config_file_exists,file_api_key_present,environment_api_key_override,environment_api_key_empty_override,credential_source}`. The source is `environment|file|none`. These fields describe only the actual file path, field presence and override state; never values, lengths, unknown field names, endpoint addresses or credentials. Reload failure preserves the last usable configuration and diagnostic snapshot. `configured` indicates that a nonempty key was loaded; it does not assert authentication or model availability, and loading/reloading never calls a model.

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
