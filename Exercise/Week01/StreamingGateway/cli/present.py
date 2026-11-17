from app.events.view import View


def format_view(view: View) -> str:
    lines = [f"[{tool.get('name', '')}] {tool.get('output') or ''}".rstrip() for tool in view.tools.values()]
    lines.extend(view.messages.values())
    if view.error:
        lines.append(view.error)
    return "\n".join(lines)
