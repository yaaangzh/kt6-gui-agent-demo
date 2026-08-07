import json
import unittest

from kt6_backend.ui_graph_planning import (
    UIGraphNotFoundError,
    UIGraphPlanningService,
    UIGraphProjectionError,
    UIGraphReasonerNotConfiguredError,
)
from kt6_backend.ui_operation_graph import SCHEMA_VERSION as OPERATION_SCHEMA_VERSION


def graph() -> dict:
    return {
        "schema_version": "kt6.ui-graph.v1",
        "graph_id": "graph-test-1",
        "capture_id": "capture-1",
        "analysis_only": True,
        "execution_authorized": False,
        "nodes": [
            {
                "node_id": "dom-save",
                "source": {"kind": "dom", "source_ref": "#save"},
                "interaction": {"candidate": True},
                "disabled": False,
            }
        ],
        "edges": [],
    }


class FakePagePerception:
    def __init__(self, value=None):
        self.value = value

    def get_ui_graph(self, capture_id):
        return self.value if capture_id == "capture-1" else None


class FakeReasoner:
    reasoner_id = "internal-glm-test"
    reasoner_version = "5.1-test"

    def __init__(self, output, *, bind_graph_id=True):
        self.output = output
        self.bind_graph_id = bind_graph_id
        self.calls = []

    def plan(self, **kwargs):
        self.calls.append(kwargs)
        result = dict(self.output)
        if self.bind_graph_id:
            result.setdefault("graph_id", kwargs["graph_id"])
        return result


