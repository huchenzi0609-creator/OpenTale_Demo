"""Offline evaluator exercising references and joint updates, not an LLM simulation."""

from __future__ import annotations

from collections import defaultdict

from opentale.domain import GenerationFrame, Operation, Presentation, Proposal, ReadingFrame
from opentale.policy_types import PolicyProgram
from opentale.reflection_types import (
    AdoptionDraft,
    MethodDraft,
    ReflectionFrame,
    ReflectionProposal,
    ResponseDraft,
)

from .base import ModelResult, ProviderError
from .feedback import OutputFeedback
from .reading import eligible_candidates, shared_expression_ids, validate_presentation
from .reflection import materials_data, reflection_data, view_data


def _shared_use(frame: GenerationFrame) -> dict:
    peer_ids = {peer["id"] for peer in frame.peers if peer.get("id") != frame.position_id}
    interfaces: dict[tuple[str, str], set[str]] = defaultdict(set)
    for interface in frame.peer_interfaces:
        if interface.get("position_id") in peer_ids:
            interfaces[(interface["expression_id"], interface["role"])].add(interface["position_id"])
    shared = [
        use for use in frame.active_uses
        if len(interfaces[(use["expression_id"], use["role"])]) >= 2
    ]
    if shared:
        # Stable selection across positions ensures writers share a real target scope.
        return min(
            shared,
            key=lambda use: (
                -len(interfaces[(use["expression_id"], use["role"])]),
                use["expression_id"], use["role"],
            ),
        )
    # Historical unit frames predate peer_interfaces. Actual frames never select by carrier ID.
    if not frame.peer_interfaces:
        legacy = next((use for use in frame.active_uses if use.get("expression_id") == "e-letter"), None)
        if legacy is not None:
            return legacy
    raise ProviderError("FIXTURE_FRAME_INVALID", "离线示例需要至少三处共同承接同一表达与用法。")


def _gestures(raw: str) -> tuple[str, tuple[str, ...]]:
    if "门" in raw:
        return "门外的声音", (
            "停在那扇尚未打开的门前",
            "想到门前的等待，却迟迟没有走过去开门",
            "把先前的等待当作门外即将出现的动静",
            "认出门外的声音，却没有立刻叫出它的主人",
        )
    if any(token in raw for token in ("封信", "书信", "信件", "信纸", "折信", "写信", "读信", "寄信", "信的")):
        return "那封信", (
            "把那封信折好，放在仍然空着的椅子上",
            "停在信的第一行，等待一个尚未说出的名字",
            "把已经读过的话当作即将写出的第一句",
            "认出那封信里的声音，却没有立刻叫出它的主人",
        )
    return "这一段共同经历", (
        "停在原先的经历中，重新看待眼前的情形",
        "把这段经历留在记忆中，暂缓接下来的动作",
        "重新接续这段经历，让先前的迟疑有了另一种意味",
        "回到刚才的情形，并没有立刻决定下一步",
    )


