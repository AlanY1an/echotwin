from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Union


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolUseStart:
    tool_use_id: str
    name: str


@dataclass
class ToolUseInputDelta:
    tool_use_id: str
    partial_json: str


@dataclass
class ToolUseEnd:
    tool_use_id: str


@dataclass
class MessageEnd:
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens" | "stop_sequence" | ...
    # Token usage for this message (fed into the cost tracker). Zero when the
    # provider doesn't report usage.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


LLMEvent = Union[TextDelta, ToolUseStart, ToolUseInputDelta, ToolUseEnd, MessageEnd]


class LLMProvider(ABC):
    @abstractmethod
    def stream_chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Yield LLM events. When `tools` is None or empty, only TextDelta + MessageEnd events appear."""


async def stream_text_only(
    provider: LLMProvider,
    system: str,
    messages: list[dict],
) -> AsyncIterator[str]:
    """Legacy callers that only need text — adapter that yields plain str deltas."""
    async for ev in provider.stream_chat(system, messages, tools=None):
        if isinstance(ev, TextDelta):
            yield ev.text
