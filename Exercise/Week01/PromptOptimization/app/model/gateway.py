import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.model.events import (
    ModelFailed,
    ModelFinished,
    ModelStreamEvent,
    ModelUsageEvent,
    TextDelta,
    ToolCall,
)
from app.model.openai_stream import ChatStreamTranslator

_INVALID_STATUS = frozenset({400, 401, 403, 422})
_RETRYABLE_STATUS = frozenset({408, 500, 502, 503, 504})


class GatewayModel:
    """调用已有模型网关。这里的密钥是虚拟密钥，供应商密钥不进入本进程。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = client

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        cancel: asyncio.Event,
        idempotency_key: str,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient()
        structured = response_format is not None
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": not structured,
        }
        if structured:
            body["response_format"] = response_format
        else:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        if temperature is not None:
            body["temperature"] = temperature
        if structured:
            try:
                async for event in _collect_completion(
                    client,
                    f"{self.base_url}/v1/chat/completions",
                    body,
                    _headers(self.api_key, idempotency_key, stream=False),
                    cancel,
                ):
                    yield event
            finally:
                if owns_client:
                    await client.aclose()
            return
        translator = ChatStreamTranslator()
        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                json=body,
                headers=_headers(self.api_key, idempotency_key, stream=True),
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    yield _failure_from_status(
                        response.status_code,
                        response.headers.get("Retry-After"),
                    )
                    return
                async for text in _iter_text(response, cancel):
                    if text is None:
                        return
                    for event in translator.feed(text):
                        yield event
                        if cancel.is_set():
                            return
                if cancel.is_set():
                    return
                for event in translator.finish():
                    yield event
        except httpx.HTTPError:
            if cancel.is_set():
                return
            if translator.pending_tools:
                for event in translator.finish():
                    yield event
                return
            phase = "after_content" if translator.emitted_content else "before_content"
            yield ModelFailed(
                code="upstream_error",
                message="连接中断",
                retryable=True,
                phase=phase,
            )
        finally:
            if owns_client:
                await client.aclose()


def _headers(api_key: str, idempotency_key: str, *, stream: bool) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Idempotency-Key": idempotency_key,
        "Accept": "text/event-stream" if stream else "application/json",
    }


async def _collect_completion(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    cancel: asyncio.Event,
) -> AsyncIterator[ModelStreamEvent]:
    if cancel.is_set():
        return
    try:
        response = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError:
        if cancel.is_set():
            return
        yield ModelFailed(
            code="upstream_error",
            message="连接中断",
            retryable=True,
            phase="before_content",
        )
        return
    if cancel.is_set():
        return
    if response.status_code >= 400:
        yield _failure_from_status(response.status_code, response.headers.get("Retry-After"))
        return
    try:
        payload = response.json()
    except json.JSONDecodeError:
        yield ModelFailed(
            code="invalid_request",
            message="上游没有返回 JSON",
            retryable=False,
            phase="before_content",
        )
        return
    for event in _events_from_completion(payload):
        yield event


def _events_from_completion(payload: dict[str, Any]) -> list[ModelStreamEvent]:
    events: list[ModelStreamEvent] = []
    choices = payload.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content:
        events.append(TextDelta(type="text.delta", delta=content))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        events.append(
            ToolCall(
                type="tool_call",
                call_id=str(call.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=str(function.get("arguments") or ""),
            )
        )
    usage = payload.get("usage") or {}
    if usage:
        events.append(
            ModelUsageEvent(
                type="model.usage",
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            )
        )
    events.append(
        ModelFinished(
            type="model.finished",
            finish_reason=str(choice.get("finish_reason") or "stop"),
        )
    )
    return events


def _failure_from_status(status: int, retry_after: str | None) -> ModelFailed:
    if status == 429:
        return ModelFailed(
            code="rate_limit",
            message="请求过于频繁",
            retryable=True,
            phase="before_content",
            retry_after_ms=_retry_after_ms(retry_after),
        )
    if status == 402:
        return ModelFailed(
            code="quota_exceeded",
            message="额度不足",
            retryable=False,
            phase="before_content",
        )
    if status == 404:
        return ModelFailed(
            code="model_not_found",
            message="模型不存在",
            retryable=False,
            phase="before_content",
        )
    if status in _INVALID_STATUS:
        return ModelFailed(
            code="invalid_request",
            message="请求无效",
            retryable=False,
            phase="before_content",
        )
    if status in _RETRYABLE_STATUS:
        code = "timeout" if status == 408 else "upstream_error"
        return ModelFailed(
            code=code,
            message="上游暂时不可用",
            retryable=True,
            phase="before_content",
        )
    return ModelFailed(
        code="invalid_request",
        message="请求失败",
        retryable=False,
        phase="before_content",
    )


def _retry_after_ms(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        return float(text) * 1000
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delay = (parsed - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delay * 1000)


async def _iter_text(response: httpx.Response, cancel: asyncio.Event) -> AsyncIterator[str | None]:
    chunks = response.aiter_text().__aiter__()
    while True:
        if cancel.is_set():
            yield None
            return
        incoming = asyncio.create_task(anext(chunks))
        stopped = asyncio.create_task(cancel.wait())
        try:
            done, _pending = await asyncio.wait(
                {incoming, stopped},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (incoming, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(incoming, stopped, return_exceptions=True)
        if cancel.is_set() or stopped in done:
            yield None
            return
        try:
            yield incoming.result()
        except StopAsyncIteration:
            return
