"""SQLite persistence. Each operation runs in a worker thread to avoid blocking the event loop."""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

from app.schemas import HistoryMessage, Usage


class ChatStorage:
    def __init__(self, path: str):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                  content TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session
                  ON messages(session_id, id);
                CREATE TABLE IF NOT EXISTS turns (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  model TEXT NOT NULL,
                  prompt_tokens INTEGER NOT NULL,
                  completion_tokens INTEGER NOT NULL,
                  estimated_cost_usd REAL NOT NULL,
                  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                """
            )

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _history(self, session_id: str, limit: int | None) -> list[HistoryMessage]:
        with closing(self._connect()) as conn:
            if limit is None:
                rows = conn.execute(
                    "SELECT role, content, created_at FROM messages "
                    "WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
            elif limit == 0:
                return []
            else:
                rows = conn.execute(
                    "SELECT role, content, created_at FROM "
                    "(SELECT id, role, content, created_at FROM messages "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                    (session_id, limit),
                ).fetchall()
        return [HistoryMessage(**dict(row)) for row in rows]

    async def history(self, session_id: str, limit: int | None = None) -> list[HistoryMessage]:
        return await asyncio.to_thread(self._history, session_id, limit)

    def _save_turn(
        self, session_id: str, user_message: str, response: str, model: str, usage: Usage
    ) -> None:
        # Both messages and usage are committed together, and only after a successful completion.
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
                    (session_id, user_message),
                )
                conn.execute(
                    "INSERT INTO messages(session_id, role, content) VALUES (?, 'assistant', ?)",
                    (session_id, response),
                )
                conn.execute(
                    "INSERT INTO turns(session_id, model, prompt_tokens, completion_tokens, "
                    "estimated_cost_usd) VALUES (?, ?, ?, ?, ?)",
                    (
                        session_id,
                        model,
                        usage.prompt_tokens,
                        usage.completion_tokens,
                        usage.estimated_cost_usd,
                    ),
                )

    async def save_turn(
        self, session_id: str, user_message: str, response: str, model: str, usage: Usage
    ) -> None:
        await asyncio.to_thread(self._save_turn, session_id, user_message, response, model, usage)
