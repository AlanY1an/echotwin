"""Fish Audio protocol errors must surface — no silent zero-audio empty turns.

Historical bug: server-side errors (bad voice_id etc.) arrive asynchronously as
finish(reason != stop); _read_loop only WARNED and turned it into end-of-stream,
the caller only saw bytes_received=0, FishProtocolError was never raised anywhere
in the codebase, and the fallback was dead code.
"""
import msgpack

from echotwin.providers.tts.fish_audio_stream import (
    FishAudioStreamProvider,
    FishConfig,
)


class FakeWS:
    def __init__(self, messages):
        self._messages = messages

    def __aiter__(self):
        async def gen():
            for m in self._messages:
                yield m

        return gen()

    async def close(self):
        pass


def _provider_with_ws(messages) -> FishAudioStreamProvider:
    p = FishAudioStreamProvider(FishConfig(api_key="k", voice_id="bad-voice"))
    p._ws = FakeWS(messages)
    return p


async def test_finish_error_sets_last_error_and_ends_stream():
    p = _provider_with_ws(
        [
            msgpack.packb(
                {"event": "finish", "reason": "error", "message": "reference not found"},
                use_bin_type=True,
            )
        ]
    )
    await p._read_loop()

    assert p.last_error is not None
    assert "reference not found" in p.last_error
    assert p._packet_queue.get_nowait() is None  # the stream still terminates normally


async def test_normal_finish_leaves_no_error():
    p = _provider_with_ws(
        [
            msgpack.packb({"event": "audio", "audio": b"\x01\x02"}, use_bin_type=True),
            msgpack.packb({"event": "finish", "reason": "stop"}, use_bin_type=True),
        ]
    )
    await p._read_loop()

    assert p.last_error is None
    assert p._packet_queue.get_nowait() == b"\x01\x02"
    assert p._packet_queue.get_nowait() is None
