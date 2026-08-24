from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from kt6_backend.execution.runtime_preflight import (
    cdp_health,
    ensure_execution_runtime,
    execution_runtime_health,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class _Connection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _Admin:
    def __init__(self, effects=None):
        self.effects = list(effects or ())
        self.ensure_calls = []

    def daemon_alive(self):
        return True

    def ensure_daemon(self, **kwargs):
        self.ensure_calls.append(kwargs)
        if self.effects:
            effect = self.effects.pop(0)
            if isinstance(effect, Exception):
                raise effect


class _Ipc:
    def __init__(self):
        self.connection = _Connection()

    def connect(self, *_args, **_kwargs):
        return self.connection, "token"

    def request(self, _connection, _token, payload):
        if payload["method"] == "Target.getTargets":
            return {"result": {"targetInfos": []}}
        return {}


def _ready_cdp(_request, **_kwargs):
    return _Response(
        {
            "Browser": "Chrome/151",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test",
        }
    )


class ExecutionRuntimePreflightTest(unittest.TestCase):
    def test_cdp_health_requires_loopback_and_a_real_websocket_endpoint(self):
        self.assertEqual(
            cdp_health("https://example.com:9222"),
            {"ready": False, "error": "browser_cdp_url_invalid"},
        )
        self.assertEqual(
            cdp_health("http://127.0.0.1:9222", opener=_ready_cdp),
            {"ready": True, "error": None},
        )

    def test_runtime_health_requires_cdp_and_a_live_harness_cdp_call(self):
        runtime = execution_runtime_health(
            "http://127.0.0.1:9222",
            opener=_ready_cdp,
            admin_module=_Admin(),
            ipc_module=_Ipc(),
        )

        self.assertTrue(runtime["ready"])
        self.assertTrue(runtime["cdp"]["ready"])
        self.assertTrue(runtime["harness"]["ready"])

    def test_ensure_retries_the_windows_endpoint_cleanup_race_once(self):
        admin = _Admin([PermissionError(5, "access denied"), None])
        ipc = _Ipc()
        sleeps = []
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = ensure_execution_runtime(
                "http://127.0.0.1:9222",
                Path(temp_dir) / "workspace",
                opener=_ready_cdp,
                admin_module=admin,
                ipc_module=ipc,
                sleep=sleeps.append,
            )

        self.assertTrue(runtime["ready"])
        self.assertEqual(len(admin.ensure_calls), 2)
        self.assertEqual(sleeps, [0.5])
        self.assertTrue(ipc.connection.closed)


if __name__ == "__main__":
    unittest.main()
