"""Tool ABC and helpers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ToolError(Exception):
    """Tool failed in a way the LLM should hear about (user-friendly message)."""


@dataclass
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


class Tool(ABC):
    name: str
    description: str
    input_schema: dict

    @abstractmethod
    async def execute(self, args: dict) -> str:
        """Run tool. Return short string on success. Raise ToolError on user-friendly failure."""


def tool_to_anthropic(tool: Tool) -> dict:
    """Convert a Tool into the dict shape Anthropic expects in `tools=[...]`."""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }
