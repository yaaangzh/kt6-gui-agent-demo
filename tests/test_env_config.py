from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kt6_backend.env_config import EnvironmentFileError, load_project_env


class EnvironmentConfigTest(unittest.TestCase):
    def test_missing_file_is_optional(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            environment: dict[str, str] = {}
            self.assertEqual(load_project_env(Path(temp_dir), environ=environment), ())
            self.assertEqual(environment, {})

    def test_loads_values_but_process_environment_wins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(
                "# local config\n"
                "KT6_MODEL_API_PROVIDER=local-gateway\n"
                "KT6_MODEL_API_KEY='local secret'\n"
                "KT6_MODEL_API_MODEL=model-from-file\n",
                encoding="utf-8",
            )
            environment = {"KT6_MODEL_API_MODEL": "model-from-process"}
            loaded = load_project_env(root, environ=environment)
            self.assertEqual(
                loaded,
                ("KT6_MODEL_API_PROVIDER", "KT6_MODEL_API_KEY"),
            )
            self.assertEqual(environment["KT6_MODEL_API_PROVIDER"], "local-gateway")
            self.assertEqual(environment["KT6_MODEL_API_KEY"], "local secret")
            self.assertEqual(environment["KT6_MODEL_API_MODEL"], "model-from-process")

    def test_rejects_duplicate_invalid_and_unterminated_entries(self):
        invalid_files = (
            "A=1\nA=2\n",
            "BAD-KEY=value\n",
            "MISSING_EQUALS\n",
            "KEY='unterminated\n",
        )
        for content in invalid_files:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                (root / ".env").write_text(content, encoding="utf-8")
                with self.assertRaises(EnvironmentFileError):
                    load_project_env(root, environ={})


if __name__ == "__main__":
    unittest.main()
