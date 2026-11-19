import pytest

from app.store import MemoryStore


@pytest.mark.asyncio
async def test_append_assigns_seq_and_since_reads_tail() -> None:
    store = MemoryStore()
    store.create_run("th_1", "run_1")
    first = await store.append("run_1", {"type": "run.started"})
    second = await store.append(
        "run_1",
        {"type": "message.started", "message_id": "m1", "role": "assistant"},
    )
    assert first.seq == 1
    assert second.seq == 2
    assert [event.seq for event in store.since("run_1", 1)] == [2]


@pytest.mark.asyncio
async def test_terminal_event_closes_the_run() -> None:
    store = MemoryStore()
    store.create_run("th_1", "run_1")
    await store.append("run_1", {"type": "run.started"})
    await store.append(
        "run_1",
        {
            "type": "run.failed",
            "code": "MODEL_STREAM_FAILED",
            "message": "上游中断",
            "retryable": True,
        },
    )
    with pytest.raises(RuntimeError):
        await store.append(
            "run_1",
            {"type": "message.delta", "message_id": "m1", "delta": "再来"},
        )


@pytest.mark.asyncio
async def test_run_can_open_with_failure() -> None:
    store = MemoryStore()
    store.create_run("th_1", "run_1")
    failed = await store.append(
        "run_1",
        {
            "type": "run.failed",
            "code": "BAD_REQUEST",
            "message": "参数无效",
            "retryable": False,
        },
    )
    assert failed.seq == 1
    assert failed.type == "run.failed"
    assert store.get_run("run_1").status == "failed"


@pytest.mark.asyncio
async def test_first_event_rejects_message_delta() -> None:
    store = MemoryStore()
    store.create_run("th_1", "run_1")
    with pytest.raises(ValueError):
        await store.append(
            "run_1",
            {"type": "message.delta", "message_id": "m1", "delta": "北京"},
        )
    assert store.since("run_1", 0) == []
    started = await store.append("run_1", {"type": "run.started"})
    assert started.seq == 1


@pytest.mark.asyncio
async def test_runs_keep_independent_sequences_from_thread_messages() -> None:
    store = MemoryStore()
    store.add_message("th_1", "user", "北京今天热吗")
    first = store.create_run("th_1", "run_1")
    second = store.create_run("th_1", "run_2")
    assert first.status == "queued"
    assert second.status == "queued"
    started = await store.append("run_1", {"type": "run.started"})
    other = await store.append("run_2", {"type": "run.started"})
    assert started.seq == 1
    assert other.seq == 1
    assert store.get_run("run_1").version == 1
    assert store.messages("th_1") == [{"role": "user", "content": "北京今天热吗"}]
    assert [event.run_id for event in store.since("run_1", 0)] == ["run_1"]
    with pytest.raises(ValueError):
        store.since("run_1", -1)


@pytest.mark.asyncio
async def test_cancel_marks_the_run_without_writing_an_event() -> None:
    store = MemoryStore()
    store.add_message("th_1", "user", "北京今天热吗")
    store.create_run("th_1", "run_1")
    await store.append("run_1", {"type": "run.started"})
    marked = store.request_cancel("run_1")
    assert marked.cancel_event.is_set()
    assert marked.status == "running"
    assert [event.type for event in store.since("run_1", 0)] == ["run.started"]
    assert store.messages("th_1") == [{"role": "user", "content": "北京今天热吗"}]

    await store.append("run_1", {"type": "run.cancelled", "reason": "用户取消"})
    again = store.request_cancel("run_1")
    assert again.status == "cancelled"
    assert [event.type for event in store.since("run_1", 0)] == ["run.started", "run.cancelled"]
