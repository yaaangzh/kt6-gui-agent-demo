from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ExecutionE2EAssetsTest(unittest.TestCase):
    def test_fixture_keeps_natural_language_and_real_pages_only(self):
        page = (ROOT / "demo" / "execution-test.html").read_text(encoding="utf-8")

        self.assertIn('id="ap-001-details"', page)
        self.assertIn('data-action-id="device.details"', page)
        self.assertIn('topologyButton.dataset.actionId = "navigation.topology"', page)
        self.assertIn('id="topology-canvas"', page)
        self.assertIn('result.dataset.testid = "canvas-selection-result"', page)
        self.assertIn('document.createElement("section")', page)
        launcher = (ROOT / "scripts" / "start-browser-executor.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('KT6_BROWSER_EXECUTION_DRIVER = "browser_harness"', launcher)
        self.assertIn('[string]$InitialTargetUrl = ""', launcher)
        self.assertIn("if ($null -ne $targetUri)", launcher)
        self.assertIn("chrome://extensions", launcher)
        self.assertIn('ArgumentList @("--new-tab", $targetUri.AbsoluteUri)', launcher)
        self.assertIn("Resolve-ChromeExecutable", launcher)
        self.assertNotIn("--remote-debugging-port", launcher)
        self.assertNotIn("--load-extension", launcher)
        self.assertNotIn("--user-data-dir", launcher)
        self.assertIn("/api/execution/health", launcher)
        self.assertIn("chrome://inspect/#remote-debugging", launcher)
        self.assertIn("No fixed remote-debugging port", launcher)
        self.assertIn("no CodeAgent/GLM CLI configuration is required", launcher)
        env_template = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("KT6_MODEL_API_BASE_URL", env_template)
        self.assertNotIn("KT6_CODEAGENT_", env_template)
        self.assertNotIn("codeagent_cli", env_template)
        self.assertFalse(
            (ROOT / "fixtures" / "execution" / "mock_ui_graph.json").exists()
        )
        self.assertFalse(
            (ROOT / "fixtures" / "execution" / "open_ap_details_intent.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
