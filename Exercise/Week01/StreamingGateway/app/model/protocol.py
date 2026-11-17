import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

from app.model.events import ModelStreamEvent


class StreamingModel(Protocol):
    def stream(
        self,
        messages: list[dict[str, str]],
        *,
        cancel: asyncio.Event,
        idempotency_key: str,
    ) -> AsyncIterator[ModelStreamEvent]: ...
