import json

import httpx
import pytest

from app.config import Settings
from app.ollama import OllamaProvider, ProviderError


@pytest.mark.asyncio
async def test_retries_503_before_first_token():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "busy"})
        lines = [
            {"message": {"content": "OK"}, "done": False},
            {"done": True, "prompt_eval_count": 10, "eval_count": 1},
        ]
        return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines) + "\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OllamaProvider(client, Settings(max_retries=2, retry_backoff_seconds=0, _env_file=None))
        chunks = [x async for x in provider.stream_chat([{"role": "user", "content": "Hi"}], "x", .5)]
    assert calls == 2
    assert chunks[0].token == "OK"
    assert chunks[1].done is True
    assert chunks[1].prompt_tokens == 10


@pytest.mark.asyncio
async def test_does_not_retry_after_token():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        lines = [{"message": {"content": "partial"}, "done": False}]
        return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines) + "\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OllamaProvider(client, Settings(max_retries=2, retry_backoff_seconds=0, _env_file=None))
        with pytest.raises(ProviderError, match="before completion"):
            _ = [x async for x in provider.stream_chat([], "x", .5)]
    assert calls == 1


@pytest.mark.asyncio
async def test_missing_model_is_not_retried():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"error": "model not found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OllamaProvider(client, Settings(max_retries=3, retry_backoff_seconds=0, _env_file=None))
        with pytest.raises(ProviderError) as raised:
            _ = [x async for x in provider.stream_chat([], "x", .5)]
    assert raised.value.code == "model_not_found"
    assert calls == 1
