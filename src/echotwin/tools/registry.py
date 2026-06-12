"""ToolRegistry — register tools, export Anthropic schema, dispatch executions."""
from __future__ import annotations

from loguru import logger

from .base import Tool, ToolError, tool_to_anthropic


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool
        logger.info(f"[tools] registered {tool.name}")

    def to_anthropic_tools(self) -> list[dict]:
        return [tool_to_anthropic(t) for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def is_empty(self) -> bool:
        return not self._tools

    async def execute(self, name: str, args: dict) -> str:
        tool = self._tools.get(name)
        if tool is None:
            logger.warning(f"[tools] unknown tool {name!r}")
            return f"Error: unknown tool {name!r}"
        try:
            result = await tool.execute(args or {})
            logger.info(f"[tools] {name}({args!r}) → {result!r}")
            return result
        except ToolError as e:
            logger.warning(f"[tools] {name} ToolError: {e}")
            return f"Error: {e}"
        except Exception as e:
            logger.exception(f"[tools] {name} unexpected error")
            return f"Error: tool {name} failed unexpectedly ({type(e).__name__})"
