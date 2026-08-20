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
from kt6_backend.execution.grounding import DOMGrounder, GroundingError
from kt6_backend.execution.models import (
    BrowserAction,
    BrowserExecutionResult,
    BrowserTarget,
    VisualTarget,
)
from kt6_backend.execution.target_resolver import (
    TargetResolutionError,
    UIGraphTargetResolver,
)
from kt6_backend.execution.url_policy import ExecutionURLPolicy
from kt6_backend.page_perception import PagePerceptionService, SQLitePageCaptureStore
from kt6_backend.perception_runtime import PerceptionRuntime
from kt6_backend.safe_dom_actions import SafeDOMActionService
from tests.test_dom_action_binding import ASSETS, device_snapshot
from tests.test_page_perception_cdp import cdp_envelope


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
                    "frame_url": "https://nce.example/devices",
                    "parent_frame_id": "",
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


def browser_target() -> BrowserTarget:
    return BrowserTarget(
        node_id="cdp:shutdown",
        backend_node_id=387,
        frame_id="frame-main",
        frame_url="https://nce.example/devices",
        page_url="https://nce.example/devices",
        click_backend_node_id=387,
        dom_id="shutdown-ap-1",
        accessible_name="关闭",
        role="button",
        expected_attributes=(),
        owner_business_id="ap_001",
        action_id="ap.shutdown",
    )


def link_target(
    *,
    click_backend_node_id: int = 351,
    target: str = "",
) -> BrowserTarget:
    href = "https://top.baidu.com/board?platform=pc&sa=pcindex_entry"
    attributes = (("href", href),)
    if target:
        attributes += (("target", target),)
    return BrowserTarget(
        node_id="cdp:hot-search",
        backend_node_id=351,
        frame_id="frame-main",
        frame_url="https://www.baidu.com/",
        page_url="https://www.baidu.com/",
        click_backend_node_id=click_backend_node_id,
        dom_id="",
        accessible_name="百度热搜",
        role="link",
        expected_attributes=attributes,
        owner_business_id="",
        action_id="",
    )


def execution_url_policy() -> ExecutionURLPolicy:
    return ExecutionURLPolicy(
        allow_private_networks=True,
        resolver=lambda _host: ("93.184.216.34",),
    )


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


class SingleTargetHarness:
    def __init__(self, page_url: str, target_id: str = "target-1"):
        self.page_url = page_url
        self.target_id = target_id
        self.active_target_id = target_id
        self.switch_calls: list[str] = []

    def switch_tab(self, target_id: str) -> None:
        self.switch_calls.append(target_id)
        self.active_target_id = target_id

    def current_tab(self) -> dict[str, str]:
        return {
            "targetId": self.active_target_id,
            "type": "page",
            "url": self.page_url,
        }


class TabHarness:
    def __init__(self, targets: list[dict], active_target_id: str | None = None):
        self.targets = {item["targetId"]: dict(item) for item in targets}
        self.active_target_id = active_target_id or (
            targets[0]["targetId"] if targets else ""
        )
        self.switch_calls: list[str] = []
        self.new_tab_calls: list[str] = []

    def current_tab(self) -> dict | None:
        return self.targets.get(self.active_target_id)

    def switch_tab(self, target_id: str) -> None:
        self.switch_calls.append(target_id)
        if target_id not in self.targets:
            raise RuntimeError(f"unknown target {target_id}")
        self.active_target_id = target_id

    def new_tab(self, url: str) -> dict:
        target_id = f"target-{len(self.targets) + 1}"
        self.targets[target_id] = {
            "targetId": target_id,
            "type": "page",
            "url": url,
            "title": "",
        }
        self.active_target_id = target_id
        self.new_tab_calls.append(url)
        return self.targets[target_id]

    def targets_payload(self) -> dict:
        return {"targetInfos": list(self.targets.values())}


class BrowserHarnessClientTest(unittest.TestCase):
    def test_runner_binds_one_exact_browser_target(self):
        page_url = "http://127.0.0.1:8787/execution-test.html"
        harness = SingleTargetHarness(page_url)
        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=lambda method, **_kwargs: {
                "targetInfos": [
                    {"targetId": "target-1", "type": "page", "url": page_url}
                ]
            }
            if method == "Target.getTargets"
            else {},
            click_call=lambda _x, _y: None,
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
        )

        session = client.bind_page_target(page_url)

        self.assertEqual(session["target_id"], "target-1")
        self.assertEqual(session["page_url"], page_url)
        self.assertEqual(harness.switch_calls, ["target-1"])

    def test_canvas_click_recomputes_the_pixel_point_from_the_live_box(self):
        page_url = "http://127.0.0.1:8787/execution-test.html"
        clicks = []
        harness = SingleTargetHarness(page_url)

        def cdp(method, **_params):
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"targetId": "target-1", "type": "page", "url": page_url}
                    ]
                }
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {"id": "frame-main", "url": page_url}
                    }
                }
            if method == "DOM.describeNode":
                return {
                    "node": {
                        "backendNodeId": 900,
                        "attributes": ["id", "topology-canvas"],
                    }
                }
            if method == "DOM.getBoxModel":
                return {"model": {"content": [10, 20, 530, 20, 530, 440, 10, 440]}}
            if method == "Page.getLayoutMetrics":
                return {"cssVisualViewport": {"clientWidth": 1280, "clientHeight": 720}}
            if method == "DOM.getNodeForLocation":
                return {"backendNodeId": 900}
            self.fail(f"unexpected CDP method: {method}")

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=cdp,
            click_call=lambda x, y: clicks.append((x, y)),
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
        )
        target = VisualTarget(
            node_id="vision:ap1",
            canvas_backend_node_id=900,
            frame_id="frame-main",
            frame_url=page_url,
            page_url=page_url,
            canvas_dom_id="topology-canvas",
            asset_id="ap_001",
            x_ratio=0.3,
            y_ratio=0.4,
            producer_id="local-cv-ocr",
        )

        client.bind_page_target(page_url)
        receipt = client.click_visual_target(target)

        self.assertEqual(receipt, {"backend_node_id": 900, "x": 166.0, "y": 188.0})
        self.assertEqual(clicks, [(166.0, 188.0)])

    def test_click_uses_a_live_verified_visible_child_for_a_boxless_link(self):
        page_url = "https://www.baidu.com/"
        clicks = []
        harness = SingleTargetHarness(page_url)

        def cdp(method, **params):
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"targetId": "target-1", "type": "page", "url": page_url}
                    ]
                }
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {"id": "frame-main", "url": page_url}
                    }
                }
            if method == "DOM.describeNode":
                return {
                    "node": {
                        "backendNodeId": 351,
                        "attributes": [
                            "href",
                            "https://top.baidu.com/board?platform=pc&sa=pcindex_entry",
                        ],
                        "children": [
                            {
                                "backendNodeId": 352,
                                "children": [{"backendNodeId": 353}],
                            }
                        ],
                    }
                }
            if method == "DOM.getBoxModel":
                self.assertEqual(params["backendNodeId"], 352)
                return {
                    "model": {
                        "content": [245, 549, 314, 549, 314, 573, 245, 573]
                    }
                }
            if method == "Page.getLayoutMetrics":
                return {
                    "cssVisualViewport": {
                        "clientWidth": 1280,
                        "clientHeight": 720,
                    }
                }
            if method == "DOM.getNodeForLocation":
                return {"backendNodeId": 353}
            self.fail(f"unexpected CDP method: {method}")

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=ExecutionURLPolicy(
                resolver=lambda _host: ("93.184.216.34",)
            ),
            cdp_call=cdp,
            click_call=lambda x, y: clicks.append((x, y)),
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
        )

        client.bind_page_target(page_url)
        receipt = client.click_backend_node(
            link_target(click_backend_node_id=352)
        )

        self.assertEqual(
            receipt,
            {"backend_node_id": 351, "x": 279.5, "y": 561.0},
        )
        self.assertEqual(clicks, [(279.5, 561.0)])

    def test_click_binds_a_new_allowed_popup_target_for_verification(self):
        page_url = "https://www.baidu.com/"
        popup_url = "https://top.baidu.com/board?platform=pc&sa=pcindex_entry"
        harness = TabHarness(
            [{"targetId": "target-1", "type": "page", "url": page_url}]
        )

        def cdp(method, **_params):
            if method == "Target.getTargets":
                return harness.targets_payload()
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {"id": "frame-main", "url": page_url}
                    }
                }
            if method == "DOM.describeNode":
                return {
                    "node": {
                        "backendNodeId": 351,
                        "attributes": [
                            "href",
                            popup_url,
                            "target",
                            "_blank",
                        ],
                    }
                }
            if method == "DOM.getBoxModel":
                return {
                    "model": {
                        "content": [245, 549, 314, 549, 314, 573, 245, 573]
                    }
                }
            if method == "Page.getLayoutMetrics":
                return {
                    "cssVisualViewport": {
                        "clientWidth": 1280,
                        "clientHeight": 720,
                    }
                }
            if method == "DOM.getNodeForLocation":
                return {"backendNodeId": 351}
            self.fail(f"unexpected CDP method: {method}")

        def click(_x, _y):
            harness.targets["target-popup"] = {
                "targetId": "target-popup",
                "type": "page",
                "url": popup_url,
                "openerId": "target-1",
            }

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=ExecutionURLPolicy(
                resolver=lambda _host: ("93.184.216.34",)
            ),
            cdp_call=cdp,
            click_call=click,
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
        )

        client.bind_page_target(page_url)
        client.click_backend_node(link_target(target="_blank"))

        self.assertEqual(harness.active_target_id, "target-popup")
        self.assertEqual(harness.switch_calls, ["target-1", "target-popup"])

    def test_click_rejects_private_link_destination_before_dispatch(self):
        page_url = "https://www.baidu.com/"
        clicks = []
        harness = SingleTargetHarness(page_url)

        def cdp(method, **_params):
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"targetId": "target-1", "type": "page", "url": page_url}
                    ]
                }
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {"id": "frame-main", "url": page_url}
                    }
                }
            self.fail(f"unexpected CDP method: {method}")

        target = BrowserTarget(
            node_id="cdp:private-link",
            backend_node_id=351,
            frame_id="frame-main",
            frame_url=page_url,
            page_url=page_url,
            click_backend_node_id=351,
            dom_id="",
            accessible_name="Private",
            role="link",
            expected_attributes=(("href", "http://127.0.0.1/admin"),),
            owner_business_id="",
            action_id="",
        )
        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=ExecutionURLPolicy(
                resolver=lambda _host: ("93.184.216.34",)
            ),
            cdp_call=cdp,
            click_call=lambda x, y: clicks.append((x, y)),
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
        )

        client.bind_page_target(page_url)
        with self.assertRaisesRegex(
            BrowserHarnessError,
            "execution_url_network_blocked",
        ):
            client.click_backend_node(target)
        self.assertEqual(clicks, [])

    def test_capture_builds_dom_and_cdp_payload_from_the_live_snapshot(self):
        page_url = "https://example.test/topology?capture=1"
        envelope = cdp_envelope(page_url)
        harness = SingleTargetHarness(page_url)

        def cdp(method, **_kwargs):
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"targetId": "target-1", "type": "page", "url": page_url}
                    ]
                }
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {"id": "main-frame", "url": page_url}
                    }
                }
            if method == "DOMSnapshot.captureSnapshot":
                return envelope["dom_snapshot"]
            if method == "Accessibility.getFullAXTree":
                return envelope["ax_tree"]
            if method == "Browser.getVersion":
                return {"product": "Chrome/Test", "protocolVersion": "1.3"}
            if method == "Page.getLayoutMetrics":
                return {
                    "cssVisualViewport": {
                        "clientWidth": 1280,
                        "clientHeight": 720,
                    }
                }
            if method == "Page.captureScreenshot":
                return {"data": "/9j/2Q=="}
            self.fail(f"unexpected CDP method: {method}")

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=cdp,
            click_call=lambda _x, _y: None,
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
        )
        client.bind_page_target(page_url)
        payload = client.capture_page_payload()

        self.assertEqual(payload["page"]["url"], page_url)
        self.assertEqual(
            payload["cdp_snapshot"]["schema_version"],
            "kt6.cdp-page-snapshot.v1",
        )
        button = next(
            item
            for item in payload["dom"]["elements"]
            if item["tag"] == "button"
        )
        self.assertEqual(button["frame_id"], "main-frame")
        self.assertEqual(button["frame_url"], page_url)
        self.assertTrue(button["actionable"])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            perception = PagePerceptionService(
                SQLitePageCaptureStore(root / "captures.sqlite3", root / "assets"),
                PerceptionRuntime(),
            )
            capture = perception.ingest(payload)
            graph = perception.get_ui_graph(capture["capture_id"])
        graph_button = next(
            node
            for node in graph["nodes"]
            if node.get("source", {}).get("backend_node_id") == 3
        )
        self.assertEqual(graph_button["source"]["frame_url"], page_url)
        self.assertEqual(graph_button["source"]["parent_frame_id"], "")

    def test_click_uses_box_center_and_fixed_helper(self):
        calls: list[tuple] = []
        harness = SingleTargetHarness("https://nce.example/devices")

        def cdp(method, **params):
            calls.append(("cdp", method, params))
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {
                            "targetId": "target-1",
                            "type": "page",
                            "url": "https://nce.example/devices",
                        }
                    ]
                }
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {
                            "id": "frame-main",
                            "url": "https://nce.example/devices",
                        }
                    }
                }
            if method == "DOM.describeNode":
                return {
                    "node": {
                        "backendNodeId": 387,
                        "attributes": [
                            "id",
                            "shutdown-ap-1",
                            "data-owner-business-id",
                            "ap_001",
                            "data-action-id",
                            "ap.shutdown",
                        ],
                    }
                }
            if method == "Page.getLayoutMetrics":
                return {
                    "cssVisualViewport": {
                        "clientWidth": 1280,
                        "clientHeight": 720,
                    }
                }
            if method == "DOM.getNodeForLocation":
                return {"backendNodeId": 387}
            return {
                "model": {
                    "content": [10, 20, 30, 20, 30, 40, 10, 40]
                }
            }

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=cdp,
            click_call=lambda x, y: calls.append(("click", x, y)),
            ensure_daemon=lambda: calls.append(("daemon",)),
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
        )

        client.bind_page_target("https://nce.example/devices")
        first = client.click_backend_node(browser_target())
        second = client.click_backend_node(browser_target())

        self.assertEqual(first, {"backend_node_id": 387, "x": 20.0, "y": 30.0})
        self.assertEqual(second, first)
        self.assertEqual(calls.count(("daemon",)), 1)
        self.assertEqual(calls.count(("click", 20.0, 30.0)), 2)
        box_calls = [
            call
            for call in calls
            if call[0] == "cdp" and call[1] == "DOM.getBoxModel"
        ]
        self.assertEqual(len(box_calls), 2)
        self.assertEqual(box_calls[0], ("cdp", "DOM.getBoxModel", {"backendNodeId": 387}))

    def test_invalid_target_or_remote_cdp_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            BrowserHarnessClient(
                cdp_url="https://browser.example/devtools",
                workspace=Path("runtime_data/browser-harness-test"),
                url_policy=execution_url_policy(),
            )

        harness = SingleTargetHarness("https://nce.example/devices")

        def invalid_box_cdp(method, **_kwargs):
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {
                            "targetId": "target-1",
                            "type": "page",
                            "url": "https://nce.example/devices",
                        }
                    ]
                }
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {
                            "id": "frame-main",
                            "url": "https://nce.example/devices",
                        }
                    }
                }
            if method == "DOM.describeNode":
                return {
                    "node": {
                        "backendNodeId": 1,
                        "attributes": [
                            "id",
                            "shutdown-ap-1",
                            "data-owner-business-id",
                            "ap_001",
                            "data-action-id",
                            "ap.shutdown",
                        ],
                    }
                }
            return {"model": {"content": [0, 0, 0, 0, 0, 0, 0, 0]}}

        client = BrowserHarnessClient(
            cdp_url="http://localhost:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=invalid_box_cdp,
            click_call=lambda _x, _y: None,
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
        )
        client.bind_page_target("https://nce.example/devices")
        with self.assertRaises(BrowserHarnessError) as raised:
            invalid_target = browser_target()
            invalid_target = BrowserTarget(
                **{
                    **invalid_target.__dict__,
                    "backend_node_id": 1,
                    "click_backend_node_id": 1,
                }
            )
            client.click_backend_node(invalid_target)
        self.assertEqual(raised.exception.error_code, "browser_target_not_visible")

    def test_click_rejects_a_different_active_page_before_box_lookup(self):
        page_url = "https://nce.example/devices"
        calls: list[str] = []
        harness = SingleTargetHarness(page_url)

        def cdp(method, **_kwargs):
            calls.append(method)
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"targetId": "target-1", "type": "page", "url": page_url}
                    ]
                }
            return {
                "frameTree": {
                    "frame": {
                        "id": "frame-main",
                        "url": "https://nce.example/other",
                    }
                }
            }

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=cdp,
            click_call=lambda _x, _y: self.fail("click must not run"),
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
        )

        client.bind_page_target(page_url)
        with self.assertRaises(BrowserHarnessError) as raised:
            client.click_backend_node(browser_target())

        self.assertEqual(raised.exception.error_code, "browser_page_changed")
        self.assertEqual(
            calls,
            ["Target.getTargets", "Target.getTargets", "Page.getFrameTree"],
        )

    def test_click_revalidates_live_attributes_and_hit_target(self):
        def run(*, live_action="ap.shutdown", hit_backend_id=387):
            calls: list[str] = []
            harness = SingleTargetHarness("https://nce.example/devices")

            def cdp(method, **_kwargs):
                calls.append(method)
                if method == "Target.getTargets":
                    return {
                        "targetInfos": [
                            {
                                "targetId": "target-1",
                                "type": "page",
                                "url": "https://nce.example/devices",
                            }
                        ]
                    }
                if method == "Page.getFrameTree":
                    return {
                        "frameTree": {
                            "frame": {
                                "id": "frame-main",
                                "url": "https://nce.example/devices",
                            }
                        }
                    }
                if method == "DOM.describeNode":
                    return {
                        "node": {
                            "backendNodeId": 387,
                            "attributes": [
                                "id",
                                "shutdown-ap-1",
                                "data-owner-business-id",
                                "ap_001",
                                "data-action-id",
                                live_action,
                            ],
                        }
                    }
                if method == "DOM.getBoxModel":
                    return {
                        "model": {
                            "content": [10, 20, 30, 20, 30, 40, 10, 40]
                        }
                    }
                if method == "Page.getLayoutMetrics":
                    return {
                        "cssVisualViewport": {
                            "clientWidth": 1280,
                            "clientHeight": 720,
                        }
                    }
                return {"backendNodeId": hit_backend_id}

            client = BrowserHarnessClient(
                cdp_url="http://127.0.0.1:9222",
                workspace=Path("runtime_data/browser-harness-test"),
                url_policy=execution_url_policy(),
                cdp_call=cdp,
                click_call=lambda _x, _y: self.fail("click must not run"),
                switch_tab_call=harness.switch_tab,
                current_tab_call=harness.current_tab,
            )
            client.bind_page_target("https://nce.example/devices")
            with self.assertRaises(BrowserHarnessError) as raised:
                client.click_backend_node(browser_target())
            return raised.exception.error_code, calls

        changed_error, changed_calls = run(live_action="device.details")
        self.assertEqual(changed_error, "browser_target_changed")
        self.assertNotIn("DOM.getBoxModel", changed_calls)

        occluded_error, occluded_calls = run(hit_backend_id=999)
        self.assertEqual(occluded_error, "browser_target_occluded")
        self.assertIn("DOM.getNodeForLocation", occluded_calls)

    def test_click_rejects_live_frame_change(self):
        page_url = "https://nce.example/devices"
        harness = SingleTargetHarness(page_url)

        def cdp(method, **_kwargs):
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"targetId": "target-1", "type": "page", "url": page_url}
                    ]
                }
            return {
                "frameTree": {
                    "frame": {
                        "id": "another-frame",
                        "url": page_url,
                    }
                }
            }

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=cdp,
            click_call=lambda _x, _y: self.fail("click must not run"),
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
        )

        client.bind_page_target(page_url)
        with self.assertRaises(BrowserHarnessError) as raised:
            client.click_backend_node(browser_target())

        self.assertEqual(
            raised.exception.error_code, "browser_target_frame_changed"
        )

    def test_open_or_bind_switches_to_the_existing_target_tab(self):
        runner_url = "http://127.0.0.1:8765/execution-runner.html"
        target_url = "http://127.0.0.1:8787/target.html"
        harness = TabHarness(
            [
                {"targetId": "target-runner", "type": "page", "url": runner_url},
                {"targetId": "target-b", "type": "page", "url": target_url},
            ],
            active_target_id="target-runner",
        )

        def cdp(method, **_params):
            if method == "Target.getTargets":
                return harness.targets_payload()
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {"id": "main-frame", "url": target_url}
                    }
                }
            self.fail(f"unexpected CDP method: {method}")

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=cdp,
            click_call=lambda _x, _y: None,
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
            new_tab_call=harness.new_tab,
        )

        session = client.open_or_bind_target(target_url)

        self.assertEqual(session["target_id"], "target-b")
        self.assertEqual(session["page_url"], target_url)
        self.assertEqual(harness.switch_calls, ["target-b"])
        self.assertEqual(harness.active_target_id, "target-b")
        self.assertEqual(harness.targets["target-runner"]["url"], runner_url)

    def test_open_target_creates_a_new_tab_and_leaves_runner_tab_unchanged(self):
        runner_url = "http://127.0.0.1:8765/execution-runner.html"
        target_url = "http://127.0.0.1:8787/target.html"
        harness = TabHarness(
            [{"targetId": "target-runner", "type": "page", "url": runner_url}],
            active_target_id="target-runner",
        )

        def cdp(method, **_params):
            if method == "Target.getTargets":
                return harness.targets_payload()
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {
                            "id": "main-frame",
                            "url": harness.targets[harness.active_target_id]["url"],
                        }
                    }
                }
            self.fail(f"unexpected CDP method: {method}")

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=cdp,
            click_call=lambda _x, _y: None,
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
            new_tab_call=harness.new_tab,
        )

        session = client.open_or_bind_target(target_url)

        self.assertEqual(session["target_id"], "target-2")
        self.assertEqual(session["page_url"], target_url)
        self.assertEqual(harness.new_tab_calls, [target_url])
        self.assertEqual(harness.active_target_id, "target-2")
        self.assertEqual(harness.targets["target-runner"]["url"], runner_url)

    def test_capture_routes_every_cdp_method_to_the_bound_target_session(self):
        runner_url = "http://127.0.0.1:8765/execution-runner.html"
        target_url = "https://example.test/topology?capture=1"
        harness = TabHarness(
            [
                {"targetId": "target-runner", "type": "page", "url": runner_url},
                {"targetId": "target-b", "type": "page", "url": target_url},
            ],
            active_target_id="target-runner",
        )
        envelope = cdp_envelope(target_url)
        routed: list[tuple[str, str]] = []

        def cdp(method, **_params):
            routed.append((harness.active_target_id, method))
            if method == "Target.getTargets":
                return harness.targets_payload()
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {"id": "main-frame", "url": target_url}
                    }
                }
            if method == "DOMSnapshot.captureSnapshot":
                return envelope["dom_snapshot"]
            if method == "Accessibility.getFullAXTree":
                return envelope["ax_tree"]
            if method == "Browser.getVersion":
                return {"product": "Chrome/Test", "protocolVersion": "1.3"}
            if method == "Page.getLayoutMetrics":
                return {
                    "cssVisualViewport": {
                        "clientWidth": 1280,
                        "clientHeight": 720,
                    }
                }
            if method == "Page.captureScreenshot":
                return {"data": "/9j/2Q=="}
            self.fail(f"unexpected CDP method: {method}")

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=cdp,
            click_call=lambda _x, _y: None,
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
            new_tab_call=harness.new_tab,
        )

        client.open_or_bind_target(target_url)
        routed.clear()
        client.capture_page_payload()

        methods = [method for _, method in routed]
        for expected in (
            "Page.getFrameTree",
            "DOMSnapshot.captureSnapshot",
            "Accessibility.getFullAXTree",
            "Page.captureScreenshot",
        ):
            self.assertIn(expected, methods)
        self.assertTrue(routed)
        self.assertTrue(all(tid == "target-b" for tid, _ in routed))

    def test_click_routes_dom_and_click_to_the_bound_target_session(self):
        runner_url = "http://127.0.0.1:8765/execution-runner.html"
        target_url = "https://nce.example/devices"
        harness = TabHarness(
            [
                {"targetId": "target-runner", "type": "page", "url": runner_url},
                {"targetId": "target-b", "type": "page", "url": target_url},
            ],
            active_target_id="target-runner",
        )
        routed: list[tuple[str, str]] = []

        def cdp(method, **_params):
            routed.append((harness.active_target_id, method))
            if method == "Target.getTargets":
                return harness.targets_payload()
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {"id": "frame-main", "url": target_url}
                    }
                }
            if method == "DOM.describeNode":
                return {
                    "node": {
                        "backendNodeId": 387,
                        "attributes": [
                            "id",
                            "shutdown-ap-1",
                            "data-owner-business-id",
                            "ap_001",
                            "data-action-id",
                            "ap.shutdown",
                        ],
                    }
                }
            if method == "DOM.getBoxModel":
                return {
                    "model": {
                        "content": [10, 20, 30, 20, 30, 40, 10, 40]
                    }
                }
            if method == "Page.getLayoutMetrics":
                return {
                    "cssVisualViewport": {
                        "clientWidth": 1280,
                        "clientHeight": 720,
                    }
                }
            if method == "DOM.getNodeForLocation":
                return {"backendNodeId": 387}
            self.fail(f"unexpected CDP method: {method}")

        def click(_x, _y):
            routed.append((harness.active_target_id, "click"))

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=cdp,
            click_call=click,
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
            new_tab_call=harness.new_tab,
        )

        client.open_or_bind_target(target_url)
        routed.clear()
        receipt = client.click_backend_node(browser_target())

        self.assertEqual(receipt, {"backend_node_id": 387, "x": 20.0, "y": 30.0})
        self.assertTrue(routed)
        self.assertTrue(all(tid == "target-b" for tid, _ in routed))
        methods = [method for _, method in routed]
        for expected in ("DOM.describeNode", "DOM.getBoxModel", "DOM.getNodeForLocation"):
            self.assertIn(expected, methods)
        self.assertIn("click", methods)

    def test_bound_target_disappearing_or_session_change_fails_closed(self):
        target_url = "https://nce.example/devices"
        harness = TabHarness(
            [{"targetId": "target-b", "type": "page", "url": target_url}],
            active_target_id="target-b",
        )

        def cdp(method, **_params):
            if method == "Target.getTargets":
                return harness.targets_payload()
            if method == "Page.getFrameTree":
                return {
                    "frameTree": {
                        "frame": {"id": "frame-main", "url": target_url}
                    }
                }
            self.fail(f"unexpected CDP method: {method}")

        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=cdp,
            click_call=lambda _x, _y: None,
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
            new_tab_call=harness.new_tab,
        )
        client.open_or_bind_target(target_url)

        harness.targets["target-runner"] = {
            "targetId": "target-runner",
            "type": "page",
            "url": "http://127.0.0.1:8765/execution-runner.html",
        }
        harness.active_target_id = "target-runner"
        with self.assertRaises(BrowserHarnessError) as raised:
            client.capture_page_payload()
        self.assertEqual(raised.exception.error_code, "browser_session_target_changed")

        harness.active_target_id = "target-b"
        del harness.targets["target-b"]
        with self.assertRaises(BrowserHarnessError) as raised:
            client.capture_page_payload()
        self.assertEqual(raised.exception.error_code, "browser_session_target_changed")

    def test_two_page_targets_with_same_url_are_ambiguous(self):
        target_url = "https://nce.example/devices"
        harness = TabHarness(
            [
                {"targetId": "target-a", "type": "page", "url": target_url},
                {"targetId": "target-b", "type": "page", "url": target_url},
            ],
        )
        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=lambda method, **_kwargs: harness.targets_payload()
            if method == "Target.getTargets"
            else {},
            click_call=lambda _x, _y: None,
            switch_tab_call=harness.switch_tab,
            current_tab_call=harness.current_tab,
            new_tab_call=harness.new_tab,
        )

        with self.assertRaises(BrowserHarnessError) as raised:
            client.open_or_bind_target(target_url)

        self.assertEqual(raised.exception.error_code, "browser_target_ambiguous")


