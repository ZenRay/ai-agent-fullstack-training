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


@dataclass(frozen=True)
class PromptCall:
    trace_id: str
    name: str
    version: int
    content_hash: str


@dataclass
class RunState:
    thread_id: str
    run_id: str
    status: str = "queued"
    seq: int = 0
    version: int = 0
    events: list[RunEvent] = field(default_factory=list)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    model_key: str | None = None
    continue_used: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    prompt_name: str | None = None
    prompt_version: int | None = None
    prompt_hash: str | None = None
    prompt_variables: dict[str, str] = field(default_factory=dict)
    prompt_calls: list[PromptCall] = field(default_factory=list)


class MemoryStore:
    def __init__(self) -> None:
        self._threads: dict[str, ThreadState] = {}
        self._runs: dict[str, RunState] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
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

    def active_run(self, thread_id: str) -> RunState | None:
        for state in self._runs.values():
            if state.thread_id == thread_id and state.status not in TERMINAL_STATUSES:
                return state
        return None

    def find_idempotency(self, key: str) -> tuple[str, str] | None:
        return self._idempotency.get(key)

    def remember_idempotency(self, key: str, fingerprint: str, run_id: str) -> None:
        current = self._idempotency.get(key)
        if current is not None and current != (fingerprint, run_id):
            raise ValueError(f"幂等键已被不同的请求使用: {key}")
        self._idempotency[key] = (fingerprint, run_id)

    async def wait_for_change(self, run_id: str, seq: int) -> None:
        async with self._changed:
            while not any(event.seq > seq for event in self.get_run(run_id).events):
                await self._changed.wait()

    def min_seq(self, run_id: str) -> int:
        self.get_run(run_id)
        return 0

    def note_model_key(self, run_id: str, key: str) -> None:
        self.get_run(run_id).model_key = key

    def bind_prompt(
        self,
        run_id: str,
        *,
        name: str,
        version: int,
        content_hash: str,
        variables: dict[str, str],
    ) -> None:
        state = self.get_run(run_id)
        if state.prompt_name is not None and (
            state.prompt_name != name
            or state.prompt_version != version
            or state.prompt_hash != content_hash
        ):
            raise ValueError(f"运行已绑定其他提示词版本: {run_id}")
        state.prompt_name = name
        state.prompt_version = version
        state.prompt_hash = content_hash
        state.prompt_variables = dict(variables)

    def note_prompt_call(
        self,
        run_id: str,
        trace_id: str,
        name: str,
        version: int,
        content_hash: str,
    ) -> None:
        state = self.get_run(run_id)
        for call in state.prompt_calls:
            if call.trace_id == trace_id:
                if (call.name, call.version, call.content_hash) != (name, version, content_hash):
                    raise ValueError(f"同一次模型调用不能改用另一版提示词: {trace_id}")
                return
        state.prompt_calls.append(
            PromptCall(trace_id=trace_id, name=name, version=version, content_hash=content_hash)
        )

    def prompt_calls(self, run_id: str) -> list[PromptCall]:
        return list(self.get_run(run_id).prompt_calls)

    def note_usage(self, run_id: str, input_tokens: int, output_tokens: int) -> None:
        state = self.get_run(run_id)
        state.input_tokens += input_tokens
        state.output_tokens += output_tokens

    def mark_continued(self, run_id: str) -> None:
        self.get_run(run_id).continue_used = True

    def continued(self, run_id: str) -> bool:
        return self.get_run(run_id).continue_used

    def unfinished(self) -> list[RunState]:
        return [state for state in self._runs.values() if state.status not in TERMINAL_STATUSES]

    def repair_status(self, run_id: str, status: str) -> None:
        if status not in RUN_STATUSES:
            raise RuntimeError(f"未知状态: {status}")
        self.get_run(run_id).status = status

    def request_cancel(self, run_id: str) -> RunState:
        state = self.get_run(run_id)
        if state.status in TERMINAL_STATUSES:
            return state
        state.cancel_event.set()
        return state
