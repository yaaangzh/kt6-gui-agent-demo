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

    def test_canvas_vision_health_is_safe_and_describes_adaptive_routing(self):
        self.assertEqual(
            app._canvas_vision_health(None),
            {
                "configured": False,
                "adapter_id": None,
                "adapter_version": None,
                "routing_mode": "evidence_only",
                "timeout_seconds": None,
            },
        )

        class Adapter:
            adapter_id = "model"
            adapter_version = "2.0"
            timeout_seconds = 75

        class Local:
            adapter_id = "local-cv"

        class Hybrid:
            adapter_id = "hybrid"
            adapter_version = "1.0"
            local_adapter = Local()
            model_adapter = Adapter()

        health = app._canvas_vision_health(Hybrid())
        self.assertTrue(health["configured"])
        self.assertEqual(health["routing_mode"], "cv_first_adaptive")
        self.assertEqual(health["timeout_seconds"], 75.0)
        self.assertEqual(health["local_adapter_id"], "local-cv")
        self.assertEqual(health["model_adapter_id"], "model")
        self.assertNotIn("endpoint", health)
        self.assertNotIn("api_key", health)

        for invalid_timeout in (
            float("nan"),
            float("inf"),
            float("-inf"),
            "invalid",
        ):
            with self.subTest(invalid_timeout=invalid_timeout):
                invalid_adapter = type(
                    "InvalidTimeoutAdapter",
                    (),
                    {"timeout_seconds": invalid_timeout},
                )()
                self.assertIsNone(
                    app._canvas_vision_health(invalid_adapter)["timeout_seconds"]
                )

    @staticmethod
    def model_environment():
        return {
            "KT6_MODEL_API_PROVIDER": "test-gateway",
            "KT6_MODEL_API_BASE_URL": "https://models.internal/v1",
            "KT6_MODEL_API_KEY": "must-not-appear-in-health",
            "KT6_MODEL_API_MODEL": "planner-model",
            "KT6_MODEL_API_ALLOWED_HOSTS": "models.internal",
            "KT6_MODEL_API_TIMEOUT_SECONDS": "75",
            "KT6_MODEL_API_MAX_TOKENS": "8192",
        }

    def test_hybrid_shares_api_config_but_can_use_a_distinct_vision_model(self):
        environment = {
            **self.model_environment(),
            "KT6_VISION_DRIVER": "hybrid",
            "KT6_VISION_MODEL": "image-model",
        }
        with patch.dict(os.environ, environment, clear=True), tempfile.TemporaryDirectory() as temp_dir:
            services = app.create_services(Path(temp_dir))
            vision = services.page_perception.canvas_vision
            planner = services.execution_scenarios.planner

        self.assertIsInstance(vision, app.HybridCanvasVisionAdapter)
        self.assertIsInstance(vision.local_adapter, app.LocalCVTopologyVisionAdapter)
        self.assertEqual(vision.requested_profile, "auto")
        self.assertIsInstance(vision.model_adapter, app.OpenAICompatibleCanvasVisionAdapter)
        self.assertEqual(vision.model_adapter.client.endpoint, "https://models.internal/v1/chat/completions")
        self.assertEqual(vision.model_adapter.client.api_key, environment["KT6_MODEL_API_KEY"])
        self.assertEqual(vision.model_adapter.model, "image-model")
        self.assertEqual(vision.model_adapter.timeout_seconds, 75)
        self.assertEqual(vision.model_adapter.max_tokens, 8192)
        self.assertEqual(planner.client.model, "planner-model")
        health = app._canvas_vision_health(vision)
        self.assertEqual(health["routing_mode"], "cv_first_adaptive")
        self.assertEqual(health["model"], "image-model")
        self.assertEqual(health["provider"], "test-gateway")
        self.assertNotIn(environment["KT6_MODEL_API_KEY"], str(health))
        self.assertNotIn("models.internal", str(health))

    def test_direct_vision_uses_the_configured_general_model_when_not_overridden(self):
        environment = {
            **self.model_environment(),
            "KT6_VISION_DRIVER": "openai_compatible",
        }
        with patch.dict(os.environ, environment, clear=True):
            adapter = app._create_canvas_vision_from_env()
        self.assertIsInstance(adapter, app.OpenAICompatibleCanvasVisionAdapter)
        self.assertEqual(adapter.model, "planner-model")
        self.assertFalse(adapter.supports_actionable_grounding)

    def test_general_model_config_does_not_enable_image_transmission(self):
        with patch.dict(os.environ, self.model_environment(), clear=True):
            self.assertIsNone(app._create_canvas_vision_from_env())

    def test_local_cv_does_not_construct_a_model_client(self):
        with (
            patch.dict(os.environ, {"KT6_VISION_DRIVER": "local_cv_ocr"}, clear=True),
            patch.object(app, "_create_model_client_from_env") as model_factory,
        ):
            adapter = app._create_canvas_vision_from_env()
        self.assertIsInstance(adapter, app.LocalCVTopologyVisionAdapter)
        model_factory.assert_not_called()

    def test_vision_model_override_requires_an_explicit_model_driver(self):
        for driver in (None, "local_cv_ocr"):
            environment = {"KT6_VISION_MODEL": "image-model"}
            if driver:
                environment["KT6_VISION_DRIVER"] = driver
            with self.subTest(driver=driver), patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, "KT6_VISION_MODEL"):
                    app._create_canvas_vision_from_env()

    def test_unknown_and_removed_drivers_are_rejected(self):
        for driver in ("unknown", "http"):
            with self.subTest(driver=driver), patch.dict(
                os.environ, {"KT6_VISION_DRIVER": driver}, clear=True
            ), self.assertRaisesRegex(ValueError, "openai_compatible, local_cv_ocr, or hybrid"):
                app._create_canvas_vision_from_env()

    def test_model_vision_missing_api_configuration_fails_before_runtime_creation(self):
        for driver in ("hybrid", "openai_compatible"):
            for config in ({}, {"KT6_MODEL_API_KEY": "secret-never-in-errors"}):
                environment = {**config, "KT6_VISION_DRIVER": driver}
                with (
                    self.subTest(driver=driver, config=tuple(config)),
                    patch.dict(os.environ, environment, clear=True),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    with self.assertRaisesRegex(ValueError, "KT6_MODEL_API") as raised:
                        app.create_services(Path(temp_dir))
                    self.assertNotIn("secret-never-in-errors", str(raised.exception))
                    self.assertFalse((Path(temp_dir) / "runtime_data").exists())

    def test_vision_uses_the_shared_api_host_boundary(self):
        environment = {
            **self.model_environment(),
            "KT6_VISION_DRIVER": "hybrid",
            "KT6_MODEL_API_ALLOWED_HOSTS": "other.internal",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "allowed_hosts"):
                app._create_canvas_vision_from_env()

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
