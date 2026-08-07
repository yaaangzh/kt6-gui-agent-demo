import copy
import json
import unittest

from kt6_backend.ui_graph import (
    MAX_GRAPH_NODES,
    SCHEMA_VERSION,
    build_ui_graph,
    serialize_ui_graph,
)
from kt6_backend.ui_operation_graph import (
    SCHEMA_VERSION as OPERATION_SCHEMA_VERSION,
    validate as validate_operation_plan,
)


def mixed_capture() -> dict:
    return {
        "capture_id": "capture-ui-graph-1",
        "capture": {
            "page": {
                "url": "https://nce.example/devices",
                "title": "Devices",
                "ui_version": "v1",
            },
            "dom": {
                "elements": [
                    {
                        "ref": "frame:0:#ap-row",
                        "selector": "#ap-row",
                        "role": "row",
                        "label": "AP-001",
                        "business_id": "ap_001",
                        "bbox": [10, 20, 300, 40],
                        "frame_id": "0",
                        "document_id": "doc-main",
                        "document_order": 0,
                    },
                    {
                        "ref": "frame:0:#shutdown",
                        "selector": "#shutdown",
                        "parent_ref": "frame:0:#ap-row",
                        "parent_relation": "direct_parent",
                        "role": "button",
                        "label": "关闭",
                        "owner_business_id": "ap_001",
                        "action_id": "ap.shutdown",
                        "actionable": True,
                        "interaction_eligible": True,
                        "bbox": [250, 25, 50, 30],
                        "frame_id": "0",
                        "document_id": "doc-main",
                        "document_order": 1,
                    },
                ]
            },
        },
        "result": {
            "perception": {
                "page_api_perception": {
                    "mode": "canvas_renderer_adapter",
                    "provenance": {"semantic_source": "page_api_adapter"},
                    "elements": [
                        {
                            "element_id": "page-ap",
                            "business_id": "ap_001",
                            "type": "ap",
                            "label": "AP-001 page API",
                            "bbox": [100, 100, 50, 50],
                            "source": {"kind": "page_api", "trust": "page_declared"},
                        },
                        {
                            "element_id": "page-user",
                            "business_id": "user_1",
                            "type": "user",
                            "label": "张三",
                            "bbox": [10, 100, 50, 50],
                        },
                    ],
                    "relations": [
                        {
                            "relation_id": "page-access",
                            "source": "user_1",
                            "target": "ap_001",
                            "type": "access",
                        }
                    ],
                },
                "canvas_perception": {
                    "mode": "canvas_vision_adapter",
                    "provenance": {"semantic_source": "canvas_pixels"},
                    "elements": [
                        {
                            "element_id": "vision-ap",
                            "business_id": "ap_001",
                            "type": "ap",
                            "label": "AP vision",
                            "bbox": [101, 101, 48, 48],
                            "actionable": True,
                            "safe_for_execution": True,
                            "source": {"kind": "dom", "method": "spoofed"},
                        },
                        {
                            "element_id": "vision-core",
                            "business_id": "core_1",
                            "type": "core",
                            "label": "CORE",
                            "bbox": [200, 100, 50, 50],
                        },
                    ],
                    "relations": [
                        {
                            "source": "ap_001",
                            "target": "core_1",
                            "type": "uplink",
                        }
                    ],
                },
                "cdp_perception": {
                    "mode": "cdp_dom_ax_snapshot",
                    "nodes": [
                        {
                            "node_id": "cdp:main:1",
                            "frame_id": "main",
                            "role": "document",
                            "name": "Devices",
                            "interaction_candidate": False,
                        },
                        {
                            "node_id": "cdp:main:2",
                            "frame_id": "main",
                            "backend_node_id": 2,
                            "role": "button",
                            "name": "保存",
                            "interaction_candidate": True,
                            "parent_id": "cdp:main:1",
                        },
                    ],
                    "relations": [
                        {
                            "type": "dom_child",
                            "source": "cdp:main:1",
                            "target": "cdp:main:2",
                            "provenance": {"source": "DOMSnapshot.captureSnapshot"},
                        }
                    ],
                },
                "candidates": {
                    "text": {
                        "mode": "topology_text_reconstruction",
                        "provenance": {"semantic_source": "provided_text"},
                        "elements": [
                            {
                                "element_id": "text-core",
                                "business_id": "core_1",
                                "type": "core",
                                "label": "CORE from text",
                            }
                        ],
                        "relations": [],
                    }
                },
            }
        },
    }


