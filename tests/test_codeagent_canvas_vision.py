from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from kt6_backend.codeagent_canvas_vision import (
    CodeAgentCanvasVisionAdapter,
    CodeAgentProcessResult,
    CodeAgentVisionResponseError,
    CodeAgentVisionTransportError,
    SubprocessCodeAgentRunner,
)
from kt6_backend.page_perception import PagePerceptionService, SQLitePageCaptureStore
from kt6_backend.perception_runtime import PerceptionRuntime
from kt6_backend.topology_vision_contract import (
    CanvasVisionResponseError,
    RESPONSE_SCHEMA_VERSION,
)
from kt6_backend.vision_recognition import CanvasFrame


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2ZQAAAABJRU5ErkJggg=="
)


class StubRunner:
    def __init__(self, factory):
        self.factory = factory
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.factory(kwargs)


def request_from_call(call: dict) -> dict:
    prompt = call["stdin"].decode("utf-8")
    _, request_text = prompt.split("\n", 1)
    return json.loads(request_text)


def response_payload(canvas_id: str = "topology-canvas") -> dict:
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "confidence": 0.96,
        "objects": [
            {
                "business_id": "GW-001",
                "type": "gateway",
                "label": "GW-001",
                "canvas_id": canvas_id,
                "bbox": [0, 0, 1, 1],
                "confidence": 0.98,
                "attributes": {"model": "S628X-PWR-F"},
            }
        ],
        "links": [],
        "co_channel_relations": [],
    }


def json_events(*events: dict) -> bytes:
    return (
        "\n".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            for event in events
        )
        + "\n"
    ).encode("utf-8")


