#!/usr/bin/env python3
"""Compare TTFT / time-to-first-sentence across candidate first-sentence models.

Motivation: the controlled bench (bench_latency.py) shows the LLM first
sentence (~970ms on Haiku) dominates pipeline latency. If a small fast model
could speak the FIRST sentence while Haiku composes the rest, mouth-to-ear
would drop by several hundred ms. This script measures whether that's true —
same persona system prompt, same rolling history, no tools, streaming for
every model, so the numbers are apples-to-apples.

Also prints each model's first sentence so Chinese quality can be eyeballed:
a fast model that opens with clunky Chinese is disqualified regardless of
speed.

Usage:
  .venv/bin/python scripts/bench_llm_models.py [--runs 6] [--persona yidiandian]

Needs ANTHROPIC_API_KEY and GROQ_API_KEY in .env.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
import frontmatter
import yaml
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from echotwin.providers.llm.base import TextDelta  # noqa: E402
from echotwin.providers.llm.claude_haiku import ClaudeHaikuProvider  # noqa: E402
from echotwin.utils.sentence_chunker import SentenceChunker  # noqa: E402

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Same conversational turns as bench_latency.py.
QUESTIONS = [
    "你今天过得怎么样呀?",
    "给我讲个好玩的事呗。",
    "你觉得夏天最适合做什么?",
    "最近有什么让你开心的事吗?",
    "你喜欢下雨天还是晴天?",
    "周末你会想干嘛?",
]
HISTORY = [
    {"role": "user", "content": "嗨,你在吗?"},
    {"role": "assistant", "content": "在呀在呀,我一直都在哦。"},
    {"role": "user", "content": "刚刚在干嘛?"},
    {"role": "assistant", "content": "在等你跟我说话呀,哈哈。"},
]

GROQ_MODELS = [
    "llama-3.1-8b-instant",
    "qwen/qwen3-32b",
    "openai/gpt-oss-20b",
]


def _percentile(vals: list[float], p: float) -> float:
    vs = sorted(vals)
    return vs[min(int(len(vs) * p), len(vs) - 1)]


def _build_system_prompt(persona_id: str) -> str:
    post = frontmatter.load(REPO / "prompts" / "personas" / f"{persona_id}.md")
    lang = post.metadata.get("language", "zh")
    tpl_name = "base_template.md" if lang == "zh" else f"base_template.{lang}.md"
    tpl_path = REPO / "prompts" / tpl_name
    if not tpl_path.exists():
        tpl_path = REPO / "prompts" / "base_template.md"
    return tpl_path.read_text() + "\n\n" + post.content


async def _groq_stream(
    session: aiohttp.ClientSession, model: str, system: str, messages: list[dict]
):
    """Yield content deltas from a streaming Groq chat completion."""
    body = {
        "model": model,
        "temperature": 0.7,
        "max_completion_tokens": 150,
        "stream": True,
        "messages": [{"role": "system", "content": system}, *messages],
    }
    if "qwen" in model.lower():
        body["reasoning_effort"] = "none"
    if "gpt-oss" in model.lower():
        body["reasoning_effort"] = "low"  # gpt-oss rejects "none"
    headers = {
        "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}",
        "Content-Type": "application/json",
        "User-Agent": "echotwin/1.0",
    }
    async with session.post(
        GROQ_URL, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Groq HTTP {resp.status}: {await resp.text()}")
        async for raw in resp.content:
            line = raw.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            delta = chunk["choices"][0].get("delta", {}).get("content")
            if delta:
                yield delta


async def bench_model(name: str, runs: int, system: str) -> dict | None:
    """Return {'ttft': [...], 'ttfs': [...], 'first_sentences': [...]} or None on failure."""
    is_claude = name.startswith("claude")
    if is_claude:
        llm = ClaudeHaikuProvider(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            model=name,
            max_tokens=150,
            temperature=0.7,
            enable_prompt_cache=True,
        )
    ttft: list[float] = []
    ttfs: list[float] = []
    sentences: list[str] = []
    async with aiohttp.ClientSession() as session:
        for i in range(runs):
            chunker = SentenceChunker()
            messages = HISTORY + [{"role": "user", "content": QUESTIONS[i % len(QUESTIONS)]}]
            t0 = time.perf_counter()
            first_delta = first_sentence = None
            sent_text = ""
            full = ""
            try:
                if is_claude:
                    async for ev in llm.stream_chat(system, messages):
                        if isinstance(ev, TextDelta):
                            if first_delta is None:
                                first_delta = time.perf_counter()
                            full += ev.text
                            if first_sentence is None:
                                out = chunker.feed(ev.text)
                                if out:
                                    first_sentence = time.perf_counter()
                                    sent_text = out[0]
                else:
                    async for delta in _groq_stream(session, name, system, messages):
                        if first_delta is None:
                            first_delta = time.perf_counter()
                        full += delta
                        if first_sentence is None:
                            out = chunker.feed(delta)
                            if out:
                                first_sentence = time.perf_counter()
                                sent_text = out[0]
            except Exception as e:
                print(f"  {name}: run {i + 1} failed: {e}")
                return None
            if first_delta is None:
                print(f"  {name}: run {i + 1} produced no text")
                return None
            if first_sentence is None:
                first_sentence = time.perf_counter()
                sent_text = full
            ttft.append((first_delta - t0) * 1000)
            ttfs.append((first_sentence - t0) * 1000)
            sentences.append(sent_text.strip())
            await asyncio.sleep(0.4)
    return {"ttft": ttft, "ttfs": ttfs, "sentences": sentences}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--persona", default=None)
    args = ap.parse_args()

    load_dotenv(REPO / ".env")
    cfg = yaml.safe_load((REPO / "config.yaml").read_text())
    persona_id = args.persona or cfg["bot"]["active_persona"]
    system = _build_system_prompt(persona_id)

    models = ["claude-haiku-4-5", *GROQ_MODELS]
    print(f"persona={persona_id} runs={args.runs} (streaming, no tools)\n")

    results: dict[str, dict] = {}
    for m in models:
        print(f"[{m}]")
        r = await bench_model(m, args.runs, system)
        if r:
            results[m] = r
            for j, (a, b, s) in enumerate(zip(r["ttft"], r["ttfs"], r["sentences"])):
                print(f"  run {j + 1}: ttft={a:5.0f}ms ttfs={b:5.0f}ms  「{s[:40]}」")
        print()

    print("== summary (p50 / p90) ==")
    print(f"  {'model':28s} {'ttft':>16s} {'first sentence':>18s}")
    for m, r in results.items():
        print(
            f"  {m:28s} {_percentile(r['ttft'], 0.5):6.0f} / {_percentile(r['ttft'], 0.9):5.0f}ms"
            f"  {_percentile(r['ttfs'], 0.5):6.0f} / {_percentile(r['ttfs'], 0.9):5.0f}ms"
        )


if __name__ == "__main__":
    asyncio.run(main())
