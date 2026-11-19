import asyncio
import copy

import pytest

from app.loop.run import execute_run
from app.model.events import ModelFailed, ModelFinished, ModelUsageEvent, TextDelta, ToolCall
from app.store.memory import MemoryStore
from app.tools.registry import Tool, ToolRegistry
from app.tools.weather import get_weather, register_weather

SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}


class ScriptedModel:
    def __init__(self, scripts: list[list]) -> None:
        self.scripts = scripts
        self.calls: list[dict] = []

    async def stream(self, messages, *, cancel, idempotency_key, tools=None):
        if len(self.calls) >= len(self.scripts):
            raise AssertionError("模型调用次数超过剧本")
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "key": idempotency_key,
                "cancel": cancel,
                "tools": copy.deepcopy(tools),
            }
        )
        for event in self.scripts[len(self.calls) - 1]:
            if cancel.is_set():
                return
            yield event


def _store() -> MemoryStore:
    store = MemoryStore()
    store.add_message("th_1", "user", "北京今天热吗")
    store.create_run("th_1", "run_1")
    return store


def _types(store: MemoryStore) -> list[str]:
    return [event.type for event in store.since("run_1", 0)]


def _finished(reason: str) -> ModelFinished:
    return ModelFinished(type="model.finished", finish_reason=reason)


def _usage(input_tokens: int, output_tokens: int) -> ModelUsageEvent:
    return ModelUsageEvent(type="model.usage", input_tokens=input_tokens, output_tokens=output_tokens)


def _call(call_id: str, name: str, arguments: str) -> ToolCall:
    return ToolCall(type="tool_call", call_id=call_id, name=name, arguments=arguments)


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


@pytest.mark.asyncio
async def test_weather_run_records_the_tool_before_the_answer() -> None:
    order: list[str] = []

    async def weather(city: str) -> str:
        order.append("tool")
        return f"{city}今天晴，31度。"

    tools = _registry(
        Tool(
            name="get_weather",
            description="查询城市当前天气。只读，不会修改数据。调用前需要城市名。",
            input_schema=SCHEMA,
            kind="read",
            handler=weather,
        )
    )
    class MarkingModel(ScriptedModel):
        async def stream(self, messages, *, cancel, idempotency_key, tools=None):
            first = not self.calls
            async for event in super().stream(
                messages,
                cancel=cancel,
                idempotency_key=idempotency_key,
                tools=tools,
            ):
                yield event
                if first and event.type == "tool_call":
                    order.append("before-finish")

    model = MarkingModel(
        [
            [
                _call("c1", "get_weather", '{"city":"北京"}'),
                _finished("tool_use"),
                _usage(2, 1),
            ],
            [
                TextDelta(type="text.delta", delta=""),
                TextDelta(type="text.delta", delta="北京"),
                TextDelta(type="text.delta", delta="今天 31 度"),
                _finished("stop"),
                _usage(4, 3),
            ],
        ]
    )
    store = _store()
    await execute_run(store, model, tools, "run_1")

    events = store.since("run_1", 0)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert _types(store) == [
        "run.started",
        "tool.started",
        "tool.arguments.done",
        "tool.result",
        "message.started",
        "message.delta",
        "message.delta",
        "message.completed",
        "run.completed",
    ]
    assert order == ["before-finish", "tool"]
    assert events[3].ok is True
    assert events[3].output == "北京今天晴，31度。"
    assert [event.delta for event in events if event.type == "message.delta"] == ["北京", "今天 31 度"]
    assert events[-1].usage.input_tokens == 6
    assert events[-1].usage.output_tokens == 4
    assert store.get_run("run_1").status == "completed"
    assert model.calls[0]["key"] == "run_1:0"
    assert model.calls[1]["key"] == "run_1:1"
    assert model.calls[0]["cancel"] is store.get_run("run_1").cancel_event
    spec = model.calls[0]["tools"][0]["function"]
    assert spec["name"] == "get_weather"
    assert spec["parameters"]["required"] == ["city"]
    assert model.calls[1]["messages"][2]["tool_call_id"] == "c1"
    assert store.messages("th_1")[-1] == {"role": "assistant", "content": "北京今天 31 度"}


@pytest.mark.asyncio
async def test_supplier_tool_calls_reason_runs_the_tool() -> None:
    calls = 0

    async def weather(city: str) -> str:
        nonlocal calls
        calls += 1
        return city

    tools = _registry(
        Tool(
            name="get_weather",
            description="查询天气",
            input_schema=SCHEMA,
            kind="read",
            handler=weather,
        )
    )
    model = ScriptedModel(
        [
            [_call("c1", "get_weather", '{"city":"北京"}'), _finished("tool_calls")],
            [TextDelta(type="text.delta", delta="好"), _finished("stop")],
        ]
    )
    store = _store()
    await execute_run(store, model, tools, "run_1")
    assert calls == 1
    assert _types(store)[-1] == "run.completed"


