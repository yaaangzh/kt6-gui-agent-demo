from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .models import RuntimeEvent, Task


class SQLiteMemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                  task_id TEXT PRIMARY KEY,
                  query TEXT NOT NULL,
                  state TEXT NOT NULL,
                  context_json TEXT NOT NULL,
                  locks_json TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                  task_id TEXT NOT NULL,
                  event_id INTEGER NOT NULL,
                  event_type TEXT NOT NULL,
                  timestamp REAL NOT NULL,
                  payload_json TEXT NOT NULL,
                  PRIMARY KEY (task_id, event_id)
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                  checkpoint_id TEXT PRIMARY KEY,
                  task_id TEXT NOT NULL,
                  step_id TEXT NOT NULL,
                  context_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                  memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  scope TEXT NOT NULL,
                  subject TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                """
            )
            connection.commit()

    def save_task(self, task: Task) -> None:
        now = time.time()
        with self.lock, closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT created_at FROM tasks WHERE task_id = ?",
                (task.task_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            connection.execute(
                """
                INSERT OR REPLACE INTO tasks (
                  task_id, query, state, context_json, locks_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.query,
                    task.state,
                    self._json(task.context),
                    self._json(sorted(task.locks)),
                    created_at,
                    now,
                ),
            )
            connection.commit()

    def save_event(self, event: RuntimeEvent) -> None:
        with self.lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO events (
                  task_id, event_id, event_type, timestamp, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.task_id,
                    event.id,
                    event.type,
                    event.timestamp,
                    self._json(event.payload),
                ),
            )
            connection.commit()

    def save_checkpoint(self, task: Task, step_id: str) -> str:
        checkpoint_id = f"{task.task_id}:{step_id}:{int(time.time() * 1000)}"
        with self.lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO checkpoints (
                  checkpoint_id, task_id, step_id, context_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (checkpoint_id, task.task_id, step_id, self._json(task.context), time.time()),
            )
            connection.commit()
        return checkpoint_id

    def remember(self, scope: str, subject: str, kind: str, payload: dict[str, Any]) -> None:
        with self.lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO memories (scope, subject, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (scope, subject, kind, self._json(payload), time.time()),
            )
            connection.commit()

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT task_id, query, state, context_json, locks_json, created_at, updated_at
                FROM tasks
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._task_row(row) for row in rows]

    def list_memories(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT memory_id, scope, subject, kind, payload_json, created_at
                FROM memories
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "memory_id": row["memory_id"],
                "scope": row["scope"],
                "subject": row["subject"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_task_record(self, task_id: str) -> dict[str, Any] | None:
        with self.lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT task_id, query, state, context_json, locks_json, created_at, updated_at
                FROM tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        return self._task_row(row) if row else None

    def get_task_events(self, task_id: str) -> list[dict[str, Any]]:
        with self.lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT task_id, event_id, event_type, timestamp, payload_json
                FROM events
                WHERE task_id = ?
                ORDER BY event_id ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            {
                "task_id": row["task_id"],
                "id": row["event_id"],
                "type": row["event_type"],
                "timestamp": row["timestamp"],
                **json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def _task_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "query": row["query"],
            "state": row["state"],
            "context": json.loads(row["context_json"]),
            "locks": json.loads(row["locks_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
