from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass
class SceneSnapshot:
    scene_key: str
    revision: int
    template_hash: str
    content_hash: str
    perception: dict[str, Any]
    created_at: float

    def to_dict(self, include_perception: bool = True) -> dict[str, Any]:
        payload = {
            "scene_key": self.scene_key,
            "revision": self.revision,
            "template_hash": self.template_hash,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
        }
        if include_perception:
            payload["perception"] = copy.deepcopy(self.perception)
        return payload


class SceneStore(Protocol):
    def get_latest(self, scene_key: str) -> SceneSnapshot | None:
        ...

    def save_snapshot(self, snapshot: SceneSnapshot) -> None:
        ...

    def save_change(
        self,
        scene_key: str,
        from_revision: int,
        to_revision: int,
        changes: dict[str, Any],
    ) -> None:
        ...

    def list_changes(self, scene_key: str, after_revision: int) -> list[dict[str, Any]]:
        ...

    def list_latest(self, limit: int = 20) -> list[dict[str, Any]]:
        ...


class InMemorySceneStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, list[SceneSnapshot]] = {}
        self._changes: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def get_latest(self, scene_key: str) -> SceneSnapshot | None:
        with self._lock:
            snapshots = self._snapshots.get(scene_key, [])
            return copy.deepcopy(snapshots[-1]) if snapshots else None

    def save_snapshot(self, snapshot: SceneSnapshot) -> None:
        with self._lock:
            snapshots = self._snapshots.setdefault(snapshot.scene_key, [])
            snapshots.append(copy.deepcopy(snapshot))

    def save_change(
        self,
        scene_key: str,
        from_revision: int,
        to_revision: int,
        changes: dict[str, Any],
    ) -> None:
        with self._lock:
            self._changes.setdefault(scene_key, []).append(
                {
                    "scene_key": scene_key,
                    "from_revision": from_revision,
                    "to_revision": to_revision,
                    "changes": copy.deepcopy(changes),
                    "created_at": time.time(),
                }
            )

    def list_changes(self, scene_key: str, after_revision: int) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(
                [
                    item
                    for item in self._changes.get(scene_key, [])
                    if item["to_revision"] > after_revision
                ]
            )

    def list_latest(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            latest = [snapshots[-1] for snapshots in self._snapshots.values() if snapshots]
            latest.sort(key=lambda item: item.created_at, reverse=True)
            return [item.to_dict(include_perception=False) for item in latest[:limit]]


class SQLiteSceneStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _init_db(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scene_snapshots (
                  scene_key TEXT NOT NULL,
                  revision INTEGER NOT NULL,
                  template_hash TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  perception_json TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  PRIMARY KEY (scene_key, revision)
                );

                CREATE INDEX IF NOT EXISTS idx_scene_snapshots_latest
                  ON scene_snapshots (scene_key, revision DESC);

                CREATE TABLE IF NOT EXISTS scene_changes (
                  scene_key TEXT NOT NULL,
                  from_revision INTEGER NOT NULL,
                  to_revision INTEGER NOT NULL,
                  changes_json TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  PRIMARY KEY (scene_key, to_revision)
                );
                """
            )
            connection.commit()

    def get_latest(self, scene_key: str) -> SceneSnapshot | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT scene_key, revision, template_hash, content_hash,
                       perception_json, created_at
                FROM scene_snapshots
                WHERE scene_key = ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (scene_key,),
            ).fetchone()
        return self._snapshot(row) if row else None

    def save_snapshot(self, snapshot: SceneSnapshot) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO scene_snapshots (
                  scene_key, revision, template_hash, content_hash,
                  perception_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.scene_key,
                    snapshot.revision,
                    snapshot.template_hash,
                    snapshot.content_hash,
                    self._json(snapshot.perception),
                    snapshot.created_at,
                ),
            )
            connection.commit()

    def save_change(
        self,
        scene_key: str,
        from_revision: int,
        to_revision: int,
        changes: dict[str, Any],
    ) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO scene_changes (
                  scene_key, from_revision, to_revision, changes_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (scene_key, from_revision, to_revision, self._json(changes), time.time()),
            )
            connection.commit()

    def list_changes(self, scene_key: str, after_revision: int) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT scene_key, from_revision, to_revision, changes_json, created_at
                FROM scene_changes
                WHERE scene_key = ? AND to_revision > ?
                ORDER BY to_revision ASC
                """,
                (scene_key, after_revision),
            ).fetchall()
        return [
            {
                "scene_key": row["scene_key"],
                "from_revision": row["from_revision"],
                "to_revision": row["to_revision"],
                "changes": json.loads(row["changes_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_latest(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT s.scene_key, s.revision, s.template_hash, s.content_hash, s.created_at
                FROM scene_snapshots s
                INNER JOIN (
                  SELECT scene_key, MAX(revision) AS revision
                  FROM scene_snapshots
                  GROUP BY scene_key
                ) latest
                  ON latest.scene_key = s.scene_key AND latest.revision = s.revision
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _snapshot(self, row: sqlite3.Row) -> SceneSnapshot:
        return SceneSnapshot(
            scene_key=row["scene_key"],
            revision=row["revision"],
            template_hash=row["template_hash"],
            content_hash=row["content_hash"],
            perception=json.loads(row["perception_json"]),
            created_at=row["created_at"],
        )

    def _json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
