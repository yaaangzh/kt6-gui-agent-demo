from pathlib import Path
import unittest

from kt6_backend.topology_text_recognizer import TopologyTextRecognizer


FIXTURE = Path(__file__).parent / "fixtures" / "enterprise_topology_ocr.txt"


class TopologyTextRecognizerTest(unittest.TestCase):
    def setUp(self):
        self.recognizer = TopologyTextRecognizer()
        self.text = FIXTURE.read_text(encoding="utf-8")

    def test_enterprise_fixture_produces_conservative_scene_graph(self):
        scene = self.recognizer.recognize(self.text, source_ref="fixture://enterprise-topology")

        self.assertEqual(scene["mode"], "topology_text_recognizer")
        self.assertEqual(scene["scene_type"], "text_topology")
        self.assertEqual(scene["object_count"], 22)
        self.assertEqual(scene["relation_count"], 19)
        self.assertEqual(scene["metrics"]["diagram_relation_count"], 7)
        self.assertEqual(scene["metrics"]["table_relation_count"], 12)
        self.assertEqual(scene["metrics"]["main_component_nodes"], 20)
        self.assertEqual(scene["metrics"]["observed_isolated_nodes"], 2)
        self.assertEqual(scene["metrics"]["connected_components"], 3)
        self.assertEqual(scene["metrics"]["undirected_cycle_rank"], 0)
        self.assertTrue(scene["usable_for_analysis"])
        self.assertFalse(scene["usable_for_actions"])
        self.assertFalse(scene["actionable_grounding"])
        self.assertFalse(scene["coordinate_space"]["actionable_grounding"])
        self.assertFalse(scene["diagnostics"]["narrative_relations_enabled"])
        self.assertEqual(scene["co_channel_relations"], [])
        self.assertTrue(scene["sources"])
        self.assertTrue(scene["evidence"])

        business_ids = {element["business_id"] for element in scene["elements"]}
        self.assertEqual(set(scene["business_object_bindings"]), business_ids)
        self.assertIn("acc_022", business_ids)
        self.assertIn("ap_022", business_ids)
        self.assertIn("acc_006", business_ids)
        self.assertIn("ap_006", business_ids)
        self.assertNotIn("trunk", business_ids)
        self.assertNotIn("ap_group", business_ids)

        for relation in scene["relations"]:
            self.assertIn(relation["source"], business_ids)
            self.assertIn(relation["target"], business_ids)
            self.assertFalse(relation["usable_for_actions"])
        for element in scene["elements"]:
            self.assertFalse(element["actionable_grounding"])
            self.assertFalse(element["usable_for_actions"])

    def test_fixture_does_not_invent_aggregation_or_independent_ap_edges(self):
        scene = self.recognizer.recognize(self.text)
        relations = {
            (relation["source"], relation["target"], relation["type"])
            for relation in scene["relations"]
        }

        self.assertIn(("gw_001", "core_001", "topology_link"), relations)
        self.assertEqual(
            {
                target
                for source, target, relation_type in relations
                if source == "core_001" and relation_type == "trunk"
            },
            {"acc_006", "acc_010", "acc_012", "acc_015", "acc_017", "acc_022"},
        )
        self.assertFalse(
            any("agg_003" in (source, target) for source, target, _ in relations),
            "AGG-003 is table-only and must not be inserted into the diagram hierarchy",
        )
        self.assertFalse(
            any(target == "ap_007" for _, target, _ in relations),
            "AP-007 is explicitly independent and has no declared parent",
        )
        issues = {(issue["code"], issue.get("business_id")) for issue in scene["issues"]}
        self.assertIn(("table_only_node_no_edge", "agg_003"), issues)
        self.assertIn(("unknown_parent", "ap_007"), issues)

        downstream = {
            (relation["source"], relation["target"])
            for relation in scene["relations"]
            if relation["type"] == "downstream"
        }
        self.assertIn(("acc_010", "ap_022"), downstream)
        self.assertIn(("acc_022", "ap_006"), downstream)
        self.assertTrue(
            all(
                relation["attributes"]["directness"] == "unknown"
                for relation in scene["relations"]
                if relation["type"] == "downstream"
            )
        )

    def test_trunk_and_ap_groups_remain_non_device_visual_groups(self):
        scene = self.recognizer.recognize(self.text)

        trunk_groups = [group for group in scene["visual_groups"] if group["kind"] == "relation_label"]
        ap_groups = [
            group for group in scene["visual_groups"] if group["kind"] == "generic_device_group"
        ]
        self.assertEqual(len(trunk_groups), 1)
        self.assertEqual(len(ap_groups), 6)
        self.assertTrue(all(group["is_device"] is False for group in trunk_groups + ap_groups))
        self.assertEqual({group["label"] for group in ap_groups}, {"AP群"})

    def test_special_markers_are_preserved_as_uncertain_evidence(self):
        scene = self.recognizer.recognize(self.text)
        elements = {element["business_id"]: element for element in scene["elements"]}
        expected_markers = {
            "ap_022": "LSW",
            "ap_029": "?",
            "ap_050": "ZTE",
            "ap_006": "FS",
            "ap_061": "ONU",
        }

        for business_id, marker in expected_markers.items():
            attributes = elements[business_id]["attributes"]
            self.assertIn(marker, attributes["special_markers"])
            self.assertTrue(attributes["classification_uncertain"])
            self.assertTrue(attributes["special_notes"])

        # IDs are normalized from the source's uppercase-hyphen form to the
        # repository's lowercase-underscore business ID convention. Prefixes
        # remain part of the identity, so AP-022 and ACC-022 never collide.
        self.assertEqual(elements["ap_022"]["attributes"]["display_id"], "AP-022")
        self.assertEqual(elements["acc_022"]["attributes"]["display_id"], "ACC-022")

        for business_id, marker_candidate in {
            "ap_022": "marker:LSW",
            "ap_029": "unknown",
            "ap_061": "marker:ONU",
        }.items():
            self.assertEqual(elements[business_id]["type"], "unknown_device")
            self.assertEqual(
                elements[business_id]["attributes"]["type_candidates"],
                ["ap", marker_candidate],
            )
            self.assertEqual(
                elements[business_id]["attributes"]["classification_status"],
                "conflicted",
            )

        self.assertEqual(elements["ap_050"]["type"], "ap")
        self.assertEqual(elements["ap_006"]["type"], "ap")
        self.assertNotIn("vendor", elements["ap_006"]["attributes"])
        self.assertNotIn("manufacturer", elements["ap_006"]["attributes"])

        uncertain_issues = [
            issue for issue in scene["issues"] if issue["code"] == "uncertain_special_device"
        ]
        self.assertEqual(
            {issue["business_id"] for issue in uncertain_issues},
            set(expected_markers),
        )
        self.assertFalse(any(issue["severity"] == "error" for issue in scene["issues"]))

    def test_crlf_and_common_indentation_do_not_change_recognition(self):
        baseline = self.recognizer.recognize(self.text)
        indented_crlf = "\r\n".join(f"        {line}" for line in self.text.splitlines())

        variant = self.recognizer.recognize(indented_crlf)

        for key in (
            "elements",
            "business_object_bindings",
            "relations",
            "visual_groups",
            "issues",
            "metrics",
            "evidence",
        ):
            self.assertEqual(variant[key], baseline[key])
        self.assertEqual(variant["sources"][0]["sha256"], baseline["sources"][0]["sha256"])

    def test_oversized_input_is_rejected_before_partial_recognition(self):
        scene = self.recognizer.recognize("X" * (self.recognizer.MAX_INPUT_CHARS + 1))

        self.assertEqual(scene["elements"], [])
        self.assertEqual(scene["relations"], [])
        self.assertFalse(scene["usable_for_analysis"])
        self.assertFalse(scene["usable_for_actions"])
        self.assertEqual(scene["issues"][0]["code"], "input_too_large")
        self.assertEqual(scene["issues"][0]["severity"], "error")

    def test_truncated_table_fails_closed(self):
        lines = self.text.splitlines()
        last_content_row = next(index for index, line in enumerate(lines) if "│ AP-007" in line)
        truncated = "\n".join(lines[: last_content_row + 1])

        scene = self.recognizer.recognize(truncated)

        self.assertFalse(scene["usable_for_analysis"])
        self.assertFalse(scene["usable_for_actions"])
        self.assertFalse(scene["diagnostics"]["table_closed"])
        self.assertIn("incomplete_device_table", {issue["code"] for issue in scene["issues"]})

    def test_broken_trunk_fanout_does_not_guess_missing_structure(self):
        broken = self.text.replace("▼         ▼           ▼           ▼         ▼         ▼", "▼         ▼           ▼           ▼         ▼          ", 1)

        scene = self.recognizer.recognize(broken)

        trunk_relations = [relation for relation in scene["relations"] if relation["type"] == "trunk"]
        self.assertEqual(trunk_relations, [])
        self.assertFalse(scene["usable_for_analysis"])
        self.assertIn("incomplete_trunk_fanout", {issue["code"] for issue in scene["issues"]})


if __name__ == "__main__":
    unittest.main()
