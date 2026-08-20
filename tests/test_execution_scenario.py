from __future__ import annotations

import base64
import binascii
import copy
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from kt6_backend.execution.action_planner import OpenAIActionPlanner
from kt6_backend.execution.browser_executor import HarnessBrowserExecutor
from kt6_backend.execution.error_categories import (
    EXECUTION_FAILED,
    PAGE_CHANGED,
    PERCEPTION_FAILED,
    PLANNER_FAILED,
    TARGET_AMBIGUOUS,
    TARGET_NOT_FOUND,
    VERIFY_FAILED,
    classify_error,
)
from kt6_backend.execution.grounding import TargetGrounderRegistry
from kt6_backend.execution.live_page_capture import _capture_canvases
from kt6_backend.execution.models import BrowserExecutionResult, BrowserTarget, VisualTarget
from kt6_backend.execution.plan_validator import (
    ACTION_PLAN_SCHEMA_VERSION,
    ActionPlanValidationError,
    ActionPlanValidator,
)
from kt6_backend.execution.scenario_runner import ScenarioRunner
from kt6_backend.execution.scenario_service import (
    ExecutionScenarioService,
    ExecutionScenarioServiceError,
)
from kt6_backend.execution.url_policy import (
    ExecutionURLPolicy,
    ExecutionURLPolicyError,
)
from kt6_backend.execution.verifier import UIGraphOutcomeVerifier
from kt6_backend.execution.verifier_registry import OutcomeVerifierRegistry


URL = "https://nce.test/devices"
TASK = "打开 AP_001 详情"
PUBLIC_TEST_IP = "93.184.216.34"


def public_url_policy() -> ExecutionURLPolicy:
    return ExecutionURLPolicy(resolver=lambda _host: (PUBLIC_TEST_IP,))


def semantic_plan(*, start_url=URL, user_request=TASK):
    return {
        "schema_version": ACTION_PLAN_SCHEMA_VERSION,
        "scenario_id": "generic-query-1",
        "start_url": start_url,
        "user_request": user_request,
        "steps": [
            {
                "id": "step-1",
                "op": "click",
                "target": {"query": "AP_001 详情", "asset_id": "ap_001"},
            },
            {
                "id": "step-2",
                "op": "verify",
                "expected": {
                    "type": "element_visible",
                    "target": {"query": "AP_001 详情面板"},
                },
            },
        ],
    }


def graph(capture_id, *, include_result=False, vision=False):
    nodes = [
        {
            "id": "cdp:detail-button",
            "kind": "control",
            "name": "AP_001 详情",
            "business_id": "ap_001",
            "owner_business_id": "ap_001",
            "action_id": "device.details",
            "role": "button",
            "disabled": False,
            "actionable": False,
            "can_click_now": False,
            "safe_for_execution": False,
            "bbox": [10, 10, 80, 30],
            "attributes": {
                "id": "ap-detail",
                "data-owner-business-id": "ap_001",
                "data-action-id": "device.details",
            },
            "source": {
                "kind": "cdp",
                "backend_node_id": 101,
                "frame_id": "main",
                "frame_url": URL,
            },
            "interaction": {"candidate": True, "status": "candidate_only"},
        }
    ]
    if include_result:
        nodes.append(
            {
                "id": "cdp:detail-panel",
                "kind": "text",
                "name": "AP_001 详情面板",
                "attributes": {"id": "detail-panel"},
                "source": {"kind": "cdp"},
                "interaction": {"candidate": False, "status": "analysis_only"},
                "safe_for_execution": False,
            }
        )
    if vision:
        nodes.extend(
            [
                {
                    "id": "vision:ap-001",
                    "kind": "business_object",
                    "name": "AP_001",
                    "business_id": "ap_001",
                    "confidence": 0.91,
                    "bbox": [100, 50, 40, 40],
                    "safe_for_execution": False,
                    "source": {
                        "kind": "vision",
                        "producer_id": "local-cv-ocr",
                        "canvas_id": "network-map",
                        "canvas_width": 400,
                        "canvas_height": 200,
                        "frame_id": "main",
                    },
                    "interaction": {"candidate": False, "status": "analysis_only"},
                },
                {
                    "id": "cdp:canvas",
                    "kind": "container",
                    "name": "Network map",
                    "attributes": {"id": "network-map"},
                    "source": {
                        "kind": "cdp",
                        "backend_node_id": 202,
                        "frame_id": "main",
                        "frame_url": URL,
                    },
                    "interaction": {"candidate": False, "status": "analysis_only"},
                    "safe_for_execution": False,
                },
            ]
        )
    return {
        "schema_version": "kt6.ui-graph.v1",
        "graph_id": f"uig:{capture_id}",
        "capture_id": capture_id,
        "page": {"url": URL, "title": "NCE"},
        "analysis_only": True,
        "execution_authorized": False,
        "safe_for_execution": False,
        "nodes": nodes,
        "edges": [],
        "issues": [],
        "stats": {"truncated": False},
    }


