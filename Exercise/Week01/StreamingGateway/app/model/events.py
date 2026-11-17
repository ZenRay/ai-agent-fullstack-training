from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter


class TextDelta(BaseModel):
    type: Literal["text.delta"]
    delta: str


class ToolCall(BaseModel):
    type: Literal["tool_call"]
    call_id: str
    name: str
    arguments: str


class ModelFinished(BaseModel):
    type: Literal["model.finished"]
    finish_reason: str


class ModelUsageEvent(BaseModel):
    type: Literal["model.usage"]
    input_tokens: int
    output_tokens: int


ModelStreamEvent = Annotated[
    TextDelta | ToolCall | ModelFinished | ModelUsageEvent,
    Field(discriminator="type"),
]
MODEL_EVENT_ADAPTER = TypeAdapter(ModelStreamEvent)
