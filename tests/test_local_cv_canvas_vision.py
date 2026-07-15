import base64
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from kt6_backend.local_cv_canvas_vision import (
    CVTopologyEvidence,
    DetectedConnector,
    LocalCVTopologyVisionAdapter,
    LocalVisionRecognitionError,
    OCRSpan,
    RapidOCROpenCVBackend,
)
from kt6_backend.page_perception import PagePerceptionService, SQLitePageCaptureStore
from kt6_backend.perception_runtime import PerceptionRuntime
from kt6_backend.vision_recognition import CanvasFrame


def valid_png(width: int, height: int) -> bytes:
    """Build a small, standards-compliant grayscale PNG without optional packages."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return (
            struct.pack(">I", len(data))
            + payload
            + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    scanlines = (b"\x00" + (b"\xff" * width)) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class FakeLocalImageBackend:
    def __init__(self, spans=(), evidence=None):
        self.spans = tuple(spans)
        self.evidence = evidence or CVTopologyEvidence()
        self.recognize_calls = []
        self.connector_calls = []

    def recognize_text(self, frame):
        self.recognize_calls.append(frame)
        return self.spans

    def analyze_connectors(
        self,
        frame,
        *,
        spans,
        diagram_nodes,
        diagram_bottom,
    ):
        self.connector_calls.append(
            {
                "frame": frame,
                "spans": tuple(spans),
                "diagram_nodes": dict(diagram_nodes),
                "diagram_bottom": diagram_bottom,
            }
        )
        return self.evidence


class LocalCVTopologyVisionAdapterTest(unittest.TestCase):
    WIDTH = 320
    HEIGHT = 240

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.raw = valid_png(self.WIDTH, self.HEIGHT)
        self.image_path = self.root / "topology.png"
        self.image_path.write_bytes(self.raw)

    def tearDown(self):
        self.temp_dir.cleanup()

    def frame(self, **overrides) -> CanvasFrame:
        values = {
            "canvas_id": "topology-canvas",
            "screenshot_path": self.image_path,
            "screenshot_sha256": hashlib.sha256(self.raw).hexdigest(),
            "mime_type": "image/png",
            "width": self.WIDTH,
            "height": self.HEIGHT,
            "client_width": float(self.WIDTH),
            "client_height": float(self.HEIGHT),
            "bbox": (0.0, 0.0, float(self.WIDTH), float(self.HEIGHT)),
        }
        values.update(overrides)
        return CanvasFrame(**values)

    @staticmethod
    def page() -> dict:
        return {
            "url": "kt6://image-test/local-cv",
            "title": "local-cv",
            "language": "zh-CN",
            "ui_version": "topology-image-cli-v1",
            "viewport": {
                "width": 320,
                "height": 240,
                "device_pixel_ratio": 1.0,
            },
        }

    def test_single_image_recognition_deduplicates_identifiers(self):
        backend = FakeLocalImageBackend(
            spans=(
                OCRSpan("GW-001", 0.82, (20.0, 20.0, 60.0, 12.0)),
                OCRSpan("GW-001", 0.96, (100.0, 30.0, 60.0, 12.0)),
                OCRSpan("unrelated text", 0.99, (10.0, 80.0, 100.0, 12.0)),
            )
        )
        adapter = LocalCVTopologyVisionAdapter(backend=backend)

        result = adapter.recognize(page=self.page(), frames=(self.frame(),))

        self.assertIsNotNone(result)
        self.assertEqual([item["business_id"] for item in result["objects"]], ["GW-001"])
        detected = result["objects"][0]
        self.assertEqual(detected["type"], "gateway")
        self.assertEqual(detected["bbox"], [100.0, 30.0, 60.0, 12.0])
        self.assertEqual(detected["confidence"], 0.96)
        self.assertEqual(detected["attributes"]["source_region"], "diagram")
        self.assertEqual(len(backend.recognize_calls), 1)
        self.assertEqual(backend.recognize_calls[0].raw, self.raw)
        self.assertEqual(set(backend.connector_calls[0]["diagram_nodes"]), {"GW-001"})

    def test_normalizes_modern_and_legacy_rapidocr_result_shapes(self):
        backend = object.__new__(RapidOCROpenCVBackend)

        class ModernResult:
            boxes = [[[1, 2], [21, 2], [21, 12], [1, 12]]]
            txts = ("GW-001",)
            scores = (0.97,)

        modern = backend._normalize_ocr_result(ModernResult(), 100, 50)
        legacy = backend._normalize_ocr_result(
            (
                [
                    [
                        [[2, 3], [32, 3], [32, 13], [2, 13]],
                        "CORE-001",
                        0.95,
                    ]
                ],
                0.1,
            ),
            100,
            50,
        )

        self.assertEqual(modern, (OCRSpan("GW-001", 0.97, (1.0, 2.0, 20.0, 10.0)),))
        self.assertEqual(legacy, (OCRSpan("CORE-001", 0.95, (2.0, 3.0, 30.0, 10.0)),))

    def test_rejects_malformed_rapidocr_output(self):
        backend = object.__new__(RapidOCROpenCVBackend)

        class BadResult:
            boxes = [[[1, 2], [21, 2], [21, 12], [1, 12]]]
            txts = ("GW-001", "unexpected")
            scores = (0.97,)

        with self.assertRaisesRegex(LocalVisionRecognitionError, "different lengths"):
            backend._normalize_ocr_result(BadResult(), 100, 50)

    def test_merges_diagram_connectors_and_device_detail_downstream_relations(self):
        spans = (
            OCRSpan("GW-001", 0.97, (25.0, 15.0, 55.0, 12.0)),
            OCRSpan("ACC-010", 0.95, (85.0, 65.0, 65.0, 12.0)),
            OCRSpan("设备详情", 0.99, (5.0, 110.0, 70.0, 12.0)),
            OCRSpan("设备", 0.99, (10.0, 125.0, 40.0, 12.0)),
            OCRSpan("下方AP", 0.99, (210.0, 125.0, 70.0, 12.0)),
            OCRSpan("ACC-010", 0.93, (20.0, 155.0, 65.0, 12.0)),
            OCRSpan("AP-022(LSW)", 0.91, (225.0, 155.0, 90.0, 12.0)),
            OCRSpan("特殊标记设备", 0.99, (5.0, 210.0, 100.0, 12.0)),
        )
        backend = FakeLocalImageBackend(
            spans=spans,
            evidence=CVTopologyEvidence(
                node_boxes={
                    "GW-001": (10.0, 8.0, 90.0, 28.0),
                    "ACC-010": (70.0, 55.0, 100.0, 28.0),
                },
                connectors=(DetectedConnector("GW-001", "ACC-010", 0.88),),
            ),
        )
        adapter = LocalCVTopologyVisionAdapter(backend=backend)

        result = adapter.recognize(page=self.page(), frames=(self.frame(),))

        self.assertEqual(
            [item["business_id"] for item in result["objects"]],
            ["ACC-010", "AP-022", "GW-001"],
        )
        relations = {(item["source"], item["target"]): item for item in result["links"]}
        self.assertEqual(
            relations[("GW-001", "ACC-010")]["attributes"]["evidence"],
            "orthogonal_pixel_connector",
        )
        self.assertEqual(
            relations[("ACC-010", "AP-022")]["type"],
            "downstream_membership",
        )
        self.assertEqual(
            relations[("ACC-010", "AP-022")]["attributes"],
            {"evidence": "device_detail_table", "directness": "unknown"},
        )
        objects = {item["business_id"]: item for item in result["objects"]}
        self.assertEqual(objects["ACC-010"]["attributes"]["source_region"], "diagram")
        self.assertIn("AP-022(LSW)", objects["ACC-010"]["attributes"]["device_detail_row"])
        call = backend.connector_calls[0]
        self.assertEqual(set(call["diagram_nodes"]), {"GW-001", "ACC-010"})
        self.assertEqual(call["diagram_bottom"], 110.0)

    def test_preserves_parallel_pixel_and_table_relations_for_same_devices(self):
        spans = (
            OCRSpan("ACC-010", 0.97, (40.0, 15.0, 65.0, 12.0)),
            OCRSpan("AP-022", 0.96, (40.0, 60.0, 55.0, 12.0)),
            OCRSpan("设备详情", 0.99, (5.0, 105.0, 70.0, 12.0)),
            OCRSpan("设备", 0.99, (10.0, 125.0, 40.0, 12.0)),
            OCRSpan("下方AP", 0.99, (210.0, 125.0, 70.0, 12.0)),
            OCRSpan("ACC-010", 0.95, (20.0, 155.0, 65.0, 12.0)),
            OCRSpan("AP-022", 0.94, (225.0, 155.0, 55.0, 12.0)),
            OCRSpan("特殊标记设备", 0.99, (5.0, 210.0, 100.0, 12.0)),
        )
        backend = FakeLocalImageBackend(
            spans=spans,
            evidence=CVTopologyEvidence(
                connectors=(DetectedConnector("ACC-010", "AP-022", 0.9),),
            ),
        )
        adapter = LocalCVTopologyVisionAdapter(backend=backend)

        result = adapter.recognize(page=self.page(), frames=(self.frame(),))

        parallel = [
            item
            for item in result["links"]
            if item["source"] == "ACC-010" and item["target"] == "AP-022"
        ]
        self.assertEqual(
            {item["type"] for item in parallel},
            {"topology_link", "downstream_membership"},
        )

    def test_corrects_ocr_o_i_l_only_in_numeric_identifier_suffix(self):
        backend = FakeLocalImageBackend(
            spans=(OCRSpan("AP-OIL", 0.90, (40.0, 20.0, 60.0, 12.0)),)
        )
        adapter = LocalCVTopologyVisionAdapter(backend=backend)

        result = adapter.recognize(page=self.page(), frames=(self.frame(),))

        detected = result["objects"][0]
        self.assertEqual(detected["business_id"], "AP-011")
        self.assertEqual(detected["confidence"], 0.82)
        self.assertTrue(detected["attributes"]["ocr_identifier_corrected"])
        self.assertEqual(detected["attributes"]["ocr_text"], "AP-OIL")

    def test_filters_spans_below_the_configured_ocr_confidence(self):
        backend = FakeLocalImageBackend(
            spans=(
                OCRSpan("GW-001", 0.6499, (20.0, 20.0, 60.0, 12.0)),
                OCRSpan("CORE-001", 0.65, (20.0, 60.0, 80.0, 12.0)),
            )
        )
        adapter = LocalCVTopologyVisionAdapter(backend=backend)

        result = adapter.recognize(page=self.page(), frames=(self.frame(),))

        self.assertEqual(
            [item["business_id"] for item in result["objects"]],
            ["CORE-001"],
        )

    def test_returns_none_when_ocr_finds_no_accepted_identifier(self):
        backend = FakeLocalImageBackend(
            spans=(
                OCRSpan("设备详情", 0.99, (10.0, 20.0, 70.0, 12.0)),
                OCRSpan("GW-001", 0.20, (10.0, 50.0, 60.0, 12.0)),
            )
        )
        adapter = LocalCVTopologyVisionAdapter(backend=backend)

        result = adapter.recognize(page=self.page(), frames=(self.frame(),))

        self.assertIsNone(result)
        self.assertEqual(len(backend.recognize_calls), 1)
        self.assertEqual(backend.connector_calls, [])

    def test_rejects_multiple_frames_before_local_inference(self):
        second_path = self.root / "second.png"
        second_path.write_bytes(self.raw)
        second = self.frame(
            canvas_id="second-canvas",
            screenshot_path=second_path,
        )
        backend = FakeLocalImageBackend()
        adapter = LocalCVTopologyVisionAdapter(backend=backend)

        with self.assertRaisesRegex(LocalVisionRecognitionError, "exactly one"):
            adapter.recognize(page=self.page(), frames=(self.frame(), second))

        self.assertEqual(backend.recognize_calls, [])

    def test_rejects_image_over_local_pixel_limit_before_local_inference(self):
        backend = FakeLocalImageBackend()
        adapter = LocalCVTopologyVisionAdapter(
            backend=backend,
            max_image_pixels=(self.WIDTH * self.HEIGHT) - 1,
        )

        with self.assertRaisesRegex(LocalVisionRecognitionError, "pixel limit"):
            adapter.recognize(page=self.page(), frames=(self.frame(),))

        self.assertEqual(backend.recognize_calls, [])

    def test_rejects_sha_mismatch_before_local_inference(self):
        backend = FakeLocalImageBackend()
        adapter = LocalCVTopologyVisionAdapter(backend=backend)

        with self.assertRaisesRegex(ValueError, "does not match screenshot_sha256"):
            adapter.recognize(
                page=self.page(),
                frames=(self.frame(screenshot_sha256="0" * 64),),
            )

        self.assertEqual(backend.recognize_calls, [])

    def test_page_perception_stamps_local_provenance_tree_and_analysis_only_binding(self):
        backend = FakeLocalImageBackend(
            spans=(
                OCRSpan("GW-001", 0.97, (100.0, 20.0, 60.0, 12.0)),
                OCRSpan("CORE-001", 0.95, (90.0, 80.0, 80.0, 12.0)),
            ),
            evidence=CVTopologyEvidence(
                connectors=(DetectedConnector("GW-001", "CORE-001", 0.92),)
            ),
        )
        adapter = LocalCVTopologyVisionAdapter(backend=backend)
        store = SQLitePageCaptureStore(
            self.root / "captures.sqlite3",
            self.root / "page_captures",
        )
        service = PagePerceptionService(
            store,
            PerceptionRuntime(),
            canvas_vision=adapter,
        )
        payload = {
            "page": self.page(),
            "dom": {"elements": []},
            "canvases": [
                {
                    "canvas_id": "topology-canvas",
                    "width": self.WIDTH,
                    "height": self.HEIGHT,
                    "client_width": self.WIDTH,
                    "client_height": self.HEIGHT,
                    "bbox": [0, 0, self.WIDTH, self.HEIGHT],
                    "data_url": "data:image/png;base64,"
                    + base64.b64encode(self.raw).decode("ascii"),
                }
            ],
            "adapter_scene": None,
        }

        capture = service.ingest(payload)

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_vision_adapter")
        self.assertEqual(capture["summary"]["semantic_source"], "canvas_pixels")
        scene = capture["scene"]
        self.assertEqual(scene["object_count"], 2)
        self.assertTrue(scene["pixel_inference_performed"])
        self.assertTrue(scene["pixel_verified"])
        self.assertFalse(scene["actionable_grounding"])
        self.assertEqual(scene["provenance"]["adapter_id"], "local-cv-ocr")
        self.assertEqual(scene["provenance"]["adapter_version"], "1.0")
        self.assertFalse(
            scene["provenance"]["adapter_supports_actionable_grounding"]
        )
        self.assertEqual(scene["semantic_tree"]["roots"], ["GW-001"])
        self.assertEqual(
            scene["semantic_tree"]["nodes"]["GW-001"]["children"],
            [
                {
                    "target": "CORE-001",
                    "relation_id": "local-line:GW-001:CORE-001",
                    "type": "topology_link",
                }
            ],
        )
        self.assertTrue(
            all(
                binding["actionable"] is False
                for binding in scene["business_object_bindings"].values()
            )
        )


if __name__ == "__main__":
    unittest.main()
