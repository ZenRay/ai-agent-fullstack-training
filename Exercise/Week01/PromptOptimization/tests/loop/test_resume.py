import copy
import sqlite3

from app.loop.resume import resume_run
from app.model.events import ModelFailed, ModelFinished, TextDelta
from app.store.sqlite import SqliteStore
from app.tools.effects import ReconcileResult
from app.tools.execute import canonical_arguments, hash_key
from app.tools.registry import Tool, ToolRegistry

SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}
ARGUMENTS = '{"city":"北京"}'


class ScriptedModel:
    def __init__(self, script: list) -> None:
        self.script = script
        self.calls: list[dict] = []

    async def stream(self, messages, *, cancel, idempotency_key, tools=None):
        self.calls.append(
            {"messages": copy.deepcopy(messages), "key": idempotency_key, "cancel": cancel}
        )
        for event in self.script:
            if cancel.is_set():
                return
            yield event


def _tools(store: SqliteStore, handler, reconcile=None) -> ToolRegistry:
    tools = ToolRegistry()
    tools.effects = store.effects
    tools.register(
        Tool(
            name="save_note",
            description="记下城市",
            input_schema=SCHEMA,
            kind="write",
            handler=handler,
            reconcile=reconcile,
        )
    )
    return tools


def _reopen(path, store: SqliteStore) -> SqliteStore:
    store.close()
    return SqliteStore(path)


async def _started(store: SqliteStore) -> None:
    store.add_message("th", "user", "北京今天热吗")
    store.create_run("th", "run_1")
    await store.append("run_1", {"type": "run.started"})


