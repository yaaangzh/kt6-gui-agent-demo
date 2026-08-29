import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / "browser_extension"


class BrowserExtensionAssetsTest(unittest.TestCase):
    def test_manifest_exposes_only_the_side_panel_runtime(self):
        manifest = json.loads(
            (EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["version"], "0.7.0")
        self.assertEqual(
            set(manifest["permissions"]),
            {"activeTab", "sidePanel", "debugger"},
        )
        self.assertNotIn("<all_urls>", manifest.get("host_permissions", []))
        self.assertNotIn("tabCapture", manifest["permissions"])
        self.assertNotIn("default_popup", manifest["action"])
        self.assertEqual(manifest["side_panel"]["default_path"], "sidepanel.html")
        self.assertEqual(manifest["background"]["service_worker"], "background.js")
        for obsolete in (
            "content-collector.js",
            "popup-v2.js",
            "popup.css",
            "popup.html",
        ):
            self.assertFalse((EXTENSION_DIR / obsolete).exists())

    def test_side_panel_uses_exact_current_tab_binding_without_raw_debugger_control(self):
        panel_html = (EXTENSION_DIR / "sidepanel.html").read_text(encoding="utf-8")
        panel_script = (EXTENSION_DIR / "sidepanel.js").read_text(encoding="utf-8")
        background = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")

        self.assertIn('src="sidepanel.js"', panel_html)
        self.assertIn("自然语言流程", panel_html)
        self.assertIn("chrome.sidePanel.setPanelBehavior", background)
        self.assertIn("chrome.debugger.attach", background)
        self.assertIn("chrome.debugger.sendCommand", background)
        self.assertIn("validateCdpCommand", background)
        self.assertNotIn("Runtime.evaluate", background)
        self.assertIn("debuggerApi.getTargets()", panel_script)
        self.assertIn("target.tabId === tab.id", panel_script)
        self.assertIn("browser_target_id: context.browserTargetId", panel_script)
        self.assertIn("browser_runtime_id: runtime.runtimeId", panel_script)
        self.assertIn("kt6.prepareRuntime", panel_script)
        self.assertIn("/api/execution/plans", panel_script)
        self.assertIn("/api/execution/runs", panel_script)
        self.assertNotIn("chrome.debugger.attach", panel_script)
        self.assertNotIn("chrome.debugger.sendCommand", panel_script)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_extension_runtime_allows_only_fixed_cdp_parameter_shapes(self):
        background_script = EXTENSION_DIR / "background.js"
        node_program = textwrap.dedent(
            """
            const fs = require("fs");
            const vm = require("vm");
            const scriptPath = process.argv[1];
            const event = { addListener() {} };
            const context = {
              console, crypto, navigator: { userAgent: "Chrome test" },
              setTimeout, clearTimeout,
              fetch: async () => ({ ok: true, async json() { return {}; } }),
              chrome: {
                sidePanel: { async setPanelBehavior() {} },
                runtime: { onInstalled: event, onStartup: event, onMessage: event },
                debugger: { onDetach: event },
              },
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context);
            const validate = context.__KT6_EXTENSION_RUNTIME_INTERNALS__.validateCdpCommand;
            let rawRejected = false;
            let keyRejected = false;
            try { validate("Runtime.evaluate", { expression: "1+1" }); } catch (_) { rawRejected = true; }
            try {
              validate("Input.dispatchKeyEvent", {
                type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13,
              });
            } catch (_) { keyRejected = true; }
            const accepted = validate("Input.insertText", { text: "前端开源项目" });
            process.stdout.write(JSON.stringify({ rawRejected, keyRejected, accepted }));
            """
        )
        completed = subprocess.run(
            [shutil.which("node"), "-e", node_program, str(background_script)],
            check=True,
            capture_output=True,
        )
        result = json.loads(completed.stdout.decode("utf-8"))
        self.assertTrue(result["rawRejected"])
        self.assertTrue(result["keyRejected"])
        self.assertEqual(result["accepted"]["text"], "前端开源项目")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_side_panel_maps_active_tab_to_one_exact_cdp_target(self):
        panel_script = EXTENSION_DIR / "sidepanel.js"
        node_program = textwrap.dedent(
            """
            const fs = require("fs");
            const vm = require("vm");
            const scriptPath = process.argv[1];
            const stubElement = {
              addEventListener() {}, replaceChildren() {}, append() {},
              className: "", textContent: "", value: "", checked: false,
              disabled: false, hidden: false,
            };
            const context = {
              console, setTimeout, clearTimeout, encodeURIComponent,
              document: {
                querySelector() { return stubElement; },
                createElement() { return { textContent: "" }; },
              },
              chrome: {
                tabs: { async query() { return [{ id: 42 }]; } },
                debugger: { async getTargets() { return [{ id: "TARGET12345678", tabId: 42, type: "page", url: "https://example.com/" }]; } },
              },
              fetch: async () => ({ ok: true, status: 200, async json() { return { ready: true }; } }),
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context);
            (async () => {
              const exact = await context.__KT6_AGENT_PANEL_INTERNALS__.resolveCurrentBrowserContext(
                context.chrome.tabs, context.chrome.debugger,
              );
              let rejected = false;
              try {
                await context.__KT6_AGENT_PANEL_INTERNALS__.resolveCurrentBrowserContext(
                  context.chrome.tabs,
                  { async getTargets() { return [
                    { id: "TARGET12345678", tabId: 42, type: "page", url: "https://example.com/" },
                    { id: "TARGET87654321", tabId: 42, type: "page", url: "https://example.com/" },
                  ]; } },
                );
              } catch (_error) { rejected = true; }
              process.stdout.write(JSON.stringify({ exact, rejected }));
            })();
            """
        )
        completed = subprocess.run(
            [shutil.which("node"), "-e", node_program, str(panel_script)],
            check=True,
            capture_output=True,
        )
        result = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(result["exact"]["browserTargetId"], "TARGET12345678")
        self.assertEqual(result["exact"]["tabId"], 42)
        self.assertEqual(result["exact"]["startUrl"], "https://example.com/")
        self.assertTrue(result["rejected"])


if __name__ == "__main__":
    unittest.main()
