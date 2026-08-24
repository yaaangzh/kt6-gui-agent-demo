import os
from pathlib import Path
import unittest

from kt6_backend.execution_e2e_cli import run_scenario_e2e


ROOT = Path(__file__).resolve().parents[1]


class ExecutionE2EAssetsTest(unittest.TestCase):
    def test_fixture_keeps_natural_language_and_real_pages_only(self):
        page = (ROOT / "demo" / "execution-test.html").read_text(encoding="utf-8")
        runner = (ROOT / "demo" / "execution-runner.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="ap-001-details"', page)
        self.assertIn('data-action-id="device.details"', page)
        self.assertIn('topologyButton.dataset.actionId = "navigation.topology"', page)
        self.assertIn('id="topology-canvas"', page)
        self.assertIn('result.dataset.testid = "canvas-selection-result"', page)
        self.assertIn('document.createElement("section")', page)
        self.assertIn("/api/execution/plans", runner)
        self.assertIn("/api/execution/runs", runner)
        self.assertIn("/api/execution/health", runner)
        launcher = (ROOT / "scripts" / "start-browser-executor.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("--remote-debugging-port=$CdpPort", launcher)
        self.assertIn("--load-extension=$extensionPath", launcher)
        self.assertIn("--disable-extensions-except=$extensionPath", launcher)
        self.assertIn("runtime_preflight ensure", launcher)
        self.assertIn("/api/execution/health", launcher)
        self.assertIn("executionDeadline", launcher)
        self.assertFalse(
            (ROOT / "fixtures" / "execution" / "mock_ui_graph.json").exists()
        )
        self.assertFalse(
            (ROOT / "fixtures" / "execution" / "open_ap_details_intent.json").exists()
        )


@unittest.skipUnless(
    os.environ.get("KT6_RUN_BROWSER_E2E") == "1",
    "requires Python 3.12, Browser Harness and a connected Chromium",
)
class RealBrowserExecutionE2ETest(unittest.TestCase):
    def test_generic_natural_language_scenario(self):
        result = run_scenario_e2e(
            start_url="http://127.0.0.1:8787/execution-test.html",
            user_request="打开 AP_001 的详情，然后进入拓扑页面，再在拓扑中选中 AP_001",
        )

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["steps"])
        self.assertTrue(all(step["status"] == "completed" for step in result["steps"]))


if __name__ == "__main__":
    unittest.main()
