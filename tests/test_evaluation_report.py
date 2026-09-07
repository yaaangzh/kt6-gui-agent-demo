from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from kt6_backend.evaluation_artifacts import (
    ACTION_EVENT_SCHEMA_VERSION,
    BROWSER_USE_DOM_SCHEMA_VERSION,
    CDP_PAGE_SNAPSHOT_SCHEMA_VERSION,
    CV_METADATA_SCHEMA_VERSION,
    PLANNER_CALL_SCHEMA_VERSION,
    UI_TARS_RESPONSE_SCHEMA_VERSION,
    VALIDATION_RESULT_SCHEMA_VERSION,
    VISION_MODEL_CALL_SCHEMA_VERSION,
)
from kt6_backend.evaluation_report import (
    EvaluationDataError,
    RUN_SCHEMA_VERSION,
    append_run_record,
    build_run_template,
    build_suite_template,
    generate_report,
    load_json_object,
    load_run_records,
    render_runs_csv,
    validate_run_record,
    validate_run_records,
    validate_suite,
    write_report_bundle,
)
from kt6_backend.evaluation_report_cli import main as evaluation_cli_main
from kt6_backend.topology_cv_routing import ROUTING_SCHEMA_VERSION
from kt6_backend.topology_fusion import FUSION_SCHEMA_VERSION, fuse_topology_payloads
from kt6_backend.topology_model_contract import MODEL_SCHEMA_VERSION
from kt6_backend.topology_vision_contract import RESPONSE_SCHEMA_VERSION
from kt6_backend.ui_graph import SCHEMA_VERSION as UI_GRAPH_SCHEMA_VERSION


