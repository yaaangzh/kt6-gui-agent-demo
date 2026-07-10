import copy
import json
from pathlib import Path
import unittest

from kt6_backend.perception_runtime import PerceptionRuntime
from kt6_backend.tools import MockBusinessTools


class PerceptionTest(unittest.TestCase):
    def test_hybrid_perception_returns_dom_and_canvas_candidates(self):
        topology = MockBusinessTools(Path("data")).query_topology("张三")

        self.assertIn("dom", topology["ui_perception_candidates"])
        self.assertIn("canvas", topology["ui_perception_candidates"])
        self.assertIn(topology["ui_perception"]["mode"], {"dom_element_perception", "canvas_screenshot_perception"})
        self.assertIn("user_zhangsan", topology["ui_perception"]["business_object_bindings"])
        self.assertIn("ap_001", topology["ui_perception"]["business_object_bindings"])
        self.assertEqual(topology["perception_decision"]["selected_mode"], topology["ui_perception"]["mode"])

    def test_same_interface_reuses_cached_scene_across_task_focus(self):
        tools = MockBusinessTools(Path("data"))

        first = tools.query_topology("张三")
        second = tools.query_ap_topology("ap_003")

        self.assertEqual(first["perception_meta"]["cache_status"], "miss")
        self.assertEqual(second["perception_meta"]["cache_status"], "hit")
        self.assertEqual(first["perception_meta"]["scene_revision"], second["perception_meta"]["scene_revision"])
        self.assertEqual(first["focus"]["target_ids"], ["user_zhangsan", "ap_001"])
        self.assertEqual(second["focus"]["target_ids"], ["ap_003"])

    def test_topology_change_creates_incremental_scene_revision(self):
        topology = json.loads(Path("data/mock_topology.json").read_text(encoding="utf-8"))
        runtime = PerceptionRuntime()
        first = runtime.resolve(topology)

        changed = copy.deepcopy(topology)
        changed["objects"][5]["x"] += 40
        changed["links"] = [
            link
            for link in changed["links"]
            if not (link["source"] == "user_zhangsan" and link["target"] == "ap_001")
        ]
        second = runtime.resolve(changed)

        self.assertEqual(second["meta"]["cache_status"], "incremental")
        self.assertEqual(second["meta"]["scene_revision"], first["meta"]["scene_revision"] + 1)
        self.assertEqual(second["changes"]["moved_nodes"][0]["business_id"], "ap_003")
        self.assertTrue(any(edge["type"] == "access" for edge in second["changes"]["removed_edges"]))
        self.assertIn("user_zhangsan", second["changes"]["blocking_business_ids"])


if __name__ == "__main__":
    unittest.main()
