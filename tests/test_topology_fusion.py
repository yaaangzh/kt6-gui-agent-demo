from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from kt6_backend.topology_fusion import (
    FUSION_SCHEMA_VERSION,
    TopologyFusionError,
    fuse_topology_payloads,
)
from kt6_backend.topology_fusion_cli import main
from kt6_backend.topology_vision_contract import (
    RESPONSE_SCHEMA_VERSION,
    TopologyVisionContract,
)


def cv_capture():
    return {
        "scene": {
            "elements": [
                {
                    "business_id": "GW-001",
                    "type": "gateway",
                    "label": "GW-001",
                    "bbox": [10, 10, 80, 30],
                    "confidence": 0.98,
                    "attributes": {"ocr_text": "GW-001"},
                },
                {
                    "business_id": "CORE-001",
                    "type": "core_switch",
                    "label": "CORE-001",
                    "bbox": [10, 80, 100, 30],
                    "confidence": 0.96,
                    "attributes": {"ocr_text": "CORE-001"},
                },
                {
                    "business_id": "AP-001",
                    "type": "ap",
                    "label": "AP-001",
                    "bbox": [10, 150, 80, 30],
                    "confidence": 0.94,
                    "attributes": {"ocr_text": "AP-001"},
                },
            ],
            "relations": [
                {
                    "source": "GW-001",
                    "target": "CORE-001",
                    "type": "topology_link",
                    "confidence": 0.88,
                    "attributes": {"line_style": "solid"},
                },
                {
                    "source": "CORE-001",
                    "target": "AP-001",
                    "type": "topology_link",
                    "confidence": 0.72,
                    "attributes": {"evidence": "pixel_path"},
                },
            ],
            "coordinate_space": {
                "frames": [
                    {"canvas_id": "uploaded_topology", "width": 500, "height": 300}
                ]
            },
        }
    }


def layered_model_result():
    return {
        "topology": {
            "name": "中文测试拓扑",
            "layers": [
                {
                    "name": "核心层",
                    "devices": [
                        {
                            "id": "GW001",
                            "type": "gateway",
                            "vendor": "ZTE",
                            "connections": {"down": ["CORE-001"]},
                        },
                        {
                            "id": "CORE-001",
                            "type": "core_switch",
                            "model": "S5731S-H24T4S-A",
                            "connections": {
                                "up": ["GW001"],
                                "down": ["AP-001", "AGG-003"],
                            },
                        },
                    ],
                },
                {
                    "name": "终端层",
                    "devices": [
                        {
                            "id": "AP-001",
                            "type": "ap",
                            "connections": {"up": ["CORE-001"]},
                        },
                        {
                            "id": "AGG-003",
                            "type": "aggregation_switch",
                            "connections": {"up": ["CORE-001"]},
                        },
                    ],
                },
            ],
        }
    }


