import pytest

from app.prompts.catalog import build_version
from app.prompts.hashing import PromptError
from app.prompts.render import render
from app.prompts.types import PromptVersion, VariableDecl


def _version(template: str, variables: list[VariableDecl]) -> PromptVersion:
    return build_version(
        name="assistant",
        version=2,
        hypothesis="检查变量怎么填",
        template=template,
        variables=variables,
    )


def test_missing_required_variable_fails() -> None:
    version = _version("今天是 {{date}}", [VariableDecl(name="date", required=True)])
    with pytest.raises(PromptError, match="缺少变量"):
        render(version, {})


def test_extra_variable_fails() -> None:
    version = _version("只回答天气。", [])
    with pytest.raises(PromptError, match="未声明的变量"):
        render(version, {"date": "2026-11-20"})


def test_declared_variable_is_substituted() -> None:
    version = _version("今天是 {{date}}。", [VariableDecl(name="date", required=True)])
    assert render(version, {"date": "2026-11-20"}) == "今天是 2026-11-20。"
