import asyncio
import copy
import json

import httpx

from app.main import create_app
from app.model.events import ModelFinished, TextDelta
from app.sse.codec import FrameBuffer, parse_frame
from app.store.memory import MemoryStore

ANSWER = [
    TextDelta(type="text.delta", delta="好的"),
    ModelFinished(type="model.finished", finish_reason="stop"),
]


class ScriptedModel:
    def __init__(self, script: list | None = None, release: asyncio.Event | None = None) -> None:
        self.script = ANSWER if script is None else script
        self.release = release
        self.calls: list[dict] = []

    async def stream(self, messages, *, cancel, idempotency_key, tools=None):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "key": idempotency_key,
                "cancel": cancel,
            }
        )
        if self.release is not None:
            await self.release.wait()
        if cancel.is_set():
            return
        for event in self.script:
            if cancel.is_set():
                return
            yield event


def _events(text: str) -> list:
    buffer = FrameBuffer()
    found = []
    for frame in buffer.feed(text):
        event = parse_frame(frame)
        if event is not None:
            found.append(event)
    return found


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class LiveResponse:
    """按块读 ASGI 响应。httpx 的 ASGI 传输会等整段流结束才返回。"""

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        chunks: asyncio.Queue[bytes | None],
        task: asyncio.Task[None],
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._chunks = chunks
        self._task = task

    async def aclose(self) -> None:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            return

    async def read_text(self) -> str:
        parts: list[str] = []
        while True:
            chunk = await self._chunks.get()
            if chunk is None:
                break
            parts.append(chunk.decode())
        await self._task
        return "".join(parts)


async def _open(app, thread_id: str, message: str, headers: dict | None = None) -> LiveResponse:
    raw_headers = [(b"content-type", b"application/json")]
    if headers:
        raw_headers.extend((key.lower().encode(), value.encode()) for key, value in headers.items())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "method": "POST",
        "headers": raw_headers,
        "scheme": "http",
        "path": f"/v1/threads/{thread_id}/runs",
        "raw_path": f"/v1/threads/{thread_id}/runs".encode(),
        "query_string": b"",
        "server": ("test", 80),
        "client": ("127.0.0.1", 123),
    }
    payload = json.dumps({"message": message}).encode()
    sent = False
    chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
    started = asyncio.Event()
    meta: dict[str, object] = {}

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Future()

    async def send(message_event):
        if message_event["type"] == "http.response.start":
            meta["status"] = message_event["status"]
            meta["headers"] = {
                key.decode().lower(): value.decode() for key, value in message_event["headers"]
            }
            started.set()
        elif message_event["type"] == "http.response.body":
            body = message_event.get("body", b"")
            if body:
                await chunks.put(body)
            if not message_event.get("more_body", False):
                await chunks.put(None)

    task = asyncio.create_task(app(scope, receive, send))
    await started.wait()
    return LiveResponse(meta["status"], meta["headers"], chunks, task)


async def _until_running(store: MemoryStore, thread_id: str) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2
    while True:
        active = store.active_run(thread_id)
        if active is not None and active.status == "running":
            return active.run_id
        if loop.time() > deadline:
            raise AssertionError("运行没有进入 running")
        await asyncio.sleep(0.01)


