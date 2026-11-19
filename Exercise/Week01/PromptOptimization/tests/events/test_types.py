import pytest
from pydantic import ValidationError

from app.events import EVENT_ADAPTER, View, reduce
from app.sse import decode, encode, encode_heartbeat

TRUNK_EVENTS = [
    ({"type": "run.started"}, {}),
    (
        {"type": "message.started", "message_id": "m1", "role": "assistant"},
        {"message_id": "m1", "role": "assistant"},
    ),
    (
        {"type": "message.delta", "message_id": "m1", "delta": "北京"},
        {"message_id": "m1", "delta": "北京"},
    ),
    (
        {"type": "message.completed", "message_id": "m1"},
        {"message_id": "m1"},
    ),
    (
        {"type": "tool.started", "call_id": "c1", "name": "get_weather"},
        {"call_id": "c1", "name": "get_weather"},
    ),
    (
        {"type": "tool.arguments.done", "call_id": "c1", "arguments": '{"city":"北京"}'},
        {"call_id": "c1", "arguments": '{"city":"北京"}'},
    ),
    (
        {"type": "tool.result", "call_id": "c1", "ok": True, "output": "31度"},
        {"call_id": "c1", "ok": True, "output": "31度"},
    ),
    (
        {"type": "run.completed", "usage": {"input_tokens": 12, "output_tokens": 8}},
        {"usage": {"input_tokens": 12, "output_tokens": 8}},
    ),
    (
        {
            "type": "run.failed",
            "code": "MODEL_STREAM_FAILED",
            "message": "上游中断",
            "retryable": True,
        },
        {"code": "MODEL_STREAM_FAILED", "message": "上游中断", "retryable": True},
    ),
    (
        {"type": "run.cancelled", "reason": "用户取消"},
        {"reason": "用户取消"},
    ),
]


@pytest.mark.parametrize(("fields", "expected"), TRUNK_EVENTS)
def test_trunk_types_keep_their_fields(fields: dict, expected: dict) -> None:
    event = EVENT_ADAPTER.validate_python(
        {"seq": 1, "thread_id": "th_1", "run_id": "run_1", **fields}
    )
    dumped = event.model_dump()
    assert dumped["type"] == fields["type"]
    assert dumped["seq"] == 1
    assert dumped["thread_id"] == "th_1"
    assert dumped["run_id"] == "run_1"
    for key, value in expected.items():
        assert dumped[key] == value
    with pytest.raises(ValidationError):
        event.seq = 2


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(
            {
                "seq": 1,
                "type": "message.unknown",
                "thread_id": "th_1",
                "run_id": "run_1",
            }
        )


def test_roundtrip_message_delta() -> None:
    event = EVENT_ADAPTER.validate_python(
        {
            "seq": 6,
            "type": "message.delta",
            "thread_id": "th_1",
            "run_id": "run_1",
            "message_id": "m1",
            "delta": "北京",
        }
    )
    restored = decode(encode(event))
    assert restored == event


def test_missing_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(
            {
                "seq": 1,
                "type": "tool.started",
                "thread_id": "th_1",
                "run_id": "run_1",
                "call_id": "c1",
            }
        )


def test_unknown_field_is_ignored() -> None:
    event = EVENT_ADAPTER.validate_python(
        {
            "seq": 1,
            "type": "run.started",
            "thread_id": "th_1",
            "run_id": "run_1",
            "status": "running",
        }
    )
    assert event.type == "run.started"
    assert not hasattr(event, "status") or "status" not in event.model_dump()


def test_heartbeat_is_not_a_run_event() -> None:
    assert decode(encode_heartbeat()) is None


def test_reduce_stitches_text_and_drops_duplicate_seq() -> None:
    view = View()
    started = EVENT_ADAPTER.validate_python(
        {"seq": 1, "type": "run.started", "thread_id": "th_1", "run_id": "run_1"}
    )
    opened = EVENT_ADAPTER.validate_python(
        {
            "seq": 2,
            "type": "message.started",
            "thread_id": "th_1",
            "run_id": "run_1",
            "message_id": "m1",
            "role": "assistant",
        }
    )
    first = EVENT_ADAPTER.validate_python(
        {
            "seq": 3,
            "type": "message.delta",
            "thread_id": "th_1",
            "run_id": "run_1",
            "message_id": "m1",
            "delta": "北京",
        }
    )
    reduce(view, started)
    reduce(view, opened)
    reduce(view, first)
    reduce(view, first)
    assert view.messages["m1"] == "北京"
    assert view.last_seq == 3


def test_reduce_weather_log() -> None:
    bodies = [
        {"type": "run.started"},
        {"type": "tool.started", "call_id": "c1", "name": "get_weather"},
        {"type": "tool.arguments.done", "call_id": "c1", "arguments": '{"city":"北京"}'},
        {"type": "tool.result", "call_id": "c1", "ok": True, "output": "31度"},
        {"type": "message.started", "message_id": "m1", "role": "assistant"},
        {"type": "message.delta", "message_id": "m1", "delta": "北京"},
        {"type": "message.delta", "message_id": "m1", "delta": "今天 31 度"},
        {"type": "message.completed", "message_id": "m1"},
        {"type": "run.completed", "usage": {"input_tokens": 12, "output_tokens": 8}},
    ]
    events = [
        EVENT_ADAPTER.validate_python({"seq": seq, "thread_id": "th_1", "run_id": "run_1", **body})
        for seq, body in enumerate(bodies, start=1)
    ]
    view = View()
    for event in events:
        reduce(view, event)
    text = view.messages["m1"]
    reduce(view, events[5])
    assert view.messages["m1"] == text == "北京今天 31 度"
    assert view.closed_messages == {"m1"}
    assert view.tools["c1"] == {
        "name": "get_weather",
        "output": "31度",
        "arguments": '{"city":"北京"}',
        "ok": True,
    }
    assert view.status == "completed"
    assert view.done is True
    assert view.usage is not None
    assert view.usage.input_tokens == 12
    assert view.usage.output_tokens == 8
    assert view.last_seq == 9
