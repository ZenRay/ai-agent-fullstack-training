import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.model.events import ToolCall
from app.model.protocol import StreamingModel
from app.store.memory import MemoryStore
from app.tools.execute import canonical_arguments, execute_tool
from app.tools.registry import ToolRegistry

# 设计里的结束原因是 tool_use。网关翻译目前沿用供应商的 tool_calls。
_TOOL_REASONS = frozenset({"tool_use", "tool_calls"})
_FAILED_FINISH = {
    "length": ("length", "输出被截断，不再把半截内容交给下一轮", False),
    "content_filter": ("content_filter", "输出被过滤，不再重试", False),
}


@dataclass
class _Memory:
    messages: list[dict[str, Any]]
    seen: dict[tuple[str, str], str] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    message_count: int = 0


@dataclass
class _PendingTools:
    calls: list[ToolCall]
    text: str


async def execute_run(
    store: MemoryStore,
    model: StreamingModel,
    tools: ToolRegistry,
    run_id: str,
    max_turns: int = 8,
) -> None:
    """把一次运行从 run.started 推进到终态。工具只在本轮模型以工具调用结束后执行。"""
    await store.append(run_id, {"type": "run.started"})
    run = store.get_run(run_id)
    memory = _Memory(messages=store.messages(run.thread_id))
    model_tools = tools.definitions() or None
    for turn in range(max_turns):
        if run.cancel_event.is_set():
            await _cancel(store, run_id)
            return
        pending = await _read_model(
            store,
            model,
            tools,
            memory,
            run_id,
            turn,
            model_tools,
        )
        if pending is None:
            return
        if await _apply_tools(store, tools, memory, run_id, pending):
            return
    await _fail(store, run_id, "max_turns", "超过轮数上限", False)


async def _read_model(
    store: MemoryStore,
    model: StreamingModel,
    tools: ToolRegistry,
    memory: _Memory,
    run_id: str,
    turn: int,
    model_tools: list[dict[str, Any]] | None,
) -> _PendingTools | None:
    run = store.get_run(run_id)
    calls: list[ToolCall] = []
    text_parts: list[str] = []
    message_id: str | None = None
    finish_reason: str | None = None
    async for event in model.stream(
        memory.messages,
        cancel=run.cancel_event,
        idempotency_key=f"{run_id}:{turn}",
        tools=model_tools,
    ):
        if run.cancel_event.is_set():
            break
        if event.type == "model.failed":
            await _fail(store, run_id, event.code, event.message, event.retryable)
            return None
        if event.type == "model.usage":
            memory.input_tokens += event.input_tokens
            memory.output_tokens += event.output_tokens
            continue
        if finish_reason is not None:
            continue
        if event.type == "text.delta":
            if not event.delta:
                continue
            if message_id is None:
                memory.message_count += 1
                message_id = f"m{memory.message_count}"
                await store.append(
                    run_id,
                    {"type": "message.started", "message_id": message_id, "role": "assistant"},
                )
            text_parts.append(event.delta)
            await store.append(
                run_id,
                {"type": "message.delta", "message_id": message_id, "delta": event.delta},
            )
        elif event.type == "tool_call":
            calls.append(event)
            await store.append(
                run_id,
                {"type": "tool.started", "call_id": event.call_id, "name": event.name},
            )
            await store.append(
                run_id,
                {
                    "type": "tool.arguments.done",
                    "call_id": event.call_id,
                    "arguments": event.arguments,
                },
            )
        elif event.type == "model.finished":
            finish_reason = event.finish_reason
        else:
            raise RuntimeError(f"未知的模型事件: {event.type}")

    text = "".join(text_parts)
    if finish_reason == "stop":
        await _complete(store, memory, run_id, message_id, text)
        return None
    failed = _FAILED_FINISH.get(finish_reason or "")
    if failed is not None:
        code, message, retryable = failed
        await _fail(store, run_id, code, message, retryable)
        return None
    if run.cancel_event.is_set():
        await _cancel(store, run_id)
        return None
    if finish_reason in _TOOL_REASONS:
        if not calls:
            await _fail(store, run_id, "tool_use", "模型以工具调用结束，但没有完整调用", False)
            return None
        return _PendingTools(calls=calls, text=text)
    if finish_reason is None:
        await _fail(
            store,
            run_id,
            "incomplete-stream",
            "运行中断，已生成的内容仍保留在前面的事件里",
            not tools.effects.has_unknown(run_id),
        )
        return None
    await _fail(store, run_id, finish_reason, f"未知的结束原因: {finish_reason}", False)
    return None


