import json
import os
from pathlib import Path
import unittest

from kt6_backend.env_config import load_project_env
from kt6_backend.execution_e2e_cli import run_dom_e2e


ROOT = Path(__file__).resolve().parents[1]
load_project_env(ROOT)


class ExecutionE2EAssetsTest(unittest.TestCase):
    def test_fixture_keeps_only_intent_and_a_real_page(self):
        page = (ROOT / "demo" / "execution-test.html").read_text(encoding="utf-8")
        intent = json.loads(
            (
                ROOT
                / "fixtures"
                / "execution"
                / "open_ap_details_intent.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(intent["goal"], "open_asset_details")
        self.assertEqual(intent["asset_id"], "ap_001")
        self.assertIn('id="ap-001-details"', page)
        self.assertIn('data-action-id="device.details"', page)
        self.assertIn('id="topology-canvas"', page)
        self.assertIn('document.createElement("section")', page)
        self.assertFalse(
            (ROOT / "fixtures" / "execution" / "mock_ui_graph.json").exists()
        )


@unittest.skipUnless(
    os.environ.get("KT6_RUN_BROWSER_E2E") == "1",
    "requires Python 3.12, Browser Harness and a connected Chromium",
)
class RealBrowserExecutionE2ETest(unittest.TestCase):
    def test_open_ap_001_details(self):
        result = run_dom_e2e()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["execution_status"], "executed_pending_verification")
        self.assertEqual(result["verification_status"], "verified")
        self.assertTrue(result["outcome_verified"])


if __name__ == "__main__":
    unittest.main()
