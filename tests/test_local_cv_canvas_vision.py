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
        self.assertEqual(scene["provenance"]["adapter_version"], "1.2")
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

    def test_offset_thin_rectangular_glyphs_are_used_as_connector_anchors(self):
        image = self.np.full((260, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "ACC-101": (110.0, 150.0, 60.0, 20.0),
            "ACC-102": (510.0, 150.0, 60.0, 20.0),
        }
        self.cv2.rectangle(
            image,
            (90, 70),
            (190, 110),
            (20, 20, 20),
            2,
            self.cv2.LINE_AA,
        )
        self.cv2.rectangle(
            image,
            (490, 70),
            (590, 110),
            (20, 20, 20),
            2,
            self.cv2.LINE_AA,
        )
        # Leave a small real-world rendering gap at each device port.  The
        # connector belongs to the rectangular glyphs, not the OCR labels 40px
        # below them.
        self.cv2.line(
            image,
            (194, 90),
            (486, 90),
            (20, 20, 20),
            3,
            self.cv2.LINE_AA,
        )

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in self._evidence(image, boxes).connectors
            },
            {frozenset(("ACC-101", "ACC-102"))},
        )

    def test_offset_glyphs_with_a_gapped_elbow_use_path_checked_fallback(self):
        image = self.np.full((380, 800, 3), 255, dtype=self.np.uint8)
        boxes = {
            "ACC-101": (110.0, 145.0, 60.0, 20.0),
            "ACC-102": (520.0, 315.0, 60.0, 20.0),
        }
        self.cv2.rectangle(
            image,
            (90, 70),
            (190, 110),
            (20, 20, 20),
            2,
            self.cv2.LINE_AA,
        )
        self.cv2.rectangle(
            image,
            (500, 240),
            (600, 280),
            (20, 20, 20),
            2,
            self.cv2.LINE_AA,
        )
        # Each port has a roughly 28px gap: raw glyph contacts miss the component and
        # neither Hough segment spans both devices.  The expanded compatibility
        # contact may recover it only after the component passes path checks.
        self.cv2.line(
            image,
            (223, 90),
            (550, 90),
            (20, 20, 20),
            3,
            self.cv2.LINE_AA,
        )
        self.cv2.line(
            image,
            (550, 90),
            (550, 208),
            (20, 20, 20),
            3,
            self.cv2.LINE_AA,
        )

        evidence = self._evidence(image, boxes)

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {frozenset(("ACC-101", "ACC-102"))},
        )
        self.assertEqual(evidence.connectors[0].evidence, "legacy_connected_pixel_path")

    def test_fallback_component_recovers_an_isolated_two_ended_elbow(self):
        image = self.np.full((480, 820, 3), 255, dtype=self.np.uint8)
        boxes = {
            "ACC-001": (40.0, 40.0, 60.0, 20.0),
            "ACC-002": (400.0, 300.0, 60.0, 20.0),
        }
        # Both ends are 14px away from the raw OCR boxes.  The precise anchor
        # contact margin misses them, while the old padded node-box component
        # path can recover the real elbow after its two-ended path check.
        self.cv2.line(
            image,
            (114, 50),
            (430, 50),
            (20, 20, 20),
            3,
            self.cv2.LINE_AA,
        )
        self.cv2.line(
            image,
            (430, 50),
            (430, 286),
            (20, 20, 20),
            3,
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
        self.assertEqual(evidence.connectors[0].evidence, "legacy_connected_pixel_path")

    def test_fallback_consensus_preserves_multiple_edges_between_connected_nodes(self):
        image = self.np.full((220, 760, 3), 255, dtype=self.np.uint8)
        self.cv2.line(image, (10, 200), (40, 200), (20, 20, 20), 2)
        boxes = {
            "A": (40.0, 90.0, 60.0, 20.0),
            "B": (300.0, 90.0, 60.0, 20.0),
            "C": (620.0, 90.0, 60.0, 20.0),
        }
        spans, nodes = self._nodes(boxes)
        strict_component = (("B", "C", False),)
        fallback_segments = (
            ("A", "B", "solid", None),
            ("A", "C", "solid", None),
        )
        fallback_corridors = (
            ("A", "B", "solid", None, 0.84),
            ("A", "C", "solid", None, 0.84),
        )

        with (
            patch.object(
                self.backend,
                "_component_connector_pairs",
                side_effect=(strict_component, ()),
            ),
            patch.object(
                self.backend,
                "_segment_connector_pairs",
                side_effect=((), fallback_segments),
            ),
            patch.object(
                self.backend,
                "_corridor_connector_pairs",
                side_effect=((), fallback_corridors),
            ),
        ):
            evidence = self.backend.analyze_connectors(
                self._frame(image),
                spans=spans,
                diagram_nodes=nodes,
                diagram_bottom=float(image.shape[0]),
            )

        connectors = {
            frozenset((connector.source, connector.target)): connector
            for connector in evidence.connectors
        }
        self.assertEqual(
            set(connectors),
            {
                frozenset(("A", "B")),
                frozenset(("A", "C")),
                frozenset(("B", "C")),
            },
        )
        self.assertEqual(
            connectors[frozenset(("A", "B"))].evidence,
            "legacy_pixel_consensus",
        )

    def test_fallback_multi_branch_preserves_every_validated_fanout_edge(self):
        image = self.np.full((220, 760, 3), 255, dtype=self.np.uint8)
        self.cv2.line(image, (10, 200), (40, 200), (20, 20, 20), 2)
        boxes = {
            "ROOT": (300.0, 30.0, 60.0, 20.0),
            "LEAF-1": (100.0, 150.0, 60.0, 20.0),
            "LEAF-2": (520.0, 150.0, 60.0, 20.0),
        }
        spans, nodes = self._nodes(boxes)

        with (
            patch.object(
                self.backend,
                "_component_connector_pairs",
                side_effect=(
                    (("LEAF-1", "LEAF-2", False),),
                    (
                        ("ROOT", "LEAF-1", True),
                        ("ROOT", "LEAF-2", True),
                    ),
                ),
            ),
            patch.object(
                self.backend,
                "_segment_connector_pairs",
                side_effect=((), ()),
            ),
            patch.object(
                self.backend,
                "_corridor_connector_pairs",
                side_effect=((), ()),
            ),
        ):
            evidence = self.backend.analyze_connectors(
                self._frame(image),
                spans=spans,
                diagram_nodes=nodes,
                diagram_bottom=float(image.shape[0]),
            )

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {
                frozenset(("LEAF-1", "LEAF-2")),
                frozenset(("ROOT", "LEAF-1")),
                frozenset(("ROOT", "LEAF-2")),
            },
        )

    def test_component_only_fallback_bridges_subgraphs_without_adding_a_cycle(self):
        image = self.np.full((220, 800, 3), 255, dtype=self.np.uint8)
        self.cv2.line(image, (10, 200), (40, 200), (20, 20, 20), 2)
        boxes = {
            "A": (40.0, 90.0, 40.0, 20.0),
            "B": (180.0, 90.0, 40.0, 20.0),
            "C": (440.0, 90.0, 40.0, 20.0),
            "D": (580.0, 90.0, 40.0, 20.0),
        }
        spans, nodes = self._nodes(boxes)

        with (
            patch.object(
                self.backend,
                "_component_connector_pairs",
                side_effect=(
                    (("A", "B", False), ("C", "D", False)),
                    (("B", "C", False), ("A", "D", False)),
                ),
            ),
            patch.object(
                self.backend,
                "_segment_connector_pairs",
                side_effect=((), ()),
            ),
            patch.object(
                self.backend,
                "_corridor_connector_pairs",
                side_effect=((), ()),
            ),
        ):
            evidence = self.backend.analyze_connectors(
                self._frame(image),
                spans=spans,
                diagram_nodes=nodes,
                diagram_bottom=float(image.shape[0]),
            )

        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {
                frozenset(("A", "B")),
                frozenset(("B", "C")),
                frozenset(("C", "D")),
            },
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

    def test_low_confidence_numbered_device_remains_a_relation_endpoint(self):
        image = self.np.full((220, 700, 3), 255, dtype=self.np.uint8)
        boxes = {
            "ACC-001": (40.0, 100.0, 60.0, 20.0),
            "ACC-002": (280.0, 100.0, 60.0, 20.0),
            "ACC-003": (520.0, 100.0, 60.0, 20.0),
        }
        self._solid_edge(
            image,
            boxes["ACC-001"],
            boxes["ACC-002"],
            (20, 20, 20),
            thickness=3,
        )
        self._solid_edge(
            image,
            boxes["ACC-002"],
            boxes["ACC-003"],
            (20, 20, 20),
            thickness=3,
        )
        spans, nodes = self._nodes(boxes)
        middle = nodes["ACC-002"]
        nodes["ACC-002"] = DeviceOccurrence(
            business_id=middle.business_id,
            prefix=middle.prefix,
            confidence=0.70,
            bbox=middle.bbox,
            raw_text=middle.raw_text,
            span_index=middle.span_index,
            corrected_ocr=True,
        )
        spans = tuple(
            OCRSpan(span.text, 0.70, span.bbox)
            if span.text == "ACC-002"
            else span
            for span in spans
        )

        evidence = self.backend.analyze_connectors(
            self._frame(image),
            spans=spans,
            diagram_nodes=nodes,
            diagram_bottom=float(image.shape[0]),
        )

        self.assertEqual(evidence.pass_through_nodes, frozenset())
        self.assertEqual(
            {
                frozenset((connector.source, connector.target))
                for connector in evidence.connectors
            },
            {
                frozenset(("ACC-001", "ACC-002")),
                frozenset(("ACC-002", "ACC-003")),
            },
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
