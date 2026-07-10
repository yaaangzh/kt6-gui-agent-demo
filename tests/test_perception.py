from pathlib import Path
import unittest

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


if __name__ == "__main__":
    unittest.main()
