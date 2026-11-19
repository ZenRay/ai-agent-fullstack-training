import asyncio
import random
from typing import Literal

import pytest

from app.model import ModelFailed, PolicyLimits, TextDelta, stream_with_policy
from app.model.events import ModelFinished, ModelUsageEvent, ToolCall

MESSAGES = [{"role": "user", "content": "北京今天热吗"}]


class FakeModel:
    def __init__(self, rounds: list) -> None:
        self.rounds = rounds
        self.calls: list[dict] = []
        self.index = 0

    async def stream(self, messages, *, cancel, idempotency_key, tools=None):
        self.calls.append(
            {"messages": messages, "key": idempotency_key, "cancel": cancel, "tools": tools}
        )
        action = self.rounds[self.index]
        self.index += 1
        if isinstance(action, Exception):
            raise action
        for step in action:
            if cancel.is_set():
                return
            if step == "block":
                await cancel.wait()
                return
            yield step


def _limits(**overrides: float | int) -> PolicyLimits:
    values: dict[str, float | int] = {
        "max_attempts": 2,
        "max_total_wait": 5,
        "first_token_timeout": 1,
        "read_timeout": 1,
        "backoff_base": 0.01,
        "backoff_cap": 0.05,
        "jitter": 0,
    }
    values.update(overrides)
    return PolicyLimits(**values)


def _failed(
    code: str,
    *,
    retryable: bool,
    phase: Literal["before_content", "after_content"] = "before_content",
    retry_after_ms: float | None = None,
) -> ModelFailed:
    return ModelFailed(
        code=code,
        message=code,
        retryable=retryable,
        phase=phase,
        retry_after_ms=retry_after_ms,
    )


def _success():
    return [
        TextDelta(type="text.delta", delta="北京"),
        ModelFinished(type="model.finished", finish_reason="stop"),
        ModelUsageEvent(type="model.usage", input_tokens=3, output_tokens=2),
    ]


async def _collect(
    routes,
    *,
    cancel: asyncio.Event | None = None,
    limits: PolicyLimits | None = None,
    pauses: list[float] | None = None,
):
    async def pause(delay: float) -> None:
        if pauses is not None:
            pauses.append(delay)

    events = []
    async for event in stream_with_policy(
        routes,
        MESSAGES,
        cancel=cancel or asyncio.Event(),
        idempotency_key="idem-1",
        limits=limits or _limits(),
        sleep=pause,
        rng=random.Random(0),
    ):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_success_yields_text_then_finished_then_usage() -> None:
    model = FakeModel([_success()])
    events = await _collect([model])
    assert [event.type for event in events] == ["text.delta", "model.finished", "model.usage"]
    assert model.calls[0]["messages"] == MESSAGES
    assert model.calls[0]["key"] == "idem-1"


@pytest.mark.asyncio
async def test_retryable_failure_before_content_is_hidden() -> None:
    pauses: list[float] = []
    model = FakeModel(
        [
            [_failed("rate_limit", retryable=True, retry_after_ms=40)],
            _success(),
        ]
    )
    events = await _collect([model], pauses=pauses)
    assert [event.type for event in events] == ["text.delta", "model.finished", "model.usage"]
    assert len(model.calls) == 2
    assert all(call["messages"] == MESSAGES and call["key"] == "idem-1" for call in model.calls)
    assert pauses[0] >= 0.04


@pytest.mark.asyncio
async def test_failure_after_text_does_not_open_the_next_route() -> None:
    primary = FakeModel(
        [
            [
                TextDelta(type="text.delta", delta="北京"),
                _failed("timeout", retryable=True, phase="before_content"),
            ]
        ]
    )
    backup = FakeModel([_success()])
    events = await _collect([primary, backup])
    assert [event.type for event in events] == ["text.delta", "model.failed"]
    assert events[1].phase == "after_content"
    assert backup.calls == []


@pytest.mark.asyncio
async def test_failure_after_tool_call_does_not_open_the_next_route() -> None:
    primary = FakeModel(
        [
            [
                ToolCall(type="tool_call", call_id="c1", name="get_weather", arguments='{"city":"北京"}'),
                _failed("timeout", retryable=True),
            ]
        ]
    )
    backup = FakeModel([_success()])
    events = await _collect([primary, backup])
    assert events[0].type == "tool_call"
    assert events[1].type == "model.failed"
    assert events[1].phase == "after_content"
    assert backup.calls == []


@pytest.mark.asyncio
async def test_cancel_produces_no_failure() -> None:
    cancel = asyncio.Event()
    model = FakeModel([["block"]])

    async def stop() -> None:
        await asyncio.sleep(0.01)
        cancel.set()

    stopper = asyncio.create_task(stop())
    events = await _collect([model], cancel=cancel, limits=_limits(first_token_timeout=2))
    await stopper
    assert events == []
    assert model.calls[0]["cancel"].is_set()


@pytest.mark.asyncio
async def test_quota_switches_route_and_terminal_errors_stop() -> None:
    backup = FakeModel([_success()])
    primary = FakeModel([[_failed("quota_exceeded", retryable=True)]])
    events = await _collect([primary, backup])
    assert [event.type for event in events] == ["text.delta", "model.finished", "model.usage"]
    assert len(primary.calls) == 1
    assert len(backup.calls) == 1

    for code in ("invalid_request", "content_filter"):
        blocked = FakeModel([_success()])
        failed = FakeModel([[_failed(code, retryable=False)]])
        outcome = await _collect([failed, blocked])
        assert [event.type for event in outcome] == ["model.failed"]
        assert outcome[0].code == code
        assert blocked.calls == []


@pytest.mark.asyncio
async def test_first_token_timeout_retries_without_setting_caller_cancel() -> None:
    cancel = asyncio.Event()
    model = FakeModel([["block"], _success()])
    events = await _collect([model], cancel=cancel, limits=_limits(first_token_timeout=0.02))
    assert [event.type for event in events] == ["text.delta", "model.finished", "model.usage"]
    assert len(model.calls) == 2
    assert model.calls[0]["cancel"].is_set()
    assert not cancel.is_set()


@pytest.mark.asyncio
async def test_retry_forwards_the_same_tools() -> None:
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
    model = FakeModel([[_failed("rate_limit", retryable=True)], _success()])

    async def pause(_delay: float) -> None:
        return None

    events = []
    async for event in stream_with_policy(
        [model],
        MESSAGES,
        cancel=asyncio.Event(),
        idempotency_key="idem-1",
        limits=_limits(),
        sleep=pause,
        tools=tools,
    ):
        events.append(event)
    assert [event.type for event in events] == ["text.delta", "model.finished", "model.usage"]
    assert [call["tools"] for call in model.calls] == [tools, tools]
    assert all(call["messages"] == MESSAGES for call in model.calls)


@pytest.mark.asyncio
async def test_usage_alone_still_waits_for_the_first_token() -> None:
    cancel = asyncio.Event()
    model = FakeModel(
        [
            [ModelUsageEvent(type="model.usage", input_tokens=1, output_tokens=0), "block"],
            _success(),
        ]
    )
    events = await _collect(
        [model],
        cancel=cancel,
        limits=_limits(first_token_timeout=0.02),
    )
    assert [event.type for event in events if event.type != "model.usage"] == [
        "text.delta",
        "model.finished",
    ]
    assert len(model.calls) == 2
    assert [call["key"] for call in model.calls] == ["idem-1", "idem-1"]
    assert not cancel.is_set()
    assert not any(event.type == "tool_call" for event in events)


@pytest.mark.asyncio
async def test_two_retryable_failures_then_success_share_one_key() -> None:
    model = FakeModel(
        [
            [_failed("upstream_error", retryable=True)],
            [_failed("upstream_error", retryable=True)],
            _success(),
        ]
    )
    events = await _collect([model], limits=_limits(max_attempts=3))
    assert [event.type for event in events] == ["text.delta", "model.finished", "model.usage"]
    assert len(model.calls) == 3
    assert {call["key"] for call in model.calls} == {"idem-1"}


@pytest.mark.asyncio
async def test_adapter_runtime_error_propagates() -> None:
    model = FakeModel([RuntimeError("adapter bug")])
    with pytest.raises(RuntimeError, match="adapter bug"):
        await _collect([model])
