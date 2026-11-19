import asyncio

import pytest

from app.main import create_app
from app.tools.effects import ReconcileResult
from app.tools.execute import execute_tool
from app.tools.registry import Tool, ToolRegistry
from app.tools.weather import get_weather

SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}


def _tool(handler, *, kind="read", reconcile=None, timeout_seconds=30.0, name="get_weather") -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name=name,
            description="测试工具",
            input_schema=SCHEMA,
            kind=kind,
            handler=handler,
            timeout_seconds=timeout_seconds,
            reconcile=reconcile,
        )
    )
    return registry


async def test_weather_answers_with_the_city() -> None:
    assert await get_weather("北京") == "北京今天晴，31度。"


def test_app_registers_weather() -> None:
    tool = create_app().state.tools.get("get_weather")
    assert tool.kind == "read"
    assert "city" in tool.input_schema["properties"]


@pytest.mark.asyncio
async def test_unknown_and_invalid_arguments_do_not_call_the_handler() -> None:
    called = 0

    async def handler(city: str) -> str:
        nonlocal called
        called += 1
        return city

    tools = _tool(handler)
    missing = await execute_tool(tools, "run_1", "c1", "missing", "{}")
    broken = await execute_tool(tools, "run_1", "c1", "get_weather", '{"city":')
    rejected = await execute_tool(tools, "run_1", "c1", "get_weather", "{}")
    assert missing["ok"] is False
    assert "未注册的工具: missing" in missing["output"]
    assert "JSON" in broken["output"]
    assert "schema" in rejected["output"]
    assert called == 0


@pytest.mark.asyncio
async def test_read_retries_once_inside_the_timeout_budget() -> None:
    calls = 0

    async def flaky(city: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("暂时不可用")
        return "晴"

    tools = _tool(flaky)
    result = await execute_tool(tools, "run_1", "c1", "get_weather", '{"city":"北京"}')
    assert result == {"ok": True, "output": "晴"}
    assert calls == 2

    calls = 0

    async def slow(city: str) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)
        return city

    timed_out = await execute_tool(
        _tool(slow, timeout_seconds=0.1),
        "run_1",
        "c1",
        "get_weather",
        '{"city":"北京"}',
    )
    assert timed_out["ok"] is False
    assert "超时" in timed_out["output"]
    assert calls == 2


@pytest.mark.asyncio
async def test_write_reuses_the_same_result_for_the_same_arguments() -> None:
    keys: list[str] = []

    async def save(city: str, idempotency_key: str) -> str:
        keys.append(idempotency_key)
        return "已写入"

    tools = _tool(save, kind="write", name="save_note")
    first = await execute_tool(tools, "run_1", "c1", "save_note", '{"city":"北京"}')
    second = await execute_tool(tools, "run_1", "c2", "save_note", '{"city": "北京"}')
    assert first == second == {"ok": True, "output": "已写入"}
    assert len(keys) == 1
    assert len(keys[0]) == 64


@pytest.mark.asyncio
async def test_write_timeout_reconciles_before_a_second_send() -> None:
    calls: list[str] = []

    async def save(city: str, idempotency_key: str) -> str:
        calls.append(idempotency_key)
        if len(calls) == 1:
            await asyncio.sleep(1)
        return "已写入"

    async def unknown(_key: str) -> ReconcileResult:
        return ReconcileResult()

    tools = _tool(save, kind="write", name="save_note", timeout_seconds=0.05, reconcile=unknown)
    first = await execute_tool(tools, "run_1", "c1", "save_note", '{"city":"北京"}')
    second = await execute_tool(tools, "run_1", "c2", "save_note", '{"city":"北京"}')
    assert first["ok"] is False
    assert "结果不明" in first["output"]
    assert second == first
    assert len(calls) == 1

    calls.clear()

    async def absent(_key: str) -> ReconcileResult:
        return ReconcileResult(absent=True)

    async def found(_key: str) -> ReconcileResult:
        return ReconcileResult(found=True, output="下游已有")

    absent_tools = _tool(save, kind="write", name="save_note", timeout_seconds=0.05, reconcile=absent)
    saved = await execute_tool(absent_tools, "run_1", "c1", "save_note", '{"city":"北京"}')
    assert saved == {"ok": True, "output": "已写入"}
    assert len(calls) == 2
    assert calls[0] == calls[1]

    calls.clear()
    found_tools = _tool(save, kind="write", name="save_note", timeout_seconds=0.05, reconcile=found)
    seen = await execute_tool(found_tools, "run_1", "c1", "save_note", '{"city":"北京"}')
    assert seen == {"ok": True, "output": "下游已有"}
    assert len(calls) == 1