def successful_events(call: dict, *, payload: dict | None = None) -> bytes:
    request = request_from_call(call)
    frame_path = request["frames"][0]["local_path"]
    return json_events(
        {
            "type": "tool_use",
            "part": {
                "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {"filePath": frame_path},
                },
            },
        },
        {
            "type": "text",
            "part": {
                "text": json.dumps(
                    payload or response_payload(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            },
        },
        {"type": "step_finish", "part": {}},
    )


def successful_stream_events(call: dict, *, payload: dict | None = None) -> bytes:
    request = request_from_call(call)
    frame_path = request["frames"][0]["local_path"]
    response_text = json.dumps(
        payload or response_payload(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return json_events(
        {
            "type": "system",
            "subtype": "init",
            "tools": ["Read"],
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-read-1",
                        "name": "Read",
                        "input": {"file_path": frame_path},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-read-1",
                        "content": "image read successfully",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": response_text}],
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": response_text,
        },
    )


class CodeAgentCanvasVisionAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.image_path = self.root / "original-canvas.png"
        self.image_path.write_bytes(ONE_PIXEL_PNG)

    def tearDown(self):
        self.temp_dir.cleanup()

    def frame(self, **overrides) -> CanvasFrame:
        values = {
            "canvas_id": "topology-canvas",
            "screenshot_path": self.image_path,
            "screenshot_sha256": hashlib.sha256(ONE_PIXEL_PNG).hexdigest(),
            "mime_type": "image/png",
            "width": 1,
            "height": 1,
            "client_width": 1.0,
            "client_height": 1.0,
            "bbox": (0.0, 0.0, 1.0, 1.0),
        }
        values.update(overrides)
        return CanvasFrame(**values)

    @staticmethod
    def page() -> dict:
        return {
            "url": "kt6://image-test/codeagent-v1",
            "title": "enterprise-v1",
            "language": "zh-CN",
            "ui_version": "topology-image-cli-v1",
            "viewport": {"width": 1, "height": 1, "device_pixel_ratio": 1},
        }

    def adapter(self, runner: StubRunner) -> CodeAgentCanvasVisionAdapter:
        return CodeAgentCanvasVisionAdapter(
            workdir=self.root,
            executable=sys.executable,
            agent="kt6-topology-vision",
            timeout_seconds=2,
            runner=runner,
        )

    def test_reads_verified_staged_pixels_and_returns_strict_topology(self):
        staged: dict[str, object] = {}

        def factory(call: dict) -> CodeAgentProcessResult:
            request = request_from_call(call)
            frame = request["frames"][0]
            staged_path = Path(frame["local_path"])
            staged["path"] = staged_path
            staged["bytes"] = staged_path.read_bytes()
            self.assertNotEqual(staged_path, self.image_path)
            self.assertEqual(frame["screenshot_sha256"], hashlib.sha256(ONE_PIXEL_PNG).hexdigest())
            return CodeAgentProcessResult(0, successful_events(call), b"")

        runner = StubRunner(factory)
        adapter = self.adapter(runner)

        result = adapter.recognize(page=self.page(), frames=(self.frame(),))

        self.assertEqual(result["objects"][0]["business_id"], "GW-001")
        self.assertEqual(result["confidence"], 0.96)
        self.assertFalse(adapter.supports_actionable_grounding)
        self.assertEqual(staged["bytes"], ONE_PIXEL_PNG)
        self.assertFalse(Path(staged["path"]).exists())
        call = runner.calls[0]
        staged_dir = str(Path(request_from_call(call)["frames"][0]["local_path"]).parent)
        self.assertEqual(
            call["args"],
            (
                "-p",
                "--output-format",
                "stream-json",
                "--input-format",
                "text",
                "--verbose",
                "--agent",
                "kt6-topology-vision",
                "--tools",
                "Read",
                "--allowedTools",
                "Read",
                "--permission-mode",
                "dontAsk",
                "--no-session-persistence",
                "--disable-slash-commands",
                "--add-dir",
                staged_dir,
            ),
        )
        self.assertEqual(call["cwd"], self.root)
        self.assertNotIn(str(self.image_path), call["stdin"].decode("utf-8"))
        self.assertNotIn("Authorization", call["stdin"].decode("utf-8"))

    def test_accepts_codeagent_cli_stream_json_events(self):
        runner = StubRunner(
            lambda call: CodeAgentProcessResult(0, successful_stream_events(call), b"")
        )

        result = self.adapter(runner).recognize(
            page=self.page(), frames=(self.frame(),)
        )

        self.assertEqual(result["objects"][0]["business_id"], "GW-001")

    def test_hybrid_context_is_bounded_and_sent_as_untrusted_cv_evidence(self):
        captured = {}

        def factory(call: dict) -> CodeAgentProcessResult:
            captured["request"] = request_from_call(call)
            return CodeAgentProcessResult(0, successful_stream_events(call), b"")

        cv_observations = {
            "objects": [
                {
                    "business_id": "GW-001",
                    "type": "gateway",
                    "label": "GW-001",
                    "canvas_id": "topology-canvas",
                    "bbox": [0, 0, 1, 1],
                    "confidence": 0.98,
                    "attributes": {
                        "ocr_text": "GW-001",
                        "pixel_verified": True,
                    },
                }
            ],
            "links": [],
        }

        result = self.adapter(StubRunner(factory)).recognize_with_context(
            page=self.page(),
            frames=(self.frame(),),
            cv_observations=cv_observations,
        )

        self.assertEqual(result["objects"][0]["business_id"], "GW-001")
        request = captured["request"]
        self.assertEqual(
            request["cv_observations"]["objects"][0]["business_id"], "GW-001"
        )
        self.assertNotIn(
            "pixel_verified",
            request["cv_observations"]["objects"][0]["attributes"],
        )
        self.assertIn("not ground truth", request["requirements"]["cv_observations"])

    def test_accepts_extended_topology_structure_and_negative_evidence(self):
        payload = response_payload()
        payload["objects"].append(
            {
                "business_id": "CORE-001",
                "type": "core_switch",
                "label": "CORE-001",
                "canvas_id": "topology-canvas",
                "bbox": [0, 0, 1, 1],
                "confidence": 0.91,
                "attributes": {},
            }
        )
        payload["negative_edges"] = [
            {
                "source": "GW-001",
                "target": "CORE-001",
                "reason": "visible gap",
                "confidence": 0.9,
            }
        ]
        payload["structure_templates"] = [
            {
                "template_id": "core-layer",
                "type": "layered",
                "layers": [
                    {"name": "核心层", "members": ["GW-001", "CORE-001"]}
                ],
            }
        ]
        payload["no_connections"] = False
        runner = StubRunner(
            lambda call: CodeAgentProcessResult(
                0, successful_stream_events(call, payload=payload), b""
            )
        )

        result = self.adapter(runner).recognize(
            page=self.page(), frames=(self.frame(),)
        )

        self.assertEqual(result["negative_edges"][0]["reason"], "visible gap")
        self.assertEqual(result["structure_templates"][0]["type"], "layered")
        self.assertFalse(result["no_connections"])

    def test_uses_configured_default_agent_when_no_override_is_supplied(self):
        runner = StubRunner(
            lambda call: CodeAgentProcessResult(0, successful_stream_events(call), b"")
        )
        adapter = CodeAgentCanvasVisionAdapter(
            workdir=self.root,
            executable=sys.executable,
            agent=None,
            timeout_seconds=2,
            runner=runner,
        )

        adapter.recognize(page=self.page(), frames=(self.frame(),))

        self.assertNotIn("--agent", runner.calls[0]["args"])

    def test_stream_json_rejects_non_read_tool_and_failed_read(self):
        def events(call: dict, *, tool_name: str, failed: bool) -> bytes:
            path = request_from_call(call)["frames"][0]["local_path"]
            return json_events(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": tool_name,
                                "input": {"file_path": path},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-1",
                                "is_error": failed,
                            }
                        ]
                    },
                },
                {"type": "result", "subtype": "success", "is_error": False},
            )

        with self.assertRaisesRegex(CodeAgentVisionResponseError, "other than read"):
            self.adapter(
                StubRunner(
                    lambda call: CodeAgentProcessResult(
                        0, events(call, tool_name="Bash", failed=False), b""
                    )
                )
            ).recognize(page=self.page(), frames=(self.frame(),))

        with self.assertRaisesRegex(CodeAgentVisionResponseError, "did not complete"):
            self.adapter(
                StubRunner(
                    lambda call: CodeAgentProcessResult(
                        0, events(call, tool_name="Read", failed=True), b""
                    )
                )
            ).recognize(page=self.page(), frames=(self.frame(),))

    def test_page_perception_accepts_only_proven_codeagent_pixel_result(self):
        runner = StubRunner(
            lambda call: CodeAgentProcessResult(0, successful_events(call), b"")
        )
        adapter = self.adapter(runner)
        store = SQLitePageCaptureStore(
            self.root / "captures.sqlite3",
            self.root / "page_captures",
        )
        service = PagePerceptionService(
            store,
            PerceptionRuntime(),
            canvas_vision=adapter,
        )
        payload = {
            "page": self.page(),
            "dom": {"elements": []},
            "canvases": [
                {
                    "canvas_id": "topology-canvas",
                    "width": 1,
                    "height": 1,
                    "client_width": 1,
                    "client_height": 1,
                    "bbox": [0, 0, 1, 1],
                    "data_url": "data:image/png;base64,"
                    + base64.b64encode(ONE_PIXEL_PNG).decode("ascii"),
                }
            ],
            "adapter_scene": None,
        }

        capture = service.ingest(payload)

        self.assertEqual(capture["summary"]["selected_mode"], "canvas_vision_adapter")
        self.assertEqual(capture["summary"]["semantic_source"], "canvas_pixels")
        self.assertEqual(capture["scene"]["object_count"], 1)
        self.assertTrue(capture["scene"]["pixel_inference_performed"])
        self.assertTrue(capture["scene"]["pixel_verified"])
        self.assertFalse(capture["scene"]["actionable_grounding"])
        self.assertEqual(
            capture["scene"]["provenance"]["adapter_id"],
            "codeagent-read-tool-vision",
        )
        self.assertIn("GW-001", capture["scene"]["semantic_tree"]["nodes"])
        self.assertEqual(capture["scene"]["semantic_tree"]["orphans"], ["GW-001"])

    def test_requires_completed_read_event_for_every_frame(self):
        def factory(call: dict) -> CodeAgentProcessResult:
            stdout = json_events(
                {
                    "type": "text",
                    "part": {"text": json.dumps(response_payload())},
                },
                {"type": "step_finish", "part": {}},
            )
            return CodeAgentProcessResult(0, stdout, b"")

        with self.assertRaisesRegex(CodeAgentVisionResponseError, "completed read"):
            self.adapter(StubRunner(factory)).recognize(
                page=self.page(), frames=(self.frame(),)
            )

    def test_rejects_unexpected_or_non_read_tool_use(self):
        def unexpected_path(call: dict) -> CodeAgentProcessResult:
            return CodeAgentProcessResult(
                0,
                json_events(
                    {
                        "type": "tool_use",
                        "part": {
                            "tool": "read",
                            "state": {
                                "status": "completed",
                                "input": {"filePath": str(self.image_path)},
                            },
                        },
                    },
                    {"type": "step_finish", "part": {}},
                ),
                b"",
            )

        with self.assertRaisesRegex(CodeAgentVisionResponseError, "unexpected file"):
            self.adapter(StubRunner(unexpected_path)).recognize(
                page=self.page(), frames=(self.frame(),)
            )

        def shell_tool(call: dict) -> CodeAgentProcessResult:
            return CodeAgentProcessResult(
                0,
                json_events(
                    {
                        "type": "tool_use",
                        "part": {
                            "tool": "bash",
                            "state": {"status": "completed", "input": {}},
                        },
                    },
                    {"type": "step_finish", "part": {}},
                ),
                b"",
            )

        with self.assertRaisesRegex(CodeAgentVisionResponseError, "other than read"):
            self.adapter(StubRunner(shell_tool)).recognize(
                page=self.page(), frames=(self.frame(),)
            )

    def test_rejects_failed_read_and_text_emitted_before_read(self):
        def failed_read(call: dict) -> CodeAgentProcessResult:
            path = request_from_call(call)["frames"][0]["local_path"]
            return CodeAgentProcessResult(
                0,
                json_events(
                    {
                        "type": "tool_use",
                        "part": {
                            "tool": "read",
                            "state": {
                                "status": "error",
                                "input": {"filePath": path},
                            },
                        },
                    },
                    {"type": "step_finish", "part": {}},
                ),
                b"",
            )

        with self.assertRaisesRegex(CodeAgentVisionResponseError, "did not complete"):
            self.adapter(StubRunner(failed_read)).recognize(
                page=self.page(), frames=(self.frame(),)
            )

        def early_text(call: dict) -> CodeAgentProcessResult:
            request = request_from_call(call)
            path = request["frames"][0]["local_path"]
            return CodeAgentProcessResult(
                0,
                json_events(
                    {"type": "text", "part": {"text": json.dumps(response_payload())}},
                    {
                        "type": "tool_use",
                        "part": {
                            "tool": "read",
                            "state": {
                                "status": "completed",
                                "input": {"filePath": path},
                            },
                        },
                    },
                    {"type": "step_finish", "part": {}},
                ),
                b"",
            )

        with self.assertRaisesRegex(CodeAgentVisionResponseError, "no final JSON text"):
            self.adapter(StubRunner(early_text)).recognize(
                page=self.page(), frames=(self.frame(),)
            )

    def test_rejects_non_json_events_session_errors_and_unfinished_steps(self):
        cases = (
            (b"not-json\n", "strict JSON events"),
            (json_events({"type": "error"}), "session error"),
            (json_events({"type": "step_start", "part": {}}), "did not finish"),
        )
        for stdout, message in cases:
            with self.subTest(message=message):
                runner = StubRunner(lambda call, output=stdout: CodeAgentProcessResult(0, output, b""))
                with self.assertRaisesRegex(CodeAgentVisionResponseError, message):
                    self.adapter(runner).recognize(page=self.page(), frames=(self.frame(),))

    def test_final_model_text_must_match_shared_strict_contract(self):
        def fenced(call: dict) -> CodeAgentProcessResult:
            payload = "```json\n" + json.dumps(response_payload()) + "\n```"
            request = request_from_call(call)
            path = request["frames"][0]["local_path"]
            return CodeAgentProcessResult(
                0,
                json_events(
                    {
                        "type": "tool_use",
                        "part": {
                            "tool": "read",
                            "state": {
                                "status": "completed",
                                "input": {"filePath": path},
                            },
                        },
                    },
                    {"type": "text", "part": {"text": payload}},
                    {"type": "step_finish", "part": {}},
                ),
                b"",
            )

        with self.assertRaises(CanvasVisionResponseError):
            self.adapter(StubRunner(fenced)).recognize(
                page=self.page(), frames=(self.frame(),)
            )

        invalid = response_payload()
        invalid["objects"][0]["bbox"] = [0, 0, 2, 2]
        with self.assertRaisesRegex(CanvasVisionResponseError, "outside"):
            self.adapter(
                StubRunner(
                    lambda call: CodeAgentProcessResult(
                        0, successful_events(call, payload=invalid), b""
                    )
                )
            ).recognize(page=self.page(), frames=(self.frame(),))

    def test_nonzero_exit_is_transport_failure_and_stderr_is_not_exposed(self):
        secret = b"provider-secret-must-not-leak"
        runner = StubRunner(lambda call: CodeAgentProcessResult(17, b"", secret))
        with self.assertRaisesRegex(CodeAgentVisionTransportError, "status 17") as raised:
            self.adapter(runner).recognize(page=self.page(), frames=(self.frame(),))
        self.assertNotIn(secret.decode(), str(raised.exception))

    def test_constructor_rejects_invalid_process_configuration(self):
        with self.assertRaisesRegex(ValueError, "existing directory"):
            CodeAgentCanvasVisionAdapter(
                workdir=self.root / "missing", executable=sys.executable
            )
        with self.assertRaisesRegex(ValueError, "agent name"):
            CodeAgentCanvasVisionAdapter(
                workdir=self.root,
                executable=sys.executable,
                agent="bad agent; rm",
            )
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            CodeAgentCanvasVisionAdapter(
                workdir=self.root,
                executable=sys.executable,
                timeout_seconds=0,
            )
        with self.assertRaisesRegex(ValueError, "not found"):
            CodeAgentCanvasVisionAdapter(
                workdir=self.root,
                executable="definitely-missing-codeagent-executable",
            )


