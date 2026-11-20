import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.events.types import TERMINAL_TYPES
from app.model.events import ToolCall
from app.model.protocol import StreamingModel
from app.prompts.assemble import assemble, tool_definitions
from app.prompts.catalog import Catalog, default_catalog
from app.prompts.hashing import PromptError
from app.prompts.output import check_output, degrade_text, repair_prompt
from app.prompts.output import response_format as structured_format
from app.prompts.render import ResolvedPrompt, render
from app.store.memory import MemoryStore
from app.tools.execute import canonical_arguments, execute_tool
from app.tools.registry import ToolRegistry

# 设计里的结束原因是 tool_use。网关翻译目前沿用供应商的 tool_calls。
_INCOMPLETE = "运行中断，已生成的内容仍保留在前面的事件里"
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


async def run_until_terminal(
    store: MemoryStore,
    model: StreamingModel,
    tools: ToolRegistry,
    run_id: str,
    max_turns: int = 8,
    *,
    started: bool = False,
    start_turn: int = 0,
    idempotency_key: str | None = None,
    prompt: ResolvedPrompt | None = None,
    catalog: Catalog | None = None,
) -> None:
    """跑完一次循环。退出时若日志还没有终态，补一条 incomplete-stream。"""
    try:
        await execute_run(
            store,
            model,
            tools,
            run_id,
            max_turns,
            started=started,
            start_turn=start_turn,
            idempotency_key=idempotency_key,
            prompt=prompt,
            catalog=catalog,
        )
    finally:
        if not _has_terminal(store, run_id):
            await store.append(run_id, _incomplete_body(store, tools, run_id))


def _has_terminal(store: MemoryStore, run_id: str) -> bool:
    try:
        events = store.since(run_id, 0)
    except KeyError:
        return True
    return any(event.type in TERMINAL_TYPES for event in events)


def _incomplete_body(store: MemoryStore, tools: ToolRegistry, run_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "run.failed",
        "code": "incomplete-stream",
        "message": _INCOMPLETE,
        "retryable": not tools.effects.has_unknown(run_id),
    }
    run = store.get_run(run_id)
    if run.input_tokens or run.output_tokens:
        body["usage"] = {"input_tokens": run.input_tokens, "output_tokens": run.output_tokens}
    return body


async def execute_run(
    store: MemoryStore,
    model: StreamingModel,
    tools: ToolRegistry,
    run_id: str,
    max_turns: int = 8,
    *,
    started: bool = False,
    start_turn: int = 0,
    idempotency_key: str | None = None,
    prompt: ResolvedPrompt | None = None,
    catalog: Catalog | None = None,
) -> None:
    """把一次运行从 run.started 推进到终态。工具只在本轮模型以工具调用结束后执行。"""
    source = catalog or default_catalog()
    if prompt is None:
        prompt = _prompt_from_store(store, run_id, source)
    if prompt is not None:
        store.bind_prompt(
            run_id,
            name=prompt.version.name,
            version=prompt.version.version,
            content_hash=prompt.version.content_hash,
            variables=prompt.variables,
        )
    if not started:
        await store.append(run_id, {"type": "run.started"})
    run = store.get_run(run_id)
    memory = _Memory(messages=store.messages(run.thread_id))
    if started:
        memory.message_count = sum(
            1 for event in store.since(run_id, 0) if event.type == "message.started"
        )
    definitions = tool_definitions(prompt.version) if prompt is not None else tools.definitions()
    model_tools = definitions or None
    for turn in range(start_turn, max_turns):
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
            idempotency_key if turn == start_turn else None,
            prompt,
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
    idempotency_key: str | None = None,
    prompt: ResolvedPrompt | None = None,
) -> _PendingTools | None:
    run = store.get_run(run_id)
    calls: list[ToolCall] = []
    text_parts: list[str] = []
    message_id: str | None = None
    finish_reason: str | None = None
    key = idempotency_key or f"{run_id}:{turn}"
    store.note_model_key(run_id, key)
    messages = memory.messages
    settings: dict[str, Any] = {}
    if prompt is not None:
        store.note_prompt_call(
            run_id,
            key,
            prompt.version.name,
            prompt.version.version,
            prompt.version.content_hash,
        )
        messages = assemble(prompt.system, memory.messages)
        settings = {
            "model": prompt.version.model.alias,
            "temperature": prompt.version.model.temperature,
            "tool_choice": prompt.version.model.tool_choice,
        }
        if prompt.version.output is not None:
            settings["response_format"] = structured_format(prompt.version.output)
    hold_text = prompt is not None and prompt.version.output is not None
    async for event in model.stream(
        messages,
        cancel=run.cancel_event,
        idempotency_key=key,
        tools=model_tools,
        **settings,
    ):
        if run.cancel_event.is_set():
            break
        if event.type == "model.failed":
            await _fail(store, run_id, event.code, event.message, event.retryable)
            return None
        if event.type == "model.usage":
            memory.input_tokens += event.input_tokens
            memory.output_tokens += event.output_tokens
            store.note_usage(run_id, event.input_tokens, event.output_tokens)
            continue
        if finish_reason is not None:
            continue
        if event.type == "text.delta":
            if not event.delta:
                continue
            text_parts.append(event.delta)
            if hold_text:
                continue
            if message_id is None:
                memory.message_count += 1
                message_id = f"m{memory.message_count}"
                await store.append(
                    run_id,
                    {"type": "message.started", "message_id": message_id, "role": "assistant"},
                )
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
        if hold_text and prompt is not None:
            await _accept_structured(
                store, model, memory, run_id, prompt, text, messages, settings, key
            )
        else:
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
        await store.append(run_id, _incomplete_body(store, tools, run_id))
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


