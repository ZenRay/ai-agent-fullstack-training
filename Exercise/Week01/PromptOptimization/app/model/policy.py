import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.model.events import ModelFailed, ModelStreamEvent
from app.model.protocol import StreamingModel

SWITCH_CODES = frozenset({"quota_exceeded", "model_not_found"})


@dataclass(frozen=True)
class PolicyLimits:
    max_attempts: int = 2
    max_total_wait: float = 8.0
    first_token_timeout: float = 30.0
    read_timeout: float = 60.0
    backoff_base: float = 0.2
    backoff_cap: float = 2.0
    jitter: float = 0.05


class _Cancelled(Exception):
    pass


class _Timeout(Exception):
    pass


class _Retry(Exception):
    def __init__(self, failure: ModelFailed) -> None:
        self.failure = failure


class _Switch(Exception):
    def __init__(self, failure: ModelFailed) -> None:
        self.failure = failure


class _Fail(Exception):
    def __init__(self, failure: ModelFailed) -> None:
        self.failure = failure


async def stream_with_policy(
    routes: Sequence[StreamingModel],
    messages: list[dict[str, Any]],
    *,
    cancel: asyncio.Event,
    idempotency_key: str,
    limits: PolicyLimits | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    rng: random.Random | None = None,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    temperature: float | None = None,
    tool_choice: str | None = None,
) -> AsyncIterator[ModelStreamEvent]:
    limits = limits or PolicyLimits()
    pause = sleep or asyncio.sleep
    random_source = rng or random.Random()
    waited = 0.0
    last_failure: ModelFailed | None = None
    for model_route in routes:
        move_on = False
        for attempt in range(limits.max_attempts):
            if cancel.is_set():
                return
            try:
                async for event in _read_attempt(
                    model_route,
                    messages,
                    cancel,
                    idempotency_key,
                    limits,
                    tools,
                    model_name=model,
                    temperature=temperature,
                    tool_choice=tool_choice,
                ):
                    yield event
            except _Cancelled:
                return
            except _Fail as exc:
                yield exc.failure
                return
            except _Switch as exc:
                last_failure = exc.failure
                move_on = True
                break
            except _Retry as exc:
                last_failure = exc.failure
                delay = _delay(exc.failure, attempt, limits, random_source)
                if attempt + 1 >= limits.max_attempts or waited + delay > limits.max_total_wait:
                    move_on = True
                    break
                await pause(delay)
                waited += delay
                continue
            else:
                return
        if move_on:
            continue
    if last_failure is not None:
        yield last_failure


async def _read_attempt(
    model: StreamingModel,
    messages: list[dict[str, Any]],
    cancel: asyncio.Event,
    idempotency_key: str,
    limits: PolicyLimits,
    tools: list[dict[str, Any]] | None,
    *,
    model_name: str | None = None,
    temperature: float | None = None,
    tool_choice: str | None = None,
) -> AsyncIterator[ModelStreamEvent]:
    abort = asyncio.Event()
    saw_content = False
    stream = model.stream(
        messages,
        cancel=abort,
        idempotency_key=idempotency_key,
        tools=tools,
        **_model_settings(model_name, temperature, tool_choice),
    )
    try:
        while True:
            if cancel.is_set():
                abort.set()
                raise _Cancelled
            timeout = limits.read_timeout if saw_content else limits.first_token_timeout
            try:
                event = await _next_event(stream, cancel, abort, timeout)
            except _Timeout:
                failure = _timeout_failure(saw_content)
                if saw_content:
                    raise _Fail(failure) from None
                raise _Retry(failure) from None
            except StopAsyncIteration:
                return
            if event.type == "text.delta" and event.delta:
                saw_content = True
            elif event.type == "tool_call":
                saw_content = True
            elif event.type == "model.failed":
                raise _decide_failure(event, saw_content)
            yield event
    finally:
        await _aclose(stream)


def _decide_failure(event: ModelFailed, saw_content: bool) -> Exception:
    if saw_content or event.phase == "after_content":
        failure = event
        if failure.phase != "after_content":
            failure = failure.model_copy(update={"phase": "after_content"})
        return _Fail(failure)
    if event.code in SWITCH_CODES:
        return _Switch(event)
    if event.retryable:
        return _Retry(event)
    return _Fail(event)


def _timeout_failure(saw_content: bool) -> ModelFailed:
    if saw_content:
        return ModelFailed(
            code="timeout",
            message="读取超时",
            retryable=True,
            phase="after_content",
        )
    return ModelFailed(
        code="timeout",
        message="首 token 超时",
        retryable=True,
        phase="before_content",
    )


def _delay(failure: ModelFailed, attempt: int, limits: PolicyLimits, rng: random.Random) -> float:
    jitter = rng.uniform(0, limits.jitter) if limits.jitter else 0.0
    if failure.retry_after_ms is not None:
        return failure.retry_after_ms / 1000 + jitter
    exponential = limits.backoff_base * (2**attempt)
    return min(limits.backoff_cap, exponential) + jitter


async def _next_event(
    stream: AsyncIterator[ModelStreamEvent],
    cancel: asyncio.Event,
    abort: asyncio.Event,
    timeout: float,
) -> ModelStreamEvent:
    get_task = asyncio.create_task(anext(stream))
    stop_task = asyncio.create_task(cancel.wait())
    cancelled = False
    timed_out = False
    ready = False
    try:
        done, _pending = await asyncio.wait(
            {get_task, stop_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel.is_set() or stop_task in done:
            abort.set()
            cancelled = True
        elif get_task in done:
            ready = True
        else:
            abort.set()
            timed_out = True
    finally:
        for task in (get_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(get_task, stop_task, return_exceptions=True)
        if not cancelled and timed_out and _completed(get_task):
            timed_out = False
            ready = True
    if cancelled:
        raise _Cancelled
    if timed_out or not ready:
        raise _Timeout
    return get_task.result()


def _completed(task: asyncio.Task[ModelStreamEvent]) -> bool:
    if not task.done() or task.cancelled():
        return False
    error = task.exception()
    return error is None or not isinstance(error, StopAsyncIteration)


async def _aclose(stream: AsyncIterator[ModelStreamEvent]) -> None:
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()


def _model_settings(
    model: str | None,
    temperature: float | None,
    tool_choice: str | None,
) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    if model is not None:
        settings["model"] = model
    if temperature is not None:
        settings["temperature"] = temperature
    if tool_choice is not None:
        settings["tool_choice"] = tool_choice
    return settings
