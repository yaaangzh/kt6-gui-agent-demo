import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / "browser_extension"


class BrowserExtensionAssetsTest(unittest.TestCase):
    def test_manifest_uses_semantic_collector_without_broad_site_permission(self):
        manifest = json.loads(
            (EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["version"], "0.4.0")
        self.assertEqual(set(manifest["permissions"]), {"activeTab", "scripting"})
        self.assertNotIn("<all_urls>", manifest.get("host_permissions", []))
        self.assertNotIn("tabCapture", manifest["permissions"])
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
        self.assertIn('"svg text"', collector_script)
        self.assertIn("candidates.length < 20", collector_script)
        self.assertIn("visual_regions: visualRegions", collector_script)
        self.assertIn('document.querySelectorAll("svg")', collector_script)
        self.assertIn("source_kind: sourceKind", collector_script)
        self.assertIn("graphicAncestorCounts", collector_script)
        self.assertIn("svg_element_texts: svgElementTexts", collector_script)
        self.assertEqual(popup_script.count("chrome.tabs.captureVisibleTab("), 1)
        self.assertIn("MAX_VISIBLE_SCREENSHOTS = 1", popup_script)
        self.assertIn("createImageBitmap", popup_script)
        self.assertIn("capture_visible_tab_crop", popup_script)
        self.assertIn('roi_status: "unverified"', popup_script)
        self.assertIn('ui_version: "kt6-browser-extension-v0.4"', popup_script)
        self.assertIn("svg_element_texts: svgElementTexts", popup_script)
        self.assertIn(
            "const canvases = primaryEvidence ? [primaryEvidence] : [];",
            popup_script,
        )
        self.assertIn("page_screenshot_fallback", popup_script)
        self.assertIn("documentProbe.documentId", popup_script)
        self.assertIn('data-metric="visible-screenshot"', popup_html)
        self.assertIn('data-metric="perception-decision"', popup_html)
        self.assertIn("fallback_text_count", collector_script)
        self.assertIn("truncated", collector_script)
        for field in (
            "asset_id",
            "management_ip",
            "serial_number",
            "action_id",
            "owner_business_id",
        ):
            self.assertIn(field, collector_script)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_visible_tab_crop_math_uses_actual_screenshot_dimensions(self):
        popup_script = EXTENSION_DIR / "popup-v2.js"
        node_program = textwrap.dedent(
            """
            const fs = require("fs");
            const vm = require("vm");
            const scriptPath = process.argv[1];
            const stubElement = {
              addEventListener() {},
              classList: { add() {}, remove() {} },
              dataset: {},
              replaceChildren() {},
              textContent: "",
              hidden: false,
            };
            const context = {
              console,
              document: {
                querySelector() { return stubElement; },
                createElement() { throw new Error("not used in this test"); },
              },
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context);
            const utils = context.__KT6_VISUAL_CAPTURE_INTERNALS__;
            const clipped = utils.clippedRegion(
              [-50, 25, 200, 100],
              { width: 100, height: 100 },
            );
            const pixels = utils.sourcePixelRegionFor(
              [100, 50, 600, 400],
              { width: 1200, height: 800 },
              2400,
              1600,
            );
            process.stdout.write(JSON.stringify({ clipped, pixels }));
            """
        )
        completed = subprocess.run(
            [shutil.which("node"), "-e", node_program, str(popup_script)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["clipped"], [0, 25, 100, 75])
        self.assertEqual(result["pixels"], [200, 100, 1200, 800])


if __name__ == "__main__":
    unittest.main()