@pytest.mark.asyncio
async def test_tool_failure_is_returned_to_the_model() -> None:
    async def broken(city: str) -> str:
        raise RuntimeError("城市名无法识别")

    tools = _registry(
        Tool(
            name="get_weather",
            description="查询天气",
            input_schema=SCHEMA,
            kind="read",
            handler=broken,
        )
    )
    model = ScriptedModel(
        [
            [_call("c1", "get_weather", '{"city":"北京"}'), _finished("tool_use")],
            [TextDelta(type="text.delta", delta="换个说法"), _finished("stop")],
        ]
    )
    store = _store()
    await execute_run(store, model, tools, "run_1")
    result = next(event for event in store.since("run_1", 0) if event.type == "tool.result")
    assert result.ok is False
    assert "城市名无法识别" in result.output
    assert "可以修改参数或换一个工具" in result.output
    assert model.calls[1]["messages"][2]["tool_call_id"] == "c1"
    assert store.get_run("run_1").status == "completed"


@pytest.mark.asyncio
async def test_unknown_tool_does_not_fail_the_run() -> None:
    tools = ToolRegistry()
    register_weather(tools)
    model = ScriptedModel(
        [
            [_call("c1", "missing", "{}"), _finished("tool_use")],
            [TextDelta(type="text.delta", delta="好"), _finished("stop")],
        ]
    )
    store = _store()
    await execute_run(store, model, tools, "run_1")
    result = next(event for event in store.since("run_1", 0) if event.type == "tool.result")
    assert result.ok is False
    assert "未注册的工具: missing" in result.output
    assert store.get_run("run_1").status == "completed"


@pytest.mark.asyncio
async def test_cancel_during_text_stops_the_upstream() -> None:
    class PausingModel:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def stream(self, messages, *, cancel, idempotency_key, tools=None):
            self.calls += 1
            yield TextDelta(type="text.delta", delta="北京")
            self.started.set()
            await self.release.wait()
            if cancel.is_set():
                return
            yield TextDelta(type="text.delta", delta="今天")
            yield _finished("stop")

    model = PausingModel()
    store = _store()
    task = asyncio.create_task(execute_run(store, model, ToolRegistry(), "run_1"))
    await model.started.wait()
    store.request_cancel("run_1")
    model.release.set()
    await task
    assert model.calls == 1
    assert [event.delta for event in store.since("run_1", 0) if event.type == "message.delta"] == ["北京"]
    assert _types(store)[-1] == "run.cancelled"
    assert store.get_run("run_1").status == "cancelled"
    assert not any(event.type == "model.failed" for event in store.since("run_1", 0))


@pytest.mark.asyncio
async def test_cancel_before_the_tool_is_sent() -> None:
    calls = 0

    async def weather(city: str) -> str:
        nonlocal calls
        calls += 1
        return city

    class CancelModel:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, messages, *, cancel, idempotency_key, tools=None):
            self.calls += 1
            yield _call("c1", "get_weather", '{"city":"北京"}')
            cancel.set()

    tools = _registry(
        Tool(
            name="get_weather",
            description="查询天气",
            input_schema=SCHEMA,
            kind="read",
            handler=weather,
        )
    )
    model = CancelModel()
    store = _store()
    await execute_run(store, model, tools, "run_1")
    assert calls == 0
    assert model.calls == 1
    assert _types(store) == ["run.started", "tool.started", "tool.arguments.done", "run.cancelled"]


@pytest.mark.asyncio
async def test_cancel_during_tool_keeps_the_result() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def weather(city: str) -> str:
        started.set()
        await release.wait()
        return "晴"

    class OnceModel:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, messages, *, cancel, idempotency_key, tools=None):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("模型调用次数超过剧本")
            yield _call("c1", "get_weather", '{"city":"北京"}')
            yield _finished("tool_use")

    tools = _registry(
        Tool(
            name="get_weather",
            description="查询天气",
            input_schema=SCHEMA,
            kind="read",
            handler=weather,
        )
    )
    model = OnceModel()
    store = _store()
    task = asyncio.create_task(execute_run(store, model, tools, "run_1"))
    await started.wait()
    store.request_cancel("run_1")
    release.set()
    await task
    events = store.since("run_1", 0)
    assert [event.type for event in events[-2:]] == ["tool.result", "run.cancelled"]
    assert events[-2].ok is True
    assert events[-2].output == "晴"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_turn_limit_keeps_the_tool_result() -> None:
    model = ScriptedModel([[_call("c1", "get_weather", '{"city":"北京"}'), _finished("tool_use")]])
    tools = _registry(
        Tool(
            name="get_weather",
            description="查询天气",
            input_schema=SCHEMA,
            kind="read",
            handler=get_weather,
        )
    )
    store = _store()
    await execute_run(store, model, tools, "run_1", max_turns=1)
    failed = store.since("run_1", 0)[-1]
    assert failed.type == "run.failed"
    assert failed.code == "max_turns"
    assert failed.retryable is False
    assert any(event.type == "tool.result" and event.ok for event in store.since("run_1", 0))
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["length", "content_filter"])
async def test_truncated_or_filtered_output_ends_the_run(reason: str) -> None:
    model = ScriptedModel(
        [[
            TextDelta(type="text.delta", delta="半截"),
            _finished(reason),
            TextDelta(type="text.delta", delta="不该出现"),
        ]]
    )
    store = _store()
    await execute_run(store, model, ToolRegistry(), "run_1")
    events = store.since("run_1", 0)
    assert [event.delta for event in events if event.type == "message.delta"] == ["半截"]
    assert events[-1].type == "run.failed"
    assert events[-1].code == reason
    assert events[-1].retryable is False
    assert len(model.calls) == 1
    assert store.messages("th_1") == [{"role": "user", "content": "北京今天热吗"}]


