import asyncio
import copy

import pytest

from app.loop.run import execute_run, run_until_terminal
from app.model.events import ModelFinished, ModelUsageEvent, TextDelta, ToolCall
from app.store.memory import MemoryStore
from app.tools.effects import ReconcileResult
from app.tools.registry import Tool, ToolRegistry

SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}
ARGUMENTS = '{"city":"北京"}'


class _Boom:
    async def stream(self, messages, *, cancel, idempotency_key, tools=None):
        yield TextDelta(type="text.delta", delta="今天")
        yield ModelUsageEvent(type="model.usage", input_tokens=4, output_tokens=1)
        raise RuntimeError("断了")


class _Script:
    def __init__(self, scripts: list[list]) -> None:
        self.scripts = scripts
        self.calls: list[dict] = []

    async def stream(self, messages, *, cancel, idempotency_key, tools=None):
        if len(self.calls) >= len(self.scripts):
            raise AssertionError("模型调用次数超过剧本")
        self.calls.append({"messages": copy.deepcopy(messages), "key": idempotency_key})
        for event in self.scripts[len(self.calls) - 1]:
            yield event


def _store() -> MemoryStore:
    store = MemoryStore()
    store.add_message("th_1", "user", "北京今天热吗")
    store.create_run("th_1", "run_1")
    return store


def _call() -> ToolCall:
    return ToolCall(type="tool_call", call_id="c1", name="save_note", arguments=ARGUMENTS)


def _finished(reason: str) -> ModelFinished:
    return ModelFinished(type="model.finished", finish_reason=reason)


async def test_exception_after_the_first_delta_closes_the_run() -> None:
    store = _store()
    with pytest.raises(RuntimeError, match="断了"):
        await run_until_terminal(store, _Boom(), ToolRegistry(), "run_1")
    events = store.since("run_1", 0)
    assert [event.delta for event in events if event.type == "message.delta"] == ["今天"]
    assert events[-1].type == "run.failed"
    assert events[-1].code == "incomplete-stream"
    assert events[-1].retryable is True
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens == 4
    assert events[-1].usage.output_tokens == 1
    assert "usage" in events[-1].model_dump(exclude_none=True)


async def test_unknown_write_is_not_sent_again() -> None:
    calls: list[str] = []

    async def save(city: str, idempotency_key: str) -> str:
        calls.append(city)
        await asyncio.sleep(1)
        return "已写入"

    async def unknown(_key: str) -> ReconcileResult:
        return ReconcileResult()

    tool = Tool(
        name="save_note",
        description="记下城市",
        input_schema=SCHEMA,
        kind="write",
        handler=save,
        timeout_seconds=0.05,
        reconcile=unknown,
    )
    tools = ToolRegistry()
    tools.register(tool)
    model = _Script(
        [
            [_call(), _finished("tool_use")],
            [_call(), _finished("tool_use")],
            [TextDelta(type="text.delta", delta="好"), _finished("stop")],
        ]
    )
    store = _store()
    await execute_run(store, model, tools, "run_1")
    results = [event for event in store.since("run_1", 0) if event.type == "tool.result"]
    assert calls == ["北京"]
    assert len(results) == 2
    assert all(result.ok is False and "结果不明" in result.output for result in results)
