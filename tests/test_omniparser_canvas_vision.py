from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kt6_backend.app import _create_canvas_vision_from_env
from kt6_backend.codeagent_canvas_vision import CodeAgentCanvasVisionAdapter
from kt6_backend.omniparser_canvas_vision import (
    OmniParserCanvasVisionAdapter,
    OmniParserHTTPResponse,
    OmniParserResponseError,
    OmniParserTransportError,
)
from kt6_backend.vision_recognition import CanvasFrame


SOM_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 96


def _payload(*, elements=None, latency=1.25, som=None):
    return {
        "som_image_base64": base64.b64encode(som or SOM_PNG).decode("ascii"),
        "parsed_content_list": (
            [
                {
                    "type": "text",
                    "content": "AP-001",
                    "bbox": [0.1, 0.2, 0.3, 0.4],
                    "interactivity": False,
                    "source": "box_ocr_content_ocr",
                },
                {
                    "type": "icon",
                    "content": "wifi",
                    "bbox": [0.5, 0.6, 0.7, 0.8],
                    "interactivity": True,
                },
            ]
            if elements is None
            else elements
        ),
        "latency": latency,
    }


class FakeOmniParserTransport:
    def __init__(self, *, payload=None, error=None, status=200, body=None):
        self.payload = payload
        self.error = error
        self.status = status
        self.body = body
        self.calls = []

    def post(self, *, url, body, timeout_seconds, max_response_bytes):
        self.calls.append(
            {
                "url": url,
                "body": json.loads(body.decode("utf-8")),
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        if self.error is not None:
            raise self.error
        response_body = self.body
        if response_body is None:
            response_body = json.dumps(self.payload, separators=(",", ":")).encode(
                "utf-8"
            )
        return OmniParserHTTPResponse(status=self.status, body=response_body)


class RecordingModelAdapter:
    adapter_id = "model-recording"
    adapter_version = "1.0"

    def __init__(self) -> None:
        self.calls = []

    def recognize(self, *, page, frames):
        self.calls.append({"kind": "plain", "frames": frames})
        return {"objects": [], "links": []}

    def recognize_with_context(self, *, page, frames, cv_observations):
        self.calls.append(
            {
                "kind": "contextual",
                "frames": frames,
                "observations": cv_observations,
            }
        )
        return {
            "objects": [
                {
                    "business_id": "AP-001",
                    "label": "AP-001",
                    "bbox": [10, 20, 30, 40],
                    "confidence": 0.9,
                }
            ],
            "links": [
                {
                    "source": "AP-001",
                    "target": "CORE-001",
                    "type": "link",
                    "confidence": 0.8,
                }
            ],
        }


class OmniParserCanvasVisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.image_path = self.root / "frame.png"
        self.image_path.write_bytes(SOM_PNG)
        self.frames = (
            CanvasFrame(
                canvas_id="topology",
                screenshot_path=self.image_path,
                screenshot_sha256="0" * 64,
                mime_type="image/png",
                width=800,
                height=600,
                client_width=800,
                client_height=600,
                bbox=(0.0, 0.0, 800.0, 600.0),
                source_kind="canvas",
                source_type="canvas",
                capture_kind="element",
                region_selector="#topology",
                roi_status="verified",
            ),
        )

    def make_adapter(self, transport, model=None):
        return OmniParserCanvasVisionAdapter(
            model_adapter=model or RecordingModelAdapter(),
            endpoint="http://127.0.0.1:8000/parse/",
            workdir=self.root / "omniparser_som",
            timeout_seconds=60.0,
            transport=transport,
        )

    def test_parses_elements_and_calls_model_with_som_frames(self):
        transport = FakeOmniParserTransport(payload=_payload())
        model = RecordingModelAdapter()
        adapter = self.make_adapter(transport, model=model)

        result = adapter.recognize(
            page={"url": "https://example.invalid/"},
            frames=self.frames,
        )

        self.assertEqual(len(transport.calls), 1)
        self.assertIn("base64_image", transport.calls[0]["body"])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0]["kind"], "contextual")

        observations = model.calls[0]["observations"]
        self.assertEqual(observations["source"], "omniparser_structured_elements")
        self.assertEqual(len(observations["elements"]), 2)
        self.assertEqual(observations["elements"][0]["idx"], 0)
        self.assertEqual(observations["elements"][0]["content"], "AP-001")
        self.assertEqual(observations["objects"][0]["business_id"], "AP-001")
        self.assertEqual(observations["objects"][0]["bbox"], [80.0, 120.0, 160.0, 120.0])
        self.assertEqual(observations["objects"][0]["attributes"]["omniparser_type"], "text")
        self.assertEqual(observations["objects"][1]["business_id"], "wifi")
        self.assertTrue(observations["objects"][1]["attributes"]["interactivity"])

        som_frame = model.calls[0]["frames"][0]
        self.assertNotEqual(som_frame.screenshot_path, self.image_path)
        self.assertTrue(som_frame.capture_method.endswith("omniparser_som"))
        self.assertTrue(som_frame.screenshot_path.exists())
        self.assertEqual(som_frame.screenshot_path.read_bytes(), SOM_PNG)
        self.assertEqual(len(som_frame.screenshot_sha256), 64)

        benchmark = result["omniparser_benchmark"]
        self.assertTrue(benchmark["parsed"])
        self.assertEqual(benchmark["element_count"], 2)
        self.assertEqual(benchmark["text_count"], 1)
        self.assertEqual(benchmark["icon_count"], 1)
        self.assertEqual(benchmark["frame_count"], 1)
        self.assertEqual(benchmark["parse_latency_s"], 1.25)
        self.assertEqual(benchmark["glm_object_count"], 1)
        self.assertEqual(benchmark["glm_link_count"], 1)
        self.assertEqual(len(benchmark["som_frames"]), 1)
        self.assertGreater(benchmark["total_ms"], 0)

    def test_transport_failure_propagates_and_model_is_not_called(self):
        transport = FakeOmniParserTransport(
            error=OmniParserTransportError("connection refused")
        )
        model = RecordingModelAdapter()
        adapter = self.make_adapter(transport, model=model)

        with self.assertRaises(OmniParserTransportError):
            adapter.recognize(
                page={"url": "https://example.invalid/"},
                frames=self.frames,
            )
        self.assertEqual(model.calls, [])

    def test_invalid_response_missing_content_list_raises(self):
        payload = _payload()
        payload.pop("parsed_content_list")
        adapter = self.make_adapter(FakeOmniParserTransport(payload=payload))

        with self.assertRaises(OmniParserResponseError):
            adapter.recognize(
                page={"url": "https://example.invalid/"},
                frames=self.frames,
            )

    def test_invalid_bbox_elements_are_skipped_and_reported(self):
        payload = _payload(
            elements=[
                {
                    "type": "text",
                    "content": "AP-001",
                    "bbox": [0.1, 0.1, 0.1, 0.2],
                },
                {
                    "type": "text",
                    "content": "CORE-001",
                    "bbox": [0.1, 0.1, 0.3, 0.3],
                },
                "not-a-mapping",
            ]
        )
        model = RecordingModelAdapter()
        adapter = self.make_adapter(FakeOmniParserTransport(payload=payload), model=model)

        result = adapter.recognize(
            page={"url": "https://example.invalid/"},
            frames=self.frames,
        )

        benchmark = result["omniparser_benchmark"]
        self.assertEqual(benchmark["element_count"], 1)
        self.assertEqual(benchmark["skipped_elements"], 2)
        observations = model.calls[0]["observations"]
        self.assertEqual(len(observations["elements"]), 1)
        self.assertEqual(observations["objects"][0]["business_id"], "CORE-001")

    def test_duplicate_content_business_ids_are_deduplicated(self):
        payload = _payload(
            elements=[
                {"type": "text", "content": "AP-001", "bbox": [0.1, 0.1, 0.2, 0.2]},
                {"type": "text", "content": "AP-001", "bbox": [0.3, 0.1, 0.4, 0.2]},
            ]
        )
        model = RecordingModelAdapter()
        adapter = self.make_adapter(FakeOmniParserTransport(payload=payload), model=model)

        adapter.recognize(
            page={"url": "https://example.invalid/"},
            frames=self.frames,
        )

        objects = model.calls[0]["observations"]["objects"]
        self.assertEqual(objects[0]["business_id"], "AP-001")
        self.assertEqual(objects[1]["business_id"], "AP-001#1")

    def test_remote_http_endpoint_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            OmniParserCanvasVisionAdapter(
                model_adapter=RecordingModelAdapter(),
                endpoint="http://192.168.1.10:8000/parse/",
                workdir=self.root,
            )

    def test_env_driver_omniparser_codeagent_builds_adapter(self):
        model_adapter = object()
        omniparser_adapter = object()
        environment = {
            "KT6_VISION_DRIVER": "omniparser",
            "KT6_HYBRID_MODEL_DRIVER": "codeagent_cli",
            "KT6_CODEAGENT_EXECUTABLE": "codeagent-test",
            "KT6_OMNIPARSER_ENDPOINT": "http://127.0.0.1:8000/parse/",
            "KT6_OMNIPARSER_TIMEOUT_SECONDS": "150",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "kt6_backend.app.CodeAgentCanvasVisionAdapter",
                return_value=model_adapter,
            ) as model_constructor,
            patch(
                "kt6_backend.app.OmniParserCanvasVisionAdapter",
                return_value=omniparser_adapter,
            ) as omniparser_constructor,
            tempfile.TemporaryDirectory() as temp_dir,
        ):
            root = Path(temp_dir).resolve()
            built = _create_canvas_vision_from_env(root)

        self.assertIs(built, omniparser_adapter)
        model_constructor.assert_called_once_with(
            workdir=root,
            executable="codeagent-test",
            agent=None,
            timeout_seconds=120.0,
        )
        omniparser_constructor.assert_called_once_with(
            model_adapter=model_adapter,
            endpoint="http://127.0.0.1:8000/parse/",
            workdir=root / "runtime_data" / "omniparser_som",
            timeout_seconds=150.0,
        )

    def test_env_driver_omniparser_requires_endpoint(self):
        with (
            patch.dict(
                os.environ,
                {
                    "KT6_VISION_DRIVER": "omniparser",
                    "KT6_HYBRID_MODEL_DRIVER": "codeagent_cli",
                    "KT6_CODEAGENT_EXECUTABLE": "codeagent-test",
                },
                clear=True,
            ),
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaisesRegex(ValueError, "KT6_OMNIPARSER_ENDPOINT"),
        ):
            _create_canvas_vision_from_env(Path(temp_dir))

    def test_env_driver_omniparser_rejects_invalid_timeout(self):
        for timeout in ("abc", "0", "99999"):
            with self.subTest(timeout=timeout), patch.dict(
                os.environ,
                {
                    "KT6_VISION_DRIVER": "omniparser",
                    "KT6_HYBRID_MODEL_DRIVER": "codeagent_cli",
                    "KT6_CODEAGENT_EXECUTABLE": "codeagent-test",
                    "KT6_OMNIPARSER_ENDPOINT": "http://127.0.0.1:8000/parse/",
                    "KT6_OMNIPARSER_TIMEOUT_SECONDS": timeout,
                },
                clear=True,
            ), tempfile.TemporaryDirectory() as temp_dir, self.assertRaises(
                ValueError
            ):
                _create_canvas_vision_from_env(Path(temp_dir))

    def test_codeagent_cv_context_carries_omniparser_source_and_elements(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = CodeAgentCanvasVisionAdapter(
                workdir=Path(temp_dir),
                executable=sys.executable,
            )
            context = adapter._cv_context(
                {
                    "source": "omniparser_structured_elements",
                    "objects": [
                        {
                            "business_id": "AP-001",
                            "label": "AP-001",
                            "bbox": [80.0, 120.0, 160.0, 120.0],
                            "confidence": 0.8,
                        }
                    ],
                    "links": [],
                    "elements": [
                        {
                            "idx": 0,
                            "type": "text",
                            "content": "AP-001",
                            "bbox": [80.0, 120.0, 160.0, 120.0],
                            "interactivity": False,
                        }
                    ],
                }
            )

        self.assertEqual(context["source"], "omniparser_structured_elements")
        self.assertEqual(context["objects"][0]["business_id"], "AP-001")
        self.assertEqual(context["elements"][0]["idx"], 0)
        self.assertEqual(context["elements"][0]["content"], "AP-001")


if __name__ == "__main__":
    unittest.main()
