# OpenTale Demo v0.2 — module contract

## Architecture and ownership

The deterministic kernel owns expressions, scoped uses, continuations and batch installation. It never calls a model. Runner assembles the same revision's local window, persists proposals, installs all changes atomically, then realizes the updated continuations and presents one reading step. A failed stage can resume without reinstalling an already committed batch. The provider is a replaceable async evaluator, not an omniscient fiction judge. UI displays prose separately from technical provenance.

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
- `ReadingFrame`: `revision`, `candidates: list[dict]` (position_id, name, text, active uses), `previous_segments: list[dict]`, `open_relations: list[dict]`, `current_focus: str | None`.
- `Presentation`: `text`, `focus_position_id`, `expression_ids: list[str]`, `reinterprets: list[str]` (existing story segment IDs). No technical references inside prose.
- `Limits`: `max_calls=24`, `max_tokens=40000`, `max_parallel=3`, `max_windows=8`; all positive and bounded by server (calls<=200, tokens<=500000, parallel<=6, windows<=100).

## Provider adapter (`opentale.providers`)

`async propose(frame: GenerationFrame) -> ModelResult[Proposal]`

`async realize(frame: GenerationFrame) -> ModelResult[str]`

`async present(frame: ReadingFrame) -> ModelResult[Presentation]`

`ModelResult[T]` exposes `value`, `input_tokens`, `output_tokens` (defaults 0). Runner owns reservation, accounting and persisting partial results. A provider call is charged even on failure; failed token counts may be unknown.

`ProviderError` exposes safe `code`, `message`, `retryable` only. Never expose raw exception strings, HTTP bodies, prompts or headers. Timeouts, HTTP failures, malformed JSON, excessive outputs and schema errors are bounded, sanitized failures. No unbounded internal retries. Fixture provider has no network. DeepSeek reads Settings from config, uses non-thinking JSON generation by default and discards reasoning content. Do not impose CoT requirements.

`make_provider(mode: str, settings: Settings)` constructs `fixture` or `deepseek`.

Config `load_settings(root: Path) -> Settings`. `Settings` fields `api_key` (SecretStr), `base_url`, `model`, `timeout_seconds`, `max_output_tokens`, `port`. `Settings.public()` exposes only provider, configured boolean, model, timeout/max output, **no key and no base URL**. `.env.local` loaded without mutating global environment; environment overrides allowed. Config errors sanitized. Only HTTPS remote endpoints or HTTP loopback accepted. Changing configuration requires UI reload (allowed only if no live task).

## HTTP API (root implements)

### Custom preset editor (v0.2)

`GET /api/presets` also reads private custom groups from `<data_dir>/presets`. Each preset includes `origin:builtin|custom`, `editable:boolean`, `version:string|null`; custom presets include `storage_path` pointing to their current version directory. `errors` lists safe diagnostics for malformed custom groups while other presets stay usable.

`POST /api/presets` creates a custom group. `PUT /api/presets/{id}` updates a custom group with required `expected_version` to reject lost updates. Both return `{preset: enrichedPreset}`. Input definition: `{id,name,description,positions:[{id,name,seed}],expressions:[{id,text,role}]}`. Positions: 3–8, IDs lowercase ASCII identifier/underscore up to 40 chars, name up to 80 chars, Markdown seed 1–12000 chars. Expressions: 1–6, safe slug IDs up to 64 chars excluding `seed-` and `expr-` prefixes, text 1–6000 chars, open role up to 80 chars (default `共同生活`). Preset ID safe slug up to 64, name up to 100, description up to 2000. IDs must be unique; all text fields trimmed/nonblank where required.

Builtins are read-only in the web editor; copy to a new ID to customize. Save generates private `preset.json`, per-character `character.json` (ID/name only), and `character.md`. A version is fully written before an atomic `current.json` pointer replacement; failed or conflicting saves leave the prior version active. Old versions remain for recovery. Custom creation/editing does not call a model and never modifies already-created snapshots. All initial expressions are shared among the selected positions in v0.2; custom topology/spawn remains out of scope.

Preset writes accept up to 1 MiB of HTTP data; all other writes retain the smaller request bound. No credential-entry field is added; parsed preset values containing the configured credential are rejected before persistence.

`GET /api/health` -> `{ok:true, version:"0.2.0"}`

`GET /api/config`, `POST /api/config/reload` -> safe Settings.public.

