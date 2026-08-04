from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable


class VisionResultCacheValidationError(ValueError):
    """A cache key, selector, or result is unsafe to persist."""


class SQLiteVisionResultCacheStore:
    """Thread-safe, bounded cache for already-computed vision results.

    The store deliberately knows nothing about a vision adapter's execution.
    Callers build the cache key and decide when a cached result is reusable.
    TTL is an absolute lifetime measured from ``created_at``; a cache hit only
    advances ``last_used_at`` for recency-based capacity pruning.
    """

    DEFAULT_TTL_SECONDS = 24 * 60 * 60
    DEFAULT_MAX_ENTRIES = 256
    MAX_CACHE_KEY_LENGTH = 512
    MAX_PAGE_ROUTE_LENGTH = 4096
    MAX_ADAPTER_ID_LENGTH = 200
    MAX_ADAPTER_VERSION_LENGTH = 100
    MAX_LIMIT = 1000

    def __init__(
        self,
        db_path: Path,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path)
        self.ttl_seconds = self._positive_finite(ttl_seconds, "ttl_seconds")
        self.max_entries = self._positive_int(max_entries, "max_entries")
        if not callable(clock):
            raise VisionResultCacheValidationError("clock must be callable")
        self._clock = clock
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def put(
        self,
        *,
        cache_key: str,
        page_route: str,
        adapter_id: str,
        adapter_version: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Insert or refresh one exact-key result and return a detached entry."""

        normalized_key = self._text(
            cache_key, "cache_key", maximum=self.MAX_CACHE_KEY_LENGTH
        )
        normalized_route = self._text(
            page_route, "page_route", maximum=self.MAX_PAGE_ROUTE_LENGTH
        )
        normalized_adapter_id = self._text(
            adapter_id, "adapter_id", maximum=self.MAX_ADAPTER_ID_LENGTH
        )
        normalized_adapter_version = self._text(
            adapter_version,
            "adapter_version",
            maximum=self.MAX_ADAPTER_VERSION_LENGTH,
        )
        result_json = self._encode_result(result)
        now = self._now()

        with self._lock, closing(self._connect()) as connection:
            self._prune_expired(connection, now)
            connection.execute(
                """
                INSERT INTO vision_result_cache (
                  cache_key, page_route, adapter_id, adapter_version,
                  result_json, created_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                  page_route = excluded.page_route,
                  adapter_id = excluded.adapter_id,
                  adapter_version = excluded.adapter_version,
                  result_json = excluded.result_json,
                  last_used_at = excluded.last_used_at
                """,
                (
                    normalized_key,
                    normalized_route,
                    normalized_adapter_id,
                    normalized_adapter_version,
                    result_json,
                    now,
                    now,
                ),
            )
            self._prune_capacity(connection)
            row = self._select_exact(connection, normalized_key)
            connection.commit()

        if row is None:  # Defensive: the just-written row should be the newest.
            raise RuntimeError("vision cache capacity pruning removed the inserted entry")
        return self._entry(row)

    def get(self, cache_key: str) -> dict[str, Any] | None:
        """Return an exact-key hit and advance its ``last_used_at`` timestamp."""

        normalized_key = self._text(
            cache_key, "cache_key", maximum=self.MAX_CACHE_KEY_LENGTH
        )
        now = self._now()
        with self._lock, closing(self._connect()) as connection:
            self._prune_expired(connection, now)
            row = self._select_exact(connection, normalized_key)
            if row is None:
                connection.commit()
                return None
            try:
                self._decode_result(row["result_json"])
            except VisionResultCacheValidationError:
                connection.execute(
                    "DELETE FROM vision_result_cache WHERE cache_key = ?",
                    (normalized_key,),
                )
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE vision_result_cache
                SET last_used_at = ?
                WHERE cache_key = ?
                """,
                (now, normalized_key),
            )
            row = self._select_exact(connection, normalized_key)
            connection.commit()
        return self._entry(row) if row is not None else None

    def list_recent_candidates(
        self,
        *,
        page_route: str,
        adapter_id: str,
        adapter_version: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List unexpired candidates for one route and adapter, newest use first."""

        normalized_route = self._text(
            page_route, "page_route", maximum=self.MAX_PAGE_ROUTE_LENGTH
        )
        normalized_adapter_id = self._text(
            adapter_id, "adapter_id", maximum=self.MAX_ADAPTER_ID_LENGTH
        )
        normalized_adapter_version = self._text(
            adapter_version,
            "adapter_version",
            maximum=self.MAX_ADAPTER_VERSION_LENGTH,
        )
        normalized_limit = self._positive_int(limit, "limit", maximum=self.MAX_LIMIT)
        now = self._now()
        with self._lock, closing(self._connect()) as connection:
            self._prune_expired(connection, now)
            rows = connection.execute(
                """
                SELECT cache_key, page_route, adapter_id, adapter_version,
                       result_json, created_at, last_used_at
                FROM vision_result_cache
                WHERE page_route = ? AND adapter_id = ? AND adapter_version = ?
                ORDER BY last_used_at DESC, created_at DESC, rowid DESC
                LIMIT ?
                """,
                (
                    normalized_route,
                    normalized_adapter_id,
                    normalized_adapter_version,
                    normalized_limit,
                ),
            ).fetchall()
            valid: list[dict[str, Any]] = []
            corrupt_keys: list[str] = []
            for row in rows:
                try:
                    valid.append(self._entry(row))
                except VisionResultCacheValidationError:
                    corrupt_keys.append(str(row["cache_key"]))
            if corrupt_keys:
                connection.executemany(
                    "DELETE FROM vision_result_cache WHERE cache_key = ?",
                    ((key,) for key in corrupt_keys),
                )
            connection.commit()
        return valid

    def prune(self) -> int:
        """Remove expired and over-capacity entries, returning the row count removed."""

        now = self._now()
        with self._lock, closing(self._connect()) as connection:
            before = int(
                connection.execute(
                    "SELECT COUNT(*) FROM vision_result_cache"
                ).fetchone()[0]
            )
            self._prune_expired(connection, now)
            self._prune_capacity(connection)
            after = int(
                connection.execute(
                    "SELECT COUNT(*) FROM vision_result_cache"
                ).fetchone()[0]
            )
            connection.commit()
        return before - after

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _init_db(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS vision_result_cache (
                  cache_key TEXT NOT NULL PRIMARY KEY,
                  page_route TEXT NOT NULL,
                  adapter_id TEXT NOT NULL,
                  adapter_version TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  last_used_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_vision_result_candidates
                  ON vision_result_cache (
                    page_route, adapter_id, adapter_version,
                    last_used_at DESC, created_at DESC
                  );

                CREATE INDEX IF NOT EXISTS idx_vision_result_created
                  ON vision_result_cache (created_at ASC);
                """
            )
            connection.commit()

    @staticmethod
    def _select_exact(
        connection: sqlite3.Connection, cache_key: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT cache_key, page_route, adapter_id, adapter_version,
                   result_json, created_at, last_used_at
            FROM vision_result_cache
            WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()

    def _prune_expired(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            "DELETE FROM vision_result_cache WHERE created_at <= ?",
            (now - self.ttl_seconds,),
        )

    def _prune_capacity(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM vision_result_cache
            WHERE cache_key IN (
              SELECT cache_key
              FROM vision_result_cache
              ORDER BY last_used_at DESC, created_at DESC, rowid DESC
              LIMIT -1 OFFSET ?
            )
            """,
            (self.max_entries,),
        )

    @classmethod
    def _entry(cls, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "cache_key": str(row["cache_key"]),
            "page_route": str(row["page_route"]),
            "adapter_id": str(row["adapter_id"]),
            "adapter_version": str(row["adapter_version"]),
            "result": cls._decode_result(row["result_json"]),
            "created_at": float(row["created_at"]),
            "last_used_at": float(row["last_used_at"]),
        }

    @staticmethod
    def _encode_result(result: dict[str, Any]) -> str:
        if not isinstance(result, dict):
            raise VisionResultCacheValidationError("result must be a JSON object")
        try:
            return json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise VisionResultCacheValidationError(
                "result must contain only finite JSON-compatible values"
            ) from exc

    @staticmethod
    def _decode_result(encoded: Any) -> dict[str, Any]:
        if not isinstance(encoded, str):
            raise VisionResultCacheValidationError("cached result is not JSON text")
        try:
            result = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise VisionResultCacheValidationError("cached result JSON is invalid") from exc
        if not isinstance(result, dict):
            raise VisionResultCacheValidationError(
                "cached result must decode to a JSON object"
            )
        return result

    def _now(self) -> float:
        try:
            now = float(self._clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise VisionResultCacheValidationError(
                "clock must return a finite timestamp"
            ) from exc
        if not math.isfinite(now):
            raise VisionResultCacheValidationError(
                "clock must return a finite timestamp"
            )
        return now

    @staticmethod
    def _text(value: Any, field: str, *, maximum: int) -> str:
        if not isinstance(value, str):
            raise VisionResultCacheValidationError(f"{field} must be a string")
        normalized = value.strip()
        if not normalized:
            raise VisionResultCacheValidationError(f"{field} must not be empty")
        if len(normalized) > maximum:
            raise VisionResultCacheValidationError(
                f"{field} must be at most {maximum} characters"
            )
        if "\x00" in normalized:
            raise VisionResultCacheValidationError(f"{field} contains a null byte")
        return normalized

    @staticmethod
    def _positive_finite(value: Any, field: str) -> float:
        if isinstance(value, bool):
            raise VisionResultCacheValidationError(f"{field} must be positive and finite")
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise VisionResultCacheValidationError(
                f"{field} must be positive and finite"
            ) from exc
        if not math.isfinite(numeric) or numeric <= 0:
            raise VisionResultCacheValidationError(
                f"{field} must be positive and finite"
            )
        return numeric

    @staticmethod
    def _positive_int(value: Any, field: str, *, maximum: int | None = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise VisionResultCacheValidationError(f"{field} must be a positive integer")
        if maximum is not None and value > maximum:
            raise VisionResultCacheValidationError(
                f"{field} must be at most {maximum}"
            )
        return value


__all__ = [
    "SQLiteVisionResultCacheStore",
    "VisionResultCacheValidationError",
]
