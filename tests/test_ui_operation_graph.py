import json
import unittest

from kt6_backend.ui_operation_graph import (
    SCHEMA_VERSION,
    operation_plan_schema,
    validate,
)


def ui_graph() -> dict:
    return {
        "schema_version": "kt6.ui-graph.v1",
        "graph_id": "uig-test-123",
        "analysis_only": True,
        "execution_authorized": False,
        "nodes": [
            {
                "node_id": "dom-submit",
                "source": {"kind": "dom", "source_ref": "#submit"},
                "actionable": False,
                "disabled": False,
                "interaction": {
                    "status": "candidate_only",
                    "candidate": True,
                    "can_click_now": False,
                    "safe_for_execution": False,
                },
            },
            {
                "node_id": "cdp-menu",
                "source": {"kind": "cdp", "backend_node_id": 42},
                "actionable": False,
                "interaction": {
                    "status": "candidate_only",
                    "candidate": True,
                    "can_click_now": False,
                    "safe_for_execution": False,
                },
            },
            {
                "node_id": "page-api-device",
                "source": {"kind": "page_api"},
                "actionable": True,
                "actionable_grounding": True,
                "interaction": {"candidate": True},
            },
            {
                "node_id": "vision-icon",
                "source": {"kind": "vision"},
                "actionable": True,
                "interaction": {"candidate": True},
            },
            {
                "node_id": "text-label",
                "source": {"kind": "text"},
                "actionable": True,
                "interaction": {"candidate": True},
            },
            {
                "node_id": "fused-control",
                "source": {"kind": "vision"},
                "actionable": True,
                "candidates": [
                    {
                        "source": {"kind": "dom", "source_ref": "#fused"},
                        "actionable": False,
                        "disabled": False,
                        "interaction": {"candidate": True},
                    }
                ],
            },
        ],
    }

def plan(*steps: dict) -> dict:
    return {"schema_version": SCHEMA_VERSION, "steps": list(steps)}


def error_codes(result: dict) -> set[str]:
    return {item["code"] for item in result["errors"]}


