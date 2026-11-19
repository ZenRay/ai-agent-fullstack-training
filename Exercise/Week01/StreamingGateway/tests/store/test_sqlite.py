import sqlite3

import pytest

from app.loop.run import execute_run
from app.model.events import ModelFinished, ModelUsageEvent, TextDelta
from app.store.sqlite import SqliteStore
from app.tools.registry import ToolRegistry


class _Model:
    async def stream(self, messages, *, cancel, idempotency_key, tools=None):
        yield TextDelta(type="text.delta", delta="北京")
        yield ModelUsageEvent(type="model.usage", input_tokens=3, output_tokens=2)
        yield ModelFinished(type="model.finished", finish_reason="stop")


def _open(path) -> SqliteStore:
    return SqliteStore(path)


async def test_reopen_keeps_messages_events_checkpoint_and_usage(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    store = _open(path)
    store.add_message("th", "user", "北京今天热吗")
    store.create_run("th", "run_1")
    await execute_run(store, _Model(), ToolRegistry(), "run_1")
    assert store.checkpoint("run_1") == {"through_seq": store.get_run("run_1").seq, "status": "completed"}
    store.close()

    again = _open(path)
    assert again.messages("th") == [
        {"role": "user", "content": "北京今天热吗"},
        {"role": "assistant", "content": "北京"},
    ]
    events = again.since("run_1", 0)
    assert [event.type for event in events][0] == "run.started"
    assert events[-1].type == "run.completed"
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    row = again.usage("run_1")
    assert row is not None
    assert row["input_tokens"] == 3
    assert row["output_tokens"] == 2
    assert row["estimated"] == 1
    assert "content" not in again.columns("usage")
    assert "北京" not in "".join("" if value is None else str(value) for value in row.values())
    again.close()


async def test_delta_does_not_move_the_checkpoint(tmp_path) -> None:
    store = _open(tmp_path / "runs.sqlite")
    store.create_run("th", "run_1")
    started = await store.append("run_1", {"type": "run.started"})
    await store.append("run_1", {"type": "message.started", "message_id": "m1", "role": "assistant"})
    await store.append("run_1", {"type": "message.delta", "message_id": "m1", "delta": "今天"})
    assert store.checkpoint("run_1") == {"through_seq": started.seq, "status": "running"}
    store.close()


async def test_terminal_append_and_a_second_open_run_are_rejected(tmp_path) -> None:
    store = _open(tmp_path / "runs.sqlite")
    store.create_run("th", "run_1")
    await store.append("run_1", {"type": "run.started"})
    with pytest.raises(ValueError):
        store.create_run("th", "run_2")
    await store.append(
        "run_1",
        {"type": "run.failed", "code": "upstream_error", "message": "上游中断", "retryable": True},
    )
    with pytest.raises(RuntimeError):
        await store.append("run_1", {"type": "message.delta", "message_id": "m1", "delta": "再来"})
    store.close()


async def test_trim_raises_the_retained_floor(tmp_path) -> None:
    store = _open(tmp_path / "runs.sqlite")
    store.add_message("th", "user", "北京今天热吗")
    store.create_run("th", "run_1")
    await execute_run(store, _Model(), ToolRegistry(), "run_1")
    floor = store.trim_folded("run_1")
    assert floor > 1
    assert all(event.seq >= floor for event in store.since("run_1", 0))
    assert store.usage("run_1") is not None
    store.close()


def test_status_can_be_repaired_from_the_log(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    store = _open(path)
    store.create_run("th", "run_1")
    store.close()
    db = sqlite3.connect(path)
    db.execute("UPDATE runs SET status = 'running' WHERE run_id = 'run_1'")
    db.commit()
    db.close()
    again = _open(path)
    again.repair_status("run_1", "failed")
    assert again.get_run("run_1").status == "failed"
    again.close()