_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class EvaluationReportTest(unittest.TestCase):
    def setUp(self):
        scratch_root = Path.cwd() / ".test-tmp"
        scratch_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=scratch_root)
        self.root = Path(self.temp_dir.name).resolve()
        suite = build_suite_template(
            suite_id="nce-simple-query",
            title="NCE-IP简单查询任务",
            task_count=2,
            repetitions=2,
            step_limit=10,
            planner_provider="deepseek",
            planner_model="deepseek-test-model",
            environment_id="nce-lab-a",
        )
        suite["status"] = "ready"
        suite["tasks"][0].update(
            {
                "title": "查询设备状态",
                "scenario_type": "dom",
                "difficulty": "easy",
                "validation": {
                    "method": "dom_assertion",
                    "description": "结果表包含目标设备及在线状态",
                },
            }
        )
        suite["tasks"][1].update(
            {
                "title": "查看拓扑节点",
                "scenario_type": "canvas",
                "difficulty": "medium",
                "validation": {
                    "method": "page_state",
                    "description": "目标节点详情面板已经打开",
                },
            }
        )
        self.suite = validate_suite(suite)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(
        self,
        scheme_id: str,
        task_id: str,
        repetition: int,
        *,
        outcome: str = "success",
        duration_ms: int = 1000,
        safety_violations: int = 0,
        planner_model: str = "deepseek-test-model",
        environment_id: str = "nce-lab-a",
        validation_method: str | None = None,
    ) -> dict[str, object]:
        success = outcome == "success"
        task = next(
            task for task in self.suite["tasks"] if task["task_id"] == task_id
        )
        uses_current_canvas = (
            scheme_id == "current"
            and task["scenario_type"] in {"canvas", "mixed"}
        )
        cv_calls = 1 if uses_current_canvas else 0
        vision_model_calls = (
            1
            if uses_current_canvas
            else (4 if scheme_id == "ui_tars" else 0)
        )
        planner_model_calls = 2
        if validation_method is None:
            validation_method = task["validation"]["method"]
        return {
            "schema_version": RUN_SCHEMA_VERSION,
            "record_status": "final",
            "run_id": f"{scheme_id}-{task_id}-r{repetition}",
            "suite_id": self.suite["suite_id"],
            "scheme_id": scheme_id,
            "task_id": task_id,
            "repetition": repetition,
            "outcome": outcome,
            "started_at": "2026-08-11T08:00:00Z",
            "duration_ms": duration_ms,
            "step_count": 4,
            "model_calls": planner_model_calls + vision_model_calls,
            "implementation": {
                "name": f"{scheme_id}-adapter",
                "version": "1.0.0",
                "revision": "abc1234",
                "branch": "evaluation-test",
            },
            "task_prompt_version": "v1",
            "planner": {
                "provider": "deepseek",
                "model": planner_model,
                "adapter_prompt_version": "adapter-v1",
            },
            "environment": {
                "environment_id": environment_id,
                "browser": "Chromium test",
                "viewport": "1920x1080",
            },
            "metrics": {
                "first_target_hit": success,
                "misclick_count": 0 if success else 1,
                "retry_count": 0 if success else 1,
                "loop_count": 0,
                "timeout_count": 1 if outcome == "timeout" else 0,
                "safety_violation_count": safety_violations,
                "cv_calls": cv_calls,
                "planner_model_calls": planner_model_calls,
                "vision_model_calls": vision_model_calls,
                "input_tokens": 100,
                "output_tokens": 20,
                "cost": 0.01,
            },
            "validation": {
                "method": validation_method,
                "passed": success,
                "evidence_ref": "validation_result-001",
            },
            "failure": None
            if success
            else {"category": outcome, "reason": "目标未达到预期状态"},
            "evidence": {"status": "pending"},
            "notes": "",
        }

    def _artifact_sources(
        self,
        run: dict[str, object],
        *,
        prefix: str = "run",
    ) -> list[tuple[str, Path]]:
        source_dir = self.root / "sources"
        source_dir.mkdir(exist_ok=True)

        def write_json(name: str, payload: object) -> Path:
            path = source_dir / name
            path.write_text(
                json.dumps(payload, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return path

        def write_jsonl(name: str, *payloads: object) -> Path:
            path = source_dir / name
            path.write_text(
                "".join(
                    json.dumps(payload, ensure_ascii=False) + "\n"
                    for payload in payloads
                ),
                encoding="utf-8",
            )
            return path

        def write_png(name: str) -> Path:
            path = source_dir / name
            path.write_bytes(_ONE_PIXEL_PNG)
            return path

        def sha256(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        def ui_graph_payload() -> dict[str, object]:
            capture_id = f"capture-{run['run_id']}"
            nodes = [
                    {
                        "id": f"node-{run['run_id']}",
                        "kind": "element",
                        "source": {
                            "kind": "dom",
                            "path": "fixture.elements[0]",
                            "source_ref": "#evaluation-root",
                        },
                        "role": "document",
                        "name": "evaluation fixture",
                        "actionable": False,
                        "can_click_now": False,
                        "safe_for_execution": False,
                        "disabled": False,
                        "interaction": {
                            "status": "analysis_only",
                            "candidate": False,
                            "authorized": False,
                            "preflight_required": False,
                            "reason_codes": ["analysis_only_ui_graph"],
                        },
                    }
                ]
            edges: list[object] = []
            issues: list[object] = []
            graph_core = {
                "capture_id": capture_id,
                "nodes": nodes,
                "edges": edges,
                "issues": issues,
            }
            graph_id = "uig:" + hashlib.sha256(
                json.dumps(
                    graph_core,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()[:24]
            return {
                "schema_version": UI_GRAPH_SCHEMA_VERSION,
                "graph_id": graph_id,
                **graph_core,
                "analysis_only": True,
                "execution_authorized": False,
                "safe_for_execution": False,
                "stats": {
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                    "issue_count": len(issues),
                    "truncated": False,
                },
            }

        sources: list[tuple[str, Path]] = [
            (
                "action_trace",
                write_jsonl(
                    f"{prefix}-actions.jsonl",
                    *[
                        {
                            "schema_version": ACTION_EVENT_SCHEMA_VERSION,
                            "run_id": run["run_id"],
                            "step_index": step_index,
                            "action": "observe",
                            "safety_violation": step_index <= int(
                                run["metrics"]["safety_violation_count"]
                            ),
                            "elapsed_ms": 5,
                        }
                        for step_index in range(1, int(run["step_count"]) + 1)
                    ],
                ),
            ),
            (
                "validation_result",
                write_json(
                    f"{prefix}-validation.json",
                    {
                        "schema_version": VALIDATION_RESULT_SCHEMA_VERSION,
                        "evidence_id": "validation_result-001",
                        "run_id": run["run_id"],
                        "task_id": run["task_id"],
                        "method": run["validation"]["method"],
                        "passed": run["validation"]["passed"],
                        **(
                            {"evaluator": run["validation"]["evaluator"]}
                            if "evaluator" in run["validation"]
                            else {}
                        ),
                    },
                ),
            ),
        ]
        scheme_id = str(run["scheme_id"])
        if scheme_id == "current":
            run_succeeded = run["outcome"] == "success"
            if run_succeeded:
                sources.append(
                    (
                        "ui_graph",
                        write_json(
                            f"{prefix}-ui-graph.json",
                            ui_graph_payload(),
                        ),
                    )
                )
            task = next(
                task
                for task in self.suite["tasks"]
                if task["task_id"] == run["task_id"]
            )
            if task["scenario_type"] in {"canvas", "mixed"}:
                screenshot = write_png(f"{prefix}-original.png")
                screenshot_sha256 = sha256(screenshot)
                cv_payload = {
                    "schema_version": RESPONSE_SCHEMA_VERSION,
                    "confidence": 0.0,
                    "objects": [],
                    "links": [],
                    "co_channel_relations": [],
                    "negative_edges": [],
                    "structure_templates": [],
                    "no_connections": True,
                }
                cv_result = write_json(f"{prefix}-cv.json", cv_payload)
                cv_source_id = str(run["run_id"])
                failure_category = (
                    str(run["failure"]["category"])
                    if isinstance(run.get("failure"), dict)
                    else ""
                )
                vision_status = {
                    "vision_timeout": "timeout",
                    "vision_invalid_json": "invalid_response",
                }.get(failure_category, "success" if run_succeeded else "error")
                routing_payload = {
                    "schema_version": ROUTING_SCHEMA_VERSION,
                    "policy_version": "evaluation-test-v1",
                    "decision": "model_assist",
                    "requested_profile": "hybrid",
                    "effective_profile": "hybrid",
                    "scene_type": "canvas_topology",
                    "requirement_satisfied": run_succeeded,
                    "model_invoked": True,
                    "result_status": "ok" if run_succeeded else "model_failed",
                    "execution_status": (
                        "completed" if run_succeeded else vision_status
                    ),
                    "source": {
                        "source_id": cv_source_id,
                        "sha256": screenshot_sha256,
                        "mime_type": "image/png",
                        "width": 1,
                        "height": 1,
                        "cv_adapter_id": "opencv-ocr-test-adapter",
                        "cv_adapter_version": "1.0.0",
                    },
                }
                sources.extend(
                    [
                        ("original_screenshot", screenshot),
                        ("cv_result", cv_result),
                        (
                            "cv_metadata",
                            write_json(
                                f"{prefix}-cv-metadata.json",
                                {
                                    "schema_version": CV_METADATA_SCHEMA_VERSION,
                                    "source_id": cv_source_id,
                                    "adapter_id": "opencv-ocr-test-adapter",
                                    "adapter_version": "1.0.0",
                                    "artifact_sha256": sha256(cv_result),
                                    "frames": [
                                        {
                                            "canvas_id": "canvas-1",
                                            "sha256": screenshot_sha256,
                                            "mime_type": "image/png",
                                            "width": 1,
                                            "height": 1,
                                        }
                                    ],
                                },
                            ),
                        ),
                        (
                            "routing_result",
                            write_json(f"{prefix}-routing.json", routing_payload),
                        ),
                    ]
                )
                vision_call: dict[str, object] = {
                    "schema_version": VISION_MODEL_CALL_SCHEMA_VERSION,
                    "run_id": run["run_id"],
                    "call_index": 1,
                    "producer": {
                        "provider": "model_api",
                        "model": "glm-5.1-evaluation-fixture",
                    },
                    "status": vision_status,
                    "duration_ms": 250 if run_succeeded else 300_000,
                    "screenshot_artifact_id": "original-screenshot-001",
                    "screenshot_sha256": screenshot_sha256,
                    "routing_artifact_id": "routing-result-001",
                    "diagnostic_artifact_ids": [],
                }
                if run_succeeded:
                    model_payload = {
                        "schema_version": MODEL_SCHEMA_VERSION,
                        "confidence": 0.0,
                        "nodes": [],
                        "links": [],
                        "structure_templates": [],
                        "negative_edges": [],
                        "no_connections": True,
                    }
                    model_result = write_json(f"{prefix}-model.json", model_payload)
                    fused_payload = fuse_topology_payloads(cv_payload, model_payload)
                    fused_payload["routing"] = routing_payload
                    sources.extend(
                        [
                            ("model_result", model_result),
                            (
                                "fused_result",
                                write_json(f"{prefix}-fused.json", fused_payload),
                            ),
                        ]
                    )
                    vision_call.update(
                        {
                            "model_result_artifact_id": "model-result-001",
                            "model_result_sha256": sha256(model_result),
                        }
                    )
                else:
                    vision_call["error_code"] = failure_category or "vision_error"
                    if vision_status == "invalid_response":
                        sources.append(
                            (
                                "model_events",
                                write_jsonl(
                                    f"{prefix}-model-events.jsonl",
                                    {
                                        "event_type": "invalid_response",
                                        "call_index": 1,
                                    },
                                ),
                            )
                        )
                        vision_call["diagnostic_artifact_ids"] = [
                            "model-events-001"
                        ]
                sources.append(
                    (
                        "vision_model_call",
                        write_jsonl(
                            f"{prefix}-vision-model-calls.jsonl",
                            vision_call,
                        ),
                    )
                )
        elif scheme_id == "browser_use":
            sources.extend(
                [
                    (
                        "dom_snapshot",
                        write_json(
                            f"{prefix}-dom-snapshot.json",
                            {
                                "schema_version": BROWSER_USE_DOM_SCHEMA_VERSION,
                                "run_id": run["run_id"],
                                "task_id": run["task_id"],
                                "safe_for_execution": False,
                                "elements": [
                                    {
                                        "element_id": f"element-{run['run_id']}",
                                        "role": "document",
                                    }
                                ],
                            },
                        ),
                    ),
                    (
                        "cdp_snapshot",
                        write_json(
                            f"{prefix}-cdp-snapshot.json",
                            {
                                "schema_version": CDP_PAGE_SNAPSHOT_SCHEMA_VERSION,
                                "safe_for_execution": False,
                                "actionable_grounding": False,
                                "frames": [{"frame_id": "main"}],
                                "dom_snapshot": {
                                    "documents": [
                                        {
                                            "document_id": "main",
                                            "nodes": [],
                                        }
                                    ]
                                },
                                "ax_tree": {"nodes": []},
                            },
                        ),
                    ),
                ]
            )
        elif scheme_id == "ui_tars":
            ui_tars_screenshots = [
                (
                    "original_screenshot",
                    write_png(f"{prefix}-step-{step_index:03d}.png"),
                )
                for step_index in range(1, int(run["step_count"]) + 1)
            ]
            sources.extend(ui_tars_screenshots)
            sources.append(
                (
                    "ui_tars_response",
                    write_jsonl(
                        f"{prefix}-ui-tars-response.jsonl",
                        *[
                            {
                                "schema_version": UI_TARS_RESPONSE_SCHEMA_VERSION,
                                "run_id": run["run_id"],
                                "step_index": step_index,
                                "call_index": step_index,
                                "producer": {
                                    "provider": "ui-tars",
                                    "model": "ui-tars-evaluation-fixture",
                                },
                                "screenshot_sha256": sha256(
                                    ui_tars_screenshots[step_index - 1][1]
                                ),
                                "screenshot_artifact_id": (
                                    f"original-screenshot-{step_index:03d}"
                                ),
                                "response": {"action": "observe"},
                            }
                            for step_index in range(
                                1, int(run["step_count"]) + 1
                            )
                        ],
                    ),
                )
            )

        planner_calls = int(run["metrics"]["planner_model_calls"])
        if planner_calls:
            planner_input_roles = {
                "ui_graph",
                "dom_snapshot",
                "cdp_snapshot",
                "original_screenshot",
            }
            planner_input_path = next(
                path for role, path in sources if role in planner_input_roles
            )
            sources.append(
                (
                    "planner_result",
                    write_jsonl(
                        f"{prefix}-planner-results.jsonl",
                        *[
                            {
                                "schema_version": PLANNER_CALL_SCHEMA_VERSION,
                                "run_id": run["run_id"],
                                "call_index": call_index,
                                "producer": {
                                    "provider": run["planner"]["provider"],
                                    "model": run["planner"]["model"],
                                },
                                "input_refs": [sha256(planner_input_path)],
                                "response": {"action": "observe"},
                            }
                            for call_index in range(1, planner_calls + 1)
                        ],
                    ),
                )
            )
        return sources

    def _archive_records(
        self,
        records: list[dict[str, object]],
        *,
        name: str,
        suite: dict[str, object] | None = None,
    ) -> tuple[Path, list[dict[str, object]]]:
        evaluation_root = self.root / name
        runs_path = evaluation_root / "runs.jsonl"
        selected_suite = suite or self.suite
        for index, run in enumerate(records, start=1):
            append_run_record(
                runs_path,
                run,
                selected_suite,
                artifact_sources=self._artifact_sources(
                    run,
                    prefix=f"{name}-{index:03d}",
                ),
            )
        return evaluation_root, load_run_records(runs_path)

    def _complete_records(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for scheme in ("current", "browser_use", "ui_tars"):
            for task in ("T01", "T02"):
                for repetition in (1, 2):
                    records.append(
                        self._run(
                            scheme,
                            task,
                            repetition,
                            duration_ms={
                                "current": 900,
                                "browser_use": 700,
                                "ui_tars": 1600,
                            }[scheme]
                            + repetition,
                        )
                    )
        return records

    def test_draft_suite_and_run_templates_require_explicit_finalization(self):
        draft_suite = build_suite_template(
            suite_id="draft-suite", title="Draft", task_count=1
        )
        with self.assertRaisesRegex(EvaluationDataError, "suite.status"):
            validate_suite(draft_suite)

        normalized = validate_suite(draft_suite, require_ready=False)
        draft_run = build_run_template(
            normalized, scheme_id="current", task_id="T01", repetition=1
        )
        self.assertEqual(draft_run["record_status"], "draft")
        with self.assertRaisesRegex(EvaluationDataError, "record_status"):
            validate_run_record(draft_run, normalized)

        draft_suite["status"] = "ready"
        with self.assertRaisesRegex(EvaluationDataError, "placeholder"):
            validate_suite(draft_suite)

    def test_complete_report_ranks_by_success_then_latency(self):
        records = self._complete_records()
        for record in records:
            if (
                record["scheme_id"] == "browser_use"
                and record["task_id"] == "T02"
                and record["repetition"] == 2
            ):
                record.update(
                    {
                        "outcome": "failure",
                        "validation": {
                            "method": "page_state",
                            "passed": False,
                            "evidence_ref": "validation_result-001",
                        },
                        "failure": {
                            "category": "stale_element",
                            "reason": "页面刷新后目标失效",
                        },
                    }
                )
                record["metrics"]["first_target_hit"] = False
                record["metrics"]["misclick_count"] = 1

        evidence_root, records = self._archive_records(
            records,
            name="complete-ranking",
        )

        report = generate_report(
            self.suite,
            records,
            generated_at="2026-08-11T09:00:00Z",
            evidence_root=evidence_root,
        )

        self.assertEqual(report["report_status"], "complete")
        self.assertEqual(report["coverage"]["coverage_rate"], 1.0)
        self.assertTrue(report["fairness"]["fair_comparison"])
        self.assertEqual(
            report["conclusion"]["recommended_scheme_id"], "current"
        )
        by_scheme = {
            item["scheme_id"]: item for item in report["overall_metrics"]
        }
        self.assertEqual(by_scheme["current"]["strict_success_rate"], 1.0)
        self.assertEqual(by_scheme["browser_use"]["strict_success_rate"], 0.75)
        self.assertEqual(by_scheme["browser_use"]["misclick_run_rate"], 0.25)
        self.assertEqual(by_scheme["current"]["total_input_tokens"], 400)
        self.assertEqual(len(report["breakdown_metrics"]), 12)

    def test_current_vision_failures_count_as_runs_but_not_successes(self):
        records = self._complete_records()
        failure_specs = {
            1: ("timeout", "vision_timeout"),
            2: ("failure", "vision_invalid_json"),
        }
        for record in records:
            if record["scheme_id"] != "current" or record["task_id"] != "T02":
                continue
            outcome, category = failure_specs[int(record["repetition"])]
            record["outcome"] = outcome
            record["validation"]["passed"] = False
            record["failure"] = {
                "category": category,
                "reason": "vision model call did not produce a usable result",
            }
            record["metrics"]["first_target_hit"] = False
            record["metrics"]["timeout_count"] = int(outcome == "timeout")

        evidence_root, records = self._archive_records(
            records,
            name="current-vision-failures",
        )
        report = generate_report(
            self.suite,
            records,
            evidence_root=evidence_root,
        )
        current = next(
            item
            for item in report["overall_metrics"]
            if item["scheme_id"] == "current"
        )

        self.assertEqual(report["coverage"]["recorded_runs"], 12)
        self.assertEqual(report["coverage"]["missing_runs"], 0)
        self.assertEqual(report["evidence"]["verified_runs"], 12)
        self.assertEqual(report["evidence"]["failed_runs"], 0)
        self.assertFalse(report["evidence"]["ranking_blocked"])
        self.assertEqual(current["recorded_runs"], 4)
        self.assertEqual(current["success_count"], 2)
        self.assertEqual(current["failure_count"], 1)
        self.assertEqual(current["timeout_count"], 1)
        self.assertEqual(current["strict_success_rate"], 0.5)
        self.assertEqual(current["observed_success_rate"], 0.5)
        self.assertEqual(current["full_repeat_success_rate"], 0.5)
        self.assertEqual(current["average_success_duration_ms"], 901.5)
        self.assertEqual(
            current["failure_categories"],
            {"vision_timeout": 1, "vision_invalid_json": 1},
        )
        self.assertEqual(
            {
                failure["failure_category"]
                for failure in report["failures"]
                if failure["scheme_id"] == "current"
            },
            {"vision_timeout", "vision_invalid_json"},
        )

    def test_incomplete_runs_are_not_ranked_or_hidden(self):
        evidence_root, records = self._archive_records(
            [self._run("current", "T01", 1)],
            name="incomplete",
        )
        report = generate_report(
            self.suite,
            records,
            generated_at="2026-08-11T09:00:00Z",
            evidence_root=evidence_root,
        )

        self.assertEqual(report["report_status"], "incomplete")
        self.assertEqual(report["coverage"]["expected_runs"], 12)
        self.assertEqual(report["coverage"]["missing_runs"], 11)
        self.assertEqual(report["conclusion"]["decision"], "incomplete")
        current = report["overall_metrics"][0]
        self.assertEqual(current["observed_success_rate"], 1.0)
        self.assertEqual(current["strict_success_rate"], 0.25)

    def test_report_requires_evidence_verification_by_default(self):
        with self.assertRaisesRegex(EvaluationDataError, "evidence_root"):
            generate_report(self.suite, [self._run("current", "T01", 1)])

    def test_planner_or_environment_mismatch_blocks_fair_comparison(self):
        records = self._complete_records()
        records[0]["planner"]["model"] = "another-model"
        records[1]["environment"]["environment_id"] = "another-lab"
        evidence_root, records = self._archive_records(
            records,
            name="planner-environment-mismatch",
        )

        report = generate_report(self.suite, records, evidence_root=evidence_root)

        self.assertEqual(report["report_status"], "fairness_blocked")
        self.assertFalse(report["fairness"]["fair_comparison"])
        self.assertEqual(report["conclusion"]["decision"], "fairness_blocked")
        self.assertGreaterEqual(len(report["warnings"]), 2)

    def test_mixed_implementation_versions_block_fair_comparison(self):
        records = self._complete_records()
        records[0]["implementation"]["revision"] = "different-build"
        evidence_root, records = self._archive_records(
            records,
            name="implementation-mismatch",
        )

        report = generate_report(self.suite, records, evidence_root=evidence_root)

        self.assertFalse(report["fairness"]["fair_comparison"])
        self.assertEqual(report["report_status"], "fairness_blocked")
        self.assertTrue(
            any("实现版本" in issue for issue in report["fairness"]["hard_issues"])
        )

    def test_mixed_model_judges_block_fair_comparison(self):
        suite = json.loads(json.dumps(self.suite))
        for task in suite["tasks"]:
            task["validation"]["method"] = "model_judge"
        records = self._complete_records()
        for record in records:
            record["validation"].update(
                {
                    "method": "model_judge",
                    "evaluator": {"provider": "qwen", "model": "qwen-eval-a"},
                }
            )
        records[0]["validation"]["evaluator"]["model"] = "qwen-eval-b"
        evidence_root, records = self._archive_records(
            records,
            name="judge-mismatch",
            suite=suite,
        )

        report = generate_report(suite, records, evidence_root=evidence_root)

        self.assertFalse(report["fairness"]["fair_comparison"])
        self.assertEqual(
            report["fairness"]["evaluator_models"],
            ["qwen/qwen-eval-a", "qwen/qwen-eval-b"],
        )

    def test_self_judging_model_is_reported_as_hard_fairness_issue(self):
        records = self._complete_records()
        records[0]["validation"] = {
            "method": "model_judge",
            "passed": True,
            "evidence_ref": "validation_result-001",
            "evaluator": {
                "provider": "deepseek",
                "model": "deepseek-test-model",
            },
        }
        evidence_root, records = self._archive_records(
            records,
            name="self-judge",
        )

        report = generate_report(self.suite, records, evidence_root=evidence_root)

        self.assertEqual(report["fairness"]["self_judge_run_count"], 1)
        self.assertFalse(report["fairness"]["fair_comparison"])

    def test_safety_violation_disqualifies_one_scheme(self):
        records = self._complete_records()
        for record in records:
            if record["scheme_id"] == "current" and record["task_id"] == "T01":
                record["metrics"]["safety_violation_count"] = 1
        evidence_root, records = self._archive_records(
            records,
            name="safety",
        )

        report = generate_report(self.suite, records, evidence_root=evidence_root)

        self.assertEqual(report["report_status"], "safety_warning")
        self.assertEqual(
            report["conclusion"]["recommended_scheme_id"], "browser_use"
        )
        current = next(
            item
            for item in report["overall_metrics"]
            if item["scheme_id"] == "current"
        )
        self.assertFalse(current["eligible_for_ranking"])

    def test_invalid_success_and_duplicate_execution_key_are_rejected(self):
        invalid = self._run("current", "T01", 1)
        invalid["validation"]["passed"] = False
        with self.assertRaisesRegex(EvaluationDataError, "validation.passed"):
            validate_run_record(invalid, self.suite)

        missing_evidence = self._run("current", "T01", 1)
        missing_evidence["validation"]["evidence_ref"] = ""
        with self.assertRaisesRegex(EvaluationDataError, "evidence_ref"):
            validate_run_record(missing_evidence, self.suite)

        over_step_limit = self._run("current", "T01", 1)
        over_step_limit["step_count"] = 11
        with self.assertRaisesRegex(EvaluationDataError, "step limit"):
            validate_run_record(over_step_limit, self.suite)

        inconsistent_calls = self._run("current", "T01", 1)
        inconsistent_calls["metrics"]["planner_model_calls"] = 1
        with self.assertRaisesRegex(EvaluationDataError, "model_calls must equal"):
            validate_run_record(inconsistent_calls, self.suite)

        legacy_artifacts = self._run("current", "T01", 1)
        legacy_artifacts["artifacts"] = ["unverified-result.json"]
        with self.assertRaisesRegex(EvaluationDataError, "no longer supported"):
            validate_run_record(legacy_artifacts, self.suite)

        missing_evaluator = self._run("current", "T01", 1)
        missing_evaluator["validation"]["method"] = "model_judge"
        with self.assertRaisesRegex(EvaluationDataError, "requires.*evaluator"):
            validate_run_record(missing_evaluator, self.suite)

        dangerous_id = self._run("current", "T01", 1)
        dangerous_id["run_id"] = "CON"
        with self.assertRaisesRegex(EvaluationDataError, "reserved"):
            validate_run_record(dangerous_id, self.suite)

        first = self._run("current", "T01", 1)
        duplicate = self._run("current", "T01", 1)
        duplicate["run_id"] = "different-run-id"
        with self.assertRaisesRegex(EvaluationDataError, "duplicate execution key"):
            validate_run_records([first, duplicate], self.suite)

    def test_load_json_object_rejects_duplicate_keys_and_non_finite_numbers(self):
        invalid_documents = {
            "duplicate-key": (
                '{"schema_version":"kt6.evaluation-suite.v1",'
                '"suite_id":"first","suite_id":"second"}'
            ),
            "nan": '{"schema_version":"kt6.evaluation-suite.v1","value":NaN}',
            "infinity": (
                '{"schema_version":"kt6.evaluation-suite.v1",'
                '"value":Infinity}'
            ),
        }

        for case, raw_json in invalid_documents.items():
            with self.subTest(case=case):
                suite_path = self.root / f"invalid-suite-{case}.json"
                suite_path.write_text(raw_json, encoding="utf-8")
                with self.assertRaises(EvaluationDataError):
                    load_json_object(suite_path)

    def test_load_run_records_rejects_duplicate_run_id_and_non_finite_numbers(self):
        invalid_lines = {
            "duplicate-run-id": '{"run_id":"first","run_id":"second"}',
            "nan": '{"run_id":"run-nan","duration_ms":NaN}',
            "infinity": (
                '{"run_id":"run-infinity","duration_ms":Infinity}'
            ),
        }

        for case, raw_jsonl in invalid_lines.items():
            with self.subTest(case=case):
                runs_path = self.root / f"invalid-runs-{case}.jsonl"
                runs_path.write_text(raw_jsonl + "\n", encoding="utf-8")
                with self.assertRaises(EvaluationDataError):
                    load_run_records(runs_path)

    def test_append_and_report_bundle_preserve_machine_and_leader_outputs(self):
        runs_path = self.root / "runs.jsonl"
        first = self._run("current", "T01", 1)
        artifact_sources = self._artifact_sources(first, prefix="append-first")
        archived = append_run_record(
            runs_path,
            first,
            self.suite,
            artifact_sources=artifact_sources,
        )
        loaded = load_run_records(runs_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(archived["evidence"]["status"], "archived")
        self.assertTrue(
            (self.root / archived["evidence"]["manifest_ref"]).is_file()
        )
        with self.assertRaisesRegex(EvaluationDataError, "duplicate run_id"):
            append_run_record(
                runs_path,
                first,
                self.suite,
                artifact_sources=artifact_sources,
            )

        report = generate_report(
            self.suite,
            loaded,
            evidence_root=self.root,
            require_evidence=True,
        )
        self.assertEqual(report["evidence"]["verified_runs"], 1)
        self.assertEqual(report["evidence"]["failed_runs"], 0)
        output_dir = self.root / "report"
        written = write_report_bundle(report, output_dir)

        self.assertEqual(len(written), 7)
        self.assertEqual(
            {path.name for path in written},
            {
                "report.json",
                "metrics.csv",
                "runs.csv",
                "report.md",
                "report.html",
                "issues.md",
                "conclusion.md",
            },
        )
        self.assertIn("现有方案", (output_dir / "report.md").read_text("utf-8"))
        self.assertIn("strict_success_rate", (output_dir / "metrics.csv").read_text("utf-8"))
        self.assertIn("<!doctype html>", (output_dir / "report.html").read_text("utf-8"))
        with self.assertRaisesRegex(EvaluationDataError, "refusing to overwrite"):
            write_report_bundle(report, output_dir)

    def test_tampered_evidence_blocks_report_ranking(self):
        runs_path = self.root / "runs.jsonl"
        run = self._run("current", "T01", 1)
        archived = append_run_record(
            runs_path,
            run,
            self.suite,
            artifact_sources=self._artifact_sources(run, prefix="tamper"),
        )
        manifest_path = self.root / archived["evidence"]["manifest_ref"]
        manifest = json.loads(manifest_path.read_text("utf-8"))
        ui_graph = next(
            item for item in manifest["artifacts"] if item["role"] == "ui_graph"
        )
        archived_ui_graph = manifest_path.parent / ui_graph["path"]
        archived_ui_graph.write_text("{}\n", encoding="utf-8")

        report = generate_report(
            self.suite,
            load_run_records(runs_path),
            evidence_root=self.root,
            require_evidence=True,
        )

        self.assertEqual(report["report_status"], "evidence_blocked")
        self.assertEqual(report["evidence"]["failed_runs"], 1)
        self.assertTrue(report["evidence"]["ranking_blocked"])
        current = next(
            item
            for item in report["overall_metrics"]
            if item["scheme_id"] == "current"
        )
        self.assertFalse(current["eligible_for_ranking"])
        self.assertEqual(report["conclusion"]["decision"], "evidence_blocked")

    def test_rehashed_but_semantically_invalid_evidence_blocks_ranking(self):
        runs_path = self.root / "runs.jsonl"
        run = self._run("current", "T01", 1)
        archived = append_run_record(
            runs_path,
            run,
            self.suite,
            artifact_sources=self._artifact_sources(run, prefix="semantic-tamper"),
        )
        records = load_run_records(runs_path)
        manifest_path = self.root / archived["evidence"]["manifest_ref"]
        manifest = json.loads(manifest_path.read_text("utf-8"))
        action_entry = next(
            item for item in manifest["artifacts"] if item["role"] == "action_trace"
        )
        archived_action = manifest_path.parent / action_entry["path"]
        action_text = archived_action.read_text("utf-8").replace(
            ACTION_EVENT_SCHEMA_VERSION,
            "kt6.evaluation-action-event.SECRET-ASSET-42",
        )
        archived_action.write_text(action_text, encoding="utf-8")
        action_entry["sha256"] = hashlib.sha256(
            archived_action.read_bytes()
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        records[0]["evidence"]["manifest_sha256"] = hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest()

        report = generate_report(
            self.suite,
            records,
            evidence_root=self.root,
            require_evidence=True,
        )

        self.assertEqual(report["report_status"], "evidence_blocked")
        self.assertEqual(report["evidence"]["failed_runs"], 1)
        self.assertTrue(report["evidence"]["ranking_blocked"])
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertIn("evidence_schema_invalid", rendered)
        self.assertNotIn("SECRET-ASSET-42", rendered)

    def test_reports_omit_local_free_text_and_validation_references(self):
        run = self._run("current", "T01", 1, outcome="failure")
        run["failure"]["reason"] = "=HYPERLINK(\"https://invalid\")"
        run["notes"] = "sensitive page text"
        report = generate_report(self.suite, [run], require_evidence=False)

        rows = list(csv.DictReader(io.StringIO(render_runs_csv(report))))

        self.assertNotIn("failure_reason", rows[0])
        self.assertNotIn("notes", report["runs"][0])
        self.assertNotIn("evidence_ref", report["runs"][0]["validation"])
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("HYPERLINK", rendered)
        self.assertNotIn("sensitive page text", rendered)

    def test_cli_init_template_record_validate_and_report_flow(self):
        suite_path = self.root / "suite.json"
        self.assertEqual(
            evaluation_cli_main(
                [
                    "init",
                    "--out",
                    str(suite_path),
                    "--suite-id",
                    "cli-suite",
                    "--title",
                    "CLI测试",
                    "--task-count",
                    "1",
                    "--repetitions",
                    "1",
                    "--planner-provider",
                    "deepseek",
                    "--planner-model",
                    "deepseek-test-model",
                    "--environment-id",
                    "nce-lab-a",
                ]
            ),
            0,
        )
        suite = json.loads(suite_path.read_text("utf-8"))
        suite["status"] = "ready"
        suite["tasks"][0]["title"] = "查询设备"
        suite["tasks"][0]["validation"]["description"] = "结果页显示设备"
        suite_path.write_text(
            json.dumps(suite, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        template_path = self.root / "draft-run.json"
        self.assertEqual(
            evaluation_cli_main(
                [
                    "run-template",
                    "--suite",
                    str(suite_path),
                    "--scheme",
                    "current",
                    "--task",
                    "T01",
                    "--repetition",
                    "1",
                    "--out",
                    str(template_path),
                ]
            ),
            0,
        )
        run = json.loads(template_path.read_text("utf-8"))
        run.update(self._run("current", "T01", 1))
        run["suite_id"] = "cli-suite"
        run["validation"]["method"] = "page_state"
        template_path.write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        runs_path = self.root / "runs.jsonl"
        artifact_sources = self._artifact_sources(run, prefix="cli")
        artifact_args: list[str] = []
        for role, path in artifact_sources:
            artifact_args.extend(["--artifact", f"{role}={path}"])
        self.assertEqual(
            evaluation_cli_main(
                [
                    "record",
                    "--suite",
                    str(suite_path),
                    "--runs",
                    str(runs_path),
                    "--input",
                    str(template_path),
                    *artifact_args,
                ]
            ),
            0,
        )
        self.assertEqual(
            evaluation_cli_main(
                [
                    "validate",
                    "--suite",
                    str(suite_path),
                    "--runs",
                    str(runs_path),
                ]
            ),
            6,
        )
        report_dir = self.root / "cli-report"
        self.assertEqual(
            evaluation_cli_main(
                [
                    "report",
                    "--suite",
                    str(suite_path),
                    "--runs",
                    str(runs_path),
                    "--out-dir",
                    str(report_dir),
                ]
            ),
            6,
        )
        self.assertTrue((report_dir / "report.md").is_file())

    def test_cli_errors_use_stable_codes_without_sensitive_details(self):
        suite_path = self.root / "suite.json"
        suite_path.write_text(
            json.dumps(self.suite, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        source_path = (
            self.root / "SECRET-ASSET-42" / "absolute-evidence-source.json"
        ).resolve()
        input_path = self.root / "unused-run.json"
        runs_path = self.root / "runs.jsonl"

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = evaluation_cli_main(
                [
                    "record",
                    "--suite",
                    str(suite_path),
                    "--runs",
                    str(runs_path),
                    "--input",
                    str(input_path),
                    "--artifact",
                    f"SECRET-ASSET-42={source_path}",
                ]
            )

        rendered = stderr.getvalue()
        self.assertEqual(exit_code, 3)
        self.assertEqual(
            json.loads(rendered),
            {
                "error_code": "evaluation_artifact_error",
                "stage": "record",
            },
        )
        self.assertNotIn("SECRET-ASSET-42", rendered)
        self.assertNotIn(str(source_path), rendered)
        self.assertNotIn("unsupported artifact role", rendered)

        malformed_suite = self.root / "SECRET-ASSET-42-invalid-suite.json"
        malformed_suite.write_text("{SECRET-ASSET-42", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = evaluation_cli_main(
                [
                    "validate",
                    "--suite",
                    str(malformed_suite.resolve()),
                    "--runs",
                    str(runs_path),
                ]
            )

        rendered = stderr.getvalue()
        self.assertEqual(exit_code, 3)
        self.assertEqual(
            json.loads(rendered),
            {
                "error_code": "evaluation_data_error",
                "stage": "validate",
            },
        )
        self.assertNotIn("SECRET-ASSET-42", rendered)
        self.assertNotIn(str(malformed_suite.resolve()), rendered)
        self.assertNotIn("invalid UTF-8 JSON file", rendered)

        missing_suite = self.root / "SECRET-ASSET-42-missing-suite.json"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = evaluation_cli_main(
                [
                    "report",
                    "--suite",
                    str(missing_suite.resolve()),
                    "--runs",
                    str(runs_path),
                    "--out-dir",
                    str(self.root / "unused-report"),
                ]
            )

        rendered = stderr.getvalue()
        self.assertEqual(exit_code, 3)
        self.assertEqual(
            json.loads(rendered),
            {
                "error_code": "evaluation_io_error",
                "stage": "report",
            },
        )
        self.assertNotIn("SECRET-ASSET-42", rendered)
        self.assertNotIn(str(missing_suite.resolve()), rendered)
        self.assertNotIn("cannot read JSON file", rendered)


if __name__ == "__main__":
    unittest.main()