async def test_restart_before_text_reuses_the_same_key(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    store = SqliteStore(path)
    await _started(store)
    store = _reopen(path, store)
    model = ScriptedModel(
        [
            TextDelta(type="text.delta", delta="北京"),
            ModelFinished(type="model.finished", finish_reason="stop"),
        ]
    )
    await resume_run(store, model, ToolRegistry(), "run_1")
    assert len(model.calls) == 1
    assert model.calls[0]["key"] == "run_1:0"
    assert store.get_run("run_1").status == "completed"
    assert store.messages("th")[-1] == {"role": "assistant", "content": "北京"}
    store.close()


async def test_partial_text_continues_once(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    store = SqliteStore(path)
    await _started(store)
    await store.append("run_1", {"type": "message.started", "message_id": "m1", "role": "assistant"})
    await store.append("run_1", {"type": "message.delta", "message_id": "m1", "delta": "今天"})
    store = _reopen(path, store)
    model = ScriptedModel(
        [
            ModelFailed(
                type="model.failed",
                code="timeout",
                message="续写失败",
                retryable=True,
                phase="before_content",
            )
        ]
    )
    await resume_run(store, model, ToolRegistry(), "run_1")
    assert len(model.calls) == 1
    assert model.calls[0]["key"] == "run_1:continue"
    assert {"role": "assistant", "content": "今天"} in model.calls[0]["messages"]
    assert store.get_run("run_1").status == "failed"
    await resume_run(store, model, ToolRegistry(), "run_1")
    assert len(model.calls) == 1
    store.close()


async def test_continue_flag_stops_a_second_model_call(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    store = SqliteStore(path)
    await _started(store)
    await store.append("run_1", {"type": "message.delta", "message_id": "m1", "delta": "今天"})
    store.mark_continued("run_1")
    store = _reopen(path, store)
    model = ScriptedModel(
        [TextDelta(type="text.delta", delta="晴"), ModelFinished(type="model.finished", finish_reason="stop")]
    )
    await resume_run(store, model, ToolRegistry(), "run_1")
    assert model.calls == []
    assert store.get_run("run_1").status == "failed"
    assert store.since("run_1", 0)[-1].type == "run.failed"
    store.close()


async def test_settled_write_is_not_sent_again(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    store = SqliteStore(path)
    await _started(store)
    await store.append("run_1", {"type": "tool.started", "call_id": "c1", "name": "save_note"})
    await store.append(
        "run_1",
        {"type": "tool.arguments.done", "call_id": "c1", "arguments": ARGUMENTS},
    )
    sent: list[str] = []

    async def save(city: str, idempotency_key: str) -> str:
        sent.append(city)
        return "saved"

    key = hash_key("run_1", "c1", {"city": "北京"})
    _receipt, owned = store.effects.claim(
        key,
        run_id="run_1",
        call_id="c1",
        name="save_note",
        arguments=canonical_arguments(ARGUMENTS),
    )
    assert owned
    store.effects.settle(key, ok=True, output="saved")
    store = _reopen(path, store)
    model = ScriptedModel(
        [TextDelta(type="text.delta", delta="好"), ModelFinished(type="model.finished", finish_reason="stop")]
    )
    await resume_run(store, model, _tools(store, save), "run_1")
    assert sent == []
    assert len(model.calls) == 1
    results = [event for event in store.since("run_1", 0) if event.type == "tool.result"]
    assert results[0].output == "saved"
    assert results[0].ok is True
    store.close()


async def test_write_without_a_receipt_runs_once(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    store = SqliteStore(path)
    await _started(store)
    await store.append("run_1", {"type": "tool.started", "call_id": "c1", "name": "save_note"})
    await store.append(
        "run_1",
        {"type": "tool.arguments.done", "call_id": "c1", "arguments": ARGUMENTS},
    )
    seen: list[str] = []

    async def save(city: str, idempotency_key: str) -> str:
        other = sqlite3.connect(path)
        row = other.execute(
            "SELECT status FROM effect_receipts WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        other.close()
        assert row is not None
        assert row[0] == "pending"
        seen.append(city)
        return "saved"

    store = _reopen(path, store)
    model = ScriptedModel(
        [TextDelta(type="text.delta", delta="好"), ModelFinished(type="model.finished", finish_reason="stop")]
    )
    await resume_run(store, model, _tools(store, save), "run_1")
    assert seen == ["北京"]
    assert len(model.calls) == 1
    store.close()


async def test_empty_receipt_uses_reconcile_instead_of_sending(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    store = SqliteStore(path)
    await _started(store)
    await store.append("run_1", {"type": "tool.started", "call_id": "c1", "name": "save_note"})
    await store.append(
        "run_1",
        {"type": "tool.arguments.done", "call_id": "c1", "arguments": ARGUMENTS},
    )
    key = hash_key("run_1", "c1", {"city": "北京"})
    store.effects.claim(
        key,
        run_id="run_1",
        call_id="c1",
        name="save_note",
        arguments=canonical_arguments(ARGUMENTS),
    )
    sent: list[str] = []

    async def save(city: str, idempotency_key: str) -> str:
        sent.append(city)
        return "saved"

    async def reconcile(idempotency_key: str) -> ReconcileResult:
        assert idempotency_key == key
        return ReconcileResult(found=True, output="已存在")

    store = _reopen(path, store)
    model = ScriptedModel(
        [TextDelta(type="text.delta", delta="好"), ModelFinished(type="model.finished", finish_reason="stop")]
    )
    await resume_run(store, model, _tools(store, save, reconcile), "run_1")
    assert sent == []
    results = [event for event in store.since("run_1", 0) if event.type == "tool.result"]
    assert results[0].output == "已存在"
    store.close()


async def test_terminal_event_repairs_a_stale_status(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    store = SqliteStore(path)
    await _started(store)
    await store.append(
        "run_1",
        {"type": "run.failed", "code": "upstream_error", "message": "上游中断", "retryable": True},
    )
    store.close()
    db = sqlite3.connect(path)
    db.execute("UPDATE runs SET status = 'running' WHERE run_id = 'run_1'")
    db.commit()
    db.close()
    store = SqliteStore(path)
    model = ScriptedModel(
        [
            TextDelta(type="text.delta", delta="不该再叫"),
            ModelFinished(type="model.finished", finish_reason="stop"),
        ]
    )
    await resume_run(store, model, ToolRegistry(), "run_1")
    assert model.calls == []
    assert store.get_run("run_1").status == "failed"
    store.close()
