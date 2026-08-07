import copy
import json
from pathlib import Path
import tempfile
import unittest

from kt6_backend.page_perception import PagePerceptionService, SQLitePageCaptureStore
from kt6_backend.perception_runtime import PerceptionRuntime


def cdp_envelope(page_url="https://example.test/topology?capture=1"):
    strings = [
        "",
        "main-frame",
        page_url,
        "Topology",
        "#document",
        "HTML",
        "BUTTON",
        "aria-label",
        "Apply changes",
    ]
    return {
        "schema_version": "kt6.cdp-page-snapshot.v1",
        "captured_at": 1_700_000_000.5,
        "page": {"url": page_url, "title": "Topology"},
        "source_metadata": {
            "source_type": "playwright_cdp_sidecar",
            "browser_product": "Chrome/Test",
            "protocol_version": "1.3",
            "raw_secret": "must-not-be-persisted",
            "safe_for_execution": True,
        },
        "frames": [
            {
                "frame_id": "main-frame",
                "url": page_url,
                "security_origin": "https://example.test",
            }
        ],
        "frame_errors": [],
        "dom_snapshot": {
            "strings": strings,
            "documents": [
                {
                    "frameId": 1,
                    "documentURL": 2,
                    "title": 3,
                    "nodes": {
                        "parentIndex": [-1, 0, 1],
                        "nodeType": [9, 1, 1],
                        "nodeName": [4, 5, 6],
                        "nodeValue": [0, 0, 0],
                        "backendNodeId": [1, 2, 3],
                        # Official CDP shape: one ArrayOfStrings per node.
                        "attributes": [[], [], [7, 8]],
                        "isClickable": {"index": [2]},
                    },
                    "layout": {
                        "nodeIndex": [2],
                        "bounds": [[10, 20, 100, 30]],
                    },
                }
            ],
        },
        "ax_tree": {
            "nodes": [
                {
                    "nodeId": "ax-root",
                    "frameId": "main-frame",
                    "backendDOMNodeId": 1,
                    "role": {"value": "RootWebArea"},
                    "name": {"value": "Topology"},
                    "childIds": ["ax-button"],
                },
                {
                    "nodeId": "ax-button",
                    "backendDOMNodeId": 3,
                    "parentId": "ax-root",
                    "role": {"value": "button"},
                    "name": {"value": "Apply changes"},
                    "properties": [
                        {"name": "disabled", "value": {"value": False}},
                        {"name": "focusable", "value": {"value": True}},
                        {"name": "safe_for_execution", "value": {"value": True}},
                    ],
                },
            ]
        },
        "actionable_grounding": True,
        "safe_for_execution": True,
    }


def page_payload():
    return {
        "page": {
            "url": "https://example.test/topology?tab=live",
            "title": "Topology",
            "language": "en",
            "ui_version": "test-cdp-v1",
            "viewport": {
                "width": 1280,
                "height": 720,
                "device_pixel_ratio": 1,
            },
        },
        "dom": {"elements": []},
        "canvases": [],
        "adapter_scene": None,
        "cdp_snapshot": cdp_envelope(),
    }


def node_by_backend(scene, backend_node_id):
    return next(
        node
        for node in scene["nodes"]
        if node["backend_node_id"] == backend_node_id
    )


class PagePerceptionCDPTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.runtime = PerceptionRuntime()
        self.store = SQLitePageCaptureStore(
            root / "captures.sqlite3",
            root / "assets",
        )
        self.service = PagePerceptionService(self.store, self.runtime)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_ingest_persists_only_normalized_cdp_candidate(self):
        capture = self.service.ingest(page_payload())
        result = self.service.get_result(capture["capture_id"])
        topology = self.service.get_topology(capture["capture_id"])
        stored = self.store.get(capture["capture_id"])

        cdp = capture["cdp_perception"]
        self.assertIsNotNone(cdp)
        self.assertEqual(cdp["node_count"], 3)
        self.assertEqual(cdp["relation_count"], 3)
        self.assertEqual(cdp["capture_metadata"]["page_route"], "https://example.test/topology")
        self.assertEqual(
            cdp["capture_metadata"]["schema_version"],
            "kt6.cdp-page-snapshot.v1",
        )
        button = node_by_backend(cdp, 3)
        self.assertEqual(button["attributes"]["aria-label"], "Apply changes")
        self.assertEqual(button["role"], "button")
        self.assertEqual(button["name"], "Apply changes")
        self.assertEqual(button["bounds"], [10.0, 20.0, 100.0, 30.0])
        self.assertTrue(button["is_clickable"])
        self.assertTrue(button["interaction_candidate"])

        self.assertFalse(cdp["actionable"])
        self.assertFalse(cdp["actionable_grounding"])
        self.assertFalse(cdp["safe_for_execution"])
        self.assertFalse(cdp["usable_for_actions"])
        self.assertFalse(button["actionable"])
        self.assertFalse(button["interaction_eligible"])
        self.assertFalse(button["actionable_grounding"])
        self.assertFalse(button["safe_for_execution"])
        self.assertFalse(button["interaction"]["can_click_now"])
        self.assertFalse(button["interaction"]["preflight_required"])
        self.assertFalse(button["interaction"]["safe_for_execution"])

        self.assertEqual(result["perception"]["candidates"]["cdp"], cdp)
        self.assertEqual(result["perception"]["raw_scenes"]["cdp"], cdp)
        self.assertEqual(result["perception"]["cdp_perception"], cdp)
        self.assertNotIn("cdp_perception", topology)
        self.assertNotIn("cdp", topology["raw_scenes"])
        self.assertNotIn("cdp", topology["ui_perception_candidates"])
        self.assertEqual(topology["cdp_perception_ref"]["node_count"], 3)
        self.assertEqual(topology["cdp_perception_ref"]["candidate_count"], 1)
        self.assertFalse(topology["cdp_perception_ref"]["safe_for_execution"])
        self.assertNotEqual(capture["scene"]["mode"], "cdp_dom_ax_snapshot")

        summary = capture["summary"]
        self.assertTrue(summary["cdp_snapshot_available"])
        self.assertEqual(summary["cdp_node_count"], 3)
        self.assertEqual(summary["cdp_relation_count"], 3)
        self.assertEqual(summary["cdp_candidate_count"], 1)
        self.assertEqual(summary["cdp_frame_count"], 1)
        self.assertFalse(summary["cdp_actionable_grounding"])
        self.assertFalse(summary["cdp_safe_for_execution"])

        self.assertIn("cdp_perception", stored["capture"])
        self.assertNotIn("cdp_snapshot", stored["capture"])
        serialized = json.dumps(stored, ensure_ascii=False)
        self.assertNotIn("must-not-be-persisted", serialized)
        self.assertNotIn('"dom_snapshot"', serialized)
        self.assertNotIn('"ax_tree"', serialized)

    def test_schema_and_page_route_are_validated(self):
        wrong_schema = page_payload()
        wrong_schema["cdp_snapshot"]["schema_version"] = "kt6.cdp-page-snapshot.v0"
        with self.assertRaisesRegex(ValueError, "schema_version"):
            self.service.ingest(wrong_schema)

        wrong_route = page_payload()
        wrong_route["cdp_snapshot"]["page"]["url"] = (
            "https://example.test/another-page?tab=live"
        )
        with self.assertRaisesRegex(ValueError, "page route mismatch"):
            self.service.ingest(wrong_route)

        malformed = page_payload()
        malformed["cdp_snapshot"] = []
        with self.assertRaisesRegex(ValueError, "cdp_snapshot must be an object"):
            self.service.ingest(malformed)

    def test_cdp_structure_and_content_participate_in_hashes(self):
        first = self.service.ingest(page_payload())
        repeated = self.service.ingest(page_payload())
        self.assertEqual(
            first["perception_meta"]["template_hash"],
            repeated["perception_meta"]["template_hash"],
        )
        self.assertEqual(
            first["perception_meta"]["content_hash"],
            repeated["perception_meta"]["content_hash"],
        )
        self.assertEqual(repeated["perception_meta"]["cache_status"], "hit")

        state_change = page_payload()
        state_change["cdp_snapshot"]["ax_tree"]["nodes"][1]["name"]["value"] = (
            "Apply updated changes"
        )
        changed_state = self.service.ingest(state_change)
        self.assertEqual(
            first["perception_meta"]["template_hash"],
            changed_state["perception_meta"]["template_hash"],
        )
        self.assertNotEqual(
            first["perception_meta"]["content_hash"],
            changed_state["perception_meta"]["content_hash"],
        )

        structure_change = page_payload()
        snapshot = structure_change["cdp_snapshot"]["dom_snapshot"]
        strings = snapshot["strings"]
        div_name = len(strings)
        strings.append("DIV")
        id_name = len(strings)
        strings.append("id")
        id_value = len(strings)
        strings.append("extra-node")
        nodes = snapshot["documents"][0]["nodes"]
        nodes["parentIndex"].append(1)
        nodes["nodeType"].append(1)
        nodes["nodeName"].append(div_name)
        nodes["nodeValue"].append(0)
        nodes["backendNodeId"].append(4)
        nodes["attributes"].append([id_name, id_value])
        structure_change["cdp_snapshot"]["ax_tree"]["nodes"].append(
            {
                "nodeId": "ax-extra",
                "frameId": "main-frame",
                "backendDOMNodeId": 4,
                "role": {"value": "group"},
                "name": {"value": "Extra node"},
            }
        )
        changed_structure = self.service.ingest(structure_change)
        self.assertNotEqual(
            first["perception_meta"]["template_hash"],
            changed_structure["perception_meta"]["template_hash"],
        )
        self.assertNotEqual(
            first["perception_meta"]["content_hash"],
            changed_structure["perception_meta"]["content_hash"],
        )
        self.assertNotEqual(
            changed_structure["scene"]["mode"],
            "cdp_dom_ax_snapshot",
        )

    def test_normalizer_fails_fast_on_node_and_relation_limits(self):
        too_many_dom = page_payload()
        count = PagePerceptionService.MAX_CDP_NODE_COUNT + 1
        document = too_many_dom["cdp_snapshot"]["dom_snapshot"]["documents"][0]
        document["nodes"] = {
            "parentIndex": [-1] * count,
            "nodeType": [1] * count,
            "nodeName": [5] * count,
            "nodeValue": [0] * count,
            "backendNodeId": list(range(1, count + 1)),
            "attributes": [[] for _ in range(count)],
        }
        document["layout"] = {}
        too_many_dom["cdp_snapshot"]["ax_tree"] = {"nodes": []}
        with self.assertRaisesRegex(ValueError, "exceeds 10000 nodes"):
            self.service.ingest(too_many_dom)

        too_many_ax = page_payload()
        too_many_ax["cdp_snapshot"]["dom_snapshot"] = {
            "strings": [],
            "documents": [],
        }
        too_many_ax["cdp_snapshot"]["ax_tree"] = {
            "nodes": [{} for _ in range(PagePerceptionService.MAX_CDP_NODE_COUNT + 1)]
        }
        with self.assertRaisesRegex(ValueError, "AX tree exceeds 10000 nodes"):
            self.service.ingest(too_many_ax)

        too_many_relations = page_payload()
        too_many_relations["cdp_snapshot"]["dom_snapshot"] = {
            "strings": [],
            "documents": [],
        }
        too_many_relations["cdp_snapshot"]["ax_tree"] = {
            "nodes": [
                {
                    "nodeId": "root",
                    "frameId": "main-frame",
                    "childIds": [
                        f"child-{index}"
                        for index in range(
                            PagePerceptionService.MAX_CDP_RELATION_COUNT + 1
                        )
                    ],
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "exceeds 40000 relations"):
            self.service.ingest(too_many_relations)


if __name__ == "__main__":
    unittest.main()
