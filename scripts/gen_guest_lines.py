#!/usr/bin/env python3
"""Synthesize the demo guest ("Sam") lines with Fish TTS and verify each one
transcribes correctly through the same ASR the bot uses. Bad lines get
flagged so we can reword before recording.

Output: scripts/demo_lines/S<n>.wav  (48k mono, ready to play into BlackHole)

Usage:
  .venv/bin/python scripts/gen_guest_lines.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dotenv import load_dotenv
import numpy as np
import soundfile as sf
import opuslib_next

from echotwin.providers.tts.fish_audio_stream import FishAudioStreamProvider, FishConfig
from echotwin.audio.ogg_demux import OggDemuxer
from echotwin.providers.asr.sherpa_stream import SherpaStreamASR
from echotwin.providers.factory import SHERPA_LANG_REPOS

load_dotenv(REPO / ".env")

SAM_VOICE = "c053bc9135c44e869fde87a59ab3974f"  # mumble_casual
OUT = REPO / "scripts" / "demo_lines"

# (id, language, emotion_tag_prefix, text)
LINES = [
    ("S1", "en", "", "So. You're the famous Ariana everyone keeps talking about. Thought you'd be taller, honestly."),
    ("S2", "en", "", "People say you're the fastest voice bot on Discord. Prove it, I've got places to be."),
    ("S3", "en", "", "Alright, make yourself useful. What's the weather like in Houston right now?"),
    ("S4", "en", "", "Mm-hm. And what time is it over there? Some of us have a life, you know."),
    ("S5", "en", "[sad]", "Okay, real talk though... I've had a genuinely rough day. My flight got cancelled. Twice."),
    ("S6", "en", "[excited]", "Oh, hold on, I just got the email. I got the job. I actually got it!"),
    ("S7", "en", "", "Okay Miss Popular, tell me your whole life story. Every detail. Don't leave anything out."),
    ("S8", "en", "", "Boring! Skip to the good part."),
    ("S9", "en", "", "Okay that was rude. Sorry. You're actually kind of fun."),
    ("S10", "zh", "", "哎?你声音怎么变啦?你现在是谁呀?"),
    ("S11", "zh", "", "太可爱了吧。行,我先撤了,下次聊!"),
]


async def synth(vid: str, text: str) -> np.ndarray:
    tts = FishAudioStreamProvider(FishConfig(
        api_key=os.environ["FISH_AUDIO_API_KEY"], voice_id=vid,
        model="s2-pro", latency="low",
    ))
    await tts.open()
    chunks: list[bytes] = []

    async def drain():
        async for c in tts.packets():
            chunks.append(c)

    t = asyncio.create_task(drain())
    await tts.push_text(text)
    await tts.flush()
    await tts.end_turn()
    await asyncio.wait_for(t, timeout=30)
    await tts.close()

    dmx = OggDemuxer()
    dec = opuslib_next.Decoder(48000, 1)
    pcm = b""
    for c in chunks:
        if not c:
            continue
        dmx.feed(c)
        for pkt in dmx.packets():
            try:
                pcm += dec.decode(pkt, 5760)
            except Exception:
                pass
    return np.frombuffer(pcm, dtype=np.int16)


async def transcribe(arr: np.ndarray, lang: str) -> str:
    repo, files = SHERPA_LANG_REPOS[lang]
    asr = SherpaStreamASR(repo=repo, model_files=files)
    await asr.preload()
    await asr.open()
    raw = arr.tobytes()
    frame = 48000 * 2 * 60 // 1000
    for i in range(0, len(raw), frame):
        await asr.feed_audio(raw[i:i + frame])
        await asyncio.sleep(0)
    for _ in range(400):
        if asr.pipeline_drained():
            break
        await asyncio.sleep(0.02)
    r = await asr.end_utterance()
    await asr.close()
    return (r.text if r else "").strip()


def _overlap(a: str, b: str) -> float:
    """Rough word-recall: fraction of reference words present in the hypothesis."""
    import re
    norm = lambda s: set(re.findall(r"[a-z0-9一-鿿]+", s.lower()))
    ref, hyp = norm(a), norm(b)
    if not ref:
        return 1.0
    return len(ref & hyp) / len(ref)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Sam voice: {SAM_VOICE}\n")
    flagged = []
    for lid, lang, emo, text in LINES:
        arr = await synth(SAM_VOICE, emo + text)
        path = OUT / f"{lid}.wav"
        sf.write(path, arr, 48000)
        heard = await transcribe(arr, lang)
        recall = _overlap(text, heard)
        mark = "OK " if recall >= 0.7 else "⚠ "
        if recall < 0.7:
            flagged.append(lid)
        print(f"{mark}{lid} ({arr.size / 48000:.1f}s, recall {recall:.0%})")
        print(f"     said:  {text}")
        print(f"     heard: {heard!r}\n")
    print("=" * 50)
    if flagged:
        print(f"⚠ needs rewording (ASR recall < 70%): {', '.join(flagged)}")
    else:
        print("✅ all lines transcribe cleanly")


if __name__ == "__main__":
    asyncio.run(main())
