"""Groq (OpenAI-compatible) LLM provider — lightweight client dedicated to arbitration.

Non-streaming single shot (25-token output @600+ TPS, streaming is pointless);
direct aiohttp calls without pulling in an SDK. Implements the same
stream_chat interface (TextDelta + MessageEnd) so it's interchangeable with
arbitrate()/ClaudeHaikuProvider. No tool use support (arbitration doesn't need it).
"""
from __future__ import annotations

from typing import AsyncIterator

import aiohttp

from .base import LLMEvent, MessageEnd, TextDelta

_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqProvider:
    def __init__(
        self,
        api_key: str,
        model: str = "qwen/qwen3-32b",
        max_tokens: int = 100,
        temperature: float = 0.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def _post(self, body: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # Groq sits behind Cloudflare; the default python UA gets 403'd
            "User-Agent": "echotwin/1.0",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _URL, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RuntimeError(f"Groq HTTP {resp.status}: {data}")
                return data

    async def stream_chat(
        self, system: str, messages: list[dict], tools=None
    ) -> AsyncIterator[LLMEvent]:
        body = {
            "model": self.model,
            "temperature": self._temperature,
            "max_completion_tokens": self._max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if "qwen" in self.model.lower():
            body["reasoning_effort"] = "none"  # disable thinking, otherwise it emits <think> and drags latency
        data = await self._post(body)
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        yield TextDelta(text=text)
        yield MessageEnd(
            stop_reason="end_turn",
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )
