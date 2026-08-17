from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kt6_backend import app
from kt6_backend.asset_inventory import AssetResolver, InMemoryAssetInventoryAdapter
from kt6_backend.dom_action_binding import DOMActionBindingService
from kt6_backend.execution.browser_executor import HarnessBrowserExecutor
from kt6_backend.execution.browser_harness_client import (
    BrowserHarnessClient,
    BrowserHarnessError,
)
from kt6_backend.execution.models import (
    BrowserAction,
    BrowserExecutionResult,
    BrowserTarget,
)
from kt6_backend.execution.target_resolver import (
    TargetResolutionError,
    UIGraphTargetResolver,
)
from kt6_backend.safe_dom_actions import SafeDOMActionService
from tests.test_dom_action_binding import ASSETS, device_snapshot


def action_snapshot(capture_id: str, created_at: float) -> dict:
    value = device_snapshot(capture_id)
    value["created_at"] = created_at
    value["content_hash"] = f"content-{capture_id}"
    return value


def cdp_graph(capture_id: str = "capture-current") -> dict:
    return {
        "schema_version": "kt6.ui-graph.v1",
        "graph_id": "uig:current",
        "capture_id": capture_id,
        "page": {"url": "https://nce.example/devices"},
        "analysis_only": True,
        "execution_authorized": False,
        "safe_for_execution": False,
        "stats": {"truncated": False},
        "nodes": [
            {
                "id": "cdp:shutdown",
                "kind": "element",
                "role": "button",
                "name": "关闭",
                "owner_business_id": "ap_001",
                "action_id": "ap.shutdown",
                "source": {
                    "kind": "cdp",
                    "frame_id": "frame-main",
                    "backend_node_id": 387,
                },
                "attributes": {
                    "id": "shutdown-ap-1",
                    "data-owner-business-id": "ap_001",
                    "data-action-id": "ap.shutdown",
                },
                "disabled": False,
                "actionable": False,
                "can_click_now": False,
                "safe_for_execution": False,
                "interaction": {
                    "status": "candidate_only",
                    "candidate": True,
                    "authorized": False,
                },
            }
        ],
        "edges": [],
        "issues": [],
    }


class GraphCaptureProvider:
    def __init__(self, snapshots: list[dict], graph: dict):
        self.snapshots = {
            item["capture_id"]: copy.deepcopy(item) for item in snapshots
        }
        self.graph = copy.deepcopy(graph)

    def get_action_snapshot(self, capture_id: str):
        value = self.snapshots.get(capture_id)
        return copy.deepcopy(value) if value else None

    def get_ui_graph(self, capture_id: str):
        if capture_id != self.graph["capture_id"]:
            return None
        return copy.deepcopy(self.graph)


class RecordingExecutor:
    executor_id = "recording_browser"

    def __init__(self, result: BrowserExecutionResult | None = None):
        self.result = result or BrowserExecutionResult(
            True, "", backend_node_id=387, x=100.0, y=200.0
        )
        self.actions: list[BrowserAction] = []

    def execute(self, action: BrowserAction) -> BrowserExecutionResult:
        self.actions.append(action)
        return self.result


class BrowserHarnessClientTest(unittest.TestCase):
    def test_click_uses_box_center_and_fixed_helper(self):
        calls: list[tuple] = []

        def cdp(method, **params):
            calls.append(("cdp", method, params))
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {"url": "https://nce.example/devices"}
                    }
                }
            if method == "Page.getLayoutMetrics":
                return {
                    "cssVisualViewport": {
                        "clientWidth": 1280,
                        "clientHeight": 720,
                    }
                }
            return {
                "model": {
                    "content": [10, 20, 30, 20, 30, 40, 10, 40]
                }
            }

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            cdp_call=cdp,
            click_call=lambda x, y: calls.append(("click", x, y)),
            ensure_daemon=lambda: calls.append(("daemon",)),
        )

        first = client.click_backend_node(
            387, expected_page_url="https://nce.example/devices"
        )
        second = client.click_backend_node(
            387, expected_page_url="https://nce.example/devices"
        )

        self.assertEqual(first, {"backend_node_id": 387, "x": 20.0, "y": 30.0})
        self.assertEqual(second, first)
        self.assertEqual(calls.count(("daemon",)), 1)
        self.assertEqual(calls.count(("click", 20.0, 30.0)), 2)
        self.assertEqual(
            calls[2],
            ("cdp", "DOM.getBoxModel", {"backendNodeId": 387}),
        )

    def test_invalid_target_or_remote_cdp_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            BrowserHarnessClient(
                cdp_url="https://browser.example/devtools",
                workspace=Path("runtime_data/browser-harness-test"),
            )

        client = BrowserHarnessClient(
            cdp_url="http://localhost:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            cdp_call=lambda method, **_kwargs: (
                {
                    "frameTree": {
                        "frame": {"url": "https://nce.example/devices"}
                    }
                }
                if method == "Page.getFrameTree"
                else {"model": {"content": [0, 0, 0, 0, 0, 0, 0, 0]}}
            ),
            click_call=lambda _x, _y: None,
        )
        with self.assertRaises(BrowserHarnessError) as raised:
            client.click_backend_node(
                1, expected_page_url="https://nce.example/devices"
            )
        self.assertEqual(raised.exception.error_code, "browser_target_not_visible")

    def test_click_rejects_a_different_active_page_before_box_lookup(self):
        calls: list[str] = []

        def cdp(method, **_kwargs):
            calls.append(method)
            return {
                "frameTree": {
                    "frame": {"url": "https://nce.example/other"}
                }
            }

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            cdp_call=cdp,
            click_call=lambda _x, _y: self.fail("click must not run"),
        )

        with self.assertRaises(BrowserHarnessError) as raised:
            client.click_backend_node(
                387, expected_page_url="https://nce.example/devices"
            )

        self.assertEqual(raised.exception.error_code, "browser_page_changed")
        self.assertEqual(calls, ["Page.getFrameTree"])


class BrowserExecutionBoundaryTest(unittest.TestCase):
    def test_executor_only_accepts_click(self):
        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            cdp_call=lambda *_args, **_kwargs: {},
            click_call=lambda _x, _y: None,
        )
        executor = HarnessBrowserExecutor(client)
        target = BrowserTarget(
            "node-1", 1, "frame-main", "https://nce.example/devices"
        )

        result = executor.execute(BrowserAction("fill", target))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "unsupported_browser_action")

    def test_resolver_requires_exact_cdp_dom_binding(self):
        resolver = UIGraphTargetResolver()
        control = {"selector": "#shutdown-ap-1", "action_id": "ap.shutdown"}

        target = resolver.resolve(
            cdp_graph(),
            expected_graph_id="uig:current",
            capture_id="capture-current",
            target_node_id="cdp:shutdown",
            control=control,
            asset_id="ap_001",
        )

        self.assertEqual(target.backend_node_id, 387)
        self.assertEqual(target.page_url, "https://nce.example/devices")
        for field, value, error in (
            ("owner_business_id", "ap_002", "asset_mismatch"),
            ("action_id", "device.details", "action_mismatch"),
            ("safe_for_execution", True, "not_candidate"),
        ):
            with self.subTest(field=field):
                graph = cdp_graph()
                graph["nodes"][0][field] = value
                with self.assertRaisesRegex(TargetResolutionError, error):
                    resolver.resolve(
                        graph,
                        expected_graph_id="uig:current",
                        capture_id="capture-current",
                        target_node_id="cdp:shutdown",
                        control=control,
                        asset_id="ap_001",
                    )

    def test_safe_action_dispatches_only_after_token_and_fresh_graph_rebind(self):
        clock = lambda: 101.0
        captures = GraphCaptureProvider(
            [
                action_snapshot("capture-initial", 99.0),
                action_snapshot("capture-current", 101.0),
            ],
            cdp_graph(),
        )
        executor = RecordingExecutor()
        service = SafeDOMActionService(
            DOMActionBindingService(
                AssetResolver(InMemoryAssetInventoryAdapter(ASSETS))
            ),
            captures,
            clock=clock,
            executor=executor,
            target_resolver=UIGraphTargetResolver(),
        )
        prepared = service.prepare(
            asset_reference="AP1",
            action="关闭",
            page_capture_id="capture-initial",
            scope={"site_id": "site-a"},
            task_id="task-1",
            principal_id="operator-1",
        )
        ready = service.preflight(
            plan_id=prepared["plan_id"],
            current_capture_id="capture-current",
            confirmed=True,
            confirmed_asset_id="ap_001",
            confirmed_action="shutdown_ap",
            permissions=["assets.ap.shutdown"],
        )

        result = service.execute(
            execution_token=ready["execution_token"],
            dry_run=False,
            graph_id="uig:current",
            target_node_id="cdp:shutdown",
        )

        self.assertEqual(result["status"], "executed_pending_verification")
        self.assertTrue(result["executed"])
        self.assertFalse(result["outcome_verified"])
        self.assertFalse(result["safe_for_execution"])
        self.assertEqual(len(executor.actions), 1)
        self.assertEqual(executor.actions[0].target.backend_node_id, 387)
        plan = service.get_plan(prepared["plan_id"])["operation_plan"]
        steps = {item["step_id"]: item for item in plan["steps"]}
        self.assertEqual(steps["execute"]["status"], "completed")
        self.assertEqual(steps["verify_outcome"]["status"], "pending")

    def test_bad_graph_target_never_reaches_executor(self):
        captures = GraphCaptureProvider(
            [
                action_snapshot("capture-initial", 99.0),
                action_snapshot("capture-current", 101.0),
            ],
            cdp_graph(),
        )
        executor = RecordingExecutor()
        service = SafeDOMActionService(
            DOMActionBindingService(
                AssetResolver(InMemoryAssetInventoryAdapter(ASSETS))
            ),
            captures,
            clock=lambda: 101.0,
            executor=executor,
            target_resolver=UIGraphTargetResolver(),
        )
        prepared = service.prepare(
            asset_reference="AP1",
            action="关闭",
            page_capture_id="capture-initial",
            scope={"site_id": "site-a"},
        )
        ready = service.preflight(
            plan_id=prepared["plan_id"],
            current_capture_id="capture-current",
            confirmed=True,
            confirmed_asset_id="ap_001",
            confirmed_action="shutdown_ap",
            permissions=["assets.ap.shutdown"],
        )

        result = service.execute(
            execution_token=ready["execution_token"],
            dry_run=False,
            graph_id="uig:other",
            target_node_id="cdp:shutdown",
        )

        self.assertEqual(result["reason"], "ui_graph_id_mismatch")
        self.assertEqual(executor.actions, [])

    def test_app_factory_enables_only_explicit_browser_harness_driver(self):
        environment = {
            "KT6_BROWSER_EXECUTION_DRIVER": "browser_harness",
            "KT6_BROWSER_HARNESS_CDP_URL": "http://127.0.0.1:9222",
        }
        with patch.dict(os.environ, environment, clear=True), tempfile.TemporaryDirectory() as temp_dir:
            services = app.create_services(Path(temp_dir))

        self.assertFalse(services.safe_dom_actions.dry_run_only)
        self.assertEqual(
            services.safe_dom_actions.executor.executor_id,
            "browser_harness",
        )

        with patch.dict(
            os.environ,
            {"KT6_BROWSER_HARNESS_CDP_URL": "http://127.0.0.1:9222"},
            clear=True,
        ), tempfile.TemporaryDirectory() as temp_dir, self.assertRaisesRegex(
            ValueError, "KT6_BROWSER_EXECUTION_DRIVER"
        ):
            app.create_services(Path(temp_dir))


if __name__ == "__main__":
    unittest.main()
