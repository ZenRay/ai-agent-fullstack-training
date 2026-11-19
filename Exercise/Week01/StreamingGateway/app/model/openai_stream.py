import json
from dataclasses import dataclass, field

from app.model.events import (
    ModelFailed,
    ModelFinished,
    ModelStreamEvent,
    ModelUsageEvent,
    TextDelta,
    ToolCall,
)


@dataclass
class _ToolBuffer:
    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class ChatStreamTranslator:
    """把 Chat Completions 的 SSE 译成内部事件。半截工具参数留在缓冲里。"""

    _text: str = ""
    _tools: dict[int, _ToolBuffer] = field(default_factory=dict)
    _emitted_content: bool = False
    _failed: bool = False
    _closed: bool = False

    @property
    def emitted_content(self) -> bool:
        return self._emitted_content

    @property
    def pending_tools(self) -> bool:
        return bool(self._tools)

    def feed(self, chunk: str) -> list[ModelStreamEvent]:
        self._text += chunk.replace("\r\n", "\n")
        events: list[ModelStreamEvent] = []
        while "\n\n" in self._text:
            raw, self._text = self._text.split("\n\n", 1)
            events.extend(self._consume_frame(raw))
        return events

    def finish(self) -> list[ModelStreamEvent]:
        if self._closed:
            return []
        self._closed = True
        if self._failed or not self._tools:
            self._tools.clear()
            return []
        self._tools.clear()
        return [
            ModelFailed(
                code="incomplete_tool_call",
                message="工具参数未收齐",
                retryable=False,
                phase="after_content",
            )
        ]

    def _consume_frame(self, raw: str) -> list[ModelStreamEvent]:
        payload = _data_payload(raw)
        if payload is None:
            return []
        if payload == "[DONE]":
            return self.finish()
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            phase = "after_content" if self._emitted_content or self._tools else "before_content"
            self._failed = True
            self._tools.clear()
            return [
                ModelFailed(
                    code="upstream_error",
                    message="上游数据块无法解析",
                    retryable=phase == "before_content",
                    phase=phase,
                )
            ]
        if not isinstance(body, dict):
            return []
        return self._consume_payload(body)

    def _consume_payload(self, body: dict) -> list[ModelStreamEvent]:
        events: list[ModelStreamEvent] = []
        choices = body.get("choices") or []
        choice = choices[0] if choices else {}
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            self._emitted_content = True
            events.append(TextDelta(type="text.delta", delta=content))
        for call in delta.get("tool_calls") or []:
            self._absorb_tool_call(call)
        finish_reason = choice.get("finish_reason")
        if finish_reason == "content_filter":
            phase = "after_content" if self._emitted_content or self._tools else "before_content"
            self._tools.clear()
            self._failed = True
            events.append(
                ModelFailed(
                    code="content_filter",
                    message="内容被过滤",
                    retryable=False,
                    phase=phase,
                )
            )
        elif finish_reason == "tool_calls":
            events.extend(self._flush_tools())
            events.append(ModelFinished(type="model.finished", finish_reason="tool_calls"))
        elif finish_reason:
            if self._tools:
                self._tools.clear()
                self._failed = True
                events.append(
                    ModelFailed(
                        code="incomplete_tool_call",
                        message="工具参数未收齐",
                        retryable=False,
                        phase="after_content",
                    )
                )
            else:
                events.append(ModelFinished(type="model.finished", finish_reason=str(finish_reason)))
        usage = body.get("usage")
        if isinstance(usage, dict):
            events.append(
                ModelUsageEvent(
                    type="model.usage",
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                )
            )
        return events

    def _absorb_tool_call(self, call: dict) -> None:
        index = int(call.get("index") or 0)
        buffer = self._tools.setdefault(index, _ToolBuffer())
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            buffer.call_id = call_id
        function = call.get("function") or {}
        name = function.get("name")
        if isinstance(name, str) and name:
            buffer.name = name
        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            buffer.arguments += arguments

    def _flush_tools(self) -> list[ToolCall]:
        calls = [
            ToolCall(
                type="tool_call",
                call_id=self._tools[index].call_id,
                name=self._tools[index].name,
                arguments=self._tools[index].arguments,
            )
            for index in sorted(self._tools)
        ]
        self._tools.clear()
        if calls:
            self._emitted_content = True
        return calls


def _data_payload(raw: str) -> str | None:
    data_lines: list[str] = []
    for line in raw.split("\n"):
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            value = line.removeprefix("data:")
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if not data_lines:
        return None
    return "\n".join(data_lines).strip()
