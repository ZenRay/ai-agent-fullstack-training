import asyncio

import pytest

from app.events import EVENT_ADAPTER
from app.sse import Subscription, encode, encode_heartbeat, encode_retry
from app.store import MemoryStore


def _started():
    return EVENT_ADAPTER.validate_python(
        {"seq": 1, "type": "run.started", "thread_id": "th_1", "run_id": "run_1"}
    )


def _failed():
    return EVENT_ADAPTER.validate_python(
        {
            "seq": 1,
            "type": "run.failed",
            "thread_id": "th_1",
            "run_id": "run_1",
            "code": "MODEL_STREAM_FAILED",
            "message": "上游中断",
            "retryable": True,
        }
    )


@pytest.mark.asyncio
async def test_idle_subscription_emits_heartbeat() -> None:
    subscription = Subscription()
    frames = subscription.frames(heartbeat_interval=0.01)
    assert await anext(frames) == encode_retry()
    assert await asyncio.wait_for(anext(frames), 1) == encode_heartbeat()
    await frames.aclose()


@pytest.mark.asyncio
async def test_terminal_event_ends_the_stream() -> None:
    failed = _failed()
    subscription = Subscription()
    assert subscription.offer(failed)
    frames = subscription.frames(heartbeat_interval=5)
    assert await anext(frames) == encode_retry()
    assert await anext(frames) == encode(failed)
    with pytest.raises(StopAsyncIteration):
        await anext(frames)


@pytest.mark.asyncio
async def test_disconnect_stops_only_this_stream() -> None:
    store = MemoryStore()
    store.create_run("th_1", "run_1")
    started = await store.append("run_1", {"type": "run.started"})
    subscription = Subscription()
    assert subscription.offer(started)
    frames = subscription.frames(heartbeat_interval=5)
    assert await anext(frames) == encode_retry()
    assert await anext(frames) == encode(started)

    async def read_next() -> str | None:
        try:
            return await anext(frames)
        except StopAsyncIteration:
            return None

    pending = asyncio.create_task(read_next())
    await asyncio.sleep(0.01)
    subscription.close()
    assert await pending is None

    continued = await store.append(
        "run_1",
        {"type": "message.started", "message_id": "m1", "role": "assistant"},
    )
    assert continued.seq == 2
    assert store.get_run("run_1").status == "running"
    assert not store.get_run("run_1").cancel_event.is_set()


def test_full_queue_rejects_without_blocking() -> None:
    subscription = Subscription(maxsize=1)
    assert subscription.offer(_started()) is True
    assert subscription.offer(_failed()) is False
    assert subscription.disconnected.is_set()
    assert subscription.offer(_started()) is False
