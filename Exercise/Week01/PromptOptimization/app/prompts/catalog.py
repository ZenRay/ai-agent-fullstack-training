import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from app.prompts.hashing import (
    PromptError,
    content_hash,
    eval_identity,
    identity_dict,
    require_hash,
)
from app.prompts.types import (
    EvalSet,
    ModelSettings,
    PromptVersion,
    ToolContract,
    VariableDecl,
)

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def default_root() -> Path:
    return Path(__file__).resolve().parent / "catalog"


def build_version(
    *,
    name: str,
    version: int,
    hypothesis: str,
    template: str,
    variables: list[VariableDecl] | None = None,
    tools: list[ToolContract] | None = None,
    assembly: Literal["system_then_thread"] = "system_then_thread",
    model: ModelSettings | None = None,
) -> PromptVersion:
    """算出内容哈希后再返回。调用方不手写哈希。"""
    draft = PromptVersion(
        name=name,
        version=version,
        hypothesis=hypothesis,
        template=template,
        variables=list(variables or []),
        tools=list(tools or []),
        assembly=assembly,
        model=model or ModelSettings(alias="chat-default", tool_choice="auto"),
        content_hash="0" * 64,
    )
    _check_version(draft)
    return draft.model_copy(update={"content_hash": content_hash(identity_dict(draft))})


def build_eval(name: str, version: int, cases: list[dict[str, Any]]) -> EvalSet:
    draft = EvalSet.model_validate(
        {"name": name, "version": version, "cases": cases, "content_hash": "0" * 64}
    )
    _check_eval(draft)
    return draft.model_copy(update={"content_hash": content_hash(eval_identity(draft))})


class Catalog:
    """Git 里的固定文件。发布只在版本文件还不存在时写入，指针另外移动。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_root()

    def load(self, name: str, version: int) -> PromptVersion:
        path = self._version_path(name, version)
        payload = _read_json(path, f"未知提示词版本: {name}@{version}")
        try:
            parsed = PromptVersion.model_validate(payload)
        except ValidationError as exc:
            raise PromptError(f"提示词版本无法读取: {name}@{version}") from exc
        if parsed.name != name or parsed.version != version:
            raise PromptError(f"版本文件与路径不一致: {name}@{version}")
        _check_version(parsed)
        require_hash(parsed.content_hash, identity_dict(parsed))
        return parsed

    def current(self) -> PromptVersion:
        pointer = _read_json(self.root / "current.json", "缺少当前提示词指针")
        name = pointer.get("name") if isinstance(pointer, dict) else None
        version = pointer.get("version") if isinstance(pointer, dict) else None
        if not isinstance(name, str) or type(version) is not int:
            raise PromptError("当前提示词指针格式不正确")
        return self.load(name, version)

    def publish(self, version: PromptVersion) -> Path:
        _check_version(version)
        require_hash(version.content_hash, identity_dict(version))
        path = self._version_path(version.name, version.version)
        if path.exists():
            raise PromptError(f"版本已存在: {version.name}@{version.version}")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, version.model_dump())
        return path

    def load_eval(self, name: str, version: int) -> EvalSet:
        path = self._eval_path(name, version)
        payload = _read_json(path, f"未知评测集: {name}@{version}")
        try:
            parsed = EvalSet.model_validate(payload)
        except ValidationError as exc:
            raise PromptError(f"评测集无法读取: {name}@{version}") from exc
        if parsed.name != name or parsed.version != version:
            raise PromptError(f"评测集文件与路径不一致: {name}@{version}")
        _check_eval(parsed)
        require_hash(parsed.content_hash, eval_identity(parsed))
        return parsed

    def publish_eval(self, eval_set: EvalSet) -> Path:
        _check_eval(eval_set)
        require_hash(eval_set.content_hash, eval_identity(eval_set))
        path = self._eval_path(eval_set.name, eval_set.version)
        if path.exists():
            raise PromptError(f"评测集已存在: {eval_set.name}@{eval_set.version}")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, eval_set.model_dump())
        return path

    def write_current(self, name: str, version: int) -> None:
        """把指针移到一份已经发布的版本。通过评测之前不要调用。"""
        self.load(name, version)
        _write_json(self.root / "current.json", {"name": name, "version": version})

    def _version_path(self, name: str, version: int) -> Path:
        return self.root / _safe_name(name) / f"v{version}.json"

    def _eval_path(self, name: str, version: int) -> Path:
        return self.root / "evals" / _safe_name(name) / f"v{version}.json"


def default_catalog() -> Catalog:
    return Catalog()


def _safe_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise PromptError(f"非法的名称: {name}")
    return name


def _check_version(version: PromptVersion) -> None:
    _unique([item.name for item in version.variables], "变量")
    _unique([tool.name for tool in version.tools], "工具")
    declared = {item.name for item in version.variables}
    unknown = sorted(set(_PLACEHOLDER.findall(version.template)) - declared)
    if unknown:
        raise PromptError(f"未声明的占位符: {', '.join(unknown)}")


def _check_eval(eval_set: EvalSet) -> None:
    if not eval_set.cases:
        raise PromptError("评测集至少要有一条用例")
    _unique([case.id for case in eval_set.cases], "用例")


def _unique(names: list[str], label: str) -> None:
    if len(names) != len(set(names)):
        raise PromptError(f"{label}名称重复")


def _read_json(path: Path, missing: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptError(missing) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PromptError(f"文件不是合法 JSON: {path.name}") from exc


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
