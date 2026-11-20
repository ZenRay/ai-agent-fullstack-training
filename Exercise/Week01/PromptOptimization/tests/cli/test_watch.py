import asyncio
import io

import httpx

from app.main import create_app
from app.model.events import ModelFinished, TextDelta, ToolCall
from app.store.memory import MemoryStore
from cli.present import format_view
from cli.watch import interrupt, stop, watch_run


class ScriptedModel:
    def __init__(self, turns: list[list], release: asyncio.Event | None = None) -> None:
        self.turns = turns
        self.release = release
        self.index = 0

    async def stream(
        self,
        messages,
        *,
        cancel,
        idempotency_key,
        tools=None,
        model=None,
        temperature=None,
        tool_choice=None,
    ):
        if self.release is not None and self.index == 0:
            await _wait_release_or_cancel(self.release, cancel)
        if cancel.is_set():
            return
        script = self.turns[self.index]
        self.index += 1
        for event in script:
            if cancel.is_set():
                return
            yield event


class TextThenWait:
    """先交出一段文字，再等取消或放行。"""

    def __init__(self, release: asyncio.Event) -> None:
        self.release = release

    async def stream(
        self,
        messages,
        *,
        cancel,
        idempotency_key,
        tools=None,
        model=None,
        temperature=None,
        tool_choice=None,
    ):
        yield TextDelta(type="text.delta", delta="北京")
        await _wait_release_or_cancel(self.release, cancel)
        if cancel.is_set():
            return
        yield ModelFinished(type="model.finished", finish_reason="stop")


