import json

from app.prompts.catalog import Catalog, build_version, default_catalog
from app.prompts.evaluate import ToolUse, Turn, diff_behavior, diff_text, evaluate, promote
from app.prompts.types import ToolContract
from app.tools.registry import ToolRegistry
from app.tools.weather import register_weather

_CITY = json.dumps({"city": "北京"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def _follow(messages, tools, settings) -> Turn:
    if messages[-1]["role"] == "tool":
        return Turn()
    names = {item["function"]["name"] for item in tools}
    user = next(message["content"] for message in reversed(messages) if message["role"] == "user")
    if "get_weather" in names and "北京" in user:
        return Turn(tool_calls=(ToolUse("get_weather", _CITY),))
    return Turn()


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_weather(registry)
    return registry


async def test_shipped_eval_passes_and_identical_text_has_no_diff() -> None:
    catalog = default_catalog()
    version = catalog.current()
    report = await evaluate(version, catalog.load_eval("weather", 1), _follow, _registry())
    assert report.quality == 1
    assert all(item.contract_ok and item.expectation_met for item in report.results)
    assert diff_text(version, version) == []


async def test_text_and_behavior_diffs_show_a_removed_tool(tmp_path) -> None:
    catalog = _copy(tmp_path)
    current = catalog.current()
    removed = build_version(
        name="assistant",
        version=2,
        hypothesis="模型不再看到查天气的工具",
        template=current.template,
        tools=[],
    )
    catalog.publish(removed)
    eval_set = catalog.load_eval("weather", 1)
    before = await evaluate(current, eval_set, _follow, _registry())
    after = await evaluate(removed, eval_set, _follow, _registry())
    assert diff_text(current, removed) == ["tools"]
    changed = {change.case_id for change in diff_behavior(before, after)}
    assert changed >= {"city-known", "tool-failed"}


async def test_promote_keeps_the_pointer_when_a_passing_case_breaks(tmp_path) -> None:
    catalog = _copy(tmp_path)
    current = catalog.current()
    removed = build_version(
        name="assistant",
        version=2,
        hypothesis="模型不再看到查天气的工具",
        template=current.template,
        tools=[],
    )
    catalog.publish(removed)
    moved = await promote(catalog, "assistant", 2, catalog.load_eval("weather", 1), _follow, _registry())
    assert moved is False
    assert catalog.current().version == 1


async def test_promote_moves_the_pointer_after_the_same_cases_still_pass(tmp_path) -> None:
    catalog = _copy(tmp_path)
    current = catalog.current()
    kept = build_version(
        name="assistant",
        version=2,
        hypothesis="再记一版，工具契约不变",
        template=current.template,
        tools=list(current.tools),
    )
    catalog.publish(kept)
    moved = await promote(catalog, "assistant", 2, catalog.load_eval("weather", 1), _follow, _registry())
    assert moved is True
    assert catalog.current().version == 2
    assert catalog.current().content_hash == kept.content_hash


async def test_quality_score_waits_until_the_contract_passes() -> None:
    version = default_catalog().current()
    eval_set = default_catalog().load_eval("weather", 1)

    async def invalid(messages, tools, settings) -> Turn:
        return Turn(tool_calls=(ToolUse("get_weather", "{}"),))

    report = await evaluate(version, eval_set, invalid, _registry())
    assert report.quality is None
    assert any(not item.contract_ok for item in report.results)


async def test_unregistered_tool_fails_the_contract() -> None:
    current = default_catalog().current()
    widened = build_version(
        name="assistant",
        version=2,
        hypothesis="提示词里多一个注册表没有的工具",
        template=current.template,
        tools=[
            *current.tools,
            ToolContract(
                name="send_mail",
                description="发信",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ],
    )
    report = await evaluate(widened, default_catalog().load_eval("weather", 1), _follow, _registry())
    assert report.quality is None
    assert any("send_mail" in error for item in report.results for error in item.errors)


def _copy(tmp_path) -> Catalog:
    source = default_catalog()
    catalog = Catalog(tmp_path)
    catalog.publish(source.load("assistant", 1))
    catalog.publish_eval(source.load_eval("weather", 1))
    catalog.write_current("assistant", 1)
    return catalog
