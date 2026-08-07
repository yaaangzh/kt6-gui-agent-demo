from pathlib import Path
import tempfile
import unittest

from kt6_backend.page_perception import PagePerceptionService, SQLitePageCaptureStore
from kt6_backend.perception_runtime import PerceptionRuntime
from kt6_backend.ui_operation_graph import SCHEMA_VERSION as OPERATION_SCHEMA_VERSION
from kt6_backend.ui_operation_graph import validate
from tests.test_page_perception import live_capture_payload


class PagePerceptionUIGraphTest(unittest.TestCase):
    def test_graph_is_built_persisted_and_exposed_without_execution_authority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = PagePerceptionService(
                SQLitePageCaptureStore(root / "captures.sqlite3", root / "assets"),
                PerceptionRuntime(),
            )
            payload = live_capture_payload()
            payload["adapter_scene"] = None
            payload["canvases"] = []
            payload["dom"] = {
                "elements": [
                    {
                        "ref": "frame:0:#panel",
                        "selector": "#panel",
                        "tag": "section",
                        "label": "设备面板",
                        "bbox": [10, 10, 300, 200],
                        "frame_id": "0",
                        "document_id": "doc-main",
                    },
                    {
                        "ref": "frame:0:#open-device",
                        "selector": "#open-device",
                        "parent_ref": "frame:0:#panel",
                        "tag": "button",
                        "role": "button",
                        "label": "打开设备",
                        "actionable": True,
                        "bbox": [30, 50, 100, 40],
                        "frame_id": "0",
                        "document_id": "doc-main",
                    },
                ]
            }

            capture = service.ingest(payload)
            graph = service.get_ui_graph(capture["capture_id"])

            self.assertEqual(graph["schema_version"], "kt6.ui-graph.v1")
            self.assertTrue(graph["analysis_only"])
            self.assertFalse(graph["execution_authorized"])
            self.assertFalse(graph["safe_for_execution"])
            self.assertNotIn("ui_graph", capture)
            self.assertEqual(capture["ui_graph_ref"]["graph_id"], graph["graph_id"])
            self.assertEqual(capture["summary"]["ui_graph_id"], graph["graph_id"])
            self.assertEqual(
                capture["summary"]["ui_graph_node_count"], graph["stats"]["node_count"]
            )

            stored = service.get_ui_graph(capture["capture_id"])
            topology = service.get_topology(capture["capture_id"])
            self.assertEqual(stored, graph)
            self.assertNotIn("ui_graph", topology)
            self.assertEqual(topology["ui_graph_ref"]["graph_id"], graph["graph_id"])
            self.assertEqual(
                topology["ui_graph_ref"]["node_count"], graph["stats"]["node_count"]
            )
            self.assertFalse(topology["ui_graph_ref"]["safe_for_execution"])
            stored["nodes"].clear()
            self.assertGreater(len(service.get_ui_graph(capture["capture_id"])["nodes"]), 0)

            button = next(
                node
                for node in graph["nodes"]
                if node.get("interaction", {}).get("candidate") is True
            )
            proposal = validate(
                graph,
                {
                    "schema_version": OPERATION_SCHEMA_VERSION,
                    "steps": [
                        {
                            "id": "click-device",
                            "op": "click",
                            "target_node_id": button["id"],
                        }
                    ],
                },
            )
            self.assertTrue(proposal["valid"], proposal["errors"])
            self.assertTrue(proposal["dry_run_only"])
            self.assertFalse(proposal["safe_for_execution"])


if __name__ == "__main__":
    unittest.main()
