import pytest

from app.events import EVENT_ADAPTER
from app.sse import FrameDecoder, encode, encode_heartbeat
from app.store import MemoryStore


def _started():
    return EVENT_ADAPTER.validate_python(
        {"seq": 1, "type": "run.started", "thread_id": "th_1", "run_id": "run_1"}
    )


def _failed():
    return EVENT_ADAPTER.validate_python(
        {
            "seq": 2,
            "type": "run.failed",
            "thread_id": "th_1",
            "run_id": "run_1",
            "code": "MODEL_STREAM_FAILED",
            "message": "上游中断",
            "retryable": True,
        }
    )


def test_encode_uses_seq_type_and_payload() -> None:
    event = _started()
    text = encode(event)
    lines = text.splitlines()
    assert lines[0] == "id: 1"
    assert lines[1] == "event: run.started"
    assert lines[2].startswith("data: ")
    assert text.endswith("\n\n")


def test_partial_frame_waits_for_blank_line() -> None:
    frame = encode(_started())
    decoder = FrameDecoder()
    assert decoder.feed(frame[:15]) == []
    assert decoder.feed(frame[15:]) == [_started()]


def test_crlf_frame_split_on_carriage_return_still_decodes() -> None:
    frame = encode(_started()).replace("\n", "\r\n")
    cut = frame.index("\r\n") + 1
    decoder = FrameDecoder()
    assert decoder.feed(frame[:cut]) == []
    assert decoder.feed(frame[cut:]) == [_started()]


@pytest.mark.asyncio
async def test_heartbeat_between_frames_is_dropped() -> None:
    started = _started()
    failed = _failed()
    blob = encode(started) + encode_heartbeat() + encode(failed)
    decoder = FrameDecoder()
    midpoint = len(encode(started)) + 4
    events = decoder.feed(blob[:midpoint]) + decoder.feed(blob[midpoint:])
    assert events == [started, failed]

    store = MemoryStore()
    store.create_run("th_1", "run_1")
    for event in events:
        await store.append("run_1", event.model_dump(exclude={"seq", "thread_id", "run_id"}))
    stored = store.get_run("run_1").events
    assert [event.type for event in stored] == ["run.started", "run.failed"]
    assert len(stored) == len(events)
