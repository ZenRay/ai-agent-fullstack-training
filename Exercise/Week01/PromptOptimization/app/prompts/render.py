import re
from dataclasses import dataclass

from app.prompts.catalog import Catalog
from app.prompts.hashing import PromptError
from app.prompts.types import PromptVersion
from app.tools.registry import ToolRegistry

_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


@dataclass(frozen=True)
class ResolvedPrompt:
    version: PromptVersion
    system: str
    variables: dict[str, str]


def render(version: PromptVersion, variables: dict[str, str] | None = None) -> str:
    """缺变量或模板拼不出来时失败。调用方要在流打开之前接住这个错误。"""
    values = dict(variables or {})
    declared = {item.name: item for item in version.variables}
    extra = sorted(set(values) - set(declared))
    if extra:
        raise PromptError(f"未声明的变量: {', '.join(extra)}")
    missing = [
        item.name
        for item in version.variables
        if item.required and not values.get(item.name, "").strip()
    ]
    if missing:
        raise PromptError(f"缺少变量: {', '.join(missing)}")
    unknown = sorted(set(_PLACEHOLDER.findall(version.template)) - set(declared))
    if unknown:
        raise PromptError(f"未声明的占位符: {', '.join(unknown)}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = values.get(name, "").strip()
        if not value:
            raise PromptError(f"缺少变量: {name}")
        return value

    return _PLACEHOLDER.sub(replace, version.template)


def resolve(
    catalog: Catalog,
    tools: ToolRegistry,
    selection: tuple[str, int] | None,
    variables: dict[str, str] | None,
) -> ResolvedPrompt:
    version = catalog.current() if selection is None else catalog.load(*selection)
    missing = [tool.name for tool in version.tools if tools.find(tool.name) is None]
    if missing:
        raise PromptError(f"未注册的工具: {', '.join(missing)}")
    values = dict(variables or {})
    return ResolvedPrompt(version=version, system=render(version, values), variables=values)
