"""Full-pipeline latency benchmark simulating the real machine — replicates the parallel architecture after the 2026-06-11 optimization.

Measures the real elapsed time from "endpoint trigger → first TTS audio byte", broken into the [latency] journey stages:
  endpoint(t0) → ASR inference (local SenseVoice, real model)
    → [TTS WS open ∥ LLM stream] (same parallel structure as production think_speak)
    → first-sentence chunking → push+flush → first TTS audio byte

Full mouth-to-ear estimate = 600ms (endpoint silence wait, config value) + the value measured here
+ ~50ms (Discord playback startup). The only part that can't be replicated locally is Discord transport (~30-70ms).

Live API benchmark (costs money, roughly N×2 Haiku + Fish calls):
Run: .venv/bin/python -m tests.perf.bench_pipeline_latency
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

from dotenv import load_dotenv

N = 5
FIXTURE = "medium"  # tests/harness/fixtures/medium.wav (real Chinese speech)
ENDPOINT_WAIT_MS = 600  # config.bot.endpoint_silence_ms
PLAYBACK_START_MS = 50  # frame_queue → discord player first frame (empirical value)


async def bench_asr(n: int) -> list[float]:
    """Local SenseVoice inference time (first stage after the endpoint, free)."""
    from tests.harness._utils import load_pcm48k_mono
    from echotwin.providers.asr.funasr_local import FunASRLocal

    asr = FunASRLocal(model_dir="models/SenseVoiceSmall", device="cpu", language="zh")
    await asr.preload()
    pcm = load_pcm48k_mono(FIXTURE).tobytes()

    out: list[float] = []
    for _ in range(n):
        await asr.feed_audio(pcm)
        t0 = time.perf_counter()
        result = await asr.end_utterance()
        out.append((time.perf_counter() - t0) * 1000)
        assert result and result.text.strip(), "ASR 返回空文本,fixture 有问题"
    return out


async def run_turn(voice_id: str, system: str, user_msg: str) -> dict[str, float]:
    """One round of [TTS open ∥ LLM] → first sentence → first audio, same structure as think_speak."""
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
            voice_id=voice_id,
            model="s2-pro",
            latency="low",
        )
    )
    chunker = SentenceChunker()
    marks: dict[str, float] = {}
    t0 = time.perf_counter()

    def mark(name: str) -> None:
        marks[name] = (time.perf_counter() - t0) * 1000

    # Production structure: open runs in parallel with the LLM, awaited before the first push
    async def timed_open():
        await tts.open()
        mark("ws_open_done")

    tts_open_task = asyncio.create_task(timed_open())

    first_audio = asyncio.Event()

    async def drain():
        async for chunk in tts.packets():
            if chunk:
                mark("first_audio")
                first_audio.set()
                return

    drain_task = asyncio.create_task(drain())

    pushed_first = False
    async for ev in llm.stream_chat(system, [{"role": "user", "content": user_msg}]):
        if isinstance(ev, TextDelta):
            if "llm_first_delta" not in marks:
                mark("llm_first_delta")
            for s in chunker.feed(ev.text):
                await tts_open_task
                await tts.push_text(s)
                await tts.flush()
                if not pushed_first:
                    mark("first_push")
                    pushed_first = True
        elif isinstance(ev, MessageEnd):
            break
    rem = chunker.flush()
    if rem:
        await tts_open_task
        await tts.push_text(rem)
    await tts_open_task
    await tts.end_turn()

    try:
        await asyncio.wait_for(first_audio.wait(), timeout=15)
    except asyncio.TimeoutError:
        pass
    drain_task.cancel()
    await asyncio.gather(drain_task, return_exceptions=True)
    await tts.close()

    if "first_audio" not in marks:
        raise RuntimeError(f"no audio produced; marks={marks}")
    return marks


def p50(xs: list[float]) -> float:
    return statistics.median(xs)


async def main() -> None:
    load_dotenv()
    if not (os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("FISH_AUDIO_API_KEY")):
        print("需要 ANTHROPIC_API_KEY + FISH_AUDIO_API_KEY")
        sys.exit(1)

    print(f"=== Stage A: 本地 SenseVoice 推理(fixture={FIXTURE}, N={N},免费)===")
    asr_ms = await bench_asr(N)
    for i, v in enumerate(asr_ms, 1):
        print(f"  run {i}: asr={v:.0f}ms")

    voice_id = os.environ.get("TEST_VOICE_ID", "")
    system = "你叫一点点点,讲话简短爽朗,回答 1-2 句。"
    user_msg = "今天过得怎么样呀"

    print(f"\n=== Stage B: [TTS open ∥ LLM] → 首音频(live API, N={N})===")
    turns: list[dict[str, float]] = []
    for i in range(N):
        try:
            m = await run_turn(voice_id, system, user_msg)
        except Exception as e:
            print(f"  run {i + 1} ERROR: {e}")
            continue
        turns.append(m)
        print(
            f"  run {i + 1}: ws_open={m.get('ws_open_done', float('nan')):.0f}ms  "
            f"llm_first={m.get('llm_first_delta', float('nan')):.0f}ms  "
            f"first_push={m.get('first_push', float('nan')):.0f}ms  "
            f"first_audio={m['first_audio']:.0f}ms"
        )
        await asyncio.sleep(1)

    if not turns:
        print("no successful turns")
        sys.exit(1)

    asr_p50 = p50(asr_ms)
    audio_p50 = p50([m["first_audio"] for m in turns])
    ws_p50 = p50([m["ws_open_done"] for m in turns if "ws_open_done" in m])
    llm_p50 = p50([m["llm_first_delta"] for m in turns if "llm_first_delta" in m])
    total = ENDPOINT_WAIT_MS + asr_p50 + audio_p50 + PLAYBACK_START_MS

    print(f"\n=== 估算 mouth-to-ear(P50,不含 Discord 传输 ~30-70ms)===")
    print(f"  端点静默等待(配置): {ENDPOINT_WAIT_MS}ms")
    print(f"  ASR 推理(本地实测): {asr_p50:.0f}ms")
    print(f"  ws_open(与 LLM 并行,实测): {ws_p50:.0f}ms")
    print(f"  LLM 首 delta(实测): {llm_p50:.0f}ms")
    print(f"  [并行段] → TTS 首音频(实测): {audio_p50:.0f}ms")
    print(f"  播放启动(估): {PLAYBACK_START_MS}ms")
    print(f"  ------------------------------------")
    print(f"  合计 ≈ {total:.0f}ms")
    print(f"  参照:真人 0-200ms | 行业'好' <800ms | 体验崩坏 >1500ms")
    print(f"  注:旧串行架构下 ws_open 会整段叠加在关键路径上(+{ws_p50:.0f}ms)")


if __name__ == "__main__":
    asyncio.run(main())
