import unittest

from kt6_backend.topology_change_detector import TopologyChangeDetector


def scene_with_edge(**edge_attributes):
    return {
        "elements": [
            {"business_id": "user_1", "center": [10, 20]},
            {"business_id": "ap_1", "center": [30, 40]},
        ],
        "relations": [
            {
                "source": "user_1",
                "target": "ap_1",
                "type": "access",
                **edge_attributes,
            }
        ],
    }


def scene_with_edges(edges):
    return {
        "elements": [
            {"business_id": "user_1", "center": [10, 20]},
            {"business_id": "ap_1", "center": [30, 40]},
        ],
        "relations": [
            {
                "source": "user_1",
                "target": "ap_1",
                "type": "access",
                **edge,
            }
            for edge in edges
        ],
    }


class TopologyChangeDetectorTest(unittest.TestCase):
    def setUp(self):
        self.detector = TopologyChangeDetector()

    def test_edge_status_change_is_reported_and_blocks_both_endpoints(self):
        previous = scene_with_edge(status="up")
        current = scene_with_edge(status="down")

        changes = self.detector.diff(previous, current)

        self.assertFalse(changes["is_empty"])
        self.assertEqual(
            changes["edge_attribute_changes"],
            [
                {
                    "source": "user_1",
                    "target": "ap_1",
                    "type": "access",
                    "changes": {"status": {"from": "up", "to": "down"}},
                }
            ],
        )
        self.assertEqual(changes["affected_business_ids"], ["ap_1", "user_1"])
        self.assertEqual(changes["blocking_business_ids"], ["ap_1", "user_1"])
        self.assertEqual(changes["rebind_business_ids"], [])
        self.assertIn("链路属性变化 1", changes["summary"])

    def test_nested_semantic_edge_attribute_change_is_detected(self):
        previous = scene_with_edge(attributes={"oper_status": "up", "color": "green"})
        current = scene_with_edge(attributes={"oper_status": "down", "color": "red"})

        changes = self.detector.diff(previous, current)

        self.assertEqual(
            changes["edge_attribute_changes"][0]["changes"],
            {"oper_status": {"from": "up", "to": "down"}},
        )

    def test_display_and_transient_edge_changes_do_not_block(self):
        previous = scene_with_edge(
            status="up",
            color="green",
            stroke_width=1,
            points=[[10, 20], [30, 40]],
            animation_progress=0.1,
            updated_at=100,
        )
        current = scene_with_edge(
            status="up",
            color="red",
            stroke_width=4,
            points=[[11, 21], [31, 41]],
            animation_progress=0.9,
            updated_at=101,
        )

        changes = self.detector.diff(previous, current)

        self.assertTrue(changes["is_empty"])
        self.assertEqual(changes["edge_attribute_changes"], [])
        self.assertEqual(changes["blocking_business_ids"], [])
        self.assertEqual(changes["summary"], "拓扑结构未变化")

    def test_parallel_edges_detect_change_on_non_last_edge(self):
        previous = scene_with_edges(
            [
                {"source_port": "port-1", "status": "up"},
                {"source_port": "port-2", "status": "up"},
            ]
        )
        current = scene_with_edges(
            [
                {"source_port": "port-1", "status": "down"},
                {"source_port": "port-2", "status": "up"},
            ]
        )

        changes = self.detector.diff(previous, current)

        self.assertEqual(changes["added_edges"], [])
        self.assertEqual(changes["removed_edges"], [])
        self.assertEqual(len(changes["edge_attribute_changes"]), 1)
        self.assertEqual(
            changes["edge_attribute_changes"][0]["changes"],
            {"status": {"from": "up", "to": "down"}},
        )
        self.assertEqual(changes["blocking_business_ids"], ["ap_1", "user_1"])

    def test_parallel_edge_port_change_remains_attribute_change_without_id(self):
        previous = scene_with_edges(
            [
                {"source_port": "port-1", "status": "up"},
                {"source_port": "port-2", "status": "up"},
            ]
        )
        current = scene_with_edges(
            [
                {"source_port": "port-3", "status": "up"},
                {"source_port": "port-2", "status": "up"},
            ]
        )

        changes = self.detector.diff(previous, current)

        self.assertEqual(changes["added_edges"], [])
        self.assertEqual(changes["removed_edges"], [])
        self.assertEqual(
            changes["edge_attribute_changes"][0]["changes"],
            {"source_port": {"from": "port-1", "to": "port-3"}},
        )

    def test_stable_edge_id_matches_changed_edge_when_parallel_edge_is_removed(self):
        previous = scene_with_edges(
            [
                {"relation_id": "link-1", "source_port": "port-1", "status": "up"},
                {"relation_id": "link-2", "source_port": "port-2", "status": "up"},
            ]
        )
        current = scene_with_edges(
            [
                {"relation_id": "link-2", "source_port": "port-3", "status": "up"},
            ]
        )

        changes = self.detector.diff(previous, current)

        self.assertEqual(changes["added_edges"], [])
        self.assertEqual(len(changes["removed_edges"]), 1)
        self.assertEqual(changes["removed_edges"][0]["relation_id"], "link-1")
        self.assertEqual(
            changes["edge_attribute_changes"][0]["changes"],
            {"source_port": {"from": "port-2", "to": "port-3"}},
        )
        self.assertEqual(
            changes["edge_attribute_changes"][0]["edge_identity"],
            {"relation_id": "link-2"},
        )

    def test_merge_keeps_distinct_parallel_edge_attribute_changes(self):
        previous = scene_with_edges(
            [
                {"source_port": "port-1", "status": "up"},
                {"source_port": "port-2", "status": "up"},
            ]
        )
        current = scene_with_edges(
            [
                {"source_port": "port-1", "status": "down"},
                {"source_port": "port-2", "status": "down"},
            ]
        )

        merged = self.detector.merge([self.detector.diff(previous, current)])

        self.assertEqual(len(merged["edge_attribute_changes"]), 2)
        self.assertEqual(
            {item["edge_identity"]["source_port"] for item in merged["edge_attribute_changes"]},
            {"port-1", "port-2"},
        )

    def test_merge_preserves_edge_attribute_changes_and_blocking_endpoints(self):
        first = self.detector.diff(
            scene_with_edge(status="up"),
            scene_with_edge(status="degraded"),
        )
        second = self.detector.diff(
            scene_with_edge(status="degraded"),
            scene_with_edge(status="down"),
        )

        merged = self.detector.merge([first, second])

        self.assertFalse(merged["is_empty"])
        self.assertEqual(len(merged["edge_attribute_changes"]), 2)
        self.assertEqual(merged["affected_business_ids"], ["ap_1", "user_1"])
        self.assertEqual(merged["blocking_business_ids"], ["ap_1", "user_1"])
        self.assertIn("链路属性变化 2", merged["summary"])

    def test_empty_change_set_exposes_edge_attribute_changes(self):
        changes = self.detector.empty()

        self.assertEqual(changes["edge_attribute_changes"], [])
        self.assertTrue(changes["is_empty"])


if __name__ == "__main__":
    unittest.main()
