import pytest

from echotwin.tools.base import Tool, ToolError, tool_to_anthropic


class FakeTool(Tool):
    name = "fake"
    description = "test"
    input_schema = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    }

    async def execute(self, args):
        if args.get("x") == "fail":
            raise ToolError("oops")
        return f"ok:{args['x']}"


@pytest.mark.asyncio
async def test_execute_success():
    r = await FakeTool().execute({"x": "hi"})
    assert r == "ok:hi"


@pytest.mark.asyncio
async def test_execute_raises_tool_error():
    with pytest.raises(ToolError):
        await FakeTool().execute({"x": "fail"})


def test_tool_to_anthropic_dict():
    d = tool_to_anthropic(FakeTool())
    assert d["name"] == "fake"
    assert d["description"] == "test"
    assert d["input_schema"]["properties"]["x"]["type"] == "string"
