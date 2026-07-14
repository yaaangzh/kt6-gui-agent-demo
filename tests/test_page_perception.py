import copy
from pathlib import Path
import tempfile
import unittest

from kt6_backend.page_perception import PagePerceptionService, SQLitePageCaptureStore
from kt6_backend.perception_runtime import PerceptionRuntime
from kt6_backend.playbook_loader import PlaybookLoader
from kt6_backend.runtime import KT6Runtime
from kt6_backend.topology_text_recognizer import TopologyTextRecognizer
from kt6_backend.tools import MockBusinessTools

from tests.test_runtime import wait_for_state


ONE_PIXEL_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2ZQAAAABJRU5ErkJggg=="
)
TOPOLOGY_TEXT_FIXTURE = Path(__file__).parent / "fixtures" / "enterprise_topology_ocr.txt"


class RecordingCanvasVisionAdapter:
    adapter_id = "recording-vision"
    adapter_version = "1.0"
    supports_actionable_grounding = True

    def __init__(self):
        self.calls = []

    def recognize(self, *, page, frames):
        self.calls.append({"page": page, "frames": frames})
        return {
            "objects": [
                {
                    "business_id": "gw_001",
                    "type": "gateway",
                    "label": "GW-001",
                    "bbox": [10, 20, 80, 40],
                    "confidence": 0.94,
                },
                {
                    "business_id": "core_001",
                    "type": "core",
                    "label": "CORE-001",
                    "bbox": [10, 100, 80, 40],
                    "confidence": 0.91,
                },
            ],
            "links": [
                {
                    "relation_id": "vision-gw-core",
                    "source": "gw_001",
                    "target": "core_001",
                    "type": "uplink",
                }
            ],
        }


class FailingCanvasVisionAdapter:
    adapter_id = "failing-vision"
    adapter_version = "1.0"

    def recognize(self, *, page, frames):
        raise RuntimeError("vision backend unavailable")


class UngroundedCanvasVisionAdapter:
    adapter_id = "ungrounded-vision"
    adapter_version = "1.0"

    def recognize(self, *, page, frames):
        return {
            "objects": [
                {
                    "business_id": "gw_001",
                    "type": "gateway",
                    "bbox": [-10, 20, 80, 40],
                    "confidence": 0.99,
                },
                {
                    "business_id": "core_001",
                    "type": "core",
                    "bbox": [10, 100, 80, 40],
                    "confidence": 0.4,
                },
            ]
        }


class AnalysisOnlyCanvasVisionAdapter(RecordingCanvasVisionAdapter):
    adapter_id = "analysis-only-vision"
    supports_actionable_grounding = False


class DanglingCanvasVisionAdapter(RecordingCanvasVisionAdapter):
    adapter_id = "dangling-vision"

    def recognize(self, *, page, frames):
        result = super().recognize(page=page, frames=frames)
        result["links"][0]["target"] = "missing_001"
        return result


def live_capture_payload() -> dict:
    return {
        "page": {
            "url": "http://127.0.0.1:8787/",
            "title": "KT6",
            "language": "zh-CN",
            "ui_version": "test-live-v1",
            "viewport": {"width": 1280, "height": 720, "device_pixel_ratio": 1},
        },
        "dom": {
            "elements": [
                {
                    "ref": "#topology-canvas",
                    "tag": "canvas",
                    "role": "img",
                    "label": "网络拓扑",
                    "aria_label": "不规则 canvas 网络拓扑画布",
                    "bbox": [20, 100, 800, 600],
                }
            ]
        },
        "canvases": [
            {
                "canvas_id": "topology-canvas",
                "width": 1400,
                "height": 900,
                "client_width": 800,
                "client_height": 600,
                "bbox": [20, 100, 800, 600],
                "data_url": ONE_PIXEL_PNG,
            }
        ],
        "adapter_scene": {
            "ui_version": "test-topology-v1",
            "topology_revision": 1,
            "site": "站点1",
            "floor": "1F",
            "scene": "实时拓扑",
            "canvas": {"width": 1400, "height": 900},
            "objects": [
                {
                    "business_id": "user_zhangsan",
                    "type": "user",
                    "label": "张三",
                    "connected_ap": "ap_001",
                    "x": 420,
                    "y": 580,
                },
                {
                    "business_id": "ap_001",
                    "type": "ap",
                    "label": "AP1",
                    "channel": 149,
                    "x": 600,
                    "y": 500,
                },
            ],
            "links": [{"source": "user_zhangsan", "target": "ap_001", "type": "access"}],
            "co_channel_relations": [],
        },
    }


class PagePerceptionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.perception_runtime = PerceptionRuntime()
        self.store = SQLitePageCaptureStore(root / "captures.sqlite3", root / "assets")
        self.service = PagePerceptionService(self.store, self.perception_runtime)

    def tearDown(self):
        self.temp_dir.cleanup()

    def service_with(self, *, canvas_vision=None, text_recognizer=None):
        return PagePerceptionService(
            self.store,
            self.perception_runtime,
            canvas_vision=canvas_vision,
            text_recognizer=text_recognizer,
        )

    def topology_text(self):
        return {
            "kind": "user_provided_ascii",
            "format": "ascii_diagram_with_device_table",
            "source_id": "enterprise-topology-fixture-v1",
            "text": TOPOLOGY_TEXT_FIXTURE.read_text(encoding="utf-8"),
        }

    def test_live_capture_persists_canvas_and_reuses_scene(self):
        first = self.service.ingest(live_capture_payload())
        second = self.service.ingest(live_capture_payload())

        self.assertEqual(first["summary"]["selected_mode"], "canvas_renderer_adapter")
        self.assertEqual(first["summary"]["canvas_screenshot_count"], 1)
        self.assertEqual(first["perception_meta"]["cache_status"], "miss")
        self.assertEqual(second["perception_meta"]["cache_status"], "hit")
        self.assertEqual(first["perception_meta"]["scene_revision"], second["perception_meta"]["scene_revision"])
        self.assertNotEqual(
            first["scene"]["input"]["canvases"][0]["screenshot_path"],
            second["scene"]["input"]["canvases"][0]["screenshot_path"],
        )

        stored = self.store.get(first["capture_id"])
        screenshot_path = Path(stored["capture"]["canvases"][0]["screenshot_path"])
        self.assertTrue(screenshot_path.exists())
        self.assertGreater(screenshot_path.stat().st_size, 0)

    def test_unknown_canvas_is_marked_for_vision_model(self):
        payload = live_capture_payload()
        payload["dom"] = {"elements": []}
        payload["adapter_scene"] = None

        capture = self.service.ingest(payload)

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_screenshot_capture")
        self.assertTrue(capture["summary"]["requires_vision_model"])
        self.assertEqual(capture["scene"]["business_object_bindings"], {})
        result = self.service.get_result(capture["capture_id"])
        self.assertEqual(result["perception"]["candidates"]["text"]["mode"], "topology_text_unavailable")
        self.assertFalse(capture["scene"]["pixel_inference_performed"])

    def test_accessible_canvas_dom_does_not_hide_missing_canvas_semantics(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None

        capture = self.service.ingest(payload)
        result = self.service.get_result(capture["capture_id"])

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_screenshot_capture")
        self.assertTrue(capture["summary"]["requires_vision_model"])
        self.assertEqual(capture["scene"]["business_object_bindings"], {})
        dom_candidate = result["perception"]["candidates"]["dom"]
        self.assertEqual(dom_candidate["object_count"], 1)
        self.assertEqual(dom_candidate["elements"][0]["label"], "不规则 canvas 网络拓扑画布")

    def test_failed_canvas_capture_falls_back_to_dom_and_preserves_errors(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        frontend_failure = copy.deepcopy(payload["canvases"][0])
        frontend_failure.pop("data_url")
        frontend_failure["capture_error"] = "SecurityError: canvas is tainted"
        backend_failure = copy.deepcopy(payload["canvases"][0])
        backend_failure["canvas_id"] = "unsupported-canvas"
        backend_failure["data_url"] = "data:image/gif;base64,AAAA"
        payload["canvases"] = [frontend_failure, backend_failure]

        capture = self.service.ingest(payload)
        result = self.service.get_result(capture["capture_id"])

        self.assertEqual(capture["summary"]["selected_mode"], "live_dom_snapshot")
        self.assertFalse(capture["summary"]["requires_vision_model"])
        canvas_candidate = result["perception"]["candidates"]["canvas"]
        self.assertEqual(canvas_candidate["mode"], "canvas_capture_unavailable")
        self.assertFalse(canvas_candidate["pixel_capture_available"])
        self.assertFalse(canvas_candidate["requires_vision_model"])
        errors = [item.get("capture_error") for item in canvas_candidate["input"]["canvases"]]
        self.assertEqual(
            errors,
            ["SecurityError: canvas is tainted", "unsupported canvas data URL"],
        )
        self.assertTrue(all("screenshot_path" not in item for item in canvas_candidate["input"]["canvases"]))
        self.assertIn("截图不可用", result["perception"]["decision"]["reason"])
        self.assertTrue(any("截图不可用" in item for item in canvas_candidate["limitations"]))
        self.assertFalse(any("已捕获真实 Canvas 像素" in item for item in canvas_candidate["limitations"]))

    def test_renderer_adapter_does_not_claim_pixels_when_screenshot_failed(self):
        payload = live_capture_payload()
        payload["canvases"][0].pop("data_url")
        payload["canvases"][0]["capture_error"] = "SecurityError: canvas is tainted"

        capture = self.service.ingest(payload)
        result = self.service.get_result(capture["capture_id"])

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_renderer_adapter")
        self.assertEqual(capture["summary"]["canvas_screenshot_count"], 0)
        self.assertFalse(capture["scene"]["pixel_capture_available"])
        self.assertIn("截图不可用", result["perception"]["decision"]["reason"])
        self.assertIn("截图不可用", capture["scene"]["limitations"][0])
        self.assertNotIn("像素来自浏览器实时截图", capture["scene"]["limitations"][0])

    def test_provided_topology_text_reconstructs_semantics_without_pixel_claims(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        payload["canvases"] = []
        payload["topology_text"] = self.topology_text()
        service = self.service_with(text_recognizer=TopologyTextRecognizer())

        capture = service.ingest(payload)
        result = service.get_result(capture["capture_id"])

        self.assertEqual(capture["summary"]["selected_mode"], "topology_text_reconstruction")
        self.assertEqual(capture["summary"]["semantic_source"], "provided_text")
        self.assertEqual(capture["scene"]["object_count"], 22)
        self.assertEqual(capture["scene"]["relation_count"], 19)
        self.assertFalse(capture["scene"]["pixel_inference_performed"])
        self.assertFalse(capture["scene"]["pixel_verified"])
        self.assertFalse(capture["scene"]["actionable_grounding"])
        self.assertFalse(capture["scene"]["usable_for_actions"])
        self.assertEqual(
            set(capture["scene"]["semantic_tree"]["orphans"]),
            {"agg_003", "ap_007"},
        )
        self.assertEqual(capture["scene"]["provenance"]["semantic_source"], "provided_text")
        self.assertEqual(
            capture["scene"]["provenance"]["recognizer_version"],
            TopologyTextRecognizer.recognizer_version,
        )
        self.assertNotIn("text", result["perception"]["raw_scenes"]["text"])
        self.assertIn("text_sha256", result["perception"]["raw_scenes"]["text"])
        self.assertTrue(any("未读取 Canvas 截图像素" in item for item in capture["scene"]["limitations"]))

    def test_screenshot_with_provided_text_remains_text_not_pixel_recognition(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        payload["topology_text"] = self.topology_text()
        service = self.service_with(text_recognizer=TopologyTextRecognizer())

        capture = service.ingest(payload)

        self.assertEqual(capture["summary"]["canvas_screenshot_count"], 1)
        self.assertEqual(capture["summary"]["selected_mode"], "topology_text_reconstruction")
        self.assertEqual(capture["scene"]["provenance"]["semantic_source"], "provided_text")
        self.assertFalse(capture["scene"]["provenance"]["pixel_inference_performed"])
        self.assertFalse(capture["scene"]["provenance"]["pixel_verified"])
        self.assertFalse(capture["scene"]["provenance"]["actionable_grounding"])
        self.assertTrue(capture["summary"]["requires_vision_model"])

    def test_incomplete_topology_text_is_not_selected_as_partial_semantics(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        payload["canvases"] = []
        observation = self.topology_text()
        lines = observation["text"].splitlines()
        last_content_row = next(index for index, line in enumerate(lines) if "│ AP-007" in line)
        observation["text"] = "\n".join(lines[: last_content_row + 1])
        payload["topology_text"] = observation
        service = self.service_with(text_recognizer=TopologyTextRecognizer())

        capture = service.ingest(payload)
        result = service.get_result(capture["capture_id"])
        text_candidate = result["perception"]["candidates"]["text"]

        self.assertNotEqual(capture["summary"]["selected_mode"], "topology_text_reconstruction")
        self.assertEqual(text_candidate["mode"], "topology_text_unavailable")
        self.assertIn(
            "incomplete_device_table",
            {issue["code"] for issue in text_candidate["recognition_issues"]},
        )
        self.assertFalse(text_candidate["actionable_grounding"])

    def test_semantically_equivalent_text_reuses_scene_revision(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        payload["canvases"] = []
        payload["topology_text"] = self.topology_text()
        service = self.service_with(text_recognizer=TopologyTextRecognizer())

        first = service.ingest(payload)
        variant = copy.deepcopy(payload)
        variant["topology_text"]["text"] = "\r\n".join(
            f"        {line}" for line in payload["topology_text"]["text"].splitlines()
        )
        second = service.ingest(variant)

        self.assertEqual(first["perception_meta"]["cache_status"], "miss")
        self.assertEqual(second["perception_meta"]["cache_status"], "hit")
        self.assertEqual(
            first["perception_meta"]["scene_revision"],
            second["perception_meta"]["scene_revision"],
        )

    def test_canvas_vision_adapter_receives_persisted_frames_and_stamps_provenance(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        adapter = RecordingCanvasVisionAdapter()
        service = self.service_with(canvas_vision=adapter)

        capture = service.ingest(payload)

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_vision_adapter")
        self.assertEqual(len(adapter.calls), 1)
        frame = adapter.calls[0]["frames"][0]
        self.assertTrue(frame.screenshot_path.exists())
        self.assertGreater(frame.screenshot_path.stat().st_size, 0)
        self.assertEqual(capture["scene"]["provenance"]["adapter_id"], "recording-vision")
        self.assertEqual(capture["scene"]["provenance"]["adapter_version"], "1.0")
        self.assertEqual(
            capture["scene"]["provenance"]["screenshot_sha256"],
            [frame.screenshot_sha256],
        )
        self.assertTrue(capture["scene"]["pixel_inference_performed"])
        self.assertTrue(capture["scene"]["pixel_verified"])
        self.assertTrue(capture["scene"]["actionable_grounding"])
        self.assertEqual(capture["scene"]["semantic_tree"]["roots"], ["gw_001"])
        self.assertEqual(
            capture["scene"]["semantic_tree"]["nodes"]["gw_001"]["children"],
            [
                {
                    "target": "core_001",
                    "relation_id": "vision-gw-core",
                    "type": "uplink",
                }
            ],
        )

    def test_canvas_vision_failure_falls_back_without_losing_error_or_pixels(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        service = self.service_with(canvas_vision=FailingCanvasVisionAdapter())

        capture = service.ingest(payload)

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_screenshot_capture")
        self.assertEqual(capture["summary"]["canvas_screenshot_count"], 1)
        self.assertTrue(capture["summary"]["requires_vision_model"])
        self.assertIn("RuntimeError: vision backend unavailable", capture["scene"]["vision_error"])
        self.assertFalse(capture["scene"]["pixel_inference_performed"])
        self.assertFalse(capture["scene"]["pixel_verified"])

    def test_canvas_vision_dangling_relation_fails_closed(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        service = self.service_with(canvas_vision=DanglingCanvasVisionAdapter())

        capture = service.ingest(payload)

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_screenshot_capture")
        self.assertIn("dangling endpoint", capture["scene"]["vision_error"])
        self.assertFalse(capture["scene"]["actionable_grounding"])

    def test_canvas_vision_is_analysis_only_without_inventory_binding_capability(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        service = self.service_with(canvas_vision=AnalysisOnlyCanvasVisionAdapter())

        capture = service.ingest(payload)

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_vision_adapter")
        self.assertTrue(capture["scene"]["pixel_inference_performed"])
        self.assertFalse(capture["scene"]["actionable_grounding"])
        self.assertFalse(
            capture["scene"]["provenance"]["adapter_supports_actionable_grounding"]
        )
        self.assertTrue(
            all(
                binding["actionable"] is False
                for binding in capture["scene"]["business_object_bindings"].values()
            )
        )

    def test_canvas_vision_requires_valid_geometry_and_confidence_for_actions(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        service = self.service_with(canvas_vision=UngroundedCanvasVisionAdapter())

        capture = service.ingest(payload)

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_vision_adapter")
        self.assertTrue(capture["scene"]["pixel_inference_performed"])
        self.assertTrue(capture["scene"]["pixel_verified"])
        self.assertFalse(capture["scene"]["actionable_grounding"])
        self.assertTrue(
            all(
                binding["actionable"] is False
                for binding in capture["scene"]["business_object_bindings"].values()
            )
        )

    def test_topology_text_payload_is_restricted_and_bounded(self):
        payload = live_capture_payload()
        payload["topology_text"] = {**self.topology_text(), "provenance": "spoofed"}
        with self.assertRaisesRegex(ValueError, "unsupported topology_text fields"):
            self.service.ingest(payload)

        payload["topology_text"] = self.topology_text()
        payload["topology_text"]["text"] = "x" * 100_001
        with self.assertRaisesRegex(ValueError, "exceeds 100000 characters"):
            self.service.ingest(payload)

    def test_dom_like_semantic_tree_keeps_non_tree_relations(self):
        elements = [
            {
                "element_id": f"node_{business_id}",
                "business_id": business_id,
                "type": "device",
                "label": business_id.upper(),
                "bbox": [0, 0, 10, 10],
                "confidence": 0.9,
            }
            for business_id in ("a", "b", "c")
        ]
        relations = [
            {"relation_id": "ab", "source": "a", "target": "b", "type": "link"},
            {"relation_id": "ac", "source": "a", "target": "c", "type": "link"},
            {"relation_id": "cb", "source": "c", "target": "b", "type": "link"},
        ]

        tree = self.service._semantic_tree(elements, relations)

        self.assertEqual(tree["roots"], ["a"])
        self.assertEqual(tree["orphans"], [])
        self.assertTrue(tree["complete"])
        self.assertEqual(
            tree["non_tree_relations"],
            [
                {
                    "source": "c",
                    "target": "b",
                    "relation_id": "cb",
                    "type": "link",
                }
            ],
        )
        self.assertEqual(len(relations), 3)

    def test_runtime_uses_live_page_capture_and_detects_movement(self):
        first = self.service.ingest(live_capture_payload())
        tools = MockBusinessTools(
            Path("data"),
            perception_runtime=self.perception_runtime,
            page_perception=self.service,
        )
        runtime = KT6Runtime(tools, PlaybookLoader(Path("playbooks")), event_delay=0)
        task = runtime.create_task(
            "用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因",
            page_capture_id=first["capture_id"],
        )
        task = wait_for_state(runtime, task.task_id, "waiting_user")

        self.assertEqual(task.context["ui_perception"]["mode"], "canvas_renderer_adapter")
        self.assertEqual(task.context["scene_ref"]["page_capture_id"], first["capture_id"])

        changed_payload = copy.deepcopy(live_capture_payload())
        changed_payload["adapter_scene"]["objects"][1]["x"] += 30
        second = self.service.ingest(changed_payload)
        accepted = runtime.execute_action(
            task.task_id,
            "execute_solution",
            {"solution_id": "rf_optimization", "page_capture_id": second["capture_id"]},
        )
        self.assertTrue(accepted)
        task = wait_for_state(runtime, task.task_id, "completed")
        self.assertTrue(any(event.type == "topology_changed" for event in task.events))


if __name__ == "__main__":
    unittest.main()