def _glimpse(values: list, fallback: str, budget: int = 180) -> str:
    if not values:
        return fallback[:budget]
    # Every live scoped value contributes, without quadratic growth over successive windows.
    quota = max(1, (budget - len(values)) // len(values))
    return "；".join(str(value)[:quota] for value in values)


def _fixture_method(
    frame: ReflectionFrame, scope_id: str, *, reading: bool = False, revision: int = 0,
) -> ReflectionProposal:
    """A scripted method evolution, deliberately not evidence of spontaneous intention."""
    if frame.language_version == 2:
        return _fixture_program(frame, scope_id, reading=reading)
    by_kind = {
        kind: [row for row in frame.objects if row["kind"] == kind]
        for kind in ("content", "choice", "method")
    }
    if reading:
        # Feedback adopted by an actor cannot smuggle track-local choices,
        # private biographies or another actor's current surface into that scope.
        by_kind["content"] = [
            row for row in by_kind["content"]
            if row.get("role") != "主体背景" and not row["id"].startswith("surface:")
        ]
        by_kind["choice"] = []
        safe = {row["id"] for row in by_kind["content"]}
        safe_methods = []
        remaining = list(by_kind["method"])
        while remaining:
            accepted = [row for row in remaining if set(row.get("inputs", [])) <= safe]
            if not accepted:
                break
            safe_methods.extend(accepted)
            safe.update(row["id"] for row in accepted)
            remaining = [row for row in remaining if row not in accepted]
        by_kind["method"] = safe_methods
    inputs = [row["id"] for row in by_kind["content"][-2:]]
    if by_kind["choice"]:
        inputs.insert(0, by_kind["choice"][-1]["id"])
    # Periodic direct selection demonstrates folding a method back into ordinary
    # materials and bounds this scripted demonstration's recursive dependency depth.
    visible_methods = {row["id"] for row in by_kind["method"]}
    reusable = sorted(set(frame.active_methods) & visible_methods)
    if reusable and revision % 3:
        # objects are offered in dependency order, not chronological order;
        # the last object can be a deep abandoned historical branch.
        inputs.insert(0, reusable[-1])
    if not inputs:
        raise ProviderError("FIXTURE_FRAME_INVALID", "离线反身示例需要至少一项可见材料。")
    revises = reusable[-1] if reusable else None
    method = MethodDraft(
        title="离线脚本·阅读回流" if reading else "离线脚本·材料重组",
        instruction="把相邻经历并置，沿其中一个动作继续，呈现这些经历怎样改变彼此的含义。",
        inputs=inputs[:4], order="reverse" if revises else "listed", limit=4, revises=revises,
    )
    scopes = [scope_id]
    if reading:
        actor = next((row["id"] for row in frame.scopes if row["id"].startswith("position:")), None)
        if actor:
            scopes.append(actor)
    # A scripted demonstration should cease executing the old organizations,
    # while immutable records remain available for inspection and later reuse.
    # revises replaces one predecessor; explicit releases clean every other
    # own-scope method, including a previously returned reading method.
    all_visible_methods = {row["id"] for row in frame.objects if row["kind"] == "method"}
    releases = [AdoptionDraft(scope_id=scope_id, method_id=value, action="release")
                for value in sorted(set(frame.active_methods) & all_visible_methods)
                if value != revises]
    adoptions = [AdoptionDraft(scope_id=value, method_id="draft:0") for value in scopes]
    if len(releases) + len(adoptions) > 16:
        raise ProviderError("FIXTURE_FRAME_INVALID", "离线示例的采用清理超过单次操作容量。")
    return ReflectionProposal(methods=[method], adoptions=[*releases, *adoptions])


def _fixture_program(frame: ReflectionFrame, scope_id: str, *, reading: bool) -> ReflectionProposal:
    """Execute inspect/revise/apply using a prior shared response as real input.

    This deliberately scripted loop demonstrates mechanics. It performs no
    semantic scoring and makes no claim about spontaneous narrative intention.
    """
    public = [row for row in frame.objects if not row.get("private_scope")]
    contents = sorted((row for row in public if row["kind"] == "content"
                       and row.get("role") != "主体背景" and not row["id"].startswith("surface:")),
                      key=lambda row: row["id"])
    if not contents:
        raise ProviderError("FIXTURE_FRAME_INVALID", "离线程序示例需要可共同读取的材料。")
    methods = [row for row in public if row["kind"] == "method"]
    active = [row for row in methods if row["id"] in frame.active_methods]
    old = sorted(active, key=lambda row: row["id"])[-1] if active else None
    visible_methods = {row["id"] for row in methods}
    responses = [row for row in public if row["kind"] == "response" and row.get("visibility") == "shared"
                 and set(row.get("about", [])).intersection(visible_methods)]
    response = responses[-1] if responses else None
    primary = contents[0]
    secondary = response or contents[min(1, len(contents) - 1)]
    next_program = {
        "steps": [
            {"id": "experience", "op": "read", "refs": [{"object_id": primary["id"]}]},
            {"id": "return", "op": "read", "refs": [{"object_id": secondary["id"]}]},
            {"id": "continued", "op": "relate", "inputs": ["experience", "return"],
             "relations": [{"source": "experience", "target": "return",
                            "label": "组织后的局部回应参与继续" if response else "相邻经历共同进入"}]},
        ], "output": "continued",
    }
    program = next_program
    if old is not None:
        # The replacement is a compact program. We inspect the actual prior
        # definition, replace its body, then execute the revised definition.
        program = {"steps": [
            {"id": "prior", "op": "inspect", "ref": {"object_id": old["id"]}},
            {"id": "changed", "op": "revise", "input": "prior", "patch": {
                "program": next_program,
                "instruction": "让这次返回的经验改变接下来关注的关系。"}},
            {"id": "used", "op": "apply", "method": "changed"},
        ], "output": "used"}
    method = MethodDraft(
        title="离线程序·阅读回流" if reading else "离线程序·关系续接",
        instruction="沿实际选中的经历与回应继续，让关系落实为人物当前的生活。",
        language_version=2, program=PolicyProgram.model_validate(program),
        revises=old["id"] if old else None,
    )
    scopes = [scope_id]
    if reading:
        actor = next((row["id"] for row in frame.scopes if row["id"].startswith("position:")), None)
        if actor:
            scopes.append(actor)
    releases = [AdoptionDraft(scope_id=scope_id, method_id=value, action="release")
                for value in frame.active_methods if value in {row["id"] for row in frame.objects}
                and value != method.revises]
    local_responses = []
    if old:
        groups = len(frame.view.groups) if frame.view else len(frame.groups)
        effects = [row for row in public if row["kind"] == "effect" and old["id"] in row.get("method_ids", [])]
        effect = effects[-1] if effects else None
        observation = effect["text"] if effect else f"{groups}组经历共同进入当前叙述。"
        local_responses.append(ResponseDraft(
            text=f"这次组织留下这样的经历：{observation}人物继续时，先前的经历与返回的理解可以互相改变。",
            about=[old["id"], *([effect["id"]] if effect else [])],
            evidence=[primary["id"], *([effect["id"]] if effect else [])], visibility="shared",
        ))
    return ReflectionProposal(methods=[method], responses=local_responses,
                              adoptions=[*releases, *(AdoptionDraft(scope_id=scope, method_id="draft:0")
                                                      for scope in scopes)])


def _selected_life(frame: ReflectionFrame, name: str) -> str:
    # The exact selected snapshots and adopted instruction affect this deterministic surface.
    material = "。".join(value.text[:180] for value in frame.materials[:6])
    if not material:
        material = "她走到窗边，抬手摸了摸窗框"
    direction = frame.instruction[:160]
    structure = ""
    if frame.language_version == 2 and frame.view:
        labels = [group.label for group in frame.view.groups if group.label]
        labels.extend(relation.label for relation in frame.view.relations)
        structure = "、".join(labels)[:160]
    return (f"{name}接续眼前的经历：{material}。"
            + (f"这些经历使她想到：{direction}。" if direction else "")
            + (f"眼前的牵连逐渐清楚：{structure}。" if structure else ""))


class FixtureProvider:
    def request_projection(self, frame: GenerationFrame | ReadingFrame, method: str, *, repair=None) -> dict:
        """Deterministic fixture inputs, safely separated from full resolver state."""
        if method not in {"propose", "realize", "present"}:
            raise ValueError("Unknown generation method")
        result = {"provider": "fixture", "fixture_version": 2, "method": method,
                  "revision": frame.revision}
        if frame.reflection is not None:
            result["reflection"] = reflection_data(frame.reflection) if method != "realize" else {
                "materials": materials_data(frame.reflection), "view": view_data(frame.reflection),
                "instruction": frame.reflection.instruction,
            }
        else:
            if isinstance(frame, GenerationFrame):
                result["frame"] = {
                    "seed_text": frame.seed_text,
                    "active_uses": [{key: row[key] for key in ("expression_id", "text", "role", "values")
                                     if key in row} for row in frame.active_uses],
                    "peers": [{"id": row["id"], "name": row["name"]} for row in frame.peers],
                    "peer_interfaces": [{key: row[key] for key in ("position_id", "expression_id", "role")
                                         if key in row} for row in frame.peer_interfaces],
                }
        if isinstance(frame, GenerationFrame):
            result["name"] = frame.name
        else:
            result["reading"] = {
                "candidates": [{"position_id": row["position_id"], "name": row["name"],
                                "text": row["text"],
                                "active_uses": [{key: use[key] for key in ("expression_id", "text", "role", "values")
                                                 if key in use} for use in row.get("active_uses", [])]}
                               for row in frame.candidates],
                "previous_segments": [{key: row[key] for key in
                                       ("id", "revision", "text", "focus_position_id", "expression_ids", "reinterprets")
                                       if key in row} for row in frame.previous_segments],
                "start_position_id": frame.start_position_id,
                "required_focus_position_id": frame.required_focus_position_id,
                "current_focus": frame.current_focus,
                "open_relations": [{key: row[key] for key in
                                    ("expression_id", "role", "position_id", "position_ids") if key in row}
                                   for row in frame.open_relations],
            }
        return result

    """Deterministic fixtures intentionally expose the causal dependency in surface text."""

    async def propose(
        self, frame: GenerationFrame, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[Proposal]:
        if frame.reflection is not None:
            own_scope = next(
                (row["id"] for row in frame.reflection.scopes
                 if row["id"] in {frame.position_id, f"position:{frame.position_id}"}),
                frame.reflection.scopes[0]["id"] if frame.reflection.scopes else frame.position_id,
            )
            return ModelResult(Proposal(
                position_id=frame.position_id, based_on_revision=frame.revision,
                text=_selected_life(frame.reflection, frame.name), operations=[],
                reflection=_fixture_method(frame.reflection, own_scope, revision=frame.revision),
            ))
        shared = _shared_use(frame)
        peers = [peer for peer in frame.peers if peer.get("id") != frame.position_id]
        if frame.peer_interfaces:
            target_ids = {
                interface["position_id"] for interface in frame.peer_interfaces
                if (interface.get("expression_id"), interface.get("role"))
                == (shared["expression_id"], shared["role"])
            }
            peers = [peer for peer in peers if peer["id"] in target_ids]
        peers = sorted(peers, key=lambda peer: peer["id"])
        if not peers:
            raise ProviderError("FIXTURE_FRAME_INVALID", "离线示例需要至少一位承接者。")
        raw = str(shared.get("text", ""))
        carrier, gestures = _gestures(raw)
        prior_values = shared.get("values", [])
        remembered = _glimpse(prior_values, raw)
        operations = []
        anchors = []
        for index, peer in enumerate(peers):
            gesture = gestures[(frame.revision + index) % len(gestures)]
            value = f"{peer['name']}此刻{gesture}；那句话仍带着{frame.name}记得的意味：{remembered}"
            anchors.append(value)
            operations.append(
                Operation(
                    op="rebind",
                    target_position_id=peer["id"],
                    expression_id=shared["expression_id"],
                    role=shared["role"],
                    value=value,
                    anchor=value,
                )
            )
        text = (
            f"{frame.name}重新想到{carrier}：{raw[:120]}。"
            + "。".join(anchors)
            + "。接下来，这一段生活还将由谁承担……"
        )
        return ModelResult(
            Proposal(
                position_id=frame.position_id,
                based_on_revision=frame.revision,
                text=text,
                operations=operations,
            )
        )

    async def realize(
        self, frame: GenerationFrame, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[str]:
        if frame.reflection is not None:
            return ModelResult(_selected_life(frame.reflection, frame.name))
        shared = _shared_use(frame)
        values = [str(value) for value in shared.get("values", [])]
        live_content = "。".join(values) if values else str(shared.get("text", ""))[:600]
        # This reads all active scoped bindings: removing one source changes this very prose.
        surface = f"{frame.name}停在这一段生活里。{live_content}。这次的停顿，使先前的记忆又取得了另一种意味。"
        return ModelResult(surface)

    async def present(
        self, frame: ReadingFrame, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[Presentation]:
        if not frame.candidates:
            raise ProviderError("FIXTURE_FRAME_INVALID", "还没有可以承接的叙述。")
        if frame.reflection is not None:
            candidates = eligible_candidates(frame)
            candidate = sorted(candidates, key=lambda row: row["position_id"])[frame.revision % len(candidates)]
            own_scope = next(
                (row["id"] for row in frame.reflection.scopes if row["id"].startswith("reading:")),
                frame.reflection.scopes[0]["id"],
            )
            value = Presentation(
                text=_selected_life(frame.reflection, candidate["name"]),
                focus_position_id=candidate["position_id"],
                expression_ids=[use["expression_id"] for use in candidate.get("active_uses", [])],
                reinterprets=[frame.previous_segments[-1]["id"]] if frame.previous_segments else [],
                reflection=_fixture_method(frame.reflection, own_scope, reading=True, revision=frame.revision),
            )
            validate_presentation(frame, value)
            return ModelResult(value)
        shared_ids = shared_expression_ids(frame, legacy_letter=True)
        try:
            eligible = eligible_candidates(frame, legacy_letter=True)
        except ProviderError as error:
            raise ProviderError("FIXTURE_FRAME_INVALID", "还没有可以共同承接的有效表达。") from error

        def relevance(candidate: dict) -> tuple[int, bool, bool, int]:
            position_id = candidate["position_id"]
            uses = [use for use in candidate.get("active_uses", []) if use.get("expression_id") in shared_ids]
            candidate_shared_ids = {use["expression_id"] for use in uses}
            relation_weight = 0
            for relation in frame.open_relations:
                if relation.get("expression_id") not in candidate_shared_ids:
                    continue
                if relation.get("role") and not any(
                    use.get("expression_id") == relation["expression_id"]
                    and use.get("role") == relation["role"] for use in uses
                ):
                    continue
                participants = relation.get("position_ids", [])
                target = relation.get("position_id")
                if target == position_id:
                    relation_weight += 3
                if position_id in participants:
                    relation_weight += 1
                    if frame.current_focus in participants and position_id != frame.current_focus:
                        relation_weight += 4
            previous_focuses = [segment.get("focus_position_id") for segment in frame.previous_segments]
            visits = previous_focuses.count(position_id)
            return (relation_weight, visits == 0, position_id != frame.current_focus, -visits)

        # Selection reads use scopes, never the technical creators hidden in diagnostic traces.
        candidate = max(sorted(eligible, key=lambda c: c["position_id"]), key=relevance)
        expression_ids = sorted(
            {
                use["expression_id"]
                for use in candidate.get("active_uses", [])
                if use.get("expression_id") in shared_ids
            }
        )
        previous = next(
            (
                segment
                for segment in reversed(frame.previous_segments)
                if set(segment.get("expression_ids", [])).intersection(expression_ids)
            ),
            None,
        )
        text = str(candidate["text"])
        # If the chosen view uses several shared carriers, the referenced carriers also
        # enter its public surface; private backgrounds and technical origins never do.
        remaining_values = [
            value
            for use in candidate.get("active_uses", [])
            if use.get("expression_id") in expression_ids
            for value in use.get("values", [])
            if str(value) not in text
        ]
        if remaining_values:
            text += "她又想起这些尚未结束的经历：" + _glimpse(remaining_values, "", budget=1000) + "。"
        if previous:
            text = "先前那一段生活再次被想起，却已经不能照原样理解下去。" + text
        presentation = Presentation(
            text=text,
            focus_position_id=candidate["position_id"],
            expression_ids=expression_ids,
            reinterprets=[previous["id"]] if previous else [],
        )
        validate_presentation(frame, presentation, legacy_letter=True)
        return ModelResult(presentation)
