"""Claude Haiku 4.5 LLM provider with prompt caching + tool use streaming."""
from __future__ import annotations

from typing import AsyncIterator

from anthropic import AsyncAnthropic
from loguru import logger

from .base import (
    LLMEvent,
    LLMProvider,
    MessageEnd,
    TextDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)


class ClaudeHaikuProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 300,
        temperature: float = 0.7,
        enable_prompt_cache: bool = True,
    ):
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._temp = temperature
        self._cache = enable_prompt_cache

    async def stream_chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        sys_block: list[dict] = [{"type": "text", "text": system}]
        if self._cache:
            sys_block[0]["cache_control"] = {"type": "ephemeral"}

        msgs = list(messages)
        if self._cache and msgs:
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "assistant" and i < len(msgs) - 1:
                    content = msgs[i].get("content")
                    if isinstance(content, str):
                        msgs[i] = {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": content,
                                    "cache_control": {"type": "ephemeral"},
                                }
                            ],
                        }
                    elif isinstance(content, list) and content:
                        # Tool-use rounds: assistant content is a block list.
                        # Rebuild with cache_control on the LAST block — and
                        # copy the dicts, they're shared with session history.
                        blocks = [dict(b) for b in content]
                        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
                        msgs[i] = {"role": "assistant", "content": blocks}
                    break

        kwargs: dict = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temp,
            system=sys_block,
            messages=msgs,
        )
        if tools:
            kwargs["tools"] = tools

        # Track current content_block index → kind, so we can emit ToolUseStart/End
        # when input_json_delta or content_block_stop arrive.
        block_kinds: dict[int, str] = {}
        block_ids: dict[int, str] = {}
        final_stop_reason: str = "end_turn"
        usage_input = 0
        usage_output = 0
        usage_cache_write = 0
        usage_cache_read = 0

        async with self._client.messages.stream(**kwargs) as stream:
            async for ev in stream:
                t = getattr(ev, "type", None)
                if t == "message_start":
                    # message_start carries the input-side usage
                    u = getattr(getattr(ev, "message", None), "usage", None)
                    if u is not None:
                        usage_input = getattr(u, "input_tokens", 0) or 0
                        usage_output = getattr(u, "output_tokens", 0) or 0
                        usage_cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
                        usage_cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
                elif t == "content_block_start":
                    idx = getattr(ev, "index", 0)
                    block = getattr(ev, "content_block", None)
                    btype = getattr(block, "type", None) if block else None
                    block_kinds[idx] = btype or ""
                    if btype == "tool_use":
                        tu_id = getattr(block, "id", "")
                        tu_name = getattr(block, "name", "")
                        block_ids[idx] = tu_id
                        yield ToolUseStart(tool_use_id=tu_id, name=tu_name)
                elif t == "content_block_delta":
                    idx = getattr(ev, "index", 0)
                    d = getattr(ev, "delta", None)
                    dtype = getattr(d, "type", None) if d else None
                    if dtype == "text_delta":
                        text = getattr(d, "text", "") or ""
                        if text:
                            yield TextDelta(text=text)
                    elif dtype == "input_json_delta":
                        partial = getattr(d, "partial_json", "") or ""
                        tu_id = block_ids.get(idx, "")
                        yield ToolUseInputDelta(tool_use_id=tu_id, partial_json=partial)
                elif t == "content_block_stop":
                    idx = getattr(ev, "index", 0)
                    if block_kinds.get(idx) == "tool_use":
                        yield ToolUseEnd(tool_use_id=block_ids.get(idx, ""))
                elif t == "message_delta":
                    # message_delta carries the final stop_reason in delta.stop_reason
                    # and the cumulative output token count in usage.output_tokens
                    d = getattr(ev, "delta", None)
                    sr = getattr(d, "stop_reason", None) if d else None
                    if sr:
                        final_stop_reason = sr
                    u = getattr(ev, "usage", None)
                    out = getattr(u, "output_tokens", None) if u else None
                    if out:
                        usage_output = out
                elif t == "message_stop":
                    yield MessageEnd(
                        stop_reason=final_stop_reason,
                        input_tokens=usage_input,
                        output_tokens=usage_output,
                        cache_creation_input_tokens=usage_cache_write,
                        cache_read_input_tokens=usage_cache_read,
                    )
                # other event types (message_start, ping) ignored
