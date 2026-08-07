import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class CDPSidecarAssetsTest(unittest.TestCase):
    def test_sidecar_is_read_only_and_uses_required_open_source_interfaces(self):
        source = (ROOT / "browser_sidecar" / "capture-ui-graph.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("chromium.connectOverCDP", source)
        self.assertIn('"DOMSnapshot.captureSnapshot"', source)
        self.assertIn('"Accessibility.getFullAXTree"', source)
        self.assertIn("safe_for_execution: false", source)
        self.assertNotIn("page.title(", source)
        for forbidden in (
            ".click(",
            "Input.dispatchMouseEvent",
            "Input.dispatchKeyEvent",
            "Runtime.evaluate",
            "Fetch.enable",
            "Network.enable",
        ):
            self.assertNotIn(forbidden, source)

    def test_sidecar_uses_playwright_core_without_downloading_a_browser(self):
        package = json.loads(
            (ROOT / "browser_sidecar" / "package.json").read_text(encoding="utf-8")
        )
        self.assertTrue(package["private"])
        self.assertEqual(package["dependencies"]["playwright-core"], "1.49.0")
        self.assertNotIn("playwright", package["dependencies"])

    def test_exported_helpers_fail_closed_on_ambiguity_and_limits(self):
        module_uri = (ROOT / "browser_sidecar" / "capture-ui-graph.mjs").as_uri()
        script = f"""
          import {{
            appendAXNodesWithinLimit,
            assertDOMSnapshotWithinLimit,
            flattenFrames,
            selectUniquePage,
            titleFromDOMSnapshot,
          }} from {json.dumps(module_uri)};
          import assert from "node:assert/strict";

          const expectThrow = (callback, pattern) => assert.throws(callback, pattern);
          const children = Array.from({{ length: 64 }}, (_, index) => ({{
            frame: {{ id: `child-${{index}}`, url: "https://example.test/frame" }},
          }}));
          expectThrow(
            () => flattenFrames({{ frame: {{ id: "root" }}, childFrames: children }}),
            /exceeds 64 frames/,
          );
          assert.equal(
            flattenFrames({{ frame: {{ id: "root" }}, childFrames: children.slice(1) }}).length,
            64,
          );

          expectThrow(
            () => assertDOMSnapshotWithinLimit({{
              documents: [{{ nodes: {{ nodeName: Array(10_001).fill(0) }} }}],
            }}),
            /exceeds 10000 nodes/,
          );
          assert.equal(
            assertDOMSnapshotWithinLimit({{ documents: [{{ nodes: {{ nodeName: Array(10_000) }} }}] }}),
            10_000,
          );
          const axNodes = Array(9_999).fill({{}});
          expectThrow(
            () => appendAXNodesWithinLimit(axNodes, [{{}}, {{}}], "main"),
            /exceeds 10000 nodes/,
          );
          assert.equal(appendAXNodesWithinLimit(axNodes, [{{}}], "main"), 10_000);

          const snapshot = {{
            strings: ["child", "Child title", "main", "Main title"],
            documents: [
              {{ frameId: 0, title: 1 }},
              {{ frameId: 2, title: 3 }},
            ],
          }};
          assert.equal(titleFromDOMSnapshot(snapshot, "main"), "Main title");

          const page = (url) => ({{ url: () => url }});
          const target = page("https://example.test/target");
          assert.equal(selectUniquePage([page("chrome://settings"), target]), target);
          expectThrow(
            () => selectUniquePage(
              [page("https://a.test/foo"), page("https://b.test/foo")],
              "foo",
            ),
            /matched 2 pages/,
          );
          expectThrow(() => selectUniquePage([], "missing"), /matched 0 pages/);
          expectThrow(() => selectUniquePage([], "   "), /must not be empty/);
          expectThrow(
            () => selectUniquePage([page("chrome://settings")]),
            /found 0 non-chrome pages/,
          );
          expectThrow(
            () => selectUniquePage([page("https://a.test"), page("about:blank")]),
            /found 2 non-chrome pages/,
          );
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
