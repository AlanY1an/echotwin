"""Generate test fixture WAV files via Fish Audio TTS + post-processing.

Each fixture has:
  - text: what the bot is "saying"
  - tag: short id used in test code
  - post-processing (silence padding, noise mix, volume scale, splicing)

Run:  .venv/bin/python -m tests.harness.fixtures.gen_fixtures

Skips fixtures whose .wav already exists. To regenerate a single fixture:
remove the .wav and re-run. Audio is committed to git so CI doesn't need
Fish credentials.

Sample rate: 48000 Hz mono int16 WAV (matches what ASR consumes; VAD
downsamples 48k→16k internally via the production path).
"""
from __future__ import annotations

import asyncio
import os
import struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
import soundfile as sf
from dotenv import load_dotenv
from loguru import logger

from echotwin.providers.tts.fish_audio_stream import FishAudioStreamProvider, FishConfig

FIXTURE_DIR = Path(__file__).parent
SAMPLE_RATE = 48000
PERSONA_VOICE_ID = os.environ.get("TEST_VOICE_ID", "")  # Yidiandiandian persona voice


@dataclass
class FixtureSpec:
    tag: str             # short id, used as filename stem
    text: str            # what to synthesize
    pad_head_ms: int = 200  # silence padding at start (simulates pre-VAD silence)
    pad_tail_ms: int = 600  # silence padding at end (so VAD detects utterance_ended)
    noise_snr_db: float | None = None   # if set, mix white noise at this SNR
    volume_db: float = 0.0              # gain in dB (negative = quieter)
    splice_with: list[tuple[str, int]] | None = None  # [(other_tag, gap_ms), ...] for double-utterance


# Fixture catalogue
FIXTURES: list[FixtureSpec] = [
    FixtureSpec(tag="short", text="你好"),
    FixtureSpec(tag="medium", text="现在几点了"),
    FixtureSpec(tag="long", text="今天天气怎么样,我想出门跑步顺便买点东西"),
    FixtureSpec(tag="wake_only", text="一点点点"),
    FixtureSpec(tag="wake_query", text="一点点点 现在几点"),
    FixtureSpec(tag="code_switch", text="hello, 现在几点了"),
    FixtureSpec(tag="quiet", text="现在几点了", volume_db=-18),
    FixtureSpec(tag="noisy", text="现在几点了", noise_snr_db=12),
    # Double + silence_gap composed from existing audio (post-process splicing)
]


async def _synth_via_fish(text: str, voice_id: str) -> bytes:
    """Call Fish Audio TTS, return raw OGG/Opus bytes."""
    cfg = FishConfig(
        api_key=os.environ["FISH_AUDIO_API_KEY"],
        voice_id=voice_id,
        model="s2-pro",
        latency="low",
    )
    tts = FishAudioStreamProvider(cfg)
    await tts.open()
    await tts.push_text(text)
    await tts.flush()
    await tts.end_turn()
    chunks: list[bytes] = []
    async for c in tts.packets():
        chunks.append(c)
    await tts.close()
    return b"".join(chunks)


def _ogg_to_pcm48k_mono(ogg_bytes: bytes) -> np.ndarray:
    """Decode OGG/Opus → 48k mono int16 PCM.

    soundfile handles OGG/Vorbis natively; for Opus-in-OGG we need ffmpeg fallback.
    Try soundfile first; if it fails, shell out to ffmpeg.
    """
    try:
        with sf.SoundFile(BytesIO(ogg_bytes)) as f:
            audio = f.read(dtype="int16")
            sr = f.samplerate
            if audio.ndim == 2:
                audio = audio.mean(axis=1).astype(np.int16)
            if sr != SAMPLE_RATE:
                # Resample (should be 48k already from Fish)
                import soxr
                audio_f = audio.astype(np.float32) / 32768.0
                audio_f = soxr.resample(audio_f, sr, SAMPLE_RATE)
                audio = (audio_f * 32768.0).clip(-32768, 32767).astype(np.int16)
            return audio
    except Exception as e:
        logger.warning(f"soundfile failed ({e}); trying ffmpeg")
        return _ogg_to_pcm48k_mono_ffmpeg(ogg_bytes)


def _ogg_to_pcm48k_mono_ffmpeg(ogg_bytes: bytes) -> np.ndarray:
    """Use ffmpeg to convert OGG/Opus → 48k mono int16 PCM."""
    import subprocess
    p = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"],
        input=ogg_bytes, capture_output=True, check=True,
    )
    return np.frombuffer(p.stdout, dtype=np.int16)


