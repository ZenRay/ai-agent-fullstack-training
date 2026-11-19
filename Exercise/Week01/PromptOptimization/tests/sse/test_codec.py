import pytest

from app.events import EVENT_ADAPTER
from app.sse import (
    SSE_HEADERS,
    FrameBuffer,
    encode,
    encode_heartbeat,
    encode_retry,
    parse_frame,
)
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


def _delta():
    return EVENT_ADAPTER.validate_python(
        {
            "seq": 6,
            "type": "message.delta",
            "thread_id": "th_1",
            "run_id": "run_1",
            "message_id": "m1",
            "delta": "北京",
        }
    )


def _events(frames: list[str]):
    return [parse_frame(frame) for frame in frames]


def test_encode_uses_seq_type_and_payload() -> None:
    event = _started()
    text = encode(event)
    lines = text.splitlines()
    assert lines[0] == "id: 1"
    assert lines[1] == "event: run.started"
    assert lines[2] == f"data: {event.model_dump_json()}"
    assert "\r\n" not in text
    assert text.endswith("\n\n")


def test_encode_retry_is_its_own_frame() -> None:
    assert encode_retry() == "retry: 3000\n\n"
    assert encode_retry(1000) == "retry: 1000\n\n"


def test_response_headers_disable_proxy_buffering() -> None:
    assert SSE_HEADERS == {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }


def test_partial_frame_waits_for_blank_line() -> None:
    frame = encode(_started())
    buffer = FrameBuffer()
    assert buffer.feed(frame[:15]) == []
    assert _events(buffer.feed(frame[15:])) == [_started()]


def test_one_read_can_contain_several_frames() -> None:
    buffer = FrameBuffer()
    assert _events(buffer.feed(encode(_started()) + encode(_failed()))) == [_started(), _failed()]


def test_heartbeat_retry_and_empty_data_are_not_events() -> None:
    blob = encode_heartbeat() + encode_retry() + "data:\n\n" + "data: \n\n"
    assert _events(FrameBuffer().feed(blob)) == [None, None, None, None]


def test_comment_line_inside_frame_is_ignored() -> None:
    event = _started()
    lines = encode(event).split("\n")
    commented = "\n".join([lines[0], ": note", *lines[1:]])
    assert _events(FrameBuffer().feed(commented)) == [event]


def test_multiline_data_joins_with_newline() -> None:
    event = _delta()
    payload = event.model_dump_json()
    split_at = payload.index(",") + 1
    frame = f"id: 6\nevent: message.delta\ndata: {payload[:split_at]}\ndata: {payload[split_at:]}\n\n"
    assert _events(FrameBuffer().feed(frame)) == [event]


def test_utf8_character_split_across_chunks_still_parses() -> None:
    payload = (
        '{"seq":6,"type":"message.delta","thread_id":"th_1",'
        '"run_id":"run_1","message_id":"m1","delta":"北京"}'
    )
    raw = f"id: 6\nevent: message.delta\ndata: {payload}\n\n".encode()
    cut = raw.index("北".encode()) + 1
    buffer = FrameBuffer()
    assert buffer.feed_bytes(raw[:cut]) == []
    parsed = _events(buffer.feed_bytes(raw[cut:]))
    assert parsed == [_delta()]


@pytest.mark.asyncio
async def test_heartbeat_between_frames_is_not_stored() -> None:
    started = _started()
    failed = _failed()
    blob = encode(started) + encode_heartbeat() + encode_retry() + encode(failed)
    events = [event for event in _events(FrameBuffer().feed(blob)) if event is not None]
    assert events == [started, failed]

    store = MemoryStore()
    store.create_run("th_1", "run_1")
    for event in events:
        await store.append("run_1", event.model_dump(exclude={"seq", "thread_id", "run_id"}))
    stored = store.get_run("run_1").events
    assert [event.type for event in stored] == ["run.started", "run.failed"]