async def _apply_tools(
    store: MemoryStore,
    tools: ToolRegistry,
    memory: _Memory,
    run_id: str,
    pending: _PendingTools,
) -> bool:
    run = store.get_run(run_id)
    executed: list[tuple[ToolCall, dict[str, Any]]] = []
    index = 0
    cancelled = False
    while index < len(pending.calls):
        if run.cancel_event.is_set():
            cancelled = True
            break
        call = pending.calls[index]
        if _is_read(tools, call.name):
            batch = [call]
            index += 1
            while index < len(pending.calls) and _is_read(tools, pending.calls[index].name):
                batch.append(pending.calls[index])
                index += 1
            outputs = await asyncio.gather(
                *[
                    execute_tool(
                        tools,
                        run_id,
                        item.call_id,
                        item.name,
                        item.arguments,
                        run.cancel_event,
                    )
                    for item in batch
                ]
            )
            executed.extend(zip(batch, outputs, strict=True))
        else:
            result = await execute_tool(
                tools,
                run_id,
                call.call_id,
                call.name,
                call.arguments,
                run.cancel_event,
            )
            executed.append((call, result))
            index += 1
        if run.cancel_event.is_set():
            cancelled = True
            break
    repeated = await _record_results(store, memory, run_id, pending.text, executed)
    if cancelled:
        await _cancel(store, run_id)
        return True
    if repeated:
        await _fail(
            store,
            run_id,
            "repeated_call",
            "同一工具用相同参数再次出现，结果没有变化。请换一个问法或停止调用该工具。",
            False,
        )
        return True
    return False


async def _record_results(
    store: MemoryStore,
    memory: _Memory,
    run_id: str,
    text: str,
    executed: list[tuple[ToolCall, dict[str, Any]]],
) -> bool:
    if not executed:
        return False
    thread_id = store.get_run(run_id).thread_id
    _remember(
        store,
        thread_id,
        memory.messages,
        "assistant",
        text,
        tool_calls=[
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call, _result in executed
        ],
    )
    repeated = False
    for call, result in executed:
        await store.append(
            run_id,
            {
                "type": "tool.result",
                "call_id": call.call_id,
                "ok": result["ok"],
                "output": result["output"],
            },
        )
        _remember(
            store,
            thread_id,
            memory.messages,
            "tool",
            result["output"],
            tool_call_id=call.call_id,
        )
        signature = (call.name, canonical_arguments(call.arguments))
        previous = memory.seen.get(signature)
        if previous is not None and previous == result["output"]:
            repeated = True
        memory.seen[signature] = result["output"]
    return repeated


async def _complete(
    store: MemoryStore,
    memory: _Memory,
    run_id: str,
    message_id: str | None,
    text: str,
) -> None:
    if message_id is not None:
        await store.append(run_id, {"type": "message.completed", "message_id": message_id})
        _remember(store, store.get_run(run_id).thread_id, memory.messages, "assistant", text)
    await store.append(
        run_id,
        {
            "type": "run.completed",
            "usage": {
                "input_tokens": memory.input_tokens,
                "output_tokens": memory.output_tokens,
            },
        },
    )


async def _fail(store: MemoryStore, run_id: str, code: str, message: str, retryable: bool) -> None:
    await store.append(
        run_id,
        {"type": "run.failed", "code": code, "message": message, "retryable": retryable},
    )


async def _cancel(store: MemoryStore, run_id: str) -> None:
    await store.append(run_id, {"type": "run.cancelled", "reason": "用户取消"})


def _remember(
    store: MemoryStore,
    thread_id: str,
    messages: list[dict[str, Any]],
    role: str,
    content: str,
    **extra: Any,
) -> None:
    messages.append({"role": role, "content": content, **extra})
    store.add_message(thread_id, role, content, **extra)


def _is_read(tools: ToolRegistry, name: str) -> bool:
    tool = tools.find(name)
    return tool is not None and tool.kind == "read"
