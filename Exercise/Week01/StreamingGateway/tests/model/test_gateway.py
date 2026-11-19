import asyncio
import json

import httpx
import pytest

from app.model.gateway import GatewayModel
from app.model.openai_stream import ChatStreamTranslator

MESSAGES = [{"role": "user", "content": "北京今天热吗"}]
VIRTUAL_KEY = "virtual-key"
PROVIDER_KEY = "sk-provider-secret"


def _data(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _choice(delta: dict, finish_reason: str | None = None) -> dict:
    return {"choices": [{"delta": delta, "finish_reason": finish_reason}]}


class _ExplodingStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes) -> None:
        self._first = first

    async def __aiter__(self):
        yield self._first
        raise httpx.ReadError("断开")

    async def aclose(self) -> None:
        return None


class _PausingStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes) -> None:
        self._first = first

    async def __aiter__(self):
        yield self._first
        await asyncio.sleep(30)
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        return None


def _model(handler) -> GatewayModel:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GatewayModel(
        base_url="http://gateway.test",
        api_key=VIRTUAL_KEY,
        model="chat-default",
        client=client,
    )


async def _events(model: GatewayModel, cancel: asyncio.Event | None = None, tools=None):
    found = []
    async for event in model.stream(
        MESSAGES,
        cancel=cancel or asyncio.Event(),
        idempotency_key="idem-1",
        tools=tools,
    ):
        found.append(event)
    return found


@pytest.mark.asyncio
async def test_request_uses_virtual_key_and_public_model() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["idempotency"] = request.headers["Idempotency-Key"]
        seen["body"] = json.loads(request.content)
        assert PROVIDER_KEY not in request.content.decode()
        frames = "".join(
            [
                ": keep-alive\n\n",
                _data(_choice({"content": "北京"})),
                _data(_choice({"content": ""})),
                _data(_choice({}, "stop")),
                _data({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}),
                "data: [DONE]\n\n",
            ]
        )
        return httpx.Response(200, content=frames.encode())

    events = await _events(_model(handler))
    assert seen["auth"] == f"Bearer {VIRTUAL_KEY}"
    assert seen["idempotency"] == "idem-1"
    assert seen["body"]["model"] == "chat-default"
    assert seen["body"]["messages"] == MESSAGES
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert "tools" not in seen["body"]
    assert [event.type for event in events] == ["text.delta", "model.finished", "model.usage"]
    assert events[0].delta == "北京"
    assert events[2].input_tokens == 3
    assert events[2].output_tokens == 2


def _tool_delta(arguments: str, *, name: str | None = None, call_id: str | None = None) -> dict:
    function: dict[str, str] = {"arguments": arguments}
    if name is not None:
        function["name"] = name
    call: dict = {"index": 0, "function": function}
    if call_id is not None:
        call["id"] = call_id
    return {"tool_calls": [call]}


def test_tool_arguments_are_emitted_only_when_complete() -> None:
    translator = ChatStreamTranslator()
    opened = _data(_choice(_tool_delta("", name="get_weather", call_id="c1")))
    partial = _data(_choice(_tool_delta('{"city":')))
    assert translator.feed(opened) == []
    assert translator.feed(partial) == []
    events = translator.feed(_data(_choice(_tool_delta('"北京"}'), "tool_calls")))
    assert [event.type for event in events] == ["tool_call", "model.finished"]
    assert events[0].call_id == "c1"
    assert events[0].name == "get_weather"
    assert events[0].arguments == '{"city":"北京"}'


def test_incomplete_tool_arguments_fail_without_a_call() -> None:
    translator = ChatStreamTranslator()
    translator.feed(_data(_choice(_tool_delta("{", name="get_weather", call_id="c1"))))
    events = translator.finish()
    assert [event.type for event in events] == ["model.failed"]
    assert events[0].phase == "after_content"
    assert events[0].code == "incomplete_tool_call"


@pytest.mark.asyncio
async def test_status_errors_before_content_are_classified() -> None:
    def limited(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "2"})

    limited_events = await _events(_model(limited))
    assert limited_events[0].code == "rate_limit"
    assert limited_events[0].retryable is True
    assert limited_events[0].phase == "before_content"
    assert limited_events[0].retry_after_ms == 2000

    def missing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    missing_events = await _events(_model(missing))
    assert missing_events[0].code == "model_not_found"
    assert missing_events[0].retryable is False
    assert missing_events[0].phase == "before_content"


@pytest.mark.asyncio
async def test_disconnect_after_text_fails_without_switching_phase() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_ExplodingStream(_data(_choice({"content": "北京"})).encode()))

    events = await _events(_model(handler))
    assert [event.type for event in events] == ["text.delta", "model.failed"]
    assert events[1].phase == "after_content"
    assert events[1].code == "upstream_error"


@pytest.mark.asyncio
async def test_cancel_stops_without_a_failure() -> None:
    cancel = asyncio.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_PausingStream(_data(_choice({"content": "北"})).encode()))

    events = []
    async for event in _model(handler).stream(MESSAGES, cancel=cancel, idempotency_key="idem-1"):
        events.append(event)
        cancel.set()
    assert [event.type for event in events] == ["text.delta"]
