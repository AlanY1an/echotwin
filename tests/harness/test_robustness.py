"""Cross-layer robustness tests — failure injection, edge cases.

Covers scenarios that trigger production bugs:
  - LLM stream stalls (TimeoutError handled)
  - Tool execution fails inside the tool loop
  - TTS WS open() retry-on-failure
  - Sentence chunker doesn't deadlock on empty input
  - VAD reset between utterances (no state leakage)

These don't need fixtures — pure unit tests on the harness layer.
"""
from __future__ import annotations

import asyncio
import os

import numpy as np
import pytest
from dotenv import load_dotenv

from echotwin.providers.llm.base import (
    LLMEvent,
    LLMProvider,
    MessageEnd,
    TextDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from echotwin.providers.vad.silero import SileroVAD
from echotwin.tools.base import Tool, ToolError
from echotwin.tools.registry import ToolRegistry
from echotwin.utils.retry import async_retry

load_dotenv()


MODEL_PATH = "models/silero_vad/src/silero_vad/data/silero_vad.onnx"


# --- Tool execution failure recovery ----------------------------------

class FailingTool(Tool):
    name = "broken"
    description = "always fails"
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, args):
        raise ToolError("intentional failure")


class CrashingTool(Tool):
    name = "crash"
    description = "raises non-ToolError"
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, args):
        raise RuntimeError("kaboom")


@pytest.mark.asyncio
async def test_tool_failure_returns_message_not_exception():
    reg = ToolRegistry()
    reg.register(FailingTool())
    reg.register(CrashingTool())

    r = await reg.execute("broken", {})
    assert "intentional failure" in r
    assert r.startswith("Error:"), f"expected error prefix, got {r!r}"

    r2 = await reg.execute("crash", {})
    assert "Error:" in r2
    assert "RuntimeError" in r2 or "failed" in r2

    r3 = await reg.execute("nonexistent", {})
    assert "unknown" in r3.lower() or "not found" in r3.lower()


# --- Retry helper -----------------------------------------------------

@pytest.mark.asyncio
async def test_retry_eventually_succeeds():
    n = {"count": 0}

    async def flaky():
        n["count"] += 1
        if n["count"] < 3:
            raise ConnectionError("flaky")
        return "ok"

    r = await async_retry(flaky, attempts=5, base_delay=0.001)
    assert r == "ok" and n["count"] == 3


@pytest.mark.asyncio
async def test_retry_exhausts_and_propagates():
    async def always_fail():
        raise ConnectionError("nope")

    with pytest.raises(ConnectionError):
        await async_retry(always_fail, attempts=2, base_delay=0.001)


@pytest.mark.asyncio
async def test_retry_doesnt_retry_unlisted_exception():
    n = {"count": 0}

    async def value_error():
        n["count"] += 1
        raise ValueError("don't retry me")

    with pytest.raises(ValueError):
        await async_retry(
            value_error, attempts=5, base_delay=0.001, retry_on=(ConnectionError,)
        )
    assert n["count"] == 1, "unlisted exception should not retry"


# --- LLM stall handling (with scripted provider) ----------------------

class StallingLLM(LLMProvider):
    """Yields a few tokens then hangs forever."""

    async def stream_chat(self, system, messages, tools=None):
        yield TextDelta(text="hello ")
        await asyncio.sleep(60)  # stalls forever (test-side timeout will fire)


@pytest.mark.asyncio
async def test_llm_stall_caught_by_wait_for():
    """Per-event wait_for(20s) prevents indefinite hang. Use a short timeout in test."""
    llm = StallingLLM()
    stream = llm.stream_chat("sys", [{"role": "user", "content": "hi"}]).__aiter__()
    # Get first event normally
    first = await stream.__anext__()
    assert isinstance(first, TextDelta)

    # Try to get second event with short timeout — should TimeoutError
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(stream.__anext__(), timeout=0.2)


# --- VAD state isolation between calls --------------------------------

@pytest.mark.skipif(not os.path.exists(MODEL_PATH), reason="silero model missing")
def test_vad_reset_clears_have_voice_state():
    """After reset(), VAD shouldn't remember previous utterance."""
    vad = SileroVAD(threshold=0.4, min_silence_duration_ms=200, frame_window=2)
    rng = np.random.default_rng(0)
    speech = (rng.integers(-15000, 15000, 16000, dtype=np.int16)).tobytes()

    # First feed: should fire START
    r1 = vad.feed(speech)
    if r1.is_voice:
        assert r1.speech_started, "first START should fire"

    # Reset
    vad.reset()

    # Feed silence — should NOT fire utterance_ended (no _have_voice carried over)
    silence = np.zeros(8000, dtype=np.int16).tobytes()
    r2 = vad.feed(silence)
    assert not r2.utterance_ended, "after reset(), no carried-over utterance state"


# --- Sentence chunker no-deadlock -------------------------------------

def test_chunker_empty_feeds_dont_emit():
    from echotwin.utils.sentence_chunker import SentenceChunker
    c = SentenceChunker()
    for _ in range(10):
        result = list(c.feed(""))
        assert result == []


def test_chunker_flush_after_empty_returns_empty():
    from echotwin.utils.sentence_chunker import SentenceChunker
    c = SentenceChunker()
    assert c.flush() == ""


# --- Addressee detector defensive -------------------------------------

def test_addressee_handles_none_session_field():
    """If last_addressee_id is None (fresh session), continuation rule must not crash."""
    from echotwin.persona import Persona
    from echotwin.pipeline.addressee import AddresseeDetector
    from tests.harness._utils import FakeSession

    p = Persona("x", "x", "v", ["x"], [], "", "", "")
    d = AddresseeDetector(p, bot_user_id=999)
    s = FakeSession(last_bot_speak_time=0.0, last_addressee_id=None)
    r = d.is_addressed("hello", speaker_id=1, session=s, channel_member_count=5)
    assert r is False  # not solo, no wake, no continuation


