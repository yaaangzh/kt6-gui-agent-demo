from __future__ import annotations

import threading
import unittest

from kt6_backend.execution.browser_harness_client import BrowserHarnessError
from kt6_backend.execution.extension_runtime import (
    BrowserExtensionClient,
    BrowserExtensionRuntimeManager,
    ExtensionRuntimeError,
)
from kt6_backend.execution.url_policy import ExecutionURLPolicy


class BrowserExtensionRuntimeManagerTest(unittest.TestCase):
    def setUp(self):
        self.manager = BrowserExtensionRuntimeManager(
            url_policy=ExecutionURLPolicy(
                resolver=lambda _host: ("93.184.216.34",)
            ),
            command_timeout_seconds=2,
        )
        self.runtime_id = "runtime_1234567890abcdef"
        self.token = "token_1234567890abcdef1234567890abcdef1234567890abcdef"
        self.target_id = "TARGET12345678"
        self.manager.register(
            runtime_id=self.runtime_id,
            token=self.token,
            target_id=self.target_id,
            page_url="https://example.com/",
            title="Example",
        )

    def test_authenticated_poll_round_trip_returns_only_fixed_command(self):
        result = []

        def dispatch():
            result.append(
                self.manager.dispatch(
                    self.runtime_id,
                    kind="cdp",
                    method="Page.getFrameTree",
                )
            )

        worker = threading.Thread(target=dispatch)
        worker.start()
        polled = self.manager.poll(
            runtime_id=self.runtime_id,
            token=self.token,
            wait_milliseconds=1000,
        )
        command = polled["command"]
        self.assertEqual(command["kind"], "cdp")
        self.assertEqual(command["method"], "Page.getFrameTree")
        self.manager.complete(
            runtime_id=self.runtime_id,
            token=self.token,
            command_id=command["command_id"],
            result={"frameTree": {"frame": {"url": "https://example.com/"}}},
        )
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["frameTree"]["frame"]["url"], "https://example.com/")
        self.assertTrue(self.manager.health()["ready"])

    def test_relay_rejects_unknown_cdp_and_wrong_token(self):
        with self.assertRaisesRegex(
            ExtensionRuntimeError,
            "browser_extension_cdp_method_not_allowed",
        ):
            self.manager.dispatch(
                self.runtime_id,
                kind="cdp",
                method="Runtime.evaluate",
            )
        with self.assertRaisesRegex(
            ExtensionRuntimeError,
            "browser_extension_runtime_unauthorized",
        ):
            self.manager.poll(
                runtime_id=self.runtime_id,
                token="token_wrongwrongwrongwrongwrongwrongwrongwrong",
                wait_milliseconds=0,
            )

    def test_client_binds_only_registered_runtime_target_and_live_page(self):
        observed = []

        def extension_worker():
            polled = self.manager.poll(
                runtime_id=self.runtime_id,
                token=self.token,
                wait_milliseconds=1000,
            )
            command = polled["command"]
            observed.append(command)
            self.manager.complete(
                runtime_id=self.runtime_id,
                token=self.token,
                command_id=command["command_id"],
                result={
                    "frameTree": {
                        "frame": {
                            "id": "main",
                            "url": "https://example.com/",
                        }
                    }
                },
            )

        worker = threading.Thread(target=extension_worker)
        worker.start()
        client = BrowserExtensionClient(
            manager=self.manager,
            url_policy=self.manager.url_policy,
        )
        binding = client.open_or_bind_target(
            "https://example.com/",
            target_id=self.target_id,
            runtime_id=self.runtime_id,
        )
        worker.join(2)

        self.assertEqual(binding["runtime_id"], self.runtime_id)
        self.assertEqual(binding["target_id"], self.target_id)
        self.assertEqual(observed[0]["method"], "Page.getFrameTree")
        with self.assertRaisesRegex(
            BrowserHarnessError,
            "browser_target_binding_mismatch",
        ):
            client.open_or_bind_target(
                "https://example.com/",
                target_id="TARGET87654321",
                runtime_id=self.runtime_id,
            )


if __name__ == "__main__":
    unittest.main()
