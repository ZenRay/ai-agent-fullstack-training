from typing import Any

from app.prompts.hashing import PromptError
from app.prompts.types import PromptVersion
from app.prompts.untrusted import mark_untrusted


def assemble(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """系统指令放在线程消息前面，不写回线程。用户原文和工具结果只在这份请求里包上标记。"""
    return [{"role": "system", "content": system}, *[_for_model(message) for message in messages]]


def _for_model(message: dict[str, Any]) -> dict[str, Any]:
    copied = dict(message)
    role = copied.get("role")
    if role in {"user", "tool"}:
        copied["content"] = mark_untrusted(str(role), str(copied.get("content") or ""))
    return copied


def tool_definitions(version: PromptVersion) -> list[dict[str, Any]]:
    if version.assembly != "system_then_thread":
        raise PromptError(f"未知的组装规则: {version.assembly}")
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in version.tools
    ]