class UIOperationGraphTest(unittest.TestCase):
    def test_schema_is_stable_and_returned_as_a_copy(self):
        first = operation_plan_schema()
        self.assertEqual(first["properties"]["schema_version"]["const"], SCHEMA_VERSION)
        self.assertEqual(
            first["properties"]["steps"]["items"]["properties"]["op"]["enum"],
            ["locate", "click", "wait", "verify"],
        )
        self.assertEqual(
            first["properties"]["graph_id"],
            {"type": "string", "minLength": 1},
        )
        self.assertNotIn("graph_id", first["required"])

        first["properties"]["schema_version"]["const"] = "mutated"
        second = operation_plan_schema()
        self.assertEqual(second["properties"]["schema_version"]["const"], SCHEMA_VERSION)

    def test_optional_graph_id_must_match_and_is_preserved(self):
        step = {"id": "wait", "op": "wait"}

        missing = validate(ui_graph(), plan(step))
        self.assertTrue(missing["valid"], missing["errors"])
        self.assertNotIn("graph_id", missing["plan"])

        required_missing = validate(
            ui_graph(),
            plan(step),
            require_graph_id=True,
        )
        self.assertFalse(required_missing["valid"])
        self.assertIn("missing_graph_id", error_codes(required_missing))

        matching_payload = plan(step)
        matching_payload["graph_id"] = "uig-test-123"
        matching = validate(ui_graph(), matching_payload)
        self.assertTrue(matching["valid"], matching["errors"])
        self.assertEqual(matching["plan"]["graph_id"], "uig-test-123")

        mismatched_payload = plan(step)
        mismatched_payload["graph_id"] = "uig-other"
        mismatched = validate(ui_graph(), mismatched_payload)
        self.assertFalse(mismatched["valid"])
        self.assertIn("graph_id_mismatch", error_codes(mismatched))
        self.assertEqual(mismatched["plan"]["graph_id"], "uig-other")

        for invalid_graph_id in ("", "   ", 123, None):
            with self.subTest(graph_id=invalid_graph_id):
                invalid_payload = plan(step)
                invalid_payload["graph_id"] = invalid_graph_id
                invalid = validate(ui_graph(), invalid_payload)
                self.assertFalse(invalid["valid"])
                self.assertIn("invalid_graph_id", error_codes(invalid))
                self.assertNotIn("graph_id", invalid["plan"])

        missing_server_graph = validate(
            {"nodes": []},
            {**plan(step), "graph_id": "uig-test-123"},
        )
        self.assertIn("graph_id_mismatch", error_codes(missing_server_graph))

    def test_valid_multistep_dag_is_normalized_but_never_executable(self):
        output = plan(
            {
                "id": "locate-submit",
                "op": "locate",
                "target_node_id": "dom-submit",
            },
            {
                "id": "click-submit",
                "op": "click",
                "target_node_id": "dom-submit",
                "depends_on": ["locate-submit"],
                "condition": {
                    "step_id": "locate-submit",
                    "predicate": "found",
                },
            },
            {
                "id": "wait-result",
                "op": "wait",
                "depends_on": ["click-submit"],
                "args": {"timeout_ms": 1500},
            },
            {
                "id": "verify-result",
                "op": "verify",
                "target_node_id": "dom-submit",
                "depends_on": ["wait-result"],
                "condition": {
                    "step_id": "click-submit",
                    "predicate": "succeeded",
                },
                "args": {"expected": True},
            },
        )

        result = validate(ui_graph(), output, instruction="提交表单")

        self.assertTrue(result["valid"])
        self.assertTrue(result["dry_run_only"])
        self.assertFalse(result["safe_for_execution"])
        self.assertEqual(result["instruction"], "提交表单")
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [item["op"] for item in result["plan"]["steps"]],
            ["locate", "click", "wait", "verify"],
        )
        self.assertIn(
            "click_target_candidate_grounding_validated",
            result["safety_reasons"],
        )
        self.assertIn(
            "click_candidate_is_not_execution_authorization",
            result["safety_reasons"],
        )
        self.assertIn(
            "live_execution_requires_separate_preflight_and_authorization",
            result["safety_reasons"],
        )

    def test_cdp_or_explicit_nested_dom_candidate_can_ground_dry_run_click(self):
        for target in ("cdp-menu", "fused-control"):
            with self.subTest(target=target):
                result = validate(
                    ui_graph(),
                    plan(
                        {
                            "id": "click-target",
                            "op": "click",
                            "target_node_id": target,
                        }
                    ),
                )
                self.assertTrue(result["valid"], result["errors"])

    def test_blocking_status_wins_even_when_candidate_is_true(self):
        for status in ("analysis_only", "blocked", "disabled", "not_actionable", "rejected"):
            with self.subTest(status=status):
                graph = {
                    "nodes": [
                        {
                            "node_id": "blocked-dom",
                            "source": {"kind": "dom", "source_ref": "#blocked"},
                            "interaction": {"candidate": True, "status": status},
                        }
                    ]
                }
                result = validate(
                    graph,
                    plan(
                        {
                            "id": "click-blocked",
                            "op": "click",
                            "target_node_id": "blocked-dom",
                        }
                    ),
                )
                self.assertFalse(result["valid"])
                self.assertIn("click_target_not_authorized", error_codes(result))

    def test_nested_candidate_inherits_outer_disabled_and_blocked_state(self):
        for outer_state in (
            {"disabled": True},
            {"interaction": {"status": "blocked"}},
        ):
            with self.subTest(outer_state=outer_state):
                node = {
                    "node_id": "fused-disabled",
                    "source": {"kind": "vision"},
                    "candidates": [
                        {
                            "source": {"kind": "dom", "source_ref": "#nested"},
                            "interaction": {"candidate": True, "status": "candidate_only"},
                        }
                    ],
                }
                node.update(outer_state)
                result = validate(
                    {"nodes": [node]},
                    plan(
                        {
                            "id": "click-nested",
                            "op": "click",
                            "target_node_id": "fused-disabled",
                        }
                    ),
                )
                self.assertFalse(result["valid"])
                self.assertIn("click_target_not_authorized", error_codes(result))

    def test_dom_and_cdp_candidates_require_rebindable_identity(self):
        graph = {
            "nodes": [
                {
                    "node_id": "dom-without-ref",
                    "source": {"kind": "dom"},
                    "interaction": {"candidate": True, "status": "candidate_only"},
                },
                {
                    "node_id": "cdp-without-backend",
                    "source": {"kind": "cdp"},
                    "interaction": {"candidate": True, "status": "candidate_only"},
                },
                {
                    "node_id": "cdp-zero-backend",
                    "source": {"kind": "cdp", "backend_node_id": 0},
                    "interaction": {"candidate": True, "status": "candidate_only"},
                },
                {
                    "node_id": "dom-temporary-ref",
                    "source": {"kind": "dom", "source_ref": "@CAPTURE:button:1"},
                    "interaction": {"candidate": True, "status": "candidate_only"},
                },
            ]
        }
        for target in (
            "dom-without-ref",
            "cdp-without-backend",
            "cdp-zero-backend",
            "dom-temporary-ref",
        ):
            with self.subTest(target=target):
                result = validate(
                    graph,
                    plan(
                        {
                            "id": "click-unbound",
                            "op": "click",
                            "target_node_id": target,
                        }
                    ),
                )
                self.assertFalse(result["valid"])
                self.assertIn("click_target_not_authorized", error_codes(result))

        selector_bound = validate(
            {
                "nodes": [
                    {
                        "node_id": "selector-bound",
                        "source": {"kind": "dom"},
                        "selector": "button[type=submit]",
                        "interaction": {"candidate": True, "status": "candidate_only"},
                    }
                ]
            },
            plan(
                {
                    "id": "click-selector",
                    "op": "click",
                    "target_node_id": "selector-bound",
                }
            ),
        )
        self.assertTrue(selector_bound["valid"], selector_bound["errors"])

    def test_page_api_vision_and_text_cannot_self_authorize_click(self):
        for target in ("page-api-device", "vision-icon", "text-label"):
            with self.subTest(target=target):
                result = validate(
                    ui_graph(),
                    plan(
                        {
                            "id": "click-untrusted",
                            "op": "click",
                            "target_node_id": target,
                        }
                    ),
                )
                self.assertFalse(result["valid"])
                self.assertIn("click_target_not_authorized", error_codes(result))
                self.assertTrue(result["dry_run_only"])
                self.assertFalse(result["safe_for_execution"])

    def test_non_click_analysis_steps_may_reference_non_actionable_sources(self):
        result = validate(
            ui_graph(),
            plan(
                {
                    "id": "locate-hint",
                    "op": "locate",
                    "target_node_id": "vision-icon",
                },
                {
                    "id": "verify-label",
                    "op": "verify",
                    "target_node_id": "text-label",
                    "depends_on": ["locate-hint"],
                },
            ),
        )
        self.assertTrue(result["valid"], result["errors"])

    def test_click_requires_explicit_candidate_and_rejects_raw_actionable(self):
        graph = {
            "nodes": [
                {
                    "node_id": "string-true",
                    "source": {"kind": "dom"},
                    "actionable": "true",
                },
                {
                    "node_id": "raw-actionable",
                    "source": {"kind": "dom", "source_ref": "#raw"},
                    "actionable": True,
                },
                {
                    "node_id": "disabled",
                    "source": {"kind": "cdp"},
                    "actionable": True,
                    "disabled": True,
                },
                {
                    "node_id": "analysis-only",
                    "source": {"kind": "dom"},
                    "actionable": True,
                    "interaction": {"status": "analysis_only"},
                },
            ]
        }
        for target in (
            "string-true",
            "raw-actionable",
            "disabled",
            "analysis-only",
        ):
            with self.subTest(target=target):
                result = validate(
                    graph,
                    plan(
                        {
                            "id": "click-target",
                            "op": "click",
                            "target_node_id": target,
                        }
                    ),
                )
                self.assertIn("click_target_not_authorized", error_codes(result))

    def test_unknown_or_ambiguous_target_is_rejected(self):
        missing = validate(
            ui_graph(),
            plan(
                {
                    "id": "click-missing",
                    "op": "click",
                    "target_node_id": "missing-node",
                }
            ),
        )
        self.assertIn("target_node_not_found", error_codes(missing))

        duplicate_graph = {
            "nodes": [
                {"node_id": "same", "source": "dom", "actionable": True},
                {"node_id": "same", "source": "cdp", "actionable": True},
            ]
        }
        ambiguous = validate(
            duplicate_graph,
            plan(
                {
                    "id": "click-same",
                    "op": "click",
                    "target_node_id": "same",
                }
            ),
        )
        self.assertIn("ambiguous_ui_node_id", error_codes(ambiguous))
        self.assertIn("ambiguous_target_node", error_codes(ambiguous))

    def test_dag_rejects_cycles_unknown_dependencies_and_unordered_conditions(self):
        cycle = validate(
            ui_graph(),
            plan(
                {"id": "a", "op": "wait", "depends_on": ["b"]},
                {"id": "b", "op": "wait", "depends_on": ["a"]},
            ),
        )
        self.assertIn("dependency_cycle", error_codes(cycle))

        invalid_references = validate(
            ui_graph(),
            plan(
                {"id": "a", "op": "wait", "depends_on": ["missing"]},
                {
                    "id": "b",
                    "op": "wait",
                    "condition": {"step_id": "a", "predicate": "succeeded"},
                },
            ),
        )
        self.assertIn("unknown_dependency", error_codes(invalid_references))
        self.assertIn("condition_step_not_dependency", error_codes(invalid_references))

    def test_freeform_conditions_operations_and_script_args_are_rejected(self):
        result = validate(
            ui_graph(),
            plan(
                {
                    "id": "unsafe",
                    "op": "javascript",
                    "condition": "window.confirm('run')",
                    "args": {"script": "document.body.click()"},
                }
            ),
        )
        self.assertFalse(result["valid"])
        self.assertIn("unsupported_operation", error_codes(result))
        self.assertIn("condition_not_object", error_codes(result))
        self.assertIn("unsupported_args", error_codes(result))
        self.assertNotIn("script", result["plan"]["steps"][0].get("args", {}))

    def test_json_input_rejects_duplicate_keys_and_non_finite_numbers(self):
        duplicate = (
            '{"schema_version":"%s","steps":[],"steps":[]}' % SCHEMA_VERSION
        )
        duplicate_result = validate(ui_graph(), duplicate)
        self.assertIn("duplicate_json_key", error_codes(duplicate_result))

        non_finite = json.dumps(
            plan({"id": "wait", "op": "wait", "args": {"expected": 1.0}})
        ).replace("1.0", "NaN")
        non_finite_result = validate(ui_graph(), non_finite)
        self.assertIn("invalid_model_json", error_codes(non_finite_result))

    def test_mapping_input_obeys_strict_json_and_utf8_size_limit(self):
        oversized = {
            "schema_version": SCHEMA_VERSION,
            "steps": [],
            "padding": "界" * 22_000,
        }
        oversized_result = validate(ui_graph(), oversized)
        self.assertIn("model_output_too_large", error_codes(oversized_result))

        non_finite = plan(
            {"id": "wait", "op": "wait", "args": {"expected": float("nan")}}
        )
        non_finite_result = validate(ui_graph(), non_finite)
        self.assertIn("invalid_model_json", error_codes(non_finite_result))

    def test_mapping_form_nodes_and_json_plan_are_supported(self):
        graph = {
            "graph": {
                "nodes": {
                    "dom-save": {
                        "source": {"kind": "dom", "source_ref": "#save"},
                        "interaction": {
                            "candidate": True,
                            "status": "candidate_only",
                        },
                    }
                }
            }
        }
        output = json.dumps(
            plan(
                {
                    "id": "click-save",
                    "op": "click",
                    "target_node_id": "dom-save",
                }
            )
        )
        result = validate(graph, output)
        self.assertTrue(result["valid"], result["errors"])


if __name__ == "__main__":
    unittest.main()
