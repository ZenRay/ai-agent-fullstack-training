import asyncio
from collections.abc import AsyncIterator
from typing import Any, Protocol

from app.model.events import ModelStreamEvent


class StreamingModel(Protocol):
    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        cancel: asyncio.Event,
        idempotency_key: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ModelStreamEvent]: ...
