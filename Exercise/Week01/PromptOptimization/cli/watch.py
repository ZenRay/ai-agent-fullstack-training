import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

import httpx

from app.events.types import TERMINAL_TYPES
from app.events.view import View, reduce
from app.sse.codec import FrameBuffer, parse_frame
from cli.present import render_event


@dataclass
class WatchResult:
    view: View
    run_id: str | None = None
    busy_run_id: str | None = None
    error: str | None = None


async def watch_run(
    client: httpx.AsyncClient,
    thread_id: str,
    message: str,
    *,
    out: TextIO | None = None,
    on_run_id: Callable[[str], None] | None = None,
    on_event: Callable[[View], None] | None = None,
) -> WatchResult:
    """创建一次运行并阅读它的 SSE。400 和 409 不进入投影。"""
    view = View()
    async with client.stream(
        "POST",
        f"/v1/threads/{thread_id}/runs",
        json={"message": message},
        headers={"Accept": "text/event-stream"},
    ) as response:
        if response.status_code == 400:
            return WatchResult(view=view, error=await _detail_text(response))
        if response.status_code == 409:
            return WatchResult(view=view, busy_run_id=_busy_run_id(await response.aread()))
        response.raise_for_status()
        run_id = response.headers.get("x-run-id")
        if not run_id:
            return WatchResult(view=view, error="缺少运行编号")
        if on_run_id is not None:
            on_run_id(run_id)
        buffer = FrameBuffer()
        async for chunk in response.aiter_bytes():
            if _fold(view, buffer, chunk, run_id, out, on_event):
                return WatchResult(view=view, run_id=run_id)
        if not view.done:
            view.status = "incomplete"
        return WatchResult(view=view, run_id=run_id)


async def stop(client: httpx.AsyncClient, run_id: str) -> httpx.Response:
    """只请求取消。调用方再决定要不要中止本地阅读。"""
    return await client.post(f"/v1/runs/{run_id}/cancel")


async def interrupt(
    client: httpx.AsyncClient,
    run_id: str | None,
    reader: object,
) -> None:
    """Ctrl+C：已经知道运行编号时先取消，再结束本地阅读。"""
    if run_id:
        await stop(client, run_id)
    cancel = getattr(reader, "cancel", None)
    if callable(cancel):
        cancel()


def _fold(
    view: View,
    buffer: FrameBuffer,
    chunk: bytes,
    run_id: str,
    out: TextIO | None,
    on_event: Callable[[View], None] | None,
) -> bool:
    for frame in buffer.feed_bytes(chunk):
        event = parse_frame(frame)
        if event is None:
            continue
        if event.type == "run.started" and event.run_id != run_id:
            view.status = "failed"
            view.error = "运行编号不一致"
            view.done = True
            return True
        previous = view.last_seq
        reduce(view, event)
        if view.last_seq == previous:
            continue
        if out is not None:
            render_event(view, event, out)
        if on_event is not None:
            on_event(view)
        if event.type in TERMINAL_TYPES:
            return True
    return False


async def _detail_text(response: httpx.Response) -> str:
    detail = _decode_detail(await response.aread())
    if isinstance(detail, str):
        return detail
    return json.dumps(detail, ensure_ascii=False)


def _busy_run_id(raw: bytes) -> str | None:
    detail = _decode_detail(raw)
    if isinstance(detail, dict):
        run_id = detail.get("run_id")
        if isinstance(run_id, str):
            return run_id
    return None


def _decode_detail(raw: bytes) -> object:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode()
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    return payload
