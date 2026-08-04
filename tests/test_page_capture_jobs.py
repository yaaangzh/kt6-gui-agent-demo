from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kt6_backend.app import create_server
from kt6_backend.page_capture_jobs import (
    PageCaptureJobCapacityError,
    PageCaptureJobService,
)


class BlockingPerception:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.payloads: list[dict] = []

    def ingest(self, payload: dict) -> dict:
        self.payloads.append(payload)
        self.started.set()
        self.release.wait(timeout=3)
        return {"capture_id": "capture_async", "status": "captured"}


class FailingPerception:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def ingest(self, payload: dict) -> dict:
        raise self.error


def wait_for_terminal(service: PageCaptureJobService, job_id: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = service.get(job_id)
        if job and job["status"] in {"completed", "error"}:
            return job
        time.sleep(0.01)
    raise AssertionError("page capture job did not reach a terminal state")


class PageCaptureJobServiceTest(unittest.TestCase):
    def test_submit_is_non_blocking_idempotent_and_returns_public_state(self):
        perception = BlockingPerception()
        service = PageCaptureJobService(
            perception,
            id_factory=lambda: "capture_job_fixed",
        )
        payload = {"page": {"url": "https://example.invalid"}, "dom": {}}

        job = service.submit(client_request_id="popup-request-1", payload=payload)
        self.assertTrue(perception.started.wait(timeout=1))
        self.assertIn(job["status"], {"queued", "running"})
        self.assertEqual(job["job_id"], "capture_job_fixed")
        self.assertNotIn("payload", job)
        self.assertNotIn("_payload_digest", job)

        duplicate = service.submit(
            client_request_id="popup-request-1",
            payload=payload,
        )
        self.assertEqual(duplicate["job_id"], job["job_id"])
        self.assertEqual(len(perception.payloads), 1)

        payload["page"]["url"] = "https://mutated.invalid"
        perception.release.set()
        completed = wait_for_terminal(service, job["job_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["capture"]["capture_id"], "capture_async")
        self.assertEqual(
            perception.payloads[0]["page"]["url"],
            "https://example.invalid",
        )

        completed["capture"]["capture_id"] = "mutated"
        self.assertEqual(
            service.get(job["job_id"])["capture"]["capture_id"],
            "capture_async",
        )

    def test_reused_request_id_with_different_payload_is_rejected(self):
        perception = BlockingPerception()
        service = PageCaptureJobService(perception)
        service.submit(client_request_id="request-1", payload={"value": 1})
        with self.assertRaisesRegex(ValueError, "different payload"):
            service.submit(client_request_id="request-1", payload={"value": 2})
        perception.release.set()

    def test_worker_errors_are_terminal_and_do_not_leak_runtime_details(self):
        secret = "token=production-secret"
        service = PageCaptureJobService(FailingPerception(RuntimeError(secret)))
        with self.assertLogs("kt6_backend.page_capture_jobs", level="ERROR"):
            job = service.submit(client_request_id="request-failure", payload={})
            failed = wait_for_terminal(service, job["job_id"])

        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["error"]["code"], "processing_failed")
        self.assertNotIn(secret, json.dumps(failed))

    def test_capacity_never_evicts_active_work_and_prunes_completed_work(self):
        perception = BlockingPerception()
        identifiers = iter(("capture_job_one", "capture_job_two"))
        service = PageCaptureJobService(
            perception,
            max_jobs=1,
            id_factory=lambda: next(identifiers),
        )
        first = service.submit(client_request_id="request-one", payload={"value": 1})
        self.assertTrue(perception.started.wait(timeout=1))
        with self.assertRaises(PageCaptureJobCapacityError):
            service.submit(client_request_id="request-two", payload={"value": 2})

        perception.release.set()
        wait_for_terminal(service, first["job_id"])
        second = service.submit(client_request_id="request-two", payload={"value": 2})
        self.assertEqual(second["job_id"], "capture_job_two")
        self.assertIsNone(service.get(first["job_id"]))

    def test_request_identifiers_and_payloads_are_validated(self):
        service = PageCaptureJobService(BlockingPerception())
        for invalid in ("", "has spaces", "slash/not-allowed", "x" * 129):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                service.submit(client_request_id=invalid, payload={})
        with self.assertRaisesRegex(ValueError, "JSON object"):
            service.submit(client_request_id="valid", payload=[])
        with self.assertRaisesRegex(ValueError, "JSON-compatible"):
            service.submit(client_request_id="valid", payload={"bad": object()})


class PageCaptureJobAPITest(unittest.TestCase):
    def test_async_capture_job_endpoint_preserves_sync_capture_endpoint(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            tempfile.TemporaryDirectory() as temp_dir,
        ):
            server, _services = create_server(
                host="127.0.0.1",
                port=0,
                root=Path(temp_dir),
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            payload = {
                "page": {
                    "url": "https://example.invalid/topology",
                    "title": "Topology",
                    "viewport": {"width": 1280, "height": 720},
                },
                "dom": {"elements": [], "stats": {}},
                "canvases": [],
            }
            try:
                submitted = self._json_request(
                    f"{base_url}/api/perception/capture-jobs",
                    method="POST",
                    body={"client_request_id": "browser-popup-1", "payload": payload},
                    expected_status=202,
                )
                duplicate = self._json_request(
                    f"{base_url}/api/perception/capture-jobs",
                    method="POST",
                    body={"client_request_id": "browser-popup-1", "payload": payload},
                    expected_status=202,
                )
                self.assertEqual(submitted["job_id"], duplicate["job_id"])

                deadline = time.monotonic() + 3
                while True:
                    current = self._json_request(
                        f"{base_url}/api/perception/capture-jobs/{submitted['job_id']}"
                    )
                    if current["status"] in {"completed", "error"}:
                        break
                    if time.monotonic() >= deadline:
                        self.fail("HTTP page capture job did not complete")
                    time.sleep(0.02)
                self.assertEqual(current["status"], "completed")
                self.assertIn("capture_id", current["capture"])

                synchronous = self._json_request(
                    f"{base_url}/api/perception/captures",
                    method="POST",
                    body=payload,
                    expected_status=201,
                )
                self.assertIn("capture_id", synchronous)

                with self.assertRaises(HTTPError) as raised:
                    self._json_request(
                        f"{base_url}/api/perception/capture-jobs/"
                        f"{submitted['job_id']}/extra"
                    )
                self.assertEqual(raised.exception.code, 404)
                raised.exception.close()

                with self.assertRaises(HTTPError) as raised:
                    self._json_request(
                        f"{base_url}/api/dom-actions/plans/plan-id/extra"
                    )
                self.assertEqual(raised.exception.code, 404)
                raised.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    @staticmethod
    def _json_request(
        url: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        expected_status: int = 200,
    ) -> dict:
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        with urlopen(request, timeout=3) as response:
            if response.status != expected_status:
                raise AssertionError(
                    f"expected HTTP {expected_status}, got {response.status}"
                )
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
