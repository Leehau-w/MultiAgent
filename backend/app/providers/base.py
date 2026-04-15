from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal


@dataclass
class ProviderMessage:
    """Unified message yielded by all provider adapters."""

    type: Literal["text", "tool_use", "tool_result", "result", "error", "usage"]
    content: str = ""
    tool_name: str | None = None
    tool_input: dict | None = None
    # Set on the final "result" message
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None


class ProviderAdapter(ABC):
    """Abstract base for all LLM provider adapters."""

    @abstractmethod
    async def run(
        self,
        *,
        prompt: str,
        system_prompt: str,
        model: str,
        tools: list[str],
        cwd: str,
        max_turns: int,
        session_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderMessage]:
        """Execute an agentic loop, yielding ProviderMessages as work happens.

        Must yield at least one message with type="result" at the end.
        """
        ...  # pragma: no cover
        # Make this an async generator so subclasses can use `yield`
        if False:
            yield  # type: ignore[misc]
