from dataclasses import dataclass, field

from app.events.types import RunEvent, Usage


@dataclass
class View:
    status: str = "idle"
    messages: dict[str, str] = field(default_factory=dict)
    closed_messages: set[str] = field(default_factory=set)
    tools: dict[str, dict] = field(default_factory=dict)
    usage: Usage | None = None
    error: str | None = None
    done: bool = False
    last_seq: int = 0


def reduce(view: View, event: RunEvent) -> None:
    if event.seq <= view.last_seq:
        return
    view.last_seq = event.seq
    if event.type == "run.started":
        view.status = "running"
    elif event.type == "message.started":
        view.messages[event.message_id] = ""
    elif event.type == "message.delta":
        view.messages[event.message_id] += event.delta
    elif event.type == "message.completed":
        view.closed_messages.add(event.message_id)
    elif event.type == "tool.started":
        view.tools[event.call_id] = {"name": event.name, "output": ""}
    elif event.type == "tool.arguments.done":
        view.tools[event.call_id]["arguments"] = event.arguments
    elif event.type == "tool.result":
        view.tools[event.call_id]["ok"] = event.ok
        view.tools[event.call_id]["output"] = event.output
    elif event.type == "run.completed":
        view.status = "completed"
        view.usage = event.usage
        view.done = True
    elif event.type == "run.failed":
        view.status = "failed"
        view.error = event.message
        view.done = True
    elif event.type == "run.cancelled":
        view.status = "cancelled"
        view.error = event.reason
        view.done = True
