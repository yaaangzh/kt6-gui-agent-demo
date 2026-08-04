from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

from kt6_backend.page_perception import PagePerceptionService, SQLitePageCaptureStore
from kt6_backend.perception_runtime import PerceptionRuntime
from kt6_backend.vision_cache_coordinator import VisionCacheCoordinator
from kt6_backend.vision_result_cache import SQLiteVisionResultCacheStore


ONE_PIXEL_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2ZQAAAABJRU5ErkJggg=="
)


class CountingVisionAdapter:
    adapter_id = "counting-vision"
    adapter_version = "1.0"
    supports_actionable_grounding = False

    def __init__(self) -> None:
        self.calls = 0

    def recognize(self, *, page, frames):
        self.calls += 1
        return {
            "objects": [
                {
                    "business_id": "ap-1",
                    "type": "ap",
                    "label": "AP1",
                    "bbox": [0, 0, 1, 1],
                    "canvas_id": frames[0].canvas_id,
                    "confidence": 0.95,
                }
            ],
            "links": [],
        }


def capture_payload() -> dict:
    return {
        "page": {
            "url": "https://example.invalid/topology",
            "title": "Topology",
            "language": "zh-CN",
            "ui_version": "cache-test-v1",
            "viewport": {
                "width": 1280,
                "height": 720,
                "device_pixel_ratio": 1,
            },
        },
        "dom": {
            "elements": [
                {
                    "ref": "#refresh",
                    "selector": "#refresh",
                    "tag": "button",
                    "role": "button",
                    "label": "刷新",
                    "bbox": [10, 10, 80, 30],
                    "actionable": True,
                }
            ]
        },
        "canvases": [
            {
                "canvas_id": "topology",
                "width": 1,
                "height": 1,
                "client_width": 1,
                "client_height": 1,
                "bbox": [0, 50, 1, 1],
                "source_kind": "canvas",
                "source_type": "canvas",
                "capture_kind": "element",
                "region_selector": "#topology",
                "roi_status": "verified",
                "data_url": ONE_PIXEL_PNG,
            }
        ],
        "adapter_scene": None,
    }


class PagePerceptionVisionCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.capture_store = SQLitePageCaptureStore(
            root / "captures.sqlite3", root / "assets"
        )
        self.adapter = CountingVisionAdapter()
        coordinator = VisionCacheCoordinator(
            SQLiteVisionResultCacheStore(root / "vision-cache.sqlite3"),
            asset_root=self.capture_store.asset_dir,
        )
        self.service = PagePerceptionService(
            self.capture_store,
            PerceptionRuntime(),
            canvas_vision=self.adapter,
            vision_cache_coordinator=coordinator,
        )

    def test_same_pixels_skip_vision_but_dom_is_recollected(self):
        first_payload = capture_payload()
        second_payload = copy.deepcopy(first_payload)
        second_payload["dom"]["elements"].append(
            {
                "ref": "#new-button",
                "selector": "#new-button",
                "tag": "button",
                "role": "button",
                "label": "新按钮",
                "bbox": [100, 10, 80, 30],
                "actionable": True,
            }
        )

        first = self.service.ingest(first_payload)
        second = self.service.ingest(second_payload)

        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(first["summary"]["vision_cache_status"], "miss")
        self.assertEqual(second["summary"]["vision_cache_status"], "exact_hit")
        self.assertEqual(second["summary"]["dom_element_count"], 2)
        self.assertEqual(second["scene"]["vision_cache"]["status"], "exact_hit")
        self.assertFalse(second["scene"]["actionable_grounding"])

    def test_force_refresh_bypasses_visual_cache(self):
        payload = capture_payload()
        self.service.ingest(payload)
        forced = copy.deepcopy(payload)
        forced["force_vision_refresh"] = True

        result = self.service.ingest(forced)

        self.assertEqual(self.adapter.calls, 2)
        self.assertEqual(result["summary"]["vision_cache_status"], "miss")


if __name__ == "__main__":
    unittest.main()
