import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kt6_backend import app
from kt6_backend.ui_operation_graph import SCHEMA_VERSION as OPERATION_SCHEMA_VERSION
from tests.test_asset_action_integration import browser_payload


class GraphAwareReasoner:
    reasoner_id = "internal-glm-test"
    reasoner_version = "5.1-test"

    def plan(self, *, instruction, ui_graph_text, graph_id):
        graph = json.loads(ui_graph_text)
        target = next(
            node["id"]
            for node in graph["nodes"]
            if node.get("interaction", {}).get("candidate") is True
            and node.get("source", {}).get("kind") in {"dom", "cdp"}
        )
        return {
            "schema_version": OPERATION_SCHEMA_VERSION,
            "graph_id": graph_id,
            "steps": [
                {"id": "locate-target", "op": "locate", "target_node_id": target},
                {
                    "id": "click-target",
                    "op": "click",
                    "target_node_id": target,
                    "depends_on": ["locate-target"],
                    "condition": {
                        "step_id": "locate-target",
                        "predicate": "found",
                    },
                },
            ],
        }


class UIGraphConfigTest(unittest.TestCase):
    def test_reasoner_environment_is_explicit_bounded_and_secret_safe(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(app._create_ui_graph_reasoner_from_env())

        with patch.dict(
            os.environ,
            {app.UI_GRAPH_REASONER_API_KEY_ENV: "orphan-secret"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, app.UI_GRAPH_REASONER_ENDPOINT_ENV):
                app._create_ui_graph_reasoner_from_env()

        with patch.dict(
            os.environ,
            {
                app.UI_GRAPH_REASONER_ENDPOINT_ENV: "https://glm.internal.example/ui-plan",
                app.UI_GRAPH_REASONER_API_KEY_ENV: "internal-secret",
                app.UI_GRAPH_REASONER_TIMEOUT_ENV: "12.5",
                app.UI_GRAPH_REASONER_ALLOWED_HOSTS_ENV: "glm.internal.example",
            },
            clear=True,
        ):
            reasoner = app._create_ui_graph_reasoner_from_env()
        self.assertEqual(reasoner.timeout_seconds, 12.5)
        self.assertEqual(reasoner.api_key, "internal-secret")

        with patch.dict(
            os.environ,
            {
                app.UI_GRAPH_REASONER_ENDPOINT_ENV: (
                    "https://glm.internal.example/ui-plan"
                ),
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "allowed_hosts"):
                app._create_ui_graph_reasoner_from_env()

        with patch.dict(
            os.environ,
            {
                app.UI_GRAPH_REASONER_ENDPOINT_ENV: "https://glm.internal.example/ui-plan",
                app.UI_GRAPH_REASONER_TIMEOUT_ENV: "NaN",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, app.UI_GRAPH_REASONER_TIMEOUT_ENV):
                app._create_ui_graph_reasoner_from_env()


class UIGraphAPITest(unittest.TestCase):
    def test_capture_graph_and_glm_plan_are_exposed_as_dry_run_only(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {}, clear=True
        ):
            root = Path(temp_dir)
            (root / "data").mkdir()
            (root / "data" / "mock_assets.json").write_text(
                '{"assets":[]}', encoding="utf-8"
            )
            server, services = app.create_server(
                host="127.0.0.1",
                port=0,
                root=root,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                capture = self.request(
                    base_url,
                    "/api/perception/captures",
                    method="POST",
                    payload=browser_payload(),
                    expected_status=201,
                )
                graph = self.request(
                    base_url,
                    f"/api/ui-graphs/{capture['capture_id']}",
                    expected_status=200,
                )
                self.assertEqual(graph["schema_version"], "kt6.ui-graph.v1")
                self.assertTrue(graph["analysis_only"])
                self.assertFalse(graph["execution_authorized"])
                self.assertGreater(len(graph["nodes"]), 0)

                unavailable = self.request(
                    base_url,
                    "/api/ui-operations/plan",
                    method="POST",
                    payload={
                        "page_capture_id": capture["capture_id"],
                        "instruction": "打开设备详情",
                    },
                    expected_status=503,
                )
                self.assertIn("not configured", unavailable["error"])

                services.ui_graph_planning.reasoner = GraphAwareReasoner()
                health = self.request(base_url, "/api/health", expected_status=200)
                self.assertTrue(health["ui_graph_reasoning"]["configured"])
                self.assertNotIn("endpoint", health["ui_graph_reasoning"])
                self.assertNotIn("api_key", health["ui_graph_reasoning"])

                proposal = self.request(
                    base_url,
                    "/api/ui-operations/plan",
                    method="POST",
                    payload={
                        "page_capture_id": capture["capture_id"],
                        "instruction": "打开设备详情",
                    },
                    expected_status=200,
                )
                self.assertEqual(proposal["status"], "planned")
                self.assertTrue(proposal["dry_run_only"])
                self.assertFalse(proposal["safe_for_execution"])
                self.assertTrue(proposal["validation"]["valid"])
                self.assertFalse(proposal["validation"]["safe_for_execution"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    @staticmethod
    def request(
        base_url,
        path,
        *,
        method="GET",
        payload=None,
        expected_status,
    ):
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(base_url + path, data=body, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            result = json.loads(response.read().decode("utf-8"))
            if response.status != expected_status:
                raise AssertionError(
                    f"expected HTTP {expected_status}, got {response.status}: {result}"
                )
            return result


if __name__ == "__main__":
    unittest.main()
