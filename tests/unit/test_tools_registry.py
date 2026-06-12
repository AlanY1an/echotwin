import pytest

from echotwin.tools.base import Tool, ToolError
from echotwin.tools.registry import ToolRegistry


class T1(Tool):
    name = "t1"
    description = "first"
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, args):
        return "result1"


class T2(Tool):
    name = "t2"
    description = "second"
    input_schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }

    async def execute(self, args):
        if args.get("q") == "raise":
            raise ToolError("bad")
        if args.get("q") == "boom":
            raise RuntimeError("internal")
        return f"echo:{args.get('q','')}"


def test_register_and_list():
    r = ToolRegistry()
    r.register(T1())
    r.register(T2())
    names = {t["name"] for t in r.to_anthropic_tools()}
    assert names == {"t1", "t2"}
    assert set(r.names()) == {"t1", "t2"}


def test_register_duplicate_raises():
    r = ToolRegistry()
    r.register(T1())
    with pytest.raises(ValueError):
        r.register(T1())


@pytest.mark.asyncio
async def test_execute_dispatches():
    r = ToolRegistry()
    r.register(T1())
    r.register(T2())
    assert (await r.execute("t1", {})) == "result1"
    assert (await r.execute("t2", {"q": "hi"})) == "echo:hi"


@pytest.mark.asyncio
async def test_execute_unknown_returns_error():
    r = ToolRegistry()
    res = await r.execute("nope", {})
    assert "unknown" in res.lower() or "not found" in res.lower()


@pytest.mark.asyncio
async def test_execute_tool_error_returns_message():
    r = ToolRegistry()
    r.register(T2())
    res = await r.execute("t2", {"q": "raise"})
    assert "bad" in res


@pytest.mark.asyncio
async def test_execute_unexpected_error_caught():
    r = ToolRegistry()
    r.register(T2())
    res = await r.execute("t2", {"q": "boom"})
    assert "Error" in res
    assert "RuntimeError" in res or "failed" in res