class URLAndPlanContractTest(unittest.TestCase):
    def test_url_policy_allows_arbitrary_public_hosts_without_a_host_list(self):
        policy = public_url_policy()
        self.assertEqual(policy.validate(URL), URL)
        self.assertEqual(
            policy.validate("https://other.test/devices"),
            "https://other.test/devices",
        )
        self.assertEqual(policy.health()["network_scope"], "public_only")
        self.assertNotIn("allowed_host_count", policy.health())

    def test_url_policy_blocks_private_and_mixed_dns_answers_by_default(self):
        answers = {
            "private.test": ("10.0.0.8",),
            "mixed.test": (PUBLIC_TEST_IP, "127.0.0.1"),
        }
        policy = ExecutionURLPolicy(resolver=lambda host: answers[host])

        for value in (
            "http://127.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "https://private.test/",
            "https://mixed.test/",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ExecutionURLPolicyError,
                "execution_url_network_blocked",
            ):
                policy.validate(value)

    def test_private_test_mode_allows_private_but_never_link_local(self):
        policy = ExecutionURLPolicy(allow_private_networks=True)
        self.assertEqual(policy.validate("http://127.0.0.1:8787/"), "http://127.0.0.1:8787/")
        self.assertEqual(policy.validate("https://10.0.0.8/"), "https://10.0.0.8/")
        with self.assertRaisesRegex(
            ExecutionURLPolicyError,
            "execution_url_network_blocked",
        ):
            policy.validate("http://169.254.169.254/latest/meta-data/")

    def test_synthetic_dns_support_does_not_allow_direct_benchmark_ip(self):
        policy = ExecutionURLPolicy(resolver=lambda _host: ("198.18.0.10",))
        self.assertEqual(
            policy.validate("https://public-through-proxy.test/"),
            "https://public-through-proxy.test/",
        )
        with self.assertRaisesRegex(
            ExecutionURLPolicyError,
            "execution_url_network_blocked",
        ):
            policy.validate("https://198.18.0.10/")

    def test_url_policy_rejects_unresolved_or_unsafe_url_shapes(self):
        unresolved = ExecutionURLPolicy(resolver=lambda _host: ())
        with self.assertRaisesRegex(
            ExecutionURLPolicyError,
            "execution_url_host_unresolved",
        ):
            unresolved.validate("https://missing.test/")

        policy = public_url_policy()
        for value in (
            "file:///tmp/test.html",
            "https://user:pass@example.test/",
            "https://example.test/#fragment",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ExecutionURLPolicyError,
                "execution_url_invalid",
            ):
                policy.validate(value)

    def test_plan_is_semantic_and_click_is_paired_with_outcome(self):
        validated = ActionPlanValidator().validate(semantic_plan())
        self.assertEqual(validated["steps"][0]["target"]["query"], "AP_001 详情")
        self.assertNotIn("source", repr(validated))
        self.assertNotIn("selector", repr(validated))
        self.assertNotIn("backend_node_id", repr(validated))

        invalid = semantic_plan()
        invalid["steps"] = invalid["steps"][:1]
        with self.assertRaises(ActionPlanValidationError):
            ActionPlanValidator().validate(invalid)


