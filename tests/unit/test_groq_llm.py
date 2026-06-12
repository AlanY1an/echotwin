"""GroqProvider — OpenAI-compatible chat completion → typed event stream (arbiter-only)."""
import pytest

from echotwin.providers.llm.groq import GroqProvider
from echotwin.providers.llm.base import MessageEnd, TextDelta


@pytest.fixture
def provider(monkeypatch):
    p = GroqProvider(api_key="gsk_test", model="qwen/qwen3-32b")

    async def fake_post(body):
        fake_post.body = body
        return {
            "choices": [{"message": {"content": '{"verdict":"reject","reason":"x"}'}}],
            "usage": {"prompt_tokens": 150, "completion_tokens": 15},
        }

    monkeypatch.setattr(p, "_post", fake_post)
    p._fake_post = fake_post
    return p


async def test_stream_chat_yields_typed_events(provider):
    events = [
        ev async for ev in provider.stream_chat("system提示", [{"role": "user", "content": "嗨"}])
    ]
    assert isinstance(events[0], TextDelta)
    assert events[0].text == '{"verdict":"reject","reason":"x"}'
    end = events[-1]
    assert isinstance(end, MessageEnd)
    assert end.stop_reason == "end_turn"
    assert end.input_tokens == 150 and end.output_tokens == 15, "usage 必须进 MessageEnd,否则记账失明"


async def test_request_body_shape(provider):
    async for _ in provider.stream_chat("sys", [{"role": "user", "content": "嗨"}]):
        pass
    body = provider._fake_post.body
    assert body["model"] == "qwen/qwen3-32b"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["temperature"] == 0
    assert body["reasoning_effort"] == "none", "qwen3 必须关思考,否则吐 <think> 拖延迟"
