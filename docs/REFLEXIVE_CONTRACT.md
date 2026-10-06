# Reflexive experiment implementation contract

This branch adds an opt-in `mode=reflexive`; default/classic remains unchanged. New runs in reflexive mode use snapshot schema 3 with `reflection: ReflectionState`. Existing schema 1/2 runs remain classic and retain data. No paid calls are authorized for development.

## Shared models (root-owned, already declared)

`opentale.reflection_types` defines:

- `MethodDraft`: title (1..100), instruction (0..1200), inputs (1..32 existing object IDs), order (`listed|reverse`), limit (1..32, default16), revises (optional visible method ID).
- `MethodRecord`: MethodDraft + id, created_revision, source_scope (diagnostic only).
- `AdoptionDraft`: scope_id, method_id, action (`adopt|release`). Newly generated methods are addressed in the same response as `draft:0`, `draft:1` etc. Existing methods require visible IDs.
- `ReflectionProposal`: methods (0..4), adoptions (0..16).
- `ReflectionFrame`: objects (request-owned capability table), scopes, active_methods, selected_inputs, groups, materials, instruction, input_fingerprint. `objects` rows contain id, kind (`content|method|choice`), text and kind-specific safe fields. `scopes` rows contain id/name. No creator provenance, hidden reasoning or API data.
- `ReflectionState`: methods, adoptions (scope -> active method IDs), choices.
- `ChoiceRecord`: id, revision, scope_id, stage, offered/selected IDs, active method IDs, groups, exact selected Material snapshots, input_fingerprint. Computed by runtime, not model reasoning.

`Proposal` and `Presentation` have optional `reflection: ReflectionProposal|None`. `GenerationFrame` and `ReadingFrame` have optional `reflection: ReflectionFrame|None`. Classic wire requests must retain their existing format; expose reflexive fields only when the frame has reflection.

## Execution (root)

Methods select visible objects. Content contributes material; choices contribute their frozen selected materials; methods recursively expand inputs. Listed/reverse ordering and limit affect actual input. Multiple scoped methods contribute separate groups and instructions. Method versions are immutable; revises records lineage, and adoption replaces that predecessor only within the requested scope. Release is scoped. References are validated against the exact request table. Expansion is bounded (steps/material count); arbitrary code, config mutation and hidden data are unavailable.

Inputs may contain existing methods or choices; no fixed meta-level field. New methods cannot reference same-response drafts as inputs (only adoptions use draft aliases), so each committed version can reference prior versions without same-batch recursion.

Root integrates actor methods into actual GenerationFrame material selection, reading methods into candidate selection/organization, captures choices, and adopts reading feedback only after all presentation frames for a revision have been frozen. Window scheduling remains same-version parallel microbatches in this first experiment; actor count/dynamic spawning remains unchanged. Joint dependency is permitted rather than mandatory in reflexive mode.

Single execution is bounded at 256 traversal/cache-lookup steps, 64 material occurrences and 120,000 full material/instruction characters; at most 16 active methods per scope. Immutable shared subgraphs are memoized per execution without changing ordered material multiplicity. Candidate previews are bounded and explicitly marked; actual selected materials are not silently truncated. Software fingerprints compare resulting materials/groups/instructions, not method IDs alone.

## Provider owner

Own providers/ and provider tests. Compile wire integer `object_indices`, optional `revises_index`, `scope_index`, and `method_index` (existing visible method object index) or `draft_index` into ReflectionProposal. Do not let the model invent IDs. Add optional reflection schema to propose/present only for reflexive frames; realize consumes selected materials and instruction but returns text only. Empty normal operations are legal in reflexive mode; classic retains peer/joint semantics. Narrative fragments remain available for rebind.

Fixture should exercise creation/adoption, reuse of methods/choice records, revision of a method and reading feedback over multiple windows, with deterministic data. This demonstrates mechanics, not spontaneous LLM emergence. Preserve all existing classic behavior/tests.

## UI owner

Own frontend/ and frontend tests. Create API body accepts `mode: classic|reflexive`; run.mode defaults classic for legacy. New creation selector with clear Chinese labels. Add reflection types matching models; schema_version 2|3. In reflexive runs show methods, adopted scopes, actual choice history (selected IDs/groups), revision lineage and input fingerprints in a technical panel separate from story. No key UI or automatic remote calls. Existing creation/tests default classic. API `GET /api/runs/{id}/reflection` returns {mode, methods, adoptions, choices, note}; optional ablation `POST /api/runs/{id}/reflection/ablate` body {scope_id,method_id} returns before/after fingerprints, selected IDs, structural_effect and note; detached, offline, no original mutation. `POST reflection/release` accepts the same body, returns Snapshot, requires no live job but permits pending recovery. Frozen work is preserved; the adoption change applies when inputs are next compiled and is replayed from method_released.

## Verification/docs owner

Own scripts/ and docs/ except CONTRACT/REFLEXIVE_CONTRACT, README, CHANGELOG. After implementation inspect it and document actual execution, limitations, reproducible offline checks. Do not overclaim self-modifying model weights or meaningful narrative intentions; choices are software-level selection traces. No changes outside this repository.
