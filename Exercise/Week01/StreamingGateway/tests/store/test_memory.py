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
