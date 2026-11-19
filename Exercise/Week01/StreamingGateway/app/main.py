from fastapi import FastAPI

from app.api.routes import create_router
from app.model.protocol import StreamingModel
from app.sse.stream import HEARTBEAT_INTERVAL
from app.store.memory import MemoryStore
from app.tools.registry import ToolRegistry
from app.tools.weather import register_weather


def create_app(
    store: MemoryStore | None = None,
    model: StreamingModel | None = None,
) -> FastAPI:
    app = FastAPI(title="Streaming Agent Gateway")
    app.state.store = store or MemoryStore()
    app.state.model = model
    app.state.tasks = set()
    app.state.heartbeat_interval = HEARTBEAT_INTERVAL
    tools = ToolRegistry()
    register_weather(tools)
    app.state.tools = tools
    app.include_router(create_router(app.state.store))
    return app
