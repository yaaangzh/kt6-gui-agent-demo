import sqlite3
from contextlib import closing
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kt6_backend import app


class AppFactoryTest(unittest.TestCase):
    def test_import_does_not_create_global_runtime_services(self):
        self.assertFalse(hasattr(app, "RUNTIME"))
        self.assertFalse(hasattr(app, "MEMORY"))
        self.assertFalse(hasattr(app, "PAGE_PERCEPTION"))

    def test_create_services_uses_the_supplied_runtime_directory(self):
        with patch.dict(os.environ, {}, clear=True), tempfile.TemporaryDirectory() as temp_dir:
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

    def test_create_services_leaves_canvas_vision_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True), tempfile.TemporaryDirectory() as temp_dir:
            services = app.create_services(Path(temp_dir))

        self.assertIsNone(services.page_perception.canvas_vision)

    def test_create_services_builds_canvas_vision_from_environment(self):
        adapter = object()
        environment = {
            "KT6_VISION_ENDPOINT": "  https://vision.internal/v1/topology  ",
            "KT6_VISION_API_KEY": "  production-secret  ",
            "KT6_VISION_TIMEOUT_SECONDS": "12.5",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(app, "HTTPTopologyVisionAdapter", return_value=adapter) as constructor,
            tempfile.TemporaryDirectory() as temp_dir,
        ):
            services = app.create_services(Path(temp_dir))

        self.assertIs(services.page_perception.canvas_vision, adapter)
        constructor.assert_called_once_with(
            endpoint="https://vision.internal/v1/topology",
            api_key="production-secret",
            timeout_seconds=12.5,
        )

    def test_create_services_uses_default_canvas_vision_timeout(self):
        adapter = object()
        with (
            patch.dict(
                os.environ,
                {"KT6_VISION_ENDPOINT": "https://vision.internal/v1/topology"},
                clear=True,
            ),
            patch.object(app, "HTTPTopologyVisionAdapter", return_value=adapter) as constructor,
            tempfile.TemporaryDirectory() as temp_dir,
        ):
            services = app.create_services(Path(temp_dir))

        self.assertIs(services.page_perception.canvas_vision, adapter)
        constructor.assert_called_once_with(
            endpoint="https://vision.internal/v1/topology",
            api_key=None,
            timeout_seconds=30.0,
        )

    def test_create_services_builds_read_only_codeagent_vision_without_auth(self):
        adapter = object()
        environment = {
            "KT6_VISION_DRIVER": "codeagent_cli",
            "KT6_CODEAGENT_EXECUTABLE": "codeagent-test",
            "KT6_CODEAGENT_AGENT": "site-topology-reader",
            "KT6_VISION_TIMEOUT_SECONDS": "75",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(app, "CodeAgentCanvasVisionAdapter", return_value=adapter) as constructor,
            tempfile.TemporaryDirectory() as temp_dir,
        ):
            root = Path(temp_dir).resolve()
            services = app.create_services(root)

        self.assertIs(services.page_perception.canvas_vision, adapter)
        constructor.assert_called_once_with(
            workdir=root,
            executable="codeagent-test",
            agent="site-topology-reader",
            timeout_seconds=75.0,
        )

    def test_codeagent_vision_uses_safe_defaults_and_rejects_http_secrets(self):
        adapter = object()
        with (
            patch.dict(
                os.environ,
                {"KT6_VISION_DRIVER": "codeagent_cli"},
                clear=True,
            ),
            patch.object(app, "CodeAgentCanvasVisionAdapter", return_value=adapter) as constructor,
            tempfile.TemporaryDirectory() as temp_dir,
        ):
            root = Path(temp_dir).resolve()
            services = app.create_services(root)

        self.assertIs(services.page_perception.canvas_vision, adapter)
        constructor.assert_called_once_with(
            workdir=root,
            executable="codeagent",
            agent="kt6-topology-vision",
            timeout_seconds=120.0,
        )

        for conflicting in (
            {"KT6_VISION_ENDPOINT": "https://vision.internal/v1"},
            {"KT6_VISION_API_KEY": "must-not-be-passed"},
        ):
            environment = {"KT6_VISION_DRIVER": "codeagent_cli", **conflicting}
            with self.subTest(conflicting=tuple(conflicting)), patch.dict(
                os.environ, environment, clear=True
            ), tempfile.TemporaryDirectory() as temp_dir, self.assertRaisesRegex(
                ValueError, "must not be configured"
            ):
                app.create_services(Path(temp_dir))

    def test_codeagent_specific_config_requires_explicit_driver(self):
        for environment in (
            {"KT6_CODEAGENT_EXECUTABLE": "codeagent"},
            {"KT6_CODEAGENT_AGENT": "kt6-topology-vision"},
            {"KT6_VISION_DRIVER": "unknown"},
        ):
            with self.subTest(environment=tuple(environment)), patch.dict(
                os.environ, environment, clear=True
            ), tempfile.TemporaryDirectory() as temp_dir, self.assertRaises(ValueError):
                app.create_services(Path(temp_dir))

    def test_create_services_builds_local_cv_ocr_vision_without_remote_config(self):
        adapter = object()
        with (
            patch.dict(
                os.environ,
                {"KT6_VISION_DRIVER": "local_cv_ocr"},
                clear=True,
            ),
            patch.object(
                app,
                "LocalCVTopologyVisionAdapter",
                return_value=adapter,
            ) as constructor,
            tempfile.TemporaryDirectory() as temp_dir,
        ):
            services = app.create_services(Path(temp_dir))

        self.assertIs(services.page_perception.canvas_vision, adapter)
        constructor.assert_called_once_with()

    def test_local_cv_ocr_rejects_remote_and_codeagent_configuration(self):
        for conflicting in (
            {"KT6_VISION_ENDPOINT": "https://vision.internal/v1/topology"},
            {"KT6_VISION_API_KEY": "must-not-be-passed"},
            {"KT6_VISION_TIMEOUT_SECONDS": "30"},
            {"KT6_CODEAGENT_EXECUTABLE": "codeagent"},
            {"KT6_CODEAGENT_AGENT": "kt6-topology-vision"},
        ):
            environment = {"KT6_VISION_DRIVER": "local_cv_ocr", **conflicting}
            with (
                self.subTest(conflicting=tuple(conflicting)),
                patch.dict(os.environ, environment, clear=True),
                patch.object(app, "LocalCVTopologyVisionAdapter") as constructor,
                tempfile.TemporaryDirectory() as temp_dir,
                self.assertRaisesRegex(
                    ValueError,
                    "must not be configured for local_cv_ocr",
                ),
            ):
                app.create_services(Path(temp_dir))

            constructor.assert_not_called()

    def test_maximum_vision_timeout_is_accepted_by_factory_and_adapter(self):
        with (
            patch.dict(
                os.environ,
                {
                    "KT6_VISION_ENDPOINT": "https://vision.internal/v1/topology",
                    "KT6_VISION_TIMEOUT_SECONDS": "300",
                },
                clear=True,
            ),
            tempfile.TemporaryDirectory() as temp_dir,
        ):
            services = app.create_services(Path(temp_dir))

        self.assertEqual(services.page_perception.canvas_vision.timeout_seconds, 300.0)

    def test_vision_companion_config_requires_endpoint_before_runtime_creation(self):
        secret = "must-not-appear-in-errors"
        for environment in (
            {"KT6_VISION_API_KEY": secret},
            {"KT6_VISION_TIMEOUT_SECONDS": "10"},
        ):
            with self.subTest(environment=tuple(environment)), patch.dict(
                os.environ,
                environment,
                clear=True,
            ), tempfile.TemporaryDirectory() as temp_dir:
                runtime_dir = Path(temp_dir) / "runtime_data"
                with self.assertRaises(ValueError) as raised:
                    app.create_services(Path(temp_dir))

                self.assertIn("KT6_VISION_ENDPOINT", str(raised.exception))
                self.assertNotIn(secret, str(raised.exception))
                self.assertFalse(runtime_dir.exists())

    def test_invalid_vision_timeouts_fail_fast_without_exposing_api_key(self):
        secret = "must-not-appear-in-errors"
        for timeout in ("not-a-number", "nan", "inf", "0", "-1", "300.1"):
            with self.subTest(timeout=timeout), patch.dict(
                os.environ,
                {
                    "KT6_VISION_ENDPOINT": "https://vision.internal/v1/topology",
                    "KT6_VISION_API_KEY": secret,
                    "KT6_VISION_TIMEOUT_SECONDS": timeout,
                },
                clear=True,
            ), tempfile.TemporaryDirectory() as temp_dir:
                runtime_dir = Path(temp_dir) / "runtime_data"
                with self.assertRaises(ValueError) as raised:
                    app.create_services(Path(temp_dir))

                self.assertIn("KT6_VISION_TIMEOUT_SECONDS", str(raised.exception))
                self.assertNotIn(secret, str(raised.exception))
                self.assertFalse(runtime_dir.exists())

    def test_invalid_vision_endpoint_fails_before_runtime_creation(self):
        secret = "must-not-appear-in-errors"
        with patch.dict(
            os.environ,
            {
                "KT6_VISION_ENDPOINT": "http://vision.internal/v1/topology",
                "KT6_VISION_API_KEY": secret,
            },
            clear=True,
        ), tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir) / "runtime_data"
            with self.assertRaises(ValueError) as raised:
                app.create_services(Path(temp_dir))

            self.assertIn("HTTPS", str(raised.exception))
            self.assertNotIn(secret, str(raised.exception))
            self.assertFalse(runtime_dir.exists())

    def test_request_body_is_json_only_and_bounded(self):
        handler = object.__new__(app.KT6Handler)
        handler.headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": "7",
        }
        handler.rfile = BytesIO(b'{"x":1}')
        self.assertEqual(handler._body(), {"x": 1})

        handler.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(app.MAX_JSON_REQUEST_BYTES + 1),
        }
        handler.rfile = BytesIO()
        with self.assertRaises(app.RequestBodyTooLarge):
            handler._body()

        handler.headers = {"Content-Type": "text/plain", "Content-Length": "2"}
        handler.rfile = BytesIO(b"{}")
        with self.assertRaisesRegex(ValueError, "Content-Type"):
            handler._body()


if __name__ == "__main__":
    unittest.main()