class TopologyFusionTest(unittest.TestCase):
    def test_layered_model_complements_cv_without_inventing_geometry(self):
        fused = fuse_topology_payloads(cv_capture(), layered_model_result())

        self.assertEqual(fused["schema_version"], FUSION_SCHEMA_VERSION)
        summary = fused["summary"]
        self.assertEqual(summary["cv_object_count"], 3)
        self.assertEqual(summary["model_object_count"], 4)
        self.assertEqual(summary["confirmed_object_count"], 3)
        self.assertEqual(summary["unlocated_model_object_count"], 1)
        self.assertEqual(summary["confirmed_link_count"], 2)
        self.assertEqual(summary["model_only_link_count"], 0)
        self.assertEqual(summary["unresolved_model_link_count"], 1)

        result = fused["result"]
        self.assertEqual(result["schema_version"], RESPONSE_SCHEMA_VERSION)
        self.assertEqual(len(result["objects"]), 3)
        gw = next(item for item in result["objects"] if item["business_id"] == "GW-001")
        self.assertEqual(gw["bbox"], [10.0, 10.0, 80.0, 30.0])
        self.assertEqual(gw["attributes"]["model_business_id"], "GW001")
        self.assertEqual(gw["attributes"]["model_semantics"]["vendor"], "ZTE")

        links = {
            frozenset((item["source"], item["target"])): item
            for item in result["links"]
        }
        self.assertEqual(
            links[frozenset(("GW-001", "CORE-001"))]["attributes"]["fusion_status"],
            "confirmed",
        )
        self.assertEqual(
            links[frozenset(("CORE-001", "AP-001"))]["attributes"]["fusion_status"],
            "confirmed",
        )
        self.assertEqual(fused["unlocated_objects"][0]["business_id"], "AGG-003")
        self.assertEqual(fused["unresolved_links"][0]["target"], "AGG-003")
        self.assertEqual(len(fused["semantic_graph"]["nodes"]), 4)
        self.assertEqual(len(fused["semantic_graph"]["links"]), 3)
        self.assertEqual(
            fused["unresolved_links"][0]["attributes"]["geometry_status"],
            "unresolved_endpoint",
        )

        # The grounded result remains consumable by the existing KT6 vision contract.
        parsed = TopologyVisionContract().parse_response_bytes(
            json.dumps(result, ensure_ascii=False).encode("utf-8"),
            {"uploaded_topology": (500, 300)},
        )
        self.assertEqual(len(parsed["objects"]), 3)
        self.assertEqual(len(parsed["links"]), 2)

    def test_nodes_edges_format_preserves_edge_attributes(self):
        cv = {
            "objects": [
                {
                    "business_id": "testNE49932",
                    "type": "network_device",
                    "label": "testNE49932",
                    "canvas_id": "c1",
                    "bbox": [40, 40, 30, 20],
                    "confidence": 0.99,
                },
                {
                    "business_id": "testNE4994",
                    "type": "network_device",
                    "label": "testNE4994",
                    "canvas_id": "c1",
                    "bbox": [140, 40, 30, 20],
                    "confidence": 0.97,
                },
            ],
            "links": [],
        }
        model = {
            "topology": {
                "layout": "star",
                "centerNode": "testNE49932",
                "nodes": [
                    {"id": "testNE49932", "role": "hub"},
                    {"id": "testNE4994"},
                ],
                "edges": [
                    {
                        "source": "testNE49932",
                        "target": "testNE4994",
                        "type": "dashed",
                        "color": "cyan",
                        "weight": 1.845,
                    }
                ],
            }
        }

        fused = fuse_topology_payloads(cv, model)

        self.assertEqual(fused["model_metadata"]["layout"], "star")
        self.assertEqual(fused["summary"]["model_only_link_count"], 1)
        link = fused["result"]["links"][0]
        self.assertEqual(link["type"], "topology_link")
        self.assertEqual(link["attributes"]["line_style"], "dashed")
        self.assertEqual(link["attributes"]["color"], "cyan")
        self.assertEqual(link["attributes"]["weight"], 1.845)
        hub = fused["result"]["objects"][0]
        self.assertEqual(hub["attributes"]["model_semantics"]["role"], "hub")

    def test_cv_peer_link_conflicting_with_model_layers_is_flagged(self):
        cv = cv_capture()
        cv["scene"]["relations"] = [
            {
                "source": "CORE-001",
                "target": "AP-001",
                "type": "topology_link",
                "confidence": 0.88,
                "attributes": {},
            }
        ]
        model = layered_model_result()
        # Remove the actual CORE -> AP relationship while retaining model layers.
        model["topology"]["layers"][0]["devices"][1]["connections"]["down"] = [
            "AGG-003"
        ]
        model["topology"]["layers"][1]["devices"][0]["connections"] = {}

        fused = fuse_topology_payloads(cv, model)

        cv_link = next(
            item
            for item in fused["result"]["links"]
            if frozenset((item["source"], item["target"]))
            == frozenset(("CORE-001", "AP-001"))
        )
        # These endpoints are in different model layers, so absence alone is not a conflict.
        self.assertEqual(cv_link["attributes"]["fusion_status"], "cv_only")

        cv["scene"]["relations"][0]["source"] = "GW-001"
        model["topology"]["layers"][0]["devices"][0]["connections"] = {}
        model["topology"]["layers"][0]["devices"][1]["connections"]["up"] = []
        fused = fuse_topology_payloads(cv, model)
        same_layer_link = next(
            item
            for item in fused["result"]["links"]
            if frozenset((item["source"], item["target"]))
            == frozenset(("GW-001", "AP-001"))
        )
        # GW and AP are not the same layer; keep it CV-only as well.
        self.assertEqual(same_layer_link["attributes"]["fusion_status"], "cv_only")

        # A same-layer CORE <-> GW edge absent from the model is an explicit conflict.
        cv["scene"]["relations"][0]["target"] = "CORE-001"
        fused = fuse_topology_payloads(cv, model)
        conflict = next(
            item
            for item in fused["result"]["links"]
            if frozenset((item["source"], item["target"]))
            == frozenset(("GW-001", "CORE-001"))
        )
        self.assertEqual(conflict["attributes"]["fusion_status"], "conflict")
        self.assertEqual(fused["summary"]["conflict_link_count"], 1)

    def test_missing_cv_bbox_is_rejected(self):
        cv = {"objects": [{"business_id": "A"}], "links": []}
        with self.assertRaisesRegex(TopologyFusionError, "requires bbox"):
            fuse_topology_payloads(cv, {"topology": {"nodes": [{"id": "A"}]}})

    def test_flat_node_connections_and_alarms_are_normalized(self):
        cv = {
            "objects": [
                {
                    "business_id": "A-001",
                    "type": "network_device",
                    "label": "A-001",
                    "canvas_id": "c1",
                    "bbox": [10, 10, 20, 20],
                    "confidence": 0.9,
                },
                {
                    "business_id": "B-001",
                    "type": "network_device",
                    "label": "B-001",
                    "canvas_id": "c1",
                    "bbox": [80, 10, 20, 20],
                    "confidence": 0.9,
                },
            ],
            "links": [],
        }
        model = {
            "topology": {
                "nodes": [
                    {"id": "A001", "connections": ["B-001"]},
                    {"id": "B-001", "connections": []},
                ],
                "alarms": [
                    {
                        "nodeId": "B-001",
                        "severity": "critical",
                        "description": "设备告警",
                    }
                ],
            }
        }

        fused = fuse_topology_payloads(cv, model)

        self.assertEqual(fused["summary"]["model_only_link_count"], 1)
        node_b = next(
            item for item in fused["result"]["objects"] if item["business_id"] == "B-001"
        )
        alarm = node_b["attributes"]["model_semantics"]["alarms"][0]
        self.assertEqual(alarm["severity"], "critical")
        self.assertEqual(alarm["description"], "设备告警")

    def test_cli_reads_bom_and_writes_unescaped_chinese_utf8(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cv_path = root / "cv.json"
            model_path = root / "model.json"
            out_path = root / "fused.json"
            cv_path.write_text(json.dumps(cv_capture()), encoding="utf-8")
            model_path.write_text(
                json.dumps(layered_model_result(), ensure_ascii=False),
                encoding="utf-8-sig",
            )

            with redirect_stdout(io.StringIO()):
                exit_code = main([str(cv_path), str(model_path), "--out", str(out_path)])

            self.assertEqual(exit_code, 0)
            raw = out_path.read_text(encoding="utf-8")
            self.assertIn("中文测试拓扑", raw)
            self.assertNotIn("\\u4e2d", raw)
            self.assertEqual(json.loads(raw)["summary"]["confirmed_object_count"], 3)


if __name__ == "__main__":
    unittest.main()
