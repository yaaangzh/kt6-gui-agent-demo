from __future__ import annotations

import base64
import binascii
import copy
from contextlib import nullcontext
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from kt6_backend.execution.action_planner import OpenAIActionPlanner
from kt6_backend.execution.browser_executor import HarnessBrowserExecutor
from kt6_backend.execution.browser_harness_client import BrowserHarnessError
from kt6_backend.execution.error_categories import (
    CANCELLED,
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
from kt6_backend.execution.scenario_runner import ScenarioExecutionError, ScenarioRunner
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


def type_plan(*, start_url=URL, user_request="搜索前端开源项目"):
    target = {"query": "搜索框", "role": "textbox"}
    return {
        "schema_version": ACTION_PLAN_SCHEMA_VERSION,
        "scenario_id": "generic-type-1",
        "start_url": start_url,
        "user_request": user_request,
        "steps": [
            {
                "id": "step-1",
                "op": "type",
                "target": target,
                "text": "找一个前端开源项目",
            },
            {
                "id": "step-2",
                "op": "verify",
                "expected": {
                    "type": "input_value",
                    "target": target,
                    "value": "找一个前端开源项目",
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


def custom_menu_graph(capture_id, *, menu_open=False, selected=False):
    nodes = [
        {
            "id": "cdp:time-control",
            "name": "时间维度 近 7 天" if selected else "时间维度 今天",
            "role": "button",
            "disabled": False,
            "safe_for_execution": False,
            "can_click_now": False,
            "bbox": [100, 100, 160, 36],
            "attributes": {"backend_node_id": 10},
            "source": {
                "kind": "cdp",
                "backend_node_id": 10,
                "frame_id": "main",
                "frame_url": URL,
            },
            "interaction": {"candidate": True, "status": "candidate_only"},
        }
    ]
    edges = []
    if menu_open:
        nodes.extend(
            [
                {
                    "id": "cdp:seven-day-option",
                    "name": "",
                    "role": "generic",
                    "disabled": False,
                    "safe_for_execution": False,
                    "can_click_now": False,
                    "bbox": [100, 150, 120, 38],
                    "attributes": {"backend_node_id": 20},
                    "source": {
                        "kind": "cdp",
                        "backend_node_id": 20,
                        "frame_id": "main",
                        "frame_url": URL,
                    },
                    "interaction": {
                        "candidate": True,
                        "status": "candidate_only",
                    },
                },
                {
                    "id": "cdp:seven-day-label",
                    "name": "近 7 天",
                    "role": "StaticText",
                    "safe_for_execution": False,
                    "source": {"kind": "cdp", "frame_id": "main"},
                    "interaction": {
                        "candidate": False,
                        "status": "analysis_only",
                    },
                },
            ]
        )
        edges.append(
            {
                "type": "parent_of",
                "source": "cdp:seven-day-option",
                "target": "cdp:seven-day-label",
            }
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
        "edges": edges,
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
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ExecutionURLPolicyError,
                "execution_url_invalid",
            ):
                policy.validate(value)
        self.assertEqual(
            policy.validate("https://example.test/#fragment"),
            "https://example.test/#fragment",
        )

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

        fragment_plan = semantic_plan(start_url="https://example.test/#assistant")
        self.assertEqual(
            ActionPlanValidator().validate(fragment_plan)["start_url"],
            "https://example.test/#assistant",
        )

    def test_wait_has_no_business_deadline(self):
        plan = semantic_plan()
        plan["steps"][1]["op"] = "wait"
        validated = ActionPlanValidator().validate(plan)
        self.assertNotIn("timeout_ms", validated["steps"][1])

        plan["steps"][1]["timeout_ms"] = 30_000
        with self.assertRaisesRegex(
            ActionPlanValidationError,
            "action_plan_condition_fields_invalid",
        ):
            ActionPlanValidator().validate(plan)

    def test_type_requires_exact_input_value_verification(self):
        validator = ActionPlanValidator()
        validated = validator.validate(type_plan())
        self.assertEqual(validated["steps"][0]["text"], "找一个前端开源项目")

        for mutate in (
            lambda plan: plan["steps"][1]["expected"].update(value="其他内容"),
            lambda plan: plan["steps"][1].update(op="wait", timeout_ms=1000),
            lambda plan: plan["steps"][0].update(text="line\nbreak"),
        ):
            with self.subTest(mutate=mutate):
                invalid = type_plan()
                mutate(invalid)
                with self.assertRaises(ActionPlanValidationError):
                    validator.validate(invalid)

    def test_bounded_extended_actions_validate_with_required_outcomes(self):
        validator = ActionPlanValidator()
        target = {"query": "搜索框", "role": "textbox"}
        cases = (
            (
                {"id": "step-1", "op": "double_click", "target": target},
                {
                    "id": "step-2",
                    "op": "verify",
                    "expected": {"type": "page_changed"},
                },
            ),
            (
                {"id": "step-1", "op": "hover", "target": target},
                {
                    "id": "step-2",
                    "op": "wait",
                    "expected": {
                        "type": "element_visible",
                        "target": {"query": "提示"},
                    },
                },
            ),
            (
                {
                    "id": "step-1",
                    "op": "press_key",
                    "target": target,
                    "key": "Enter",
                },
                {
                    "id": "step-2",
                    "op": "verify",
                    "expected": {"type": "page_changed"},
                },
            ),
            (
                {
                    "id": "step-1",
                    "op": "scroll",
                    "direction": "down",
                    "amount": "page",
                },
                {
                    "id": "step-2",
                    "op": "wait",
                    "expected": {
                        "type": "element_visible",
                        "target": {"query": "页面底部"},
                    },
                },
            ),
            (
                {
                    "id": "step-1",
                    "op": "select_option",
                    "target": {"query": "时间范围", "role": "combobox"},
                    "option": "近 7 天",
                },
                {
                    "id": "step-2",
                    "op": "verify",
                    "expected": {
                        "type": "selected",
                        "target": {"query": "近 7 天", "role": "option"},
                    },
                },
            ),
        )
        for action, outcome in cases:
            with self.subTest(op=action["op"]):
                plan = semantic_plan()
                plan["steps"] = [action, outcome]
                self.assertEqual(validator.validate(plan)["steps"][0]["op"], action["op"])

    def test_extended_actions_reject_unbounded_parameters(self):
        invalid_actions = (
            {"id": "step-1", "op": "press_key", "target": {"query": "输入框"}, "key": "F12"},
            {"id": "step-1", "op": "scroll", "direction": "left", "amount": "infinite"},
            {"id": "step-1", "op": "select_option", "target": {"query": "范围"}, "option": ""},
        )
        for action in invalid_actions:
            with self.subTest(op=action["op"]):
                plan = semantic_plan()
                plan["steps"][0] = action
                with self.assertRaises(ActionPlanValidationError):
                    ActionPlanValidator().validate(plan)


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
        self.assertIn("double_click", client.messages[0]["content"])
        self.assertIn("select_option", client.messages[0]["content"])
        self.assertEqual(planner.last_plan_metrics["model_calls"], 1)
        self.assertLessEqual(planner.last_plan_metrics["projection_bytes"], 48 * 1024)
        self.assertGreater(planner.last_plan_metrics["projection_nodes"], 0)

    def test_deepseek_planner_disables_thinking_for_json_plan(self):
        class Result:
            @staticmethod
            def json_content():
                return semantic_plan()

        class Client:
            model = "deepseek-v4-pro"

            def complete(self, **kwargs):
                self.options = kwargs
                return Result()

        client = Client()
        planner = OpenAIActionPlanner(client=client, provider="deepseek")
        planner.plan(start_url=URL, user_request=TASK, ui_graph=graph("c1"))

        self.assertEqual(
            client.options["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_openai_planner_retries_one_invalid_plan_response(self):
        invalid_plan = semantic_plan()
        invalid_plan["steps"] = []

        class Result:
            def __init__(self, content):
                self.content = content

            def json_content(self):
                return self.content

        class Client:
            model = "approved-model"

            def __init__(self):
                self.calls = []

            def complete(self, **kwargs):
                self.calls.append(kwargs)
                content = invalid_plan if len(self.calls) == 1 else semantic_plan()
                return Result(content)

        client = Client()
        planner = OpenAIActionPlanner(client=client, provider="internal")

        result = planner.plan(
            start_url=URL,
            user_request=TASK,
            ui_graph=graph("c1"),
        )

        self.assertEqual(result["schema_version"], ACTION_PLAN_SCHEMA_VERSION)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(planner.last_plan_metrics["model_calls"], 2)
        self.assertIn(
            "previous response violated",
            client.calls[1]["messages"][-1]["content"],
        )

    def test_service_checks_public_network_policy_before_model_planning(self):
        class Runner:
            def inspect(self, start_url, *, user_request=""):
                self.start_url = start_url
                self.user_request = user_request
                return {
                    "capture_id": "capture-plan",
                    "graph_id": "uig:capture-plan",
                    "ui_graph": graph("capture-plan"),
                    "capture_metrics": {"total": 12.5},
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
        self.assertEqual(runner.user_request, TASK)
        self.assertEqual(generated["planning_graph_id"], "uig:capture-plan")
        self.assertNotIn("planning_preview", generated)
        self.assertEqual(generated["planning_metrics"]["capture"]["total"], 12.5)
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
    def test_visual_capture_profile_is_selected_by_requested_capability(self):
        self.assertIsNone(ScenarioRunner._vision_profile("输入 ainfrra，点击百度一下"))
        self.assertEqual(
            ScenarioRunner._vision_profile("点击拓扑节点 AP-1"),
            "nodes_only",
        )
        self.assertEqual(
            ScenarioRunner._vision_profile("分析 AP-1 和网关的连接关系"),
            "connectivity_query",
        )
        self.assertEqual(
            ScenarioRunner._vision_profile("解释图中的设备类型"),
            "semantic_enrichment",
        )

    def test_inspection_maps_harness_startup_failure_to_scenario_error(self):
        class Client:
            exclusive_session = staticmethod(nullcontext)

            def open_or_bind_target(self, _page_url):
                raise BrowserHarnessError("browser_harness_daemon_unavailable")

        class Executor:
            client = Client()

        runner = ScenarioRunner(
            page_perception=object(),
            browser_executor=Executor(),
            grounders=TargetGrounderRegistry(),
            verifiers=OutcomeVerifierRegistry(
                [], ui_graph_verifier=UIGraphOutcomeVerifier()
            ),
            url_policy=public_url_policy(),
        )

        with self.assertRaisesRegex(
            ScenarioExecutionError,
            "browser_harness_daemon_unavailable",
        ) as raised:
            runner.inspect(URL)
        self.assertEqual(
            raised.exception.error_code,
            "browser_harness_daemon_unavailable",
        )

    def test_runner_builds_guarded_extended_browser_actions(self):
        class Executor:
            client = object()

            def __init__(self):
                self.actions = []

            def execute(self, action):
                self.actions.append(action)
                backend_node_id = (
                    action.target.backend_node_id
                    if isinstance(action.target, BrowserTarget)
                    else None
                )
                return BrowserExecutionResult(True, "", backend_node_id=backend_node_id)

        executor = Executor()
        runner = ScenarioRunner(
            page_perception=object(),
            browser_executor=executor,
            grounders=TargetGrounderRegistry(),
            verifiers=OutcomeVerifierRegistry(
                [], ui_graph_verifier=UIGraphOutcomeVerifier()
            ),
            url_policy=public_url_policy(),
        )
        capture_index = 0

        def capture(_label, **_kwargs):
            nonlocal capture_index
            capture_index += 1
            capture_id = f"extended-{capture_index}"
            current_graph = graph(capture_id)
            return {"capture_id": capture_id}, current_graph, {}

        for op in ("double_click", "hover"):
            runner._pointer_action(
                {"id": f"step-{capture_index + 1}", "op": op, "target": {"query": "AP_001 详情"}},
                pending=None,
                capture=capture,
            )
        runner._press_key(
            {
                "id": "step-key",
                "op": "press_key",
                "target": {"query": "AP_001 详情"},
                "key": "Enter",
            },
            pending=None,
            capture=capture,
        )
        runner._scroll(
            {"id": "step-scroll", "op": "scroll", "direction": "down", "amount": "page"},
            pending=None,
            capture=capture,
        )

        def select_capture(_label, **_kwargs):
            nonlocal capture_index
            capture_index += 1
            capture_id = f"select-{capture_index}"
            current_graph = graph(capture_id)
            current_graph["nodes"][0]["name"] = "时间范围"
            current_graph["nodes"][0]["role"] = "combobox"
            return {"capture_id": capture_id}, current_graph, {}

        runner._select_option(
            {
                "id": "step-select",
                "op": "select_option",
                "target": {"query": "时间范围", "role": "combobox"},
                "option": "近 7 天",
            },
            pending=None,
            capture=select_capture,
        )

        self.assertEqual(
            [action.op for action in executor.actions],
            ["double_click", "hover", "press_key", "scroll", "select_option"],
        )
        self.assertEqual(executor.actions[2].key, "Enter")
        self.assertIsNone(executor.actions[3].target)
        self.assertEqual(executor.actions[4].option, "近 7 天")

    def test_runner_executes_real_grounded_click_and_verifies_new_graph(self):
        class Client:
            exclusive_session = staticmethod(nullcontext)

            def __init__(self):
                self.sequence = 0

            def open_or_bind_target(self, page_url):
                return {"target_id": "target-1", "page_url": page_url}

            def capture_page_payload(
                self, *, include_canvas=True, include_preview=True
            ):
                self.sequence += 1
                return {
                    "sequence": self.sequence,
                    "page_url": URL,
                }

        class Perception:
            def __init__(self):
                self.snapshots = {}

            def ingest(self, payload, *, persist=True, include_execution_views=False):
                capture_id = f"capture-{payload['sequence']}"
                self.snapshots[capture_id] = {
                    "capture_id": capture_id,
                    "created_at": float(payload["sequence"]),
                    "dom": {"elements": []},
                }
                sequence = int(capture_id.rsplit("-", 1)[1])
                return {
                    "capture_id": capture_id,
                    "summary": {},
                    "ui_graph": graph(capture_id, include_result=sequence >= 3),
                    "action_snapshot": copy.deepcopy(self.snapshots[capture_id]),
                }

            def persist_capture(self, _capture_id):
                return None

            def discard_capture(self, _capture_id):
                return None

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

    def test_runner_executes_grounded_type_and_verifies_exact_value(self):
        class Client:
            exclusive_session = staticmethod(nullcontext)

            def __init__(self):
                self.sequence = 0

            def open_or_bind_target(self, page_url):
                return {"target_id": "target-input", "page_url": page_url}

            def capture_page_payload(
                self, *, include_canvas=True, include_preview=True
            ):
                self.sequence += 1
                return {"sequence": self.sequence, "page_url": URL}

        class Perception:
            def __init__(self):
                self.snapshots = {}

            def ingest(self, payload, *, persist=True, include_execution_views=False):
                capture_id = f"capture-type-{payload['sequence']}"
                self.snapshots[capture_id] = {"capture_id": capture_id}
                sequence = int(capture_id.rsplit("-", 1)[1])
                value = graph(capture_id)
                value["nodes"] = [
                    {
                        "id": "cdp:search",
                        "kind": "control",
                        "name": "当前动态热词",
                        "role": "textbox",
                        "disabled": False,
                        "actionable": False,
                        "can_click_now": False,
                        "safe_for_execution": False,
                        "bbox": [10, 10, 180, 30],
                        "attributes": {
                            "id": "search-input",
                            "value": "找一个前端开源项目" if sequence >= 3 else "",
                        },
                        "source": {
                            "kind": "cdp",
                            "backend_node_id": 303,
                            "frame_id": "main",
                            "frame_url": URL,
                        },
                        "interaction": {"candidate": True, "status": "candidate_only"},
                    }
                ]
                return {
                    "capture_id": capture_id,
                    "summary": {},
                    "ui_graph": value,
                    "action_snapshot": self.snapshots[capture_id],
                }

            def persist_capture(self, _capture_id):
                return None

            def discard_capture(self, _capture_id):
                return None

            def get_ui_graph(self, capture_id):
                sequence = int(capture_id.rsplit("-", 1)[1])
                value = graph(capture_id)
                value["nodes"] = [
                    {
                        "id": "cdp:search",
                        "kind": "control",
                        "name": "当前动态热词",
                        "role": "textbox",
                        "disabled": False,
                        "actionable": False,
                        "can_click_now": False,
                        "safe_for_execution": False,
                        "bbox": [10, 10, 180, 30],
                        "attributes": {
                            "id": "search-input",
                            "value": "找一个前端开源项目" if sequence >= 3 else "",
                        },
                        "source": {
                            "kind": "cdp",
                            "backend_node_id": 303,
                            "frame_id": "main",
                            "frame_url": URL,
                        },
                        "interaction": {"candidate": True, "status": "candidate_only"},
                    }
                ]
                return value

            def get_action_snapshot(self, capture_id):
                return self.snapshots[capture_id]

        class Executor:
            def __init__(self, client):
                self.client = client
                self.actions = []

            def execute(self, action):
                self.actions.append(action)
                return BrowserExecutionResult(True, "", backend_node_id=303)

        client = Client()
        executor = Executor(client)
        runner = ScenarioRunner(
            page_perception=Perception(),
            browser_executor=executor,
            grounders=TargetGrounderRegistry(),
            verifiers=OutcomeVerifierRegistry(
                [], ui_graph_verifier=UIGraphOutcomeVerifier()
            ),
            url_policy=public_url_policy(),
            clock=lambda: 10.0,
        )
        scratch = Path("runtime_data") / "test-type-runner"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp_dir:
            result = runner.run(
                type_plan(),
                run_id="run-type",
                out_dir=Path(temp_dir) / "evidence",
                confirmed=True,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(executor.actions[0].op, "type")
        self.assertEqual(executor.actions[0].text, "找一个前端开源项目")

    def test_verification_keeps_observing_until_success(self):
        attempts = []
        waits = []

        class Verifiers:
            def verify_expected(self, **_kwargs):
                attempts.append(len(attempts) + 1)
                return len(attempts) == 3, "unit-verifier"

        runner = ScenarioRunner(
            page_perception=object(),
            browser_executor=type("Executor", (), {"client": object()})(),
            grounders=TargetGrounderRegistry(),
            verifiers=Verifiers(),
            url_policy=public_url_policy(),
            wait=waits.append,
        )
        capture_number = 0

        def capture(_label, **_kwargs):
            nonlocal capture_number
            capture_number += 1
            value = graph(f"verify-{capture_number}")
            return {"capture_id": f"verify-{capture_number}"}, value, {}

        result, pending = runner._verify(
            semantic_plan()["steps"][1],
            pending={"before": {}, "before_graph": graph("before")},
            capture=capture,
            cancelled=lambda: False,
        )

        self.assertTrue(result["outcome_verified"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(len(waits), 2)
        self.assertIsNone(pending)

    def test_wait_stops_only_when_cancelled(self):
        cancelled = False

        class Verifiers:
            @staticmethod
            def verify_expected(**_kwargs):
                return False, "unit-verifier"

        def mark_cancelled(_seconds):
            nonlocal cancelled
            cancelled = True

        runner = ScenarioRunner(
            page_perception=object(),
            browser_executor=type("Executor", (), {"client": object()})(),
            grounders=TargetGrounderRegistry(),
            verifiers=Verifiers(),
            url_policy=public_url_policy(),
            wait=mark_cancelled,
        )
        step = semantic_plan()["steps"][1]
        step["op"] = "wait"

        with self.assertRaisesRegex(ScenarioExecutionError, "execution_cancelled"):
            runner._wait_for(
                step,
                pending={"before": {}, "before_graph": graph("before")},
                capture=lambda _label, **_kwargs: ({}, graph("after"), {}),
                cancelled=lambda: cancelled,
            )


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
            "browser_key_invalid": EXECUTION_FAILED,
            "browser_scroll_invalid": EXECUTION_FAILED,
            "browser_select_option_missing": EXECUTION_FAILED,
            "execution_cancelled": CANCELLED,
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
                    "type": "element_visible",
                    "target": {"query": "AP_001 详情"},
                },
                before=before,
                after=graph("c-still-visible"),
            )
        )

        input_before = graph("input-before")
        input_after = graph("input-after")
        for value, typed in ((input_before, ""), (input_after, "前端开源项目")):
            value["nodes"][0]["name"] = "搜索框"
            value["nodes"][0]["role"] = "textbox"
            value["nodes"][0]["attributes"]["value"] = typed
            duplicate = copy.deepcopy(value["nodes"][0])
            duplicate["id"] = duplicate["id"].replace("cdp:", "dom:")
            duplicate["source"] = {"kind": "dom"}
            duplicate["attributes"].pop("value", None)
            value["nodes"].append(duplicate)
        self.assertTrue(
            verifier.verify(
                expected={
                    "type": "input_value",
                    "target": {"query": "搜索框", "role": "textbox"},
                    "value": "前端开源项目",
                },
                before=input_before,
                after=input_after,
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

    def test_custom_menu_label_grounds_and_verifies_without_aria_role(self):
        before = custom_menu_graph("menu-before")
        opened = custom_menu_graph("menu-opened", menu_open=True)
        selected = custom_menu_graph("menu-selected", selected=True)
        target = {"query": "近7天", "role": "menuitem"}

        grounded = TargetGrounderRegistry().resolve(target, opened)
        self.assertIsInstance(grounded, BrowserTarget)
        self.assertEqual(grounded.backend_node_id, 20)

        verifier = UIGraphOutcomeVerifier()
        self.assertTrue(
            verifier.verify(
                expected={"type": "element_visible", "target": target},
                before=before,
                after=opened,
            )
        )
        self.assertTrue(
            verifier.verify(
                expected={"type": "element_selected", "target": target},
                before=opened,
                after=selected,
            )
        )


if __name__ == "__main__":
    unittest.main()
