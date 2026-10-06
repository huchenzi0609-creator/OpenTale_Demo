"""Provider boundary: narrative values and safe errors, never transport details."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from opentale.domain import GenerationFrame, Presentation, Proposal, ReadingFrame

from .feedback import OutputFeedback

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ModelResult(Generic[T]):
    value: T
    input_tokens: int = 0
    output_tokens: int = 0


class ProviderError(Exception):
    """Safe to persist or display; underlying HTTP data must never be attached."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        *,
        usage: tuple[int, int] | None = None,
        feedback: OutputFeedback | None = None,
    ) -> None:
        if usage is not None and (
            not isinstance(usage, tuple)
            or len(usage) != 2
            or any(type(count) is not int or not 0 <= count <= 5_000_000 for count in usage)
        ):
            raise ValueError("Invalid provider usage accounting")
        self.code = code
        self.message = message
        self.retryable = retryable
        # Exact, validated token counts can settle a rejected but billed response.
        # This is local accounting metadata, never raw model output or public diagnostics.
        self.usage = usage
        if feedback is not None and not isinstance(feedback, OutputFeedback):
            raise ValueError("Invalid safe output feedback")
        self.feedback = feedback
        super().__init__(message)

    def public(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class Provider(Protocol):
    async def propose(
        self, frame: GenerationFrame, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[Proposal]: ...

    async def realize(
        self, frame: GenerationFrame, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[str]: ...

    async def present(
        self, frame: ReadingFrame, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[Presentation]: ...
