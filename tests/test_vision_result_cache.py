from __future__ import annotations

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from kt6_backend.vision_result_cache import (
    SQLiteVisionResultCacheStore,
    VisionResultCacheValidationError,
)


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class SQLiteVisionResultCacheStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.clock = FakeClock()
        self.db_path = Path(self.temp_dir.name) / "vision-cache.sqlite3"
        self.store = SQLiteVisionResultCacheStore(
            self.db_path,
            ttl_seconds=60,
            max_entries=3,
            clock=self.clock,
        )

    def put(self, key: str, *, route: str = "/topology", value: int = 1) -> dict:
        return self.store.put(
            cache_key=key,
            page_route=route,
            adapter_id="hybrid",
            adapter_version="1.0",
            result={"objects": [{"id": key}], "value": value},
        )

    def test_exact_get_returns_json_detached_copies_and_updates_last_used(self):
        source = {"objects": [{"id": "site-1"}], "nested": {"alarm": False}}
        inserted = self.store.put(
            cache_key="sha256:one",
            page_route="https://example.invalid/topology",
            adapter_id="hybrid",
            adapter_version="1.0",
            result=source,
        )
        source["nested"]["alarm"] = True
        inserted["result"]["objects"][0]["id"] = "mutated"
        self.clock.advance(5)

        hit = self.store.get("sha256:one")

        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["result"]["objects"][0]["id"], "site-1")
        self.assertFalse(hit["result"]["nested"]["alarm"])
        self.assertEqual(hit["created_at"], 1_000.0)
        self.assertEqual(hit["last_used_at"], 1_005.0)
        hit["result"]["nested"]["alarm"] = True
        self.assertFalse(self.store.get("sha256:one")["result"]["nested"]["alarm"])

    def test_put_upserts_result_but_preserves_created_timestamp(self):
        self.put("same", value=1)
        self.clock.advance(7)
        updated = self.put("same", route="/new-route", value=2)

        self.assertEqual(updated["created_at"], 1_000.0)
        self.assertEqual(updated["last_used_at"], 1_007.0)
        self.assertEqual(updated["page_route"], "/new-route")
        self.assertEqual(updated["result"]["value"], 2)

    def test_recent_candidates_are_filtered_and_ordered_by_use(self):
        store = SQLiteVisionResultCacheStore(
            self.db_path,
            ttl_seconds=60,
            max_entries=10,
            clock=self.clock,
        )
        store.put(
            cache_key="first",
            page_route="/topology",
            adapter_id="hybrid",
            adapter_version="1.0",
            result={"value": 1},
        )
        self.clock.advance(1)
        store.put(
            cache_key="second",
            page_route="/topology",
            adapter_id="hybrid",
            adapter_version="1.0",
            result={"value": 2},
        )
        store.put(
            cache_key="other-adapter",
            page_route="/topology",
            adapter_id="model-only",
            adapter_version="1.0",
            result={"value": 3},
        )
        store.put(
            cache_key="other-version",
            page_route="/topology",
            adapter_id="hybrid",
            adapter_version="2.0",
            result={"value": 4},
        )
        self.clock.advance(1)
        store.get("first")

        candidates = store.list_recent_candidates(
            page_route="/topology",
            adapter_id="hybrid",
            adapter_version="1.0",
            limit=2,
        )

        self.assertEqual(
            [item["cache_key"] for item in candidates],
            ["first", "second"],
        )

    def test_ttl_is_absolute_and_expired_hits_are_removed(self):
        self.put("expires")
        self.clock.advance(59)
        self.assertIsNotNone(self.store.get("expires"))
        self.clock.advance(1)

        self.assertIsNone(self.store.get("expires"))
        self.assertEqual(
            self.store.list_recent_candidates(
                page_route="/topology",
                adapter_id="hybrid",
                adapter_version="1.0",
            ),
            [],
        )

    def test_capacity_prunes_least_recently_used_entry(self):
        self.put("one")
        self.clock.advance(1)
        self.put("two")
        self.clock.advance(1)
        self.put("three")
        self.clock.advance(1)
        self.store.get("one")
        self.clock.advance(1)
        self.put("four")

        self.assertIsNone(self.store.get("two"))
        self.assertIsNotNone(self.store.get("one"))
        self.assertIsNotNone(self.store.get("three"))
        self.assertIsNotNone(self.store.get("four"))

    def test_validation_rejects_unsafe_selectors_and_non_json_results(self):
        valid = {
            "cache_key": "valid",
            "page_route": "/topology",
            "adapter_id": "hybrid",
            "adapter_version": "1.0",
        }
        for field, value in (
            ("cache_key", ""),
            ("page_route", "bad\x00route"),
            ("adapter_id", None),
            ("adapter_version", "x" * 101),
        ):
            arguments = {**valid, field: value, "result": {}}
            with self.subTest(field=field), self.assertRaises(
                VisionResultCacheValidationError
            ):
                self.store.put(**arguments)

        with self.assertRaises(VisionResultCacheValidationError):
            self.store.put(**valid, result=[])
        with self.assertRaises(VisionResultCacheValidationError):
            self.store.put(**valid, result={"bad": object()})
        with self.assertRaises(VisionResultCacheValidationError):
            self.store.put(**valid, result={"bad": float("nan")})

    def test_corrupt_database_payload_is_treated_as_a_cache_miss(self):
        self.put("corrupt")
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE vision_result_cache SET result_json = ? WHERE cache_key = ?",
                ("not-json", "corrupt"),
            )
            connection.commit()

        self.assertIsNone(self.store.get("corrupt"))
        self.assertIsNone(self.store.get("corrupt"))

    def test_operations_are_safe_across_threads(self):
        store = SQLiteVisionResultCacheStore(
            Path(self.temp_dir.name) / "threaded.sqlite3",
            ttl_seconds=60,
            max_entries=100,
        )

        def write_and_read(index: int) -> str:
            key = f"key-{index}"
            store.put(
                cache_key=key,
                page_route="/threaded",
                adapter_id="hybrid",
                adapter_version="1.0",
                result={"index": index},
            )
            hit = store.get(key)
            assert hit is not None
            return hit["cache_key"]

        with ThreadPoolExecutor(max_workers=8) as executor:
            observed = set(executor.map(write_and_read, range(32)))

        self.assertEqual(observed, {f"key-{index}" for index in range(32)})
        candidates = store.list_recent_candidates(
            page_route="/threaded",
            adapter_id="hybrid",
            adapter_version="1.0",
            limit=100,
        )
        self.assertEqual(len(candidates), 32)


if __name__ == "__main__":
    unittest.main()
