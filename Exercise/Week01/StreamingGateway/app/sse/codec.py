from app.events.types import EVENT_ADAPTER, RunEvent


def encode(event: RunEvent) -> str:
    payload = event.model_dump_json()
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


def encode_heartbeat() -> str:
    return ": keep-alive\n\n"


def decode(frame: str) -> RunEvent | None:
    if frame.startswith(":"):
        return None
    data = next(
        line.removeprefix("data:").strip()
        for line in frame.splitlines()
        if line.startswith("data:")
    )
    return EVENT_ADAPTER.validate_json(data)


def _is_skippable_frame(raw: str) -> bool:
    stripped = raw.strip()
    return not stripped or stripped.startswith(":")


class FrameDecoder:
    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[RunEvent]:
        text = (self._buffer + chunk).replace("\r\n", "\n")
        pending_cr = text.endswith("\r")
        if pending_cr:
            text = text[:-1]
        events: list[RunEvent] = []
        while "\n\n" in text:
            raw, text = text.split("\n\n", 1)
            if _is_skippable_frame(raw):
                continue
            event = decode(raw)
            if event is not None:
                events.append(event)
        self._buffer = f"{text}\r" if pending_cr else text
        return events
