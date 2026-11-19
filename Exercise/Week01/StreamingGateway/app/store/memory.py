import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.events.types import EVENT_ADAPTER, OPENING_TYPES, TERMINAL_TYPES, RunEvent

RUN_STATUSES = frozenset(
    {"queued", "running", "waiting_for_approval", "completed", "failed", "cancelled"}
)
STATUS_OF = {
    "run.started": "running",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


@dataclass
class ThreadState:
    thread_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunState:
    thread_id: str
    run_id: str
    status: str = "queued"
    seq: int = 0
    version: int = 0
    events: list[RunEvent] = field(default_factory=list)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


class MemoryStore:
    def __init__(self) -> None:
        self._threads: dict[str, ThreadState] = {}
        self._runs: dict[str, RunState] = {}
        self._changed = asyncio.Condition()

    def ensure_thread(self, thread_id: str) -> ThreadState:
        thread = self._threads.get(thread_id)
        if thread is None:
            thread = ThreadState(thread_id=thread_id)
            self._threads[thread_id] = thread
        return thread

    def add_message(self, thread_id: str, role: str, content: str, **extra: Any) -> None:
        thread = self.ensure_thread(thread_id)
        thread.messages.append({"role": role, "content": content, **extra})

    def messages(self, thread_id: str) -> list[dict[str, Any]]:
        thread = self._threads.get(thread_id)
        if thread is None:
            return []
        return [dict(message) for message in thread.messages]

    def create_run(self, thread_id: str, run_id: str) -> RunState:
        self.ensure_thread(thread_id)
        if run_id in self._runs:
            raise ValueError(f"运行已存在: {run_id}")
        state = RunState(thread_id=thread_id, run_id=run_id)
        self._runs[run_id] = state
        return state

    def get_run(self, run_id: str) -> RunState:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise KeyError(f"未知运行: {run_id}") from exc

    async def append(self, run_id: str, body: dict[str, Any]) -> RunEvent:
        async with self._changed:
            state = self.get_run(run_id)
            if state.events and state.events[-1].type in TERMINAL_TYPES:
                raise RuntimeError(f"运行已结束: {run_id}")
            if not state.events and body.get("type") not in OPENING_TYPES:
                raise ValueError(f"流必须以 run.started 或 run.failed 开始: {run_id}")
            state.seq += 1
            state.version += 1
            event = EVENT_ADAPTER.validate_python(
                {
                    **body,
                    "seq": state.seq,
                    "thread_id": state.thread_id,
                    "run_id": state.run_id,
                }
            )
            next_status = STATUS_OF.get(event.type, state.status)
            if next_status not in RUN_STATUSES:
                raise RuntimeError(f"未知状态: {next_status}")
            state.status = next_status
            state.events.append(event)
            self._changed.notify_all()
            return event

    def since(self, run_id: str, seq: int) -> list[RunEvent]:
        if seq < 0:
            raise ValueError(f"游标不能为负: {seq}")
        state = self.get_run(run_id)
        return [event for event in state.events if event.seq > seq]

    def request_cancel(self, run_id: str) -> RunState:
        state = self.get_run(run_id)
        if state.status in TERMINAL_STATUSES:
            return state
        state.cancel_event.set()
        return state
