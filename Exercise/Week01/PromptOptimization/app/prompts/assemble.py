from typing import Any

from app.prompts.hashing import PromptError
from app.prompts.types import PromptVersion


def assemble(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """系统指令放在线程消息前面，不写回线程。"""
    return [{"role": "system", "content": system}, *[dict(message) for message in messages]]


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
