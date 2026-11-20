from contextlib import asynccontextmanager

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.ollama import ProviderChunk, ProviderError


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def stream_chat(self, messages, model, temperature):
        self.calls.append((messages, model, temperature))
        yield ProviderChunk(token="Hello")
        yield ProviderChunk(token=" world")
        yield ProviderChunk(done=True, prompt_tokens=12, completion_tokens=2)


class FailingProvider:
    async def stream_chat(self, messages, model, temperature):
        yield ProviderChunk(token="partial")
        raise ProviderError("provider_unavailable", "Ollama disconnected", 503, True)


@asynccontextmanager
async def api_client(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "chat.db"),
        input_cost_per_million_usd=1.0,
        output_cost_per_million_usd=2.0,
        _env_file=None,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        fake = FakeProvider()
        app.state.provider = fake
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, app, fake


@pytest.mark.asyncio
async def test_full_chat_persists_and_uses_history(tmp_path):
    async with api_client(tmp_path) as (client, _, provider):
        a = await client.post("/chat", json={"message": "Hello", "session_id": "session-1"})
        assert a.status_code == 200, a.text
        assert a.json()["response"] == "Hello world"
        assert a.json()["usage"] == {
            "prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14,
            "estimated_cost_usd": 0.000016,
        }
        b = await client.post("/chat", json={"message": "Again", "session_id": "session-1"})
        assert b.status_code == 200
        assert [m["role"] for m in provider.calls[1][0]] == [
            "system", "user", "assistant", "user"
        ]
        history = (await client.get("/sessions/session-1/messages")).json()["messages"]
        assert len(history) == 4
        assert [m["content"] for m in history] == ["Hello", "Hello world", "Again", "Hello world"]


@pytest.mark.asyncio
async def test_sse_stream_and_save(tmp_path):
    async with api_client(tmp_path) as (client, _, _):
        r = await client.post("/chat/stream", json={"message": "Stream", "session_id": "test-sse"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "event: start" in r.text
        assert r.text.count("event: token") == 2
        assert '"text": "Hello"' in r.text
        assert "event: done" in r.text
        history = (await client.get("/sessions/test-sse/messages")).json()["messages"]
        assert len(history) == 2
        assert history[1]["content"] == "Hello world"


@pytest.mark.asyncio
async def test_sse_error_does_not_save_partial_turn(tmp_path):
    async with api_client(tmp_path) as (client, app, _):
        app.state.provider = FailingProvider()
        r = await client.post("/chat/stream", json={"message": "Oops", "session_id": "broken"})
        assert r.status_code == 200  # SSE errors arrive in-band after headers are sent.
        assert "event: error" in r.text
        assert "event: done" not in r.text
        history = (await client.get("/sessions/broken/messages")).json()["messages"]
        assert history == []


@pytest.mark.asyncio
async def test_nonstream_provider_error_and_validation(tmp_path):
    async with api_client(tmp_path) as (client, app, _):
        app.state.provider = FailingProvider()
        r = await client.post("/chat", json={"message": "Oops", "session_id": "broken"})
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "provider_unavailable"
        assert (await client.get("/sessions/broken/messages")).json()["messages"] == []
        bad = await client.post("/chat", json={"message": "hello", "temperature": 5})
        assert bad.status_code == 422


@pytest.mark.asyncio
async def test_auto_session_id_and_homepage(tmp_path):
    async with api_client(tmp_path) as (client, _, _):
        r = await client.post("/chat", json={"message": "Hi"})
        assert r.status_code == 200
        assert len(r.json()["session_id"]) == 36
        homepage = await client.get("/")
        assert homepage.status_code == 200
        assert "Streaming LLM Chat" in homepage.text

@pytest.mark.asyncio
async def test_sessions_survive_restart_and_are_isolated(tmp_path):
    db = tmp_path / "chat.db"
    settings = Settings(database_path=str(db), _env_file=None)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        app.state.provider = FakeProvider()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/chat", json={"session_id": "first", "message": "A"})).status_code == 200
            assert (await client.post("/chat", json={"session_id": "second", "message": "B"})).status_code == 200

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as client:
            first = (await client.get("/sessions/first/messages")).json()["messages"]
            second = (await client.get("/sessions/second/messages")).json()["messages"]
            assert first[0]["content"] == "A"
            assert second[0]["content"] == "B"
            assert len(first) == len(second) == 2
