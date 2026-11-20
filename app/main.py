"""FastAPI endpoints for full chat, streaming SSE chat, and session history."""

import asyncio
import json
import logging
import sqlite3
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Path as ApiPath
from fastapi.responses import FileResponse, StreamingResponse

from app.config import Settings
from app.ollama import OllamaProvider, ProviderError
from app.schemas import ChatRequest, ChatResponse, SessionHistory, Usage
from app.storage import ChatStorage

logger = logging.getLogger(__name__)
HTML_FILE = Path(__file__).parent / "static" / "index.html"


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def make_usage(prompt: int, completion: int, settings: Settings) -> Usage:
    cost = (
        prompt * settings.input_cost_per_million_usd
        + completion * settings.output_cost_per_million_usd
    ) / 1_000_000
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        estimated_cost_usd=round(cost, 8),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage = ChatStorage(config.database_path)
        await storage.initialize()
        timeout = httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=10,
            pool=5,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            app.state.storage = storage
            app.state.provider = OllamaProvider(client, config)
            # Per-session locks prevent competing turns inside this *single* server process.
            app.state.session_locks = defaultdict(asyncio.Lock)
            yield

    app = FastAPI(
        title="Streaming LLM Chat API",
        version="0.1.0",
        description="Chat with Ollama, stream tokens via SSE, persist completed turns in SQLite.",
        lifespan=lifespan,
    )

    def request_details(req: ChatRequest):
        session_id = req.session_id or str(uuid4())
        model = req.model or config.default_model
        temperature = (
            req.temperature if req.temperature is not None else config.default_temperature
        )
        system_prompt = (
            req.system_prompt
            if req.system_prompt is not None
            else config.default_system_prompt
        )
        return session_id, model, temperature, system_prompt

    async def messages_for(req: ChatRequest, session_id: str, system_prompt: str):
        existing = await app.state.storage.history(session_id, config.max_history_messages)
        return (
            [{"role": "system", "content": system_prompt}]
            + [{"role": msg.role, "content": msg.content} for msg in existing]
            + [{"role": "user", "content": req.message}]
        )

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(HTML_FILE)

    @app.get("/health")
    async def health():
        return {"status": "ok", "provider": "ollama", "default_model": config.default_model}

    @app.get("/config", include_in_schema=False)
    async def public_config():
        return {
            "model": config.default_model,
            "temperature": config.default_temperature,
            "system_prompt": config.default_system_prompt,
        }

    @app.get("/sessions/{session_id}/messages", response_model=SessionHistory)
    async def get_session(
        session_id: str = ApiPath(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    ):
        return SessionHistory(
            session_id=session_id,
            messages=await app.state.storage.history(session_id),
        )

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest):
        session_id, model, temperature, system_prompt = request_details(req)
        try:
            async with asyncio.timeout(config.total_timeout_seconds):
                async with app.state.session_locks[session_id]:
                    messages = await messages_for(req, session_id, system_prompt)
                    tokens = []
                    usage = None
                    async for chunk in app.state.provider.stream_chat(messages, model, temperature):
                        if chunk.token:
                            tokens.append(chunk.token)
                        if chunk.done:
                            usage = make_usage(chunk.prompt_tokens, chunk.completion_tokens, config)
                    if usage is None:
                        raise ProviderError("incomplete_response", "No final usage event")
                    response = "".join(tokens)
                    await app.state.storage.save_turn(session_id, req.message, response, model, usage)
                    return ChatResponse(
                        session_id=session_id, model=model, response=response, usage=usage
                    )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail={"code": "request_timeout", "message": "Request deadline exceeded"},
            ) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.as_dict()) from exc
        except (OSError, sqlite3.Error) as exc:
            logger.exception("Storage operation failed")
            raise HTTPException(
                status_code=500,
                detail={"code": "storage_error", "message": "Could not access chat storage"},
            ) from exc

    @app.post("/chat/stream", response_class=StreamingResponse)
    async def chat_stream(req: ChatRequest):
        session_id, model, temperature, system_prompt = request_details(req)

        async def events():
            yield sse("start", {"session_id": session_id, "model": model})
            try:
                async with asyncio.timeout(config.total_timeout_seconds):
                    async with app.state.session_locks[session_id]:
                        messages = await messages_for(req, session_id, system_prompt)
                        tokens = []
                        usage = None
                        async for chunk in app.state.provider.stream_chat(
                            messages, model, temperature
                        ):
                            if chunk.token:
                                tokens.append(chunk.token)
                                yield sse("token", {"text": chunk.token})
                            if chunk.done:
                                usage = make_usage(
                                    chunk.prompt_tokens, chunk.completion_tokens, config
                                )
                        if usage is None:
                            raise ProviderError("incomplete_response", "No final usage event")
                        await app.state.storage.save_turn(
                            session_id, req.message, "".join(tokens), model, usage
                        )
                        yield sse(
                            "done",
                            {"session_id": session_id, "model": model, "usage": usage.model_dump()},
                        )
            except TimeoutError:
                yield sse("error", {"code": "request_timeout", "message": "Request deadline exceeded"})
            except ProviderError as exc:
                yield sse("error", exc.as_dict())
            except (OSError, sqlite3.Error):
                logger.exception("Storage operation failed")
                yield sse("error", {"code": "storage_error", "message": "Could not access chat storage"})

        # A POST endpoint cannot use native EventSource; use fetch() and read the SSE body.
        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()
