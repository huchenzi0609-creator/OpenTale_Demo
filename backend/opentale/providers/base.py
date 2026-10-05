"""Provider boundary: narrative values and safe errors, never transport details."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from opentale.domain import GenerationFrame, Presentation, Proposal, ReadingFrame

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ModelResult(Generic[T]):
    value: T
    input_tokens: int = 0
    output_tokens: int = 0


class ProviderError(Exception):
    """Safe to persist or display; underlying HTTP data must never be attached."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)

    def public(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class Provider(Protocol):
    async def propose(self, frame: GenerationFrame) -> ModelResult[Proposal]: ...

    async def realize(self, frame: GenerationFrame) -> ModelResult[str]: ...

    async def present(self, frame: ReadingFrame) -> ModelResult[Presentation]: ...