@pytest.mark.asyncio
async def test_model_failure_keeps_the_delta_and_does_not_continue() -> None:
    model = ScriptedModel(
        [
            [
                TextDelta(type="text.delta", delta="今天"),
                ModelFailed(
                    type="model.failed",
                    code="timeout",
                    message="上游中断",
                    retryable=True,
                    phase="after_content",
                ),
            ],
            [TextDelta(type="text.delta", delta="不该再叫"), _finished("stop")],
        ]
    )
    store = _store()
    await execute_run(store, model, ToolRegistry(), "run_1")
    events = store.since("run_1", 0)
    assert [event.delta for event in events if event.type == "message.delta"] == ["今天"]
    assert events[-1].type == "run.failed"
    assert events[-1].code == "timeout"
    assert events[-1].retryable is True
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_repeated_call_stops_the_run() -> None:
    calls = 0

    async def weather(city: str) -> str:
        nonlocal calls
        calls += 1
        return "晴"

    tools = _registry(
        Tool(
            name="get_weather",
            description="查询天气",
            input_schema=SCHEMA,
            kind="read",
            handler=weather,
        )
    )
    model = ScriptedModel(
        [
            [_call("c1", "get_weather", '{"city":"北京"}'), _finished("tool_use")],
            [_call("c2", "get_weather", '{"city": "北京"}'), _finished("tool_use")],
            [TextDelta(type="text.delta", delta="不该再叫"), _finished("stop")],
        ]
    )
    store = _store()
    await execute_run(store, model, tools, "run_1")
    failed = store.since("run_1", 0)[-1]
    assert failed.type == "run.failed"
    assert failed.code == "repeated_call"
    assert failed.retryable is False
    assert calls == 2
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_reads_run_together_and_writes_run_in_order() -> None:
    gate = asyncio.Event()
    started: list[str] = []
    write_order: list[str] = []

    async def read_city(city: str) -> str:
        started.append(city)
        if len(started) == 2:
            gate.set()
        await gate.wait()
        return city

    async def write_city(city: str, idempotency_key: str) -> str:
        write_order.append(f"{city}:start")
        await asyncio.sleep(0.01)
        write_order.append(f"{city}:end")
        return city

    read_tool = Tool(
        name="get_weather",
        description="查询天气",
        input_schema=SCHEMA,
        kind="read",
        handler=read_city,
    )
    model = ScriptedModel(
        [
            [
                _call("c1", "get_weather", '{"city":"北京"}'),
                _call("c2", "get_weather", '{"city":"上海"}'),
                _finished("tool_use"),
            ],
            [TextDelta(type="text.delta", delta="好"), _finished("stop")],
        ]
    )
    store = _store()
    await asyncio.wait_for(execute_run(store, model, _registry(read_tool), "run_1"), 1)
    assert started == ["北京", "上海"]
    outputs = [event.output for event in store.since("run_1", 0) if event.type == "tool.result"]
    assert outputs == ["北京", "上海"]

    write_one = Tool(
        name="save_one",
        description="写入",
        input_schema=SCHEMA,
        kind="write",
        handler=write_city,
    )
    write_two = Tool(
        name="save_two",
        description="写入",
        input_schema=SCHEMA,
        kind="write",
        handler=write_city,
    )
    writers = ScriptedModel(
        [
            [
                _call("c1", "save_one", '{"city":"北京"}'),
                _call("c2", "save_two", '{"city":"上海"}'),
                _finished("tool_use"),
            ],
            [TextDelta(type="text.delta", delta="好"), _finished("stop")],
        ]
    )
    await execute_run(_store(), writers, _registry(write_one, write_two), "run_1")
    assert write_order == ["北京:start", "北京:end", "上海:start", "上海:end"]


@pytest.mark.asyncio
async def test_stream_without_finish_is_incomplete() -> None:
    model = ScriptedModel([[TextDelta(type="text.delta", delta="今天")]])
    store = _store()
    await execute_run(store, model, ToolRegistry(), "run_1")
    failed = store.since("run_1", 0)[-1]
    assert failed.type == "run.failed"
    assert failed.code == "incomplete-stream"
    assert failed.retryable is True
    assert any(event.type == "message.delta" and event.delta == "今天" for event in store.since("run_1", 0))
