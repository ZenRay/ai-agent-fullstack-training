from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    input_tokens: int
    output_tokens: int


class RunEventBase(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    seq: int
    thread_id: str
    run_id: str


class RunStarted(RunEventBase):
    type: Literal["run.started"]


class MessageStarted(RunEventBase):
    type: Literal["message.started"]
    message_id: str
    role: Literal["assistant"]


class MessageDelta(RunEventBase):
    type: Literal["message.delta"]
    message_id: str
    delta: str


class MessageCompleted(RunEventBase):
    type: Literal["message.completed"]
    message_id: str


class ToolStarted(RunEventBase):
    type: Literal["tool.started"]
    call_id: str
    name: str


class ToolArgumentsDone(RunEventBase):
    type: Literal["tool.arguments.done"]
    call_id: str
    arguments: str


class ToolResult(RunEventBase):
    type: Literal["tool.result"]
    call_id: str
    ok: bool
    output: str


class RunCompleted(RunEventBase):
    type: Literal["run.completed"]
    usage: Usage


class RunFailed(RunEventBase):
    type: Literal["run.failed"]
    code: str
    message: str
    retryable: bool
    usage: Usage | None = None


class RunCancelled(RunEventBase):
    type: Literal["run.cancelled"]
    reason: str


RunEvent = Annotated[
    RunStarted
    | MessageStarted
    | MessageDelta
    | MessageCompleted
    | ToolStarted
    | ToolArgumentsDone
    | ToolResult
    | RunCompleted
    | RunFailed
    | RunCancelled,
    Field(discriminator="type"),
]
EVENT_ADAPTER = TypeAdapter(RunEvent)

EventType = Literal[
    "run.started",
    "message.started",
    "message.delta",
    "message.completed",
    "tool.started",
    "tool.arguments.done",
    "tool.result",
    "run.completed",
    "run.failed",
    "run.cancelled",
]
OPENING_TYPES = frozenset({"run.started", "run.failed"})
TERMINAL_TYPES = frozenset({"run.completed", "run.failed", "run.cancelled"})
