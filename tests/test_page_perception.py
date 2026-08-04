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


class FusionMetadataCanvasVisionAdapter(RecordingCanvasVisionAdapter):
    adapter_id = "fusion-metadata-vision"
    supports_actionable_grounding = False

    def recognize(self, *, page, frames):
        result = super().recognize(page=page, frames=frames)
        result["fusion_summary"] = {
            "confirmed_object_count": 2,
            "confirmed_link_count": 1,
        }
        result["fusion_analysis"] = {
            "structure_templates": [
                {
                    "template_id": "layers-1",
                    "type": "layered",
                    "layers": [{"name": "核心层", "members": ["gw_001"]}],
                }
            ],
            "rejected_links": [],
            "unlocated_objects": [],
            "unresolved_links": [],
        }
        return result


class DanglingCanvasVisionAdapter(RecordingCanvasVisionAdapter):
    adapter_id = "dangling-vision"

    def recognize(self, *, page, frames):
        result = super().recognize(page=page, frames=frames)
        result["links"][0]["target"] = "missing_001"
        return result


class RoutingCanvasVisionAdapter(RecordingCanvasVisionAdapter):
    adapter_id = "routing-vision"
    supports_actionable_grounding = False

    def recognize(self, *, page, frames):
        result = super().recognize(page=page, frames=frames)
        result["vision_routing"] = {
            "decision": "model_assist",
            "scene_type": "complex_topology",
            "effective_profile": "visible_topology",
            "reason_codes": ["cv_connectivity_incomplete"],
        }
        return result


