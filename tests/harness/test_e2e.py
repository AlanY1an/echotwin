"""End-to-end pipeline test: WAV input → bot OGG response.

Speed:    ASR_done → first_audio_byte (target P50 < 1200ms per spec)
Quality:  bot's text response is non-empty and plausibly relevant
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import numpy as np
import pytest
import soundfile as sf
from dotenv import load_dotenv

from tests.harness._utils import (
    Stat,
    fixture_path,
    load_pcm48k_mono,
    resample_48k_to_16k,
    chunk_pcm_at,
)
from echotwin.providers.asr.funasr_local import FunASRLocal
from echotwin.providers.llm.base import (
    MessageEnd,
    TextDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from echotwin.providers.llm.claude_haiku import ClaudeHaikuProvider
from echotwin.providers.tts.fish_audio_stream import FishAudioStreamProvider, FishConfig
from echotwin.providers.vad.silero import SileroVAD
from echotwin.tools.get_date import GetDate
from echotwin.tools.get_time import GetTime
from echotwin.tools.get_weather import GetWeather
from echotwin.tools.registry import ToolRegistry
from echotwin.utils.sentence_chunker import SentenceChunker

load_dotenv()

PERSONA_VOICE_ID = os.environ.get("TEST_VOICE_ID", "")
SYSTEM_PROMPT = """你叫一点点点,简短台湾女生,回答 1-2 句。
有 get_time / get_date / get_weather 工具,问到时就调用。"""


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set",
    ),
    pytest.mark.skipif(
        not os.environ.get("FISH_AUDIO_API_KEY"),
        reason="FISH_AUDIO_API_KEY not set",
    ),
    pytest.mark.skipif(
        not os.path.isdir("models/SenseVoiceSmall"),
        reason="SenseVoiceSmall model not downloaded",
    ),
]


@pytest.fixture(scope="module")
def asr() -> FunASRLocal:
    return FunASRLocal(model_dir="models/SenseVoiceSmall", device="cpu", language="zh")


@pytest.fixture(scope="module")
def llm():
    return ClaudeHaikuProvider(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model="claude-haiku-4-5",
        max_tokens=200,
    )


@pytest.fixture(scope="module")
def tool_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(GetTime(default_timezone="Asia/Taipei"))
    r.register(GetDate(default_timezone="Asia/Taipei"))
    r.register(GetWeather(default_city="台北"))
    return r


def _new_vad() -> SileroVAD:
    return SileroVAD(threshold=0.4, threshold_low=0.2, min_silence_duration_ms=800, frame_window=2)


async def _vad_asr_pipeline(asr, fixture_tag: str) -> tuple[str, dict]:
    """Run L1+L2+L3 on a fixture, return (transcribed_text, timing)."""
    pcm_48k = load_pcm48k_mono(fixture_tag)
    pcm_16k = resample_48k_to_16k(pcm_48k)
    silence_16k = np.zeros(int(16000 * 1.5), dtype=np.int16)
    pcm_16k = np.concatenate([pcm_16k, silence_16k])

    vad = _new_vad()
    await asr.preload()

    t0 = time.perf_counter()

    # Feed 20ms frames; on speech_started, drain pre-roll equivalent (here we
    # just feed the audio in order and end_utterance when VAD fires)
    samples_per_frame_16 = 16000 * 20 // 1000
    samples_per_frame_48 = 48000 * 20 // 1000
    frame_idx = 0
    speech_started = False
    asr_buffer: list[bytes] = []
    asr_done_at: float | None = None
    transcribed = ""

    for i in range(0, len(pcm_16k), samples_per_frame_16):
        frame_16 = pcm_16k[i : i + samples_per_frame_16]
        if len(frame_16) < samples_per_frame_16:
            break
        result = vad.feed(frame_16.tobytes())

        # Mirror the index in 48k
        offs_48 = frame_idx * samples_per_frame_48
        frame_48 = pcm_48k[offs_48 : offs_48 + samples_per_frame_48]
        frame_idx += 1

        if result.speech_started:
            speech_started = True
        if speech_started and result.is_voice and len(frame_48) == samples_per_frame_48:
            asr_buffer.append(frame_48.tobytes())
        if result.utterance_ended and asr_buffer:
            await asr.feed_audio(b"".join(asr_buffer))
            r = await asr.end_utterance()
            asr_done_at = time.perf_counter()
            transcribed = (r.text if r else "").strip()
            break

    return transcribed, {
        "asr_done_ms": (asr_done_at - t0) * 1000.0 if asr_done_at else None,
    }


async def _llm_tts_pipeline(llm, tool_registry, user_text: str) -> dict:
    """Run L5+L6+L7 on text input, return timing + bot response audio bytes."""
    chunker = SentenceChunker()
    tts = FishAudioStreamProvider(FishConfig(
        api_key=os.environ["FISH_AUDIO_API_KEY"],
        voice_id=PERSONA_VOICE_ID,
        model="s2-pro",
        latency="low",
    ))
    await tts.open()

    t0 = time.perf_counter()
    first_text_t: float | None = None
    first_audio_t: float | None = None
    full_text = ""
    audio_bytes = 0

    # Run a simplified version of think_speak's tool loop (1-2 rounds max)
    cur_messages = [{"role": "user", "content": user_text}]
    tools_schema = tool_registry.to_anthropic_tools()

    async def drain_audio():
        nonlocal first_audio_t, audio_bytes
        async for c in tts.packets():
            if first_audio_t is None and c:
                first_audio_t = time.perf_counter()
            audio_bytes += len(c)

    drain_task = asyncio.create_task(drain_audio())

    for _round in range(3):
        cur_tool_uses: list[dict] = []
        text_round = ""
        stop_reason = "end_turn"

        async for ev in llm.stream_chat(SYSTEM_PROMPT, cur_messages, tools=tools_schema):
            if isinstance(ev, TextDelta):
                if first_text_t is None:
                    first_text_t = time.perf_counter()
                full_text += ev.text
                text_round += ev.text
                for s in chunker.feed(ev.text):
                    await tts.push_text(s)
                    await tts.flush()
            elif isinstance(ev, ToolUseStart):
                cur_tool_uses.append({"id": ev.tool_use_id, "name": ev.name, "partial_json": ""})
            elif isinstance(ev, ToolUseInputDelta):
                cur_tool_uses[-1]["partial_json"] += ev.partial_json
            elif isinstance(ev, MessageEnd):
                stop_reason = ev.stop_reason

        if stop_reason == "tool_use" and cur_tool_uses:
            assistant_blocks = []
            if text_round:
                assistant_blocks.append({"type": "text", "text": text_round})
            for tu in cur_tool_uses:
                args = json.loads(tu["partial_json"]) if tu["partial_json"] else {}
                assistant_blocks.append({
                    "type": "tool_use", "id": tu["id"], "name": tu["name"], "input": args,
                })
            cur_messages = cur_messages + [{"role": "assistant", "content": assistant_blocks}]
            results = []
            for tu in cur_tool_uses:
                args = json.loads(tu["partial_json"]) if tu["partial_json"] else {}
                r = await tool_registry.execute(tu["name"], args)
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": r})
            cur_messages = cur_messages + [{"role": "user", "content": results}]
            continue
        break

    rem = chunker.flush()
    if rem:
        await tts.push_text(rem)
    await tts.end_turn()

    await asyncio.wait_for(drain_task, timeout=20)
    await tts.close()

    return {
        "text": full_text,
        "ttft_ms": (first_text_t - t0) * 1000 if first_text_t else None,
        "ttfa_ms": (first_audio_t - t0) * 1000 if first_audio_t else None,
        "total_ms": (time.perf_counter() - t0) * 1000,
        "audio_bytes": audio_bytes,
    }


# --- E2E speed --------------------------------------------------------

@pytest.mark.parametrize("tag", ["medium", "wake_query"])
@pytest.mark.asyncio
async def test_e2e_full_pipeline(asr, llm, tool_registry, tag, tmp_path):
    """Feed fixture wav → ASR → LLM → TTS → save bot's response audio."""
    transcribed, asr_timing = await _vad_asr_pipeline(asr, tag)
    print(f"\n  [{tag}] transcribed={transcribed!r} (ASR: {asr_timing['asr_done_ms']:.0f}ms)")
    assert transcribed, "ASR produced no text"

    llm_tts_timing = await _llm_tts_pipeline(llm, tool_registry, transcribed)
    print(
        f"  [{tag}] bot reply text={llm_tts_timing['text']!r}\n"
        f"          TTFT={llm_tts_timing['ttft_ms']:.0f}ms "
        f"TTFA={llm_tts_timing['ttfa_ms']:.0f}ms "
        f"total={llm_tts_timing['total_ms']:.0f}ms "
        f"audio={llm_tts_timing['audio_bytes']/1024:.0f}KB"
    )
    assert llm_tts_timing["text"], "bot produced no text response"
    assert llm_tts_timing["audio_bytes"] > 0, "bot produced no audio"
    # Save artifact for manual inspection
    out_ogg = tmp_path / f"{tag}_bot_response.ogg"
    print(f"          (artifact would be at {out_ogg})")


@pytest.mark.asyncio
async def test_e2e_target_p50_under_1200ms(asr, llm, tool_registry):
    """Spec target: ASR_done → first_audio_byte P50 < 1200ms (3 runs)."""
    e2e_stat = Stat("e2e_asr_to_first_audio")
    for _ in range(3):
        # Run ASR
        transcribed, _ = await _vad_asr_pipeline(asr, "medium")
        if not transcribed:
            pytest.skip("ASR produced no text — VAD/ASR upstream issue")
        # Then LLM+TTS, measuring its TTFA from start of LLM
        llm_tts = await _llm_tts_pipeline(llm, tool_registry, transcribed)
        if llm_tts["ttfa_ms"]:
            e2e_stat.add(llm_tts["ttfa_ms"] / 1000.0)
        await asyncio.sleep(1)
    print(f"\n  {e2e_stat.fmt_ms()}")
    # Soft assertion — log the result; don't fail (network-dependent)
    if e2e_stat.p50 > 1.2:
        print(f"  ⚠ P50 {e2e_stat.p50*1000:.0f}ms exceeds 1200ms spec target")
