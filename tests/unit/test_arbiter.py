"""Gray-zone LLM arbitration — verdict JSON parsing / timeout fallback / cost accounting."""
import asyncio

from echotwin.pipeline.arbiter import arbitrate
from echotwin.pipeline.organic import Verdict
from echotwin.providers.llm.base import MessageEnd, TextDelta


class FakeLLM:
    def __init__(self, text, delay=0.0, usage=None):
        self._text = text
        self._delay = delay
        self._usage = usage or {}
        self.seen = None

    def stream_chat(self, system, messages, tools=None):
        self.seen = {"system": system, "messages": messages}

        async def gen():
            if self._delay:
                await asyncio.sleep(self._delay)
            yield TextDelta(text=self._text)
            yield MessageEnd(stop_reason="end_turn", **self._usage)

        return gen()


class FakeTracker:
    def __init__(self):
        self.records = []

    async def record(self, kind, amount, **ids):
        self.records.append((kind, amount))


def _kw(**over):
    kw = dict(
        bot_name="Hinata",
        speaker="Alan",
        utterance="你能不能在这版本的结束啊",
        room_lines=["小雨: 这把好难", "阿伟: 我在收拾东西"],
        last_bot_reply="哈哈这把好精彩呀",
        last_addressee="小雨",
        in_window=True,
        clarify_pending=False,
        timeout=1.0,
    )
    kw.update(over)
    return kw


async def test_arbitrate_parses_verdict():
    llm = FakeLLM('{"verdict":"reject","reason":"在和阿伟说话"}')
    verdict, reason = await arbitrate(llm, **_kw())
    assert verdict == Verdict.REJECT
    assert "阿伟" in reason
    # Live context must make it into the payload
    assert "你能不能在这版本的结束啊" in llm.seen["messages"][0]["content"]
    assert "哈哈这把好精彩呀" in llm.seen["messages"][0]["content"]


async def test_arbitrate_tolerates_wrapped_json():
    llm = FakeLLM('好的。{"verdict":"accept","reason":"在追问bot"}')
    verdict, _ = await arbitrate(llm, **_kw())
    assert verdict == Verdict.ACCEPT


async def test_arbitrate_malformed_returns_none():
    llm = FakeLLM("这句应该接")  # no JSON
    assert await arbitrate(llm, **_kw()) is None


async def test_arbitrate_timeout_returns_none():
    llm = FakeLLM('{"verdict":"accept","reason":"x"}', delay=0.5)
    assert await arbitrate(llm, **_kw(timeout=0.05)) is None


async def test_arbitrate_records_cost():
    """Every new paid path must record costs (CLAUDE.md rule), or the quota guard goes blind."""
    llm = FakeLLM(
        '{"verdict":"reject","reason":"x"}',
        usage={"input_tokens": 600, "output_tokens": 20},
    )
    tracker = FakeTracker()
    await arbitrate(llm, **_kw(cost_tracker=tracker, ids={"guild_id": "1"}))
    kinds = {k for k, _ in tracker.records}
    assert "claude_haiku_4_5_input" in kinds
    assert "claude_haiku_4_5_output" in kinds


async def test_arbitrate_strips_think_block():
    """qwen3 think blocks often contain braces; strip them before searching for JSON."""
    llm = FakeLLM('<think>嗯,这里有个{歧义}…</think>{"verdict":"reject","reason":"解说"}')
    verdict, reason = await arbitrate(llm, **_kw())
    assert verdict == Verdict.REJECT and reason == "解说"


async def test_arbitrate_cost_prefix_switches_kinds():
    """Switching the arbiter model must switch the cost-accounting keys too (pricing.py has matching entries)."""
    from echotwin.cost.pricing import PRICING

    llm = FakeLLM(
        '{"verdict":"reject","reason":"x"}',
        usage={"input_tokens": 150, "output_tokens": 15},
    )
    tracker = FakeTracker()
    await arbitrate(
        llm, **_kw(cost_tracker=tracker, ids={}, cost_prefix="groq_qwen3_32b")
    )
    kinds = {k for k, _ in tracker.records}
    assert kinds == {"groq_qwen3_32b_input", "groq_qwen3_32b_output"}
    assert all(k in PRICING for k in kinds), "记账 kind 必须在价目表里,否则成本算零"
