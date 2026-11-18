import asyncio
from collections.abc import AsyncIterator

from app.events.types import TERMINAL_TYPES, RunEvent
from app.sse.codec import encode, encode_heartbeat, encode_retry

HEARTBEAT_INTERVAL = 15.0

_HEARTBEAT = object()


class Subscription:
    def __init__(self, maxsize: int = 16) -> None:
        self._queue: asyncio.Queue[RunEvent | None] = asyncio.Queue(maxsize=maxsize)
        self.disconnected = asyncio.Event()

    def offer(self, event: RunEvent) -> bool:
        if self.disconnected.is_set():
            return False
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.disconnected.set()
            return False
        return True

    def close(self) -> None:
        self.disconnected.set()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def frames(self, *, heartbeat_interval: float = HEARTBEAT_INTERVAL) -> AsyncIterator[str]:
        yield encode_retry()
        while not self.disconnected.is_set():
            event = await self._next_event(heartbeat_interval)
            if event is _HEARTBEAT:
                yield encode_heartbeat()
                continue
            if event is None:
                return
            yield encode(event)
            if event.type in TERMINAL_TYPES:
                return

    async def _next_event(self, heartbeat_interval: float) -> RunEvent | None | object:
        get_task = asyncio.create_task(self._queue.get())
        stop_task = asyncio.create_task(self.disconnected.wait())
        outcome: RunEvent | None | object = _HEARTBEAT
        try:
            done, _pending = await asyncio.wait(
                {get_task, stop_task},
                timeout=heartbeat_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done and get_task not in done:
                outcome = None
            elif get_task in done:
                outcome = get_task.result()
        finally:
            for task in (get_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get_task, stop_task, return_exceptions=True)
            if self.disconnected.is_set():
                outcome = None
            elif outcome is _HEARTBEAT and _took_event(get_task):
                outcome = get_task.result()
        return outcome


def _took_event(task: asyncio.Task[RunEvent | None]) -> bool:
    return task.done() and not task.cancelled() and task.exception() is None
