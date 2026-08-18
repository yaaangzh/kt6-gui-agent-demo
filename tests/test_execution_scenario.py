from __future__ import annotations

import base64
import binascii
import copy
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from kt6_backend.execution.fixture_canvas_vision import (
    ExecutionFixtureCanvasVisionAdapter,
)
from kt6_backend.execution.grounding import (
    CanvasGrounder,
    GroundedDOMStep,
    GroundingError,
)
from kt6_backend.execution.models import BrowserExecutionResult, CanvasTarget
from kt6_backend.execution.live_page_capture import _capture_canvases
from kt6_backend.execution.natural_language_parser import (
    NaturalLanguageIntentParser,
    NaturalLanguageParseError,
)
from kt6_backend.execution.plan_generator import FixturePlanGenerator
from kt6_backend.execution.plan_validator import (
    ActionPlanValidationError,
    ActionPlanValidator,
)
from kt6_backend.execution.scenario_service import ExecutionScenarioService
from kt6_backend.execution.scenario_runner import ScenarioRunner
from kt6_backend.vision_recognition import CanvasFrame


FIXTURE_URL = "http://127.0.0.1:8787/execution-test.html"
REQUEST = "打开 AP_001 的详情，然后进入拓扑页面并在拓扑中选中 AP_001"


class ActionPlanContractTest(unittest.TestCase):
    def test_natural_language_expands_to_six_semantic_steps(self):
        intents = NaturalLanguageIntentParser().parse(REQUEST)
        plan = FixturePlanGenerator().generate(
            start_url=FIXTURE_URL,
            user_request=REQUEST,
            intents=intents,
        )
        validated = ActionPlanValidator().validate(plan)

        self.assertEqual(
            [item["intent"] for item in intents],
            ["open_asset_details", "open_topology", "select_canvas_asset"],
        )
        self.assertEqual(
            [step["op"] for step in validated["steps"]],
            ["click", "verify", "click", "wait", "click", "verify"],
        )
        serialized = repr(validated)
        self.assertNotIn("backend_node_id", serialized)
        self.assertNotIn("selector", serialized)
        self.assertNotIn("x_ratio", serialized)

    def test_parser_and_validator_reject_unknown_language_or_coordinates(self):
        with self.assertRaises(NaturalLanguageParseError):
            NaturalLanguageIntentParser().parse("随便操作一下")
        generated = ExecutionScenarioService(root=Path("."), runner=None).generate_plan(
            start_url=FIXTURE_URL,
            user_request=REQUEST,
        )
        tampered = copy.deepcopy(generated["plan"])
        tampered["steps"][0]["target"]["x"] = 10
        with self.assertRaises(ActionPlanValidationError):
            ActionPlanValidator().validate(tampered)

    def test_validator_rejects_mismatched_outcome_before_execution(self):
        generated = ExecutionScenarioService(root=Path("."), runner=None).generate_plan(
            start_url=FIXTURE_URL,
            user_request=REQUEST,
        )
        tampered = copy.deepcopy(generated["plan"])
        tampered["steps"][1]["expected"] = {
            "type": "canvas_asset_selected",
            "asset_id": "ap_001",
        }

        with self.assertRaisesRegex(
            ActionPlanValidationError,
            "action_plan_sequence_invalid",
        ):
            ActionPlanValidator().validate(tampered)

    def test_service_returns_readable_plan_before_execution(self):
        generated = ExecutionScenarioService(root=Path("."), runner=None).generate_plan(
            start_url=FIXTURE_URL,
            user_request=REQUEST,
        )

        self.assertEqual(len(generated["readable_steps"]), 6)
        self.assertTrue(generated["requires_confirmation"])
        self.assertFalse(generated["runner_configured"])


