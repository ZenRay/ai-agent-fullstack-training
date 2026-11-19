import asyncio
import hashlib
import json
import time
from typing import Any

from jsonschema import Draft202012Validator

from app.tools.effects import ReconcileResult
from app.tools.registry import Tool, ToolRegistry

_TIMEOUT_UNKNOWN = "超时，结果不明，不要重试这次写操作"
_UNKNOWN_AGAIN = "结果不明，可能已经生效，不要再用相同参数发送"
_PRIOR_UNKNOWN = "此前相同工具有一次写操作结果不明，可能已经生效。本次结果："


def canonical_arguments(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments
    if not isinstance(parsed, dict):
        return arguments
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_key(run_id: str, call_id: str, parsed: dict[str, Any]) -> str:
    canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = f"{run_id}\n{call_id}\n{canonical}".encode()
    return hashlib.sha256(raw).hexdigest()


def parse_and_validate(schema: dict[str, Any], arguments: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return None, "参数不是合法的 JSON。请按工具说明重新给出参数。"
    if not isinstance(parsed, dict):
        return None, "参数必须是 JSON 对象。请按工具说明重新给出参数。"
    errors = list(Draft202012Validator(schema).iter_errors(parsed))
    if errors:
        detail = errors[0].message
        return None, f"参数不符合 schema: {detail}。请按工具说明修改后再调用。"
    return parsed, None


async def execute_tool(
    tools: ToolRegistry,
    run_id: str,
    call_id: str,
    name: str,
    arguments: str,
    cancel: asyncio.Event | None = None,
) -> dict[str, Any]:
    tool = tools.find(name)
    if tool is None:
        return {"ok": False, "output": f"未注册的工具: {name}。请改用已有工具。"}
    parsed, error = parse_and_validate(tool.input_schema, arguments)
    if error is not None or parsed is None:
        return {"ok": False, "output": error or "参数不符合 schema。请按工具说明修改后再调用。"}
    if tool.kind == "write":
        return await _execute_write(tools, tool, run_id, call_id, arguments, parsed, cancel)
    return await _execute_read(tool, parsed, cancel)


async def _execute_read(
    tool: Tool,
    parsed: dict[str, Any],
    cancel: asyncio.Event | None,
) -> dict[str, Any]:
    deadline = time.monotonic() + tool.timeout_seconds
    last = _read_timeout(tool.name)
    for attempt in range(2):
        if attempt and cancel is not None and cancel.is_set():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # 第一次只占用一半预算，超时或失败后仍留得下再试一次。
        slot = remaining if attempt else remaining / 2
        try:
            output = await asyncio.wait_for(tool.handler(**parsed), slot)
        except TimeoutError:
            last = _read_timeout(tool.name)
            continue
        except Exception as exc:
            last = _call_failed(tool.name, exc)
            continue
        return {"ok": True, "output": _text(output)}
    return {"ok": False, "output": last}


async def _execute_write(
    tools: ToolRegistry,
    tool: Tool,
    run_id: str,
    call_id: str,
    arguments: str,
    parsed: dict[str, Any],
    cancel: asyncio.Event | None,
) -> dict[str, Any]:
    canonical = canonical_arguments(arguments)
    cached = tools.effects.find_result(run_id, tool.name, canonical)
    if cached is not None and cached.output is not None:
        return {"ok": bool(cached.ok), "output": cached.output}
    key = hash_key(run_id, call_id, parsed)
    _receipt, owned = tools.effects.claim(
        key,
        run_id=run_id,
        call_id=call_id,
        name=tool.name,
        arguments=canonical,
    )
    if not owned:
        current = tools.effects.find_result(run_id, tool.name, canonical)
        if current is not None and current.output is not None:
            return {"ok": bool(current.ok), "output": current.output}
        return {"ok": False, "output": _UNKNOWN_AGAIN}
    try:
        output = await asyncio.wait_for(
            tool.handler(**parsed, idempotency_key=key),
            tool.timeout_seconds,
        )
    except TimeoutError:
        return await _recover_write(tools, tool, run_id, key, parsed, canonical, cancel)
    except Exception as exc:
        result = _note(tools, run_id, tool.name, canonical, _call_failed(tool.name, exc))
        tools.effects.settle(key, ok=False, output=result["output"])
        return result
    result = _note(tools, run_id, tool.name, canonical, _text(output), ok=True)
    tools.effects.settle(key, ok=True, output=result["output"])
    return result


async def _recover_write(
    tools: ToolRegistry,
    tool: Tool,
    run_id: str,
    key: str,
    parsed: dict[str, Any],
    canonical: str,
    cancel: asyncio.Event | None,
) -> dict[str, Any]:
    seen = await _reconcile(tool, key)
    if seen.found:
        result = _note(tools, run_id, tool.name, canonical, seen.output, ok=True)
        tools.effects.settle(key, ok=True, output=result["output"])
        return result
    if seen.absent and (cancel is None or not cancel.is_set()):
        try:
            output = await asyncio.wait_for(
                tool.handler(**parsed, idempotency_key=key),
                tool.timeout_seconds,
            )
        except TimeoutError:
            tools.effects.mark_unknown(key, _TIMEOUT_UNKNOWN)
            return {"ok": False, "output": _TIMEOUT_UNKNOWN}
        except Exception as exc:
            result = _note(tools, run_id, tool.name, canonical, _call_failed(tool.name, exc))
            tools.effects.settle(key, ok=False, output=result["output"])
            return result
        result = _note(tools, run_id, tool.name, canonical, _text(output), ok=True)
        tools.effects.settle(key, ok=True, output=result["output"])
        return result
    if seen.absent:
        text = "已取消，写操作没有发出"
        tools.effects.settle(key, ok=False, output=text)
        return {"ok": False, "output": text}
    tools.effects.mark_unknown(key, _TIMEOUT_UNKNOWN)
    return {"ok": False, "output": _TIMEOUT_UNKNOWN}


async def _reconcile(tool: Tool, key: str) -> ReconcileResult:
    if tool.reconcile is None:
        return ReconcileResult()
    try:
        return await tool.reconcile(key)
    except Exception:
        return ReconcileResult()


def _note(
    tools: ToolRegistry,
    run_id: str,
    name: str,
    canonical: str,
    output: str,
    *,
    ok: bool = False,
) -> dict[str, Any]:
    if tools.effects.has_other_unknown(run_id, name, canonical):
        output = f"{_PRIOR_UNKNOWN}{output}"
    return {"ok": ok, "output": output}


def _text(output: object) -> str:
    return output if isinstance(output, str) else str(output)


def _read_timeout(name: str) -> str:
    return f"读取 {name} 超时。可以稍后重试，或缩小查询范围。"


def _call_failed(name: str, exc: Exception) -> str:
    return f"调用 {name} 失败: {exc}。可以修改参数或换一个工具。"
