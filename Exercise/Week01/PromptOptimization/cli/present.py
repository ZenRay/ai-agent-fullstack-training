from typing import TextIO

from app.events.types import RunEvent
from app.events.view import View
from app.prompts.untrusted import strip_hidden


def format_view(view: View) -> str:
    lines = [
        f"[{tool.get('name', '')}] {strip_hidden(str(tool.get('output') or ''))}".rstrip()
        for tool in view.tools.values()
    ]
    lines.extend(strip_hidden(text) for text in view.messages.values())
    if view.error:
        lines.append(view.error)
    return "\n".join(lines)


def render_event(view: View, event: RunEvent, out: TextIO) -> None:
    """按新到达的事件追加输出，不重画已经打过的内容。"""
    if event.type == "tool.result":
        tool = view.tools[event.call_id]
        line = f"[{tool.get('name', '')}] {strip_hidden(str(tool.get('output') or ''))}".rstrip()
        out.write(line + "\n")
    elif event.type == "message.delta":
        out.write(strip_hidden(event.delta))
    elif event.type == "message.completed":
        out.write("\n")
    elif event.type in {"run.failed", "run.cancelled"} and view.error:
        out.write(view.error + "\n")
    else:
        return
    out.flush()