class PlannerAndServiceTest(unittest.TestCase):
    def test_openai_planner_validates_model_action_plan(self):
        class Result:
            def json_content(self):
                return semantic_plan()

        class Client:
            model = "approved-model"

            def complete(self, **kwargs):
                self.messages = kwargs["messages"]
                return Result()

        client = Client()
        planner = OpenAIActionPlanner(client=client, provider="internal")
        result = planner.plan(start_url=URL, user_request=TASK, ui_graph=graph("c1"))

        self.assertEqual(result["schema_version"], ACTION_PLAN_SCHEMA_VERSION)
        self.assertIn("untrusted page data", client.messages[0]["content"])

    def test_service_checks_public_network_policy_before_model_planning(self):
        class Runner:
            def inspect(self, start_url):
                self.start_url = start_url
                return {
                    "capture_id": "capture-plan",
                    "graph_id": "uig:capture-plan",
                    "ui_graph": graph("capture-plan"),
                    "preview_data_url": "data:image/jpeg;base64,/9j/2Q==",
                }

        class Planner:
            planner_id = "test-planner"
            planner_model = "test-model"

            def plan(self, **kwargs):
                self.ui_graph = kwargs["ui_graph"]
                return semantic_plan(
                    start_url=kwargs["start_url"],
                    user_request=kwargs["user_request"],
                )

        runner = Runner()
        planner = Planner()
        service = ExecutionScenarioService(
            root=Path("."),
            runner=runner,
            planner=planner,
            url_policy=public_url_policy(),
        )
        generated = service.generate_plan(start_url=URL, user_request=TASK)

        self.assertEqual(runner.start_url, URL)
        self.assertEqual(generated["planning_graph_id"], "uig:capture-plan")
        self.assertEqual(generated["planner"]["model"], "test-model")
        another = service.generate_plan(
            start_url="https://other.test/",
            user_request=TASK,
        )
        self.assertEqual(another["plan"]["start_url"], "https://other.test/")
        with self.assertRaises(ExecutionScenarioServiceError):
            service.generate_plan(
                start_url="http://127.0.0.1/",
                user_request=TASK,
            )


class UnifiedGroundingTest(unittest.TestCase):
    def test_registry_prefers_reliable_dom_candidate(self):
        target = TargetGrounderRegistry(vision_producer_id="local-cv-ocr").resolve(
            {"query": "AP_001 详情", "asset_id": "ap_001"},
            graph("c1", vision=True),
        )
        self.assertIsInstance(target, BrowserTarget)
        self.assertEqual(target.backend_node_id, 101)

    def test_registry_uses_configured_formal_vision_when_dom_is_missing(self):
        value = graph("c1", vision=True)
        value["nodes"] = [node for node in value["nodes"] if node["id"] != "cdp:detail-button"]
        target = TargetGrounderRegistry(vision_producer_id="local-cv-ocr").resolve(
            {"query": "AP_001", "asset_id": "ap_001"},
            value,
        )
        self.assertIsInstance(target, VisualTarget)
        self.assertEqual(target.canvas_backend_node_id, 202)
        self.assertEqual(target.producer_id, "local-cv-ocr")

    def test_live_capture_selects_largest_visible_canvas_without_fixed_id(self):
        def chunk(name, data):
            return (
                struct.pack(">I", len(data))
                + name
                + data
                + struct.pack(">I", binascii.crc32(name + data) & 0xFFFFFFFF)
            )

        scanlines = b"\x00" + b"\x20\xa6\x7a" * 2
        scanlines += b"\x00" + b"\x24\x6b\xfd" * 2
        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanlines))
            + chunk(b"IEND", b"")
        )

        canvases = _capture_canvases(
            lambda _method, **_params: {"data": base64.b64encode(png).decode()},
            {
                "nodes": [
                    {
                        "dom_node_type": 1,
                        "dom_node_name": "CANVAS",
                        "attributes": {"id": "network-map"},
                        "bounds": [5, 10, 300, 180],
                        "backend_node_id": 202,
                        "frame_id": "main",
                        "document_index": 0,
                    }
                ]
            },
            page_url=URL,
        )
        self.assertEqual(canvases[0]["canvas_id"], "network-map")
        self.assertEqual(canvases[0]["region_selector"], "#network-map")