class UIGraphTest(unittest.TestCase):
    def test_normalizes_sources_and_preserves_parent_owner_and_action_claims(self):
        graph = build_ui_graph(mixed_capture())

        self.assertEqual(graph["schema_version"], SCHEMA_VERSION)
        self.assertTrue(graph["graph_id"].startswith("uig:"))
        self.assertTrue(graph["analysis_only"])
        self.assertFalse(graph["execution_authorized"])
        self.assertFalse(graph["safe_for_execution"])

        elements = [node for node in graph["nodes"] if node["kind"] == "element"]
        self.assertEqual(
            {node["source"]["kind"] for node in elements},
            {"dom", "cdp", "page_api", "vision", "text"},
        )
        self.assertTrue(all(node["actionable"] is False for node in graph["nodes"]))
        self.assertTrue(all(node["can_click_now"] is False for node in graph["nodes"]))
        self.assertTrue(
            all(node["safe_for_execution"] is False for node in graph["nodes"])
        )
        self.assertTrue(
            all(node["interaction"]["authorized"] is False for node in graph["nodes"])
        )

        dom_parent = next(
            node for node in elements if node["source"].get("source_ref") == "frame:0:#ap-row"
        )
        control = next(node for node in elements if node.get("action_id") == "ap.shutdown")
        business = next(
            node
            for node in graph["nodes"]
            if node["kind"] == "business_object" and node["business_id"] == "ap_001"
        )
        action = next(
            node
            for node in graph["nodes"]
            if node["kind"] == "action_claim" and node["action_id"] == "ap.shutdown"
        )
        edge_keys = {
            (edge["type"], edge["source"], edge["target"])
            for edge in graph["edges"]
        }
        self.assertIn(("parent_of", dom_parent["id"], control["id"]), edge_keys)
        self.assertIn(("owner_of", business["id"], control["id"]), edge_keys)
        self.assertIn(("supports_action", control["id"], action["id"]), edge_keys)
        self.assertTrue(control["interaction"]["candidate"])

        cdp_button = next(
            node
            for node in elements
            if node["source"]["kind"] == "cdp" and node["name"] == "保存"
        )
        self.assertTrue(cdp_button["interaction"]["candidate"])
        self.assertFalse(cdp_button["actionable"])
        self.assertTrue(
            any(
                edge["type"] == "parent_of"
                and edge.get("relation_type") == "dom_child"
                and edge["target"] == cdp_button["id"]
                for edge in graph["edges"]
            )
        )

        vision = next(
            node
            for node in elements
            if node["source"]["kind"] == "vision" and node["business_id"] == "ap_001"
        )
        self.assertEqual(vision["source"]["declared_kind"], "dom")
        self.assertNotIn("safe_for_execution", vision.get("attributes", {}))

    def test_json_and_jsonl_are_deterministic_and_keep_safety_fields(self):
        capture = mixed_capture()
        first = build_ui_graph(capture)
        second = build_ui_graph(copy.deepcopy(capture))

        self.assertEqual(first, second)
        compact = serialize_ui_graph(first)
        self.assertEqual(compact, serialize_ui_graph(second))
        decoded = json.loads(compact)
        self.assertEqual(decoded["graph_id"], first["graph_id"])
        self.assertEqual(len(decoded["nodes"]), len(first["nodes"]))
        self.assertTrue(all(node["actionable"] is False for node in decoded["nodes"]))

        records = [json.loads(line) for line in serialize_ui_graph(first, format="jsonl").splitlines()]
        self.assertEqual(records[0]["record"], "graph")
        self.assertEqual(records[-1]["record"], "stats")
        node_records = [record for record in records if record["record"] == "node"]
        edge_records = [record for record in records if record["record"] == "edge"]
        self.assertEqual(len(node_records), len(first["nodes"]))
        self.assertEqual(len(edge_records), len(first["edges"]))
        self.assertEqual(
            [record["id"] for record in node_records],
            sorted(record["id"] for record in node_records),
        )
        with self.assertRaisesRegex(ValueError, "json.*jsonl"):
            serialize_ui_graph(first, format="mermaid")

    def test_stable_source_ref_keeps_node_id_across_label_and_layout_changes(self):
        original = mixed_capture()
        first = build_ui_graph(original)
        changed = copy.deepcopy(original)
        control = changed["capture"]["dom"]["elements"][1]
        control["label"] = "关闭设备（新文案）"
        control["bbox"] = [400, 80, 90, 36]
        second = build_ui_graph(changed)

        first_id = next(
            node["id"]
            for node in first["nodes"]
            if node.get("action_id") == "ap.shutdown"
        )
        second_id = next(
            node["id"]
            for node in second["nodes"]
            if node.get("action_id") == "ap.shutdown"
        )
        self.assertEqual(first_id, second_id)
        self.assertNotEqual(first["graph_id"], second["graph_id"])

    def test_dom_candidate_requires_explicit_claim_and_stable_rebind(self):
        graph = build_ui_graph(
            {
                "capture_id": "dom-candidate-boundary",
                "dom_perception": {
                    "elements": [
                        {
                            "element_id": "dom-no-ref",
                            "business_id": "no-ref",
                            "label": "No ref",
                            "actionable": True,
                            "interaction_eligible": True,
                        },
                        {
                            "business_id": "pseudo-ref",
                            "label": "Pseudo ref",
                            "selector": "@CAPTURE:button:1",
                            "source_ref": "@CAPTURE:button:1",
                            "interaction_eligible": True,
                        },
                        {
                            "business_id": "raw-only",
                            "label": "Raw only",
                            "selector": "#raw-only",
                            "actionable": True,
                        },
                        {
                            "business_id": "stable-ref",
                            "label": "Stable ref",
                            "selector": "#stable",
                            "interaction_eligible": True,
                        },
                    ]
                },
                "dom_action_bindings": {
                    "dom-no-ref": {"element_id": "dom-no-ref"}
                },
            }
        )
        by_business_id = {
            node["business_id"]: node
            for node in graph["nodes"]
            if node.get("business_id")
        }

        for business_id in ("no-ref", "pseudo-ref", "raw-only"):
            with self.subTest(business_id=business_id):
                node = by_business_id[business_id]
                self.assertFalse(node["interaction"]["candidate"])
                plan = {
                    "schema_version": OPERATION_SCHEMA_VERSION,
                    "graph_id": graph["graph_id"],
                    "steps": [
                        {
                            "id": "click-target",
                            "op": "click",
                            "target_node_id": node["id"],
                        }
                    ],
                }
                result = validate_operation_plan(
                    graph,
                    plan,
                    require_graph_id=True,
                )
                self.assertFalse(result["valid"])

        stable = by_business_id["stable-ref"]
        self.assertTrue(stable["interaction"]["candidate"])
        self.assertEqual(stable["source"]["source_ref"], "#stable")
        stable_result = validate_operation_plan(
            graph,
            {
                "schema_version": OPERATION_SCHEMA_VERSION,
                "graph_id": graph["graph_id"],
                "steps": [
                    {
                        "id": "click-stable",
                        "op": "click",
                        "target_node_id": stable["id"],
                    }
                ],
            },
            require_graph_id=True,
        )
        self.assertTrue(stable_result["valid"], stable_result["errors"])

    def test_cdp_semantic_projection_is_bounded_and_reports_truncation(self):
        nodes = [
            {
                "node_id": f"cdp:main:{index}",
                "frame_id": "main",
                "role": "button",
                "name": f"Control {index}",
                "interaction_candidate": index % 2 == 0,
            }
            for index in range(MAX_GRAPH_NODES + 5)
        ]
        graph = build_ui_graph(
            {
                "capture_id": "large-cdp",
                "page": {"url": "https://example.test"},
                "cdp_perception": {"nodes": nodes, "relations": []},
            }
        )

        self.assertLessEqual(len(graph["nodes"]), MAX_GRAPH_NODES)
        self.assertTrue(graph["stats"]["truncated"])
        self.assertEqual(graph["stats"]["input_element_count"], MAX_GRAPH_NODES + 5)
        issue = next(
            issue for issue in graph["issues"] if issue["code"] == "cdp_semantic_projection"
        )
        self.assertTrue(issue["limit_reached"])
        self.assertEqual(issue["retained_count"], MAX_GRAPH_NODES)


if __name__ == "__main__":
    unittest.main()
