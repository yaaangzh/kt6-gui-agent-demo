import base64
import json
from pathlib import Path
import struct
import tempfile
import unittest

from kt6_backend.topology_image_cli import (
    TopologyImageCLIError,
    acceptance_result,
    build_capture_payload,
    inspect_image,
    submit_capture,
)


def minimal_png(width=320, height=180):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.body))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return self.body[:limit]


class TopologyImageCLITest(unittest.TestCase):
    def test_build_payload_forces_pixels_only_capture(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "topology.png"
            raw = minimal_png()
            path.write_bytes(raw)

            payload, digest = build_capture_payload(path, "enterprise-v1")

        self.assertEqual(payload["dom"], {"elements": []})
        self.assertIsNone(payload["adapter_scene"])
        self.assertNotIn("topology_text", payload)
        self.assertEqual(len(payload["canvases"]), 1)
        canvas = payload["canvases"][0]
        self.assertEqual((canvas["width"], canvas["height"]), (320, 180))
        self.assertEqual(canvas["bbox"], [0, 0, 320, 180])
        encoded = canvas["data_url"].split(",", 1)[1]
        self.assertEqual(base64.b64decode(encoded), raw)
        self.assertEqual(len(digest), 64)

    def test_invalid_or_excessive_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.png"
            path.write_bytes(b"not an image")
            with self.assertRaisesRegex(TopologyImageCLIError, "only PNG"):
                inspect_image(path)

            path.write_bytes(minimal_png(32769, 1))
            with self.assertRaisesRegex(TopologyImageCLIError, "dimensions exceed"):
                inspect_image(path)

    def test_submit_capture_posts_to_existing_perception_endpoint(self):
        observed = {}

        def opener(request, timeout):
            observed["url"] = request.full_url
            observed["timeout"] = timeout
            observed["payload"] = json.loads(request.data)
            return FakeResponse({"capture_id": "capture_1", "scene": {}, "summary": {}})

        result = submit_capture(
            "https://kt6.example/base",
            {"page": {"url": "kt6://image-test/test"}},
            timeout_seconds=12,
            opener=opener,
        )

        self.assertEqual(observed["url"], "https://kt6.example/base/api/perception/captures")
        self.assertEqual(observed["timeout"], 12)
        self.assertEqual(observed["payload"]["page"]["url"], "kt6://image-test/test")
        self.assertEqual(result["capture_id"], "capture_1")

    def test_acceptance_requires_real_pixel_provenance_and_matching_hash(self):
        digest = "a" * 64
        response = {
            "capture_id": "capture_1",
            "summary": {"selected_mode": "canvas_vision_adapter"},
            "scene": {
                "object_count": 2,
                "relation_count": 1,
                "elements": [{"business_id": "gw_001"}, {"business_id": "core_001"}],
                "relations": [{"source": "gw_001", "target": "core_001"}],
                "provenance": {
                    "semantic_source": "canvas_pixels",
                    "pixel_inference_performed": True,
                    "pixel_verified": True,
                    "adapter_id": "production-vision",
                    "adapter_version": "1.0",
                    "screenshot_sha256": [digest],
                    "actionable_grounding": False,
                },
            },
        }

        accepted, report = acceptance_result(response, digest)
        self.assertTrue(accepted)
        self.assertTrue(report["screenshot_sha256_matches"])
        self.assertFalse(report["actionable_grounding"])

        response["summary"]["selected_mode"] = "canvas_renderer_adapter"
        accepted, _ = acceptance_result(response, digest)
        self.assertFalse(accepted)


if __name__ == "__main__":
    unittest.main()
