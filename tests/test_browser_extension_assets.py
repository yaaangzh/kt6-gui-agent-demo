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
        self.assertEqual(manifest["version"], "0.5.1")
        self.assertEqual(
            set(manifest["permissions"]), {"activeTab", "scripting", "storage"}
        )
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
        self.assertIn('ui_version: "kt6-browser-extension-v0.5.1"', popup_script)
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
        self.assertIn("parent_relation", collector_script)
        self.assertIn("omitted_ancestor_count", collector_script)
        self.assertIn("dom: { elements, stats }", popup_script)
        self.assertIn("globalThis.__KT6_PAGE_ADAPTER__", popup_script)
        self.assertIn('world: "MAIN"', popup_script)
        self.assertIn('source_type: "explicit_page_adapter"', popup_script)
        self.assertIn('safe_for_execution: false', popup_script)
        self.assertIn("SENSITIVE_KEY", popup_script)
        self.assertIn("AbortController", popup_script)
        self.assertIn("captureInFlight", popup_script)
        self.assertIn("backendWaitMilliseconds", popup_script)
        self.assertIn('data-metric="page-adapter"', popup_html)
        self.assertIn("v0.5.1", popup_html)
        self.assertIn("/api/perception/capture-jobs", popup_script)
        self.assertIn("client_request_id: clientRequestId", popup_script)
        self.assertIn("chrome.storage.local", popup_script)
        self.assertIn("PENDING_CAPTURE_STORAGE_KEY", popup_script)
        self.assertIn("waitForCaptureJob", popup_script)
        self.assertIn("resumePendingCaptureOnOpen", popup_script)
        self.assertIn('status === "error" || status === "failed"', popup_script)
        self.assertIn("visionConfigurationState", popup_script)
        self.assertNotIn("fetch(CAPTURE_ENDPOINT", popup_script)
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
            const visionStates = [
              utils.visionConfigurationState({ vision: { configured: true } }),
              utils.visionConfigurationState({ vision: { configured: false } }),
              utils.visionConfigurationState(null),
            ];
            process.stdout.write(JSON.stringify({ clipped, pixels, visionStates }));
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
        self.assertEqual(result["visionStates"], [True, False, None])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_explicit_page_adapter_is_read_only_bounded_and_sanitized(self):
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
              location: { href: "https://nce.example/topology" },
              document: {
                querySelector() { return stubElement; },
                createElement() { throw new Error("not used in this test"); },
              },
              __KT6_PAGE_ADAPTER__: {
                adapter_id: "nce-readonly",
                adapter_version: "2.0",
                snapshot() {
                  return {
                    objects: [{
                      business_id: "ap_001",
                      label: "AP1",
                      x: 10,
                      y: 20,
                      safe_for_execution: true,
                      interaction: { clickable: true },
                      attributes: { status: "online", token: "secret" },
                    }],
                    links: [],
                  };
                },
              },
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context);
            context.readExplicitPageAdapter().then((result) => {
              process.stdout.write(JSON.stringify(result));
            });
            """
        )
        completed = subprocess.run(
            [shutil.which("node"), "-e", node_program, str(popup_script)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "available")
        scene = result["adapter_scene"]
        node = scene["objects"][0]
        self.assertNotIn("safe_for_execution", node)
        self.assertNotIn("interaction", node)
        self.assertNotIn("token", node["attributes"])
        self.assertEqual(node["attributes"]["status"], "online")
        self.assertFalse(scene["source_metadata"]["safe_for_execution"])
        self.assertEqual(scene["source_metadata"]["adapter_id"], "nce-readonly")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_malformed_page_adapter_relations_fail_closed_without_throwing(self):
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
              setTimeout,
              clearTimeout,
              location: { href: "https://nce.example/topology?token=hidden" },
              document: {
                querySelector() { return stubElement; },
                createElement() { throw new Error("not used in this test"); },
              },
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context);
            (async () => {
              const snapshots = [
                {
                  objects: [{ business_id: "ap_1", x: 1, y: 2 }],
                  links: { source: "ap_1", target: "ap_1" },
                },
                {
                  objects: [
                    { business_id: "ap_1", x: 1, y: 2 },
                    { business_id: "ap_1", x: 3, y: 4 },
                  ],
                  links: [],
                },
                {
                  objects: [{ business_id: "ap_1", x: 1, y: 2 }],
                  links: [{ source: "ap_1", target: "missing" }],
                },
              ];
              const statuses = [];
              for (const snapshot of snapshots) {
                context.__KT6_PAGE_ADAPTER__ = {
                  snapshot() { return snapshot; },
                };
                const result = await context.readExplicitPageAdapter();
                statuses.push(result.status);
              }
              process.stdout.write(JSON.stringify(statuses));
            })();
            """
        )
        completed = subprocess.run(
            [shutil.which("node"), "-e", node_program, str(popup_script)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(completed.stdout), ["invalid"] * 3)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_capture_job_polling_clears_terminal_and_keeps_timeout_pending(self):
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
              setTimeout,
              clearTimeout,
              AbortController,
              encodeURIComponent,
              document: {
                querySelector() { return stubElement; },
                createElement() { throw new Error("not used in this test"); },
              },
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context);

            (async () => {
              const utils = context.__KT6_CAPTURE_JOB_INTERNALS__;
              let pending = {
                job_id: "capture_job_test",
                submitted_at_ms: Date.now(),
                context: {},
              };
              let removed = false;
              context.chrome = {
                storage: {
                  local: {
                    async get(key) { return { [key]: pending }; },
                    async set(value) { pending = Object.values(value)[0]; },
                    async remove() { pending = null; removed = true; },
                  },
                },
              };
              let requests = 0;
              context.fetch = async () => {
                requests += 1;
                return {
                  ok: true,
                  status: 200,
                  async json() {
                    return requests === 1
                      ? { status: "running" }
                      : {
                          status: "completed",
                          capture: { capture_id: "capture_done", summary: {} },
                        };
                  },
                };
              };
              const completed = await utils.waitForCaptureJob(
                pending,
                null,
                () => {},
                { pollIntervalMs: 0, waitMs: 1000 },
              );
              const completedRemoved = removed;

              removed = false;
              pending = {
                job_id: "capture_job_slow",
                submitted_at_ms: Date.now(),
                context: {},
              };
              context.fetch = async () => ({
                ok: true,
                status: 200,
                async json() { return { status: "running" }; },
              });
              let timeoutKept = false;
              try {
                await utils.waitForCaptureJob(
                  pending,
                  null,
                  () => {},
                  { pollIntervalMs: 0, waitMs: 0 },
                );
              } catch (error) {
                timeoutKept = error.keepPending === true && pending !== null;
              }
              const timeoutRemoved = removed;
              removed = false;
              pending = {
                job_id: "capture_job_failed",
                submitted_at_ms: Date.now(),
                context: {},
              };
              context.fetch = async () => ({
                ok: true,
                status: 200,
                async json() {
                  return {
                    status: "failed",
                    error: { code: "processing_failed", message: "failed safely" },
                  };
                },
              });
              let failureCleared = false;
              try {
                await utils.waitForCaptureJob(pending, null, () => {}, {
                  pollIntervalMs: 0,
                  waitMs: 1000,
                });
              } catch (error) {
                failureCleared = removed && pending === null && error.message === "failed safely";
              }
              removed = false;
              pending = {
                job_id: "capture_job_expired",
                submitted_at_ms: Date.now(),
                context: {},
              };
              context.fetch = async () => ({
                ok: false,
                status: 404,
                async json() { return { error: "page capture job not found" }; },
              });
              let expiredCleared = false;
              try {
                await utils.waitForCaptureJob(pending, null, () => {}, {
                  pollIntervalMs: 0,
                  waitMs: 1000,
                });
              } catch (error) {
                expiredCleared =
                  removed &&
                  pending === null &&
                  error.httpStatus === 404 &&
                  error.status === 404 &&
                  error.message ===
                    "后端已重启或任务记录已过期，请重新采集";
              }
              process.stdout.write(JSON.stringify({
                captureId: completed.capture_id,
                requests,
                completedRemoved,
                timeoutKept,
                timeoutRemoved,
                failureCleared,
                expiredCleared,
              }));
            })();
            """
        )
        completed = subprocess.run(
            [shutil.which("node"), "-e", node_program, str(popup_script)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["captureId"], "capture_done")
        self.assertEqual(result["requests"], 2)
        self.assertTrue(result["completedRemoved"])
        self.assertTrue(result["timeoutKept"])
        self.assertFalse(result["timeoutRemoved"])
        self.assertTrue(result["failureCleared"])
        self.assertTrue(result["expiredCleared"])


if __name__ == "__main__":
    unittest.main()
