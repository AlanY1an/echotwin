"""Measure ASR_done -> first_audio_byte latency.

Live API benchmark — set ANTHROPIC_API_KEY + FISH_AUDIO_API_KEY before running.

Run:  .venv/bin/python -m tests.perf.bench_e2e_latency
Target: P50 < 1200ms (the original spec's end-to-end latency goal).
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

from dotenv import load_dotenv


N = 5  # bump to 20 for stable P95


async def run_once(persona_voice_id: str, system: str, user_msg: str) -> tuple[float, float]:
    from echotwin.providers.llm.base import MessageEnd, TextDelta
    from echotwin.providers.llm.claude_haiku import ClaudeHaikuProvider
    from echotwin.providers.tts.fish_audio_stream import (
        FishAudioStreamProvider,
        FishConfig,
    )
    from echotwin.utils.sentence_chunker import SentenceChunker

    llm = ClaudeHaikuProvider(api_key=os.environ["ANTHROPIC_API_KEY"], max_tokens=80)
    tts = FishAudioStreamProvider(
        FishConfig(
            api_key=os.environ["FISH_AUDIO_API_KEY"],
            voice_id=persona_voice_id,
            model="s2-pro",
            latency="low",
        )
    )
    await tts.open()
    chunker = SentenceChunker()

    t0 = time.perf_counter()
    first_text_time: float | None = None
    first_audio_time: float | None = None

    async def drain():
        nonlocal first_audio_time
        async for chunk in tts.packets():
            if first_audio_time is None and chunk:
                first_audio_time = time.perf_counter()
                return  # we only need to time the first byte

    drain_task = asyncio.create_task(drain())

    async for ev in llm.stream_chat(system, [{"role": "user", "content": user_msg}]):
        if isinstance(ev, TextDelta):
            if first_text_time is None:
                first_text_time = time.perf_counter()
            for s in chunker.feed(ev.text):
                await tts.push_text(s)
                await tts.flush()
        elif isinstance(ev, MessageEnd):
            break
    rem = chunker.flush()
    if rem:
        await tts.push_text(rem)
    await tts.end_turn()

    try:
        await asyncio.wait_for(drain_task, timeout=15)
    except asyncio.TimeoutError:
        pass
    await tts.close()

    if first_text_time is None or first_audio_time is None:
        raise RuntimeError("LLM or TTS produced no output")

    return (
        (first_text_time - t0) * 1000,
        (first_audio_time - t0) * 1000,
    )


async def main():
    load_dotenv()
    persona_voice = os.environ.get("TEST_VOICE_ID", "")
    system = "你叫一点点点,讲话简短爽朗。"
    user_msg = "你好"

    text_lat: list[float] = []
    audio_lat: list[float] = []
    for i in range(N):
        try:
            t_text, t_audio = await run_once(persona_voice, system, user_msg)
        except Exception as e:
            print(f"run {i+1} ERROR: {e}")
            continue
        text_lat.append(t_text)
        audio_lat.append(t_audio)
        print(f"run {i+1}: first_text={t_text:.0f}ms  first_audio={t_audio:.0f}ms")
        await asyncio.sleep(1)

    if not audio_lat:
        print("no successful runs")
        sys.exit(1)

    print("\n=== Summary (N={}) ===".format(len(audio_lat)))
    print(
        f"first_text  P50={statistics.median(text_lat):.0f}ms  "
        f"max={max(text_lat):.0f}ms"
    )
    print(
        f"first_audio P50={statistics.median(audio_lat):.0f}ms  "
        f"max={max(audio_lat):.0f}ms"
    )
    p50 = statistics.median(audio_lat)
    if p50 > 1200:
        print(f"⚠ P50 {p50:.0f}ms exceeds 1200ms target")
        sys.exit(2)
    print("✅ P50 within target")


if __name__ == "__main__":
    asyncio.run(main())