class GenericScenarioRunnerTest(unittest.TestCase):
    def test_runner_executes_real_grounded_click_and_verifies_new_graph(self):
        class Client:
            def __init__(self):
                self.sequence = 0

            def open_or_bind_target(self, page_url):
                return {"target_id": "target-1", "page_url": page_url}

            def capture_page_payload(self, *, include_canvas=True):
                self.sequence += 1
                return {
                    "sequence": self.sequence,
                    "page_url": URL,
                    "preview_data_url": "data:image/jpeg;base64,/9j/2Q==",
                }

        class Perception:
            def __init__(self):
                self.snapshots = {}

            def ingest(self, payload):
                capture_id = f"capture-{payload['sequence']}"
                self.snapshots[capture_id] = {
                    "capture_id": capture_id,
                    "created_at": float(payload["sequence"]),
                    "dom": {"elements": []},
                }
                return {"capture_id": capture_id}

            def get_ui_graph(self, capture_id):
                sequence = int(capture_id.rsplit("-", 1)[1])
                return graph(capture_id, include_result=sequence >= 3)

            def get_action_snapshot(self, capture_id):
                return copy.deepcopy(self.snapshots[capture_id])

        class Executor:
            def __init__(self, client):
                self.client = client

            def execute(self, _action):
                return BrowserExecutionResult(True, "", backend_node_id=101, x=20, y=20)

        client = Client()
        runner = ScenarioRunner(
            page_perception=Perception(),
            browser_executor=Executor(client),
            grounders=TargetGrounderRegistry(),
            verifiers=OutcomeVerifierRegistry(
                [], ui_graph_verifier=UIGraphOutcomeVerifier()
            ),
            url_policy=public_url_policy(),
            clock=lambda: 10.0,
            wait=lambda _seconds: None,
        )
        scratch = Path.cwd() / ".test-tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp_dir:
            result = runner.run(
                semantic_plan(),
                run_id="run-generic",
                out_dir=Path(temp_dir) / "evidence",
                confirmed=True,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["capture_count"], 3)
        self.assertEqual(len(result["steps"]), 2)


class FailureCategoryAndGenericVerifierTest(unittest.TestCase):
    def test_classify_error_maps_loop_failures_to_stable_categories(self):
        cases = {
            "execution_planner_invalid_response": PLANNER_FAILED,
            "dom_grounding_target_missing": TARGET_NOT_FOUND,
            "dom_grounding_target_ambiguous": TARGET_AMBIGUOUS,
            "browser_target_ambiguous": TARGET_AMBIGUOUS,
            "scenario_capture_incomplete": PERCEPTION_FAILED,
            "browser_page_changed": PAGE_CHANGED,
            "browser_target_occluded": EXECUTION_FAILED,
            "browser_session_target_changed": EXECUTION_FAILED,
            "scenario_expected_outcome_missing": VERIFY_FAILED,
        }
        for code, category in cases.items():
            with self.subTest(code=code):
                self.assertEqual(classify_error(code), category)
        self.assertEqual(classify_error(""), "unclassified")
        self.assertEqual(classify_error("unknown_error"), "unclassified")

    def test_validator_accepts_generic_outcome_types(self):
        validator = ActionPlanValidator()
        for expected_type in (
            "element_disappeared",
            "text_present",
            "url_changed",
            "selected",
        ):
            with self.subTest(expected_type=expected_type):
                plan = semantic_plan()
                if expected_type == "url_changed":
                    plan["steps"][1]["expected"] = {"type": expected_type}
                else:
                    plan["steps"][1]["expected"] = {
                        "type": expected_type,
                        "target": {"query": "AP_001 详情面板"},
                    }
                validator.validate(plan)

    def test_ui_graph_verifier_supports_generic_outcomes(self):
        verifier = UIGraphOutcomeVerifier()
        before = graph("c-before")
        after = graph("c-after", include_result=True)

        self.assertTrue(
            verifier.verify(
                expected={
                    "type": "element_visible",
                    "target": {"query": "AP_001 详情面板"},
                },
                before=before,
                after=after,
            )
        )
        self.assertTrue(
            verifier.verify(
                expected={
                    "type": "text_present",
                    "target": {"query": "AP_001 详情"},
                },
                before=before,
                after=after,
            )
        )

        gone = graph("c-gone")
        gone["nodes"] = [
            node for node in gone["nodes"] if node["id"] != "cdp:detail-button"
        ]
        self.assertTrue(
            verifier.verify(
                expected={
                    "type": "element_disappeared",
                    "target": {"query": "AP_001 详情"},
                },
                before=before,
                after=gone,
            )
        )

        changed = graph("c-changed")
        changed["page"] = {"url": URL + "/next", "title": "NCE"}
        self.assertTrue(
            verifier.verify(
                expected={"type": "url_changed"},
                before=before,
                after=changed,
            )
        )


if __name__ == "__main__":
    unittest.main()
