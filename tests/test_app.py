import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from kt6_backend import app


class AppFactoryTest(unittest.TestCase):
    def test_import_does_not_create_global_runtime_services(self):
        self.assertFalse(hasattr(app, "RUNTIME"))
        self.assertFalse(hasattr(app, "MEMORY"))
        self.assertFalse(hasattr(app, "PAGE_PERCEPTION"))

    def test_create_services_uses_the_supplied_runtime_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            services = app.create_services(root)

            expected_runtime_dir = root / "runtime_data"
            self.assertEqual(services.memory.db_path, expected_runtime_dir / "kt6_memory.sqlite3")
            self.assertEqual(services.scene_store.db_path, expected_runtime_dir / "kt6_scene.sqlite3")
            self.assertEqual(
                services.page_capture_store.db_path,
                expected_runtime_dir / "kt6_page_captures.sqlite3",
            )
            self.assertTrue(services.memory.db_path.exists())

            with closing(sqlite3.connect(services.memory.db_path)) as connection:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(journal_mode.lower(), "wal")


if __name__ == "__main__":
    unittest.main()
