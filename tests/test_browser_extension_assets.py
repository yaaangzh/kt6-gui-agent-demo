import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / "browser_extension"


class BrowserExtensionAssetsTest(unittest.TestCase):
    def test_manifest_uses_semantic_collector_without_broad_site_permission(self):
        manifest = json.loads(
            (EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertEqual(set(manifest["permissions"]), {"activeTab", "scripting"})
        self.assertNotIn("<all_urls>", manifest.get("host_permissions", []))
        self.assertEqual(manifest["action"]["default_popup"], "popup.html")

    def test_popup_and_injected_collector_are_wired_together(self):
        popup_html = (EXTENSION_DIR / "popup.html").read_text(encoding="utf-8")
        popup_script = (EXTENSION_DIR / "popup-v2.js").read_text(encoding="utf-8")
        collector_script = (EXTENSION_DIR / "content-collector.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('src="popup-v2.js"', popup_html)
        self.assertIn('files: ["content-collector.js"]', popup_script)
        self.assertIn("识别到的关键元素", popup_html)
        self.assertIn("MAX_CAPTURED_ELEMENTS = 220", collector_script)
        self.assertIn("fallback_text_count", collector_script)
        self.assertIn("truncated", collector_script)


if __name__ == "__main__":
    unittest.main()
