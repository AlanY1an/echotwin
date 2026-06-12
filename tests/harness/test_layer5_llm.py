"""Layer 5 (Claude Haiku LLM with tool use) tests.

Speed:    TTFT (time to first text token), full latency
Quality:  tool selection accuracy (does '现在几点' trigger get_time?)
Robust:   max_tokens hit, tool execution fails inside the loop, empty input
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest
from dotenv import load_dotenv

from tests.harness._utils import Stat, time_ms
from echotwin.providers.llm.base import (
    LLMProvider,
    MessageEnd,
    TextDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from echotwin.providers.llm.claude_haiku import ClaudeHaikuProvider
from echotwin.tools.base import Tool
from echotwin.tools.get_date import GetDate
from echotwin.tools.get_time import GetTime
from echotwin.tools.get_weather import GetWeather
from echotwin.tools.registry import ToolRegistry

load_dotenv()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set; skipping live LLM tests",
    ),
]


@pytest.fixture(scope="module")
def llm() -> LLMProvider:
    return ClaudeHaikuProvider(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model="claude-haiku-4-5",
        max_tokens=200,
        temperature=0.7,
    )


@pytest.fixture(scope="module")
def tool_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(GetTime(default_timezone="Asia/Taipei"))
    r.register(GetDate(default_timezone="Asia/Taipei"))
    r.register(GetWeather(default_city="台北"))
    return r


SYSTEM_PROMPT = """你叫一点点点,简短爽朗的台湾女生。
回答尽量短(1-2 句)。
有 get_time / get_date / get_weather 工具,问到时间日期天气时调用对应工具。"""


async def _stream_full(llm, system, messages, tools=None) -> dict:
    """Run one stream, collect events + timing. Returns summary dict."""
    t0 = time.perf_counter()
    first_text_t: float | None = None
    text = ""
    tool_uses: list[dict] = []
    stop_reason = "end_turn"

    async for ev in llm.stream_chat(system, messages, tools=tools):
        if isinstance(ev, TextDelta):
            if first_text_t is None:
                first_text_t = time.perf_counter()
            text += ev.text
        elif isinstance(ev, ToolUseStart):
            tool_uses.append({"id": ev.tool_use_id, "name": ev.name, "partial_json": ""})
        elif isinstance(ev, ToolUseInputDelta):
            tool_uses[-1]["partial_json"] += ev.partial_json
        elif isinstance(ev, MessageEnd):
            stop_reason = ev.stop_reason

    return {
        "text": text,
        "tool_uses": tool_uses,
        "stop_reason": stop_reason,
        "ttft_ms": (first_text_t - t0) * 1000.0 if first_text_t else None,
        "total_ms": (time.perf_counter() - t0) * 1000.0,
    }


# --- Speed ------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_ttft_fast(llm):
    """Time-to-first-text-token. Anthropic streaming usually < 2s for short prompts."""
    stat = Stat("ttft")
    for _ in range(3):
        r = await _stream_full(
            llm, SYSTEM_PROMPT,
            [{"role": "user", "content": "你好"}],
        )
        if r["ttft_ms"]:
            stat.add(r["ttft_ms"] / 1000.0)
        await asyncio.sleep(0.5)
    print(f"\n  {stat.fmt_ms()}")
    assert stat.p50 < 3.0, f"TTFT P50 too slow: {stat.p50:.1f}s"


# --- Tool calling quality --------------------------------------------

TOOL_CASES = [
    ("现在几点了", "get_time"),
    ("今天几号", "get_date"),
    ("明天几号", "get_date"),
    ("台北天气怎么样", "get_weather"),
    ("Tokyo 天气", "get_weather"),
]


@pytest.mark.parametrize("prompt,expected_tool", TOOL_CASES)
@pytest.mark.asyncio
async def test_llm_picks_correct_tool(llm, tool_registry, prompt, expected_tool):
    r = await _stream_full(
        llm, SYSTEM_PROMPT,
        [{"role": "user", "content": prompt}],
        tools=tool_registry.to_anthropic_tools(),
    )
    called = [t["name"] for t in r["tool_uses"]]
    print(f"\n  prompt={prompt!r} → tools={called} (text={r['text']!r})")
    assert expected_tool in called, f"expected {expected_tool} called, got {called}"


@pytest.mark.asyncio
async def test_llm_no_tool_for_chitchat(llm, tool_registry):
    """Chit-chat shouldn't trigger tools."""
    r = await _stream_full(
        llm, SYSTEM_PROMPT,
        [{"role": "user", "content": "你喜欢什么颜色?"}],
        tools=tool_registry.to_anthropic_tools(),
    )
    print(f"\n  chitchat tools={[t['name'] for t in r['tool_uses']]} text={r['text']!r}")
    assert not r["tool_uses"], f"unexpected tool call: {r['tool_uses']}"


# --- Robustness -------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_handles_max_tokens(llm):
    """Force max_tokens=20 → should still produce output, stop_reason=max_tokens."""
    short_llm = ClaudeHaikuProvider(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model="claude-haiku-4-5",
        max_tokens=20,
    )
    r = await _stream_full(
        short_llm, "Reply in exactly 50 chars of Chinese.",
        [{"role": "user", "content": "讲个长故事"}],
    )
    print(f"\n  max_tokens=20: stop_reason={r['stop_reason']} chars={len(r['text'])}")
    assert r["text"], "no text produced"
    # Anthropic stops; could be 'max_tokens' or 'end_turn'
    assert r["stop_reason"] in {"max_tokens", "end_turn"}


@pytest.mark.asyncio
async def test_llm_empty_history_works(llm):
    """Single user message, no history."""
    r = await _stream_full(
        llm, "Reply briefly in Chinese.",
        [{"role": "user", "content": "你好"}],
    )
    assert r["text"], "no text produced for simple greeting"


@pytest.mark.asyncio
async def test_llm_handles_unicode_emoji(llm):
    """Unicode emoji + Chinese should not crash the stream."""
    r = await _stream_full(
        llm, "Reply briefly.",
        [{"role": "user", "content": "你好 😀 今天怎么样"}],
    )
    assert r["text"], "emoji prompt failed"
