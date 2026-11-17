import asyncio
from collections.abc import AsyncIterator

from app.model.events import ModelStreamEvent


class GatewayModel:
    """指向已有模型网关的适配器。流式请求在接入步骤实现，供应商密钥不放在这里。"""

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        cancel: asyncio.Event,
        idempotency_key: str,
    ) -> AsyncIterator[ModelStreamEvent]:
        if False:
            yield  # pragma: no cover
        raise NotImplementedError("模型网关的流式调用尚未接入")