def _pad_silence(audio: np.ndarray, head_ms: int, tail_ms: int) -> np.ndarray:
    head = np.zeros(int(SAMPLE_RATE * head_ms / 1000), dtype=audio.dtype)
    tail = np.zeros(int(SAMPLE_RATE * tail_ms / 1000), dtype=audio.dtype)
    return np.concatenate([head, audio, tail])


def _apply_gain_db(audio: np.ndarray, gain_db: float) -> np.ndarray:
    if gain_db == 0:
        return audio
    factor = 10.0 ** (gain_db / 20.0)
    return (audio.astype(np.float32) * factor).clip(-32768, 32767).astype(np.int16)


def _mix_noise(audio: np.ndarray, snr_db: float) -> np.ndarray:
    """Add white noise at the given SNR."""
    rms_signal = np.sqrt(np.mean(audio.astype(np.float64) ** 2))
    noise_rms = rms_signal / (10.0 ** (snr_db / 20.0))
    rng = np.random.default_rng(42)
    noise = rng.standard_normal(len(audio)) * noise_rms
    mixed = audio.astype(np.float64) + noise
    return mixed.clip(-32768, 32767).astype(np.int16)


def _save(audio: np.ndarray, path: Path) -> None:
    sf.write(str(path), audio, SAMPLE_RATE, subtype="PCM_16")


async def generate_one(spec: FixtureSpec) -> Path:
    """Generate one fixture if missing. Returns path."""
    out = FIXTURE_DIR / f"{spec.tag}.wav"
    if out.exists() and out.stat().st_size > 0:
        logger.info(f"[skip] {out.name} already exists")
        return out

    logger.info(f"[gen]  {out.name}: synthesizing {spec.text!r}")
    ogg = await _synth_via_fish(spec.text, PERSONA_VOICE_ID)
    audio = _ogg_to_pcm48k_mono(ogg)

    if spec.volume_db != 0:
        audio = _apply_gain_db(audio, spec.volume_db)
    if spec.noise_snr_db is not None:
        audio = _mix_noise(audio, spec.noise_snr_db)
    audio = _pad_silence(audio, spec.pad_head_ms, spec.pad_tail_ms)

    _save(audio, out)
    logger.info(f"[done] {out.name}: {len(audio)/SAMPLE_RATE:.2f}s, {out.stat().st_size/1024:.0f} KB")
    return out


def _splice(spec_a_path: Path, spec_b_path: Path, gap_ms: int, out: Path) -> None:
    """Concatenate two fixtures with a silence gap in between."""
    a, sr_a = sf.read(str(spec_a_path), dtype="int16")
    b, sr_b = sf.read(str(spec_b_path), dtype="int16")
    assert sr_a == sr_b == SAMPLE_RATE, "fixture sample rate mismatch"
    gap = np.zeros(int(SAMPLE_RATE * gap_ms / 1000), dtype=np.int16)
    spliced = np.concatenate([a, gap, b])
    _save(spliced, out)
    logger.info(f"[done] {out.name}: spliced {spec_a_path.stem}+{spec_b_path.stem} gap={gap_ms}ms")


async def main():
    load_dotenv()
    if "FISH_AUDIO_API_KEY" not in os.environ:
        raise SystemExit("FISH_AUDIO_API_KEY not set; can't synthesize fixtures")

    # Phase 1: synthesize base fixtures
    for spec in FIXTURES:
        await generate_one(spec)

    # Phase 2: composite fixtures (require base ones to exist)
    composites = [
        # short_gap: two utterances 300ms apart — VAD should see ONE end + ONE start
        ("short_gap", "medium", "short", 300),
        # long_gap: two utterances 1500ms apart — VAD should see TWO endpoint events
        ("long_gap", "medium", "short", 1500),
    ]
    for tag, base_a, base_b, gap_ms in composites:
        out = FIXTURE_DIR / f"{tag}.wav"
        if out.exists() and out.stat().st_size > 0:
            logger.info(f"[skip] {out.name} already exists")
            continue
        a = FIXTURE_DIR / f"{base_a}.wav"
        b = FIXTURE_DIR / f"{base_b}.wav"
        _splice(a, b, gap_ms, out)


if __name__ == "__main__":
    asyncio.run(main())