class FixtureCanvasPerceptionTest(unittest.TestCase):
    def test_live_capture_crops_the_visible_canvas_pixels(self):
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
            + chunk("IHDR".encode(), struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk("IDAT".encode(), zlib.compress(scanlines))
            + chunk("IEND".encode(), b"")
        )
        calls = []

        def cdp(method, **params):
            calls.append((method, params))
            return {"data": base64.b64encode(png).decode("ascii")}

        canvases = _capture_canvases(
            cdp,
            {
                "nodes": [
                    {
                        "dom_node_type": 1,
                        "frame_id": "frame-main",
                        "document_index": 0,
                        "attributes": {"id": "topology-canvas"},
                        "bounds": [10, 20, 200, 100],
                    }
                ]
            },
            page_url=FIXTURE_URL,
        )

        self.assertEqual(canvases[0]["width"], 2)
        self.assertEqual(canvases[0]["height"], 2)
        self.assertEqual(canvases[0]["bbox"], [10.0, 20.0, 200.0, 100.0])
        self.assertEqual(calls[0][0], "Page.captureScreenshot")

    def test_adapter_derives_node_boxes_from_real_pixel_values(self):
        width, height = 60, 20
        pixels = [(255, 255, 255)] * (width * height)
        for left, color in (
            (1, (32, 166, 122)),
            (21, (36, 107, 253)),
            (41, (138, 92, 245)),
        ):
            for y in range(5, 15):
                for x in range(left, left + 10):
                    pixels[y * width + x] = color
        adapter = ExecutionFixtureCanvasVisionAdapter(
            pixel_loader=lambda _path: (width, height, pixels)
        )
        frame = CanvasFrame(
            canvas_id="topology-canvas",
            screenshot_path=Path("unused.png"),
            screenshot_sha256="a" * 64,
            mime_type="image/png",
            width=width,
            height=height,
            client_width=width,
            client_height=height,
            bbox=(0, 0, width, height),
        )

        result = adapter.recognize(
            page={"url": FIXTURE_URL},
            frames=(frame,),
        )

        boxes = {item["business_id"]: item["bbox"] for item in result["objects"]}
        self.assertEqual(boxes["ap_001"], [1, 5, 10, 10])
        self.assertEqual(boxes["switch_001"], [21, 5, 10, 10])
        self.assertEqual(boxes["ap_002"], [41, 5, 10, 10])

    def test_canvas_grounder_uses_current_graph_geometry(self):
        graph = {
            "schema_version": "kt6.ui-graph.v1",
            "graph_id": "uig:canvas",
            "capture_id": "capture-canvas",
            "page": {"url": FIXTURE_URL},
            "analysis_only": True,
            "execution_authorized": False,
            "safe_for_execution": False,
            "stats": {"truncated": False},
            "nodes": [
                {
                    "id": "vision:ap1",
                    "business_id": "ap_001",
                    "bbox": [100, 120, 30, 30],
                    "confidence": 0.99,
                    "safe_for_execution": False,
                    "source": {
                        "kind": "vision",
                        "producer_id": "execution-fixture-canvas-cv",
                        "canvas_id": "topology-canvas",
                        "canvas_width": 520,
                        "canvas_height": 420,
                    },
                },
                {
                    "id": "cdp:canvas",
                    "source": {
                        "kind": "cdp",
                        "backend_node_id": 900,
                        "frame_id": "frame-main",
                        "frame_url": FIXTURE_URL,
                    },
                    "attributes": {"id": "topology-canvas"},
                },
            ],
        }

        target = CanvasGrounder().resolve(
            {
                "source": "canvas",
                "action": "select_canvas_asset",
                "asset_id": "ap_001",
                "name": "AP_001",
            },
            graph,
        )

        self.assertEqual(target.canvas_backend_node_id, 900)
        self.assertAlmostEqual(target.x_ratio, 115 / 520, places=6)
        self.assertAlmostEqual(target.y_ratio, 135 / 420, places=6)
        changed = copy.deepcopy(graph)
        changed["nodes"][0]["source"]["producer_id"] = "untrusted"
        with self.assertRaises(GroundingError):
            CanvasGrounder().resolve(
                {
                    "source": "canvas",
                    "action": "select_canvas_asset",
                    "asset_id": "ap_001",
                    "name": "AP_001",
                },
                changed,
            )