class BrowserExecutionBoundaryTest(unittest.TestCase):
    def test_dom_grounder_selects_the_current_real_graph_target(self):
        graph = cdp_graph()
        decision = DOMGrounder().resolve({"query": "关闭"}, graph)

        self.assertEqual(decision.node_id, "cdp:shutdown")
        self.assertEqual(decision.backend_node_id, 387)
        self.assertEqual(decision.dom_id, "shutdown-ap-1")
        self.assertEqual(decision.page_url, "https://nce.example/devices")
        self.assertEqual(decision.owner_business_id, "ap_001")

        graph["nodes"].append(copy.deepcopy(graph["nodes"][0]))
        graph["nodes"][1]["id"] = "cdp:duplicate"
        with self.assertRaisesRegex(GroundingError, "ambiguous"):
            DOMGrounder().resolve({"query": "关闭"}, graph)

    def test_dom_grounder_accepts_a_boxless_link_with_one_visible_dom_child(self):
        graph = cdp_graph()
        link = graph["nodes"][0]
        link.update(
            {
                "id": "cdp:hot-search",
                "role": "link",
                "name": "百度热搜",
                "bbox": [314.0, 549.0, 0.0, 0.0],
                "owner_business_id": "",
                "action_id": "",
            }
        )
        link["source"]["backend_node_id"] = 351
        link["attributes"] = {
            "href": "https://top.baidu.com/board?platform=pc&sa=pcindex_entry",
            "target": "_blank",
        }
        graph["nodes"].append(
            {
                "id": "cdp:hot-search-label",
                "kind": "element",
                "role": "generic",
                "name": "百度热搜",
                "bbox": [245.0, 549.0, 69.0, 24.0],
                "source": {
                    "kind": "cdp",
                    "frame_id": "frame-main",
                    "frame_url": "https://nce.example/devices",
                    "backend_node_id": 352,
                },
                "attributes": {},
                "disabled": False,
                "actionable": False,
                "can_click_now": False,
                "safe_for_execution": False,
                "interaction": {
                    "status": "analysis_only",
                    "candidate": False,
                    "authorized": False,
                },
            }
        )
        graph["edges"].append(
            {
                "source": "cdp:hot-search",
                "target": "cdp:hot-search-label",
                "type": "parent_of",
                "relation_type": "dom_child",
            }
        )

        decision = DOMGrounder().resolve(
            {"query": "百度热搜", "role": "link"},
            graph,
        )

        self.assertEqual(decision.backend_node_id, 351)
        self.assertEqual(decision.click_backend_node_id, 352)
        self.assertEqual(decision.dom_id, "")
        self.assertIn(
            (
                "href",
                "https://top.baidu.com/board?platform=pc&sa=pcindex_entry",
            ),
            decision.expected_attributes,
        )

    def test_executor_only_accepts_click(self):
        client = BrowserHarnessClient(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser-harness-test"),
            url_policy=execution_url_policy(),
            cdp_call=lambda *_args, **_kwargs: {},
            click_call=lambda _x, _y: None,
        )
        executor = HarnessBrowserExecutor(client)
        target = browser_target()

        result = executor.execute(BrowserAction("fill", target))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "unsupported_browser_action")

    def test_resolver_requires_exact_cdp_dom_binding(self):
        resolver = UIGraphTargetResolver()
        control = {
            "selector": "#shutdown-ap-1",
            "action_id": "ap.shutdown",
            "frame_id": "0",
            "frame_url": "https://nce.example/devices",
        }

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

        wrong_frame = dict(control, frame_id="7")
        with self.assertRaisesRegex(TargetResolutionError, "frame_mismatch"):
            resolver.resolve(
                cdp_graph(),
                expected_graph_id="uig:current",
                capture_id="capture-current",
                target_node_id="cdp:shutdown",
                control=wrong_frame,
                asset_id="ap_001",
            )

    def test_live_action_without_outcome_verifier_is_blocked(self):
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

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "outcome_verifier_unavailable")
        self.assertEqual(executor.actions, [])
        plan = service.get_plan(prepared["plan_id"])["operation_plan"]
        steps = {item["step_id"]: item for item in plan["steps"]}
        self.assertEqual(steps["execute"]["status"], "blocked")

    def test_unverified_action_never_reaches_executor_even_with_bad_graph(self):
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

        self.assertEqual(result["reason"], "outcome_verifier_unavailable")
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
        self.assertTrue(services.execution_scenarios.health()["configured"])
        self.assertEqual(
            services.execution_scenarios.health()["url_policy"]["network_scope"],
            "public_only",
        )

        with patch.dict(
            os.environ,
            {"KT6_BROWSER_HARNESS_CDP_URL": "http://127.0.0.1:9222"},
            clear=True,
        ), tempfile.TemporaryDirectory() as temp_dir, self.assertRaisesRegex(
            ValueError, "KT6_BROWSER_EXECUTION_DRIVER"
        ):
            app.create_services(Path(temp_dir))

    def test_execution_private_network_flag_is_one_global_test_mode(self):
        with patch.dict(
            os.environ,
            {"KT6_EXECUTION_ALLOW_PRIVATE_NETWORKS": "1"},
            clear=True,
        ):
            policy = app._create_execution_url_policy_from_env()

        self.assertTrue(policy.allow_private_networks)
        self.assertEqual(policy.health()["network_scope"], "public_and_private_test")
        self.assertEqual(
            policy.validate("http://127.0.0.1:8787/"),
            "http://127.0.0.1:8787/",
        )

        with patch.dict(
            os.environ,
            {"KT6_EXECUTION_ALLOW_PRIVATE_NETWORKS": "sometimes"},
            clear=True,
        ), self.assertRaisesRegex(ValueError, "must be a boolean"):
            app._create_execution_url_policy_from_env()


if __name__ == "__main__":
    unittest.main()