`GET /api/presets` -> `{presets:[{id,name,description,positions:[{id,name,seed}],expressions:[...]}]}`

`POST /api/runs` body `{preset_id:"letters",provider:"fixture"|"deepseek",limits?:{...}}` -> full snapshot.

`GET /api/runs` -> `{runs:[{id,status,provider,revision,created_at,updated_at,title,usage:{calls,input_tokens,output_tokens},last_error}]}`

`GET /api/runs/{id}` -> full snapshot:

```
{schema_version:1, run:{id,title,provider,status,revision,created_at,updated_at,
 limits,usage:{calls,input_tokens,output_tokens,reserved_tokens,unknown_tokens,failed_calls,windows},last_error,phase,fork_of},
 positions:[], expressions:[], uses:[], continuations:[], events:[], story:[],
 reading:{focus_position_id,open_relations:[],interpretations:[]},
 pending:null|{base_revision,proposals:{},realizations:{},committed:boolean},
 audit:{shared_expressions:[],effective_edges:[],joint_targets:[],last_batch:[]}}
```

Events `{id,type,revision,at,data}`. Types `created`, `window_started`, `proposal_ready`, `batch_installed`, `continuation_resumed`, `presented`, `paused`, `error`, `budget_exhausted`, `forked`. Technical evidence stays in event.data. Story segment `{id,revision,text,focus_position_id,expression_ids:[],reinterprets:[]}`. Status `idle|running|paused|error|budget_exhausted`; phase `idle|drafting|realizing|presenting`.

`POST /api/runs/{id}/step` -> full snapshot after one complete/recovered window. On safe errors, return 422 `{error:{code,message,retryable}}`; stored snapshot retains recoverable state.

`POST /api/runs/{id}/start` -> snapshot, background loop; `POST /pause` -> snapshot, cooperative pause at operation boundary. Resuming uses pending results. Same run cannot execute overlapping steps.

`POST /api/runs/{id}/fork` body `{title?:str}` -> independent snapshot; no merge/global-history authority.

`DELETE /api/runs/{id}` -> `{deleted:true}`, forbidden while active.

`GET /api/runs/{id}/export` -> sanitized JSON file; no provider config/secrets/reasoning.

`GET /api/runs/{id}/replay` -> `{ok,digest,checks:[{name,passed}],note}`. Re-executes recorded initial snapshot, batch proposals, continuation surfaces and presentations with no model calls. Compares positions/expressions/uses/continuations/story/reading. Missing or corrupt evidence fails safely. This is semantic-state replay, not reproduction of provider billing, wall-clock times or unpublished in-flight requests.

`POST /api/runs/{id}/counterfactual` body `{target_position_id,source_position_id}` -> OFFLINE only `{baseline_fingerprint,changed_fingerprint,changed_use_ids:[],baseline_text,changed_text,structural_effect:boolean,semantic_fixture_effect:boolean,note}`. Uses a detached copy, never changes original or invokes a paid provider.

`GET /api/verify` -> actual offline runtime checks `{ok,checks:[{name,passed,detail}],note}`. These demonstrate mechanics only.

Usage reserves a conservative request token estimate before a remote call. `reserved_tokens` are in-flight reservations; failures/restarts move unavailable token accounting to `unknown_tokens`; `failed_calls` and completed `windows` remain inspectable. Fixture reserves no tokens because it performs no model inference, while call and window admission still applies. Missing remote usage cannot prove an exact billing cap.

Error status 400/404/409/422/503 with safe error envelope. No permissive CORS; default localhost single origin.

## Frontend

React + TypeScript + Vite. Dev port 5196, proxy /api to 127.0.0.1:8231; production served by backend at 8231. Chinese UI: run creation, provider/config status, budgets, start/step/pause, phase display, selectable scoped positions, shared expression/uses graph, story reader, diagnostics/verification, offline counterfactual, fork/export/delete. No key-entry field. Poll active runs with backoff; display pending state and preserve partial story on safe errors. Tests verify control behavior and separation of technical provenance from prose.

## Verification contract

Demonstrate order-independent batch installation, two inputs affecting the same active target, scoped contradictions, stale-batch rejection, live expression reuse, changed condition fingerprints and fixture realization after source ablation, replay/snapshot recovery, budget admission before calls, partial-stage resume, safe provider failures and secret exclusion. No claim of actual LLM semantic/literary performance without a separately authorized live experiment.
