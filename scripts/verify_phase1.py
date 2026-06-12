"""Phase 1 automated acceptance — no Discord; real components replay the full timeline.

Replays the production flow (post Phase 1, 2026-06-11):
  t=-300ms silence begins → speculative ASR pre-run (real SenseVoice)
  t=0     endpoint confirmed: adopt speculative result (residual = max(0, inference - 300ms)) + pre-open TTS WS
  t≈0     consumer dispatch: filler packets pre-filled into frame_queue (real cached OGG, real Fish synthesis)
  t+residual  LLM stream starts (real Haiku) ∥ WS handshake already in flight
  …      first sentence → push (wait for WS ready) → first audio bytes from Fish

Acceptance formula (per dev-docs plan T5):
  real mouth-to-ear = 600 (endpoint) + this script's first_audio + 50 (playback) ≤ 2400ms
  perceived         = 600 + filler-ready time + 50                               ≤ 1000ms

Run: .venv/bin/python -m scripts.verify_phase1   (live API, ~¥0.3)
"""
from __future__ import annotations

import asyncio
import os
import queue as sync_queue
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

N_TURNS = 3
SPEC_FIRE_MS = 300   # speculative_asr_silence_ms
ENDPOINT_MS = 600    # endpoint_silence_ms
PLAYBACK_MS = 50


def p50(xs):
    return statistics.median(xs)


async def make_stub_bot():
    """Real config + real persona, with VoiceAgentBot's real methods bound."""
    from echotwin.bot import VoiceAgentBot
    from echotwin.config import load_config
    from echotwin.persona import load_persona

    cfg = load_config("config.yaml")
    # active_persona in runtime_config may override the yaml
    import json
    rc = Path("data/runtime_config.json")
    persona_id = cfg.bot.active_persona
    if rc.exists():
        persona_id = json.loads(rc.read_text()).get("active_persona", persona_id)
    persona = load_persona(Path("prompts") / "personas" / f"{persona_id}.md")

    bot = SimpleNamespace(config=cfg, persona=persona, cost_tracker=None)
    bot.active_voice_id = lambda: cfg.tts.fish_audio_stream.voice_id or persona.voice_id
    for name in ("_synth_with_persona", "_filler_paths", "_ensure_filler_audio", "pick_filler_path"):
        setattr(bot, name, getattr(VoiceAgentBot, name).__get__(bot))
    return bot


async def stage_filler(bot) -> tuple[float, int]:
    """Real synthesis (when missing) + real demux pre-fill; returns (filler-ready time in ms, packet count)."""
    from echotwin.pipeline.filler import enqueue_filler_packets, should_play_filler

    assert should_play_filler("今天天气怎么样", bot.config.bot.filler_mode, bot.config.bot.filler_keywords)
    assert not should_play_filler("你好呀", bot.config.bot.filler_mode, bot.config.bot.filler_keywords)

    await bot._ensure_filler_audio()  # actually calls Fish synthesis when missing
    path = bot.pick_filler_path()
    assert path is not None, "垫话音频不存在且合成失败"

    q: sync_queue.Queue = sync_queue.Queue(maxsize=200)
    t0 = time.perf_counter()
    n = enqueue_filler_packets(path, q)
    ms = (time.perf_counter() - t0) * 1000
    assert n > 0, "垫话 OGG demux 出 0 个包"
    return ms, n


async def stage_spec_asr() -> tuple[float, float, str]:
    """Real SenseVoice speculation: returns (inference ms, residual ms after endpoint, text)."""
    sys.path.insert(0, "tests")
    from harness._utils import load_pcm48k_mono
    from echotwin.providers.asr.funasr_local import FunASRLocal

    asr = FunASRLocal(model_dir="models/SenseVoiceSmall", device="cpu", language="zh")
    await asr.preload()
    pcm = load_pcm48k_mono("medium").tobytes()
    await asr.feed_audio(pcm)

    t0 = time.perf_counter()
    result, fed = await asr.speculate()
    spec_ms = (time.perf_counter() - t0) * 1000
    assert result is not None and fed == asr.buffered_bytes()
    asr.drop_buffer()  # simulate finalize adopting the result (HIT)
    residual = max(0.0, spec_ms - (ENDPOINT_MS - SPEC_FIRE_MS))
    return spec_ms, residual, result.text


