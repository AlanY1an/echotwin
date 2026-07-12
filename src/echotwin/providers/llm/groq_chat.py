"""GroqChatProvider — full conversation brain on Groq's OpenAI-compatible API.

Drop-in replacement for ClaudeHaikuProvider: same stream_chat() event
contract (TextDelta / ToolUseStart / ToolUseInputDelta / ToolUseEnd /
MessageEnd), same Anthropic-style inputs. Internally converts:

- tools:    Anthropic {name, description, input_schema}
            → OpenAI {type: "function", function: {..., parameters}}
- messages: Anthropic content blocks (tool_use / tool_result)
            → OpenAI assistant.tool_calls / role="tool" messages
- stream:   OpenAI SSE deltas → our event dataclasses

Unlike the arbiter's GroqProvider (single-shot, no tools), this one streams
and supports the full tool loop. No prompt caching — Groq has no
cache_control; the small/fast models make it a latency win regardless.
"""
from __future__ import annotations

import json
import re
from typing import AsyncIterator

import aiohttp
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

_DEFAULT_URL = "https://api.groq.com/openai/v1/chat/completions"

_STOP_REASON = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


def _tools_to_openai(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object"}),
            },
        }
        for t in tools
    ]


def _messages_to_openai(system: str, messages: list[dict]) -> list[dict]:
    """Anthropic-style history → OpenAI chat messages."""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        # Block list: split into text / tool_use (assistant) / tool_result (user)
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []
        for block in content or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    }
                )
            elif btype == "tool_result":
                rc = block.get("content", "")
                if not isinstance(rc, str):
                    rc = json.dumps(rc, ensure_ascii=False)
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": rc,
                    }
                )
        if role == "assistant":
            msg: dict = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        elif tool_results:
            out.extend(tool_results)
            # Any stray text alongside tool_results becomes a user message
            if text_parts:
                out.append({"role": "user", "content": "".join(text_parts)})
        else:
            out.append({"role": role, "content": "".join(text_parts)})
    return out


class GroqChatProvider(LLMProvider):
    cost_prefix = "groq_qwen3_32b"  # overridden per-model in __init__

    def __init__(
        self,
        api_key: str,
        model: str = "qwen/qwen3-32b",
        max_tokens: int = 300,
        temperature: float = 0.7,
        base_url: str = _DEFAULT_URL,
        reasoning_effort: str | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        # Any OpenAI-compatible /chat/completions endpoint works (Groq,
        # Cerebras, …) — same streaming shape, same tool_calls deltas.
        self._url = base_url
        self._reasoning_effort = reasoning_effort
        # groq_qwen3_32b-style key for the pricing table
        self.cost_prefix = "groq_" + model.split("/")[-1].replace("-", "_").replace(".", "_")

    async def stream_chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        body: dict = {
            "model": self.model,
            "temperature": self._temperature,
            "max_completion_tokens": self._max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": _messages_to_openai(system, messages),
        }
        oai_tools = _tools_to_openai(tools)
        if oai_tools:
            body["tools"] = oai_tools
        if self._reasoning_effort:
            body["reasoning_effort"] = self._reasoning_effort
        elif "qwen" in self.model.lower():
            body["reasoning_effort"] = "none"  # disable thinking — latency killer

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "echotwin/1.0",  # default python UA gets 403'd by Cloudflare
        }

        # Track streamed tool calls by OpenAI's positional index
        open_tools: dict[int, str] = {}  # index → tool_use_id
        finish_reason = "stop"
        usage: dict = {}

        # Retry loop covers two transient failure modes:
        # - 429 rate limits (free-tier RPM/TPM): wait per the body's
        #   "try again in Ns" hint, then re-request.
        # - Empty completions: gpt-oss occasionally finishes with zero content
        #   and no tool calls. Nothing has been yielded yet in that case, so a
        #   silent re-request (~200ms on Cerebras) is invisible to the user.
        import asyncio
        max_retries = 4
        empty_retries = 2
        async with aiohttp.ClientSession() as http:
            for attempt in range(max_retries + 1):
                emitted = False
                async with http.post(
                    self._url, json=body, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60, sock_connect=10),
                ) as resp:
                    if resp.status == 429 and attempt < max_retries:
                        body_txt = await resp.text()
                        delay = 1.5 * (attempt + 1)
                        m = re.search(r"try again in ([\d.]+)s", body_txt)
                        if m:
                            delay = float(m.group(1)) + 0.3
                        logger.warning(
                            f"[groq_chat] 429 rate-limited, retry {attempt + 1}/{max_retries} in {delay:.1f}s"
                        )
                        await asyncio.sleep(min(delay, 8.0))
                        continue
                    if resp.status != 200:
                        raise RuntimeError(f"Groq HTTP {resp.status}: {await resp.text()}")
                    async for raw in resp.content:
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload == "[DONE]":
                            break
                        chunk = json.loads(payload)
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            emitted = True
                            yield TextDelta(text=delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            emitted = True
                            idx = tc.get("index", 0)
                            if idx not in open_tools:
                                tool_id = tc.get("id") or f"call_{idx}"
                                open_tools[idx] = tool_id
                                yield ToolUseStart(
                                    tool_use_id=tool_id,
                                    name=(tc.get("function") or {}).get("name", ""),
                                )
                            args = (tc.get("function") or {}).get("arguments")
                            if args:
                                yield ToolUseInputDelta(
                                    tool_use_id=open_tools[idx], partial_json=args
                                )
                if not emitted and attempt < empty_retries:
                    logger.warning(
                        f"[groq_chat] empty completion — retrying ({attempt + 1}/{empty_retries})"
                    )
                    continue
                break  # streamed successfully (or retries exhausted)

        for tool_id in open_tools.values():
            yield ToolUseEnd(tool_use_id=tool_id)

        yield MessageEnd(
            stop_reason=_STOP_REASON.get(finish_reason, finish_reason),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )
