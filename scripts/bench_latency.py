#!/usr/bin/env python3
"""Controlled latency benchmark for the pipeline's network stages.

Why: production ``[latency]`` log lines mix clean single-user turns with
multi-party queue waits and arbiter rounds, so their aggregates overstate
what the pipeline itself costs. This script measures each network stage in
isolation under production config — real persona system prompt, tools
schema, prompt cache on, s2-pro low-latency TTS — one turn at a time.

Stages measured (N runs each, p50/p90):

  llm_ttft   request → first Claude text delta
  llm_ttfs   request → first complete sentence out of SentenceChunker
             (this, not TTFT, is what TTS actually waits for)
  tts_open   Fish WS handshake + voice bind (the cost that the pre-opened
             socket optimization removes from the hot path)
  tts_ttfa   first-sentence push → first Opus packet, on a pre-opened
             socket (the production path)

It ends with an honest mouth-to-ear composition:

  endpoint_silence (config) + asr_tail (local, tight) + llm_ttfs
  + tts_ttfa + Discord playout

Usage:
  .venv/bin/python scripts/bench_latency.py [--runs 8] [--persona yidiandian]

Needs ANTHROPIC_API_KEY and FISH_AUDIO_API_KEY in .env; voice id is taken
from the persona file (override with TEST_VOICE_ID).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from pathlib import Path

import frontmatter
import yaml
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent

import sys  # noqa: E402

sys.path.insert(0, str(REPO / "src"))

from echotwin.providers.llm.base import TextDelta  # noqa: E402
from echotwin.providers.llm.claude_haiku import ClaudeHaikuProvider  # noqa: E402
from echotwin.providers.tts.fish_audio_stream import (  # noqa: E402
    FishAudioStreamProvider,
    FishConfig,
)
from echotwin.tools.get_date import GetDate  # noqa: E402
from echotwin.tools.get_time import GetTime  # noqa: E402
from echotwin.tools.get_weather import GetWeather  # noqa: E402
from echotwin.tools.registry import ToolRegistry  # noqa: E402
from echotwin.utils.sentence_chunker import SentenceChunker  # noqa: E402

# Rotating user questions — short, tool-free, conversational (the common case).
QUESTIONS = [
    "你今天过得怎么样呀?",
    "给我讲个好玩的事呗。",
    "你觉得夏天最适合做什么?",
    "最近有什么让你开心的事吗?",
    "你喜欢下雨天还是晴天?",
    "周末你会想干嘛?",
    "你最喜欢吃什么呀?",
    "跟我说说你自己吧。",
]

# Fixed short history so every run pays the same context cost as a real
# mid-conversation turn (production keeps rolling history per guild).
HISTORY = [
    {"role": "user", "content": "嗨,你在吗?"},
    {"role": "assistant", "content": "在呀在呀,我一直都在哦。"},
    {"role": "user", "content": "刚刚在干嘛?"},
    {"role": "assistant", "content": "在等你跟我说话呀,哈哈。"},
]

# Typical first sentence of a reply — what a turn actually pushes to TTS first.
TTS_SENTENCE = "哎呀,这个问题问得好,让我想一想哦。"


def _percentile(vals: list[float], p: float) -> float:
    vs = sorted(vals)
    return vs[min(int(len(vs) * p), len(vs) - 1)]


def _fmt(name: str, vals: list[float]) -> str:
    return (
        f"  {name:10s} n={len(vals):2d}  p50={_percentile(vals, 0.5):6.0f}ms"
        f"  p90={_percentile(vals, 0.9):6.0f}ms"
        f"  min={min(vals):6.0f}ms  max={max(vals):6.0f}ms"
    )


def _build_system_prompt(persona_id: str) -> str:
    """base_template + persona body — same composition as persona.py."""
    post = frontmatter.load(REPO / "prompts" / "personas" / f"{persona_id}.md")
    lang = post.metadata.get("language", "zh")
    tpl_name = "base_template.md" if lang == "zh" else f"base_template.{lang}.md"
    tpl_path = REPO / "prompts" / tpl_name
    if not tpl_path.exists():
        tpl_path = REPO / "prompts" / "base_template.md"
    return tpl_path.read_text() + "\n\n" + post.content


def _voice_id(persona_id: str) -> str:
    env = os.environ.get("TEST_VOICE_ID")
    if env:
        return env
    post = frontmatter.load(REPO / "prompts" / "personas" / f"{persona_id}.md")
    vid = str(post.metadata.get("voice_id", ""))
    if not vid or "REPLACE" in vid:
        raise SystemExit(f"persona {persona_id} has no usable voice_id; set TEST_VOICE_ID")
    return vid


async def bench_llm(runs: int, persona_id: str) -> dict[str, list[float]]:
    system = _build_system_prompt(persona_id)
    registry = ToolRegistry()
    registry.register(GetTime(default_timezone="Asia/Taipei"))
    registry.register(GetDate(default_timezone="Asia/Taipei"))
    registry.register(GetWeather(default_city="台北"))
    tools = registry.to_anthropic_tools()

    llm = ClaudeHaikuProvider(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model="claude-haiku-4-5",
        max_tokens=150,
        temperature=0.7,
        enable_prompt_cache=True,
    )

    ttft: list[float] = []
    ttfs: list[float] = []
    for i in range(runs):
        chunker = SentenceChunker()
        messages = HISTORY + [{"role": "user", "content": QUESTIONS[i % len(QUESTIONS)]}]
        t0 = time.perf_counter()
        first_delta = first_sentence = None
        async for ev in llm.stream_chat(system, messages, tools=tools):
            if isinstance(ev, TextDelta):
                if first_delta is None:
                    first_delta = time.perf_counter()
                if first_sentence is None and chunker.feed(ev.text):
                    first_sentence = time.perf_counter()
        if first_delta:
            ttft.append((first_delta - t0) * 1000)
        # A short reply may end before the chunker sees a terminator; the
        # whole reply is then the "first sentence".
        if first_sentence is None:
            first_sentence = time.perf_counter()
        ttfs.append((first_sentence - t0) * 1000)
        tag = "cache MISS" if i == 0 else "cache hit"
        print(
            f"  llm run {i + 1}/{runs} ({tag}): ttft={ttft[-1]:.0f}ms ttfs={ttfs[-1]:.0f}ms"
        )
        await asyncio.sleep(0.5)
    return {"ttft": ttft, "ttfs": ttfs}


async def bench_tts(runs: int, voice_id: str) -> dict[str, list[float]]:
    open_ms: list[float] = []
    ttfa: list[float] = []
    for i in range(runs):
        tts = FishAudioStreamProvider(
            FishConfig(
                api_key=os.environ["FISH_AUDIO_API_KEY"],
                voice_id=voice_id,
                model="s2-pro",
                latency="low",
            )
        )
        t0 = time.perf_counter()
        await tts.open()
        open_ms.append((time.perf_counter() - t0) * 1000)

        first_pkt = asyncio.get_event_loop().create_future()

        async def drain() -> None:
            async for pkt in tts.packets():
                if pkt and not first_pkt.done():
                    first_pkt.set_result(time.perf_counter())

        drain_task = asyncio.create_task(drain())
        t1 = time.perf_counter()
        await tts.push_text(TTS_SENTENCE)
        await tts.flush()
        await tts.end_turn()
        t_first = await asyncio.wait_for(first_pkt, timeout=10)
        ttfa.append((t_first - t1) * 1000)
        drain_task.cancel()
        await tts.close()
        print(f"  tts run {i + 1}/{runs}: open={open_ms[-1]:.0f}ms ttfa={ttfa[-1]:.0f}ms")
        await asyncio.sleep(0.5)
    return {"open": open_ms, "ttfa": ttfa}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=8)
    ap.add_argument("--persona", default=None, help="persona id (default: config.yaml active_persona)")
    args = ap.parse_args()

    load_dotenv(REPO / ".env")
    cfg = yaml.safe_load((REPO / "config.yaml").read_text())
    persona_id = args.persona or cfg["bot"]["active_persona"]
    endpoint_silence = cfg["bot"].get("endpoint_silence_ms", 500)
    tick = cfg["bot"].get("endpoint_tick_ms", 100)
    voice_id = _voice_id(persona_id)

    print(f"persona={persona_id} model=claude-haiku-4-5 tts=s2-pro/low runs={args.runs}\n")

    print("[1/2] LLM (persona prompt + tools + prompt cache)")
    llm_stats = await bench_llm(args.runs, persona_id)
    print("\n[2/2] Fish Audio TTS (streaming WS)")
    tts_stats = await bench_tts(args.runs, voice_id)

    print("\n== stage stats ==")
    print(_fmt("llm_ttft", llm_stats["ttft"]))
    print(_fmt("llm_ttfs", llm_stats["ttfs"]))
    print(_fmt("tts_open", tts_stats["open"]))
    print(_fmt("tts_ttfa", tts_stats["ttfa"]))

    # Cache-hit-only TTFS (run 1 pays the prompt-cache write).
    ttfs_hit = llm_stats["ttfs"][1:] or llm_stats["ttfs"]
    llm_p50 = _percentile(ttfs_hit, 0.5)
    tts_p50 = _percentile(tts_stats["ttfa"], 0.5)
    asr_tail = 19  # p50 over 84 production turns; local compute, 9–33ms spread
    playout = 40  # Discord 20ms frame cadence + jitter buffer, estimate
    endpoint = endpoint_silence + tick // 2

    print("\n== honest mouth-to-ear composition (p50, single user, no queue) ==")
    print(f"  VAD endpoint silence   {endpoint:5d}ms   (config: {endpoint_silence}ms + tick/2)")
    print(f"  streaming ASR tail     {asr_tail:5d}ms   (from production logs, local compute)")
    print(f"  LLM first sentence     {llm_p50:5.0f}ms   (measured, cache-hit runs)")
    print(f"  Fish TTS first audio   {tts_p50:5.0f}ms   (measured, pre-opened socket)")
    print(f"  Discord playout        {playout:5d}ms   (estimate)")
    total = endpoint + asr_tail + llm_p50 + tts_p50 + playout
    print(f"  {'TOTAL':22s} {total:5.0f}ms")
    print(
        f"\n  ex-endpoint (endpoint→ear): {total - endpoint:.0f}ms — "
        "comparable to the [latency] log totals + playout"
    )


if __name__ == "__main__":
    asyncio.run(main())
