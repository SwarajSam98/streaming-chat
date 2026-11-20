"""Streaming Ollama client with retry-before-first-token semantics."""

import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from app.config import Settings


@dataclass(frozen=True)
class ProviderChunk:
    token: str = ""
    done: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ProviderError(Exception):
    def __init__(
        self, code: str, message: str, status_code: int = 502, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class OllamaProvider:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def stream_chat(
        self, messages: list[dict[str, str]], model: str, temperature: float
    ) -> AsyncIterator[ProviderChunk]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        endpoint = self.settings.ollama_base_url.rstrip("/") + "/api/chat"
        for attempt in range(self.settings.max_retries + 1):
            emitted = False
            try:
                async with self.client.stream("POST", endpoint, json=payload) as response:
                    if response.status_code >= 400:
                        raw = await response.aread()
                        try:
                            detail = json.loads(raw).get("error", "Ollama request failed")
                        except (ValueError, AttributeError):
                            detail = raw.decode("utf-8", errors="replace")[:300]
                        retryable = response.status_code in {408, 429} or response.status_code >= 500
                        code = "model_not_found" if response.status_code == 404 else "provider_http_error"
                        raise ProviderError(
                            code=code,
                            message=str(detail),
                            status_code=(503 if retryable else 502),
                            retryable=retryable,
                        )

                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError as exc:
                            raise ProviderError("bad_provider_response", "Invalid Ollama JSON") from exc
                        if not isinstance(obj, dict):
                            raise ProviderError("bad_provider_response", "Invalid Ollama event")
                        if obj.get("error"):
                            raise ProviderError("provider_error", str(obj["error"]))
                        message = obj.get("message") or {}
                        token = message.get("content", "") if isinstance(message, dict) else ""
                        if token:
                            emitted = True
                            yield ProviderChunk(token=str(token))
                        if obj.get("done") is True:
                            yield ProviderChunk(
                                done=True,
                                prompt_tokens=int(obj.get("prompt_eval_count") or 0),
                                completion_tokens=int(obj.get("eval_count") or 0),
                            )
                            return
                    raise ProviderError(
                        "incomplete_response", "Ollama closed the stream before completion",
                        retryable=True,
                    )
            except httpx.TimeoutException as exc:
                error = ProviderError("provider_timeout", "Ollama timed out", 504, True)
            except httpx.TransportError as exc:
                error = ProviderError(
                    "provider_unavailable", "Cannot communicate with Ollama", 503, True
                )
            except ProviderError as exc:
                error = exc

            # Restarting after output has been shown would duplicate tokens; never do that.
            if emitted or not error.retryable or attempt >= self.settings.max_retries:
                raise error
            await asyncio.sleep(self.settings.retry_backoff_seconds * (2**attempt))