async def _until_status(store: MemoryStore, run_id: str, status: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2
    while store.get_run(run_id).status != status:
        if loop.time() > deadline:
            raise AssertionError(store.get_run(run_id).status)
        await asyncio.sleep(0.01)


async def test_missing_message_does_not_create_a_run() -> None:
    app = create_app(model=ScriptedModel())
    store: MemoryStore = app.state.store
    async with _client(app) as client:
        empty = await client.post("/v1/threads/th/runs", json={"message": "  "})
        missing = await client.post("/v1/threads/th/runs", json={})
        invalid = await client.post(
            "/v1/threads/th/runs",
            content=b"{",
            headers={"Content-Type": "application/json"},
        )
    assert empty.status_code == 400
    assert missing.status_code == 400
    assert invalid.status_code == 400
    assert store._runs == {}
    assert store.messages("th") == []


async def test_create_streams_until_completed_and_keeps_the_user_message() -> None:
    app = create_app(model=ScriptedModel())
    store: MemoryStore = app.state.store
    async with _client(app) as client:
        response = await _open(app, "th", "你好")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache, no-transform"
        assert response.headers["x-accel-buffering"] == "no"
        run_id = response.headers["x-run-id"]
        events = _events(await response.read_text())
    assert [event.type for event in events[:1]] == ["run.started"]
    assert events[-1].type == "run.completed"
    assert events[0].run_id == run_id
    assert store.messages("th")[0] == {"role": "user", "content": "你好"}
    status = None
    async with _client(app) as client:
        status = await client.get(f"/v1/runs/{run_id}")
    assert status.json() == {
        "run_id": run_id,
        "thread_id": "th",
        "status": "completed",
        "seq": events[-1].seq,
    }


async def test_a_busy_thread_rejects_a_second_run() -> None:
    release = asyncio.Event()
    app = create_app(model=ScriptedModel(release=release))
    store: MemoryStore = app.state.store
    async with _client(app) as client:
        response = await _open(app, "th", "第一问")
        run_id = response.headers["x-run-id"]
        assert await _until_running(store, "th") == run_id
        rejected = await client.post("/v1/threads/th/runs", json={"message": "第二问"})
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == {"run_id": run_id}
        assert store.messages("th") == [{"role": "user", "content": "第一问"}]
        release.set()
        await response.read_text()
    assert store.get_run(run_id).status == "completed"


async def test_same_idempotency_key_replays_the_same_run() -> None:
    release = asyncio.Event()
    app = create_app(model=ScriptedModel(release=release))
    store: MemoryStore = app.state.store
    headers = {"Idempotency-Key": "same"}
    async with _client(app) as client:
        first = await _open(app, "th", "你好", headers)
        run_id = first.headers["x-run-id"]
        await _until_running(store, "th")
        replay = await _open(app, "th", "你好", headers)
        assert replay.headers["x-run-id"] == run_id
        assert len(store._runs) == 1
        assert store.messages("th") == [{"role": "user", "content": "你好"}]
        release.set()
        assert _events(await first.read_text())[-1].type == "run.completed"
        assert _events(await replay.read_text())[-1].run_id == run_id
        conflict = await client.post(
            "/v1/threads/th/runs",
            json={"message": "另一问"},
            headers=headers,
        )
    assert conflict.status_code == 409
    assert [item for item in store.messages("th") if item["role"] == "user"] == [
        {"role": "user", "content": "你好"}
    ]
    assert len(store._runs) == 1


async def test_cancel_sets_a_mark_and_a_second_cancel_does_not_append_another_event() -> None:
    release = asyncio.Event()
    app = create_app(model=ScriptedModel(release=release))
    store: MemoryStore = app.state.store
    async with _client(app) as client:
        response = await _open(app, "th", "停一下")
        run_id = response.headers["x-run-id"]
        await _until_running(store, "th")
        cancelling = await client.post(f"/v1/runs/{run_id}/cancel")
        assert cancelling.status_code == 202
        assert cancelling.json() == {"run_id": run_id, "status": "cancelling"}
        current = await client.get(f"/v1/runs/{run_id}")
        assert current.json()["status"] == "running"
        release.set()
        await response.read_text()
        await _until_status(store, run_id, "cancelled")
        again = await client.post(f"/v1/runs/{run_id}/cancel")
        missing = await client.post("/v1/runs/missing/cancel")
    assert again.status_code == 200
    assert again.json() == {"run_id": run_id, "status": "cancelled"}
    assert missing.status_code == 404
    assert [event.type for event in store.since(run_id, 0)].count("run.cancelled") == 1
    assert store.messages("th") == [{"role": "user", "content": "停一下"}]


async def test_cancel_of_a_completed_run_conflicts() -> None:
    app = create_app(model=ScriptedModel())
    async with _client(app) as client:
        response = await _open(app, "th", "你好")
        run_id = response.headers["x-run-id"]
        await response.read_text()
        rejected = await client.post(f"/v1/runs/{run_id}/cancel")
        unknown = await client.get("/v1/runs/missing")
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["status"] == "completed"
    assert unknown.status_code == 404


async def test_closing_the_response_does_not_cancel_the_run() -> None:
    release = asyncio.Event()
    app = create_app(model=ScriptedModel(release=release))
    store: MemoryStore = app.state.store
    response = await _open(app, "th", "你好")
    run_id = response.headers["x-run-id"]
    await _until_running(store, "th")
    await response.aclose()
    assert not store.get_run(run_id).cancel_event.is_set()
    release.set()
    await _until_status(store, run_id, "completed")
    assert not store.get_run(run_id).cancel_event.is_set()
    assert [event.type for event in store.since(run_id, 0)][-1] == "run.completed"
    assert len(app.state.tasks) == 0


async def test_create_without_a_model_is_rejected_before_a_run_exists() -> None:
    app = create_app()
    store: MemoryStore = app.state.store
    async with _client(app) as client:
        rejected = await client.post("/v1/threads/th/runs", json={"message": "你好"})
    assert rejected.status_code == 500
    assert store._runs == {}
    assert store.messages("th") == []
