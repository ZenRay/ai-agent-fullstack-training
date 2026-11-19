import asyncio
import copy
import random

from app.loop.run import execute_run
from app.model.events import ModelFailed, ModelFinished, TextDelta, ToolCall
from app.model.policy import PolicyLimits, stream_with_policy
from app.store.memory import MemoryStore
from app.tools.registry import Tool, ToolRegistry

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
            }
        )
        for event in self.scripts[len(self.calls) - 1]:
            if cancel.is_set():
                return
            yield event


class PolicyModel:
    """把策略套在剧本模型外。循环仍只调用一次 stream。"""

    def __init__(self, inner: ScriptedModel, limits: PolicyLimits) -> None:
        self.inner = inner
        self.limits = limits

    async def stream(self, messages, *, cancel, idempotency_key, tools=None):
        async for event in stream_with_policy(
            [self.inner],
            messages,
            cancel=cancel,
            idempotency_key=idempotency_key,
            limits=self.limits,
            sleep=_no_wait,
            rng=random.Random(0),
            tools=tools,
        ):
            yield event


class GatedModel:
    def __init__(self, release: asyncio.Event) -> None:
        self.release = release

    async def stream(self, messages, *, cancel, idempotency_key, tools=None):
        yield TextDelta(type="text.delta", delta="今天")
        await self.release.wait()
        if cancel.is_set():
            return
        yield TextDelta(type="text.delta", delta="晴")
        yield ModelFinished(type="model.finished", finish_reason="stop")


async def _no_wait(_delay: float) -> None:
    return None


def _limits() -> PolicyLimits:
    return PolicyLimits(
        max_attempts=3,
        max_total_wait=5,
        first_token_timeout=1,
        read_timeout=1,
        backoff_base=0.01,
        backoff_cap=0.05,
        jitter=0,
    )


def _failed() -> ModelFailed:
    return ModelFailed(
        type="model.failed",
        code="upstream_error",
        message="上游暂时失败",
        retryable=True,
        phase="before_content",
    )


def _store() -> MemoryStore:
    store = MemoryStore()
    store.add_message("th_1", "user", "北京今天热吗")
    store.create_run("th_1", "run_1")
    return store


def _text(delta: str) -> TextDelta:
    return TextDelta(type="text.delta", delta=delta)


def _finished(reason: str) -> ModelFinished:
    return ModelFinished(type="model.finished", finish_reason=reason)


async def test_retry_before_content_reaches_the_run_log_once() -> None:
    inner = ScriptedModel(
        [
            [_failed()],
            [_failed()],
            [_text("北京"), _finished("stop")],
        ]
    )
    store = _store()
    await execute_run(store, PolicyModel(inner, _limits()), ToolRegistry(), "run_1")
    events = store.since("run_1", 0)
    assert [event.type for event in events] == [
        "run.started",
        "message.started",
        "message.delta",
        "message.completed",
        "run.completed",
    ]
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert events[2].delta == "北京"
    assert len(inner.calls) == 3
    assert {call["key"] for call in inner.calls} == {"run_1:0"}
    assert all(call["messages"] == [{"role": "user", "content": "北京今天热吗"}] for call in inner.calls)


async def test_retryable_failure_after_text_does_not_call_again() -> None:
    inner = ScriptedModel(
        [[
            _text("今天"),
            ModelFailed(
                type="model.failed",
                code="timeout",
                message="上游中断",
                retryable=True,
                phase="after_content",
            ),
        ]]
    )
    store = _store()
    await execute_run(store, PolicyModel(inner, _limits()), ToolRegistry(), "run_1")
    events = store.since("run_1", 0)
    assert [event.delta for event in events if event.type == "message.delta"] == ["今天"]
    assert events[-1].type == "run.failed"
    assert events[-1].retryable is True
    assert len(inner.calls) == 1


async def test_failure_after_a_write_does_not_start_another_model_call() -> None:
    used = 0

    async def save(city: str, idempotency_key: str) -> str:
        nonlocal used
        used += 1
        return f"{city}:{idempotency_key[-6:]}"

    tool = Tool(
        name="save_note",
        description="记下城市",
        input_schema=SCHEMA,
        kind="write",
        handler=save,
    )
    tools = ToolRegistry()
    tools.register(tool)
    model = ScriptedModel(
        [
            [
                ToolCall(type="tool_call", call_id="c1", name="save_note", arguments='{"city":"北京"}'),
                _finished("tool_use"),
            ],
            [
                _text("半截"),
                ModelFailed(
                    type="model.failed",
                    code="timeout",
                    message="上游中断",
                    retryable=True,
                    phase="after_content",
                ),
            ],
            [_text("不该再叫"), _finished("stop")],
        ]
    )
    store = _store()
    await execute_run(store, model, tools, "run_1")
    events = store.since("run_1", 0)
    assert used == 1
    assert len(model.calls) == 2
    assert [event.delta for event in events if event.type == "message.delta"] == ["半截"]
    assert events[-1].type == "run.failed"


async def test_subscriber_disconnect_leaves_the_run_going() -> None:
    release = asyncio.Event()
    store = _store()
    run_task = asyncio.create_task(execute_run(store, GatedModel(release), ToolRegistry(), "run_1"))
    seen = asyncio.Event()
    found: dict[str, int] = {}

    async def watch() -> None:
        seq = 0
        while True:
            for event in store.since("run_1", seq):
                seq = event.seq
                if event.type == "message.delta":
                    found["seq"] = event.seq
                    seen.set()
                    await asyncio.Event().wait()
            await store.wait_for_change("run_1", seq)

    watch_task = asyncio.create_task(watch())
    await seen.wait()
    watch_task.cancel()
    await asyncio.gather(watch_task, return_exceptions=True)
    assert not store.get_run("run_1").cancel_event.is_set()

    release.set()
    await run_task
    tail = store.since("run_1", found["seq"])
    assert [event.seq for event in tail] == list(range(found["seq"] + 1, found["seq"] + 1 + len(tail)))
    assert tail[-1].type == "run.completed"
    assert not any(
        event.type == "run.failed" and event.code == "incomplete-stream" for event in store.since("run_1", 0)
    )
    assert not store.get_run("run_1").cancel_event.is_set()
