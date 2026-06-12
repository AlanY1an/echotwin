"""ClaudeHaikuProvider — MessageEnd must carry token usage (the data source for cost tracking).

Historical bug: usage was never extracted, CostTracker.record had zero call sites
in the entire codebase, and the quota system was effectively a no-op.
"""
from types import SimpleNamespace

from echotwin.providers.llm.base import MessageEnd, TextDelta
from echotwin.providers.llm.claude_haiku import ClaudeHaikuProvider


def _sdk_events():
    """Simulate the raw Anthropic SDK event sequence (including usage)."""
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=1,
                    cache_creation_input_tokens=5,
                    cache_read_input_tokens=10,
                )
            ),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="text"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="你好"),
        ),
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=50),
        ),
        SimpleNamespace(type="message_stop"),
    ]


class _FakeStream:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e

        return gen()


class _FakeMessages:
    def __init__(self, events):
        self._events = events

    def stream(self, **kwargs):
        return _FakeStream(self._events)


async def test_message_end_carries_usage():
    provider = ClaudeHaikuProvider(api_key="test-key")
    provider._client = SimpleNamespace(messages=_FakeMessages(_sdk_events()))

    events = [ev async for ev in provider.stream_chat("sys", [{"role": "user", "content": "hi"}])]

    assert any(isinstance(e, TextDelta) and e.text == "你好" for e in events)
    ends = [e for e in events if isinstance(e, MessageEnd)]
    assert len(ends) == 1
    end = ends[0]
    assert end.stop_reason == "end_turn"
    assert end.input_tokens == 100
    assert end.output_tokens == 50  # cumulative value from message_delta, not the 1 from message_start
    assert end.cache_creation_input_tokens == 5
    assert end.cache_read_input_tokens == 10


class _CapturingMessages:
    def __init__(self, events):
        self._events = events
        self.kwargs = None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return _FakeStream(self._events)


async def test_cache_breakpoint_applied_to_tool_use_assistant():
    """Assistant messages from tool rounds (content is a block list) must also get a cache
    breakpoint, otherwise the second round of two-round calls like weather lookups
    re-pays the full input tokens."""
    provider = ClaudeHaikuProvider(api_key="k")
    cap = _CapturingMessages(_sdk_events())
    provider._client = SimpleNamespace(messages=cap)

    history = [
        {"role": "user", "content": "查天气"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "我看看"},
                {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {}},
            ],
        },
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "晴 25 度"}
        ]},
    ]
    async for _ in provider.stream_chat("sys", history):
        pass

    sent_assistant = cap.kwargs["messages"][1]
    assert sent_assistant["content"][-1].get("cache_control") == {"type": "ephemeral"}
    # The shared original history must not be mutated in place (session.dialogue holds the same dicts)
    assert "cache_control" not in history[1]["content"][-1]
