"""Phase 2 automated acceptance — full timeline simulation of streaming ASR + speculative LLM.

Replays the production flow (with speculative_llm enabled):
  while speaking: FunASRStream incremental recognition in 600ms chunks (partials emitted as the user speaks)
  t=-300ms after 300ms of silence: partial stable + pipeline drained → SpeculativeLLM pre-opens the stream
  t=0     endpoint: end_utterance only has the is_final tail chunk left (measured); texts match → attach
  t≈0     TTS WS pre-opened ∥ speculative stream has already been running for 300ms
  …      first sentence → push → first audio from Fish

Acceptance: real mouth-to-ear = 600 + tail chunk + first_audio + 50 ≤ 2000ms

Run: .venv/bin/python -m scripts.verify_phase2   (live API, ~¥0.3)
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

import numpy as np
from dotenv import load_dotenv

N_TURNS = 3
SPEC_LEAD_MS = 300   # how far the speculative stream leads the endpoint


def p50(xs):
    return statistics.median(xs)


async def stage_streaming_asr():
    """Stream-feed the medium fixture; returns (tail-chunk ms, partial, final)."""
    sys.path.insert(0, "tests")
    from harness._utils import load_pcm48k_mono
    from echotwin.providers.asr.sherpa_stream import SherpaStreamASR

    asr = SherpaStreamASR()
    await asr.preload()
    await asr.open()
    pcm = load_pcm48k_mono("medium").tobytes()
    frame = 48000 * 2 * 60 // 1000  # 60ms
    for i in range(0, len(pcm), frame):
        await asr.feed_audio(pcm[i:i + frame])
        await asyncio.sleep(0)
    for _ in range(200):
        if asr.pipeline_drained():
            break
        await asyncio.sleep(0.05)
    partial = asr.partial_text()
    t0 = time.perf_counter()
    result = await asr.end_utterance()
    final_ms = (time.perf_counter() - t0) * 1000
    assert result is not None
    return final_ms, partial, result.text


async def run_turn(final_pass_ms: float, user_text: str) -> dict:
    """Speculative stream leads by 300ms; after the endpoint (t0): tail chunk → attach → first TTS audio."""
    from echotwin.config import load_config
    from echotwin.pipeline.speculative import SpeculativeLLM
    from echotwin.providers.llm.base import MessageEnd, TextDelta
    from echotwin.providers.llm.claude_haiku import ClaudeHaikuProvider
    from echotwin.providers.tts.fish_audio_stream import FishAudioStreamProvider, FishConfig
    from echotwin.utils.sentence_chunker import SentenceChunker

    cfg = load_config("config.yaml")
    f = cfg.tts.fish_audio_stream
    import json
    from pathlib import Path
    persona_id = json.loads(Path("data/runtime_config.json").read_text()).get(
        "active_persona", cfg.bot.active_persona
    ) if Path("data/runtime_config.json").exists() else cfg.bot.active_persona
    from echotwin.persona import load_persona
    persona = load_persona(Path("prompts/personas") / f"{persona_id}.md")
    voice = f.voice_id or persona.voice_id

    llm = ClaudeHaikuProvider(api_key=os.environ["ANTHROPIC_API_KEY"], max_tokens=80)
    payload = json.dumps({"speaker": "u", "emotion": "NEUTRAL", "content": user_text},
                         ensure_ascii=False)
    spec = SpeculativeLLM(
        llm, "你叫一点点点,讲话简短爽朗,回答 1-2 句。",
        [{"role": "user", "content": payload}],
        user_text=user_text, user_payload=payload, tools=None, dialogue_len=0,
    )
    await asyncio.sleep(SPEC_LEAD_MS / 1000)  # speculative stream leads the endpoint by 300ms

    # ===== t0 = endpoint =====
    marks: dict = {}
    t0 = time.perf_counter()
    mark = lambda k: marks.setdefault(k, (time.perf_counter() - t0) * 1000)

    tts = FishAudioStreamProvider(FishConfig(
        api_key=f.api_key, voice_id=voice, model=f.model, latency=f.latency,
    ))

    async def timed_open():
        await tts.open()
        mark("ws_open_done")

    open_task = asyncio.create_task(timed_open())
    await asyncio.sleep(final_pass_ms / 1000)  # is_final tail chunk (measured value)
    mark("asr_final_done")

    assert spec.matches(user_text, 0), "投机匹配失败"
    chunker = SentenceChunker()
    first_audio = asyncio.Event()

    async def drain():
        async for chunk in tts.packets():
            if chunk:
                mark("first_audio")
                first_audio.set()
                return

    drain_task = asyncio.create_task(drain())
    async for ev in spec.events():
        if isinstance(ev, TextDelta):
            mark("llm_first_delta")
            for s in chunker.feed(ev.text):
                await open_task
                await tts.push_text(s)
                await tts.flush()
        elif isinstance(ev, MessageEnd):
            break
    rem = chunker.flush()
    if rem:
        await open_task
        await tts.push_text(rem)
    await open_task
    await tts.end_turn()
    try:
        await asyncio.wait_for(first_audio.wait(), timeout=15)
    except asyncio.TimeoutError:
        pass
    drain_task.cancel()
    await asyncio.gather(drain_task, return_exceptions=True)
    await tts.close()
    await spec.abort()
    if "first_audio" not in marks:
        raise RuntimeError(f"no audio; marks={marks}")
    return marks


async def main():
    load_dotenv()
    print("=== 流式 ASR(真 paraformer,60ms 帧流式喂)===")
    final_ms, partial, final_text = await stage_streaming_asr()
    print(f"  partial={partial!r}")
    print(f"  final={final_text!r}  is_final 尾块={final_ms:.0f}ms")
    print(f"  对照:批式 SenseVoice 端点后要 ~590ms;流式只剩 {final_ms:.0f}ms")

    print(f"\n=== 完整轮(N={N_TURNS},投机 LLM 领先 {SPEC_LEAD_MS}ms)===")
    turns = []
    for i in range(N_TURNS):
        try:
            m = await run_turn(final_ms, "今天天气怎么样(不用调工具,凭感觉答)")
            turns.append(m)
            print(
                f"  run {i+1}: ws_open={m.get('ws_open_done', -1):.0f} "
                f"asr_final={m.get('asr_final_done', -1):.0f} "
                f"llm_first={m.get('llm_first_delta', -1):.0f} "
                f"first_audio={m['first_audio']:.0f}"
            )
        except Exception as e:
            print(f"  run {i+1} ERROR: {e}")
        await asyncio.sleep(1)
    if not turns:
        sys.exit(1)

    fa = p50([m["first_audio"] for m in turns])
    real = 600 + fa + 50
    print(f"\n=== 验收(P50)===")
    print(f"  真实 mouth-to-ear ≈ 600 + {fa:.0f} + 50 = {real:.0f}ms (目标 ≤2000) "
          f"{'✅' if real <= 2000 else '❌'}")
    print(f"  对照:基线 2816ms / Phase 1 实测 2215ms")


if __name__ == "__main__":
    asyncio.run(main())
