"""Layer 7 (Fish Audio TTS) tests.

Speed:    TTFA (time to first audio byte), full duration
Quality:  OGG decodable, total bytes reasonable for input length
Robust:   long text, special chars
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest
from dotenv import load_dotenv

from tests.harness._utils import Stat
from echotwin.providers.tts.fish_audio_stream import FishAudioStreamProvider, FishConfig

load_dotenv()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("FISH_AUDIO_API_KEY"),
        reason="FISH_AUDIO_API_KEY not set; skipping live TTS tests",
    ),
]

PERSONA_VOICE_ID = os.environ.get("TEST_VOICE_ID", "")


def _make_tts() -> FishAudioStreamProvider:
    return FishAudioStreamProvider(FishConfig(
        api_key=os.environ["FISH_AUDIO_API_KEY"],
        voice_id=PERSONA_VOICE_ID,
        model="s2-pro",
        latency="low",
    ))


async def _synth(text: str) -> dict:
    tts = _make_tts()
    t0 = time.perf_counter()
    await tts.open()
    open_t = time.perf_counter()

    await tts.push_text(text)
    await tts.flush()
    await tts.end_turn()
    push_t = time.perf_counter()

    first_audio_t: float | None = None
    chunks: list[bytes] = []
    async for c in tts.packets():
        if first_audio_t is None and c:
            first_audio_t = time.perf_counter()
        chunks.append(c)
    await tts.close()
    end_t = time.perf_counter()

    return {
        "text_len": len(text),
        "open_ms": (open_t - t0) * 1000,
        "ttfa_ms": (first_audio_t - t0) * 1000 if first_audio_t else None,
        "total_ms": (end_t - t0) * 1000,
        "audio_bytes": sum(len(c) for c in chunks),
    }


# --- Speed ------------------------------------------------------------

@pytest.mark.asyncio
async def test_tts_ttfa_under_2s():
    """First audio byte should arrive < 2s after open() (low-latency mode)."""
    stat = Stat("ttfa")
    for _ in range(3):
        r = await _synth("你好")
        stat.add(r["ttfa_ms"] / 1000.0)
        print(f"\n  text='你好' open={r['open_ms']:.0f}ms ttfa={r['ttfa_ms']:.0f}ms total={r['total_ms']:.0f}ms")
        await asyncio.sleep(0.5)
    print(f"\n  {stat.fmt_ms()}")
    assert stat.p50 < 2.0, f"TTFA P50 too slow: {stat.p50:.2f}s"


@pytest.mark.asyncio
async def test_tts_long_text_completes():
    """50-char paragraph completes within reasonable time + produces enough bytes."""
    text = "今天天气不错,我打算出门跑步,顺便去超市买点东西,可能还要去一下书店。"
    r = await _synth(text)
    print(f"\n  long text ({r['text_len']} chars): ttfa={r['ttfa_ms']:.0f}ms "
          f"total={r['total_ms']:.0f}ms audio={r['audio_bytes']/1024:.0f}KB")
    assert r["audio_bytes"] > 10_000, "long text produced suspiciously little audio"
    assert r["total_ms"] < 30_000, f"too slow: {r['total_ms']/1000:.1f}s"


# --- Quality ----------------------------------------------------------

@pytest.mark.asyncio
async def test_tts_audio_decodes_with_demux(tmp_path):
    """Verify the OGG bytes can actually be demuxed into opus packets."""
    from echotwin.audio.ogg_demux import OggDemuxer

    tts = _make_tts()
    await tts.open()
    await tts.push_text("你好")
    await tts.flush()
    await tts.end_turn()
    raw = b""
    async for c in tts.packets():
        raw += c
    await tts.close()

    demux = OggDemuxer()
    demux.feed(raw)
    pkts = list(demux.packets()) + list(demux.flush())
    print(f"\n  raw_bytes={len(raw)} opus_packets={len(pkts)}")
    assert pkts, "no opus packets demuxed"
    # Each packet should be a reasonable opus frame size
    avg = sum(len(p) for p in pkts) / len(pkts)
    assert 10 < avg < 500, f"strange opus packet size avg={avg}"


# --- Robustness -------------------------------------------------------

@pytest.mark.asyncio
async def test_tts_special_chars_dont_crash():
    """Emoji, brackets, English mixed shouldn't break Fish."""
    r = await _synth("你好 😀 (test 123) 现在几点?")
    print(f"\n  special chars: audio={r['audio_bytes']}B total={r['total_ms']:.0f}ms")
    assert r["audio_bytes"] > 0, "special-chars text produced no audio"


@pytest.mark.asyncio
async def test_tts_very_short_text():
    """Single character should work."""
    r = await _synth("好")
    print(f"\n  single char: ttfa={r['ttfa_ms']:.0f}ms audio={r['audio_bytes']}B")
    assert r["audio_bytes"] > 0