async def run_turn(bot, asr_residual_ms: float) -> dict:
    """From the endpoint (t0): pre-open WS ∥ (after residual) LLM → first sentence → first audio."""
    from echotwin.providers.llm.base import MessageEnd, TextDelta
    from echotwin.providers.llm.claude_haiku import ClaudeHaikuProvider
    from echotwin.providers.tts.fish_audio_stream import FishAudioStreamProvider, FishConfig
    from echotwin.utils.sentence_chunker import SentenceChunker

    f = bot.config.tts.fish_audio_stream
    tts = FishAudioStreamProvider(FishConfig(
        api_key=f.api_key, voice_id=bot.active_voice_id(), model=f.model, latency=f.latency,
    ))
    llm = ClaudeHaikuProvider(api_key=os.environ["ANTHROPIC_API_KEY"], max_tokens=80)
    chunker = SentenceChunker()
    marks: dict = {}
    t0 = time.perf_counter()
    mark = lambda k: marks.setdefault(k, (time.perf_counter() - t0) * 1000)

    async def timed_open():
        await tts.open()
        mark("ws_open_done")

    open_task = asyncio.create_task(timed_open())  # pre-open at the endpoint

    first_audio = asyncio.Event()

    async def drain():
        async for chunk in tts.packets():
            if chunk:
                mark("first_audio")
                first_audio.set()
                return

    drain_task = asyncio.create_task(drain())

    await asyncio.sleep(asr_residual_ms / 1000)  # speculation residual: LLM waits for the transcript to be ready
    mark("llm_fired")
    async for ev in llm.stream_chat(
        "你叫一点点点,讲话简短爽朗,回答 1-2 句。",
        [{"role": "user", "content": "今天天气怎么样(不用调工具,凭感觉答)"}],
    ):
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
    if "first_audio" not in marks:
        raise RuntimeError(f"no audio; marks={marks}")
    return marks


async def main():
    load_dotenv()
    bot = await make_stub_bot()
    print(f"persona={bot.persona.id} voice={bot.active_voice_id()[:8]}…")

    print("\n=== 垫话链路(合成→缓存→demux→预灌)===")
    filler_ms, n_pkts = await stage_filler(bot)
    print(f"  垫话就绪 {filler_ms:.1f}ms, {n_pkts} 个 opus 包")
    perceived = ENDPOINT_MS + filler_ms + PLAYBACK_MS
    print(f"  体感延迟 ≈ {perceived:.0f}ms (目标 ≤1000) {'✅' if perceived <= 1000 else '❌'}")

    print("\n=== 投机 ASR(真 SenseVoice,medium fixture)===")
    spec_ms, residual, text = await stage_spec_asr()
    print(f"  推理 {spec_ms:.0f}ms → 端点后残余 {residual:.0f}ms (无投机时 = {spec_ms:.0f}ms)")
    print(f"  文本: {text[:30]}…")

    print(f"\n=== 完整轮(N={N_TURNS},预开 WS ∥ 残余 {residual:.0f}ms 后 LLM)===")
    turns = []
    for i in range(N_TURNS):
        try:
            m = await run_turn(bot, residual)
            turns.append(m)
            print(
                f"  run {i+1}: ws_open={m.get('ws_open_done', -1):.0f} "
                f"llm_first={m.get('llm_first_delta', -1):.0f} first_audio={m['first_audio']:.0f}"
            )
        except Exception as e:
            print(f"  run {i+1} ERROR: {e}")
        await asyncio.sleep(1)
    if not turns:
        sys.exit(1)

    fa = p50([m["first_audio"] for m in turns])
    real = ENDPOINT_MS + fa + PLAYBACK_MS
    print(f"\n=== 验收(P50)===")
    print(f"  真实 mouth-to-ear ≈ 600 + {fa:.0f} + 50 = {real:.0f}ms (目标 ≤2400) {'✅' if real <= 2400 else '❌'}")
    print(f"  体感(垫话轮)   ≈ {perceived:.0f}ms (目标 ≤1000) {'✅' if perceived <= 1000 else '❌'}")
    print(f"  对照:优化前实测 2816ms;无投机时本轮 ≈ {real + (spec_ms - residual):.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