class ScenarioRunnerContractTest(unittest.TestCase):
    def test_runner_uses_fresh_capture_for_every_step(self):
        generated = ExecutionScenarioService(root=Path("."), runner=None).generate_plan(
            start_url=FIXTURE_URL,
            user_request=REQUEST,
        )

        class Client:
            def __init__(self):
                self.capture_count = 0
                self.canvas_flags = []

            def reset_execution_fixture(self, page_url):
                self.page_url = page_url

            def bind_page_target(self, page_url):
                return {"target_id": "target-1", "page_url": page_url}

            def capture_page_payload(self, *, expected_page_url, include_canvas=True):
                self.capture_count += 1
                self.canvas_flags.append(include_canvas)
                return {
                    "page_url": expected_page_url,
                    "sequence": self.capture_count,
                    "include_canvas": include_canvas,
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
                return {
                    "schema_version": "kt6.ui-graph.v1",
                    "graph_id": f"uig:{capture_id}",
                    "capture_id": capture_id,
                    "page": {"url": FIXTURE_URL},
                    "analysis_only": True,
                    "execution_authorized": False,
                    "safe_for_execution": False,
                    "stats": {"truncated": False},
                    "nodes": [],
                }

            def get_action_snapshot(self, capture_id):
                return copy.deepcopy(self.snapshots[capture_id])

        class Grounders:
            def resolve(self, target, graph):
                if target.get("source") == "canvas":
                    return CanvasTarget(
                        node_id="vision:ap1",
                        canvas_backend_node_id=900,
                        frame_id="frame-main",
                        frame_url=FIXTURE_URL,
                        page_url=FIXTURE_URL,
                        canvas_dom_id="topology-canvas",
                        asset_id="ap_001",
                        x_ratio=0.3,
                        y_ratio=0.4,
                        producer_id="execution-fixture-canvas-cv",
                    )
                return GroundedDOMStep(
                    graph_id=graph["graph_id"],
                    capture_id=graph["capture_id"],
                    target_node_id="cdp:target",
                    asset_id=target["asset_id"],
                    action_id=target["action"],
                )

        class SafeActions:
            def __init__(self):
                self.plan = 0

            def prepare(self, **_kwargs):
                self.plan += 1
                return {"status": "prepared", "plan_id": f"plan-{self.plan}"}

            def preflight(self, **_kwargs):
                return {"status": "ready", "execution_token": "token"}

            def execute(self, **_kwargs):
                return {"status": "executed_pending_verification"}

            def verify_outcome(self, **_kwargs):
                return {"status": "verified"}

        class Executor:
            def __init__(self, client):
                self.client = client

            def execute(self, _action):
                return BrowserExecutionResult(True, "", backend_node_id=900, x=1, y=1)

        class Verifiers:
            def verify_expected(self, **_kwargs):
                return True, "fixture-verifier"

        client = Client()
        scratch_root = Path.cwd() / ".test-tmp"
        scratch_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch_root) as temp_dir:
            runner = ScenarioRunner(
                page_perception=Perception(),
                safe_dom_actions=SafeActions(),
                browser_executor=Executor(client),
                grounders=Grounders(),
                verifiers=Verifiers(),
                clock=lambda: 10.0,
                wait=lambda _seconds: None,
            )
            result = runner.run(
                generated["plan"],
                run_id="run-test",
                out_dir=Path(temp_dir) / "evidence",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["steps"]), 6)
        self.assertEqual(result["capture_count"], 8)
        self.assertEqual(client.capture_count, 8)
        self.assertEqual(client.canvas_flags.count(True), 1)


if __name__ == "__main__":
    unittest.main()
