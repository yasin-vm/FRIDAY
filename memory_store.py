"""Local SQLite persistence for FRIDAY conversations and approved memories."""

from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


DATABASE_PATH = Path(__file__).resolve().parent / "memory" / "friday.sqlite3"
MEMORY_TOKEN_PATTERN = re.compile(r"[a-z0-9_]{2,}", re.IGNORECASE)
MEMORY_TERM_NORMALIZATION = {
    "prefers": "prefer",
    "preferred": "prefer",
    "preference": "prefer",
    "preferences": "prefer",
}
MEMORY_QUERY_EXPANSIONS = {
    "editor": ("code", "ide"),
    "ide": ("editor", "code"),
}


@dataclass(frozen=True)
class Conversation:
    id: str
    title: str
    updated_at: str


@dataclass(frozen=True)
class StoredMessage:
    role: str
    content: str
    created_at: str


@dataclass(frozen=True)
class Memory:
    id: int
    content: str
    created_at: str


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryStore:
    """Owns all local persistence; memory entries are saved only on explicit request."""

    def __init__(self, path: Path = DATABASE_PATH) -> None:
        self.path = Path(path).expanduser()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    conversation_id TEXT NOT NULL
                        REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation_idx
                    ON messages(conversation_id, id);
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create_conversation(self, title: str = "New chat") -> str:
        conversation_id = str(uuid.uuid4())
        now = _timestamp()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, title, now, now),
            )
        return conversation_id

    def save_message(self, conversation_id: str, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("Conversation messages must have a user or assistant role.")
        now = _timestamp()
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, now),
            )
            if role == "user":
                title = content.strip().replace("\r", " ").replace("\n", " ")[:60]
                connection.execute(
                    "UPDATE conversations SET "
                    "title = CASE WHEN title = 'New chat' THEN ? ELSE title END, "
                    "updated_at = ? WHERE id = ?",
                    (title or "New chat", now, conversation_id),
                )
            else:
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
            if cursor.rowcount != 1:
                raise ValueError(f"Conversation does not exist: {conversation_id}")

    def list_recent_conversations(self, limit: int = 20) -> list[Conversation]:
        if limit < 1:
            raise ValueError("Conversation limit must be positive.")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, title, updated_at FROM conversations "
                "ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Conversation(row["id"], row["title"], row["updated_at"]) for row in rows]

    def get_messages(self, conversation_id: str) -> list[StoredMessage]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        return [
            StoredMessage(row["role"], row["content"], row["created_at"])
            for row in rows
        ]

    def add_memory(self, content: str) -> int:
        value = content.strip()
        if not value:
            raise ValueError("Memory cannot be empty.")
        now = _timestamp()
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO memories (content, created_at) VALUES (?, ?)",
                (value, now),
            )
            return int(cursor.lastrowid)

    def list_memories(self) -> list[Memory]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, content, created_at FROM memories ORDER BY id DESC"
            ).fetchall()
        return [Memory(row["id"], row["content"], row["created_at"]) for row in rows]

    def search_memories(
        self,
        query: str,
        limit: int = 5,
        max_chars: int = 3_000,
    ) -> list[str]:
        if limit < 1 or max_chars < 1:
            return []
        terms = list(
            dict.fromkeys(
                MEMORY_TERM_NORMALIZATION.get(term, term)
                for term in MEMORY_TOKEN_PATTERN.findall(query.lower())
            )
        )
        terms.extend(
            related
            for term in tuple(terms)
            for related in MEMORY_QUERY_EXPANSIONS.get(term, ())
            if related not in terms
        )
        if not terms:
            return []
        conditions = " OR ".join("content LIKE ?" for _ in terms)
        parameters = [f"%{term}%" for term in terms]
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT content FROM memories WHERE " + conditions + " LIMIT 200",
                parameters,
            ).fetchall()
        ranked = sorted(
            (
                (
                    len(
                        set(terms)
                        & {
                            MEMORY_TERM_NORMALIZATION.get(token, token)
                            for token in MEMORY_TOKEN_PATTERN.findall(
                                row["content"].lower()
                            )
                        }
                    ),
                    row["content"],
                )
                for row in rows
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        results: list[str] = []
        remaining = max_chars
        for score, content in ranked:
            if score == 0:
                break
            if len(content) > remaining:
                content = content[:remaining]
            if content:
                results.append(content)
                remaining -= len(content)
            if len(results) >= limit or remaining <= 0:
                break
        return results

    def delete_memory(self, memory_id: int) -> bool:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cursor.rowcount == 1
