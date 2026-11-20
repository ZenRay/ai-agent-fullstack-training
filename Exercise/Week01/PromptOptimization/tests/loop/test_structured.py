import copy
import json

from app.loop.run import execute_run
from app.model.events import ModelFinished, TextDelta, ToolCall
from app.prompts.catalog import default_catalog
from app.prompts.render import ResolvedPrompt, render
from app.store.memory import MemoryStore
from app.tools.registry import ToolRegistry
from app.tools.weather import register_weather


def _card(celsius: int) -> str:
    return json.dumps(
        {
            "reason": "工具结果写明了温度",
            "city": "北京",
            "celsius": celsius,
            "kind": "weather",
        },
        ensure_ascii=False,
    )


class ScriptedModel:
    def __init__(self, scripts: list[list]) -> None:
        self.scripts = scripts
        self.calls: list[dict] = []

    async def stream(
        self,
        messages,
        *,
        cancel,
        idempotency_key,
        tools=None,
        model=None,
        temperature=None,
        tool_choice=None,
        response_format=None,
    ):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "key": idempotency_key,
                "response_format": copy.deepcopy(response_format),
            }
        )
        for event in self.scripts[len(self.calls) - 1]:
            if cancel.is_set():
                return
            yield event


def _tools() -> ToolRegistry:
    registry = ToolRegistry()
    register_weather(registry)
    return registry


def _prompt(version: int) -> ResolvedPrompt:
    selected = default_catalog().load("assistant", version)
    return ResolvedPrompt(version=selected, system=render(selected, {}), variables={})


async def _run(model: ScriptedModel, version: int, *, source: str | None) -> MemoryStore:
    store = MemoryStore()
    store.add_message("th", "user", "北京今天热吗")
    if source is not None:
        store.add_message("th", "tool", source, tool_call_id="c0")
    store.create_run("th", "run_1")
    await execute_run(store, model, _tools(), "run_1", prompt=_prompt(version))
    return store


def _deltas(store: MemoryStore) -> list[str]:
    return [event.delta for event in store.since("run_1", 0) if event.type == "message.delta"]


def _failed(store: MemoryStore):
    return next(event for event in store.since("run_1", 0) if event.type == "run.failed")


async def test_fenced_json_is_repaired_locally() -> None:
    model = ScriptedModel(
        [[TextDelta(type="text.delta", delta=f"```json\n{_card(31)}\n```"), _stop()]]
    )
    store = await _run(model, 2, source="北京今天 31 度")
    assert len(model.calls) == 1
    assert model.calls[0]["response_format"]["type"] == "json_schema"
    assert _deltas(store) == [_card(31)]
    assert store.get_run("run_1").repaired is False
    assert store.get_run("run_1").degraded is False
    assert store.output_attempts("run_1")[0].code == "accepted"
    assert default_catalog().current().version == 1


async def test_business_rule_triggers_one_repair() -> None:
    model = ScriptedModel(
        [
            [TextDelta(type="text.delta", delta=_card(99)), _stop()],
            [TextDelta(type="text.delta", delta=_card(31)), _stop()],
        ]
    )
    store = await _run(model, 2, source="北京今天 31 度")
    assert len(model.calls) == 2
    assert model.calls[1]["tools"] is None
    assert model.calls[1]["key"] == "run_1:0:repair"
    assert "99" in model.calls[1]["messages"][-1]["content"]
    assert all("99" not in delta for delta in _deltas(store))
    assert json.loads(_deltas(store)[0])["celsius"] == 31
    run = store.get_run("run_1")
    assert run.repaired is True
    assert run.degraded is False
    assert [item.code for item in store.output_attempts("run_1")] == ["business_rule", "repaired"]
    assert store.output_attempts("run_1")[0].failed_field == "celsius"


async def test_failed_repair_degrades_without_executing_tools() -> None:
    model = ScriptedModel(
        [
            [TextDelta(type="text.delta", delta="不是 JSON"), _stop()],
            [
                TextDelta(type="text.delta", delta="还是不行"),
                ToolCall(
                    type="tool_call",
                    call_id="c1",
                    name="get_weather",
                    arguments='{"city":"北京"}',
                ),
                _stop(),
            ],
        ]
    )
    store = await _run(model, 2, source="北京今天 31 度")
    events = store.since("run_1", 0)
    assert [event.type for event in events if event.type == "tool.started"] == []
    assert events[-1].type == "run.completed"
    assert _deltas(store) == ["这不是可执行结构。缺少：reason、city、celsius、kind。"]
    run = store.get_run("run_1")
    assert run.degraded is True
    assert run.repaired is False
    assert run.degrade_reason == _deltas(store)[0]
    assert "不是 JSON" not in _deltas(store)[0]


async def test_length_and_content_filter_do_not_repair() -> None:
    for reason in ("length", "content_filter"):
        model = ScriptedModel(
            [[TextDelta(type="text.delta", delta='{"reason":'), _finish(reason)]]
        )
        store = await _run(model, 2, source="北京今天 31 度")
        assert len(model.calls) == 1
        assert _deltas(store) == []
        assert _failed(store).code == reason
        assert '{"reason":' not in "".join(
            message["content"] for message in store.messages("th")
        )


async def test_free_text_version_still_streams() -> None:
    model = ScriptedModel(
        [
            [
                TextDelta(type="text.delta", delta="北"),
                TextDelta(type="text.delta", delta="京"),
                _stop(),
            ]
        ]
    )
    store = await _run(model, 1, source=None)
    assert len(model.calls) == 1
    assert model.calls[0]["response_format"] is None
    assert _deltas(store) == ["北", "京"]
    assert store.get_run("run_1").degraded is False


def _stop() -> ModelFinished:
    return _finish("stop")


def _finish(reason: str) -> ModelFinished:
    return ModelFinished(type="model.finished", finish_reason=reason)
