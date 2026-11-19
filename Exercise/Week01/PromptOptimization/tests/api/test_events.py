import asyncio

import httpx

from app.loop.run import execute_run
from app.main import create_app
from app.store.sqlite import SqliteStore
from tests.api.test_runs import LiveResponse, ScriptedModel, _events


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _open_get(app, run_id: str, since: int = 0) -> LiveResponse:
    path = f"/v1/runs/{run_id}/events"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "method": "GET",
        "headers": [],
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": f"since={since}".encode(),
        "server": ("test", 80),
        "client": ("127.0.0.1", 123),
    }
    chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
    started = asyncio.Event()
    meta: dict[str, object] = {}

    async def receive():
        await asyncio.Future()

    async def send(message):
        if message["type"] == "http.response.start":
            meta["status"] = message["status"]
            meta["headers"] = {key.decode().lower(): value.decode() for key, value in message["headers"]}
            started.set()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                await chunks.put(body)
            if not message.get("more_body", False):
                await chunks.put(None)

    task = asyncio.create_task(app(scope, receive, send))
    await started.wait()
    return LiveResponse(meta["status"], meta["headers"], chunks, task)


async def _completed(tmp_path):
    store = SqliteStore(tmp_path / "runs.sqlite")
    app = create_app(store=store, model=ScriptedModel())
    store.add_message("th", "user", "你好")
    store.create_run("th", "run_1")
    await execute_run(store, app.state.model, app.state.tools, "run_1")
    return app, store


async def test_replay_returns_events_strictly_after_the_cursor(tmp_path) -> None:
    app, store = await _completed(tmp_path)
    async with _client(app) as client:
        response = await client.get("/v1/runs/run_1/events", params={"since": 1})
        missing = await client.get("/v1/runs/missing/events")
        negative = await client.get("/v1/runs/run_1/events", params={"since": -1})
    assert response.status_code == 200
    events = _events(response.text)
    assert events
    assert all(event.seq > 1 for event in events)
    assert [event.seq for event in events] == list(range(events[0].seq, events[-1].seq + 1))
    assert events[-1].type == "run.completed"
    assert missing.status_code == 404
    assert negative.status_code == 400
    assert store.get_run("run_1").run_id == "run_1"
    store.close()


async def test_cursor_before_the_window_is_a_gap(tmp_path) -> None:
    app, store = await _completed(tmp_path)
    floor = store.trim_folded("run_1")
    async with _client(app) as client:
        response = await client.get("/v1/runs/run_1/events", params={"since": 0})
    assert response.status_code == 409
    assert response.json() == {"gap": True, "thread_id": "th", "min_seq": floor}
    assert "好的" not in response.text
    store.close()


async def test_closing_the_replay_does_not_cancel_or_create_a_run(tmp_path) -> None:
    store = SqliteStore(tmp_path / "runs.sqlite")
    release = asyncio.Event()
    app = create_app(store=store, model=ScriptedModel(release=release))
    store.add_message("th", "user", "你好")
    store.create_run("th", "run_1")
    task = asyncio.create_task(execute_run(store, app.state.model, app.state.tools, "run_1"))
    for _ in range(50):
        if store.get_run("run_1").status == "running":
            break
        await asyncio.sleep(0.01)
    response = await _open_get(app, "run_1")
    body = b""
    while b"run.started" not in body:
        body += await asyncio.wait_for(response._chunks.get(), 2)
    await response.aclose()
    assert not store.get_run("run_1").cancel_event.is_set()
    release.set()
    await task
    assert store.get_run("run_1").status == "completed"
    assert not store.get_run("run_1").cancel_event.is_set()
    store.close()
