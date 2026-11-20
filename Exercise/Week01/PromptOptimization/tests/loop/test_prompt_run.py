import copy

from app.loop.resume import resume_run
from app.loop.run import execute_run
from app.model.events import ModelFinished, TextDelta, ToolCall
from app.prompts.catalog import Catalog, build_version, default_catalog
from app.prompts.render import ResolvedPrompt, render
from app.store.memory import MemoryStore
from app.tools.registry import ToolRegistry
from app.tools.weather import register_weather


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
    ):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "model": model,
                "temperature": temperature,
                "tool_choice": tool_choice,
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


def _resolved(version=None, variables=None) -> ResolvedPrompt:
    selected = version or default_catalog().current()
    values = dict(variables or {})
    return ResolvedPrompt(version=selected, system=render(selected, values), variables=values)


async def test_one_run_uses_the_same_version_on_every_turn() -> None:
    store = MemoryStore()
    store.add_message("th", "user", "北京今天热吗")
    store.create_run("th", "run_1")
    resolved = _resolved()
    model = ScriptedModel(
        [
            [
                ToolCall(
                    type="tool_call",
                    call_id="c1",
                    name="get_weather",
                    arguments='{"city":"北京"}',
                ),
                ModelFinished(type="model.finished", finish_reason="tool_calls"),
            ],
            [
                TextDelta(type="text.delta", delta="北京今天 31 度"),
                ModelFinished(type="model.finished", finish_reason="stop"),
            ],
        ]
    )
    await execute_run(store, model, _tools(), "run_1", prompt=resolved)
    assert [call["messages"][0] for call in model.calls] == [
        {"role": "system", "content": resolved.system},
        {"role": "system", "content": resolved.system},
    ]
    assert all(item["role"] != "system" for item in store.messages("th"))
    assert model.calls[0]["tools"][0]["function"]["description"] == resolved.version.tools[0].description
    assert model.calls[0]["model"] == "chat-default"
    assert model.calls[0]["temperature"] is None
    assert model.calls[0]["tool_choice"] == "auto"
    calls = store.prompt_calls("run_1")
    assert [call.trace_id for call in calls] == ["run_1:0", "run_1:1"]
    assert {call.content_hash for call in calls} == {resolved.version.content_hash}


async def test_a_later_run_can_use_another_version() -> None:
    current = default_catalog().current()
    other = build_version(
        name="assistant",
        version=2,
        hypothesis="下一问换一种说法",
        template="另一版系统指令。遇到天气仍调用 get_weather。",
        tools=list(current.tools),
    )
    store = MemoryStore()
    store.add_message("th", "user", "你好")
    store.create_run("th", "run_1")
    store.add_message("th", "user", "北京今天热吗")
    store.create_run("th", "run_2")
    answer = [
        TextDelta(type="text.delta", delta="好"),
        ModelFinished(type="model.finished", finish_reason="stop"),
    ]
    model = ScriptedModel([answer, answer])
    await execute_run(store, model, _tools(), "run_1", prompt=_resolved(current))
    await execute_run(store, model, _tools(), "run_2", prompt=_resolved(other))
    assert model.calls[0]["messages"][0]["content"] == current.template
    assert model.calls[1]["messages"][0]["content"] == other.template
    assert store.prompt_calls("run_1")[0].content_hash != store.prompt_calls("run_2")[0].content_hash


async def test_resume_keeps_the_version_recorded_on_the_run(tmp_path) -> None:
    shipped = default_catalog().load("assistant", 1)
    other = build_version(
        name="assistant",
        version=2,
        hypothesis="指针已经移到下一版",
        template="这是当前指针指向的另一版。",
        tools=list(shipped.tools),
    )
    catalog = Catalog(tmp_path)
    catalog.publish(shipped)
    catalog.publish(other)
    catalog.write_current("assistant", 2)
    store = MemoryStore()
    store.add_message("th", "user", "北京今天热吗")
    store.create_run("th", "run_1")
    store.bind_prompt(
        "run_1",
        name=shipped.name,
        version=shipped.version,
        content_hash=shipped.content_hash,
        variables={},
    )
    answer = [
        TextDelta(type="text.delta", delta="好"),
        ModelFinished(type="model.finished", finish_reason="stop"),
    ]
    model = ScriptedModel([answer])
    await resume_run(store, model, _tools(), "run_1", catalog=catalog)
    assert model.calls[0]["messages"][0]["content"] == shipped.template
    assert store.prompt_calls("run_1")[0].version == 1
