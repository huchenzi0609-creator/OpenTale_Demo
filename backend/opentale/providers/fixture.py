"""Offline evaluator exercising references and joint updates, not an LLM simulation."""

from __future__ import annotations

from collections import defaultdict

from opentale.domain import GenerationFrame, Operation, Presentation, Proposal, ReadingFrame

from .base import ModelResult, ProviderError


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


class FixtureProvider:
    """Deterministic fixtures intentionally expose the causal dependency in surface text."""

    async def propose(self, frame: GenerationFrame) -> ModelResult[Proposal]:
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

    async def realize(self, frame: GenerationFrame) -> ModelResult[str]:
        shared = _shared_use(frame)
        values = [str(value) for value in shared.get("values", [])]
        live_content = "。".join(values) if values else str(shared.get("text", ""))[:600]
        # This reads all active scoped bindings: removing one source changes this very prose.
        surface = f"{frame.name}停在这一段生活里。{live_content}。这次的停顿，使先前的记忆又取得了另一种意味。"
        return ModelResult(surface)

    async def present(self, frame: ReadingFrame) -> ModelResult[Presentation]:
        if not frame.candidates:
            raise ProviderError("FIXTURE_FRAME_INVALID", "还没有可以承接的叙述。")
        participants: dict[str, set[str]] = defaultdict(set)
        for candidate in frame.candidates:
            for use in candidate.get("active_uses", []):
                participants[use["expression_id"]].add(candidate["position_id"])
        shared_ids = {expression_id for expression_id, positions in participants.items() if len(positions) >= 2}
        # Compatibility for the old single-candidate fixture test, not custom-preset selection.
        if not shared_ids and len(frame.candidates) == 1 and "e-letter" in participants:
            shared_ids.add("e-letter")
        eligible = [
            candidate for candidate in frame.candidates
            if shared_ids.intersection(use["expression_id"] for use in candidate.get("active_uses", []))
        ]
        if not eligible:
            raise ProviderError("FIXTURE_FRAME_INVALID", "还没有可以共同承接的有效表达。")

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
        return ModelResult(
            Presentation(
                text=text,
                focus_position_id=candidate["position_id"],
                expression_ids=expression_ids,
                reinterprets=[previous["id"]] if previous else [],
            )
        )