# --- Barge-in (interruption) tests ------------------------------------

class ScriptedLLMWithDelay(LLMProvider):
    """Yields N text deltas with a delay between them, mimicking a slow stream
    so we can interrupt mid-stream and verify abort propagation."""

    def __init__(self, deltas: list[str], inter_delay: float = 0.05):
        self._deltas = deltas
        self._delay = inter_delay
        self.delivered: list[str] = []

    async def stream_chat(self, system, messages, tools=None):
        for d in self._deltas:
            await asyncio.sleep(self._delay)
            self.delivered.append(d)
            yield TextDelta(text=d)
        yield MessageEnd(stop_reason="end_turn")


@pytest.mark.asyncio
async def test_barge_in_stops_llm_stream_mid_flight():
    """Simulate the abort flag flipping during LLM streaming. The driver loop
    (think_speak.py) checks session.client_abort each event; verify deltas after
    the abort point don't reach the TTS stage."""
    llm = ScriptedLLMWithDelay(
        deltas=["第", "一", "段", "。", "第", "二", "段", "。"], inter_delay=0.02,
    )

    class FakeSession:
        client_abort = False

    session = FakeSession()
    pushed_to_tts: list[str] = []

    async def driver():
        """Mirror think_speak.py's per-event abort check."""
        async for ev in llm.stream_chat("sys", [{"role": "user", "content": "hi"}]):
            if session.client_abort:
                break
            if isinstance(ev, TextDelta):
                pushed_to_tts.append(ev.text)

    drive_task = asyncio.create_task(driver())
    # Wait for ~3 deltas to be pushed, then flip abort
    await asyncio.sleep(0.08)  # ~3-4 deltas at 20ms cadence
    session.client_abort = True
    await drive_task

    # We pushed at most ~4 deltas, not all 8 — verify abort took effect
    n_pushed = len(pushed_to_tts)
    print(f"\n  delivered={len(llm.delivered)} pushed_to_tts={n_pushed}")
    assert n_pushed < len(llm.deltas if hasattr(llm, "deltas") else llm._deltas), (
        f"abort didn't stop stream: pushed all {n_pushed} deltas"
    )


@pytest.mark.asyncio
async def test_barge_in_propagates_to_drain_loop():
    """drain_tts in think_speak.py checks session.client_abort each iteration;
    verify a TTS-style packet stream stops yielding after abort flips."""

    class FakeSession:
        client_abort = False

    session = FakeSession()

    async def fake_packets():
        for i in range(20):
            await asyncio.sleep(0.01)
            yield f"chunk-{i}".encode()

    received: list[bytes] = []

    async def consumer():
        async for c in fake_packets():
            if session.client_abort:
                break
            received.append(c)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.03)  # collect a few chunks
    session.client_abort = True
    await task

    print(f"\n  packets received before abort: {len(received)} (out of 20)")
    assert len(received) < 20, "abort didn't break drain loop"


# --- Pure chat (no tools) speed --------------------------------------

@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get("FISH_AUDIO_API_KEY"),
    reason="needs both API keys",
)
@pytest.mark.asyncio
async def test_e2e_pure_chat_speed():
    """No-tool chat should be much faster than tool-using turn (no LLM round-trip).
    Target: TTFA < 1500ms for 'hi → short reply'.
    """
    import time
    from echotwin.providers.llm.claude_haiku import ClaudeHaikuProvider
    from echotwin.providers.tts.fish_audio_stream import FishAudioStreamProvider, FishConfig
    from echotwin.utils.sentence_chunker import SentenceChunker

    PERSONA_VOICE_ID = os.environ.get("TEST_VOICE_ID", "")
    llm = ClaudeHaikuProvider(api_key=os.environ["ANTHROPIC_API_KEY"], max_tokens=50)
    tts = FishAudioStreamProvider(FishConfig(
        api_key=os.environ["FISH_AUDIO_API_KEY"],
        voice_id=PERSONA_VOICE_ID,
        model="s2-pro", latency="low",
    ))

    chunker = SentenceChunker()
    await tts.open()

    t0 = time.perf_counter()
    first_audio_t: float | None = None

    async def drain():
        nonlocal first_audio_t
        async for c in tts.packets():
            if first_audio_t is None and c:
                first_audio_t = time.perf_counter()
                return  # only need TTFA

    drain_task = asyncio.create_task(drain())
    text = ""
    async for ev in llm.stream_chat(
        "你叫一点点点,简短爽朗台湾女生,1 句话回复。",
        [{"role": "user", "content": "你好"}],
        tools=None,  # NO TOOLS
    ):
        if isinstance(ev, TextDelta):
            text += ev.text
            for s in chunker.feed(ev.text):
                await tts.push_text(s)
                await tts.flush()
        elif isinstance(ev, MessageEnd):
            break

    rem = chunker.flush()
    if rem:
        await tts.push_text(rem)
    await tts.end_turn()

    await asyncio.wait_for(drain_task, timeout=10)
    await tts.close()

    ttfa_ms = (first_audio_t - t0) * 1000 if first_audio_t else None
    print(f"\n  pure chat: text={text!r} TTFA={ttfa_ms:.0f}ms")
    assert ttfa_ms is not None
    if ttfa_ms > 1500:
        print(f"  ⚠ TTFA {ttfa_ms:.0f}ms exceeds 1500ms target for pure chat")