class UIGraphPlanningServiceTest(unittest.TestCase):
    def test_returns_validated_dry_run_plan_without_execution_authority(self):
        reasoner = FakeReasoner(
            {
                "schema_version": OPERATION_SCHEMA_VERSION,
                "steps": [
                    {
                        "id": "click-save",
                        "op": "click",
                        "target_node_id": "dom-save",
                    }
                ],
            }
        )
        service = UIGraphPlanningService(FakePagePerception(graph()), reasoner)

        result = service.plan(capture_id="capture-1", instruction="保存")

        self.assertEqual(result["status"], "planned")
        self.assertTrue(result["validation"]["valid"])
        self.assertTrue(result["dry_run_only"])
        self.assertFalse(result["safe_for_execution"])
        self.assertFalse(result["validation"]["safe_for_execution"])
        self.assertEqual(result["reasoner"]["reasoner_id"], "internal-glm-test")
        self.assertEqual(reasoner.calls[0]["graph_id"], "graph-test-1")
        self.assertIn('"graph_id":"graph-test-1"', reasoner.calls[0]["ui_graph_text"])

    def test_invalid_model_target_is_returned_as_rejected_proposal(self):
        reasoner = FakeReasoner(
            {
                "schema_version": OPERATION_SCHEMA_VERSION,
                "steps": [
                    {
                        "id": "click-missing",
                        "op": "click",
                        "target_node_id": "missing",
                    }
                ],
            }
        )
        service = UIGraphPlanningService(FakePagePerception(graph()), reasoner)

        result = service.plan(capture_id="capture-1", instruction="保存")

        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["validation"]["valid"])
        self.assertTrue(result["dry_run_only"])

    def test_large_graph_is_projected_under_byte_budget_with_candidate_parent_chain(self):
        value = graph()
        value["nodes"].insert(
            0,
            {
                "id": "dom-parent",
                "kind": "element",
                "role": "region",
                "name": "父容器",
                "source": {"kind": "dom", "source_ref": "#parent"},
                "interaction": {"candidate": False, "status": "analysis_only"},
            },
        )
        value["nodes"].extend(
            {
                "id": f"noise-{index:03d}",
                "kind": "element",
                "role": "note",
                "name": "无关长文本" * 80,
                "source": {"kind": "dom", "source_ref": f"#noise-{index}"},
                "interaction": {"candidate": False, "status": "analysis_only"},
            }
            for index in range(120)
        )
        value["edges"] = [
            {
                "id": "edge-parent",
                "type": "parent_of",
                "source": "dom-parent",
                "target": "dom-save",
            }
        ]
        reasoner = FakeReasoner(
            {
                "schema_version": OPERATION_SCHEMA_VERSION,
                "steps": [
                    {
                        "id": "click-save",
                        "op": "click",
                        "target_node_id": "dom-save",
                    }
                ],
            }
        )
        reasoner.MAX_GRAPH_TEXT_BYTES = 1800
        service = UIGraphPlanningService(FakePagePerception(value), reasoner)

        result = service.plan(capture_id="capture-1", instruction="保存")

        sent = reasoner.calls[0]["ui_graph_text"]
        view = json.loads(sent)
        self.assertLessEqual(len(sent.encode("utf-8")), 1800)
        self.assertTrue(result["model_projection"]["truncated"])
        self.assertEqual(view["graph_id"], "graph-test-1")
        self.assertIn("dom-save", {node["id"] for node in view["nodes"]})
        self.assertIn("dom-parent", {node["id"] for node in view["nodes"]})
        self.assertIn("parent_of", {edge["type"] for edge in view["edges"]})
        self.assertEqual(result["status"], "planned")

    def test_truncated_stored_graph_is_rejected_before_reasoner_call(self):
        value = graph()
        value["stats"] = {"truncated": True}
        reasoner = FakeReasoner(
            {
                "schema_version": OPERATION_SCHEMA_VERSION,
                "steps": [],
            }
        )
        service = UIGraphPlanningService(FakePagePerception(value), reasoner)

        with self.assertRaisesRegex(UIGraphProjectionError, "truncated"):
            service.plan(capture_id="capture-1", instruction="保存")
        self.assertEqual(reasoner.calls, [])

    def test_model_cannot_target_node_omitted_from_bounded_projection(self):
        value = graph()
        value["nodes"].extend(
            {
                "id": f"noise-{index:03d}",
                "kind": "element",
                "role": "note",
                "name": "irrelevant-long-text-" * 80,
                "source": {"kind": "text", "source_ref": f"noise-{index}"},
                "interaction": {"candidate": False, "status": "analysis_only"},
            }
            for index in range(40)
        )
        value["nodes"].append(
            {
                "id": "zz-hidden-target",
                "kind": "element",
                "role": "note",
                "name": "outside-budget-node",
                "source": {"kind": "text", "source_ref": "hidden"},
                "interaction": {"candidate": False, "status": "analysis_only"},
            }
        )
        reasoner = FakeReasoner(
            {
                "schema_version": OPERATION_SCHEMA_VERSION,
                "steps": [
                    {
                        "id": "locate-hidden",
                        "op": "locate",
                        "target_node_id": "zz-hidden-target",
                    }
                ],
            }
        )
        reasoner.MAX_GRAPH_TEXT_BYTES = 1_200
        service = UIGraphPlanningService(FakePagePerception(value), reasoner)

        result = service.plan(
            capture_id="capture-1",
            instruction="locate the outside-budget node",
        )

        sent_node_ids = {
            node["id"] for node in json.loads(reasoner.calls[0]["ui_graph_text"])["nodes"]
        }
        self.assertNotIn("zz-hidden-target", sent_node_ids)
        self.assertEqual(result["status"], "rejected")
        self.assertIn(
            "target_node_not_found",
            {error["code"] for error in result["validation"]["errors"]},
        )

    def test_missing_graph_and_unconfigured_reasoner_fail_explicitly(self):
        service = UIGraphPlanningService(FakePagePerception(None))
        with self.assertRaises(UIGraphNotFoundError):
            service.get_graph("capture-1")

        service = UIGraphPlanningService(FakePagePerception(graph()))
        with self.assertRaises(UIGraphReasonerNotConfiguredError):
            service.plan(capture_id="capture-1", instruction="保存")

        health = service.health()
        self.assertFalse(health["configured"])
        self.assertFalse(health["safe_for_execution"])

    def test_capture_and_model_graph_bindings_fail_closed(self):
        mismatched_graph = graph()
        mismatched_graph["capture_id"] = "capture-other"
        mismatched = UIGraphPlanningService(FakePagePerception(mismatched_graph))
        with self.assertRaises(UIGraphNotFoundError):
            mismatched.get_graph("capture-1")

        reasoner = FakeReasoner(
            {
                "schema_version": OPERATION_SCHEMA_VERSION,
                "steps": [{"id": "wait", "op": "wait"}],
            },
            bind_graph_id=False,
        )
        service = UIGraphPlanningService(FakePagePerception(graph()), reasoner)
        result = service.plan(capture_id="capture-1", instruction="wait")
        self.assertEqual(result["status"], "rejected")
        self.assertIn(
            "missing_graph_id",
            {error["code"] for error in result["validation"]["errors"]},
        )

        with self.assertRaisesRegex(ValueError, "instruction exceeds"):
            service.plan(
                capture_id="capture-1",
                instruction="x" * 8_001,
            )


if __name__ == "__main__":
    unittest.main()
