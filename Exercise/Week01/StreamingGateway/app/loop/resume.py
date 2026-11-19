from typing import Any

from app.events.types import TERMINAL_TYPES
from app.loop.run import execute_run
from app.model.protocol import StreamingModel
from app.store.memory import STATUS_OF, MemoryStore
from app.store.sqlite import SqliteStore
from app.tools.effects import ReconcileResult
from app.tools.execute import canonical_arguments, execute_tool, hash_key, parse_and_validate
from app.tools.registry import Tool, ToolRegistry

_UNKNOWN = "结果不明，没有再次发送"


async def resume_unfinished(
    store: MemoryStore | SqliteStore,
    model: StreamingModel,
    tools: ToolRegistry,
) -> None:
    for run in store.unfinished():
        await resume_run(store, model, tools, run.run_id)


async def resume_run(
    store: MemoryStore | SqliteStore,
    model: StreamingModel,
    tools: ToolRegistry,
    run_id: str,
) -> None:
    events = store.since(run_id, 0)
    terminal = next((event for event in reversed(events) if event.type in TERMINAL_TYPES), None)
    if terminal is not None:
        store.repair_status(run_id, STATUS_OF[terminal.type])
        return
    if not any(event.type == "run.started" for event in events):
        await execute_run(store, model, tools, run_id)
        return
    run = store.get_run(run_id)
    if run.cancel_event.is_set():
        await store.append(run_id, {"type": "run.cancelled", "reason": "用户取消"})
        return
    pending = _pending(events)
    if pending:
        await _resolve_pending(store, tools, run_id, events, pending)
        if store.get_run(run_id).status in {"completed", "failed", "cancelled"}:
            return
        if store.get_run(run_id).cancel_event.is_set():
            await store.append(run_id, {"type": "run.cancelled", "reason": "用户取消"})
            return
        await execute_run(store, model, tools, run_id, started=True, start_turn=_start_turn(events))
        return
    if any(event.type == "message.completed" for event in events):
        await store.append(
            run_id,
            {"type": "run.completed", "usage": {"input_tokens": 0, "output_tokens": 0}},
        )
        return
    if _has_text(events):
        await _continue_once(store, model, tools, run_id, events)
        return
    key = f"{run_id}:0"
    store.note_model_key(run_id, key)
    await execute_run(store, model, tools, run_id, started=True, idempotency_key=key)


async def _continue_once(store, model, tools, run_id: str, events) -> None:
    if store.continued(run_id):
        await store.append(
            run_id,
            {
                "type": "run.failed",
                "code": "incomplete-stream",
                "message": "半截回答未能续上",
                "retryable": False,
            },
        )
        return
    store.mark_continued(run_id)
    partial = _partial_text(events)
    if partial:
        store.add_message(store.get_run(run_id).thread_id, "assistant", partial)
    key = f"{run_id}:continue"
    store.note_model_key(run_id, key)
    await execute_run(store, model, tools, run_id, started=True, idempotency_key=key)


async def _resolve_pending(store, tools: ToolRegistry, run_id: str, events, pending) -> None:
    thread_id = store.get_run(run_id).thread_id
    payload = [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
        for call_id, name, arguments in pending
    ]
    results: list[tuple[str, dict[str, Any]]] = []
    for call_id, name, arguments in pending:
        tool = tools.find(name)
        if tool is not None and tool.kind == "write":
            result = await _resume_write(store, tools, tool, run_id, call_id, arguments)
        else:
            result = await execute_tool(
                tools,
                run_id,
                call_id,
                name,
                arguments,
                store.get_run(run_id).cancel_event,
            )
        results.append((call_id, result))
    if not _messages_have_calls(store.messages(thread_id), [call_id for call_id, _, _ in pending]):
        store.add_message(thread_id, "assistant", _partial_text(events), tool_calls=payload)
    for call_id, result in results:
        await store.append(
            run_id,
            {
                "type": "tool.result",
                "call_id": call_id,
                "ok": result["ok"],
                "output": result["output"],
            },
        )


async def _resume_write(
    store,
    tools: ToolRegistry,
    tool: Tool,
    run_id: str,
    call_id: str,
    arguments: str,
) -> dict[str, Any]:
    parsed, error = parse_and_validate(tool.input_schema, arguments)
    if error is not None or parsed is None:
        return {"ok": False, "output": error or "参数不符合 schema。请按工具说明修改后再调用。"}
    found = tools.effects.find_result(run_id, tool.name, canonical_arguments(arguments))
    if found is not None and found.output is not None:
        return {"ok": bool(found.ok), "output": found.output}
    key = hash_key(run_id, call_id, parsed)
    receipt = tools.effects.get(key)
    if receipt is None:
        return await execute_tool(
            tools,
            run_id,
            call_id,
            tool.name,
            arguments,
            store.get_run(run_id).cancel_event,
        )
    if receipt.output is not None:
        return {"ok": bool(receipt.ok), "output": receipt.output}
    seen = await _reconcile(tool, key)
    if seen.found:
        tools.effects.settle(key, ok=True, output=seen.output)
        return {"ok": True, "output": seen.output}
    if seen.absent and tool.reconcile is not None:
        output = await tool.handler(**parsed, idempotency_key=key)
        text = output if isinstance(output, str) else str(output)
        tools.effects.settle(key, ok=True, output=text)
        return {"ok": True, "output": text}
    if receipt.status != "unknown":
        tools.effects.mark_unknown(key, _UNKNOWN)
    current = tools.effects.get(key)
    output = current.output if current is not None and current.output is not None else _UNKNOWN
    return {"ok": False, "output": output}


async def _reconcile(tool: Tool, key: str) -> ReconcileResult:
    if tool.reconcile is None:
        return ReconcileResult()
    try:
        return await tool.reconcile(key)
    except Exception:
        return ReconcileResult()


def _pending(events) -> list[tuple[str, str, str]]:
    names: dict[str, str] = {}
    done: dict[str, str] = {}
    resulted: set[str] = set()
    for event in events:
        if event.type == "tool.started":
            names[event.call_id] = event.name
        elif event.type == "tool.arguments.done":
            done[event.call_id] = event.arguments
        elif event.type == "tool.result":
            resulted.add(event.call_id)
    return [
        (call_id, names.get(call_id, ""), arguments)
        for call_id, arguments in done.items()
        if call_id not in resulted
    ]


def _has_text(events) -> bool:
    return any(event.type == "message.delta" and event.delta for event in events)


def _partial_text(events) -> str:
    return "".join(event.delta for event in events if event.type == "message.delta" and event.delta)


def _start_turn(events) -> int:
    rounds = 0
    saw_tool = False
    for event in events:
        if event.type == "message.completed":
            rounds += 1
            saw_tool = False
        elif event.type == "tool.arguments.done":
            saw_tool = True
    if saw_tool:
        rounds += 1
    return rounds


def _messages_have_calls(messages: list[dict[str, Any]], call_ids: list[str]) -> bool:
    seen: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            if isinstance(call_id, str):
                seen.add(call_id)
    return all(call_id in seen for call_id in call_ids)
