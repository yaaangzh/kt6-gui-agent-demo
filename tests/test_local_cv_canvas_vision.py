import base64
import hashlib
import importlib.util
import math
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from kt6_backend.local_cv_canvas_vision import (
    CVTopologyEvidence,
    DetectedConnector,
    DeviceOccurrence,
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
            ),
            evidence=CVTopologyEvidence(
                pass_through_nodes=frozenset({"GW-001"}),
            ),
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
        self.assertEqual(
            detected["attributes"]["pixel_role"],
            "pass_through_ocr_candidate",
        )
        self.assertEqual(
            detected["attributes"]["relation_excluded_reason"],
            "uncertain_text_over_continuous_connector",
        )
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
            relations[("GW-001", "ACC-010")]["attributes"]["direction"],
            "undirected",
        )
        self.assertFalse(
            relations[("GW-001", "ACC-010")]["attributes"]["directed"],
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

    def test_extended_identifier_support_does_not_regress_legacy_families(self):
        legacy_ids = [
            "CORE-001",
            "AGG-002",
            "ACC-003",
            "LSW-004",
            "ONU-005",
            "RTR-006",
            "GW-007",
            "AP-008",
            "AC-009",
            "FW-010",
            "SW-011",
        ]
        text = " ".join(legacy_ids)
        adapter = LocalCVTopologyVisionAdapter(backend=FakeLocalImageBackend())

        occurrences = adapter._device_occurrences(
            (OCRSpan(text, 0.98, (10.0, 20.0, 660.0, 16.0)),),
            frame_width=800,
            frame_height=100,
        )

        self.assertEqual(
            [occurrence.business_id for occurrence in occurrences],
            legacy_ids,
        )
        self.assertTrue(all(not occurrence.corrected_ocr for occurrence in occurrences))

    def test_parses_generic_families_and_limits_ocr_correction_to_numeric_suffixes(self):
        spans = (
            OCRSpan("testNE49OIL", 0.98, (10.0, 10.0, 120.0, 16.0)),
            OCRSpan("CommonSubnet9O2", 0.98, (10.0, 35.0, 160.0, 16.0)),
            OCRSpan("SUBNETA_32I4O57", 0.98, (10.0, 60.0, 170.0, 16.0)),
            OCRSpan("Subnet_MI1jeVtRT6", 0.98, (10.0, 85.0, 180.0, 16.0)),
            OCRSpan("Name_nzSWSI5996", 0.98, (10.0, 110.0, 170.0, 16.0)),
            OCRSpan("V2SN_NQ008Rgcjj", 0.98, (10.0, 135.0, 170.0, 16.0)),
            OCRSpan("OSS", 0.98, (10.0, 160.0, 40.0, 16.0)),
            OCRSpan("CameraRoot", 0.98, (10.0, 185.0, 100.0, 16.0)),
        )
        adapter = LocalCVTopologyVisionAdapter(backend=FakeLocalImageBackend())

        occurrences = adapter._device_occurrences(
            spans,
            frame_width=400,
            frame_height=240,
        )
        by_id = {occurrence.business_id: occurrence for occurrence in occurrences}

        self.assertEqual(
            set(by_id),
            {
                "testNE49011",
                "CommonSubnet902",
                "SUBNETA_3214057",
                "Subnet_MI1jeVtRT6",
                "Name_nzSWSI5996",
                "V2SN_NQ008Rgcjj",
                "OSS",
                "CameraRoot",
            },
        )
        for business_id in (
            "testNE49011",
            "CommonSubnet902",
            "SUBNETA_3214057",
        ):
            self.assertTrue(by_id[business_id].corrected_ocr)
            self.assertEqual(by_id[business_id].confidence, 0.9)
        for business_id in (
            "Subnet_MI1jeVtRT6",
            "Name_nzSWSI5996",
            "V2SN_NQ008Rgcjj",
            "OSS",
            "CameraRoot",
        ):
            self.assertFalse(by_id[business_id].corrected_ocr)
            self.assertEqual(by_id[business_id].confidence, 0.98)

    def test_recognizes_all_34_attachment_physical_topology_ids_exactly(self):
        expected_ids = [
            "OSS",
            "CameraRoot",
            "CommonSubnet46272",
            "CommonSubnet982",
            "CommonSubnet824",
            "CommonSubnet246",
            "CommonSubnet584",
            "CommonSubnet592",
            "CommonSubnet734",
            "Subnet_MI1jeVtRT6",
            "Subnet_BEtHLXeVGd",
            "Subnet_KvVkJB8iGx",
            "Subnet_37zwLIPGoC",
            "Subnet_QyoY7I28kQ",
            "Subnet_KnDdwQUX",
            "Subnet_kX0ZbRDM",
            "Subnet_4RENSFSNC",
            "Subnet_L30C6xMXE",
            "Subnet_BRcO3M1d",
            "Name_nzSWSI5996",
            "SUBNETA_3246857",
            "V2SN_7MJXScBKdK",
            "V2SN_7MJXScBKdK1",
            "V2SN_7MJXScBKdK2",
            "V2SN_7MJXScBKdK3",
            "V2SN_7MJXScBKdK4",
            "V2SN_7MJXScBKdK5",
            "V2SN_7MJXScBKdK6",
            "V2SN_7MJXScBKdK7",
            "V2SN_7MJXScBKdK8",
            "V2SN_7MJXScBKdK9",
            "V2SN_s4g6B3QE6C",
            "V2SN_NQ008Rgcjj",
            "V2SN_PC7SIV2KIR",
        ]
        spans = tuple(
            OCRSpan(
                business_id,
                0.99,
                (10.0, 10.0 + index * 24.0, 260.0, 16.0),
            )
            for index, business_id in enumerate(expected_ids)
        )
        adapter = LocalCVTopologyVisionAdapter(backend=FakeLocalImageBackend())

        occurrences = adapter._device_occurrences(
            spans,
            frame_width=400,
            frame_height=900,
        )
        actual_ids = [occurrence.business_id for occurrence in occurrences]

        self.assertEqual(len(actual_ids), 34)
        self.assertEqual(set(actual_ids), set(expected_ids))

    def test_joins_known_identifier_prefix_and_suffix_ocr_spans(self):
        spans = (
            OCRSpan("testNE", 0.97, (10.0, 20.0, 58.0, 16.0)),
            OCRSpan("49932", 0.96, (72.0, 20.0, 52.0, 16.0)),
            OCRSpan("V2SN_", 0.98, (10.0, 55.0, 60.0, 16.0)),
            OCRSpan("NQ008Rgcjj", 0.97, (74.0, 55.0, 100.0, 16.0)),
        )
        adapter = LocalCVTopologyVisionAdapter(backend=FakeLocalImageBackend())

        occurrences = adapter._device_occurrences(
            spans,
            frame_width=240,
            frame_height=140,
        )

        self.assertEqual(
            {occurrence.business_id for occurrence in occurrences},
            {"testNE49932", "V2SN_NQ008Rgcjj"},
        )

    def test_does_not_promote_common_ui_text_to_opaque_topology_ids(self):
        spans = (
            OCRSpan("Device Name admin", 0.99, (10.0, 10.0, 180.0, 16.0)),
            OCRSpan("Subnet status", 0.99, (10.0, 35.0, 140.0, 16.0)),
            OCRSpan("Camera Root Cause", 0.99, (10.0, 60.0, 180.0, 16.0)),
            OCRSpan("Subnet Name network", 0.99, (10.0, 85.0, 200.0, 16.0)),
        )
        adapter = LocalCVTopologyVisionAdapter(backend=FakeLocalImageBackend())

        occurrences = adapter._device_occurrences(
            spans,
            frame_width=260,
            frame_height=140,
        )

        self.assertEqual(occurrences, ())

    def test_emits_observed_line_metadata_and_weight_as_undirected_relation(self):
        backend = FakeLocalImageBackend(
            spans=(
                OCRSpan("testNE79324", 0.98, (20.0, 20.0, 100.0, 14.0)),
                OCRSpan("testNE79323", 0.97, (180.0, 100.0, 100.0, 14.0)),
            ),
            evidence=CVTopologyEvidence(
                connectors=(
                    DetectedConnector(
                        "testNE79324",
                        "testNE79323",
                        0.91,
                        evidence="multi_angle_pixel_connector",
                        line_style="dashed",
                        line_color="cyan",
                        weight=1.845,
                    ),
                ),
            ),
        )
        adapter = LocalCVTopologyVisionAdapter(backend=backend)

        result = adapter.recognize(page=self.page(), frames=(self.frame(),))

        self.assertEqual(
            result["links"][0]["attributes"],
            {
                "evidence": "multi_angle_pixel_connector",
                "direction": "undirected",
                "directed": False,
                "line_style": "dashed",
                "line_color": "cyan",
                "weight": 1.845,
            },
        )

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
        self.assertEqual(scene["provenance"]["adapter_version"], "1.3")
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


@unittest.skipUnless(
    importlib.util.find_spec("cv2") is not None
    and importlib.util.find_spec("numpy") is not None,
    "synthetic connector tests require OpenCV and NumPy",
)
class RapidOCROpenCVConnectorSyntheticTest(unittest.TestCase):
    """Exercise the real pixel connector backend with hand-authored OCR boxes."""

    @classmethod
    def setUpClass(cls):
        import cv2
        import numpy as np

        cls.cv2 = cv2
        cls.np = np
        cls.backend = object.__new__(RapidOCROpenCVBackend)
        cls.backend._cv2 = cv2
        cls.backend._np = np

    def _frame(self, image):
        encoded, payload = self.cv2.imencode(".png", image)
        self.assertTrue(encoded)
        height, width = image.shape[:2]
        return SimpleNamespace(
            raw=payload.tobytes(),
            width=width,
            height=height,
        )

    @staticmethod
    def _nodes(boxes):
        spans = []
        nodes = {}
        for index, (business_id, bbox) in enumerate(boxes.items()):
            spans.append(OCRSpan(business_id, 0.99, bbox))
            nodes[business_id] = DeviceOccurrence(
                business_id=business_id,
                prefix=business_id.split("-", 1)[0],
                confidence=0.99,
                bbox=bbox,
                raw_text=business_id,
                span_index=index,
            )
        return tuple(spans), nodes

    @staticmethod
    def _boundary_points(source_box, target_box):
        def center(box):
            x, y, width, height = box
            return x + width / 2, y + height / 2

        source_x, source_y = center(source_box)
        target_x, target_y = center(target_box)
        delta_x = target_x - source_x
        delta_y = target_y - source_y

        def boundary_scale(box):
            _x, _y, width, height = box
            candidates = []
            if delta_x:
                candidates.append((width / 2) / abs(delta_x))
            if delta_y:
                candidates.append((height / 2) / abs(delta_y))
            return min(candidates)

        source_scale = boundary_scale(source_box)
        target_scale = boundary_scale(target_box)
        return (
            (
                round(source_x + delta_x * source_scale),
                round(source_y + delta_y * source_scale),
            ),
            (
                round(target_x - delta_x * target_scale),
                round(target_y - delta_y * target_scale),
            ),
        )

    def _solid_edge(self, image, source_box, target_box, color, thickness=4):
        start, end = self._boundary_points(source_box, target_box)
        self.cv2.line(image, start, end, color, thickness, self.cv2.LINE_AA)

    def _dashed_edge(
        self,
        image,
        source_box,
        target_box,
        color,
        *,
        thickness=4,
        dash=14,
        gap=8,
    ):
        start, end = self._boundary_points(source_box, target_box)
        delta_x = end[0] - start[0]
        delta_y = end[1] - start[1]
        length = math.hypot(delta_x, delta_y)
        offset = 0.0
        while offset < length:
            segment_end = min(length, offset + dash)
            first = (
                round(start[0] + delta_x * offset / length),
                round(start[1] + delta_y * offset / length),
            )
            second = (
                round(start[0] + delta_x * segment_end / length),
                round(start[1] + delta_y * segment_end / length),
            )
            self.cv2.line(image, first, second, color, thickness, self.cv2.LINE_AA)
            offset += dash + gap

    def _detect(self, image, boxes):
        evidence = self._evidence(image, boxes)
        return {(item.source, item.target) for item in evidence.connectors}

    def _evidence(self, image, boxes, extra_spans=()):
        spans, nodes = self._nodes(boxes)
        return self.backend.analyze_connectors(
            self._frame(image),
            spans=spans + tuple(extra_spans),
            diagram_nodes=nodes,
            diagram_bottom=float(image.shape[0]),
        )

    def test_ne_star_detects_exact_arbitrary_angle_edges_without_peripheral_links(self):
        image = self.np.full((620, 1040, 3), 255, dtype=self.np.uint8)
        boxes = {
            "testNE49932": (485.0, 35.0, 70.0, 24.0),
            "testNE4994": (35.0, 230.0, 70.0, 24.0),
            "testNE4995": (150.0, 315.0, 70.0, 24.0),
            "testNE49911": (270.0, 405.0, 70.0, 24.0),
            "testNE49925": (390.0, 510.0, 70.0, 24.0),
            "testNE49913": (580.0, 510.0, 70.0, 24.0),
            "testNE49944": (700.0, 405.0, 70.0, 24.0),
            "testNE49938": (820.0, 315.0, 70.0, 24.0),
            "testNE49943": (935.0, 230.0, 70.0, 24.0),
        }
        for target in boxes:
            if target == "testNE49932":
                continue
            self._solid_edge(
                image,
                boxes["testNE49932"],
                boxes[target],
                (20, 20, 20),
            )

        detected = {
            frozenset(edge)
            for edge in self._detect(image, boxes)
        }
        expected = {
            frozenset(("testNE49932", target))
            for target in boxes
            if target != "testNE49932"
        }

        self.assertEqual(detected, expected)

    def test_trunk_junction_projects_unique_core_to_all_access_leaves(self):
        image = self.np.full((500, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (315.0, 30.0, 70.0, 24.0),
            "ACC-001": (80.0, 400.0, 70.0, 24.0),
            "ACC-002": (315.0, 400.0, 70.0, 24.0),
            "ACC-003": (550.0, 400.0, 70.0, 24.0),
        }
        self.cv2.line(image, (350, 54), (350, 220), (20, 20, 20), 4)
        self.cv2.line(image, (115, 220), (585, 220), (20, 20, 20), 4)
        self.cv2.line(image, (115, 220), (115, 400), (20, 20, 20), 4)
        self.cv2.line(image, (350, 220), (350, 400), (20, 20, 20), 4)
        self.cv2.line(image, (585, 220), (585, 400), (20, 20, 20), 4)

        detected = self._detect(image, boxes)

        self.assertEqual(
            detected,
            {
                ("CORE-001", "ACC-001"),
                ("CORE-001", "ACC-002"),
                ("CORE-001", "ACC-003"),
            },
        )

    def test_legacy_layered_fallback_recovers_gapped_production_trunk(self):
        image = self.np.full((500, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (315.0, 30.0, 70.0, 24.0),
            "ACC-001": (80.0, 400.0, 70.0, 24.0),
            "ACC-002": (315.0, 400.0, 70.0, 24.0),
            "ACC-003": (550.0, 400.0, 70.0, 24.0),
        }
        # The visible line ends are 24px away from the OCR boxes.  Precise
        # anchor/corridor gates reject this rendering, while the pre-v1.2
        # padded connected-component path recovered the full trunk.
        self.cv2.line(image, (350, 78), (350, 220), (20, 20, 20), 4)
        self.cv2.line(image, (115, 220), (585, 220), (20, 20, 20), 4)
        for x_position in (115, 350, 585):
            self.cv2.line(
                image,
                (x_position, 220),
                (x_position, 376),
                (20, 20, 20),
                4,
            )

        evidence = self._evidence(image, boxes)

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {
                frozenset(("CORE-001", "ACC-001")),
                frozenset(("CORE-001", "ACC-002")),
                frozenset(("CORE-001", "ACC-003")),
            },
        )
        self.assertEqual(
            {connector.evidence for connector in evidence.connectors},
            {
                "directional_probe_component",
                "legacy_padded_hough_segment",
            },
        )

    def test_directional_probe_recovers_large_node_to_fanout_gaps(self):
        image = self.np.full((430, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "ACC-001": (315.0, 30.0, 70.0, 24.0),
            "AP-001": (100.0, 340.0, 70.0, 24.0),
            "AP-002": (530.0, 340.0, 70.0, 24.0),
        }
        # Each visible connector stops roughly 60px before its OCR label.
        # The old rectangular halo cannot associate either side, while a
        # directionally verified vertical probe can reach the real component.
        self.cv2.line(image, (350, 110), (350, 150), (20, 20, 20), 4)
        self.cv2.line(image, (135, 150), (565, 150), (20, 20, 20), 4)
        self.cv2.line(image, (135, 150), (135, 280), (20, 20, 20), 4)
        self.cv2.line(image, (565, 150), (565, 280), (20, 20, 20), 4)

        evidence = self._evidence(image, boxes)

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {
                frozenset(("ACC-001", "AP-001")),
                frozenset(("ACC-001", "AP-002")),
            },
        )
        self.assertEqual(
            {connector.evidence for connector in evidence.connectors},
            {"directional_probe_component"},
        )

    def test_directional_probe_recovers_a_wide_production_fanout(self):
        image = self.np.full((880, 2003, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (684.0, 300.0, 84.0, 30.0),
            "ACC-010": (178.0, 708.0, 72.0, 25.0),
            "ACC-022": (652.0, 708.0, 72.0, 25.0),
            "ACC-006": (890.0, 708.0, 73.0, 25.0),
            "ACC-012": (1207.0, 708.0, 73.0, 25.0),
            "ACC-015": (1603.0, 708.0, 72.0, 25.0),
        }
        centers = (214, 688, 926, 1243, 1639)
        self.cv2.line(image, (726, 390), (726, 500), (20, 20, 20), 4)
        self.cv2.line(image, (214, 500), (1639, 500), (20, 20, 20), 4)
        for center_x in centers:
            self.cv2.line(
                image,
                (center_x, 500),
                (center_x, 640),
                (20, 20, 20),
                4,
            )

        evidence = self._evidence(image, boxes)

        self.assertEqual(
            {
                (connector.source, connector.target)
                for connector in evidence.connectors
            },
            {
                ("CORE-001", "ACC-010"),
                ("CORE-001", "ACC-022"),
                ("CORE-001", "ACC-006"),
                ("CORE-001", "ACC-012"),
                ("CORE-001", "ACC-015"),
            },
        )

    def test_directional_probe_recovers_three_layers_without_shortcuts(self):
        image = self.np.full((810, 900, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (415.0, 30.0, 70.0, 24.0),
            "ACC-001": (225.0, 360.0, 70.0, 24.0),
            "ACC-002": (605.0, 360.0, 70.0, 24.0),
            "AP-001": (65.0, 740.0, 70.0, 24.0),
            "AP-002": (285.0, 740.0, 70.0, 24.0),
            "AP-003": (545.0, 740.0, 70.0, 24.0),
            "AP-004": (765.0, 740.0, 70.0, 24.0),
        }
        self.cv2.line(image, (450, 110), (450, 200), (20, 20, 20), 4)
        self.cv2.line(image, (260, 200), (640, 200), (20, 20, 20), 4)
        self.cv2.line(image, (260, 200), (260, 300), (20, 20, 20), 4)
        self.cv2.line(image, (640, 200), (640, 300), (20, 20, 20), 4)
        for root_x, bus_left, bus_right, leaves in (
            (260, 100, 320, (100, 320)),
            (640, 580, 800, (580, 800)),
        ):
            self.cv2.line(image, (root_x, 440), (root_x, 520), (20, 20, 20), 4)
            self.cv2.line(
                image,
                (bus_left, 520),
                (bus_right, 520),
                (20, 20, 20),
                4,
            )
            for leaf_x in leaves:
                self.cv2.line(
                    image,
                    (leaf_x, 520),
                    (leaf_x, 680),
                    (20, 20, 20),
                    4,
                )

        evidence = self._evidence(image, boxes)

        self.assertEqual(
            {(connector.source, connector.target) for connector in evidence.connectors},
            {
                ("CORE-001", "ACC-001"),
                ("CORE-001", "ACC-002"),
                ("ACC-001", "AP-001"),
                ("ACC-001", "AP-002"),
                ("ACC-002", "AP-003"),
                ("ACC-002", "AP-004"),
            },
        )
        self.assertEqual(
            {connector.evidence for connector in evidence.connectors},
            {"directional_probe_component"},
        )

    def test_directional_probe_completes_already_connected_internal_nodes(self):
        image = self.np.full((860, 1100, 3), 255, dtype=self.np.uint8)
        boxes = {
            "GW-001": (515.0, 20.0, 70.0, 24.0),
            "CORE-001": (515.0, 180.0, 70.0, 24.0),
            "ACC-001": (515.0, 500.0, 70.0, 24.0),
            "AP-001": (515.0, 760.0, 70.0, 24.0),
            **{
                f"AP-{index:03d}": (x, 620.0, 70.0, 24.0)
                for index, x in zip(
                    range(2, 9),
                    (20.0, 120.0, 220.0, 320.0, 700.0, 800.0, 900.0),
                )
            },
        }
        # These two exact paths make CORE and ACC members of the primary
        # connected-node set before fallback runs.
        self.cv2.line(image, (550, 44), (550, 180), (20, 20, 20), 4)
        self.cv2.line(image, (550, 524), (550, 760), (20, 20, 20), 4)
        # The missing middle edge has source pixels but leaves an OCR-sized
        # blank interval at both endpoints.  Completing topology must not be
        # limited to edges where one endpoint is otherwise an orphan.
        self.cv2.line(image, (550, 260), (550, 440), (20, 20, 20), 4)

        evidence = self._evidence(image, boxes)

        self.assertEqual(
            {
                (connector.source, connector.target)
                for connector in evidence.connectors
            },
            {
                ("GW-001", "CORE-001"),
                ("CORE-001", "ACC-001"),
                ("ACC-001", "AP-001"),
            },
        )
        self.assertEqual(
            next(
                connector.evidence
                for connector in evidence.connectors
                if connector.source == "CORE-001"
                and connector.target == "ACC-001"
            ),
            "directional_probe_component",
        )

    def test_directional_probe_runs_at_five_edges_in_a_21_node_scene(self):
        image = self.np.full((900, 1800, 3), 255, dtype=self.np.uint8)
        boxes = {}
        for index, center_x in enumerate((550.0, 100.0, 900.0, 1300.0), 1):
            boxes[f"GW-{index:03d}"] = (center_x - 35.0, 20.0, 70.0, 24.0)
            boxes[f"CORE-{index:03d}"] = (
                center_x - 35.0,
                180.0,
                70.0,
                24.0,
            )
        boxes["ACC-001"] = (515.0, 500.0, 70.0, 24.0)
        boxes["AP-001"] = (515.0, 800.0, 70.0, 24.0)
        for index, x in enumerate(
            (20, 140, 260, 380, 700, 820, 940, 1060, 1180, 1450, 1600),
            101,
        ):
            boxes[f"AP-{index:03d}"] = (float(x), 800.0, 70.0, 24.0)

        for center_x in (550, 100, 900, 1300):
            self.cv2.line(
                image,
                (center_x, 44),
                (center_x, 180),
                (20, 20, 20),
                4,
            )
        self.cv2.line(image, (550, 524), (550, 800), (20, 20, 20), 4)
        self.cv2.line(image, (550, 260), (550, 440), (20, 20, 20), 4)

        with patch.object(
            self.backend,
            "_legacy_layered_component_pairs",
            side_effect=AssertionError("wide legacy projection must stay disabled"),
        ):
            evidence = self._evidence(image, boxes)

        self.assertEqual(len(evidence.connectors), 6)
        self.assertIn(
            ("CORE-001", "ACC-001", "directional_probe_component"),
            {
                (connector.source, connector.target, connector.evidence)
                for connector in evidence.connectors
            },
        )

    def test_legacy_padded_hough_fallback_recovers_gapped_diagonal_star(self):
        image = self.np.full((580, 820, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (375.0, 278.0, 70.0, 24.0),
            "ACC-001": (40.0, 40.0, 70.0, 24.0),
            "ACC-002": (710.0, 40.0, 70.0, 24.0),
            "ACC-003": (40.0, 510.0, 70.0, 24.0),
            "ACC-004": (710.0, 510.0, 70.0, 24.0),
        }
        center_box = boxes["CORE-001"]
        for business_id, target_box in boxes.items():
            if business_id == "CORE-001":
                continue
            start, end = self._boundary_points(center_box, target_box)
            delta_x = end[0] - start[0]
            delta_y = end[1] - start[1]
            length = math.hypot(delta_x, delta_y)
            gap = 28.0
            gapped_start = (
                round(start[0] + delta_x * gap / length),
                round(start[1] + delta_y * gap / length),
            )
            gapped_end = (
                round(end[0] - delta_x * gap / length),
                round(end[1] - delta_y * gap / length),
            )
            self.cv2.line(
                image,
                gapped_start,
                gapped_end,
                (20, 20, 20),
                4,
                self.cv2.LINE_AA,
            )

        evidence = self._evidence(image, boxes)

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {
                frozenset(("CORE-001", "ACC-001")),
                frozenset(("CORE-001", "ACC-002")),
                frozenset(("CORE-001", "ACC-003")),
                frozenset(("CORE-001", "ACC-004")),
            },
        )
        self.assertEqual(
            {connector.evidence for connector in evidence.connectors},
            {"legacy_padded_hough_segment"},
        )

    def test_legacy_fallback_is_not_called_when_precise_coverage_is_sufficient(self):
        image = self.np.full((500, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (315.0, 30.0, 70.0, 24.0),
            "ACC-001": (80.0, 400.0, 70.0, 24.0),
            "ACC-002": (315.0, 400.0, 70.0, 24.0),
            "ACC-003": (550.0, 400.0, 70.0, 24.0),
        }
        self.cv2.line(image, (350, 54), (350, 220), (20, 20, 20), 4)
        self.cv2.line(image, (115, 220), (585, 220), (20, 20, 20), 4)
        self.cv2.line(image, (115, 220), (115, 400), (20, 20, 20), 4)
        self.cv2.line(image, (350, 220), (350, 400), (20, 20, 20), 4)
        self.cv2.line(image, (585, 220), (585, 400), (20, 20, 20), 4)

        with patch.object(
            self.backend,
            "_legacy_layered_component_pairs",
            side_effect=AssertionError("legacy fallback must stay disabled"),
        ):
            detected = self._detect(image, boxes)

        self.assertEqual(
            detected,
            {
                ("CORE-001", "ACC-001"),
                ("CORE-001", "ACC-002"),
                ("CORE-001", "ACC-003"),
            },
        )

    def test_legacy_fallback_stays_off_for_a_small_partial_topology(self):
        image = self.np.full((260, 760, 3), 255, dtype=self.np.uint8)
        boxes = {
            "ACC-001": (40.0, 100.0, 70.0, 24.0),
            "ACC-002": (240.0, 100.0, 70.0, 24.0),
            "ACC-003": (440.0, 100.0, 70.0, 24.0),
            "ACC-004": (640.0, 100.0, 70.0, 24.0),
        }
        self._solid_edge(
            image,
            boxes["ACC-001"],
            boxes["ACC-002"],
            (20, 20, 20),
            thickness=3,
        )

        with patch.object(
            self.backend,
            "_legacy_layered_component_pairs",
            side_effect=AssertionError("legacy fallback must stay disabled"),
        ):
            evidence = self._evidence(image, boxes)

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {frozenset(("ACC-001", "ACC-002"))},
        )

    def test_legacy_fallback_adds_new_coverage_to_a_large_partial_topology(self):
        image = self.np.full((320, 1120, 3), 255, dtype=self.np.uint8)
        boxes = {
            f"ACC-{index:03d}": (
                40.0 + (index - 1) * 170.0,
                130.0,
                70.0,
                24.0,
            )
            for index in range(1, 7)
        }
        self._solid_edge(
            image,
            boxes["ACC-001"],
            boxes["ACC-002"],
            (20, 20, 20),
            thickness=3,
        )

        with patch.object(
            self.backend,
            "_legacy_padded_segment_pairs",
            return_value=(("ACC-003", "ACC-004", (0, 0, 100, 0)),),
        ), patch.object(
            self.backend,
            "_legacy_layered_component_pairs",
            return_value=(),
        ):
            evidence = self._evidence(image, boxes)

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {
                frozenset(("ACC-001", "ACC-002")),
                frozenset(("ACC-003", "ACC-004")),
            },
        )

    def test_legacy_fallback_recovers_a_three_layer_role_tree(self):
        image = self.np.full((840, 900, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (415.0, 30.0, 70.0, 24.0),
            "AGG-001": (250.0, 350.0, 70.0, 24.0),
            "AGG-002": (680.0, 350.0, 70.0, 24.0),
            "ACC-001": (40.0, 750.0, 70.0, 24.0),
            "ACC-002": (280.0, 750.0, 70.0, 24.0),
            "ACC-003": (520.0, 750.0, 70.0, 24.0),
            "ACC-004": (790.0, 750.0, 70.0, 24.0),
        }
        self.cv2.line(image, (450, 78), (450, 300), (20, 20, 20), 4)
        self.cv2.line(image, (235, 300), (665, 300), (20, 20, 20), 4)
        self.cv2.line(image, (235, 300), (235, 520), (20, 20, 20), 4)
        self.cv2.line(image, (665, 300), (665, 520), (20, 20, 20), 4)
        self.cv2.line(image, (75, 520), (315, 520), (20, 20, 20), 4)
        self.cv2.line(image, (555, 520), (825, 520), (20, 20, 20), 4)
        for x_position in (75, 315, 555, 825):
            self.cv2.line(
                image,
                (x_position, 520),
                (x_position, 726),
                (20, 20, 20),
                4,
            )

        evidence = self._evidence(image, boxes)

        self.assertEqual(
            {(item.source, item.target) for item in evidence.connectors},
            {
                ("CORE-001", "AGG-001"),
                ("CORE-001", "AGG-002"),
                ("AGG-001", "ACC-001"),
                ("AGG-001", "ACC-002"),
                ("AGG-002", "ACC-003"),
                ("AGG-002", "ACC-004"),
            },
        )
        self.assertEqual(
            {item.evidence for item in evidence.connectors},
            {"legacy_layered_pixel_component"},
        )

    def test_legacy_fallback_rejects_a_cross_layer_container_frame(self):
        image = self.np.full((500, 400, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (165.0, 80.0, 70.0, 24.0),
            "ACC-001": (165.0, 340.0, 70.0, 24.0),
        }
        self.cv2.rectangle(image, (100, 50), (300, 390), (20, 20, 20), 4)

        self.assertEqual(self._evidence(image, boxes).connectors, ())

    def test_legacy_padded_hough_rejects_nodes_near_same_container_border(self):
        image = self.np.full((420, 420, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (105.0, 56.0, 70.0, 24.0),
            "ACC-001": (245.0, 56.0, 70.0, 24.0),
        }
        self.cv2.rectangle(image, (100, 50), (320, 370), (20, 20, 20), 4)

        _spans, nodes = self._nodes(boxes)
        frame = self._frame(image)
        decoded = self.backend._decode(frame.raw)
        gray = self.cv2.cvtColor(decoded, self.cv2.COLOR_BGR2GRAY)
        mask = self.cv2.threshold(
            gray,
            0,
            255,
            self.cv2.THRESH_BINARY_INV + self.cv2.THRESH_OTSU,
        )[1]
        component_labels, _stats, blocked_labels = (
            self.backend._legacy_component_analysis(
                mask,
                diagram_nodes=nodes,
            )
        )
        pairs = self.backend._legacy_padded_segment_pairs(
            ((100, 50, 320, 50),),
            mask=mask,
            node_boxes=boxes,
            anchor_boxes=boxes,
            diagram_nodes=nodes,
            component_labels=component_labels,
            blocked_component_labels=blocked_labels,
        )

        self.assertTrue(blocked_labels)
        self.assertEqual(pairs, ())

    def test_legacy_padded_hough_keeps_real_edge_inside_container(self):
        image = self.np.full((500, 500, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (215.0, 100.0, 70.0, 24.0),
            "ACC-001": (215.0, 350.0, 70.0, 24.0),
        }
        self.cv2.rectangle(image, (50, 50), (450, 450), (20, 20, 20), 4)
        self.cv2.line(image, (250, 148), (250, 326), (20, 20, 20), 4)

        _spans, nodes = self._nodes(boxes)
        frame = self._frame(image)
        decoded = self.backend._decode(frame.raw)
        gray = self.cv2.cvtColor(decoded, self.cv2.COLOR_BGR2GRAY)
        mask = self.cv2.threshold(
            gray,
            0,
            255,
            self.cv2.THRESH_BINARY_INV + self.cv2.THRESH_OTSU,
        )[1]
        component_labels, _stats, blocked_labels = (
            self.backend._legacy_component_analysis(
                mask,
                diagram_nodes=nodes,
            )
        )

        self.assertTrue(blocked_labels)
        self.assertEqual(
            self.backend._legacy_padded_segment_pairs(
                ((250, 148, 250, 326),),
                mask=mask,
                node_boxes=boxes,
                anchor_boxes=boxes,
                diagram_nodes=nodes,
                component_labels=component_labels,
                blocked_component_labels=blocked_labels,
            ),
            (("CORE-001", "ACC-001", (250, 148, 250, 326)),),
        )

    def test_legacy_crossing_scan_is_bounded_for_dense_segment_sets(self):
        segment_pairs = tuple(
            (
                f"LEFT-{index}",
                f"RIGHT-{index}",
                (0, index, 100, index),
            )
            for index in range(self.backend.MAX_LEGACY_CROSSING_PAIRS + 1)
        )

        self.assertEqual(
            self.backend._legacy_straight_crossing_node_groups(segment_pairs),
            (),
        )

    def test_legacy_fallback_keeps_only_straight_gapped_crossing_edges(self):
        image = self.np.full((620, 620, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (275.0, 30.0, 70.0, 24.0),
            "AGG-001": (30.0, 298.0, 70.0, 24.0),
            "AGG-002": (520.0, 298.0, 70.0, 24.0),
            "ACC-001": (275.0, 566.0, 70.0, 24.0),
        }
        self.cv2.line(image, (310, 78), (310, 542), (20, 20, 20), 4)
        self.cv2.line(image, (124, 310), (496, 310), (20, 20, 20), 4)

        evidence = self._evidence(image, boxes)

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {
                frozenset(("CORE-001", "ACC-001")),
                frozenset(("AGG-001", "AGG-002")),
            },
        )
        self.assertEqual(
            {connector.evidence for connector in evidence.connectors},
            {"legacy_padded_hough_segment"},
        )

    def test_wide_gapped_crossing_still_keeps_only_straight_edges(self):
        boxes = {
            "CORE-001": (275.0, 30.0, 70.0, 24.0),
            "AGG-001": (30.0, 298.0, 70.0, 24.0),
            "AGG-002": (520.0, 298.0, 70.0, 24.0),
            "ACC-001": (275.0, 566.0, 70.0, 24.0),
        }
        for gap in (36, 40, 44):
            with self.subTest(gap=gap):
                image = self.np.full((620, 620, 3), 255, dtype=self.np.uint8)
                self.cv2.line(
                    image,
                    (310, 54 + gap),
                    (310, 566 - gap),
                    (20, 20, 20),
                    4,
                )
                self.cv2.line(
                    image,
                    (100 + gap, 310),
                    (520 - gap, 310),
                    (20, 20, 20),
                    4,
                )

                self.assertEqual(
                    {
                        frozenset((connector.source, connector.target))
                        for connector in self._evidence(image, boxes).connectors
                    },
                    {
                        frozenset(("CORE-001", "ACC-001")),
                        frozenset(("AGG-001", "AGG-002")),
                    },
                )

    def test_legacy_fallback_recovers_generic_three_layer_tree(self):
        image = self.np.full((780, 760, 3), 255, dtype=self.np.uint8)
        boxes = {
            "testNE100": (345.0, 30.0, 70.0, 24.0),
            "testNE200": (180.0, 330.0, 70.0, 24.0),
            "testNE201": (510.0, 330.0, 70.0, 24.0),
            "testNE300": (50.0, 700.0, 70.0, 24.0),
            "testNE301": (250.0, 700.0, 70.0, 24.0),
            "testNE302": (440.0, 700.0, 70.0, 24.0),
            "testNE303": (640.0, 700.0, 70.0, 24.0),
        }
        self.cv2.line(image, (380, 78), (380, 280), (20, 20, 20), 4)
        self.cv2.line(image, (215, 280), (545, 280), (20, 20, 20), 4)
        self.cv2.line(image, (215, 280), (215, 500), (20, 20, 20), 4)
        self.cv2.line(image, (545, 280), (545, 500), (20, 20, 20), 4)
        self.cv2.line(image, (85, 500), (285, 500), (20, 20, 20), 4)
        self.cv2.line(image, (475, 500), (675, 500), (20, 20, 20), 4)
        for x_position in (85, 285, 475, 675):
            self.cv2.line(
                image,
                (x_position, 500),
                (x_position, 676),
                (20, 20, 20),
                4,
            )

        self.assertTrue(
            {
                ("testNE100", "testNE200"),
                ("testNE100", "testNE201"),
                ("testNE200", "testNE300"),
                ("testNE200", "testNE301"),
                ("testNE201", "testNE302"),
                ("testNE201", "testNE303"),
            }.issubset(self._detect(image, boxes)),
        )

    def test_layered_trunk_recovers_generic_testne_root_and_all_leaves(self):
        image = self.np.full((500, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "testNE9000": (315.0, 30.0, 70.0, 24.0),
            "testNE1000": (80.0, 400.0, 70.0, 24.0),
            "testNE2000": (315.0, 400.0, 70.0, 24.0),
            "testNE3000": (550.0, 400.0, 70.0, 24.0),
        }
        self.cv2.line(image, (350, 54), (350, 220), (20, 20, 20), 4)
        self.cv2.line(image, (115, 220), (585, 220), (20, 20, 20), 4)
        self.cv2.line(image, (115, 220), (115, 400), (20, 20, 20), 4)
        self.cv2.line(image, (350, 220), (350, 400), (20, 20, 20), 4)
        self.cv2.line(image, (585, 220), (585, 400), (20, 20, 20), 4)

        detected = {
            frozenset(edge)
            for edge in self._detect(image, boxes)
        }

        self.assertEqual(
            detected,
            {
                frozenset(("testNE9000", "testNE1000")),
                frozenset(("testNE9000", "testNE2000")),
                frozenset(("testNE9000", "testNE3000")),
            },
        )

    def test_diagonal_x_crossing_does_not_become_a_shared_bus(self):
        image = self.np.full((500, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (60.0, 50.0, 70.0, 24.0),
            "ACC-001": (570.0, 50.0, 70.0, 24.0),
            "ACC-002": (60.0, 400.0, 70.0, 24.0),
            "ACC-003": (570.0, 400.0, 70.0, 24.0),
        }
        self.cv2.line(image, (130, 74), (570, 400), (20, 20, 20), 4)
        self.cv2.line(image, (570, 74), (130, 400), (20, 20, 20), 4)

        detected = {
            frozenset(edge)
            for edge in self._detect(image, boxes)
        }

        self.assertEqual(
            detected,
            {
                frozenset(("CORE-001", "ACC-003")),
                frozenset(("ACC-001", "ACC-002")),
            },
        )

    def test_orthogonal_crossing_without_layered_leaves_is_not_a_shared_bus(self):
        image = self.np.full((520, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (30.0, 238.0, 70.0, 24.0),
            "ACC-001": (600.0, 238.0, 70.0, 24.0),
            "ACC-002": (315.0, 30.0, 70.0, 24.0),
            "ACC-003": (315.0, 446.0, 70.0, 24.0),
        }
        self.cv2.line(image, (100, 250), (600, 250), (20, 20, 20), 4)
        self.cv2.line(image, (350, 54), (350, 446), (20, 20, 20), 4)

        detected = {
            frozenset(edge)
            for edge in self._detect(image, boxes)
        }

        self.assertEqual(
            detected,
            {
                frozenset(("CORE-001", "ACC-001")),
                frozenset(("ACC-002", "ACC-003")),
            },
        )

    def test_undirected_endpoint_order_stays_stable_when_hub_degree_changes(self):
        boxes = {
            "testNE9000": (315.0, 238.0, 70.0, 24.0),
            "testNE1000": (65.0, 238.0, 70.0, 24.0),
            "testNE2000": (315.0, 38.0, 70.0, 24.0),
            "testNE3000": (565.0, 238.0, 70.0, 24.0),
        }
        first_image = self.np.full((500, 700, 3), 255, dtype=self.np.uint8)
        self._solid_edge(
            first_image,
            boxes["testNE9000"],
            boxes["testNE1000"],
            (20, 20, 20),
        )
        second_image = first_image.copy()
        self._solid_edge(
            second_image,
            boxes["testNE9000"],
            boxes["testNE2000"],
            (20, 20, 20),
        )
        self._solid_edge(
            second_image,
            boxes["testNE9000"],
            boxes["testNE3000"],
            (20, 20, 20),
        )

        pair = frozenset(("testNE9000", "testNE1000"))
        first_edge = next(
            item
            for item in self._evidence(first_image, boxes).connectors
            if frozenset((item.source, item.target)) == pair
        )
        second_edge = next(
            item
            for item in self._evidence(second_image, boxes).connectors
            if frozenset((item.source, item.target)) == pair
        )

        self.assertEqual(
            (first_edge.source, first_edge.target),
            (second_edge.source, second_edge.target),
        )

    def test_reference_star_recovers_all_43_hub_edges_without_extra_links(self):
        peripheral_ids = [
            "testNE4994",
            "testNE4995",
            "testNE49911",
            "testNE49925",
            "testNE49913",
            "testNE49944",
            "testNE49938",
            "testNE49943",
            "testNE49937",
            "testNE49997",
            "testNE49926",
            "testNE4991",
            "testNE49945",
            "testNE49931",
            "testNE49923",
            "testNE4998",
            "testNE49924",
            "testNE49919",
            "testNE49941",
            "testNE49920",
            "testNE4992",
            "testNE49942",
            "testNE49930",
            "testNE49929",
            "testNE4996",
            "testNE49935",
            "testNE49933",
            "testNE49915",
            "testNE49927",
            "testNE49946",
            "testNE49917",
            "testNE49921",
            "testNE4993",
            "testNE49914",
            "testNE49912",
            "testNE49916",
            "testNE49918",
            "testNE4999",
            "testNE49939",
            "testNE49934",
            "testNE49940",
            "testNE49910",
            "testNE49949",
        ]
        image = self.np.full((1400, 1800, 3), 255, dtype=self.np.uint8)
        center_x, center_y = 900.0, 700.0
        box_width, box_height = 100.0, 22.0
        boxes = {
            "testNE49932": (
                center_x - box_width / 2,
                center_y - box_height / 2,
                box_width,
                box_height,
            )
        }
        for index, business_id in enumerate(peripheral_ids):
            angle = 2 * math.pi * index / len(peripheral_ids) - math.pi / 2
            radius_x = 760.0 - (index % 3) * 12.0
            radius_y = 590.0 - (index % 4) * 10.0
            node_x = center_x + radius_x * math.cos(angle)
            node_y = center_y + radius_y * math.sin(angle)
            boxes[business_id] = (
                node_x - box_width / 2,
                node_y - box_height / 2,
                box_width,
                box_height,
            )
            self._solid_edge(
                image,
                boxes["testNE49932"],
                boxes[business_id],
                (0, 140, 0),
                thickness=3,
            )

        actual = {
            frozenset((item.source, item.target))
            for item in self._evidence(image, boxes).connectors
        }
        expected = {
            frozenset(("testNE49932", business_id))
            for business_id in peripheral_ids
        }

        self.assertEqual(actual, expected)

    def test_black_background_detects_orange_red_solid_and_cyan_dashed_edges(self):
        image = self.np.zeros((440, 820, 3), dtype=self.np.uint8)
        boxes = {
            "CORE-001": (375.0, 35.0, 70.0, 24.0),
            "ACC-001": (80.0, 300.0, 70.0, 24.0),
            "ACC-002": (300.0, 300.0, 70.0, 24.0),
            "ACC-003": (520.0, 300.0, 70.0, 24.0),
            "ACC-004": (680.0, 300.0, 70.0, 24.0),
        }
        self._solid_edge(
            image,
            boxes["CORE-001"],
            boxes["ACC-001"],
            (0, 165, 255),  # orange in BGR
        )
        self._solid_edge(
            image,
            boxes["CORE-001"],
            boxes["ACC-002"],
            (0, 0, 255),  # red in BGR
        )
        self._dashed_edge(
            image,
            boxes["CORE-001"],
            boxes["ACC-003"],
            (255, 255, 0),  # cyan in BGR
        )

        evidence = self._evidence(image, boxes)
        detected = {(item.source, item.target) for item in evidence.connectors}

        self.assertEqual(
            detected,
            {
                ("CORE-001", "ACC-001"),
                ("CORE-001", "ACC-002"),
                ("CORE-001", "ACC-003"),
            },
            "the isolated ACC-004 must not be inferred from the black background",
        )
        by_pair = {
            frozenset((item.source, item.target)): item
            for item in evidence.connectors
        }
        self.assertEqual(
            (
                by_pair[frozenset(("CORE-001", "ACC-001"))].line_style,
                by_pair[frozenset(("CORE-001", "ACC-001"))].line_color,
            ),
            ("solid", "orange"),
        )
        self.assertEqual(
            (
                by_pair[frozenset(("CORE-001", "ACC-002"))].line_style,
                by_pair[frozenset(("CORE-001", "ACC-002"))].line_color,
            ),
            ("solid", "red"),
        )
        self.assertEqual(
            (
                by_pair[frozenset(("CORE-001", "ACC-003"))].line_style,
                by_pair[frozenset(("CORE-001", "ACC-003"))].line_color,
            ),
            ("dashed", "cyan"),
        )

    def test_dense_icon_star_uses_glyph_anchors_instead_of_offset_labels(self):
        center = (600, 450)
        peripheral_count = 24
        icon_centers = {"testNE0": center}
        boxes = {"testNE0": (568.0, 515.0, 64.0, 16.0)}
        for index in range(peripheral_count):
            angle = 2 * math.pi * index / peripheral_count
            icon_center = (
                int(center[0] + 500 * math.cos(angle)),
                int(center[1] + 350 * math.sin(angle)),
            )
            business_id = f"testNE{index + 1}"
            icon_centers[business_id] = icon_center
            boxes[business_id] = (
                icon_center[0] - 32.0,
                icon_center[1] + 65.0,
                64.0,
                16.0,
            )

        expected = {
            frozenset(("testNE0", business_id))
            for business_id in icon_centers
            if business_id != "testNE0"
        }
        for line_thickness in (3, 5, 8):
            with self.subTest(line_thickness=line_thickness):
                image = self.np.full(
                    (900, 1200, 3),
                    255,
                    dtype=self.np.uint8,
                )
                for icon_center in icon_centers.values():
                    self.cv2.circle(
                        image,
                        icon_center,
                        12,
                        (0, 140, 0),
                        thickness=-1,
                        lineType=self.cv2.LINE_AA,
                    )
                for business_id, target in icon_centers.items():
                    if business_id == "testNE0":
                        continue
                    delta_x = target[0] - center[0]
                    delta_y = target[1] - center[1]
                    length = math.hypot(delta_x, delta_y)
                    start = (
                        round(center[0] + delta_x * 12 / length),
                        round(center[1] + delta_y * 12 / length),
                    )
                    end = (
                        round(target[0] - delta_x * 12 / length),
                        round(target[1] - delta_y * 12 / length),
                    )
                    self.cv2.line(
                        image,
                        start,
                        end,
                        (0, 140, 0),
                        line_thickness,
                        self.cv2.LINE_AA,
                    )

                actual = {
                    frozenset((connector.source, connector.target))
                    for connector in self._evidence(image, boxes).connectors
                }
                self.assertEqual(actual, expected)

        outlined = self.np.full((300, 800, 3), 255, dtype=self.np.uint8)
        outlined_boxes = {
            "testNE101": (118.0, 150.0, 64.0, 16.0),
            "testNE102": (618.0, 150.0, 64.0, 16.0),
        }
        for icon_center in ((150, 100), (650, 100)):
            self.cv2.circle(
                outlined,
                icon_center,
                12,
                (0, 140, 0),
                thickness=2,
                lineType=self.cv2.LINE_AA,
            )
        self.cv2.line(
            outlined,
            (162, 100),
            (638, 100),
            (0, 140, 0),
            3,
            self.cv2.LINE_AA,
        )
        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in self._evidence(
                    outlined,
                    outlined_boxes,
                ).connectors
            },
            {frozenset(("testNE101", "testNE102"))},
        )

    def test_group_rectangle_does_not_connect_contained_labels(self):
        image = self.np.full((320, 520, 3), 255, dtype=self.np.uint8)
        boxes = {
            "ACC-001": (110.0, 140.0, 100.0, 22.0),
            "ACC-002": (260.0, 140.0, 100.0, 22.0),
        }
        self.cv2.rectangle(image, (85, 105), (385, 195), (20, 20, 20), 3)

        evidence = self._evidence(image, boxes)

        self.assertEqual(evidence.connectors, ())
        self.assertNotEqual(
            evidence.node_boxes["ACC-001"],
            evidence.node_boxes["ACC-002"],
        )

        outlined = self.np.full((260, 800, 3), 255, dtype=self.np.uint8)
        outlined_boxes = {
            "ACC-101": (100.0, 115.0, 60.0, 20.0),
            "ACC-102": (640.0, 115.0, 60.0, 20.0),
        }
        self.cv2.rectangle(outlined, (60, 80), (200, 170), (20, 20, 20), 3)
        self.cv2.rectangle(outlined, (600, 80), (740, 170), (20, 20, 20), 3)
        self.cv2.line(outlined, (200, 125), (600, 125), (20, 20, 20), 3)
        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in self._evidence(
                    outlined,
                    outlined_boxes,
                ).connectors
            },
            {frozenset(("ACC-101", "ACC-102"))},
        )

    def test_same_layer_leaf_stubs_on_shared_bus_are_not_a_direct_edge(self):
        image = self.np.full((420, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "AP-026": (130.0, 340.0, 70.0, 24.0),
            "AP-034": (500.0, 340.0, 70.0, 24.0),
        }
        # Both AP stubs reach the same upstream bus, but there is no direct
        # horizontal AP-to-AP pixel path.  Reconstructed component continuity
        # must not turn the two leaves into peers.
        self.cv2.line(image, (165, 340), (165, 150), (20, 20, 20), 4)
        self.cv2.line(image, (535, 340), (535, 150), (20, 20, 20), 4)
        self.cv2.line(image, (165, 150), (535, 150), (20, 20, 20), 4)

        self.assertEqual(self._evidence(image, boxes).connectors, ())

    def test_different_roles_on_same_side_of_bus_are_not_a_direct_edge(self):
        image = self.np.full((420, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "CORE-001": (130.0, 340.0, 80.0, 24.0),
            "AP-034": (500.0, 340.0, 70.0, 24.0),
        }
        self.cv2.line(image, (170, 340), (170, 150), (20, 20, 20), 4)
        self.cv2.line(image, (535, 340), (535, 150), (20, 20, 20), 4)
        self.cv2.line(image, (170, 150), (535, 150), (20, 20, 20), 4)

        # Role hierarchy may orient an observed edge, but it must never create
        # one when both endpoints leave toward the same upstream bus.
        self.assertEqual(self._evidence(image, boxes).connectors, ())

    def test_directional_fallback_does_not_infer_ap_to_ap_hierarchy(self):
        image = self.np.full((660, 500, 3), 255, dtype=self.np.uint8)
        boxes = {
            "AP-001": (215.0, 100.0, 70.0, 24.0),
            "AP-002": (215.0, 500.0, 70.0, 24.0),
        }
        self.cv2.line(image, (250, 204), (250, 420), (20, 20, 20), 4)

        self.assertEqual(self._evidence(image, boxes).connectors, ())

    def test_same_layer_leaf_direct_source_line_remains_a_valid_edge(self):
        image = self.np.full((260, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "AP-101": (80.0, 110.0, 70.0, 24.0),
            "AP-102": (550.0, 110.0, 70.0, 24.0),
        }
        self.cv2.line(image, (150, 122), (550, 122), (20, 20, 20), 4)

        evidence = self._evidence(image, boxes)

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {frozenset(("AP-101", "AP-102"))},
        )

    def test_short_endpoint_gaps_keep_a_real_direct_edge(self):
        boxes = {
            "ACC-101": (80.0, 110.0, 70.0, 24.0),
            "ACC-102": (550.0, 110.0, 70.0, 24.0),
        }
        for gap in (4, 6, 9):
            with self.subTest(gap=gap):
                image = self.np.full((260, 700, 3), 255, dtype=self.np.uint8)
                self.cv2.line(
                    image,
                    (150 + gap, 122),
                    (550 - gap, 122),
                    (20, 20, 20),
                    4,
                )
                self.assertEqual(
                    {
                        frozenset((connector.source, connector.target))
                        for connector in self._evidence(image, boxes).connectors
                    },
                    {frozenset(("ACC-101", "ACC-102"))},
                )

    def test_two_endpoint_stubs_do_not_validate_a_long_reconstructed_path(self):
        boxes = {
            "AP-101": (80.0, 110.0, 70.0, 24.0),
            "AP-102": (550.0, 110.0, 70.0, 24.0),
        }
        _spans, nodes = self._nodes(boxes)
        reconstructed = self.np.zeros((260, 700), dtype=self.np.uint8)
        source = self.np.zeros_like(reconstructed)
        self.cv2.line(reconstructed, (150, 122), (550, 122), 255, 3)
        self.cv2.line(source, (150, 122), (190, 122), 255, 3)
        self.cv2.line(source, (510, 122), (550, 122), 255, 3)

        self.assertEqual(
            self.backend._component_connector_pairs(
                reconstructed,
                source_mask=source,
                contact_boxes=boxes,
                diagram_nodes=nodes,
                horizontal_mask=source,
                vertical_mask=self.np.zeros_like(source),
            ),
            (),
        )

    def test_multi_branch_ignores_an_endpoint_without_source_pixels(self):
        boxes = {
            "CORE-001": (315.0, 30.0, 70.0, 24.0),
            "AP-001": (80.0, 400.0, 70.0, 24.0),
            "AP-002": (550.0, 400.0, 70.0, 24.0),
            "AP-003": (315.0, 400.0, 70.0, 24.0),
        }
        _spans, nodes = self._nodes(boxes)
        source = self.np.zeros((500, 700), dtype=self.np.uint8)
        self.cv2.line(source, (350, 54), (350, 220), 255, 4)
        self.cv2.line(source, (115, 220), (585, 220), 255, 4)
        self.cv2.line(source, (115, 220), (115, 400), 255, 4)
        self.cv2.line(source, (585, 220), (585, 400), 255, 4)
        reconstructed = source.copy()
        self.cv2.line(reconstructed, (350, 220), (350, 400), 255, 4)

        pairs = self.backend._component_connector_pairs(
            reconstructed,
            source_mask=source,
            contact_boxes=boxes,
            diagram_nodes=nodes,
            horizontal_mask=source,
            vertical_mask=source,
        )

        self.assertEqual(
            {
                frozenset((first, second))
                for first, second, _multi_branch in pairs
            },
            {
                frozenset(("CORE-001", "AP-001")),
                frozenset(("CORE-001", "AP-002")),
            },
        )

    def test_false_third_endpoint_downgrades_to_a_real_two_node_path(self):
        boxes = {
            "ACC-001": (40.0, 188.0, 70.0, 24.0),
            "ACC-002": (590.0, 188.0, 70.0, 24.0),
            "AP-999": (315.0, 40.0, 70.0, 24.0),
        }
        _spans, nodes = self._nodes(boxes)
        source = self.np.zeros((300, 700), dtype=self.np.uint8)
        self.cv2.line(source, (110, 200), (590, 200), 255, 4)
        reconstructed = source.copy()
        self.cv2.line(reconstructed, (350, 64), (350, 200), 255, 4)

        pairs = self.backend._component_connector_pairs(
            reconstructed,
            source_mask=source,
            contact_boxes=boxes,
            diagram_nodes=nodes,
            horizontal_mask=source,
            vertical_mask=source,
        )

        self.assertEqual(
            pairs,
            (("ACC-001", "ACC-002", False),),
        )

    def test_dense_perpendicular_strokes_do_not_form_a_phantom_edge(self):
        boxes = {
            "ACC-001": (40.0, 148.0, 80.0, 24.0),
            "ACC-002": (500.0, 148.0, 80.0, 24.0),
        }
        for spacing, stroke_width in ((14, 4), (4, 1)):
            with self.subTest(spacing=spacing, stroke_width=stroke_width):
                image = self.np.full(
                    (320, 620, 3),
                    255,
                    dtype=self.np.uint8,
                )
                for x in range(135, 500, spacing):
                    self.cv2.line(
                        image,
                        (x, 40),
                        (x, 280),
                        (20, 20, 20),
                        stroke_width,
                        self.cv2.LINE_AA,
                    )

                self.assertEqual(self._detect(image, boxes), set())

                self.cv2.line(
                    image,
                    (120, 160),
                    (500, 160),
                    (20, 20, 20),
                    1,
                    self.cv2.LINE_AA,
                )
                evidence = self._evidence(image, boxes)
                self.assertEqual(
                    {
                        frozenset((connector.source, connector.target))
                        for connector in evidence.connectors
                    },
                    {frozenset(("ACC-001", "ACC-002"))},
                )

    def test_compact_x_crossing_keeps_only_the_two_straight_edges(self):
        boxes = {
            "A": (200.0, 100.0, 30.0, 16.0),
            "B": (320.0, 100.0, 30.0, 16.0),
            "C": (200.0, 220.0, 30.0, 16.0),
            "D": (320.0, 220.0, 30.0, 16.0),
        }
        expected = {
            frozenset(("A", "D")),
            frozenset(("B", "C")),
        }
        for thickness in (3, 5):
            with self.subTest(thickness=thickness):
                image = self.np.full((340, 580, 3), 255, dtype=self.np.uint8)
                self.cv2.line(
                    image,
                    (223, 116),
                    (327, 220),
                    (20, 20, 20),
                    thickness,
                    self.cv2.LINE_AA,
                )
                self.cv2.line(
                    image,
                    (327, 116),
                    (223, 220),
                    (20, 20, 20),
                    thickness,
                    self.cv2.LINE_AA,
                )
                actual = {
                    frozenset((connector.source, connector.target))
                    for connector in self._evidence(image, boxes).connectors
                }

                self.assertEqual(actual, expected)

        plus = self.np.full((520, 620, 3), 255, dtype=self.np.uint8)
        plus_boxes = {
            "P00": (40.0, 282.0, 60.0, 16.0),
            "P01": (520.0, 282.0, 60.0, 16.0),
            "P10": (280.0, 40.0, 60.0, 16.0),
            "P11": (280.0, 440.0, 60.0, 16.0),
            "A": (285.0, 180.0, 50.0, 16.0),
        }
        self.cv2.line(
            plus,
            (100, 290),
            (520, 290),
            (20, 20, 20),
            5,
            self.cv2.LINE_AA,
        )
        self.cv2.line(
            plus,
            (310, 56),
            (310, 440),
            (20, 20, 20),
            5,
            self.cv2.LINE_AA,
        )
        plus_spans, plus_nodes = self._nodes(plus_boxes)
        uncertain = plus_nodes["A"]
        plus_nodes["A"] = DeviceOccurrence(
            business_id=uncertain.business_id,
            prefix=uncertain.prefix,
            confidence=0.72,
            bbox=uncertain.bbox,
            raw_text=uncertain.raw_text,
            span_index=uncertain.span_index,
            corrected_ocr=True,
        )
        plus_spans = tuple(
            OCRSpan(span.text, 0.72, span.bbox)
            if index == uncertain.span_index
            else span
            for index, span in enumerate(plus_spans)
        )
        plus_evidence = self.backend.analyze_connectors(
            self._frame(plus),
            spans=plus_spans,
            diagram_nodes=plus_nodes,
            diagram_bottom=float(plus.shape[0]),
        )
        self.assertEqual(plus_evidence.pass_through_nodes, frozenset({"A"}))
        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in plus_evidence.connectors
            },
            {
                frozenset(("P00", "P01")),
                frozenset(("P10", "P11")),
            },
        )

        # A high-confidence, text-only middle node is genuinely ambiguous and
        # must remain an endpoint rather than being collapsed into A-C.
        chain = self.np.full((220, 700, 3), 255, dtype=self.np.uint8)
        chain_boxes = {
            "ACC-001": (40.0, 100.0, 60.0, 20.0),
            "ACC-002": (280.0, 100.0, 60.0, 20.0),
            "ACC-003": (520.0, 100.0, 60.0, 20.0),
        }
        self._solid_edge(
            chain,
            chain_boxes["ACC-001"],
            chain_boxes["ACC-002"],
            (20, 20, 20),
            thickness=3,
        )
        self._solid_edge(
            chain,
            chain_boxes["ACC-002"],
            chain_boxes["ACC-003"],
            (20, 20, 20),
            thickness=3,
        )
        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in self._evidence(chain, chain_boxes).connectors
            },
            {
                frozenset(("ACC-001", "ACC-002")),
                frozenset(("ACC-002", "ACC-003")),
            },
        )
        chain_spans, chain_nodes = self._nodes(chain_boxes)
        corrected_middle = chain_nodes["ACC-002"]
        chain_nodes["ACC-002"] = DeviceOccurrence(
            business_id=corrected_middle.business_id,
            prefix=corrected_middle.prefix,
            confidence=0.91,
            bbox=corrected_middle.bbox,
            raw_text=corrected_middle.raw_text,
            span_index=corrected_middle.span_index,
            corrected_ocr=True,
        )
        corrected_chain = self.backend.analyze_connectors(
            self._frame(chain),
            spans=chain_spans,
            diagram_nodes=chain_nodes,
            diagram_bottom=float(chain.shape[0]),
        )
        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in corrected_chain.connectors
            },
            {
                frozenset(("ACC-001", "ACC-002")),
                frozenset(("ACC-002", "ACC-003")),
            },
        )

        low_confidence_middle = chain_nodes["ACC-002"]
        chain_nodes["ACC-002"] = DeviceOccurrence(
            business_id=low_confidence_middle.business_id,
            prefix=low_confidence_middle.prefix,
            confidence=0.70,
            bbox=low_confidence_middle.bbox,
            raw_text=low_confidence_middle.raw_text,
            span_index=low_confidence_middle.span_index,
            corrected_ocr=True,
        )
        low_confidence_spans = tuple(
            OCRSpan(span.text, 0.70, span.bbox)
            if span.text == "ACC-002"
            else span
            for span in chain_spans
        )
        low_confidence_chain = self.backend.analyze_connectors(
            self._frame(chain),
            spans=low_confidence_spans,
            diagram_nodes=chain_nodes,
            diagram_bottom=float(chain.shape[0]),
        )
        self.assertEqual(low_confidence_chain.pass_through_nodes, frozenset())
        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in low_confidence_chain.connectors
            },
            {
                frozenset(("ACC-001", "ACC-002")),
                frozenset(("ACC-002", "ACC-003")),
            },
        )

        pass_dashed = self.np.full((220, 620, 3), 255, dtype=self.np.uint8)
        pass_dashed_boxes = {
            "P1": (40.0, 100.0, 60.0, 20.0),
            "A": (285.0, 100.0, 50.0, 20.0),
            "P2": (520.0, 100.0, 60.0, 20.0),
        }
        for start, end in (
            (100, 130),
            (165, 195),
            (230, 283),
            (337, 370),
            (405, 435),
            (470, 520),
        ):
            self.cv2.line(
                pass_dashed,
                (start, 110),
                (end, 110),
                (20, 20, 20),
                3,
                self.cv2.LINE_AA,
            )
        pass_spans, pass_nodes = self._nodes(pass_dashed_boxes)
        uncertain = pass_nodes["A"]
        pass_nodes["A"] = DeviceOccurrence(
            business_id=uncertain.business_id,
            prefix=uncertain.prefix,
            confidence=0.70,
            bbox=uncertain.bbox,
            raw_text=uncertain.raw_text,
            span_index=uncertain.span_index,
            corrected_ocr=True,
        )
        pass_spans = tuple(
            OCRSpan(span.text, 0.70, span.bbox)
            if index == uncertain.span_index
            else span
            for index, span in enumerate(pass_spans)
        )
        pass_evidence = self.backend.analyze_connectors(
            self._frame(pass_dashed),
            spans=pass_spans,
            diagram_nodes=pass_nodes,
            diagram_bottom=float(pass_dashed.shape[0]),
        )
        self.assertEqual(pass_evidence.pass_through_nodes, frozenset({"A"}))
        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in pass_evidence.connectors
            },
            {frozenset(("P1", "P2"))},
        )

    def test_large_blank_gap_requires_edge_label_evidence(self):
        image = self.np.full((260, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "testNE1": (40.0, 110.0, 80.0, 20.0),
            "testNE2": (580.0, 110.0, 80.0, 20.0),
        }
        self.cv2.line(image, (120, 120), (320, 120), (20, 20, 20), 3)
        self.cv2.line(image, (380, 120), (580, 120), (20, 20, 20), 3)

        self.assertEqual(self._detect(image, boxes), set())

        for rejected_span in (
            OCRSpan("备注", 0.99, (325.0, 110.0, 50.0, 20.0)),
            OCRSpan("GE0/0/1", 0.01, (325.0, 110.0, 50.0, 20.0)),
        ):
            with self.subTest(rejected_span=rejected_span.text):
                self.assertEqual(
                    self._evidence(
                        image,
                        boxes,
                        extra_spans=(rejected_span,),
                    ).connectors,
                    (),
                )

        evidence = self._evidence(
            image,
            boxes,
            extra_spans=(OCRSpan("GE0/0/1", 0.98, (325.0, 110.0, 50.0, 20.0)),),
        )
        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {frozenset(("testNE1", "testNE2"))},
        )

        floating = self.np.full((220, 580, 3), 255, dtype=self.np.uint8)
        floating_boxes = {
            "A": (40.0, 90.0, 40.0, 20.0),
            "B": (500.0, 90.0, 40.0, 20.0),
        }
        self.cv2.line(floating, (120, 100), (460, 100), (20, 20, 20), 3)
        self.assertEqual(self._evidence(floating, floating_boxes).connectors, ())
        self.cv2.line(floating, (80, 100), (500, 100), (20, 20, 20), 3)
        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in self._evidence(
                    floating,
                    floating_boxes,
                ).connectors
            },
            {frozenset(("A", "B"))},
        )
        low_confidence_weight = self._evidence(
            floating,
            floating_boxes,
            extra_spans=(OCRSpan("9.999", 0.01, (270.0, 90.0, 40.0, 20.0)),),
        )
        self.assertEqual(len(low_confidence_weight.connectors), 1)
        self.assertIsNone(low_confidence_weight.connectors[0].weight)

        offset = self.np.full((500, 300, 3), 255, dtype=self.np.uint8)
        offset_boxes = {
            "A": (80.0, 20.0, 40.0, 20.0),
            "B": (130.0, 440.0, 40.0, 20.0),
        }
        self.cv2.line(offset, (100, 40), (100, 238), (20, 20, 20), 3)
        self.cv2.line(offset, (150, 262), (150, 440), (20, 20, 20), 3)
        self.assertEqual(
            self._evidence(
                offset,
                offset_boxes,
                extra_spans=(
                    OCRSpan("GE0/0/1", 0.98, (80.0, 240.0, 120.0, 20.0)),
                ),
            ).connectors,
            (),
        )

        detached_owner = self.np.full((240, 620, 3), 255, dtype=self.np.uint8)
        detached_boxes = {
            "A": (40.0, 100.0, 60.0, 20.0),
            "B": (270.0, 100.0, 60.0, 20.0),
            "C": (500.0, 100.0, 60.0, 20.0),
        }
        self.cv2.circle(
            detached_owner,
            (300, 60),
            12,
            (20, 20, 20),
            thickness=-1,
        )
        self.cv2.line(
            detached_owner,
            (100, 110),
            (265, 110),
            (20, 20, 20),
            3,
        )
        self.cv2.line(
            detached_owner,
            (335, 110),
            (500, 110),
            (20, 20, 20),
            3,
        )
        self.assertEqual(
            self._evidence(detached_owner, detached_boxes).connectors,
            (),
        )

        joined_owner = self.np.full((240, 700, 3), 255, dtype=self.np.uint8)
        self.cv2.circle(
            joined_owner,
            (260, 110),
            12,
            (20, 20, 20),
            thickness=-1,
        )
        self.cv2.line(joined_owner, (272, 110), (300, 110), (20, 20, 20), 3)
        self.cv2.line(joined_owner, (402, 110), (600, 110), (20, 20, 20), 3)
        joined_spans = (
            OCRSpan("testNE", 0.99, (300.0, 100.0, 50.0, 20.0)),
            OCRSpan("12345", 0.99, (352.0, 100.0, 50.0, 20.0)),
            OCRSpan("ACC-001", 0.99, (600.0, 100.0, 60.0, 20.0)),
        )
        joined_nodes = {
            "testNE12345": DeviceOccurrence(
                business_id="testNE12345",
                prefix="TESTNE",
                confidence=0.99,
                bbox=(300.0, 100.0, 102.0, 20.0),
                raw_text="testNE 12345",
                span_index=0,
            ),
            "ACC-001": DeviceOccurrence(
                business_id="ACC-001",
                prefix="ACC",
                confidence=0.99,
                bbox=(600.0, 100.0, 60.0, 20.0),
                raw_text="ACC-001",
                span_index=2,
            ),
        }
        joined_evidence = self.backend.analyze_connectors(
            self._frame(joined_owner),
            spans=joined_spans,
            diagram_nodes=joined_nodes,
            diagram_bottom=float(joined_owner.shape[0]),
        )
        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in joined_evidence.connectors
            },
            {frozenset(("testNE12345", "ACC-001"))},
        )

    def test_edge_label_at_orthogonal_elbow_does_not_break_path(self):
        image = self.np.full((400, 520, 3), 255, dtype=self.np.uint8)
        boxes = {
            "A": (40.0, 40.0, 60.0, 20.0),
            "B": (400.0, 300.0, 60.0, 20.0),
        }
        self.cv2.line(image, (100, 50), (430, 50), (20, 20, 20), 3)
        self.cv2.line(image, (430, 50), (430, 300), (20, 20, 20), 3)
        self.cv2.line(image, (430, 20), (430, 34), (20, 20, 20), 3)

        evidence = self._evidence(
            image,
            boxes,
            extra_spans=(OCRSpan("GE0/0/1", 0.98, (417.5, 40.0, 25.0, 20.0)),),
        )

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {frozenset(("A", "B"))},
        )

    def test_dashed_edge_detection_is_scale_invariant(self):
        def detect_at_scale(scale):
            image = self.np.full(
                (260 * scale, 320 * scale, 3),
                255,
                dtype=self.np.uint8,
            )
            boxes = {
                "A": (40.0 * scale, 100.0 * scale, 40.0 * scale, 20.0 * scale),
                "B": (220.0 * scale, 100.0 * scale, 40.0 * scale, 20.0 * scale),
            }
            for start, end in ((80, 110), (135, 165), (190, 220)):
                self.cv2.line(
                    image,
                    (start * scale, 110 * scale),
                    (end * scale, 110 * scale),
                    (20, 20, 20),
                    3 * scale,
                    self.cv2.LINE_AA,
                )
            return self._evidence(image, boxes)

        for scale in (1, 2):
            with self.subTest(scale=scale):
                evidence = detect_at_scale(scale)
                self.assertEqual(len(evidence.connectors), 1)
                connector = evidence.connectors[0]
                self.assertEqual(
                    frozenset((connector.source, connector.target)),
                    frozenset(("A", "B")),
                )
                self.assertEqual(connector.line_style, "dashed")

        # Once the scene exceeds 100 nodes, a dense remote cluster must not
        # consume the whole corridor budget and hide a long isolated edge.
        dense = self.np.full((420, 640, 3), 255, dtype=self.np.uint8)
        dense_boxes = {
            "A": (20.0, 30.0, 30.0, 16.0),
            "B": (590.0, 30.0, 30.0, 16.0),
        }
        for index in range(99):
            column = index % 11
            row = index // 11
            dense_boxes[f"N{index:02d}"] = (
                190.0 + column * 24.0,
                220.0 + row * 20.0,
                20.0,
                12.0,
            )
        for start in range(50, 570, 45):
            self.cv2.line(
                dense,
                (start, 38),
                (min(start + 20, 570), 38),
                (20, 20, 20),
                2,
                self.cv2.LINE_AA,
            )
        self.cv2.line(
            dense,
            (570, 38),
            (590, 38),
            (20, 20, 20),
            2,
            self.cv2.LINE_AA,
        )
        dense_pairs = {
            frozenset((connector.source, connector.target))
            for connector in self._evidence(dense, dense_boxes).connectors
        }
        self.assertIn(frozenset(("A", "B")), dense_pairs)
        self.assertFalse(
            any("A" in pair or "B" in pair for pair in dense_pairs - {frozenset(("A", "B"))})
        )

        high_density = self.np.full((1900, 4000, 3), 255, dtype=self.np.uint8)
        high_density_boxes = {
            "A": (490.0, 1190.0, 20.0, 20.0),
            "B": (3490.0, 390.0, 20.0, 20.0),
        }
        auxiliary_index = 0
        for origin_x, origin_y in ((500.0, 1200.0), (3500.0, 400.0)):
            for angle_degrees in (15, 45, 75, 105, 135):
                angle = math.radians(angle_degrees)
                for distance in (100.0, 200.0):
                    center_x = origin_x + math.cos(angle) * distance
                    center_y = origin_y + math.sin(angle) * distance
                    high_density_boxes[f"X{auxiliary_index:03d}"] = (
                        center_x - 10.0,
                        center_y - 10.0,
                        20.0,
                        20.0,
                    )
                    auxiliary_index += 1
        filler_index = 0
        while len(high_density_boxes) < 500:
            high_density_boxes[f"Z{filler_index:03d}"] = (
                1800.0 + (filler_index % 24) * 24.0,
                1250.0 + (filler_index // 24) * 24.0,
                20.0,
                20.0,
            )
            filler_index += 1
        self._dashed_edge(
            high_density,
            high_density_boxes["A"],
            high_density_boxes["B"],
            (20, 20, 20),
            thickness=2,
            dash=30,
            gap=35,
        )
        high_density_pairs = {
            frozenset((connector.source, connector.target))
            for connector in self._evidence(
                high_density,
                high_density_boxes,
            ).connectors
        }
        self.assertEqual(high_density_pairs, {frozenset(("A", "B"))})

    def test_reference_weighted_graph_recovers_all_13_colored_edges(self):
        image = self.np.zeros((1200, 1600, 3), dtype=self.np.uint8)
        centers = {
            "24": (600, 240),
            "48": (180, 80),
            "6": (390, 70),
            "16": (810, 70),
            "23": (1030, 150),
            "39": (620, 500),
            "44": (120, 650),
            "25": (360, 650),
            "36": (120, 940),
            "45": (360, 940),
            "5": (650, 680),
            "43": (840, 820),
            "29": (1040, 940),
            "49": (1160, 620),
            "21": (1370, 800),
            "11": (1160, 1030),
            "38": (1400, 1030),
            "14": (1270, 80),
            "41": (1450, 180),
            "28": (1320, 360),
            "33": (1480, 460),
            "30": (1420, 600),
            "47": (830, 1080),
        }
        boxes = {
            f"testNE793{suffix}": (x - 55.0, y - 11.0, 110.0, 22.0)
            for suffix, (x, y) in centers.items()
        }
        expected_edges = [
            ("24", "48", "solid", "orange", 6.572),
            ("24", "6", "solid", "orange", 4.402),
            ("24", "16", "solid", "red", 7.758),
            ("24", "23", "dashed", "cyan", 1.845),
            ("24", "39", "solid", "orange", 6.231),
            ("44", "25", "dashed", "cyan", 0.431),
            ("44", "45", "solid", "orange", 6.416),
            ("44", "36", "solid", "orange", 6.710),
            ("25", "45", "solid", "orange", 8.809),
            ("5", "43", "solid", "orange", 1.705),
            ("43", "29", "dashed", "cyan", 3.031),
            ("49", "21", "dashed", "cyan", 2.501),
            ("11", "38", "solid", "orange", 1.191),
        ]
        colors = {
            "orange": (0, 165, 255),
            "red": (0, 0, 255),
            "cyan": (255, 255, 0),
        }
        weight_spans = []
        for source_suffix, target_suffix, style, color, weight in expected_edges:
            source = f"testNE793{source_suffix}"
            target = f"testNE793{target_suffix}"
            draw = self._dashed_edge if style == "dashed" else self._solid_edge
            draw(image, boxes[source], boxes[target], colors[color])
            source_center = (
                boxes[source][0] + boxes[source][2] / 2,
                boxes[source][1] + boxes[source][3] / 2,
            )
            target_center = (
                boxes[target][0] + boxes[target][2] / 2,
                boxes[target][1] + boxes[target][3] / 2,
            )
            midpoint = (
                (source_center[0] + target_center[0]) / 2,
                (source_center[1] + target_center[1]) / 2,
            )
            delta_x = target_center[0] - source_center[0]
            delta_y = target_center[1] - source_center[1]
            edge_length = math.hypot(delta_x, delta_y)
            label_center = (
                midpoint[0] - delta_y / edge_length * 10.0,
                midpoint[1] + delta_x / edge_length * 10.0,
            )
            weight_spans.append(
                OCRSpan(
                    f"{weight:.3f}",
                    0.98,
                    (
                        label_center[0] - 17.0,
                        label_center[1] - 6.0,
                        34.0,
                        12.0,
                    ),
                )
            )

        actual = {
            frozenset((item.source, item.target)): (
                item.line_style,
                item.line_color,
                item.weight,
            )
            for item in self._evidence(
                image,
                boxes,
                extra_spans=weight_spans,
            ).connectors
        }
        expected = {
            frozenset(
                (
                    f"testNE793{source_suffix}",
                    f"testNE793{target_suffix}",
                )
            ): (style, color, weight)
            for source_suffix, target_suffix, style, color, weight in expected_edges
        }

        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
