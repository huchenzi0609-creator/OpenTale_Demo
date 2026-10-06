"""Shared application capacity bounds; counts, characters and tokens have distinct units.

These are local acceptance ceilings, not claims about a remote model's context or output.
Raising a ceiling does not increase an experiment's saved budget or trigger generation.
"""

DEFAULT_TOKEN_BUDGET = 128_000
MAX_TOKEN_BUDGET = 5_120_000
MAX_CALL_BUDGET = 1_000
MAX_WINDOW_BUDGET = 1_000
MAX_PARALLEL_POSITIONS = 8
MAX_READING_TRACKS = 64
MAX_FORMAT_REPAIRS = 2

MAX_PROPOSAL_OPERATIONS = 256
MAX_PROPOSAL_FRAGMENTS = 128
MAX_FRAGMENT_TARGETS = 256
MAX_PROPOSAL_TEXT_CHARS = 32_000
MAX_OPERATION_TEXT_CHARS = 16_000
MAX_BINDING_TEXT_CHARS = 32_000
MAX_REALIZATION_TEXT_CHARS = 32_000
MAX_PRESENTATION_TEXT_CHARS = 64_000

MAX_MODEL_CONTENT_CHARS = 256_000
MAX_MODEL_REQUEST_CHARS = 512_000
MAX_MODEL_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MODEL_OUTPUT_TOKENS = 32_768


def budget_bounds() -> dict[str, int]:
    return {
        "max_calls": MAX_CALL_BUDGET,
        "max_tokens": MAX_TOKEN_BUDGET,
        "max_parallel": MAX_PARALLEL_POSITIONS,
        "max_windows": MAX_WINDOW_BUDGET,
    }
