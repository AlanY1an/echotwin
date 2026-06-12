"""SherpaStreamASR — streaming zipformer: partials while speaking, near-zero wait at endpoint.

Model comes from the HF cache (~100MB); skipped when not downloaded.
"""
from pathlib import Path

import pytest

from tests.harness._utils import load_pcm48k_mono
from echotwin.providers.asr.sherpa_stream import SherpaStreamASR

_HF_CACHE = Path.home() / ".cache" / "huggingface" / "hub" / \
    "models--csukuangfj--sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"

pytestmark = pytest.mark.skipif(
    not (_HF_CACHE.exists() and any(_HF_CACHE.rglob("tokens.txt"))),
    reason="sherpa streaming zipformer model not downloaded",
)


@pytest.fixture(scope="module")
def stream_asr() -> SherpaStreamASR:
    return SherpaStreamASR()


async def test_partial_grows_during_speech_and_final_matches(stream_asr):
    import asyncio

    await stream_asr.preload()
    await stream_asr.open()
    pcm = load_pcm48k_mono("medium").tobytes()
    frame = 48000 * 2 * 60 // 1000  # 60ms frames, mimicking Discord pacing
    for i in range(0, len(pcm), frame):
        await stream_asr.feed_audio(pcm[i:i + frame])
        await asyncio.sleep(0)
    for _ in range(100):
        if stream_asr.pipeline_drained():
            break
        await asyncio.sleep(0.02)
    partial = stream_asr.partial_text()
    result = await stream_asr.end_utterance()
    assert result is not None and result.text.strip()
    assert partial, "说话期间必须有 partial 文本"
    assert result.text.startswith(partial[:2]), (
        f"final 应延续 partial: partial={partial!r} final={result.text!r}"
    )
    # raw PCM must be kept for the emotion sidecar
    assert len(stream_asr.last_utterance_pcm) > 0


async def test_speculate_declines_and_state_resets(stream_asr):
    await stream_asr.open()
    assert (await stream_asr.speculate()) == (None, -1), "流式模式不做投机 ASR(防截尾)"
    await stream_asr.feed_audio(b"\x00" * 9600)
    assert stream_asr.buffered_bytes() == 9600
    stream_asr.drop_buffer()
    assert stream_asr.buffered_bytes() == 0
    assert stream_asr.partial_text() == ""
