import hashlib
import json
import re
from typing import Any

from app.prompts.types import EvalSet, PromptVersion

_HASH = re.compile(r"^[0-9a-f]{64}$")


class PromptError(ValueError):
    """提示词版本无法加载、发布或渲染。"""


def canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def identity_dict(version: PromptVersion) -> dict[str, Any]:
    """假设、名称和版本号不进入哈希。改这些不会让同一段指令变成另一份内容。"""
    return {
        "template": version.template,
        "variables": [item.model_dump() for item in version.variables],
        "tools": [tool.model_dump() for tool in version.tools],
        "assembly": version.assembly,
        "model": version.model.model_dump(),
    }


def eval_identity(eval_set: EvalSet) -> dict[str, Any]:
    return {
        "name": eval_set.name,
        "version": eval_set.version,
        "cases": [case.model_dump() for case in eval_set.cases],
    }


def require_hash(stored: str, payload: dict[str, Any]) -> None:
    if not _HASH.fullmatch(stored):
        raise PromptError("内容哈希格式不正确")
    digest = content_hash(payload)
    if stored != digest:
        raise PromptError("内容哈希不一致")
