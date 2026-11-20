from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import create_router
from app.loop.resume import resume_unfinished
from app.model.protocol import StreamingModel
from app.prompts.catalog import default_catalog
from app.sse.stream import HEARTBEAT_INTERVAL
from app.store.memory import MemoryStore
from app.store.sqlite import SqliteStore
from app.tools.registry import ToolRegistry
from app.tools.weather import register_weather


def create_app(
    store: MemoryStore | SqliteStore | None = None,
    model: StreamingModel | None = None,
) -> FastAPI:
    app = FastAPI(title="Streaming Agent Gateway", lifespan=_lifespan)
    app.state.store = store or MemoryStore()
    app.state.model = model
    app.state.tasks = set()
    app.state.heartbeat_interval = HEARTBEAT_INTERVAL
    tools = ToolRegistry()
    register_weather(tools)
    if isinstance(app.state.store, SqliteStore):
        tools.effects = app.state.store.effects
    app.state.tools = tools
    app.state.catalog = default_catalog()
    app.include_router(create_router(app.state.store))
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI):
    store = app.state.store
    if isinstance(store, SqliteStore) and app.state.model is not None:
        await resume_unfinished(store, app.state.model, app.state.tools)
    yield
