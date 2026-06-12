"""Demo: synthesize text via Fish Audio + Ogg demux, count Opus packets."""
from __future__ import annotations
import asyncio
import os
import sys

from echotwin.providers.tts.fish_audio_stream import FishAudioStreamProvider, FishConfig
from echotwin.audio.ogg_demux import OggDemuxer


async def main() -> int:
    api_key = os.environ.get("FISH_AUDIO_API_KEY", "")
    if not api_key:
        print("ERROR: FISH_AUDIO_API_KEY not set", file=sys.stderr)
        return 1
    text = sys.argv[1] if len(sys.argv) > 1 else "你好,这是一次测试。"
    cfg = FishConfig(
        api_key=api_key,
        voice_id=os.environ.get("TEST_VOICE_ID", ""),
    )
    provider = FishAudioStreamProvider(cfg)
    await provider.open()
    await provider.push_text(text)
    await provider.end_turn()

    demux = OggDemuxer()
    pkt_count = 0
    total_bytes = 0
    async for chunk in provider.packets():
        demux.feed(chunk)
        for opus in demux.packets():
            pkt_count += 1
            total_bytes += len(opus)
    for opus in demux.flush():
        pkt_count += 1
        total_bytes += len(opus)
    print(f"Got {pkt_count} Opus packets, {total_bytes} bytes total")
    await provider.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
