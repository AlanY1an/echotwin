"""End-to-end tool round-trip:
  scripted LLM emits ToolUseStart/InputDelta/End → registry executes tool →
  second turn produces final text.

Mirrors the inner loop of think_speak.py:_run_llm_with_tools without the TTS+Discord
glue, so we can verify the tool-use control flow in isolation.
"""
import json

import pytest

from echotwin.providers.llm.base import (
    LLMProvider,
    MessageEnd,
    TextDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from echotwin.tools.base import Tool
from echotwin.tools.registry import ToolRegistry


class FakeWeather(Tool):
    name = "get_weather"
    description = "fake"
    input_schema = {"type": "object", "properties": {"city": {"type": "string"}}}

    async def execute(self, args):
        return f"{args.get('city','?')} 晴 25 度"


class ScriptedLLM(LLMProvider):
    def __init__(self, scripts):
        self._scripts = scripts
        self._call = 0

    async def stream_chat(self, system, messages, tools=None):
        events = self._scripts[self._call]
        self._call += 1
        for ev in events:
            yield ev


@pytest.mark.asyncio
async def test_tool_use_round_trip():
    reg = ToolRegistry()
    reg.register(FakeWeather())
    llm = ScriptedLLM(
        [
            # Round 1: model decides to call tool
            [
                ToolUseStart(tool_use_id="tu1", name="get_weather"),
                ToolUseInputDelta(tool_use_id="tu1", partial_json='{"city":'),
                ToolUseInputDelta(tool_use_id="tu1", partial_json='"台北"}'),
                ToolUseEnd(tool_use_id="tu1"),
                MessageEnd(stop_reason="tool_use"),
            ],
            # Round 2: model summarizes the tool result
            [
                TextDelta(text="台北今天晴 25 度,出门记得防晒"),
                MessageEnd(stop_reason="end_turn"),
            ],
        ]
    )

    cur_messages = [{"role": "user", "content": "台北天气"}]
    full_response = ""

    for _round in range(5):
        cur_tool_uses = []
        text_round = ""
        stop_reason = "end_turn"
        async for ev in llm.stream_chat("sys", cur_messages, tools=reg.to_anthropic_tools()):
            if isinstance(ev, TextDelta):
                full_response += ev.text
                text_round += ev.text
            elif isinstance(ev, ToolUseStart):
                cur_tool_uses.append({"id": ev.tool_use_id, "name": ev.name, "partial_json": ""})
            elif isinstance(ev, ToolUseInputDelta):
                cur_tool_uses[-1]["partial_json"] += ev.partial_json
            elif isinstance(ev, MessageEnd):
                stop_reason = ev.stop_reason

        if stop_reason == "tool_use" and cur_tool_uses:
            asst_blocks = []
            if text_round:
                asst_blocks.append({"type": "text", "text": text_round})
            for tu in cur_tool_uses:
                asst_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tu["id"],
                        "name": tu["name"],
                        "input": json.loads(tu["partial_json"]),
                    }
                )
            cur_messages = cur_messages + [{"role": "assistant", "content": asst_blocks}]
            results = []
            for tu in cur_tool_uses:
                args = json.loads(tu["partial_json"])
                r = await reg.execute(tu["name"], args)
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": r})
            cur_messages = cur_messages + [{"role": "user", "content": results}]
            continue
        break

    assert "台北" in full_response
    assert "25" in full_response
