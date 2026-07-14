import copy
from pathlib import Path
import tempfile
import unittest

from kt6_backend.page_perception import PagePerceptionService, SQLitePageCaptureStore
from kt6_backend.perception_runtime import PerceptionRuntime
from kt6_backend.playbook_loader import PlaybookLoader
from kt6_backend.runtime import KT6Runtime
from kt6_backend.tools import MockBusinessTools

from tests.test_runtime import wait_for_state


ONE_PIXEL_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2ZQAAAABJRU5ErkJggg=="
)


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