class SubprocessCodeAgentRunnerTest(unittest.TestCase):
    def test_prompt_is_sent_over_stdin_without_shell(self):
        prompt = b'{"request":"pixels"}'
        runner = SubprocessCodeAgentRunner()
        result = runner.run(
            executable=Path(sys.executable),
            args=(
                "-c",
                "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)",
            ),
            stdin=prompt,
            cwd=Path.cwd(),
            timeout_seconds=5,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, prompt)
        self.assertEqual(result.stderr, b"")

    def test_timeout_terminates_the_process(self):
        runner = SubprocessCodeAgentRunner()
        with self.assertRaisesRegex(CodeAgentVisionTransportError, "timed out"):
            runner.run(
                executable=Path(sys.executable),
                args=("-c", "import time; time.sleep(5)"),
                stdin=b"",
                cwd=Path.cwd(),
                timeout_seconds=0.05,
                max_stdout_bytes=1024,
                max_stderr_bytes=1024,
            )

    def test_stdout_is_streamed_to_sink_and_reports_progress(self):
        sink = BytesIO()
        progress = []
        runner = SubprocessCodeAgentRunner(
            stdout_sink=sink,
            progress_callback=progress.append,
            heartbeat_seconds=0.01,
        )
        result = runner.run(
            executable=Path(sys.executable),
            args=(
                "-c",
                "import json,sys,time; "
                "print(json.dumps({'type':'system','subtype':'init'}), flush=True); "
                "sys.stdout.flush(); time.sleep(.05)",
            ),
            stdin=b"",
            cwd=Path.cwd(),
            timeout_seconds=5,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )
        self.assertEqual(sink.getvalue(), result.stdout)
        self.assertTrue(progress)
        self.assertTrue(any(item.idle_seconds is not None for item in progress))
        self.assertTrue(
            any(item.last_event == "system/init" for item in progress)
        )
        self.assertTrue(any(item.stdout_bytes > 0 for item in progress))

    def test_timeout_reports_last_event_and_preserves_streamed_bytes(self):
        sink = BytesIO()
        runner = SubprocessCodeAgentRunner(stdout_sink=sink)
        with self.assertRaisesRegex(
            CodeAgentVisionTransportError,
            "last event: assistant/tool_use:Read",
        ):
            runner.run(
                executable=Path(sys.executable),
                args=(
                    "-c",
                    "import json,time; "
                    "print(json.dumps({'type':'assistant','message':{'content':["
                    "{'type':'tool_use','name':'Read'}]}}), flush=True); "
                    "time.sleep(5)",
                ),
                stdin=b"",
                cwd=Path.cwd(),
                timeout_seconds=0.1,
                max_stdout_bytes=4096,
                max_stderr_bytes=1024,
            )
        self.assertIn(b'"name": "Read"', sink.getvalue())

    def test_success_event_ends_process_after_grace_period(self):
        runner = SubprocessCodeAgentRunner(terminal_grace_seconds=0.05)
        started = time.monotonic()
        result = runner.run(
            executable=Path(sys.executable),
            args=(
                "-c",
                "import json,time; "
                "print(json.dumps({'type':'result','subtype':'success',"
                "'is_error':False,'result':'ok'}), flush=True); "
                "time.sleep(5)",
            ),
            stdin=b"",
            cwd=Path.cwd(),
            timeout_seconds=2,
            max_stdout_bytes=4096,
            max_stderr_bytes=1024,
        )
        self.assertEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn(b'"subtype": "success"', result.stdout)

    def test_progress_intervals_must_be_finite(self):
        with self.assertRaisesRegex(ValueError, "heartbeat_seconds"):
            SubprocessCodeAgentRunner(heartbeat_seconds=0)
        with self.assertRaisesRegex(ValueError, "terminal_grace_seconds"):
            SubprocessCodeAgentRunner(terminal_grace_seconds=-1)


if __name__ == "__main__":
    unittest.main()