async def _wait_release_or_cancel(release: asyncio.Event, cancel: asyncio.Event) -> None:
    release_task = asyncio.create_task(release.wait())
    cancel_task = asyncio.create_task(cancel.wait())
    await asyncio.wait({release_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in (release_task, cancel_task):
        if not task.done():
            task.cancel()
    await asyncio.gather(release_task, cancel_task, return_exceptions=True)


def _client(app, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport or httpx.ASGITransport(app=app),
        base_url="http://test",
    )


class StreamingTransport(httpx.AsyncBaseTransport):
    """响应头一到就交出正文，方便在运行过程中取消或切断。"""

    def __init__(self, app) -> None:
        self.app = app
        self._streams: list[asyncio.Queue[bytes | None]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = b""
        async for part in request.stream:
            body += part
        chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        started = asyncio.Event()
        finished = asyncio.Event()
        meta: dict[str, object] = {}
        bodies: list[bytes] = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Future()

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                meta["status"] = message["status"]
                meta["headers"] = {
                    key.decode().lower(): value.decode() for key, value in message["headers"]
                }
                started.set()
            elif message["type"] == "http.response.body":
                payload = message.get("body", b"")
                if payload:
                    bodies.append(payload)
                    await chunks.put(payload)
                if not message.get("more_body", False):
                    finished.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": ("test", 80),
            "client": ("127.0.0.1", 123),
        }

        async def run() -> None:
            try:
                await self.app(scope, receive, send)
            finally:
                finished.set()
                await chunks.put(None)

        task = asyncio.create_task(run())
        await _until_started(started, task)
        headers = meta["headers"]
        assert isinstance(headers, dict)
        if str(headers.get("content-type", "")).startswith("text/event-stream"):
            self._streams.append(chunks)
            return httpx.Response(int(meta["status"]), headers=headers, stream=_QueueStream(chunks, task))
        await finished.wait()
        await task
        return httpx.Response(int(meta["status"]), headers=headers, content=b"".join(bodies))

    async def cut(self) -> None:
        for chunks in self._streams:
            await chunks.put(None)


class _QueueStream(httpx.AsyncByteStream):
    def __init__(self, chunks: asyncio.Queue[bytes | None], task: asyncio.Task[None]) -> None:
        self._chunks = chunks
        self._task = task

    async def __aiter__(self):
        while True:
            chunk = await self._chunks.get()
            if chunk is None:
                return
            yield chunk

    async def aclose(self) -> None:
        await self._chunks.put(None)
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                return


async def _until_started(started: asyncio.Event, task: asyncio.Task[None]) -> None:
    waiter = asyncio.create_task(started.wait())
    done, _pending = await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
    if task in done and not waiter.done():
        waiter.cancel()
        exception = task.exception()
        if exception is not None:
            raise exception


async def _until(ready) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2
    while not ready():
        if loop.time() > deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.01)


def _weather_turns() -> list[list]:
    return [
        [
            ToolCall(type="tool_call", call_id="c1", name="get_weather", arguments='{"city":"北京"}'),
            ModelFinished(type="model.finished", finish_reason="tool_use"),
        ],
        [
            TextDelta(type="text.delta", delta="带上伞"),
            ModelFinished(type="model.finished", finish_reason="stop"),
        ],
    ]


async def test_weather_run_prints_the_tool_before_the_answer() -> None:
    app = create_app(model=ScriptedModel(_weather_turns()))
    out = io.StringIO()
    async with _client(app) as client:
        result = await watch_run(client, "th", "北京今天热吗", out=out)
    text = format_view(result.view)
    assert text == "[get_weather] 北京今天晴，31度。\n带上伞"
    assert out.getvalue().startswith("[get_weather] 北京今天晴，31度。\n带上伞")
    assert result.view.status == "completed"
    assert result.view.done


async def test_a_cut_stream_stays_incomplete_and_does_not_cancel_the_run() -> None:
    release = asyncio.Event()
    app = create_app(model=TextThenWait(release))
    store: MemoryStore = app.state.store
    transport = StreamingTransport(app)
    seen = asyncio.Event()

    def notice(view) -> None:
        if any(text == "北京" for text in view.messages.values()):
            seen.set()

    async with _client(app, transport) as client:
        task = asyncio.create_task(watch_run(client, "th", "北京今天热吗", on_event=notice))
        await seen.wait()
        await transport.cut()
        result = await task
        run_id = result.run_id
        assert run_id is not None
        assert not store.get_run(run_id).cancel_event.is_set()
        release.set()
        await _until(lambda: store.get_run(run_id).status == "completed")
    assert result.view.status == "incomplete"
    assert result.view.done is False
    assert result.view.messages == {"m1": "北京"}
    assert not store.get_run(run_id).cancel_event.is_set()


async def test_stop_writes_one_cancelled_event() -> None:
    release = asyncio.Event()
    app = create_app(model=ScriptedModel([[]], release))
    store: MemoryStore = app.state.store
    holder: dict[str, str] = {}
    async with _client(app, StreamingTransport(app)) as client:
        task = asyncio.create_task(
            watch_run(client, "th", "停一下", on_run_id=lambda run_id: holder.setdefault("id", run_id))
        )
        await _until(lambda: "id" in holder)
        response = await stop(client, holder["id"])
        result = await task
    assert response.status_code == 202
    assert result.view.status == "cancelled"
    assert [event.type for event in store.since(holder["id"], 0)].count("run.cancelled") == 1


async def test_interrupt_stops_the_run_and_then_the_reader() -> None:
    release = asyncio.Event()
    app = create_app(model=ScriptedModel([[]], release))
    store: MemoryStore = app.state.store
    holder: dict[str, str] = {}

    async def hang() -> None:
        await asyncio.Event().wait()

    async with _client(app, StreamingTransport(app)) as client:
        watch = asyncio.create_task(
            watch_run(client, "th", "停一下", on_run_id=lambda run_id: holder.setdefault("id", run_id))
        )
        await _until(lambda: "id" in holder)
        reader = asyncio.create_task(hang())
        await interrupt(client, holder["id"], reader)
        await _until(lambda: store.get_run(holder["id"]).status == "cancelled")
        await watch
    assert reader.cancelled()
    assert [event.type for event in store.since(holder["id"], 0)].count("run.cancelled") == 1


async def test_a_busy_thread_returns_the_existing_run_without_a_new_view() -> None:
    release = asyncio.Event()
    app = create_app(model=ScriptedModel([[]], release))
    holder: dict[str, str] = {}
    async with _client(app, StreamingTransport(app)) as client:
        first = asyncio.create_task(
            watch_run(client, "th", "第一问", on_run_id=lambda run_id: holder.setdefault("id", run_id))
        )
        await _until(lambda: "id" in holder)
        second = await watch_run(client, "th", "第二问")
        release.set()
        await first
    assert second.busy_run_id == holder["id"]
    assert second.view.last_seq == 0
    assert second.view.status == "idle"
