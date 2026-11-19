from fastapi import FastAPI

from app.api.routes import create_router
from app.store.memory import MemoryStore
from app.tools.registry import ToolRegistry
from app.tools.weather import register_weather


def create_app(store: MemoryStore | None = None) -> FastAPI:
    app = FastAPI(title="Streaming Agent Gateway")
    app.state.store = store or MemoryStore()
    tools = ToolRegistry()
    register_weather(tools)
    app.state.tools = tools
    app.include_router(create_router(app.state.store))
    return app
