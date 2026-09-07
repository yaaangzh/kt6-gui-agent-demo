from __future__ import annotations

import base64
import copy
import hashlib
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any

from kt6_backend.evaluation_artifacts import (
    EvaluationArtifactError,
    archive_run_evidence,
    verify_run_evidence,
)
from kt6_backend.topology_fusion import fuse_topology_payloads
from kt6_backend.topology_model_contract import MODEL_SCHEMA_VERSION
from kt6_backend.topology_vision_contract import RESPONSE_SCHEMA_VERSION
from kt6_backend.ui_graph import build_ui_graph


_ACTION_SCHEMA = "kt6.evaluation-action-event.v1"
_PLANNER_SCHEMA = "kt6.evaluation-planner-call.v1"
_UI_TARS_SCHEMA = "kt6.evaluation-ui-tars-response.v1"
_VALIDATION_SCHEMA = "kt6.evaluation-validation-result.v1"
_VISION_MODEL_CALL_SCHEMA = "kt6.evaluation-vision-model-call.v1"
_CV_METADATA_SCHEMA = "kt6.cv-artifact-metadata.v1"
_ROUTING_SCHEMA = "kt6.topology-routing.v1"
_BROWSER_USE_DOM_SCHEMA = "kt6.browser-use-dom-snapshot.v1"
_CDP_SNAPSHOT_SCHEMA = "kt6.cdp-page-snapshot.v1"

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class EvaluationArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_root = Path(__file__).resolve().parents[1] / ".test-tmp"
        cls._temporary_root.mkdir(parents=True, exist_ok=True)

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="evaluation-artifacts-",
            dir=self._temporary_root,
        )
        self.workspace = Path(self._temporary_directory.name)
        self.evaluation_root = self.workspace / "evaluation"
        self.sources = self.workspace / "sources"
        self.sources.mkdir()
        self.suite = self._suite()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_archives_complete_current_dom_bundle_and_preserves_sources(self):
        run = self._run("current", "T_DOM", repetition=1)
        run["metrics"]["planner_model_calls"] = 1
        graph = self._ui_graph("dom-main")
        artifact_sources = self._common_sources(run)
        artifact_sources.extend(
            [
                ("ui_graph", self._write_json("ui-graph.json", graph)),
                (
                    "planner_result",
                    self._write_jsonl(
                        "planner-result.jsonl",
                        self._planner_call(run, 1, [graph["graph_id"]]),
                    ),
                ),
            ]
        )
        original_bytes = {path: path.read_bytes() for _, path in artifact_sources}

        archived_run, returned_manifest = archive_run_evidence(
            self.evaluation_root, self.suite, run, artifact_sources
        )

        evidence = archived_run["evidence"]
        manifest_path = self.evaluation_root / evidence["manifest_ref"]
        self.assertEqual(
            self.evaluation_root
            / "artifacts"
            / "current"
            / "T_DOM"
            / "r001"
            / "manifest.json",
            manifest_path,
        )
        manifest_bytes = manifest_path.read_bytes()
        self.assertEqual(
            hashlib.sha256(manifest_bytes).hexdigest(), evidence["manifest_sha256"]
        )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        self.assertEqual(returned_manifest, manifest)
        self.assertTrue(manifest["complete"])
        self.assertTrue(manifest["storage_policy"]["contains_sensitive_page_data"])
        self.assertFalse(manifest["storage_policy"]["git_allowed"])
        self.assertEqual(
            {"action_trace", "planner_result", "validation_result", "ui_graph"},
            set(manifest["present_roles"]),
        )
        self.assertEqual(5, manifest["artifact_count"])
        for artifact in manifest["artifacts"]:
            archived_path = manifest_path.parent / artifact["path"]
            self.assertEqual(
                hashlib.sha256(archived_path.read_bytes()).hexdigest(),
                artifact["sha256"],
            )
            self.assertEqual(archived_path.stat().st_size, artifact["size_bytes"])
        for source, expected in original_bytes.items():
            self.assertTrue(source.is_file())
            self.assertEqual(expected, source.read_bytes())

        verification = verify_run_evidence(
            self.evaluation_root, self.suite, archived_run
        )
        self.assertTrue(verification["verified"], verification["errors"])
        self.assertEqual(5, verification["artifact_count"])

    def test_missing_required_role_fails_without_final_directory(self):
        run = self._run("current", "T_DOM", repetition=1)
        final_dir = self._final_dir(run)

        with self.assertRaisesRegex(EvaluationArtifactError, "ui_graph"):
            archive_run_evidence(
                self.evaluation_root, self.suite, run, self._common_sources(run)
            )

        self.assertFalse(final_dir.exists())

    def test_existing_target_directory_is_never_overwritten(self):
        run = self._run("current", "T_DOM", repetition=1)
        artifact_sources = self._dom_sources(run, "existing")
        archived_run, _ = archive_run_evidence(
            self.evaluation_root, self.suite, run, artifact_sources
        )
        manifest_path = self.evaluation_root / archived_run["evidence"]["manifest_ref"]
        original_manifest = manifest_path.read_bytes()

        with self.assertRaisesRegex(EvaluationArtifactError, "refusing to overwrite"):
            archive_run_evidence(
                self.evaluation_root, self.suite, run, artifact_sources
            )

        self.assertEqual(original_manifest, manifest_path.read_bytes())

    def test_tampered_archived_file_fails_hash_verification(self):
        run = self._run("current", "T_DOM", repetition=1)
        archived_run, manifest = archive_run_evidence(
            self.evaluation_root, self.suite, run, self._dom_sources(run, "tamper")
        )
        manifest_path = self.evaluation_root / archived_run["evidence"]["manifest_ref"]
        graph_entry = next(
            item for item in manifest["artifacts"] if item["role"] == "ui_graph"
        )
        graph_path = manifest_path.parent / graph_entry["path"]
        graph_path.write_bytes(graph_path.read_bytes() + b"\n")

        verification = verify_run_evidence(
            self.evaluation_root, self.suite, archived_run
        )

        self.assertFalse(verification["verified"])
        self.assertTrue(
            any("SHA-256" in error for error in verification["errors"]),
            verification["errors"],
        )

    def test_validation_result_identity_mismatch_is_rejected(self):
        run = self._run("current", "T_DOM", repetition=1)
        sources = self._dom_sources(run, "validation-mismatch")
        validation_index = next(
            index for index, (role, _) in enumerate(sources)
            if role == "validation_result"
        )
        sources[validation_index] = (
            "validation_result",
            self._write_json(
                "validation-mismatch.json",
                {
                    "schema_version": _VALIDATION_SCHEMA,
                    "evidence_id": "validation_result-001",
                    "run_id": "another-run",
                    "task_id": run["task_id"],
                    "method": run["validation"]["method"],
                    "passed": run["validation"]["passed"],
                },
            ),
        )

        with self.assertRaisesRegex(EvaluationArtifactError, "run_id"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

        self.assertFalse(self._final_dir(run).exists())

    def test_dangerous_identifiers_and_manifest_references_are_rejected(self):
        run = self._run("current", "T_DOM", repetition=1)
        artifact_sources = self._dom_sources(run, "dangerous")
        for field, dangerous_value in (
            ("scheme_id", "../escape"),
            ("task_id", "T_DOM/../../escape"),
        ):
            with self.subTest(field=field):
                dangerous_run = dict(run)
                dangerous_run[field] = dangerous_value
                with self.assertRaisesRegex(EvaluationArtifactError, "safe ASCII slug"):
                    archive_run_evidence(
                        self.evaluation_root,
                        self.suite,
                        dangerous_run,
                        artifact_sources,
                    )

        indexed_run = dict(run)
        indexed_run["evidence"] = {
            "status": "archived",
            "manifest_ref": "../outside/manifest.json",
            "manifest_sha256": "0" * 64,
            "artifact_count": 1,
            "total_bytes": 1,
            "archive_duration_ms": 0,
            "complete": True,
        }
        verification = verify_run_evidence(
            self.evaluation_root, self.suite, indexed_run
        )
        self.assertFalse(verification["verified"])
        self.assertTrue(
            any("canonical relative path" in error for error in verification["errors"]),
            verification["errors"],
        )

    def test_empty_shell_json_cannot_masquerade_as_ui_graph(self):
        run = self._run("current", "T_DOM", repetition=1)
        sources = self._common_sources(run)
        sources.append(("ui_graph", self._write_json("empty-ui-graph.json", {})))

        with self.assertRaisesRegex(EvaluationArtifactError, "ui_graph.schema_version"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

        self.assertFalse(self._final_dir(run).exists())

    def test_ui_graph_execution_flags_cannot_be_reauthorized_by_rehashing(self):
        run = self._run("current", "T_DOM", repetition=1)
        graph = copy.deepcopy(self._ui_graph("unsafe-node"))
        graph["nodes"][0]["can_click_now"] = True
        graph["nodes"][0]["safe_for_execution"] = True
        graph_core = {
            "capture_id": graph["capture_id"],
            "nodes": graph["nodes"],
            "edges": graph["edges"],
            "issues": graph["issues"],
        }
        graph["graph_id"] = "uig:" + hashlib.sha256(
            json.dumps(
                graph_core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()[:24]
        sources = self._common_sources(run)
        sources.append(("ui_graph", self._write_json("unsafe-ui-graph.json", graph)))

        with self.assertRaisesRegex(EvaluationArtifactError, "non-actionable"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

        self.assertFalse(self._final_dir(run).exists())

    def test_action_trace_must_cover_every_reported_step(self):
        run = self._run("current", "T_DOM", repetition=1)
        run["step_count"] = 2
        sources = [
            (
                "action_trace",
                self._write_jsonl(
                    "incomplete-action-trace.jsonl", self._action(run, 1)
                ),
            ),
            self._common_sources(run)[1],
            ("ui_graph", self._write_json("trace-ui-graph.json", self._ui_graph("trace"))),
        ]

        with self.assertRaisesRegex(EvaluationArtifactError, "does not cover every run step"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

    def test_planner_result_must_cover_calls_and_reference_bundle_inputs(self):
        run = self._run("current", "T_DOM", repetition=1)
        run["metrics"]["planner_model_calls"] = 2
        graph = self._ui_graph("planner")
        sources = self._common_sources(run)
        sources.extend(
            [
                ("ui_graph", self._write_json("planner-ui-graph.json", graph)),
                (
                    "planner_result",
                    self._write_jsonl(
                        "incomplete-planner.jsonl",
                        self._planner_call(run, 1, [graph["graph_id"]]),
                    ),
                ),
            ]
        )

        with self.assertRaisesRegex(
            EvaluationArtifactError, "does not cover every planner model call"
        ):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

    def test_browser_use_accepts_real_dom_or_cdp_snapshot_contract(self):
        for repetition, role in ((1, "dom_snapshot"), (2, "cdp_snapshot")):
            with self.subTest(role=role):
                run = self._run("browser_use", "T_BROWSER", repetition=repetition)
                payload = (
                    self._dom_snapshot(run)
                    if role == "dom_snapshot"
                    else self._cdp_snapshot()
                )
                sources = self._common_sources(run, stem=f"browser-{repetition}")
                sources.append(
                    (role, self._write_json(f"{role}-{repetition}.json", payload))
                )

                archived_run, manifest = archive_run_evidence(
                    self.evaluation_root, self.suite, run, sources
                )

                self.assertIn(role, manifest["present_roles"])
                verification = verify_run_evidence(
                    self.evaluation_root, self.suite, archived_run
                )
                self.assertTrue(verification["verified"], verification["errors"])

    def test_current_canvas_archives_bound_cv_model_routing_fusion_and_graph(self):
        run = self._run("current", "T_CANVAS", repetition=1)
        run["metrics"].update({"cv_calls": 1, "vision_model_calls": 1})
        sources = self._canvas_sources(run, "canvas-complete")

        archived_run, manifest = archive_run_evidence(
            self.evaluation_root, self.suite, run, sources
        )

        self.assertTrue(
            {
                "original_screenshot",
                "cv_result",
                "cv_metadata",
                "model_result",
                "vision_model_call",
                "routing_result",
                "fused_result",
                "ui_graph",
            }.issubset(set(manifest["present_roles"]))
        )
        verification = verify_run_evidence(
            self.evaluation_root, self.suite, archived_run
        )
        self.assertTrue(verification["verified"], verification["errors"])

    def test_cv_metadata_with_wrong_result_hash_is_rejected(self):
        run = self._run("current", "T_CANVAS", repetition=1)
        run["metrics"].update({"cv_calls": 1, "vision_model_calls": 1})
        sources = self._canvas_sources(run, "bad-cv-hash", bad_cv_hash=True)

        with self.assertRaisesRegex(EvaluationArtifactError, "does not bind cv_result"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

        self.assertFalse(self._final_dir(run).exists())

    def test_cv_metadata_dimensions_must_match_screenshot_bytes(self):
        run = self._run("current", "T_CANVAS", repetition=1)
        run["metrics"].update({"cv_calls": 1, "vision_model_calls": 1})
        sources = self._canvas_sources(run, "bad-image-size")
        metadata_path = next(path for role, path in sources if role == "cv_metadata")
        routing_path = next(path for role, path in sources if role == "routing_result")
        fused_path = next(path for role, path in sources if role == "fused_result")
        metadata = json.loads(metadata_path.read_text("utf-8"))
        routing = json.loads(routing_path.read_text("utf-8"))
        fused = json.loads(fused_path.read_text("utf-8"))
        for payload in (metadata["frames"][0], routing["source"], fused["routing"]["source"]):
            payload["width"] = 999
            payload["height"] = 777
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        routing_path.write_text(json.dumps(routing), encoding="utf-8")
        fused_path.write_text(json.dumps(fused), encoding="utf-8")

        with self.assertRaisesRegex(EvaluationArtifactError, "dimensions do not match"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

    def test_png_magic_without_decodable_image_body_is_rejected(self):
        run = self._run("current", "T_CANVAS", repetition=1)
        run["metrics"].update({"cv_calls": 1, "vision_model_calls": 1})
        sources = self._canvas_sources(
            run,
            "fake-png",
            # Complete PNG signature and IHDR with 1x1 dimensions, but no
            # required IDAT/IEND chunks: dimensions alone must not pass.
            screenshot_bytes=_ONE_PIXEL_PNG[:33],
        )

        with self.assertRaisesRegex(EvaluationArtifactError, "incomplete image body"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

        self.assertFalse(self._final_dir(run).exists())

    def test_png_rgba_scanline_shorter_than_declared_pixels_is_rejected(self):
        run = self._run("current", "T_CANVAS", repetition=1)
        run["metrics"].update({"cv_calls": 1, "vision_model_calls": 1})
        ihdr = (
            (1).to_bytes(4, "big")
            + (1).to_bytes(4, "big")
            + bytes([8, 6, 0, 0, 0])
        )
        compressed_scanline = zlib.compress(b"\x00")
        self.assertEqual(b"\x00", zlib.decompress(compressed_scanline))
        malformed_png = (
            b"\x89PNG\r\n\x1a\n"
            + self._png_chunk(b"IHDR", ihdr)
            + self._png_chunk(b"IDAT", compressed_scanline)
            + self._png_chunk(b"IEND", b"")
        )
        sources = self._canvas_sources(
            run,
            "short-rgba-scanline",
            screenshot_bytes=malformed_png,
        )

        with self.assertRaisesRegex(EvaluationArtifactError, "incomplete image body"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

        self.assertFalse(self._final_dir(run).exists())

    def test_png_non_zlib_idat_is_wrapped_and_not_published(self):
        run = self._run("current", "T_CANVAS", repetition=1)
        run["metrics"].update({"cv_calls": 1, "vision_model_calls": 1})
        ihdr = (
            (1).to_bytes(4, "big")
            + (1).to_bytes(4, "big")
            + bytes([8, 6, 0, 0, 0])
        )
        malformed_png = (
            b"\x89PNG\r\n\x1a\n"
            + self._png_chunk(b"IHDR", ihdr)
            + self._png_chunk(b"IDAT", b"not-zlib")
            + self._png_chunk(b"IEND", b"")
        )
        sources = self._canvas_sources(
            run,
            "non-zlib-idat",
            screenshot_bytes=malformed_png,
        )

        with self.assertRaisesRegex(EvaluationArtifactError, "incomplete image body"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

        self.assertFalse(self._final_dir(run).exists())

    def test_webp_vp8x_metadata_without_pixel_chunk_is_rejected(self):
        run = self._run("ui_tars", "T_TARS", repetition=1)
        run["metrics"]["vision_model_calls"] = 1
        vp8x_data = bytes(10)  # flags/reserved + (width-1) + (height-1) => 1x1
        vp8x_chunk = b"VP8X" + len(vp8x_data).to_bytes(4, "little") + vp8x_data
        riff_payload = b"WEBP" + vp8x_chunk
        metadata_only_webp = (
            b"RIFF" + len(riff_payload).to_bytes(4, "little") + riff_payload
        )
        screenshot = self.sources / "metadata-only.webp"
        screenshot.write_bytes(metadata_only_webp)
        sources = self._common_sources(run, stem="metadata-only-webp")
        sources.extend(
            [
                ("original_screenshot", screenshot),
                (
                    "ui_tars_response",
                    self._write_jsonl(
                        "metadata-only-webp-response.jsonl",
                        self._ui_tars_response(run, 1, self._sha256(screenshot)),
                    ),
                ),
            ]
        )

        with self.assertRaisesRegex(EvaluationArtifactError, "incomplete image body"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

        self.assertFalse(self._final_dir(run).exists())

    def test_failed_vision_model_calls_archive_timeout_and_invalid_response(self):
        for repetition, status, error_code, diagnostic_role in (
            (1, "timeout", "vision_timeout", None),
            (2, "invalid_response", "invalid_json", "model_stderr"),
        ):
            with self.subTest(status=status):
                run = self._run("current", "T_CANVAS", repetition=repetition)
                run["outcome"] = "failure"
                run["validation"]["passed"] = False
                run["metrics"].update({"cv_calls": 1, "vision_model_calls": 1})
                run["failure"] = {"category": error_code, "reason": status}
                sources = self._failed_canvas_sources(
                    run,
                    f"vision-{status}",
                    status=status,
                    error_code=error_code,
                    diagnostic_role=diagnostic_role,
                )

                archived_run, manifest = archive_run_evidence(
                    self.evaluation_root, self.suite, run, sources
                )

                self.assertIn("vision_model_call", manifest["present_roles"])
                self.assertNotIn("model_result", manifest["present_roles"])
                self.assertNotIn("fused_result", manifest["present_roles"])
                call_entry = next(
                    artifact
                    for artifact in manifest["artifacts"]
                    if artifact["role"] == "vision_model_call"
                )
                manifest_path = (
                    self.evaluation_root / archived_run["evidence"]["manifest_ref"]
                )
                archived_call = json.loads(
                    (manifest_path.parent / call_entry["path"])
                    .read_text(encoding="utf-8")
                    .strip()
                )
                self.assertEqual(status, archived_call["status"])
                self.assertEqual(error_code, archived_call["error_code"])
                verification = verify_run_evidence(
                    self.evaluation_root, self.suite, archived_run
                )
                self.assertTrue(verification["verified"], verification["errors"])

    def test_non_standard_json_constants_are_wrapped_and_not_published(self):
        run = self._run("current", "T_DOM", repetition=1)
        graph = self._ui_graph("nan")
        graph["stats"]["invalid_metric"] = float("nan")
        sources = self._common_sources(run, stem="nan-json")
        sources.append(("ui_graph", self._write_json("nan-ui-graph.json", graph)))

        with self.assertRaisesRegex(EvaluationArtifactError, "not valid UTF-8 JSON"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

        self.assertFalse(self._final_dir(run).exists())

    def test_ui_tars_archives_one_bound_screenshot_and_response_per_step(self):
        run = self._run("ui_tars", "T_TARS", repetition=1)
        run["step_count"] = 2
        run["metrics"]["vision_model_calls"] = 2
        first = self._write_png("ui-tars-before.png", extra=b"before")
        second = self._write_png("ui-tars-after.png", extra=b"after")
        first_hash = self._sha256(first)
        second_hash = self._sha256(second)
        sources = self._common_sources(run, stem="ui-tars")
        sources.extend(
            [
                ("original_screenshot", first),
                ("original_screenshot", second),
                (
                    "ui_tars_response",
                    self._write_jsonl(
                        "ui-tars-response.jsonl",
                        self._ui_tars_response(run, 1, first_hash),
                        self._ui_tars_response(run, 2, second_hash),
                    ),
                ),
            ]
        )

        archived_run, manifest = archive_run_evidence(
            self.evaluation_root, self.suite, run, sources
        )

        screenshots = [
            artifact
            for artifact in manifest["artifacts"]
            if artifact["role"] == "original_screenshot"
        ]
        self.assertEqual(
            ["original-screenshot-001.png", "original-screenshot-002.png"],
            [artifact["path"] for artifact in screenshots],
        )
        self.assertEqual(
            [first_hash, second_hash], [artifact["sha256"] for artifact in screenshots]
        )
        verification = verify_run_evidence(
            self.evaluation_root, self.suite, archived_run
        )
        self.assertTrue(verification["verified"], verification["errors"])

    def test_ui_tars_allows_identical_pixels_with_distinct_artifact_ids(self):
        run = self._run("ui_tars", "T_TARS", repetition=1)
        run["step_count"] = 2
        run["metrics"]["vision_model_calls"] = 2
        first = self._write_png("ui-tars-identical-first.png")
        second = self._write_png("ui-tars-identical-second.png")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        screenshot_hash = self._sha256(first)
        sources = self._common_sources(run, stem="ui-tars-identical")
        sources.extend(
            [
                ("original_screenshot", first),
                ("original_screenshot", second),
                (
                    "ui_tars_response",
                    self._write_jsonl(
                        "ui-tars-identical-response.jsonl",
                        self._ui_tars_response(
                            run,
                            1,
                            screenshot_hash,
                            screenshot_artifact_id="original-screenshot-001",
                        ),
                        self._ui_tars_response(
                            run,
                            2,
                            screenshot_hash,
                            screenshot_artifact_id="original-screenshot-002",
                        ),
                    ),
                ),
            ]
        )

        archived_run, manifest = archive_run_evidence(
            self.evaluation_root, self.suite, run, sources
        )

        screenshots = [
            artifact
            for artifact in manifest["artifacts"]
            if artifact["role"] == "original_screenshot"
        ]
        self.assertEqual(2, len(screenshots))
        self.assertEqual(1, len({artifact["sha256"] for artifact in screenshots}))
        verification = verify_run_evidence(
            self.evaluation_root, self.suite, archived_run
        )
        self.assertTrue(verification["verified"], verification["errors"])

    def test_ui_tars_response_with_unbundled_screenshot_hash_is_rejected(self):
        run = self._run("ui_tars", "T_TARS", repetition=1)
        run["metrics"]["vision_model_calls"] = 1
        screenshot = self._write_png("ui-tars-observation.png")
        sources = self._common_sources(run, stem="ui-tars-bad-hash")
        sources.extend(
            [
                ("original_screenshot", screenshot),
                (
                    "ui_tars_response",
                    self._write_jsonl(
                        "ui-tars-bad-hash.jsonl",
                        self._ui_tars_response(run, 1, "0" * 64),
                    ),
                ),
            ]
        )

        with self.assertRaisesRegex(EvaluationArtifactError, "screenshot"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

    def test_ui_tars_cannot_reuse_one_screenshot_for_two_distinct_steps(self):
        run = self._run("ui_tars", "T_TARS", repetition=1)
        run["step_count"] = 2
        run["metrics"]["vision_model_calls"] = 2
        first = self._write_png("ui-tars-reused-first.png", extra=b"first")
        second = self._write_png("ui-tars-unreferenced-second.png", extra=b"second")
        first_hash = self._sha256(first)
        sources = self._common_sources(run, stem="ui-tars-reused")
        sources.extend(
            [
                ("original_screenshot", first),
                ("original_screenshot", second),
                (
                    "ui_tars_response",
                    self._write_jsonl(
                        "ui-tars-reused-response.jsonl",
                        self._ui_tars_response(run, 1, first_hash),
                        self._ui_tars_response(
                            run,
                            2,
                            first_hash,
                            screenshot_artifact_id="original-screenshot-001",
                        ),
                    ),
                ),
            ]
        )

        with self.assertRaisesRegex(EvaluationArtifactError, "screenshot"):
            archive_run_evidence(self.evaluation_root, self.suite, run, sources)

        self.assertFalse(self._final_dir(run).exists())

    @staticmethod
    def _suite() -> dict[str, Any]:
        return {
            "suite_id": "suite-artifacts",
            "schemes": [
                {"scheme_id": "current", "evidence_profile": "current_hybrid"},
                {"scheme_id": "browser_use", "evidence_profile": "browser_use"},
                {"scheme_id": "ui_tars", "evidence_profile": "ui_tars"},
            ],
            "tasks": [
                {"task_id": "T_DOM", "scenario_type": "dom"},
                {"task_id": "T_CANVAS", "scenario_type": "canvas"},
                {"task_id": "T_BROWSER", "scenario_type": "dom"},
                {"task_id": "T_TARS", "scenario_type": "mixed"},
            ],
        }

    @staticmethod
    def _run(scheme_id: str, task_id: str, *, repetition: int) -> dict[str, Any]:
        run_id = f"{scheme_id}-{task_id}-r{repetition}"
        return {
            "run_id": run_id,
            "suite_id": "suite-artifacts",
            "scheme_id": scheme_id,
            "task_id": task_id,
            "repetition": repetition,
            "outcome": "success",
            "step_count": 1,
            "implementation": {
                "name": f"{scheme_id}-adapter",
                "version": "1.0.0",
                "revision": "abc1234",
                "branch": "test",
            },
            "planner": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "adapter_prompt_version": "v1",
            },
            "task_prompt_version": "v1",
            "environment": {
                "environment_id": "isolated-test",
                "browser": "chromium-test",
                "viewport": "1920x1080",
            },
            "metrics": {
                "cv_calls": 0,
                "planner_model_calls": 0,
                "vision_model_calls": 0,
                "safety_violation_count": 0,
            },
            "validation": {
                "method": "page_state",
                "passed": True,
                "evidence_ref": "validation_result-001",
            },
            "evidence": {"status": "pending"},
        }

    def _common_sources(
        self, run: dict[str, Any], *, stem: str = "run"
    ) -> list[tuple[str, Path]]:
        return [
            (
                "action_trace",
                self._write_jsonl(
                    f"{stem}-action-trace.jsonl",
                    *[
                        self._action(run, step_index)
                        for step_index in range(1, run["step_count"] + 1)
                    ],
                ),
            ),
            (
                "validation_result",
                self._write_json(
                    f"{stem}-validation-result.json",
                    {
                        "schema_version": _VALIDATION_SCHEMA,
                        "evidence_id": "validation_result-001",
                        "run_id": run["run_id"],
                        "task_id": run["task_id"],
                        "method": run["validation"]["method"],
                        "passed": run["validation"]["passed"],
                    },
                ),
            ),
        ]

    def _dom_sources(self, run: dict[str, Any], stem: str) -> list[tuple[str, Path]]:
        sources = self._common_sources(run, stem=stem)
        sources.append(
            (
                "ui_graph",
                self._write_json(f"{stem}-ui-graph.json", self._ui_graph(stem)),
            )
        )
        return sources

    def _canvas_sources(
        self,
        run: dict[str, Any],
        stem: str,
        *,
        bad_cv_hash: bool = False,
        screenshot_bytes: bytes | None = None,
    ) -> list[tuple[str, Path]]:
        screenshot = self._write_png(
            f"{stem}-original.png",
            content=screenshot_bytes,
        )
        screenshot_hash = self._sha256(screenshot)
        cv_payload = self._cv_payload()
        cv_path = self._write_json(f"{stem}-cv.json", cv_payload)
        cv_hash = "0" * 64 if bad_cv_hash else self._sha256(cv_path)
        metadata = {
            "schema_version": _CV_METADATA_SCHEMA,
            "source_id": f"{stem}-source",
            "adapter_id": "local-cv-ocr",
            "adapter_version": "1.5",
            "artifact_sha256": cv_hash,
            "frames": [
                {
                    "canvas_id": "canvas-main",
                    "sha256": screenshot_hash,
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                }
            ],
        }
        routing = self._routing_payload(metadata, screenshot_hash)
        model = self._model_payload()
        model_path = self._write_json(f"{stem}-model.json", model)
        routing_path = self._write_json(f"{stem}-routing.json", routing)
        fused = fuse_topology_payloads(cv_payload, model)
        fused["routing"] = copy.deepcopy(routing)
        vision_call = self._vision_model_call(
            run,
            status="success",
            screenshot_sha256=screenshot_hash,
            model_result_sha256=self._sha256(model_path),
        )
        sources = self._common_sources(run, stem=stem)
        sources.extend(
            [
                ("original_screenshot", screenshot),
                ("cv_result", cv_path),
                ("cv_metadata", self._write_json(f"{stem}-cv-metadata.json", metadata)),
                ("model_result", model_path),
                ("vision_model_call", self._write_jsonl(
                    f"{stem}-vision-model-call.jsonl", vision_call
                )),
                ("routing_result", routing_path),
                ("fused_result", self._write_json(f"{stem}-fused.json", fused)),
                (
                    "ui_graph",
                    self._write_json(f"{stem}-ui-graph.json", self._ui_graph(stem)),
                ),
            ]
        )
        return sources

    def _failed_canvas_sources(
        self,
        run: dict[str, Any],
        stem: str,
        *,
        status: str,
        error_code: str,
        diagnostic_role: str | None,
    ) -> list[tuple[str, Path]]:
        screenshot = self._write_png(f"{stem}-original.png")
        screenshot_hash = self._sha256(screenshot)
        cv_payload = self._cv_payload()
        cv_path = self._write_json(f"{stem}-cv.json", cv_payload)
        metadata = {
            "schema_version": _CV_METADATA_SCHEMA,
            "source_id": f"{stem}-source",
            "adapter_id": "local-cv-ocr",
            "adapter_version": "1.5",
            "artifact_sha256": self._sha256(cv_path),
            "frames": [
                {
                    "canvas_id": "canvas-main",
                    "sha256": screenshot_hash,
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                }
            ],
        }
        routing = self._routing_payload(metadata, screenshot_hash)
        routing.update(
            {
                "requirement_satisfied": False,
                "result_status": "incomplete",
                "connectivity_status": "incomplete",
                "reason_codes": [error_code],
                "missing_capabilities": ["semantic_model_result"],
                "execution_status": f"failed_{status}",
            }
        )
        diagnostic_ids: list[str] = []
        diagnostics: list[tuple[str, Path]] = []
        if diagnostic_role == "model_stderr":
            diagnostic_path = self.sources / f"{stem}-model-stderr.log"
            diagnostic_path.write_text(
                "provider returned a non-JSON response\n", encoding="utf-8"
            )
            diagnostics.append(("model_stderr", diagnostic_path))
            diagnostic_ids.append("model-stderr-001")
        vision_call = self._vision_model_call(
            run,
            status=status,
            screenshot_sha256=screenshot_hash,
            error_code=error_code,
            diagnostic_artifact_ids=diagnostic_ids,
        )
        sources = self._common_sources(run, stem=stem)
        sources.extend(
            [
                ("original_screenshot", screenshot),
                ("cv_result", cv_path),
                ("cv_metadata", self._write_json(f"{stem}-cv-metadata.json", metadata)),
                ("vision_model_call", self._write_jsonl(
                    f"{stem}-vision-model-call.jsonl", vision_call
                )),
                ("routing_result", self._write_json(f"{stem}-routing.json", routing)),
                ("ui_graph", self._write_json(
                    f"{stem}-ui-graph.json", self._ui_graph(stem)
                )),
                *diagnostics,
            ]
        )
        return sources

    @staticmethod
    def _action(run: dict[str, Any], step_index: int) -> dict[str, Any]:
        return {
            "schema_version": _ACTION_SCHEMA,
            "run_id": run["run_id"],
            "step_index": step_index,
            "action": "observe",
            "safety_violation": step_index <= int(
                run["metrics"]["safety_violation_count"]
            ),
            "elapsed_ms": 5,
        }

    @staticmethod
    def _planner_call(
        run: dict[str, Any], call_index: int, input_refs: list[str]
    ) -> dict[str, Any]:
        return {
            "schema_version": _PLANNER_SCHEMA,
            "run_id": run["run_id"],
            "call_index": call_index,
            "producer": {
                "provider": run["planner"]["provider"],
                "model": run["planner"]["model"],
            },
            "input_refs": input_refs,
            "response": {"steps": [{"op": "observe"}]},
        }

    @staticmethod
    def _ui_tars_response(
        run: dict[str, Any],
        step_index: int,
        screenshot_sha256: str,
        *,
        screenshot_artifact_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": _UI_TARS_SCHEMA,
            "run_id": run["run_id"],
            "step_index": step_index,
            "call_index": step_index,
            "producer": {"provider": "ui-tars", "model": "UI-TARS-1.5-7B"},
            "screenshot_artifact_id": (
                screenshot_artifact_id
                if screenshot_artifact_id is not None
                else f"original-screenshot-{step_index:03d}"
            ),
            "screenshot_sha256": screenshot_sha256,
            "response": {"action": "click", "coordinates": [1, 1]},
        }

    @staticmethod
    def _vision_model_call(
        run: dict[str, Any],
        *,
        status: str,
        screenshot_sha256: str,
        model_result_sha256: str | None = None,
        error_code: str | None = None,
        diagnostic_artifact_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": _VISION_MODEL_CALL_SCHEMA,
            "run_id": run["run_id"],
            "call_index": 1,
            "producer": {"provider": "model_api", "model": "vision-model"},
            "status": status,
            "duration_ms": 300_000 if status == "timeout" else 125,
            "screenshot_artifact_id": "original-screenshot-001",
            "screenshot_sha256": screenshot_sha256,
            "routing_artifact_id": "routing-result-001",
            "diagnostic_artifact_ids": diagnostic_artifact_ids or [],
        }
        if status == "success":
            record["model_result_artifact_id"] = "model-result-001"
            record["model_result_sha256"] = model_result_sha256
        else:
            record["error_code"] = error_code
        return record

    @staticmethod
    def _ui_graph(capture_suffix: str) -> dict[str, Any]:
        return build_ui_graph(
            {
                "capture_id": f"capture-{capture_suffix}",
                "capture": {
                    "page": {"url": "https://example.test", "title": "Test"},
                    "dom": {
                        "elements": [
                            {
                                "ref": "frame:0:#submit",
                                "selector": "#submit",
                                "role": "button",
                                "label": "Submit",
                                "bbox": [1, 2, 100, 30],
                                "frame_id": "0",
                                "document_id": "doc-main",
                                "document_order": 0,
                            }
                        ]
                    },
                },
            }
        )

    @staticmethod
    def _cv_payload() -> dict[str, Any]:
        return {
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "confidence": 0.93,
            "objects": [
                {
                    "business_id": "GW-001",
                    "type": "gateway",
                    "label": "GW-001",
                    "canvas_id": "canvas-main",
                    "bbox": [0, 0, 1, 1],
                    "confidence": 0.95,
                    "attributes": {},
                }
            ],
            "links": [],
            "co_channel_relations": [],
            "negative_edges": [],
            "structure_templates": [],
            "no_connections": True,
            "diagnostics": {"ocr_engine": "fixture"},
        }

    @staticmethod
    def _model_payload() -> dict[str, Any]:
        return {
            "schema_version": MODEL_SCHEMA_VERSION,
            "confidence": 0.91,
            "nodes": [
                {
                    "id": "GW-001",
                    "type": "gateway",
                    "role": "edge_gateway",
                    "confidence": 0.94,
                }
            ],
            "links": [],
            "structure_templates": [],
            "negative_edges": [],
            "no_connections": True,
        }

    @staticmethod
    def _routing_payload(
        metadata: dict[str, Any], screenshot_sha256: str
    ) -> dict[str, Any]:
        frame = metadata["frames"][0]
        return {
            "schema_version": _ROUTING_SCHEMA,
            "policy_version": "cv-route-v1",
            "decision": "model_assist",
            "scene_type": "structured_topology",
            "requested_profile": "auto",
            "effective_profile": "visible_topology",
            "requirement_satisfied": True,
            "result_status": "complete",
            "connectivity_status": "complete",
            "reason_codes": [],
            "satisfied_capabilities": ["objects"],
            "missing_capabilities": [],
            "model_invoked": True,
            "metrics": {},
            "source": {
                "source_id": metadata["source_id"],
                "sha256": screenshot_sha256,
                "mime_type": frame["mime_type"],
                "width": frame["width"],
                "height": frame["height"],
                "cv_adapter_id": metadata["adapter_id"],
                "cv_adapter_version": metadata["adapter_version"],
            },
            "execution_status": "completed_with_model",
        }

    @staticmethod
    def _dom_snapshot(run: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": _BROWSER_USE_DOM_SCHEMA,
            "run_id": run["run_id"],
            "task_id": run["task_id"],
            "safe_for_execution": False,
            "elements": [
                {
                    "ref": "frame:0:#query",
                    "role": "button",
                    "name": "Query",
                }
            ],
        }

    @staticmethod
    def _cdp_snapshot() -> dict[str, Any]:
        return {
            "schema_version": _CDP_SNAPSHOT_SCHEMA,
            "safe_for_execution": False,
            "actionable_grounding": False,
            "frames": [{"frame_id": "main", "url": "https://example.test"}],
            "dom_snapshot": {"documents": [{"nodes": {"nodeType": [9]}}]},
            "ax_tree": {"nodes": []},
        }

    def _write_json(self, name: str, payload: Any) -> Path:
        path = self.sources / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return path

    def _write_jsonl(self, name: str, *records: dict[str, Any]) -> Path:
        path = self.sources / name
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        return path

    def _write_png(
        self,
        name: str,
        *,
        extra: bytes = b"",
        content: bytes | None = None,
    ) -> Path:
        path = self.sources / name
        path.write_bytes((_ONE_PIXEL_PNG + extra) if content is None else content)
        return path

    def _final_dir(self, run: dict[str, Any]) -> Path:
        return (
            self.evaluation_root
            / "artifacts"
            / run["scheme_id"]
            / run["task_id"]
            / f"r{run['repetition']:03d}"
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return (
            len(data).to_bytes(4, "big")
            + chunk_type
            + data
            + crc.to_bytes(4, "big")
        )


if __name__ == "__main__":
    unittest.main()
