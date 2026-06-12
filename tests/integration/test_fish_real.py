import os
import pytest

from echotwin.providers.tts.fish_audio_stream import FishAudioStreamProvider, FishConfig


VOICE_ID = os.environ.get("TEST_VOICE_ID", "")


@pytest.mark.live
@pytest.mark.skipif(not os.getenv("FISH_AUDIO_API_KEY"), reason="needs FISH_AUDIO_API_KEY")
@pytest.mark.asyncio
async def test_real_fish_audio_synthesis():
    cfg = FishConfig(
        api_key=os.environ["FISH_AUDIO_API_KEY"],
        voice_id=VOICE_ID,
    )
    provider = FishAudioStreamProvider(cfg)
    await provider.open()
    await provider.push_text("测试一段话。")
    await provider.end_turn()

    total = 0
    async for pkt in provider.packets():
        total += len(pkt)
    assert total > 1000, f"expected >1KB audio, got {total}B"
    await provider.close()
