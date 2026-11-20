from app.main import create_app
from app.prompts.catalog import Catalog, build_version, default_catalog
from app.prompts.types import ToolContract, VariableDecl
from app.store.memory import MemoryStore
from tests.api.test_runs import ScriptedModel, _client


async def test_unknown_version_and_extra_variables_fail_before_a_run_exists() -> None:
    app = create_app(model=ScriptedModel())
    store: MemoryStore = app.state.store
    async with _client(app) as client:
        unknown = await client.post(
            "/v1/threads/th/runs",
            json={"message": "你好", "prompt": {"name": "assistant", "version": 99}},
        )
        extra = await client.post(
            "/v1/threads/th/runs",
            json={"message": "你好", "variables": {"date": "2026-11-20"}},
        )
    assert unknown.status_code == 400
    assert extra.status_code == 400
    assert store._runs == {}
    assert store.messages("th") == []


async def test_missing_variable_and_unknown_tool_fail_before_a_run_exists(tmp_path) -> None:
    dated = build_version(
        name="assistant",
        version=1,
        hypothesis="日期是必填变量",
        template="今天是 {{date}}。",
        variables=[VariableDecl(name="date", required=True)],
    )
    mailed = build_version(
        name="assistant",
        version=2,
        hypothesis="提示词点了一个没有注册的工具",
        template="不要发信。",
        tools=[
            ToolContract(
                name="send_mail",
                description="发信",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            )
        ],
    )
    catalog = Catalog(tmp_path)
    catalog.publish(dated)
    catalog.publish(mailed)
    catalog.write_current("assistant", 1)
    app = create_app(model=ScriptedModel())
    app.state.catalog = catalog
    store: MemoryStore = app.state.store
    async with _client(app) as client:
        missing = await client.post("/v1/threads/th/runs", json={"message": "你好"})
        unknown_tool = await client.post(
            "/v1/threads/th/runs",
            json={"message": "你好", "prompt": {"name": "assistant", "version": 2}},
        )
    assert missing.status_code == 400
    assert unknown_tool.status_code == 400
    assert store._runs == {}


async def test_explicit_current_version_replays_the_same_run() -> None:
    app = create_app(model=ScriptedModel())
    headers = {"Idempotency-Key": "same"}
    async with _client(app) as client:
        first = await client.post("/v1/threads/th/runs", json={"message": "你好"}, headers=headers)
        second = await client.post(
            "/v1/threads/th/runs",
            json={"message": "你好", "prompt": {"name": "assistant", "version": 1}},
            headers=headers,
        )
    assert first.status_code == 200
    assert second.status_code == 200
    run_id = first.headers["x-run-id"]
    assert second.headers["x-run-id"] == run_id
    store: MemoryStore = app.state.store
    version = default_catalog().current()
    assert store.get_run(run_id).prompt_hash == version.content_hash
    assert [call.trace_id for call in store.prompt_calls(run_id)] == [f"{run_id}:0"]
    assert all(item["role"] != "system" for item in store.messages("th"))
    status = None
    async with _client(app) as client:
        status = await client.get(f"/v1/runs/{run_id}")
    assert status.json()["prompt_name"] == "assistant"
    assert status.json()["prompt_version"] == 1
    assert status.json()["prompt_hash"] == version.content_hash
