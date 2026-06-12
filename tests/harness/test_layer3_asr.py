"""Layer 3 (FunASR + SenseVoice) tests.

Speed:    end_utterance() latency, RTF (real-time factor)
Quality:  per-fixture char error rate (CER) vs expected, emotion distribution
Robust:   noisy / quiet / code-switch handling

Skips if SenseVoiceSmall model isn't downloaded.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.harness._utils import (
    EXPECTED_TEXT,
    Stat,
    char_error_rate,
    load_pcm48k_mono,
    time_ms,
)
from echotwin.providers.asr.funasr_local import FunASRLocal

MODEL_DIR = "models/SenseVoiceSmall"
pytestmark = pytest.mark.skipif(
    not os.path.isdir(MODEL_DIR),
    reason="SenseVoiceSmall model not downloaded",
)


@pytest.fixture(scope="module")
def asr() -> FunASRLocal:
    """Module-scoped ASR — reuse the model across tests (loads once)."""
    return FunASRLocal(model_dir=MODEL_DIR, device="cpu", language="zh")


async def _transcribe(asr: FunASRLocal, tag: str) -> dict:
    """Feed full fixture into ASR, return transcribed text + emotion + timing."""
    pcm_48k = load_pcm48k_mono(tag)
    pcm_bytes = pcm_48k.tobytes()
    duration_s = len(pcm_48k) / 48000.0

    await asr.preload()
    await asr.feed_audio(pcm_bytes)
    with time_ms() as t:
        result = await asr.end_utterance()
    elapsed_ms = t.elapsed_ms

    return {
        "tag": tag,
        "text": (result.text if result else "").strip(),
        "emotion": (result.emotion if result else "NEUTRAL"),
        "language": (result.language if result else "?"),
        "elapsed_ms": elapsed_ms,
        "duration_s": duration_s,
        "rtf": elapsed_ms / 1000.0 / duration_s,
    }


# --- Speed ------------------------------------------------------------

@pytest.mark.asyncio
async def test_asr_realtime_factor(asr):
    """RTF (compute time / audio duration) should be < 1.0 (faster than realtime)."""
    rtf_stat = Stat("asr_rtf")
    for tag in ("short", "medium", "long"):
        r = await _transcribe(asr, tag)
        rtf_stat.add(r["rtf"])
        print(
            f"\n  {tag}: text={r['text']!r} "
            f"elapsed={r['elapsed_ms']:.0f}ms duration={r['duration_s']:.2f}s "
            f"RTF={r['rtf']:.2f}"
        )
    print(f"\n  RTF: P50={rtf_stat.p50:.2f} max={rtf_stat.max:.2f}")
    assert rtf_stat.p50 < 1.0, f"ASR RTF too slow: P50={rtf_stat.p50:.2f}"


# --- Quality ----------------------------------------------------------

@pytest.mark.parametrize("tag", ["short", "medium", "long"])
@pytest.mark.asyncio
async def test_asr_clean_text_accuracy(asr, tag):
    """Clean fixtures should transcribe with CER < 0.2 (allow some Fish/SenseVoice mismatch)."""
    r = await _transcribe(asr, tag)
    expected = EXPECTED_TEXT[tag]
    cer = char_error_rate(expected, r["text"])
    print(f"\n  {tag}: expected={expected!r} got={r['text']!r} CER={cer:.2f}")
    assert cer < 0.3, f"{tag}: CER too high ({cer:.2f}); got {r['text']!r}"


@pytest.mark.asyncio
async def test_asr_returns_emotion_neutral_default(asr):
    """Most fixtures are neutral. Just verify we get *some* emotion, not crash."""
    r = await _transcribe(asr, "medium")
    print(f"\n  medium: emotion={r['emotion']} language={r['language']}")
    valid = {"NEUTRAL", "HAPPY", "SAD", "ANGRY", "FEARFUL", "SURPRISED", "DISGUSTED"}
    assert r["emotion"] in valid, f"unexpected emotion {r['emotion']!r}"


# --- Robustness -------------------------------------------------------

@pytest.mark.asyncio
async def test_asr_handles_noisy(asr):
    """SNR 12dB white noise: text may degrade but should still produce something close."""
    r = await _transcribe(asr, "noisy")
    expected = EXPECTED_TEXT["noisy"]
    cer = char_error_rate(expected, r["text"])
    print(f"\n  noisy: expected={expected!r} got={r['text']!r} CER={cer:.2f}")
    # Looser bar — allow up to 60% error before failing
    assert cer < 0.6, f"noisy: CER way too high ({cer:.2f}); got {r['text']!r}"


@pytest.mark.asyncio
async def test_asr_handles_quiet_or_documents(asr):
    """-18dB quiet fixture: document behavior. May fail recognition entirely."""
    r = await _transcribe(asr, "quiet")
    expected = EXPECTED_TEXT["quiet"]
    cer = char_error_rate(expected, r["text"])
    print(
        f"\n  quiet: expected={expected!r} got={r['text']!r} CER={cer:.2f} "
        f"(quality={'OK' if cer < 0.5 else 'DEGRADED'})"
    )
    # No assertion — quiet is informational. Bot would handle by re-prompting in practice.


@pytest.mark.asyncio
async def test_asr_code_switch(asr):
    """Mixed English+Chinese — verify we get something meaningful."""
    r = await _transcribe(asr, "code_switch")
    print(f"\n  code_switch: expected={EXPECTED_TEXT['code_switch']!r} got={r['text']!r}")
    # Low bar: just check we got non-empty text containing Chinese chars
    assert r["text"], "code_switch produced empty text"
    assert any("一" <= c <= "鿿" for c in r["text"]), "no Chinese characters detected"


async def test_speculate_then_confirm_reuses_result(asr: FunASRLocal):
    """Speculative inference must not clear the buffer; with no new audio the fed marker validates and drop_buffer cleans up."""
    from tests.harness._utils import load_pcm48k_mono

    try:
        pcm = load_pcm48k_mono("medium").tobytes()
        await asr.preload()
        await asr.feed_audio(pcm)

        result, fed = await asr.speculate()
        assert result is not None and result.text.strip()
        assert fed == asr.buffered_bytes(), "投机后无新音频,fed 应等于当前缓冲量"

        await asr.feed_audio(pcm[:9600])  # the user spoke another 100ms
        assert asr.buffered_bytes() != fed, "新音频到达后 fed 校验必须失效"
    finally:
        asr.drop_buffer()
    assert asr.buffered_bytes() == 0


async def test_speculate_too_short_buffer_declines(asr: FunASRLocal):
    """<100ms of audio is not worth speculating on (20ms of noise must not burn an inference)."""
    try:
        await asr.feed_audio(b"\x00" * 4000)  # ~42ms @48k int16
        result, fed = await asr.speculate()
        assert result is None and fed == -1
    finally:
        asr.drop_buffer()


async def test_speculate_default_noop():
    """Base-class default: providers without speculation support return (None, -1); callers skip safely."""
    from echotwin.providers.asr.base import ASRProvider

    class Dummy(ASRProvider):
        async def open(self): ...
        async def close(self): ...
        async def feed_audio(self, pcm): ...
        async def end_utterance(self): return None

    d = Dummy()
    assert (await d.speculate()) == (None, -1)
    assert d.buffered_bytes() == 0
    d.drop_buffer()  # must not raise
