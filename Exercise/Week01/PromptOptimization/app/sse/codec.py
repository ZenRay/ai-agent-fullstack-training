import codecs

from app.events.types import EVENT_ADAPTER, RunEvent

SSE_HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


def encode(event: RunEvent) -> str:
    payload = event.model_dump_json(exclude_none=True)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


def encode_heartbeat() -> str:
    return ": keep-alive\n\n"


def encode_retry(milliseconds: int = 3000) -> str:
    return f"retry: {milliseconds}\n\n"


def parse_frame(frame: str) -> RunEvent | None:
    data_lines: list[str] = []
    for raw in frame.split("\n"):
        if raw.startswith(":"):
            continue
        if raw.startswith("data:"):
            value = raw.removeprefix("data:")
            data_lines.append(value[1:] if value.startswith(" ") else value)
    payload = "\n".join(data_lines)
    if not payload:
        return None
    return EVENT_ADAPTER.validate_json(payload)


decode = parse_frame


class FrameBuffer:
    def __init__(self) -> None:
        self._text = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")

    def feed(self, chunk: str) -> list[str]:
        self._text += chunk
        frames: list[str] = []
        while "\n\n" in self._text:
            frame, self._text = self._text.split("\n\n", 1)
            frames.append(frame)
        return frames

    def feed_bytes(self, chunk: bytes) -> list[str]:
        return self.feed(self._decoder.decode(chunk))