async def _accept_structured(
    store: MemoryStore,
    model: StreamingModel,
    memory: _Memory,
    run_id: str,
    prompt: ResolvedPrompt,
    text: str,
    messages: list[dict[str, Any]],
    settings: dict[str, Any],
    turn_key: str,
) -> None:
    contract = prompt.version.output
    if contract is None:
        await _emit_answer(store, memory, run_id, text)
        return
    sources = [
        str(message.get("content") or "")
        for message in memory.messages
        if message.get("role") == "tool"
    ]
    verdict = check_output(contract, text, sources)
    if verdict.ok:
        store.note_output_attempt(run_id, 1, None, "accepted")
        await _emit_answer(store, memory, run_id, verdict.document)
        return
    store.note_output_attempt(run_id, 1, verdict.failed_field, verdict.code)
    repair_key = f"{turn_key}:repair"
    store.note_model_key(run_id, repair_key)
    store.note_prompt_call(
        run_id,
        repair_key,
        prompt.version.name,
        prompt.version.version,
        prompt.version.content_hash,
    )
    repair_messages = [
        *messages,
        {"role": "user", "content": repair_prompt(verdict, verdict.document)},
    ]
    repair_settings = {
        key: value
        for key, value in settings.items()
        if key in {"model", "temperature", "response_format"}
    }
    repaired = await _read_repair(
        store, model, memory, run_id, repair_messages, repair_settings, repair_key
    )
    if repaired is None:
        return
    repaired_text, finish_reason = repaired
    failed = _FAILED_FINISH.get(finish_reason or "")
    if failed is not None:
        code, message, retryable = failed
        await _fail(store, run_id, code, message, retryable)
        return
    if finish_reason != "stop":
        await _fail(store, run_id, finish_reason or "repair", "修复没有返回完整结果", False)
        return
    second = check_output(contract, repaired_text, sources)
    if second.ok:
        store.note_output_attempt(run_id, 2, None, "repaired")
        store.note_output_outcome(run_id, repaired=True, degraded=False, degrade_reason=None)
        await _emit_answer(store, memory, run_id, second.document)
        return
    store.note_output_attempt(run_id, 2, second.failed_field, second.code)
    reason = degrade_text(second.missing)
    store.note_output_outcome(run_id, repaired=False, degraded=True, degrade_reason=reason)
    await _emit_answer(store, memory, run_id, reason)


async def _read_repair(
    store: MemoryStore,
    model: StreamingModel,
    memory: _Memory,
    run_id: str,
    messages: list[dict[str, Any]],
    settings: dict[str, Any],
    repair_key: str,
) -> tuple[str, str | None] | None:
    run = store.get_run(run_id)
    parts: list[str] = []
    finish_reason: str | None = None
    async for event in model.stream(
        messages,
        cancel=run.cancel_event,
        idempotency_key=repair_key,
        tools=None,
        **settings,
    ):
        if run.cancel_event.is_set():
            break
        if event.type == "model.failed":
            await _fail(store, run_id, event.code, event.message, event.retryable)
            return None
        if event.type == "model.usage":
            memory.input_tokens += event.input_tokens
            memory.output_tokens += event.output_tokens
            store.note_usage(run_id, event.input_tokens, event.output_tokens)
            continue
        if finish_reason is not None:
            continue
        if event.type == "text.delta" and event.delta:
            parts.append(event.delta)
        elif event.type == "model.finished":
            finish_reason = event.finish_reason
    if run.cancel_event.is_set():
        await _cancel(store, run_id)
        return None
    return "".join(parts), finish_reason


async def _emit_answer(store: MemoryStore, memory: _Memory, run_id: str, text: str) -> None:
    memory.message_count += 1
    message_id = f"m{memory.message_count}"
    await store.append(
        run_id,
        {"type": "message.started", "message_id": message_id, "role": "assistant"},
    )
    if text:
        await store.append(
            run_id,
            {"type": "message.delta", "message_id": message_id, "delta": text},
        )
    await _complete(store, memory, run_id, message_id, text)


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


def _prompt_from_store(store: MemoryStore, run_id: str, catalog: Catalog) -> ResolvedPrompt | None:
    run = store.get_run(run_id)
    if run.prompt_name is None or run.prompt_version is None:
        return None
    version = catalog.load(run.prompt_name, run.prompt_version)
    if run.prompt_hash is not None and run.prompt_hash != version.content_hash:
        raise PromptError(f"运行记录的提示词哈希和文件不一致: {run_id}")
    values = dict(run.prompt_variables)
    return ResolvedPrompt(version=version, system=render(version, values), variables=values)
