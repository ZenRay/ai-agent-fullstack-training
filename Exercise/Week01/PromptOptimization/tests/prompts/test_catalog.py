import json

import pytest

from app.prompts.catalog import Catalog, build_version, default_catalog
from app.prompts.hashing import PromptError
from app.prompts.types import ToolContract, VariableDecl


def test_shipped_version_matches_its_hash_and_pointer() -> None:
    catalog = default_catalog()
    current = catalog.current()
    assert current.name == "assistant"
    assert current.version == 1
    assert current.hypothesis == "把查天气的指令和工具契约收成可追溯的第一版"
    assert current.tools[0].name == "get_weather"
    assert current.model.alias == "chat-default"
    assert current.model.temperature is None
    assert current.model.tool_choice == "auto"
    again = catalog.load("assistant", 1)
    assert again.content_hash == current.content_hash


def test_editing_a_published_file_breaks_the_hash(tmp_path) -> None:
    catalog = _catalog_with_v1(tmp_path)
    path = tmp_path / "assistant" / "v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["template"] = "就地改过的指令"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PromptError, match="内容哈希不一致"):
        catalog.load("assistant", 1)


def test_publish_refuses_to_rewrite_a_version_even_if_only_the_hypothesis_changes(tmp_path) -> None:
    catalog = _catalog_with_v1(tmp_path)
    current = catalog.load("assistant", 1)
    rewritten = current.model_copy(update={"hypothesis": "只改假设"})
    with pytest.raises(PromptError, match="版本已存在"):
        catalog.publish(rewritten)


def test_unknown_version_is_rejected(tmp_path) -> None:
    catalog = _catalog_with_v1(tmp_path)
    with pytest.raises(PromptError, match="未知提示词版本"):
        catalog.load("assistant", 9)


def _catalog_with_v1(tmp_path) -> Catalog:
    catalog = Catalog(tmp_path)
    catalog.publish(default_catalog().load("assistant", 1))
    catalog.write_current("assistant", 1)
    return catalog


def test_build_version_rejects_an_undeclared_placeholder() -> None:
    with pytest.raises(PromptError, match="未声明的占位符"):
        build_version(
            name="assistant",
            version=2,
            hypothesis="占位符没有声明",
            template="今天是 {{date}}",
            tools=[_weather()],
        )


def _weather() -> ToolContract:
    return default_catalog().load("assistant", 1).tools[0]


def test_required_variable_is_part_of_the_version() -> None:
    version = build_version(
        name="assistant",
        version=2,
        hypothesis="把日期留成变量",
        template="今天是 {{date}}",
        variables=[VariableDecl(name="date", required=True)],
    )
    assert version.variables[0].required is True
