"""Offline evaluator exercising references and joint updates, not an LLM simulation."""

from __future__ import annotations

from opentale.domain import GenerationFrame, Operation, Presentation, Proposal, ReadingFrame

from .base import ModelResult, ProviderError


def _shared_use(frame: GenerationFrame) -> dict:
    shared = next((use for use in frame.active_uses if use.get("expression_id") == "e-letter"), None)
    if shared is None:
        raise ProviderError("FIXTURE_FRAME_INVALID", "离线示例需要共享的书信表达。")
    return shared


class FixtureProvider:
    """Deterministic fixtures intentionally expose the causal dependency in surface text."""

    async def propose(self, frame: GenerationFrame) -> ModelResult[Proposal]:
        shared = _shared_use(frame)
        peers = [peer for peer in frame.peers if peer.get("id") != frame.position_id]
        if not peers:
            raise ProviderError("FIXTURE_FRAME_INVALID", "离线示例需要至少一位承接者。")
        corridor = any(word in str(shared.get("text", "")) for word in ("门", "脚步", "灯"))
        carrier = "门外的声音" if corridor else "那封信"
        gestures = (
            (
                "把灯留着，停在那扇尚未打开的门前",
                "听见回廊里的脚步，却迟迟没有走过去开门",
                "把先前的等待当作某个人即将归来的脚步",
                "认出门外的声音，却没有立刻叫出它的主人",
            )
            if corridor
            else (
                "把那封信折好，放在仍然空着的椅子上",
                "停在信的第一行，等待一个尚未说出的名字",
                "把已经读过的话当作即将写出的第一句",
                "认出那封信里的声音，却没有立刻叫出它的主人",
            )
        )
        prior_values = shared.get("values", [])
        # A bounded glimpse of the live frame keeps the second window dependent on the first.
        remembered = str(prior_values[0])[:90] if prior_values else str(shared.get("text", ""))[:90]
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
        text = f"{frame.name}重新想到{carrier}。" + "。".join(anchors) + "。接下来，这一段生活还将由谁承担……"
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
        values = [str(value)[:420] for value in shared.get("values", [])]
        live_content = "。".join(values) if values else str(shared.get("text", ""))[:600]
        # This reads all active scoped bindings: removing one source changes this very prose.
        surface = f"{frame.name}停在这一段生活里。{live_content}。这次的停顿，使先前的记忆又取得了另一种意味。"
        return ModelResult(surface)

    async def present(self, frame: ReadingFrame) -> ModelResult[Presentation]:
        if not frame.candidates:
            raise ProviderError("FIXTURE_FRAME_INVALID", "还没有可以承接的叙述。")
        def relevance(candidate: dict) -> tuple[int, bool, bool, int]:
            position_id = candidate["position_id"]
            shared_ids = {use.get("expression_id") for use in candidate.get("active_uses", [])}
            relation_weight = 0
            for relation in frame.open_relations:
                if relation.get("expression_id") not in shared_ids:
                    continue
                participants = relation.get("position_ids", [])
                target = relation.get("position_id")
                if target == position_id:
                    relation_weight += 3
                elif position_id in participants:
                    relation_weight += 1
                    if frame.current_focus in participants and position_id != frame.current_focus:
                        relation_weight += 4
            previous_focuses = [segment.get("focus_position_id") for segment in frame.previous_segments]
            visits = previous_focuses.count(position_id)
            return (relation_weight, visits == 0, position_id != frame.current_focus, -visits)

        # Selection reads use scopes, never the technical creators hidden in diagnostic traces.
        candidate = max(sorted(frame.candidates, key=lambda c: c["position_id"]), key=relevance)
        expression_ids = sorted(
            {
                use["expression_id"]
                for use in candidate.get("active_uses", [])
                if use.get("expression_id") == "e-letter"
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
