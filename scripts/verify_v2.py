"""Run isolated fixture windows and inspect v2 execution evidence without model API access."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from opentale.config import Settings
from opentale.domain import Limits
from opentale.kernel import semantic_projection
from opentale.presets import initialize, read_presets
from opentale.replay import verify_replay
from opentale.request_evidence import verify_request_evidence
from opentale.runner import Runner
from opentale.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class VerificationFailure(Exception):
    """Only fixed, non-narrative failure labels are printed."""


def require(condition: bool, label: str) -> None:
    if not condition:
        raise VerificationFailure(label)


async def verify(windows: int, directory: Path) -> dict:
    # Settings() is the plain defaults model. It does not load .env.local or process credentials.
    settings = Settings()
    preset = next(item for item in read_presets(ROOT) if item["id"] == "letters")
    state = initialize(preset, "fixture", Limits(
        max_calls=None, max_tokens=None, max_parallel=3, max_windows=None,
    ), "reflexive_v2")
    store = Store(directory)
    store.save(state)
    runner = Runner(store, settings)
    replay_checks = []
    request_checks = []
    try:
        for completed in range(1, windows + 1):
            require(state.run.provider == "fixture", "fixture_provider_required")
            state = await runner.step(state.run.id)
            require(state.pending is None and state.run.revision == completed, "window_not_completed")
            replay_checks.append(verify_replay(state, store.read_events(state.run.id))["ok"])
            request_checks.append(verify_request_evidence(state))
            require(replay_checks[-1], "semantic_replay_mismatch")
            require(request_checks[-1], "request_evidence_mismatch")
            restored = store.load(state.run.id)
            require(semantic_projection(restored) == semantic_projection(state), "checkpoint_mismatch")
            require(verify_request_evidence(restored), "restored_request_evidence_mismatch")
        require(state.schema_version == 5 and state.reflection is not None, "v2_schema_missing")
        reflection = state.reflection
        jobs_per_window = 2 * len(state.positions) + sum(track.enabled for track in state.reading_tracks)
        require(state.run.usage.calls == windows * jobs_per_window, "unexpected_fixture_call_count")
        require(len(state.requests) == state.run.usage.calls, "request_count_mismatch")
        require(all(request.accepted_result_hash for request in state.requests), "accepted_request_missing")
        require(state.run.usage.input_tokens == state.run.usage.output_tokens == 0, "fixture_token_usage")
        require(bool(reflection.views) and bool(reflection.effects), "view_effect_records_missing")
        require(any(method.language_version == 2 for method in reflection.methods), "view_program_missing")
        response_dependencies = sum(bool(effect.response_ids) for effect in reflection.effects)
        if windows >= 2:
            require(response_dependencies > 0, "response_not_used_as_input")
        return {
            "ok": True,
            "provider": "fixture",
            "schema_version": state.schema_version,
            "completed_windows": state.run.usage.windows,
            "positions": len(state.positions),
            "reading_tracks": len(state.reading_tracks),
            "calls": state.run.usage.calls,
            "input_tokens": state.run.usage.input_tokens,
            "output_tokens": state.run.usage.output_tokens,
            "methods": len(reflection.methods),
            "choices": len(reflection.choices),
            "views": len(reflection.views),
            "responses": len(reflection.responses),
            "effects": len(reflection.effects),
            "effects_using_responses": response_dependencies,
            "object_versions": len(reflection.object_versions),
            "requests": len(state.requests),
            "replay_passed_windows": sum(replay_checks),
            "request_evidence_passed_windows": sum(request_checks),
            "scheduler_mode": state.schedule.mode if state.schedule else None,
            "note": "Offline execution evidence only; no claim about live-model intention or literary quality.",
        }
    finally:
        await runner.shutdown()


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("windows must be positive")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=positive_int, default=3,
                        help="Completed local fixture windows (default: 3); never uses a model API.")
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="opentale-v2-verification-") as directory:
            result = asyncio.run(verify(args.windows, Path(directory)))
    except VerificationFailure as error:
        print(json.dumps({"ok": False, "check": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from None
    except Exception:
        # No raw exception strings, model bodies, material text, or private paths in terminal output.
        print(json.dumps({"ok": False, "check": "isolated_v2_execution_failed"}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
