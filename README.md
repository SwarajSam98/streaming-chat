# Streaming LLM Chat API (FastAPI + Ollama + uv)

A working local POC: JSON chat, Server-Sent Events token streaming, configurable model/temperature/system prompt, SQLite session history, Ollama token usage, estimated USD API cost, retries/timeouts, and a browser UI.

No external API key is needed. **Ollama runs locally**; by default estimated API cost is `$0`, excluding electricity/hardware. Usage counts are supplied by Ollama, not estimated from character lengths.

## Requirements

- Python 3.11+ (or let uv install one)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Ollama](https://ollama.com/download)

## 1. Install uv (only if needed)

**macOS / Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
```

**Windows PowerShell**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv --version
```

If uv is not recognized, restart your terminal to refresh PATH.

## 2. Start Ollama and download the model

Install Ollama using its official download page. Start the Ollama app (on most desktop installs it runs in the background), or run `ollama serve` if there is no running Ollama server.

```bash
ollama pull qwen2.5:0.5b
ollama list
```

Check `http://127.0.0.1:11434/api/tags` or `curl http://127.0.0.1:11434/api/tags` to verify that the daemon is available. If `ollama serve` says the address is already in use, your Ollama app is likely running already: do not start a second daemon.

For improved quality on machines with more RAM, run `ollama pull qwen2.5:1.5b` and change `DEFAULT_MODEL` in `.env` or pass `"model": "qwen2.5:1.5b"` per request.

## 3. Set up this project with uv

Unzip this project and `cd` into `streaming-llm-chat`:

```bash
uv python install 3.12
uv sync --python 3.12
```

Then create your local environment file:

**macOS / Linux**

```bash
cp .env.example .env
```

**Windows PowerShell**

```powershell
Copy-Item .env.example .env
```

Edit `.env` if you want different defaults. `.env` is gitignored. You can install extra dependencies with `uv add <package>`, and dev dependencies with `uv add --dev <package>`.

## 4. Run the FastAPI app

```bash
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- Browser chat: http://127.0.0.1:8000
- Interactive API docs: http://127.0.0.1:8000/docs
- App health: http://127.0.0.1:8000/health

Run **one Uvicorn worker** for this POC: per-session request locks are process-local.

## 5. Try the APIs

### POST /chat — complete JSON response

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-123","message":"Explain FastAPI in one sentence","model":"qwen2.5:0.5b","temperature":0.3}'
```

Example shape (actual text/token counts vary):

```json
{
  "session_id": "demo-123",
  "model": "qwen2.5:0.5b",
  "response": "FastAPI is a Python framework for building APIs.",
  "usage": {
    "prompt_tokens": 33,
    "completion_tokens": 14,
    "total_tokens": 47,
    "estimated_cost_usd": 0.0
  }
}
```

### POST /chat/stream — SSE response (use `curl -N`)

```bash
curl -N -X POST http://127.0.0.1:8000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-123","message":"Now explain SSE"}'
```

Events, in order:

```text
event: start
data: {"session_id":"demo-123","model":"qwen2.5:0.5b"}

event: token
data: {"text":"Hello"}

event: token
data: {"text":" there"}

event: done
data: {"session_id":"demo-123","model":"qwen2.5:0.5b","usage":{"prompt_tokens":42,"completion_tokens":8,"total_tokens":50,"estimated_cost_usd":0.0}}
```

The server also emits `event: error` with a structured error object on provider failure. Because SSE headers were already sent, the HTTP status can still be **200** when a later SSE error occurs. Native browser `EventSource` is GET-only; the included UI uses `fetch()` to stream a **POST** SSE response.

### Inspect a session

```bash
curl http://127.0.0.1:8000/sessions/demo-123/messages
```

Sessions are stored in `./data/chat.db`, and remain after restarting FastAPI. If no `session_id` is sent, the server generates one and returns it. Reuse that ID for subsequent requests.

## 6. Run tests (no Ollama needed)

```bash
uv run pytest -q
```

The tests mock Ollama, covering regular responses, SSE events, session context, usage, provider errors, retries, request validation, and persistence after failures.

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama REST base URL |
| `DEFAULT_MODEL` | `qwen2.5:0.5b` | Default model |
| `DEFAULT_TEMPERATURE` | `0.7` | Sampling temperature |
| `DEFAULT_SYSTEM_PROMPT` | Helpful assistant prompt | Default system instruction |
| `DATABASE_PATH` | `./data/chat.db` | SQLite database |
| `MAX_HISTORY_MESSAGES` | `20` | Recent messages sent to model, not total stored |
| `CONNECT_TIMEOUT_SECONDS` | `5` | Provider connection timeout |
| `READ_TIMEOUT_SECONDS` | `60` | Timeout while waiting for next provider data |
| `TOTAL_TIMEOUT_SECONDS` | `150` | Max total duration per request |
| `MAX_RETRIES` | `2` | Retried provider failures **before** first token |
| `RETRY_BACKOFF_SECONDS` | `0.5` | Exponential backoff base |
| `INPUT_COST_PER_MILLION_USD` | `0` | Optional cost estimation |
| `OUTPUT_COST_PER_MILLION_USD` | `0` | Optional cost estimation |

### Reliability notes

- Retries are limited to HTTP 408/429/5xx, provider connection issues and provider timeouts, **and only before any tokens were emitted**. This avoids duplicate tokens when a provider drops mid-stream.
- Failed, timed-out, or cancelled streaming requests do not add partial conversations to SQLite. `done` is emitted after messages + usage were saved.
- The system prompt is applied anew per request; it is not saved as a chat message. The most recent `MAX_HISTORY_MESSAGES` roles/content are sent to the model as context.
- Usage values are Ollama's `prompt_eval_count` and `eval_count` in the stream's final chunk. Cost is estimated as `(input_tokens × input_rate + output_tokens × output_rate) / 1,000,000`.
- The `/health` route confirms the FastAPI process is up; it does **not** guarantee that Ollama is reachable.

## POC caveats / production improvements

This demo has no authentication or rate limiting. Do not expose it to the public Internet. SQLite and process-local locks are reasonable for a single-worker POC, but for production use PostgreSQL, a shared locking strategy, authentication, request limits, observability, and session retention policies. Add a separate integration test against a running Ollama server before deployment.
