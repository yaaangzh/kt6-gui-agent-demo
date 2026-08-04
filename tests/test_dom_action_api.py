import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kt6_backend.app import create_server
from tests.test_asset_action_integration import browser_payload


class DOMActionAPITest(unittest.TestCase):
    def test_http_api_runs_prepare_preflight_and_dry_run_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "mock_assets.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                "asset_id": "ap_001",
                                "asset_type": "ap",
                                "name": "AP1",
                                "management_ip": "10.0.0.1",
                                "serial_number": "SN-001",
                                "site_id": "site-a",
                                "status": "online",
                                "version": 3,
                                "allowed_origins": ["https://nce.example"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server, _services = create_server(
                host="127.0.0.1",
                port=0,
                root=root,
            )
            thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                health = self.get(base_url, "/api/health", expected_status=200)
                self.assertEqual(health["status"], "ok")
                self.assertFalse(health["vision"]["configured"])
                self.assertEqual(
                    health["page_api"]["mode"], "explicit_read_only_adapter"
                )

                initial = self.post(
                    base_url,
                    "/api/perception/captures",
                    browser_payload(),
                    expected_status=201,
                )
                prepared = self.post(
                    base_url,
                    "/api/dom-actions/prepare",
                    {
                        "asset_reference": "AP1",
                        "action": "关闭",
                        "page_capture_id": initial["capture_id"],
                        "scope": {"site_id": "site-a"},
                        "task_id": "task-api",
                        "principal_id": "operator-api",
                    },
                    expected_status=201,
                )
                prepared_status = self.get(
                    base_url,
                    f"/api/dom-actions/plans/{prepared['plan_id']}",
                    expected_status=200,
                )
                self.assertEqual(prepared_status["status"], "prepared")
                self.assertNotIn("binding", prepared_status)
                current = self.post(
                    base_url,
                    "/api/perception/captures",
                    browser_payload(),
                    expected_status=201,
                )
                ready = self.post(
                    base_url,
                    "/api/dom-actions/preflight",
                    {
                        "plan_id": prepared["plan_id"],
                        "page_capture_id": current["capture_id"],
                        "confirmed": True,
                        "confirmed_asset_id": "ap_001",
                        "confirmed_action": "shutdown_ap",
                        "permissions": ["assets.ap.shutdown"],
                    },
                    expected_status=201,
                )
                result = self.post(
                    base_url,
                    "/api/dom-actions/execute",
                    {
                        "execution_token": ready["execution_token"],
                        "dry_run": True,
                    },
                    expected_status=200,
                )

                self.assertEqual(result["status"], "dry_run_ok")
                self.assertEqual(result["asset_id"], "ap_001")
                self.assertFalse(result["executed"])

                completed_status = self.get(
                    base_url,
                    f"/api/dom-actions/plans/{prepared['plan_id']}",
                    expected_status=200,
                )
                self.assertEqual(completed_status["status"], "dry_run_ok")
                self.assertFalse(completed_status["safe_for_execution"])

                rejected = self.post(
                    base_url,
                    "/api/dom-actions/execute",
                    {
                        "execution_token": ready["execution_token"],
                        "dry_run": False,
                    },
                    expected_status=409,
                )
                self.assertEqual(
                    rejected["reason"], "token_invalid_or_replayed"
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    @staticmethod
    def post(
        base_url: str,
        path: str,
        payload: dict,
        *,
        expected_status: int,
    ) -> dict:
        request = Request(
            base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            body = json.loads(response.read().decode("utf-8"))
            if response.status != expected_status:
                raise AssertionError(
                    f"expected HTTP {expected_status}, got {response.status}: {body}"
                )
            return body

    @staticmethod
    def get(
        base_url: str,
        path: str,
        *,
        expected_status: int,
    ) -> dict:
        request = Request(
            base_url + path,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            body = json.loads(response.read().decode("utf-8"))
            if response.status != expected_status:
                raise AssertionError(
                    f"expected HTTP {expected_status}, got {response.status}: {body}"
                )
            return body


if __name__ == "__main__":
    unittest.main()