class SpoofingCanvasVisionAdapter(RecordingCanvasVisionAdapter):
    adapter_id = "spoofing-vision"
    supports_actionable_grounding = True

    def recognize(self, *, page, frames):
        result = super().recognize(page=page, frames=frames)
        item = result["objects"][0]
        item["actionable"] = True
        item["safe_for_execution"] = True
        item["source"] = {"kind": "dom"}
        item["interaction"] = {"can_click_now": True}
        item["attributes"] = {
            "safe_for_execution": True,
            "interaction_eligible": True,
            "vendor": "Huawei",
        }
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

    def test_perception_decision_describes_all_evidence_channel_combinations(self):
        cases = (
            ("empty", False, False, False),
            ("dom_only", True, False, False),
            ("page_api_only", False, True, False),
            ("canvas_only", False, False, True),
            ("dom_and_page_api", True, True, False),
            ("dom_and_canvas", True, False, True),
            ("page_api_and_canvas", False, True, True),
            ("dom_and_page_api_and_canvas", True, True, True),
        )
        for expected, include_dom, include_page_api, include_canvas in cases:
            with self.subTest(expected=expected):
                payload = live_capture_payload()
                if not include_dom:
                    payload["dom"] = {"elements": []}
                if include_page_api:
                    payload["adapter_scene"]["source_metadata"] = {
                        "source_type": "explicit_page_adapter",
                        "adapter_id": "test-page-adapter",
                        "adapter_version": "1.0",
                        "snapshot_complete": True,
                    }
                else:
                    payload["adapter_scene"] = None
                if not include_canvas:
                    payload["canvases"] = []

                capture = self.service.ingest(payload)
                result = self.service.get_result(capture["capture_id"])

                self.assertEqual(
                    capture["summary"]["perception_decision"],
                    expected,
                )
                self.assertEqual(
                    result["perception"]["decision"]["perception_decision"],
                    expected,
                )

    def test_svg_element_texts_are_retained_as_non_binding_evidence(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        payload["canvases"] = []
        payload["svg_element_texts"] = [
            {
                "text": "  CSG1  ",
                "bbox": [10.125, 20, 80, 30],
                "selector": "#topology svg text:nth-of-type(1)",
                "frame_id": 0,
                "frame_url": "https://nce.example/topology",
                "document_id": "document-svg-1",
                "business_id": "must_not_become_a_binding",
                "actionable": True,
            },
            "  MW  ",
            {"text": "AP1", "bbox": ["invalid", 0, 10, 10]},
            {"text": "   "},
            123,
        ]

        capture = self.service.ingest(payload)
        result = self.service.get_result(capture["capture_id"])
        stored = self.store.get(capture["capture_id"])
        expected = [
            {
                "text": "CSG1",
                "bbox": [10.12, 20.0, 80.0, 30.0],
                "selector": "#topology svg text:nth-of-type(1)",
                "frame_id": "0",
                "frame_url": "https://nce.example/topology",
                "document_id": "document-svg-1",
            },
            "MW",
            {"text": "AP1"},
        ]

        self.assertEqual(
            result["perception"]["raw_scenes"]["svg_element_texts"],
            expected,
        )
        self.assertEqual(stored["capture"]["svg_element_texts"], expected)
        self.assertEqual(capture["summary"]["perception_decision"], "empty")
        self.assertEqual(capture["scene"]["business_object_bindings"], {})
        self.assertEqual(capture["dom_action_bindings"], {})
        self.assertNotIn(
            "business_id",
            result["perception"]["raw_scenes"]["svg_element_texts"][0],
        )
        self.assertNotIn(
            "actionable",
            result["perception"]["raw_scenes"]["svg_element_texts"][0],
        )

    def test_mixed_dom_assets_and_canvas_vision_keep_both_perception_paths(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {
            "elements": [
                {
                    "ref": "frame:0:#ap-1",
                    "selector": "#ap-1",
                    "tag": "div",
                    "label": "AP1",
                    "business_id": "ap_001",
                    "asset_id": "asset-ap-001",
                    "bbox": [20, 20, 120, 40],
                    "frame_id": "0",
                    "frame_url": "https://nce.example/topology",
                    "document_id": "document-1",
                },
                {
                    "ref": "frame:0:#shutdown-ap-1",
                    "selector": "#shutdown-ap-1",
                    "parent_ref": "frame:0:#ap-1",
                    "tag": "button",
                    "role": "button",
                    "label": "关闭 AP1",
                    "actionable": True,
                    "action_id": "ap.shutdown",
                    "owner_business_id": "ap_001",
                    "bbox": [20, 70, 120, 40],
                    "frame_id": "0",
                    "frame_url": "https://nce.example/topology",
                    "document_id": "document-1",
                },
            ]
        }
        adapter = RecordingCanvasVisionAdapter()
        service = self.service_with(canvas_vision=adapter)

        capture = service.ingest(payload)
        result = service.get_result(capture["capture_id"])
        topology = service.get_topology(capture["capture_id"])

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_vision_adapter")
        self.assertEqual(capture["summary"]["perception_decision"], "dom_and_canvas")
        self.assertEqual(capture["canvas_perception"]["mode"], "canvas_vision_adapter")
        self.assertEqual(capture["dom_perception"]["mode"], "live_dom_snapshot")
        self.assertEqual(
            result["perception"]["dom_perception"], capture["dom_perception"]
        )
        self.assertEqual(topology["dom_perception"], capture["dom_perception"])
        self.assertIn("gw_001", capture["scene"]["business_object_bindings"])
        self.assertIn("ap_001", capture["dom_business_object_bindings"])
        self.assertIn("frame:0:#shutdown-ap-1", capture["dom_action_bindings"])
        self.assertEqual(
            result["perception"]["dom_business_object_bindings"]["ap_001"][
                "binding_status"
            ],
            "observed",
        )
        self.assertEqual(
            topology["canvas_perception"]["mode"],
            "canvas_vision_adapter",
        )
        self.assertIn("ap_001", topology["dom_business_object_bindings"])

    def test_verified_visible_tab_crop_remains_analysis_only(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        payload["canvases"][0].update(
            {
                "source_kind": "graphic_container",
                "source_type": "",
                "capture_kind": "visible_tab",
                "capture_method": "capture_visible_tab_crop",
                "roi_status": "verified",
            }
        )
        service = self.service_with(canvas_vision=RecordingCanvasVisionAdapter())

        capture = service.ingest(payload)

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_vision_adapter")
        self.assertFalse(capture["scene"]["actionable_grounding"])
        self.assertFalse(
            capture["scene"]["provenance"]["source_allows_actionable_grounding"]
        )

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

    def test_dom_actions_remain_available_when_canvas_needs_vision(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {
            "elements": [
                {
                    "ref": "frame:0:#resource-menu",
                    "selector": "#resource-menu",
                    "tag": "button",
                    "role": "button",
                    "label": "资源管理",
                    "bbox": [20, 20, 120, 40],
                    "actionable": True,
                    "frame_id": "0",
                    "frame_url": "https://nce.example/portal",
                    "document_id": "document-main",
                }
            ]
        }

        capture = self.service.ingest(payload)
        topology = self.service.get_topology(capture["capture_id"])

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_screenshot_capture")
        binding = capture["dom_action_bindings"]["frame:0:#resource-menu"]
        self.assertEqual(binding["dom_ref"], "#resource-menu")
        self.assertEqual(
            topology["dom_action_bindings"],
            capture["dom_action_bindings"],
        )

    def test_dom_scene_builds_browser_hierarchy_without_topology_relations(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["canvases"] = []
        payload["dom"] = {
            "elements": [
                {
                    "ref": "#panel",
                    "parent_ref": "",
                    "depth": 1,
                    "document_order": 0,
                    "tag": "section",
                    "role": "region",
                    "label": "设备面板",
                    "bbox": [10, 10, 300, 200],
                },
                {
                    "ref": "#later-child",
                    "parent_ref": "#panel",
                    "depth": 3,
                    "document_order": 2,
                    "tag": "button",
                    "label": "稍后按钮",
                    "bbox": [30, 80, 80, 30],
                },
                {
                    "ref": "#earlier-child",
                    "parent_ref": "#panel",
                    "depth": 2,
                    "document_order": 1,
                    "tag": "div",
                    "role": "status",
                    "label": "状态",
                    "bbox": [30, 40, 80, 30],
                },
            ]
        }

        capture = self.service.ingest(payload)
        tree = capture["scene"]["ui_tree"]

        self.assertEqual(capture["summary"]["selected_mode"], "live_dom_snapshot")
        self.assertEqual(tree["tree_type"], "browser_dom_hierarchy")
        self.assertEqual(tree["roots"], ["#panel"])
        self.assertEqual(
            tree["nodes"]["#panel"]["children"],
            ["#earlier-child", "#later-child"],
        )
        self.assertEqual(tree["nodes"]["#earlier-child"]["parent_ref"], "#panel")
        self.assertTrue(tree["complete"])
        self.assertEqual(tree["issues"], [])
        self.assertEqual(capture["scene"]["relations"], [])
        self.assertEqual(capture["scene"]["relation_count"], 0)

        stored = self.store.get(capture["capture_id"])["capture"]["dom"]["elements"]
        self.assertEqual(stored[2]["parent_ref"], "#panel")
        self.assertEqual(stored[2]["depth"], 2)
        self.assertEqual(stored[2]["document_order"], 1)

    def test_dom_projection_preserves_coverage_and_frame_scoped_hierarchy(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["canvases"] = []
        payload["dom"] = {
            "stats": {
                "frame_count": 2,
                "scanned_element_count": 300,
                "captured_element_count": 3,
                "truncated": False,
                "unknown": "discarded",
            },
            "coverage": {
                "source_complete": False,
                "compressed": True,
                "omitted_ancestor_count": 2,
            },
            "elements": [
                {
                    "ref": "frame:0:#panel",
                    "frame_id": "0",
                    "frame_url": "https://nce.example/main",
                    "document_id": "doc-0",
                    "parent_relation": "root",
                    "bbox": [0, 0, 100, 100],
                },
                {
                    "ref": "frame:0:#button",
                    "parent_ref": "frame:0:#panel",
                    "parent_relation": "nearest_captured_ancestor",
                    "omitted_ancestor_count": 2,
                    "frame_id": "0",
                    "frame_url": "https://nce.example/main",
                    "document_id": "doc-0",
                    "bbox": [10, 10, 20, 20],
                },
                {
                    "ref": "frame:7:#panel",
                    "frame_id": "7",
                    "frame_url": "https://nce.example/frame",
                    "document_id": "doc-7",
                    "bbox": [0, 0, 100, 100],
                },
            ],
        }

        capture = self.service.ingest(payload)
        result = self.service.get_result(capture["capture_id"])
        topology = self.service.get_topology(capture["capture_id"])
        dom = capture["dom_perception"]
        tree = dom["ui_tree"]

        self.assertEqual(capture["summary"]["dom_stats"]["frame_count"], 2)
        self.assertNotIn("unknown", capture["summary"]["dom_stats"])
        self.assertEqual(dom["coverage"]["tree_scope"], "semantic_projection")
        self.assertFalse(tree["source_complete"])
        self.assertTrue(tree["action_binding_complete"])
        self.assertTrue(tree["compressed"])
        self.assertTrue(tree["graph_consistent"])
        self.assertFalse(tree["complete"])
        self.assertEqual(len(tree["frame_roots"]), 2)
        parent = tree["nodes"]["frame:0:#panel"]
        child = tree["nodes"]["frame:0:#button"]
        self.assertEqual(child["parent_element_id"], parent["element_id"])
        self.assertEqual(parent["child_element_ids"], [child["element_id"]])
        self.assertEqual(child["parent_relation"], "nearest_captured_ancestor")
        self.assertEqual(result["perception"]["dom_perception"], dom)
        self.assertEqual(topology["dom_perception"], dom)

    def test_dom_frame_collection_error_blocks_action_binding_completeness(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["canvases"] = []
        payload["dom"]["stats"] = {
            "frame_collection_error": "cross-origin frame unavailable"
        }

        capture = self.service.ingest(payload)

        self.assertFalse(
            capture["dom_perception"]["coverage"]["action_binding_complete"]
        )

    def test_browser_extension_dom_preserves_frame_selector_and_action_binding(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["canvases"] = []
        payload["dom"] = {
            "elements": [
                {
                    "ref": "frame:7:@capture:1",
                    "selector": "div[aria-label=\"Network Digital Map\"]",
                    "parent_ref": "",
                    "depth": 4,
                    "document_order": 0,
                    "tag": "div",
                    "role": "button",
                    "label": "Network Digital Map",
                    "aria_label": "Network Digital Map",
                    "bbox": [300, 200, 180, 96],
                    "disabled": False,
                    "checked": False,
                    "actionable": True,
                    "frame_id": "7",
                    "frame_url": "https://nce.example/portal",
                    "document_id": "document-7",
                }
            ]
        }

        capture = self.service.ingest(payload)
        topology = self.service.get_topology(capture["capture_id"])
        result = self.service.get_result(capture["capture_id"])
        scene = capture["scene"]

        self.assertEqual(capture["summary"]["selected_mode"], "live_dom_snapshot")
        self.assertEqual(capture["summary"]["dom_actionable_element_count"], 1)
        self.assertEqual(scene["elements"][0]["frame_id"], "7")
        self.assertEqual(
            scene["elements"][0]["selector"],
            "div[aria-label=\"Network Digital Map\"]",
        )
        self.assertEqual(
            scene["ui_tree"]["roots"],
            ["frame:7:@capture:1"],
        )
        binding = scene["dom_action_bindings"]["frame:7:@capture:1"]
        self.assertEqual(binding["frame_id"], "7")
        self.assertEqual(binding["document_id"], "document-7")
        self.assertEqual(
            result["perception"]["dom_action_bindings"],
            scene["dom_action_bindings"],
        )
        self.assertEqual(
            capture["dom_action_bindings"],
            scene["dom_action_bindings"],
        )
        self.assertEqual(topology["dom_action_bindings"], scene["dom_action_bindings"])
        self.assertFalse(scene["actionable_grounding"])
        element = scene["elements"][0]
        self.assertEqual(element["source"]["kind"], "dom")
        self.assertEqual(element["interaction"]["status"], "preflight_required")
        self.assertFalse(element["interaction"]["can_click_now"])
        self.assertTrue(element["interaction_eligible"])

    def test_dom_business_id_stays_observed_until_asset_action_preflight(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["canvases"] = []
        payload["dom"] = {
            "elements": [
                {
                    "ref": "frame:0:#ap-1-row",
                    "selector": "#ap-1-row",
                    "tag": "div",
                    "role": "row",
                    "label": "AP1",
                    "business_id": "ap_001",
                    "business_type": "ap",
                    "asset_id": "ap_001",
                    "management_ip": "10.0.0.1",
                    "serial_number": "SN-001",
                    "site_id": "site-a",
                    "asset_version": 3,
                    "bbox": [20, 80, 500, 40],
                    "frame_id": "0",
                    "frame_url": "https://nce.example/devices",
                    "document_id": "document-1",
                },
                {
                    "ref": "frame:0:#shutdown-ap-1",
                    "selector": "#shutdown-ap-1",
                    "parent_ref": "frame:0:#ap-1-row",
                    "tag": "button",
                    "role": "button",
                    "label": "关闭",
                    "action_id": "ap.shutdown",
                    "owner_business_id": "ap_001",
                    "bbox": [440, 85, 60, 30],
                    "actionable": True,
                    "frame_id": "0",
                    "frame_url": "https://nce.example/devices",
                    "document_id": "document-1",
                },
            ]
        }

        capture = self.service.ingest(payload)
        scene = capture["scene"]
        business = scene["business_object_bindings"]["ap_001"]
        action = capture["dom_action_bindings"][
            "frame:0:#shutdown-ap-1"
        ]

        self.assertEqual(scene["mode"], "live_dom_snapshot")
        self.assertFalse(scene["actionable_grounding"])
        self.assertFalse(scene["execution_grounding"]["safe_for_execution"])
        self.assertEqual(business["binding_status"], "observed")
        self.assertFalse(business["safe_for_execution"])
        self.assertEqual(action["binding_status"], "observed")
        self.assertEqual(action["action_id"], "ap.shutdown")
        self.assertEqual(action["owner_business_id"], "ap_001")

        snapshot = self.service.get_action_snapshot(capture["capture_id"])
        subject = snapshot["dom"]["elements"][0]
        control = snapshot["dom"]["elements"][1]
        self.assertEqual(subject["management_ip"], "10.0.0.1")
        self.assertEqual(subject["serial_number"], "SN-001")
        self.assertEqual(subject["asset_version"], 3)
        self.assertEqual(control["action_id"], "ap.shutdown")
        self.assertEqual(control["owner_business_id"], "ap_001")

    def test_pseudo_ref_does_not_become_selector(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["canvases"] = []
        payload["dom"] = {
            "elements": [
                {
                    "ref": "frame:0:@capture:1",
                    "selector": "",
                    "tag": "button",
                    "role": "button",
                    "label": "关闭",
                    "action_id": "ap.shutdown",
                    "bbox": [20, 20, 60, 30],
                    "actionable": True,
                    "frame_id": "0",
                    "frame_url": "https://nce.example/devices",
                    "document_id": "document-1",
                }
            ]
        }

        capture = self.service.ingest(payload)
        snapshot = self.service.get_action_snapshot(capture["capture_id"])

        self.assertEqual(
            snapshot["dom"]["elements"][0]["ref"],
            "frame:0:@capture:1",
        )
        self.assertEqual(snapshot["dom"]["elements"][0]["selector"], "")

    def test_dom_hierarchy_records_bad_refs_without_losing_nodes(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["canvases"] = []
        payload["dom"] = {
            "elements": [
                {"ref": "", "bbox": [0, 0, 10, 10]},
                {"ref": "#same", "bbox": [20, 0, 10, 10]},
                {"ref": "#same", "bbox": [40, 0, 10, 10]},
                {
                    "ref": "#orphan",
                    "parent_ref": "#missing",
                    "bbox": [60, 0, 10, 10],
                },
                {
                    "ref": "#ambiguous-child",
                    "parent_ref": "#same",
                    "bbox": [80, 0, 10, 10],
                },
            ]
        }

        capture = self.service.ingest(payload)
        tree = capture["scene"]["ui_tree"]

        self.assertEqual(len(tree["nodes"]), 5)
        self.assertFalse(tree["complete"])
        self.assertEqual(
            {issue["code"] for issue in tree["issues"]},
            {
                "dom_ref_missing",
                "dom_ref_duplicate",
                "dom_parent_unknown",
                "dom_parent_ambiguous",
            },
        )
        self.assertIn("#orphan", tree["roots"])
        self.assertIn("#ambiguous-child", tree["roots"])
        self.assertEqual(capture["scene"]["relations"], [])

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

    def test_explicit_page_adapter_metadata_is_bounded_and_analysis_only(self):
        payload = live_capture_payload()
        payload["adapter_scene"]["source_metadata"] = {
            "source_type": "explicit_page_adapter",
            "adapter_id": "nce-topology-sdk",
            "adapter_version": "2.1",
            "frame_id": "7",
            "frame_url": "https://nce.example/topology",
            "document_id": "document-7",
            "captured_at": 123.5,
            "snapshot_complete": True,
            "safe_for_execution": True,
            "secret": "discarded",
        }
        payload["adapter_scene"]["objects"][0].update(
            {
                "actionable": True,
                "safe_for_execution": True,
                "interaction": {"can_click_now": True},
            }
        )

        capture = self.service.ingest(payload)
        scene = capture["scene"]
        metadata = capture["summary"]["adapter_source_metadata"]
        element = scene["elements"][0]

        self.assertEqual(metadata["source_type"], "explicit_page_adapter")
        self.assertEqual(metadata["adapter_id"], "nce-topology-sdk")
        self.assertFalse(metadata["safe_for_execution"])
        self.assertNotIn("secret", metadata)
        self.assertEqual(scene["source_metadata"], metadata)
        self.assertTrue(capture["summary"]["adapter_snapshot_complete"])
        self.assertEqual(element["source"]["kind"], "page_api")
        self.assertEqual(element["source"]["semantic_source"], "page_api_adapter")
        self.assertEqual(scene["provenance"]["semantic_source"], "page_api_adapter")
        self.assertTrue(scene["provenance"]["page_adapter_snapshot_complete"])
        self.assertEqual(scene["business_object_bindings"]["user_zhangsan"]["method"], "page_api_adapter")
        self.assertEqual(element["interaction"]["status"], "analysis_only")
        self.assertFalse(element["interaction"]["can_click_now"])
        self.assertFalse(element["interaction_eligible"])
        self.assertNotIn("actionable", element["attributes"])
        self.assertNotIn("safe_for_execution", element["attributes"])
        self.assertNotIn("interaction", element["attributes"])
        self.assertFalse(scene["actionable_grounding"])
        self.assertTrue(
            all(not item["actionable"] for item in scene["business_object_bindings"].values())
        )

    def test_partial_page_adapter_keeps_api_evidence_but_prefers_successful_vision(self):
        payload = live_capture_payload()
        payload["dom"] = {"elements": []}
        payload["adapter_scene"]["source_metadata"] = {
            "source_type": "explicit_page_adapter",
            "adapter_id": "nce-topology-sdk",
            "adapter_version": "2.1",
            "snapshot_complete": False,
        }
        adapter = RecordingCanvasVisionAdapter()
        service = self.service_with(canvas_vision=adapter)

        capture = service.ingest(payload)
        result = service.get_result(capture["capture_id"])

        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(capture["summary"]["selected_mode"], "canvas_vision_adapter")
        self.assertFalse(capture["summary"]["adapter_snapshot_complete"])
        self.assertEqual(capture["scene"]["provenance"]["semantic_source"], "canvas_pixels")
        page_api = capture["page_api_perception"]
        self.assertEqual(page_api["provenance"]["semantic_source"], "page_api_adapter")
        self.assertEqual(page_api["elements"][0]["source"]["kind"], "page_api")
        self.assertEqual(page_api["elements"][0]["interaction"]["status"], "analysis_only")
        self.assertFalse(page_api["actionable_grounding"])
        self.assertEqual(result["perception"]["candidates"]["page_api"], page_api)

    def test_complete_page_adapter_skips_available_vision(self):
        payload = live_capture_payload()
        payload["dom"] = {"elements": []}
        payload["adapter_scene"]["source_metadata"] = {
            "source_type": "explicit_page_adapter",
            "adapter_id": "nce-topology-sdk",
            "adapter_version": "2.1",
            "snapshot_complete": True,
        }
        adapter = RecordingCanvasVisionAdapter()
        service = self.service_with(canvas_vision=adapter)

        capture = service.ingest(payload)

        self.assertEqual(adapter.calls, [])
        self.assertEqual(capture["summary"]["selected_mode"], "canvas_renderer_adapter")
        self.assertEqual(capture["scene"]["provenance"]["semantic_source"], "page_api_adapter")
        self.assertEqual(capture["scene"]["elements"][0]["source"]["kind"], "page_api")
        self.assertFalse(capture["scene"]["actionable_grounding"])

    def test_partial_page_adapter_falls_back_when_vision_fails(self):
        payload = live_capture_payload()
        payload["dom"] = {"elements": []}
        payload["adapter_scene"]["source_metadata"] = {
            "source_type": "explicit_page_adapter",
            "snapshot_complete": False,
        }
        service = self.service_with(canvas_vision=FailingCanvasVisionAdapter())

        capture = service.ingest(payload)

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_renderer_adapter")
        self.assertEqual(capture["scene"]["vision_fallback"], "incomplete_page_api_snapshot")
        self.assertIn("RuntimeError: vision backend unavailable", capture["scene"]["vision_error"])
        self.assertTrue(capture["scene"]["requires_vision_model"])
        self.assertTrue(any("已回退到页面 API" in item for item in capture["scene"]["limitations"]))
        self.assertEqual(capture["page_api_perception"]["elements"][0]["source"]["kind"], "page_api")
        self.assertFalse(capture["scene"]["actionable_grounding"])

    def test_malformed_page_adapter_contract_is_rejected(self):
        base = copy.deepcopy(live_capture_payload()["adapter_scene"])
        invalid_entry = copy.deepcopy(base)
        invalid_entry["objects"] = ["not-an-object"]
        invalid_coordinate = copy.deepcopy(base)
        invalid_coordinate["objects"][0]["x"] = float("nan")
        invalid_dimension = copy.deepcopy(base)
        invalid_dimension["objects"][0]["width"] = 0
        invalid_canvas = copy.deepcopy(base)
        invalid_canvas["canvas"] = {"width": float("inf"), "height": 900}
        invalid_relation_entry = copy.deepcopy(base)
        invalid_relation_entry["links"] = ["not-a-relation"]
        dangling_relation = copy.deepcopy(base)
        dangling_relation["links"] = [
            {"source": "user_zhangsan", "target": "missing-node"}
        ]
        cases = (
            ("scene_type", "not-an-object", "adapter_scene must be an object"),
            ("objects_type", {**base, "objects": {}}, "objects must be a list"),
            ("object_entry", invalid_entry, "object entries must be objects"),
            ("coordinate", invalid_coordinate, "object x must be finite"),
            ("dimension", invalid_dimension, "object dimensions must be positive"),
            ("canvas_type", {**base, "canvas": []}, "canvas must be an object"),
            ("canvas_coordinate", invalid_canvas, "canvas.width must be finite"),
            ("relations_type", {**base, "links": {}}, "links must be a list"),
            ("relation_entry", invalid_relation_entry, "links entries must be objects"),
            ("dangling_relation", dangling_relation, "dangling endpoint"),
            (
                "source_metadata_type",
                {**base, "source_metadata": "not-an-object"},
                "source_metadata must be an object",
            ),
        )
        for name, adapter_scene, error in cases:
            with self.subTest(name=name):
                payload = live_capture_payload()
                payload["canvases"] = []
                payload["adapter_scene"] = adapter_scene
                with self.assertRaisesRegex(ValueError, error):
                    self.service.ingest(payload)

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

    def test_svg_region_raster_metadata_reaches_analysis_only_vision_frame(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        payload["canvases"][0].update(
            {
                "source_kind": "visual_region",
                "source_type": "svg_region",
                "capture_method": "visible_tab_crop",
                "source_ref": "visual-region:topology",
                "source_canvas_id": "topology-svg-root",
                "frame_id": "0",
                "frame_url": "https://nce.example/topology",
                "document_id": "document-svg-1",
                "region_selector": "#topology-map > svg",
                "capture_kind": "visible_tab",
                "roi_status": "verified",
                "source_region": {
                    "x": 20,
                    "y": 100,
                    "width": 800,
                    "height": 600,
                },
                "source_pixel_region": [40, 200, 1600, 1200],
                "source_frame_id": "0",
                "source_frame_url": "https://nce.example/topology",
                "visible_ratio": 0.95,
                "visible_capture_error": "",
                "primitive_count": 27,
                "device_pixel_ratio": 2,
                "coordinate_space": {
                    "type": "viewport_css_pixels",
                    "origin": [20, 100],
                },
            }
        )
        adapter = RecordingCanvasVisionAdapter()
        service = self.service_with(canvas_vision=adapter)

        capture = service.ingest(payload)
        result = service.get_result(capture["capture_id"])

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_vision_adapter")
        self.assertEqual(len(adapter.calls), 1)
        frame = adapter.calls[0]["frames"][0]
        self.assertEqual(frame.source_kind, "visual_region")
        self.assertEqual(frame.source_type, "svg_region")
        self.assertEqual(frame.capture_method, "visible_tab_crop")
        self.assertEqual(frame.source_ref, "visual-region:topology")
        self.assertEqual(frame.source_canvas_id, "topology-svg-root")
        self.assertEqual(frame.frame_id, "0")
        self.assertEqual(frame.frame_url, "https://nce.example/topology")
        self.assertEqual(frame.document_id, "document-svg-1")
        self.assertEqual(frame.region_selector, "#topology-map > svg")
        self.assertEqual(frame.capture_kind, "visible_tab")
        self.assertEqual(frame.roi_status, "verified")
        self.assertEqual(
            frame.source_region,
            {"x": 20, "y": 100, "width": 800, "height": 600},
        )
        self.assertEqual(
            frame.source_pixel_region,
            [40, 200, 1600, 1200],
        )
        self.assertEqual(frame.source_frame_id, "0")
        self.assertEqual(
            frame.source_frame_url,
            "https://nce.example/topology",
        )
        self.assertEqual(frame.visible_ratio, 0.95)
        self.assertEqual(frame.visible_capture_error, "")
        self.assertEqual(frame.primitive_count, 27)
        self.assertEqual(frame.device_pixel_ratio, 2)
        self.assertEqual(
            frame.coordinate_space,
            {"type": "viewport_css_pixels", "origin": [20, 100]},
        )

        visual_input = result["perception"]["candidates"]["canvas"]["input"][
            "canvases"
        ][0]
        for field in (
            "source_kind",
            "source_type",
            "capture_method",
            "source_ref",
            "source_canvas_id",
            "frame_id",
            "frame_url",
            "document_id",
            "region_selector",
            "capture_kind",
            "roi_status",
            "source_region",
            "source_pixel_region",
            "source_frame_id",
            "source_frame_url",
            "visible_ratio",
            "visible_capture_error",
            "primitive_count",
            "device_pixel_ratio",
            "coordinate_space",
        ):
            self.assertEqual(visual_input[field], payload["canvases"][0][field])
        self.assertTrue(frame.screenshot_path.exists())
        self.assertTrue(capture["scene"]["pixel_inference_performed"])
        self.assertFalse(capture["scene"]["actionable_grounding"])
        self.assertFalse(
            capture["scene"]["provenance"]["source_allows_actionable_grounding"]
        )
        self.assertTrue(
            all(
                binding["actionable"] is False
                for binding in capture["scene"]["business_object_bindings"].values()
            )
        )

    def test_svg_data_url_is_rejected_without_reaching_vision_adapter(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["canvases"][0].update(
            {
                "source_kind": "visual_region",
                "source_type": "svg_region",
                "capture_method": "inline_svg",
                "region_selector": "#topology-map > svg",
                "data_url": "data:image/svg+xml;base64,PHN2Zy8+",
            }
        )
        adapter = RecordingCanvasVisionAdapter()
        service = self.service_with(canvas_vision=adapter)

        capture = service.ingest(payload)
        result = service.get_result(capture["capture_id"])

        self.assertEqual(capture["summary"]["selected_mode"], "live_dom_snapshot")
        self.assertEqual(adapter.calls, [])
        visual_input = result["perception"]["candidates"]["canvas"]["input"][
            "canvases"
        ][0]
        self.assertEqual(visual_input["source_type"], "svg_region")
        self.assertEqual(visual_input["capture_method"], "inline_svg")
        self.assertEqual(
            visual_input["capture_error"],
            "unsupported canvas data URL",
        )
        self.assertNotIn("screenshot_path", visual_input)
        self.assertFalse(
            result["perception"]["candidates"]["canvas"]["pixel_capture_available"]
        )

    def test_unverified_visible_tab_roi_is_analysis_only(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        payload["canvases"][0].update(
            {
                "source_kind": "visual_region",
                "source_type": "canvas_region",
                "capture_kind": "visible_tab",
                "roi_status": "unverified",
                "source_region": {
                    "x": 0,
                    "y": 0,
                    "width": 1280,
                    "height": 720,
                },
                "source_frame_id": "3",
                "source_frame_url": "https://nce.example/embedded-topology",
                "visible_ratio": 1,
            }
        )
        adapter = RecordingCanvasVisionAdapter()
        service = self.service_with(canvas_vision=adapter)

        capture = service.ingest(payload)

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_vision_adapter")
        self.assertEqual(len(adapter.calls), 1)
        frame = adapter.calls[0]["frames"][0]
        self.assertEqual(frame.roi_status, "unverified")
        self.assertEqual(frame.source_frame_id, "3")
        self.assertFalse(capture["scene"]["actionable_grounding"])
        self.assertFalse(
            capture["scene"]["provenance"]["source_allows_actionable_grounding"]
        )
        self.assertTrue(
            all(
                binding["actionable"] is False
                for binding in capture["scene"]["business_object_bindings"].values()
            )
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
        self.assertFalse(capture["scene"]["actionable_grounding"])
        self.assertTrue(
            all(not item["actionable"] for item in capture["scene"]["business_object_bindings"].values())
        )
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
        element = capture["scene"]["elements"][0]
        self.assertEqual(element["source"]["kind"], "vision")
        self.assertEqual(element["interaction"]["status"], "analysis_only")
        self.assertFalse(element["interaction"]["can_click_now"])

    def test_canvas_vision_routing_is_visible_in_scene_and_summary(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        service = self.service_with(canvas_vision=RoutingCanvasVisionAdapter())

        capture = service.ingest(payload)

        expected = {
            "decision": "model_assist",
            "scene_type": "complex_topology",
            "effective_profile": "visible_topology",
            "reason_codes": ["cv_connectivity_incomplete"],
        }
        self.assertEqual(capture["scene"]["vision_routing"], expected)
        self.assertEqual(capture["summary"]["vision_routing"], expected)

    def test_canvas_adapter_cannot_self_authorize_nodes(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        service = self.service_with(canvas_vision=SpoofingCanvasVisionAdapter())

        capture = service.ingest(payload)
        element = capture["scene"]["elements"][0]

        self.assertEqual(element["source"]["kind"], "vision")
        self.assertEqual(element["source"]["producer_id"], "spoofing-vision")
        self.assertEqual(element["interaction"]["status"], "analysis_only")
        self.assertFalse(element["interaction"]["can_click_now"])
        self.assertFalse(element["interaction_eligible"])
        self.assertNotIn("safe_for_execution", element["attributes"])
        self.assertNotIn("interaction_eligible", element["attributes"])
        self.assertNotIn("actionable", element["attributes"])
        self.assertEqual(element["attributes"]["vendor"], "Huawei")
        self.assertFalse(capture["scene"]["actionable_grounding"])

    def test_text_nodes_are_explicitly_analysis_only(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        payload["canvases"] = []
        payload["topology_text"] = self.topology_text()
        service = self.service_with(text_recognizer=TopologyTextRecognizer())

        capture = service.ingest(payload)
        element = capture["scene"]["elements"][0]

        self.assertEqual(element["source"]["kind"], "text")
        self.assertEqual(element["interaction"]["status"], "analysis_only")
        self.assertFalse(element["interaction_eligible"])

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

    def test_canvas_vision_preserves_hybrid_fusion_summary(self):
        payload = live_capture_payload()
        payload["adapter_scene"] = None
        payload["dom"] = {"elements": []}
        service = self.service_with(canvas_vision=FusionMetadataCanvasVisionAdapter())

        capture = service.ingest(payload)

        self.assertEqual(
            capture["scene"]["fusion_summary"],
            {"confirmed_object_count": 2, "confirmed_link_count": 1},
        )
        self.assertEqual(
            capture["scene"]["fusion_analysis"]["structure_templates"][0]["type"],
            "layered",
        )

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

    def test_runtime_rejects_analysis_only_page_api_scene(self):
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
        self.assertFalse(accepted)
        task = runtime.get_task(task.task_id)
        self.assertTrue(any(event.payload.get("reason") == "non_actionable_grounding" for event in task.events))


if __name__ == "__main__":
    unittest.main()
