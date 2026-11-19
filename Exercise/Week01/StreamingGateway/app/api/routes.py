import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Coroutine

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.events.types import TERMINAL_TYPES
from app.loop.run import execute_run
from app.sse.codec import SSE_HEADERS, encode, encode_heartbeat, encode_retry
from app.sse.stream import HEARTBEAT_INTERVAL
from app.store.memory import MemoryStore


class RunRouter(APIRouter):
    def __init__(self, store: MemoryStore) -> None:
        super().__init__()
        self.store = store


def create_router(store: MemoryStore) -> RunRouter:
    """创建、取消和状态查询。事件补发不在这里。"""
    router = RunRouter(store)

    @router.post("/v1/threads/{thread_id}/runs")
    async def create_run(thread_id: str, request: Request) -> StreamingResponse:
        message = await _read_message(request)
        fingerprint = _fingerprint(thread_id, message)
        key = request.headers.get("Idempotency-Key", "").strip()
        if key:
            found = store.find_idempotency(key)
            if found is not None:
                saved, run_id = found
                if saved != fingerprint:
                    raise HTTPException(status_code=409, detail={"message": "幂等键已被不同的请求使用"})
                return _stream(request, run_id)
        active = store.active_run(thread_id)
        if active is not None:
            raise HTTPException(status_code=409, detail={"run_id": active.run_id})
        model = getattr(request.app.state, "model", None)
        if model is None:
            raise HTTPException(status_code=500, detail="未配置模型")
        store.add_message(thread_id, "user", message)
        run = store.create_run(thread_id, _new_run_id())
        if key:
            store.remember_idempotency(key, fingerprint, run.run_id)
        _track(request, execute_run(store, model, request.app.state.tools, run.run_id))
        return _stream(request, run.run_id)

    @router.post("/v1/runs/{run_id}/cancel", response_model=None)
    async def cancel_run(run_id: str) -> JSONResponse:
        try:
            run = store.get_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="未知运行") from None
        if run.status in {"completed", "failed"}:
            raise HTTPException(status_code=409, detail={"run_id": run_id, "status": run.status})
        if run.status == "cancelled":
            return JSONResponse({"run_id": run_id, "status": "cancelled"})
        store.request_cancel(run_id)
        return JSONResponse({"run_id": run_id, "status": "cancelling"}, status_code=202)

    @router.get("/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, str | int]:
        try:
            run = store.get_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="未知运行") from None
        return {
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "status": run.status,
            "seq": run.seq,
        }

    return router


def _stream(request: Request, run_id: str) -> StreamingResponse:
    interval = getattr(request.app.state, "heartbeat_interval", HEARTBEAT_INTERVAL)
    headers = {**SSE_HEADERS, "X-Run-Id": run_id}
    return StreamingResponse(
        _watch(request, run_id, interval),
        media_type="text/event-stream",
        headers=headers,
    )


async def _watch(request: Request, run_id: str, heartbeat_interval: float) -> AsyncIterator[str]:
    store: MemoryStore = request.app.state.store
    yield encode_retry()
    seq = 0
    while True:
        if await request.is_disconnected():
            return
        events = store.since(run_id, seq)
        if not events:
            try:
                await asyncio.wait_for(store.wait_for_change(run_id, seq), heartbeat_interval)
            except TimeoutError:
                yield encode_heartbeat()
            continue
        for event in events:
            seq = event.seq
            yield encode(event)
            if event.type in TERMINAL_TYPES:
                return


def _track(request: Request, work: Coroutine[object, object, None]) -> None:
    task = asyncio.create_task(work)
    tasks: set[asyncio.Task[None]] = request.app.state.tasks
    tasks.add(task)

    def _done(finished: asyncio.Task[None]) -> None:
        tasks.discard(finished)
        if not finished.cancelled():
            finished.exception()

    task.add_done_callback(_done)


async def _read_message(request: Request) -> str:
    try:
        payload = json.loads(await request.body())
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="正文不是合法 JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
        raise HTTPException(status_code=400, detail="缺少用户消息")
    message = payload["message"].strip()
    if not message:
        raise HTTPException(status_code=400, detail="缺少用户消息")
    return message


def _fingerprint(thread_id: str, message: str) -> str:
    return f"{thread_id}\n{message}"


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"
