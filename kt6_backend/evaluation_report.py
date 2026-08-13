"""Offline, deterministic evaluation aggregation and report generation.

The evaluation pipeline intentionally consumes bounded JSON records instead of
calling browser agents or public model endpoints.  Each implementation under
test can therefore run inside its approved environment and export the same
``kt6.evaluation-run.v1`` contract for local comparison.
"""

from __future__ import annotations

import csv
import html
import io
import json
import math
import os
import shutil
import statistics
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .evaluation_artifacts import (
    EVIDENCE_PROFILES,
    ROLE_FORMATS,
    EvaluationArtifactError,
    archive_run_evidence,
    remove_new_evidence_archive,
    verify_run_evidence,
)


SUITE_SCHEMA_VERSION = "kt6.evaluation-suite.v1"
RUN_SCHEMA_VERSION = "kt6.evaluation-run.v1"
REPORT_SCHEMA_VERSION = "kt6.evaluation-report.v1"

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_JSONL_BYTES = 32 * 1024 * 1024
MAX_SCHEMES = 20
MAX_TASKS = 1000
MAX_REPETITIONS = 100
MAX_RUNS = 100_000
MAX_EVIDENCE_ARTIFACTS = 200
MAX_EVIDENCE_BYTES = 1024 * 1024 * 1024

OUTCOMES = frozenset({"success", "partial", "failure", "timeout", "blocked"})
SCENARIO_TYPES = frozenset({"dom", "canvas", "mixed", "complex", "other"})
DIFFICULTIES = frozenset({"easy", "medium", "hard", "unknown"})
VALIDATION_METHODS = frozenset(
    {
        "api_assertion",
        "dom_assertion",
        "page_state",
        "human_blind",
        "model_judge",
        "other",
    }
)


class EvaluationDataError(ValueError):
    """Raised when an evaluation artifact violates the bounded contract."""


def _strict_json_loads(text: str) -> Any:
    """Parse interoperable JSON without duplicate keys or NaN/Infinity."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    return json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def load_json_object(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    """Load one bounded UTF-8 JSON object."""

    resolved = Path(path).expanduser().resolve()
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise EvaluationDataError(f"cannot read JSON file: {resolved}") from exc
    if not raw:
        raise EvaluationDataError(f"JSON file is empty: {resolved}")
    if len(raw) > max_bytes:
        raise EvaluationDataError(
            f"JSON file exceeds {max_bytes} bytes: {resolved}"
        )
    try:
        payload = _strict_json_loads(raw.decode("utf-8-sig"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise EvaluationDataError(f"invalid UTF-8 JSON file: {resolved}") from exc
    if not isinstance(payload, dict):
        raise EvaluationDataError(f"JSON root must be an object: {resolved}")
    return payload


def load_run_records(path: Path) -> list[dict[str, Any]]:
    """Load bounded JSONL run records and reject duplicate execution keys."""

    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        return []
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise EvaluationDataError(f"cannot read run JSONL: {resolved}") from exc
    if len(raw) > MAX_JSONL_BYTES:
        raise EvaluationDataError(
            f"run JSONL exceeds {MAX_JSONL_BYTES} bytes: {resolved}"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EvaluationDataError(f"run JSONL is not UTF-8: {resolved}") from exc

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(records) >= MAX_RUNS:
            raise EvaluationDataError(f"run JSONL exceeds {MAX_RUNS} records")
        try:
            payload = _strict_json_loads(line)
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise EvaluationDataError(
                f"invalid JSONL at line {line_number}: {resolved}"
            ) from exc
        if not isinstance(payload, dict):
            raise EvaluationDataError(
                f"run JSONL line {line_number} must contain an object"
            )
        records.append(payload)
    return records


def validate_suite(payload: Mapping[str, Any], *, require_ready: bool = True) -> dict[str, Any]:
    """Validate and normalize an evaluation suite manifest."""

    suite = _copy_mapping(payload, "suite")
    _require_exact_schema(suite, SUITE_SCHEMA_VERSION, "suite")
    suite_id = _require_identifier(suite.get("suite_id"), "suite.suite_id")
    title = _require_text(suite.get("title"), "suite.title", max_length=300)
    description = _optional_text(
        suite.get("description", ""), "suite.description", max_length=2000
    )
    status = _require_choice(suite.get("status"), {"draft", "ready"}, "suite.status")
    if require_ready and status != "ready":
        raise EvaluationDataError(
            "suite.status must be 'ready' before recording or reporting results"
        )

    experiment = _copy_mapping(suite.get("experiment"), "suite.experiment")
    repetitions = _require_int(
        experiment.get("repetitions_per_task"),
        "suite.experiment.repetitions_per_task",
        minimum=1,
        maximum=MAX_REPETITIONS,
    )
    default_step_limit = _require_int(
        experiment.get("default_step_limit"),
        "suite.experiment.default_step_limit",
        minimum=1,
        maximum=10_000,
    )
    task_prompt_version = _require_text(
        experiment.get("task_prompt_version"),
        "suite.experiment.task_prompt_version",
        max_length=200,
    )
    environment_id = _optional_text(
        experiment.get("environment_id", ""),
        "suite.experiment.environment_id",
        max_length=200,
    )
    require_shared_planner = _require_bool(
        experiment.get("require_shared_planner", False),
        "suite.experiment.require_shared_planner",
    )
    shared_planner_raw = experiment.get("shared_planner")
    shared_planner: dict[str, str] | None = None
    if shared_planner_raw is not None:
        planner = _copy_mapping(
            shared_planner_raw, "suite.experiment.shared_planner"
        )
        shared_planner = {
            "provider": _require_text(
                planner.get("provider"),
                "suite.experiment.shared_planner.provider",
                max_length=100,
            ),
            "model": _require_text(
                planner.get("model"),
                "suite.experiment.shared_planner.model",
                max_length=200,
            ),
        }
    if require_shared_planner and shared_planner is None:
        raise EvaluationDataError(
            "suite.experiment.shared_planner is required when "
            "require_shared_planner=true"
        )

    schemes_raw = _require_list(suite.get("schemes"), "suite.schemes", 1, MAX_SCHEMES)
    schemes: list[dict[str, Any]] = []
    scheme_ids: set[str] = set()
    for index, raw_scheme in enumerate(schemes_raw):
        field = f"suite.schemes[{index}]"
        scheme = _copy_mapping(raw_scheme, field)
        scheme_id = _require_identifier(scheme.get("scheme_id"), f"{field}.scheme_id")
        if scheme_id.casefold() in scheme_ids:
            raise EvaluationDataError(f"duplicate scheme_id: {scheme_id}")
        scheme_ids.add(scheme_id.casefold())
        required_groups_raw = scheme.get("required_artifact_groups", [])
        if not isinstance(required_groups_raw, list):
            raise EvaluationDataError(
                f"{field}.required_artifact_groups must be an array"
            )
        required_groups: list[list[str]] = []
        for group_index, raw_group in enumerate(required_groups_raw):
            group_field = f"{field}.required_artifact_groups[{group_index}]"
            if not isinstance(raw_group, list) or not raw_group:
                raise EvaluationDataError(
                    f"{group_field} must be a non-empty array"
                )
            roles: list[str] = []
            for role_index, raw_role in enumerate(raw_group):
                role = _require_text(
                    raw_role, f"{group_field}[{role_index}]", max_length=100
                )
                if role not in ROLE_FORMATS or role == "run_metadata":
                    raise EvaluationDataError(
                        f"{group_field}[{role_index}] has unsupported artifact role: "
                        f"{role}"
                    )
                roles.append(role)
            normalized_roles = sorted(set(roles))
            if normalized_roles not in required_groups:
                required_groups.append(normalized_roles)
        schemes.append(
            {
                "scheme_id": scheme_id,
                "label": _require_text(scheme.get("label"), f"{field}.label", 200),
                "perception": _require_text(
                    scheme.get("perception"), f"{field}.perception", 500
                ),
                "planner": _require_text(
                    scheme.get("planner"), f"{field}.planner", 300
                ),
                "executor": _require_text(
                    scheme.get("executor"), f"{field}.executor", 300
                ),
                "evidence_profile": _require_choice(
                    scheme.get("evidence_profile"),
                    EVIDENCE_PROFILES,
                    f"{field}.evidence_profile",
                ),
                "required_artifact_groups": required_groups,
            }
        )

    tasks_raw = _require_list(suite.get("tasks"), "suite.tasks", 1, MAX_TASKS)
    tasks: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    for index, raw_task in enumerate(tasks_raw):
        field = f"suite.tasks[{index}]"
        task = _copy_mapping(raw_task, field)
        task_id = _require_identifier(task.get("task_id"), f"{field}.task_id")
        if task_id.casefold() in task_ids:
            raise EvaluationDataError(f"duplicate task_id: {task_id}")
        task_ids.add(task_id.casefold())
        validation_raw = _copy_mapping(task.get("validation"), f"{field}.validation")
        validation = {
            "method": _require_choice(
                validation_raw.get("method"),
                VALIDATION_METHODS,
                f"{field}.validation.method",
            ),
            "description": _require_text(
                validation_raw.get("description"),
                f"{field}.validation.description",
                1000,
            ),
        }
        tasks.append(
            {
                "task_id": task_id,
                "title": _require_text(task.get("title"), f"{field}.title", 500),
                "scenario_type": _require_choice(
                    task.get("scenario_type"),
                    SCENARIO_TYPES,
                    f"{field}.scenario_type",
                ),
                "difficulty": _require_choice(
                    task.get("difficulty", "unknown"),
                    DIFFICULTIES,
                    f"{field}.difficulty",
                ),
                "step_limit": _require_int(
                    task.get("step_limit", default_step_limit),
                    f"{field}.step_limit",
                    minimum=1,
                    maximum=10_000,
                ),
                "validation": validation,
                "tags": _normalize_text_list(task.get("tags", []), f"{field}.tags", 50),
            }
        )

    if status == "ready":
        ready_fields = [
            title,
            environment_id,
            *(
                [shared_planner["provider"], shared_planner["model"]]
                if shared_planner is not None
                else []
            ),
            *(task["title"] for task in tasks),
            *(task["validation"]["description"] for task in tasks),
        ]
        if any(_is_placeholder(value) for value in ready_fields):
            raise EvaluationDataError(
                "ready suite still contains fill-/待填写 placeholder values"
            )

    return {
        "schema_version": SUITE_SCHEMA_VERSION,
        "suite_id": suite_id,
        "title": title,
        "description": description,
        "status": status,
        "experiment": {
            "repetitions_per_task": repetitions,
            "default_step_limit": default_step_limit,
            "task_prompt_version": task_prompt_version,
            "environment_id": environment_id,
            "require_shared_planner": require_shared_planner,
            "shared_planner": shared_planner,
        },
        "schemes": schemes,
        "tasks": tasks,
    }


def validate_run_record(
    payload: Mapping[str, Any],
    suite: Mapping[str, Any],
    *,
    require_final: bool = True,
    require_archived_evidence: bool = False,
) -> dict[str, Any]:
    """Validate one run and its compact evidence index against a suite."""

    run = _copy_mapping(payload, "run")
    _require_exact_schema(run, RUN_SCHEMA_VERSION, "run")
    record_status = _require_choice(
        run.get("record_status"), {"draft", "final"}, "run.record_status"
    )
    if require_final and record_status != "final":
        raise EvaluationDataError("run.record_status must be 'final'")

    suite_id = _require_text(run.get("suite_id"), "run.suite_id", 200)
    if suite_id != suite["suite_id"]:
        raise EvaluationDataError(
            f"run.suite_id {suite_id!r} does not match {suite['suite_id']!r}"
        )
    run_id = _require_identifier(run.get("run_id"), "run.run_id")
    scheme_id = _require_identifier(run.get("scheme_id"), "run.scheme_id")
    scheme_ids = {item["scheme_id"] for item in suite["schemes"]}
    if scheme_id not in scheme_ids:
        raise EvaluationDataError(f"unknown run.scheme_id: {scheme_id}")
    task_id = _require_identifier(run.get("task_id"), "run.task_id")
    task_by_id = {item["task_id"]: item for item in suite["tasks"]}
    if task_id not in task_by_id:
        raise EvaluationDataError(f"unknown run.task_id: {task_id}")
    repetition = _require_int(
        run.get("repetition"),
        "run.repetition",
        minimum=1,
        maximum=suite["experiment"]["repetitions_per_task"],
    )
    outcome = _require_choice(run.get("outcome"), OUTCOMES, "run.outcome")
    duration_ms = _require_number(
        run.get("duration_ms"), "run.duration_ms", minimum=0, maximum=86_400_000
    )
    step_count = _require_int(
        run.get("step_count"), "run.step_count", minimum=0, maximum=100_000
    )
    model_calls = _require_int(
        run.get("model_calls"), "run.model_calls", minimum=0, maximum=100_000
    )
    task = task_by_id[task_id]
    if outcome == "success" and step_count > task["step_limit"]:
        raise EvaluationDataError(
            f"successful run exceeds task step limit {task['step_limit']}"
        )

    validation_raw = _copy_mapping(run.get("validation"), "run.validation")
    validation_method = _require_choice(
        validation_raw.get("method"), VALIDATION_METHODS, "run.validation.method"
    )
    validation_passed = _require_bool(
        validation_raw.get("passed"), "run.validation.passed"
    )
    if outcome == "success" and not validation_passed:
        raise EvaluationDataError("successful run requires validation.passed=true")
    if outcome != "success" and validation_passed:
        raise EvaluationDataError(
            "non-successful run cannot set validation.passed=true"
        )
    evidence_ref = _optional_text(
        validation_raw.get("evidence_ref", ""),
        "run.validation.evidence_ref",
        500,
    )
    if record_status == "final" and evidence_ref != "validation_result-001":
        raise EvaluationDataError(
            "final run.validation.evidence_ref must be 'validation_result-001'"
        )
    evaluator_raw = validation_raw.get("evaluator")
    evaluator: dict[str, str] | None = None
    if evaluator_raw is not None:
        evaluator_map = _copy_mapping(evaluator_raw, "run.validation.evaluator")
        evaluator = {
            "provider": _require_text(
                evaluator_map.get("provider"),
                "run.validation.evaluator.provider",
                100,
            ),
            "model": _require_text(
                evaluator_map.get("model"), "run.validation.evaluator.model", 200
            ),
        }
    if validation_method == "model_judge" and evaluator is None:
        raise EvaluationDataError(
            "model_judge validation requires run.validation.evaluator"
        )

    planner_raw = _copy_mapping(run.get("planner"), "run.planner")
    planner = {
        "provider": _require_text(
            planner_raw.get("provider"), "run.planner.provider", 100
        ),
        "model": _require_text(planner_raw.get("model"), "run.planner.model", 200),
        "adapter_prompt_version": _require_text(
            planner_raw.get("adapter_prompt_version"),
            "run.planner.adapter_prompt_version",
            200,
        ),
    }
    task_prompt_version = _require_text(
        run.get("task_prompt_version"), "run.task_prompt_version", 200
    )

    implementation_raw = _copy_mapping(
        run.get("implementation"), "run.implementation"
    )
    implementation = {
        "name": _require_text(
            implementation_raw.get("name"), "run.implementation.name", 200
        ),
        "version": _require_text(
            implementation_raw.get("version"), "run.implementation.version", 200
        ),
        "revision": _require_text(
            implementation_raw.get("revision"),
            "run.implementation.revision",
            200,
        ),
        "branch": _require_text(
            implementation_raw.get("branch"), "run.implementation.branch", 200
        ),
    }

    environment_raw = _copy_mapping(run.get("environment"), "run.environment")
    environment = {
        "environment_id": _require_text(
            environment_raw.get("environment_id"),
            "run.environment.environment_id",
            200,
        ),
        "browser": _require_text(
            environment_raw.get("browser"), "run.environment.browser", 300
        ),
        "viewport": _require_text(
            environment_raw.get("viewport"), "run.environment.viewport", 100
        ),
    }

    if record_status == "final":
        final_fields = [
            planner["provider"],
            planner["model"],
            planner["adapter_prompt_version"],
            environment["environment_id"],
            environment["browser"],
            environment["viewport"],
            implementation["name"],
            implementation["version"],
            implementation["revision"],
            implementation["branch"],
            evidence_ref,
        ]
        if any(_is_placeholder(value) for value in final_fields):
            raise EvaluationDataError(
                "final run still contains fill-/待填写 placeholder values"
            )

    metrics_raw = _copy_mapping(run.get("metrics", {}), "run.metrics")
    metrics = {
        "first_target_hit": _optional_bool(
            metrics_raw.get("first_target_hit"), "run.metrics.first_target_hit"
        ),
        "misclick_count": _metric_count(metrics_raw, "misclick_count"),
        "retry_count": _metric_count(metrics_raw, "retry_count"),
        "loop_count": _metric_count(metrics_raw, "loop_count"),
        "timeout_count": _metric_count(metrics_raw, "timeout_count"),
        "safety_violation_count": _metric_count(
            metrics_raw, "safety_violation_count"
        ),
        "cv_calls": _metric_count(metrics_raw, "cv_calls"),
        "planner_model_calls": _metric_count(
            metrics_raw, "planner_model_calls"
        ),
        "vision_model_calls": _metric_count(metrics_raw, "vision_model_calls"),
        "input_tokens": _metric_count(metrics_raw, "input_tokens"),
        "output_tokens": _metric_count(metrics_raw, "output_tokens"),
        "cost": _require_number(
            metrics_raw.get("cost"),
            "run.metrics.cost",
            minimum=0,
            maximum=1_000_000_000,
        ),
    }
    if model_calls != (
        metrics["planner_model_calls"] + metrics["vision_model_calls"]
    ):
        raise EvaluationDataError(
            "run.model_calls must equal run.metrics.planner_model_calls + "
            "run.metrics.vision_model_calls"
        )

    if "artifacts" in run:
        raise EvaluationDataError(
            "run.artifacts is no longer supported; use run.evidence and archive "
            "role=path inputs"
        )
    evidence = _normalize_evidence(
        run.get("evidence"),
        require_archived=require_archived_evidence,
        scheme_id=scheme_id,
        task_id=task_id,
        repetition=repetition,
    )

    failure_raw = run.get("failure")
    failure: dict[str, str] | None = None
    if failure_raw is not None:
        failure_map = _copy_mapping(failure_raw, "run.failure")
        failure = {
            "category": _require_identifier(
                failure_map.get("category"), "run.failure.category"
            ),
            "reason": _optional_text(
                failure_map.get("reason", ""), "run.failure.reason", 1000
            ),
        }
    if outcome != "success" and failure is None:
        raise EvaluationDataError("non-successful run requires run.failure")

    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "record_status": record_status,
        "run_id": run_id,
        "suite_id": suite_id,
        "scheme_id": scheme_id,
        "task_id": task_id,
        "repetition": repetition,
        "outcome": outcome,
        "started_at": _require_timestamp(run.get("started_at"), "run.started_at"),
        "duration_ms": duration_ms,
        "step_count": step_count,
        "model_calls": model_calls,
        "task_prompt_version": task_prompt_version,
        "implementation": implementation,
        "planner": planner,
        "environment": environment,
        "metrics": metrics,
        "validation": {
            "method": validation_method,
            "passed": validation_passed,
            "evidence_ref": evidence_ref,
            "evaluator": evaluator,
        },
        "failure": failure,
        "evidence": evidence,
        "notes": _optional_text(run.get("notes", ""), "run.notes", 1000),
    }


def validate_run_records(
    records: Iterable[Mapping[str, Any]],
    suite: Mapping[str, Any],
    *,
    require_archived_evidence: bool = False,
) -> list[dict[str, Any]]:
    """Validate all records and reject duplicate run ids or execution keys."""

    normalized: list[dict[str, Any]] = []
    run_ids: set[str] = set()
    keys: set[tuple[str, str, int]] = set()
    for index, record in enumerate(records):
        try:
            run = validate_run_record(
                record,
                suite,
                require_archived_evidence=require_archived_evidence,
            )
        except EvaluationDataError as exc:
            raise EvaluationDataError(f"run record {index + 1}: {exc}") from exc
        run_id_key = run["run_id"].casefold()
        if run_id_key in run_ids:
            raise EvaluationDataError(f"duplicate run_id: {run['run_id']}")
        run_ids.add(run_id_key)
        key = _run_key(run)
        if key in keys:
            raise EvaluationDataError(
                "duplicate execution key: "
                f"scheme={key[0]}, task={key[1]}, repetition={key[2]}"
            )
        keys.add(key)
        normalized.append(run)
    return normalized


def append_run_record(
    runs_path: Path,
    run_payload: Mapping[str, Any],
    suite: Mapping[str, Any],
    *,
    artifact_sources: Sequence[tuple[str, Path]],
) -> dict[str, Any]:
    """Archive immutable evidence, then append its compact run index."""

    run = validate_run_record(
        run_payload,
        suite,
        require_archived_evidence=False,
    )
    if run["evidence"]["status"] != "pending":
        raise EvaluationDataError(
            "new run input must use evidence.status='pending'; archived evidence "
            "is created by append_run_record"
        )
    destination = Path(runs_path).expanduser().resolve()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EvaluationDataError(
            f"cannot create evaluation directory: {destination.parent}"
        ) from exc
    lock_path = destination.with_name(f".{destination.name}.lock")
    lock_descriptor: int | None = None
    try:
        lock_descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        existing = validate_run_records(
            load_run_records(destination),
            suite,
            require_archived_evidence=True,
        )
        existing_ids = {item["run_id"].casefold() for item in existing}
        existing_keys = {_run_key(item) for item in existing}
        if run["run_id"].casefold() in existing_ids:
            raise EvaluationDataError(f"duplicate run_id: {run['run_id']}")
        if _run_key(run) in existing_keys:
            key = _run_key(run)
            raise EvaluationDataError(
                "duplicate execution key: "
                f"scheme={key[0]}, task={key[1]}, repetition={key[2]}"
            )
        try:
            archived_run, _manifest = archive_run_evidence(
                destination.parent,
                suite,
                run,
                artifact_sources,
            )
        except EvaluationArtifactError as exc:
            raise EvaluationDataError(f"cannot archive run evidence: {exc}") from exc

        try:
            archived_run = validate_run_record(
                archived_run,
                suite,
                require_archived_evidence=True,
            )
            line = (
                json.dumps(
                    archived_run,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
            encoded_line = line.encode("utf-8")
            try:
                existing_bytes = destination.read_bytes() if destination.exists() else b""
            except OSError as exc:
                raise EvaluationDataError(
                    f"cannot read run JSONL before append: {destination}"
                ) from exc
            separator = b"" if not existing_bytes or existing_bytes.endswith(b"\n") else b"\n"
            if len(existing_bytes) + len(separator) + len(encoded_line) > MAX_JSONL_BYTES:
                raise EvaluationDataError(
                    f"run JSONL would exceed {MAX_JSONL_BYTES} bytes: {destination}"
                )
            try:
                temporary_path: Path | None = None
                with tempfile.NamedTemporaryFile(
                    mode="w+b",
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    dir=destination.parent,
                    delete=False,
                ) as stream:
                    temporary_path = Path(stream.name)
                    stream.write(existing_bytes)
                    stream.write(separator)
                    stream.write(encoded_line)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, destination)
                temporary_path = None
            except OSError as exc:
                try:
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise EvaluationDataError(
                    f"cannot atomically append run JSONL: {destination}"
                ) from exc
        except (EvaluationDataError, OSError) as exc:
            try:
                remove_new_evidence_archive(destination.parent, archived_run)
            except EvaluationArtifactError as rollback_exc:
                raise EvaluationDataError(
                    "cannot append run JSONL and evidence rollback also failed: "
                    f"{rollback_exc}"
                ) from exc
            if isinstance(exc, EvaluationDataError):
                raise
            raise EvaluationDataError(
                f"cannot append run JSONL: {destination}"
            ) from exc
        return archived_run
    except FileExistsError as exc:
        raise EvaluationDataError(
            f"another process is recording runs (lock exists): {lock_path.name}"
        ) from exc
    except OSError as exc:
        raise EvaluationDataError(
            f"cannot create run recording lock: {lock_path.name}"
        ) from exc
    finally:
        if lock_descriptor is not None:
            try:
                os.close(lock_descriptor)
            except OSError:
                pass
            try:
                lock_path.unlink()
            except OSError:
                pass


def generate_report(
    suite_payload: Mapping[str, Any],
    run_payloads: Iterable[Mapping[str, Any]],
    *,
    generated_at: str | None = None,
    evidence_root: Path | None = None,
    require_evidence: bool = True,
) -> dict[str, Any]:
    """Build a deterministic structured report for all expected executions."""

    suite = validate_suite(suite_payload)
    if require_evidence and evidence_root is None:
        raise EvaluationDataError(
            "evidence_root is required when require_evidence=true"
        )
    runs = validate_run_records(
        run_payloads,
        suite,
        require_archived_evidence=require_evidence,
    )
    timestamp = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    timestamp = _require_timestamp(timestamp, "generated_at")

    evidence_checks, evidence = _verify_report_evidence(
        suite,
        runs,
        evidence_root=evidence_root,
        verification_required=require_evidence,
    )
    evidence_by_run_id = {
        str(check["run_id"]): check for check in evidence_checks
    }

    repetitions = suite["experiment"]["repetitions_per_task"]
    expected_keys = {
        (scheme["scheme_id"], task["task_id"], repetition)
        for scheme in suite["schemes"]
        for task in suite["tasks"]
        for repetition in range(1, repetitions + 1)
    }
    recorded_keys = {_run_key(run) for run in runs}
    missing_keys = sorted(expected_keys - recorded_keys)
    task_by_id = {task["task_id"]: task for task in suite["tasks"]}
    scheme_by_id = {
        scheme["scheme_id"]: scheme for scheme in suite["schemes"]
    }

    overall: list[dict[str, Any]] = []
    breakdowns: list[dict[str, Any]] = []
    for scheme in suite["schemes"]:
        scheme_id = scheme["scheme_id"]
        scheme_runs = [run for run in runs if run["scheme_id"] == scheme_id]
        overall.append(
            _aggregate_runs(
                scheme_id=scheme_id,
                scheme_label=scheme["label"],
                scope_type="overall",
                scope_value="all",
                task_ids=[task["task_id"] for task in suite["tasks"]],
                repetitions=repetitions,
                runs=scheme_runs,
                evidence_checks=evidence_by_run_id,
                evidence_verification_performed=evidence[
                    "verification_performed"
                ],
            )
        )
        for scenario in sorted({task["scenario_type"] for task in suite["tasks"]}):
            ids = [
                task["task_id"]
                for task in suite["tasks"]
                if task["scenario_type"] == scenario
            ]
            breakdowns.append(
                _aggregate_runs(
                    scheme_id=scheme_id,
                    scheme_label=scheme["label"],
                    scope_type="scenario",
                    scope_value=scenario,
                    task_ids=ids,
                    repetitions=repetitions,
                    runs=[run for run in scheme_runs if run["task_id"] in ids],
                    evidence_checks=evidence_by_run_id,
                    evidence_verification_performed=evidence[
                        "verification_performed"
                    ],
                )
            )
        for difficulty in sorted({task["difficulty"] for task in suite["tasks"]}):
            ids = [
                task["task_id"]
                for task in suite["tasks"]
                if task["difficulty"] == difficulty
            ]
            breakdowns.append(
                _aggregate_runs(
                    scheme_id=scheme_id,
                    scheme_label=scheme["label"],
                    scope_type="difficulty",
                    scope_value=difficulty,
                    task_ids=ids,
                    repetitions=repetitions,
                    runs=[run for run in scheme_runs if run["task_id"] in ids],
                    evidence_checks=evidence_by_run_id,
                    evidence_verification_performed=evidence[
                        "verification_performed"
                    ],
                )
            )

    task_summaries: list[dict[str, Any]] = []
    for task in suite["tasks"]:
        for scheme in suite["schemes"]:
            selected = [
                run
                for run in runs
                if run["task_id"] == task["task_id"]
                and run["scheme_id"] == scheme["scheme_id"]
            ]
            successes = [run for run in selected if run["outcome"] == "success"]
            task_summaries.append(
                {
                    "task_id": task["task_id"],
                    "task_title": task["title"],
                    "scenario_type": task["scenario_type"],
                    "difficulty": task["difficulty"],
                    "scheme_id": scheme["scheme_id"],
                    "scheme_label": scheme["label"],
                    "expected_runs": repetitions,
                    "recorded_runs": len(selected),
                    "success_count": len(successes),
                    "strict_success_rate": _ratio(len(successes), repetitions),
                    "average_success_duration_ms": _mean_or_none(
                        [run["duration_ms"] for run in successes]
                    ),
                    "outcomes": dict(Counter(run["outcome"] for run in selected)),
                }
            )

    warnings, fairness = _fairness_analysis(suite, runs, missing_keys)
    if not evidence["verification_performed"]:
        warnings.append(
            "未提供证据目录，Manifest 与文件哈希未校验；本报告只可查看指标，不能排名。"
        )
    if evidence["verification_performed"] and evidence["failed_runs"]:
        warnings.append(
            f"有 {evidence['failed_runs']} 次运行的原始证据归档不完整或哈希校验失败，"
            "当前报告不能作为方案排名依据。"
        )
        warnings.extend(
            f"证据校验失败 {check['run_id']}：{'; '.join(check['errors'])}"
            for check in evidence["run_checks"]
            if check["verified"] is False
        )
    failures = [
        {
            "run_id": run["run_id"],
            "scheme_id": run["scheme_id"],
            "scheme_label": scheme_by_id[run["scheme_id"]]["label"],
            "task_id": run["task_id"],
            "task_title": task_by_id[run["task_id"]]["title"],
            "repetition": run["repetition"],
            "outcome": run["outcome"],
            "failure_category": (
                run["failure"]["category"] if run["failure"] else ""
            ),
            "safety_violation_count": run["metrics"]["safety_violation_count"],
        }
        for run in runs
        if run["outcome"] != "success"
        or run["metrics"]["safety_violation_count"] > 0
    ]
    conclusion = _build_conclusion(overall, fairness, evidence)

    report_status = "complete"
    if missing_keys:
        report_status = "incomplete"
    if not fairness["fair_comparison"]:
        report_status = "fairness_blocked"
    if sum(metric["safety_violation_count"] for metric in overall) > 0:
        report_status = "safety_warning"
    if evidence["ranking_blocked"]:
        report_status = "evidence_blocked"

    normalized_runs = [
        _report_safe_run(run)
        for run in sorted(
            runs,
            key=lambda item: (
                item["scheme_id"],
                item["task_id"],
                item["repetition"],
            ),
        )
    ]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": timestamp,
        "report_status": report_status,
        "suite": suite,
        "coverage": {
            "expected_runs": len(expected_keys),
            "recorded_runs": len(runs),
            "missing_runs": len(missing_keys),
            "coverage_rate": _ratio(len(runs), len(expected_keys)),
            "missing_execution_keys": [
                {
                    "scheme_id": scheme_id,
                    "task_id": task_id,
                    "repetition": repetition,
                }
                for scheme_id, task_id, repetition in missing_keys
            ],
        },
        "fairness": fairness,
        "evidence": evidence,
        "warnings": warnings,
        "overall_metrics": overall,
        "breakdown_metrics": breakdowns,
        "task_summaries": task_summaries,
        "failures": failures,
        "conclusion": conclusion,
        "runs": normalized_runs,
    }


def write_report_bundle(report: Mapping[str, Any], output_dir: Path) -> list[Path]:
    """Stage and atomically publish a new, non-overwriting report directory."""

    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise EvaluationDataError("unsupported report schema_version")
    destination = Path(output_dir).expanduser().resolve()
    files = {
        "report.json": json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n",
        "metrics.csv": render_metrics_csv(report),
        "runs.csv": render_runs_csv(report),
        "report.md": render_markdown_report(report),
        "report.html": render_html_report(report),
        "issues.md": render_issues_markdown(report),
        "conclusion.md": render_conclusion_markdown(report),
    }
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EvaluationDataError(
            f"cannot create report parent directory: {destination.parent}"
        ) from exc
    if os.path.lexists(destination):
        raise EvaluationDataError(
            f"refusing to overwrite existing report directory: {destination}"
        )
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.",
                dir=destination.parent,
            )
        )
        for name, content in files.items():
            _atomic_write_text(temporary_dir / name, content)
        if os.path.lexists(destination):
            raise EvaluationDataError(
                f"refusing to overwrite existing report directory: {destination}"
            )
        os.rename(temporary_dir, destination)
        temporary_dir = None
    except EvaluationDataError:
        raise
    except OSError as exc:
        raise EvaluationDataError(
            f"cannot publish report directory: {destination}"
        ) from exc
    finally:
        if temporary_dir is not None and temporary_dir.exists():
            shutil.rmtree(temporary_dir, ignore_errors=True)
    return [destination / name for name in files]


def render_metrics_csv(report: Mapping[str, Any]) -> str:
    fields = [
        "scheme_id",
        "scheme_label",
        "scope_type",
        "scope_value",
        "expected_runs",
        "recorded_runs",
        "coverage_rate",
        "success_count",
        "strict_success_rate",
        "observed_success_rate",
        "full_repeat_success_rate",
        "average_success_duration_ms",
        "p50_success_duration_ms",
        "p95_success_duration_ms",
        "average_success_steps",
        "average_model_calls",
        "average_cv_calls",
        "average_planner_model_calls",
        "average_vision_model_calls",
        "first_target_hit_rate",
        "misclick_run_rate",
        "loop_run_rate",
        "timeout_run_rate",
        "retry_count",
        "safety_violation_count",
        "total_input_tokens",
        "total_output_tokens",
        "total_cost",
        "evidence_verification_performed",
        "evidence_verified_runs",
        "evidence_failed_runs",
        "evidence_verification_rate",
        "evidence_artifact_count",
        "evidence_total_bytes",
        "average_evidence_archive_duration_ms",
        "evidence_complete",
        "eligible_for_ranking",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for metric in [*report["overall_metrics"], *report["breakdown_metrics"]]:
        writer.writerow({field: _csv_value(metric.get(field)) for field in fields})
    return stream.getvalue()


def render_runs_csv(report: Mapping[str, Any]) -> str:
    fields = [
        "run_id",
        "scheme_id",
        "task_id",
        "repetition",
        "outcome",
        "duration_ms",
        "step_count",
        "model_calls",
        "cv_calls",
        "planner_model_calls",
        "vision_model_calls",
        "implementation_name",
        "implementation_version",
        "implementation_revision",
        "implementation_branch",
        "planner_provider",
        "planner_model",
        "task_prompt_version",
        "environment_id",
        "validation_method",
        "validation_passed",
        "first_target_hit",
        "misclick_count",
        "retry_count",
        "loop_count",
        "timeout_count",
        "safety_violation_count",
        "input_tokens",
        "output_tokens",
        "cost",
        "failure_category",
        "evidence_status",
        "evidence_manifest_ref",
        "evidence_manifest_sha256",
        "evidence_artifact_count",
        "evidence_total_bytes",
        "evidence_archive_duration_ms",
        "evidence_verified",
        "evidence_errors",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    evidence_by_run = {
        check["run_id"]: check for check in report["evidence"]["run_checks"]
    }
    for run in report["runs"]:
        failure = run.get("failure") or {}
        evidence = run["evidence"]
        evidence_check = evidence_by_run.get(run["run_id"], {})
        row = {
            "run_id": run["run_id"],
            "scheme_id": run["scheme_id"],
            "task_id": run["task_id"],
            "repetition": run["repetition"],
            "outcome": run["outcome"],
            "duration_ms": run["duration_ms"],
            "step_count": run["step_count"],
            "model_calls": run["model_calls"],
            "cv_calls": run["metrics"]["cv_calls"],
            "planner_model_calls": run["metrics"]["planner_model_calls"],
            "vision_model_calls": run["metrics"]["vision_model_calls"],
            "implementation_name": run["implementation"]["name"],
            "implementation_version": run["implementation"]["version"],
            "implementation_revision": run["implementation"]["revision"],
            "implementation_branch": run["implementation"]["branch"],
            "planner_provider": run["planner"]["provider"],
            "planner_model": run["planner"]["model"],
            "task_prompt_version": run["task_prompt_version"],
            "environment_id": run["environment"]["environment_id"],
            "validation_method": run["validation"]["method"],
            "validation_passed": run["validation"]["passed"],
            "first_target_hit": run["metrics"]["first_target_hit"],
            "misclick_count": run["metrics"]["misclick_count"],
            "retry_count": run["metrics"]["retry_count"],
            "loop_count": run["metrics"]["loop_count"],
            "timeout_count": run["metrics"]["timeout_count"],
            "safety_violation_count": run["metrics"]["safety_violation_count"],
            "input_tokens": run["metrics"]["input_tokens"],
            "output_tokens": run["metrics"]["output_tokens"],
            "cost": run["metrics"]["cost"],
            "failure_category": failure.get("category", ""),
            "evidence_status": evidence["status"],
            "evidence_manifest_ref": evidence.get("manifest_ref", ""),
            "evidence_manifest_sha256": evidence.get("manifest_sha256", ""),
            "evidence_artifact_count": evidence.get("artifact_count", 0),
            "evidence_total_bytes": evidence.get("total_bytes", 0),
            "evidence_archive_duration_ms": evidence.get(
                "archive_duration_ms", 0
            ),
            "evidence_verified": evidence_check.get("verified"),
            "evidence_errors": "; ".join(evidence_check.get("errors", [])),
        }
        writer.writerow({field: _csv_value(row.get(field)) for field in fields})
    return stream.getvalue()


def render_markdown_report(report: Mapping[str, Any]) -> str:
    suite = report["suite"]
    coverage = report["coverage"]
    lines = [
        f"# {_md_text(suite['title'])}评测报告",
        "",
        f"- 报告状态：`{report['report_status']}`",
        f"- 生成时间：`{report['generated_at']}`",
        f"- 任务数：{len(suite['tasks'])}",
        f"- 每任务重复次数：{suite['experiment']['repetitions_per_task']}",
        f"- 运行覆盖：{coverage['recorded_runs']}/{coverage['expected_runs']} "
        f"({_percent(coverage['coverage_rate'])})",
        f"- 证据校验：{report['evidence']['verified_runs']}/"
        f"{report['evidence']['recorded_runs']} "
        f"({_percent(report['evidence']['verification_rate'])})",
        "",
        "## 核心结果",
        "",
        "| 方案 | 覆盖率 | 完成率（严格） | 成功耗时均值 | P95耗时 | 平均步数 | CV调用 | 规划模型调用 | 视觉模型调用 | 证据通过率 | 安全违规 | 成本 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for metric in report["overall_metrics"]:
        lines.append(
            "| {label} | {coverage} | {success} | {avg} | {p95} | {steps} | "
            "{cv} | {planner_calls} | {vision_calls} | {evidence} | {safety} | "
            "{cost} |".format(
                label=_md_cell(metric["scheme_label"]),
                coverage=_percent(metric["coverage_rate"]),
                success=_percent(metric["strict_success_rate"]),
                avg=_milliseconds(metric["average_success_duration_ms"]),
                p95=_milliseconds(metric["p95_success_duration_ms"]),
                steps=_number(metric["average_success_steps"]),
                cv=_number(metric["average_cv_calls"]),
                planner_calls=_number(metric["average_planner_model_calls"]),
                vision_calls=_number(metric["average_vision_model_calls"]),
                evidence=_percent(metric["evidence_verification_rate"]),
                safety=metric["safety_violation_count"],
                cost=_number(metric["total_cost"], digits=4),
            )
        )
    lines.extend(
        [
            "",
            "## 分类结果",
            "",
            "| 方案 | 分类维度 | 分类 | 覆盖率 | 严格完成率 | 平均成功耗时 |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    for metric in report["breakdown_metrics"]:
        lines.append(
            f"| {_md_cell(metric['scheme_label'])} | {metric['scope_type']} | "
            f"{_md_cell(metric['scope_value'])} | {_percent(metric['coverage_rate'])} | "
            f"{_percent(metric['strict_success_rate'])} | "
            f"{_milliseconds(metric['average_success_duration_ms'])} |"
        )
    lines.extend(
        [
            "",
            "## 原始证据完整性",
            "",
            "| 方案 | 已记录 | 校验通过 | 校验失败 | 通过率 | 文件数 | 归档大小 | 平均归档耗时 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in report["evidence"]["by_scheme"]:
        lines.append(
            f"| {_md_cell(item['scheme_label'])} | {item['recorded_runs']} | "
            f"{item['verified_runs']} | {item['failed_runs']} | "
            f"{_percent(item['verification_rate'])} | {item['artifact_count']} | "
            f"{_bytes(item['total_bytes'])} | "
            f"{_milliseconds(item['average_archive_duration_ms'])} |"
        )
    if not report["evidence"]["verification_performed"]:
        lines.extend(
            [
                "",
                "本次为未指定证据目录的内存聚合，未执行 manifest 与文件哈希校验。",
            ]
        )
    lines.extend(["", "## 公平性与数据质量", ""])
    if report["warnings"]:
        lines.extend(f"- {_md_text(warning)}" for warning in report["warnings"])
    else:
        lines.append("- 未发现缺失运行或统一模型、任务提示、环境不一致问题。")
    lines.extend(
        [
            "",
            "## 结论",
            "",
            _md_text(report["conclusion"]["summary"]),
            "",
            "详细失败记录见 `issues.md`，逐次运行数据见 `runs.csv`，机器可读结果见 `report.json`。",
            "",
        ]
    )
    return "\n".join(lines)


def render_issues_markdown(report: Mapping[str, Any]) -> str:
    lines = ["# 评测问题清单", ""]
    evidence_failures = [
        check
        for check in report["evidence"]["run_checks"]
        if check["verified"] is False
    ]
    if report["coverage"]["missing_runs"]:
        lines.extend(
            [
                "## 缺失运行",
                "",
                "| 方案 | 任务 | 重复序号 |",
                "|---|---|---:|",
            ]
        )
        for item in report["coverage"]["missing_execution_keys"]:
            lines.append(
                f"| {_md_cell(item['scheme_id'])} | {_md_cell(item['task_id'])} | "
                f"{item['repetition']} |"
            )
        lines.append("")
    if report["warnings"]:
        lines.extend(["## 数据质量警告", ""])
        lines.extend(f"- {_md_text(warning)}" for warning in report["warnings"])
        lines.append("")
    if evidence_failures:
        lines.extend(
            [
                "## 原始证据校验失败",
                "",
                "| 方案 | 任务 | 次数 | Manifest | 错误 |",
                "|---|---|---:|---|---|",
            ]
        )
        for check in evidence_failures:
            lines.append(
                f"| {_md_cell(check['scheme_id'])} | "
                f"{_md_cell(check['task_id'])} | {check['repetition']} | "
                f"{_md_cell(check['manifest_ref'])} | "
                f"{_md_cell('; '.join(check['errors']))} |"
            )
        lines.append("")
    if report["failures"]:
        lines.extend(
            [
                "## 失败及安全问题",
                "",
                "| 方案 | 任务 | 次数 | 结果 | 类型 | 安全违规 |",
                "|---|---|---:|---|---|---:|",
            ]
        )
        for failure in report["failures"]:
            lines.append(
                f"| {_md_cell(failure['scheme_label'])} | "
                f"{_md_cell(failure['task_id'])} | {failure['repetition']} | "
                f"{failure['outcome']} | {_md_cell(failure['failure_category'])} | "
                f"{failure['safety_violation_count']} |"
            )
        lines.append("")
    if (
        not report["coverage"]["missing_runs"]
        and not report["warnings"]
        and not evidence_failures
        and not report["failures"]
    ):
        lines.append("未发现缺失运行、数据质量警告、失败任务或安全违规。")
        lines.append("")
    return "\n".join(lines)


def render_conclusion_markdown(report: Mapping[str, Any]) -> str:
    conclusion = report["conclusion"]
    lines = [
        "# 评测结论",
        "",
        f"- 决策状态：`{conclusion['decision']}`",
        f"- 推荐方案：`{conclusion.get('recommended_scheme_id') or '暂无'}`",
        "",
        _md_text(conclusion["summary"]),
        "",
    ]
    if conclusion["ranking"]:
        lines.extend(["## 排名依据", ""])
        for index, item in enumerate(conclusion["ranking"], start=1):
            lines.append(
                f"{index}. {_md_text(item['scheme_label'])}：严格完成率 "
                f"{_percent(item['strict_success_rate'])}，P95成功耗时 "
                f"{_milliseconds(item['p95_success_duration_ms'])}。"
            )
        lines.append("")
    lines.extend(
        [
            "安全性是硬门槛；完成率优先于耗时，完成率接近时再比较稳定性、耗时和成本。",
            "",
        ]
    )
    return "\n".join(lines)


def render_html_report(report: Mapping[str, Any]) -> str:
    suite = report["suite"]
    rows = []
    for metric in report["overall_metrics"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(metric['scheme_label'])}</td>"
            f"<td>{_percent(metric['coverage_rate'])}</td>"
            f"<td>{_percent(metric['strict_success_rate'])}</td>"
            f"<td>{_milliseconds(metric['average_success_duration_ms'])}</td>"
            f"<td>{_milliseconds(metric['p95_success_duration_ms'])}</td>"
            f"<td>{_number(metric['average_success_steps'])}</td>"
            f"<td>{_number(metric['average_cv_calls'])}</td>"
            f"<td>{_number(metric['average_planner_model_calls'])}</td>"
            f"<td>{_number(metric['average_vision_model_calls'])}</td>"
            f"<td>{_percent(metric['evidence_verification_rate'])}</td>"
            f"<td>{metric['safety_violation_count']}</td>"
            "</tr>"
        )
    evidence_rows = []
    for item in report["evidence"]["by_scheme"]:
        evidence_rows.append(
            "<tr>"
            f"<td>{html.escape(item['scheme_label'])}</td>"
            f"<td>{item['recorded_runs']}</td>"
            f"<td>{item['verified_runs']}</td>"
            f"<td>{item['failed_runs']}</td>"
            f"<td>{_percent(item['verification_rate'])}</td>"
            f"<td>{item['artifact_count']}</td>"
            f"<td>{_bytes(item['total_bytes'])}</td>"
            f"<td>{_milliseconds(item['average_archive_duration_ms'])}</td>"
            "</tr>"
        )
    warnings = "".join(
        f"<li>{html.escape(item)}</li>" for item in report["warnings"]
    ) or "<li>未发现公平性或覆盖率问题。</li>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(suite['title'])}评测报告</title>
  <style>
    body {{ font-family: system-ui, "Microsoft YaHei", sans-serif; margin: 32px; color: #17202a; }}
    .summary {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 20px 0; }}
    .card {{ border: 1px solid #d5d8dc; border-radius: 8px; padding: 12px 16px; min-width: 150px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 16px 0 28px; }}
    th, td {{ border: 1px solid #d5d8dc; padding: 8px; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ background: #f4f6f7; }}
    .status {{ font-weight: 700; }}
  </style>
</head>
<body>
  <h1>{html.escape(suite['title'])}评测报告</h1>
  <p class="status">状态：{html.escape(report['report_status'])}</p>
  <div class="summary">
    <div class="card">任务数<br><strong>{len(suite['tasks'])}</strong></div>
    <div class="card">已记录/应记录<br><strong>{report['coverage']['recorded_runs']}/{report['coverage']['expected_runs']}</strong></div>
    <div class="card">覆盖率<br><strong>{_percent(report['coverage']['coverage_rate'])}</strong></div>
    <div class="card">证据校验通过<br><strong>{report['evidence']['verified_runs']}/{report['evidence']['recorded_runs']}</strong></div>
  </div>
  <h2>核心结果</h2>
  <table>
    <thead><tr><th>方案</th><th>覆盖率</th><th>严格完成率</th><th>平均成功耗时</th><th>P95耗时</th><th>平均步数</th><th>CV调用</th><th>规划模型调用</th><th>视觉模型调用</th><th>证据通过率</th><th>安全违规</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <h2>原始证据完整性</h2>
  <table>
    <thead><tr><th>方案</th><th>已记录</th><th>校验通过</th><th>校验失败</th><th>通过率</th><th>文件数</th><th>归档大小</th><th>平均归档耗时</th></tr></thead>
    <tbody>{''.join(evidence_rows)}</tbody>
  </table>
  <h2>公平性与数据质量</h2>
  <ul>{warnings}</ul>
  <h2>结论</h2>
  <p>{html.escape(report['conclusion']['summary'])}</p>
</body>
</html>
"""


def build_suite_template(
    *,
    suite_id: str,
    title: str,
    task_count: int = 28,
    repetitions: int = 3,
    step_limit: int = 10,
    planner_provider: str = "deepseek",
    planner_model: str = "fill-exact-model-name",
    environment_id: str = "fill-test-environment-id",
) -> dict[str, Any]:
    """Create a deliberately draft three-scheme suite template."""

    if task_count < 1 or task_count > MAX_TASKS:
        raise EvaluationDataError(f"task_count must be between 1 and {MAX_TASKS}")
    if repetitions < 1 or repetitions > MAX_REPETITIONS:
        raise EvaluationDataError(
            f"repetitions must be between 1 and {MAX_REPETITIONS}"
        )
    if step_limit < 1:
        raise EvaluationDataError("step_limit must be positive")
    tasks = [
        {
            "task_id": f"T{index:02d}",
            "title": f"待填写任务 {index:02d}",
            "scenario_type": "dom",
            "difficulty": "unknown",
            "step_limit": step_limit,
            "validation": {
                "method": "page_state",
                "description": "待填写确定性成功条件",
            },
            "tags": [],
        }
        for index in range(1, task_count + 1)
    ]
    return {
        "schema_version": SUITE_SCHEMA_VERSION,
        "suite_id": _require_identifier(suite_id, "suite_id"),
        "title": _require_text(title, "title", 300),
        "description": "现有方案、Browser Use 与 UI-TARS 三组统一评测。",
        "status": "draft",
        "experiment": {
            "repetitions_per_task": repetitions,
            "default_step_limit": step_limit,
            "task_prompt_version": "v1",
            "environment_id": environment_id,
            "require_shared_planner": True,
            "shared_planner": {
                "provider": planner_provider,
                "model": planner_model,
            },
        },
        "schemes": [
            {
                "scheme_id": "current",
                "label": "现有方案",
                "perception": "DOM + OpenCV/OCR + UI Graph",
                "planner": f"{planner_provider} API",
                "executor": "Playwright",
                "evidence_profile": "current_hybrid",
                "required_artifact_groups": [],
            },
            {
                "scheme_id": "browser_use",
                "label": "Browser Use",
                "perception": "Browser Use CDP/DOM",
                "planner": f"{planner_provider} API",
                "executor": "Browser Use/CDP",
                "evidence_profile": "browser_use",
                "required_artifact_groups": [],
            },
            {
                "scheme_id": "ui_tars",
                "label": "UI-TARS",
                "perception": "UI-TARS screenshot grounding",
                "planner": f"{planner_provider} API",
                "executor": "Playwright",
                "evidence_profile": "ui_tars",
                "required_artifact_groups": [],
            },
        ],
        "tasks": tasks,
    }


def build_run_template(
    suite: Mapping[str, Any],
    *,
    scheme_id: str,
    task_id: str,
    repetition: int,
) -> dict[str, Any]:
    """Create a draft run record that must be completed before import."""

    normalized_suite = validate_suite(suite, require_ready=False)
    scheme_ids = {item["scheme_id"] for item in normalized_suite["schemes"]}
    task_ids = {item["task_id"] for item in normalized_suite["tasks"]}
    if scheme_id not in scheme_ids:
        raise EvaluationDataError(f"unknown scheme_id: {scheme_id}")
    if task_id not in task_ids:
        raise EvaluationDataError(f"unknown task_id: {task_id}")
    if repetition < 1 or repetition > normalized_suite["experiment"]["repetitions_per_task"]:
        raise EvaluationDataError("repetition is outside suite range")
    planner = normalized_suite["experiment"].get("shared_planner") or {
        "provider": "fill-provider",
        "model": "fill-model",
    }
    environment_id = normalized_suite["experiment"].get("environment_id") or "fill-environment"
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "record_status": "draft",
        "run_id": f"{scheme_id}-{task_id}-r{repetition}",
        "suite_id": normalized_suite["suite_id"],
        "scheme_id": scheme_id,
        "task_id": task_id,
        "repetition": repetition,
        "outcome": "failure",
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration_ms": 0,
        "step_count": 0,
        "model_calls": 0,
        "task_prompt_version": normalized_suite["experiment"]["task_prompt_version"],
        "implementation": {
            "name": "fill-implementation-name",
            "version": "fill-implementation-version",
            "revision": "fill-git-revision-or-build-id",
            "branch": "fill-branch-or-release-channel",
        },
        "planner": {
            "provider": planner["provider"],
            "model": planner["model"],
            "adapter_prompt_version": "fill-adapter-prompt-version",
        },
        "environment": {
            "environment_id": environment_id,
            "browser": "fill-browser-version",
            "viewport": "1920x1080",
        },
        "metrics": {
            "first_target_hit": None,
            "misclick_count": 0,
            "retry_count": 0,
            "loop_count": 0,
            "timeout_count": 0,
            "safety_violation_count": 0,
            "cv_calls": 0,
            "planner_model_calls": 0,
            "vision_model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0,
        },
        "validation": {
            "method": "page_state",
            "passed": False,
            "evidence_ref": "validation_result-001",
        },
        "failure": {
            "category": "not_run",
            "reason": "Complete this draft after the real execution.",
        },
        "evidence": {"status": "pending"},
        "notes": "",
    }


def _verify_report_evidence(
    suite: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
    *,
    evidence_root: Path | None,
    verification_required: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    verification_performed = evidence_root is not None
    resolved_root = (
        Path(evidence_root).expanduser().resolve()
        if evidence_root is not None
        else None
    )
    checks: list[dict[str, Any]] = []
    for run in runs:
        if resolved_root is None:
            index = run.get("evidence", {})
            checks.append(
                {
                    "run_id": run["run_id"],
                    "scheme_id": run["scheme_id"],
                    "task_id": run["task_id"],
                    "repetition": run["repetition"],
                    "verified": None,
                    "manifest_ref": index.get("manifest_ref", ""),
                    "manifest_sha256": index.get("manifest_sha256", ""),
                    "artifact_count": index.get("artifact_count", 0),
                    "total_bytes": index.get("total_bytes", 0),
                    "archive_duration_ms": index.get("archive_duration_ms", 0),
                    "present_roles": [],
                    "errors": [],
                }
            )
            continue
        try:
            raw_check = verify_run_evidence(resolved_root, suite, run)
        except (EvaluationArtifactError, OSError) as exc:
            raw_check = {
                "run_id": run["run_id"],
                "verified": False,
                "manifest_ref": run["evidence"].get("manifest_ref", ""),
                "manifest_sha256": "",
                "artifact_count": 0,
                "total_bytes": 0,
                "archive_duration_ms": run["evidence"].get(
                    "archive_duration_ms", 0
                ),
                "present_roles": [],
                "errors": [str(exc)],
            }
        errors = [
            _sanitize_evidence_error(str(error), resolved_root)
            for error in raw_check.get("errors", [])
        ]
        checks.append(
            {
                "run_id": run["run_id"],
                "scheme_id": run["scheme_id"],
                "task_id": run["task_id"],
                "repetition": run["repetition"],
                "verified": raw_check.get("verified") is True and not errors,
                "manifest_ref": str(raw_check.get("manifest_ref", "")),
                "manifest_sha256": str(
                    raw_check.get("manifest_sha256", "")
                ),
                "artifact_count": int(raw_check.get("artifact_count", 0)),
                "total_bytes": int(raw_check.get("total_bytes", 0)),
                "archive_duration_ms": run["evidence"].get(
                    "archive_duration_ms", 0
                ),
                "present_roles": sorted(
                    str(role) for role in raw_check.get("present_roles", [])
                ),
                "errors": errors,
            }
        )

    checks.sort(
        key=lambda item: (
            item["scheme_id"],
            item["task_id"],
            item["repetition"],
        )
    )
    verified_runs = sum(check["verified"] is True for check in checks)
    failed_runs = sum(check["verified"] is False for check in checks)
    not_checked_runs = sum(check["verified"] is None for check in checks)
    by_scheme: list[dict[str, Any]] = []
    for scheme in suite["schemes"]:
        selected = [
            check
            for check in checks
            if check["scheme_id"] == scheme["scheme_id"]
        ]
        selected_verified = sum(check["verified"] is True for check in selected)
        selected_failed = sum(check["verified"] is False for check in selected)
        by_scheme.append(
            {
                "scheme_id": scheme["scheme_id"],
                "scheme_label": scheme["label"],
                "recorded_runs": len(selected),
                "verified_runs": selected_verified,
                "failed_runs": selected_failed,
                "not_checked_runs": sum(
                    check["verified"] is None for check in selected
                ),
                "verification_rate": (
                    _ratio(selected_verified, len(selected))
                    if verification_performed
                    else None
                ),
                "artifact_count": sum(
                    check["artifact_count"] for check in selected
                ),
                "total_bytes": sum(check["total_bytes"] for check in selected),
                "average_archive_duration_ms": _mean_or_none(
                    [float(check["archive_duration_ms"]) for check in selected]
                ),
            }
        )

    fully_verified = (
        verification_performed
        and failed_runs == 0
        and not_checked_runs == 0
        and verified_runs == len(runs)
    )
    status = "not_checked"
    if verification_performed:
        status = "verified" if fully_verified else "failed"
    summary = {
        "status": status,
        "verification_required": verification_required,
        "verification_performed": verification_performed,
        "recorded_runs": len(runs),
        "verified_runs": verified_runs,
        "failed_runs": failed_runs,
        "not_checked_runs": not_checked_runs,
        "verification_rate": (
            _ratio(verified_runs, len(runs)) if verification_performed else None
        ),
        "artifact_count": sum(check["artifact_count"] for check in checks),
        "total_bytes": sum(check["total_bytes"] for check in checks),
        "average_archive_duration_ms": _mean_or_none(
            [float(check["archive_duration_ms"]) for check in checks]
        ),
        "fully_verified": fully_verified,
        "ranking_blocked": not fully_verified,
        "by_scheme": by_scheme,
        "run_checks": checks,
    }
    return checks, summary


def _aggregate_runs(
    *,
    scheme_id: str,
    scheme_label: str,
    scope_type: str,
    scope_value: str,
    task_ids: Sequence[str],
    repetitions: int,
    runs: Sequence[Mapping[str, Any]],
    evidence_checks: Mapping[str, Mapping[str, Any]],
    evidence_verification_performed: bool,
) -> dict[str, Any]:
    expected = len(task_ids) * repetitions
    success_runs = [run for run in runs if run["outcome"] == "success"]
    outcome_counts = Counter(run["outcome"] for run in runs)
    success_durations = [float(run["duration_ms"]) for run in success_runs]
    all_durations = [float(run["duration_ms"]) for run in runs]
    hit_values = [
        run["metrics"]["first_target_hit"]
        for run in runs
        if run["metrics"]["first_target_hit"] is not None
    ]
    fully_successful_tasks = 0
    for task_id in task_ids:
        task_runs = [run for run in runs if run["task_id"] == task_id]
        if len(task_runs) == repetitions and all(
            run["outcome"] == "success" for run in task_runs
        ):
            fully_successful_tasks += 1
    safety_count = sum(
        run["metrics"]["safety_violation_count"] for run in runs
    )
    selected_evidence = [
        evidence_checks[run["run_id"]]
        for run in runs
        if run["run_id"] in evidence_checks
    ]
    evidence_verified_runs = sum(
        check.get("verified") is True for check in selected_evidence
    )
    evidence_failed_runs = sum(
        check.get("verified") is False for check in selected_evidence
    )
    evidence_complete: bool | None = None
    if evidence_verification_performed:
        evidence_complete = (
            len(selected_evidence) == len(runs)
            and evidence_verified_runs == len(runs)
        )
    coverage = _ratio(len(runs), expected)
    return {
        "scheme_id": scheme_id,
        "scheme_label": scheme_label,
        "scope_type": scope_type,
        "scope_value": scope_value,
        "task_count": len(task_ids),
        "expected_runs": expected,
        "recorded_runs": len(runs),
        "coverage_rate": coverage,
        "success_count": outcome_counts["success"],
        "partial_count": outcome_counts["partial"],
        "failure_count": outcome_counts["failure"],
        "timeout_count": outcome_counts["timeout"],
        "blocked_count": outcome_counts["blocked"],
        "strict_success_rate": _ratio(outcome_counts["success"], expected),
        "observed_success_rate": _ratio(outcome_counts["success"], len(runs)),
        "full_repeat_success_count": fully_successful_tasks,
        "full_repeat_success_rate": _ratio(fully_successful_tasks, len(task_ids)),
        "average_success_duration_ms": _mean_or_none(success_durations),
        "p50_success_duration_ms": _percentile(success_durations, 50),
        "p95_success_duration_ms": _percentile(success_durations, 95),
        "average_all_duration_ms": _mean_or_none(all_durations),
        "average_success_steps": _mean_or_none(
            [float(run["step_count"]) for run in success_runs]
        ),
        "average_model_calls": _mean_or_none(
            [float(run["model_calls"]) for run in runs]
        ),
        "average_cv_calls": _mean_or_none(
            [float(run["metrics"]["cv_calls"]) for run in runs]
        ),
        "average_planner_model_calls": _mean_or_none(
            [float(run["metrics"]["planner_model_calls"]) for run in runs]
        ),
        "average_vision_model_calls": _mean_or_none(
            [float(run["metrics"]["vision_model_calls"]) for run in runs]
        ),
        "first_target_hit_rate": _ratio(
            sum(value is True for value in hit_values), len(hit_values)
        ),
        "first_target_hit_observations": len(hit_values),
        "misclick_count": sum(run["metrics"]["misclick_count"] for run in runs),
        "misclick_run_rate": _ratio(
            sum(run["metrics"]["misclick_count"] > 0 for run in runs), len(runs)
        ),
        "retry_count": sum(run["metrics"]["retry_count"] for run in runs),
        "loop_count": sum(run["metrics"]["loop_count"] for run in runs),
        "loop_run_rate": _ratio(
            sum(run["metrics"]["loop_count"] > 0 for run in runs), len(runs)
        ),
        "timeout_run_rate": _ratio(
            sum(
                run["outcome"] == "timeout"
                or run["metrics"]["timeout_count"] > 0
                for run in runs
            ),
            len(runs),
        ),
        "safety_violation_count": safety_count,
        "safety_violation_run_rate": _ratio(
            sum(
                run["metrics"]["safety_violation_count"] > 0 for run in runs
            ),
            len(runs),
        ),
        "total_input_tokens": sum(run["metrics"]["input_tokens"] for run in runs),
        "total_output_tokens": sum(run["metrics"]["output_tokens"] for run in runs),
        "total_cost": round(sum(run["metrics"]["cost"] for run in runs), 8),
        "average_cost": _mean_or_none(
            [float(run["metrics"]["cost"]) for run in runs]
        ),
        "failure_categories": dict(
            Counter(
                run["failure"]["category"]
                for run in runs
                if run.get("failure") is not None
            )
        ),
        "evidence_verification_performed": evidence_verification_performed,
        "evidence_verified_runs": evidence_verified_runs,
        "evidence_failed_runs": evidence_failed_runs,
        "evidence_verification_rate": (
            _ratio(evidence_verified_runs, len(runs))
            if evidence_verification_performed
            else None
        ),
        "evidence_artifact_count": sum(
            int(check.get("artifact_count", 0)) for check in selected_evidence
        ),
        "evidence_total_bytes": sum(
            int(check.get("total_bytes", 0)) for check in selected_evidence
        ),
        "average_evidence_archive_duration_ms": _mean_or_none(
            [
                float(check.get("archive_duration_ms", 0))
                for check in selected_evidence
            ]
        ),
        "evidence_complete": evidence_complete,
        "eligible_for_ranking": (
            coverage == 1.0
            and safety_count == 0
            and evidence_complete is True
        ),
    }


def _fairness_analysis(
    suite: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
    missing_keys: Sequence[tuple[str, str, int]],
) -> tuple[list[str], dict[str, Any]]:
    experiment = suite["experiment"]
    warnings: list[str] = []
    hard_issues: list[str] = []
    planner_models: dict[str, list[str]] = {}
    adapter_prompt_versions: dict[str, list[str]] = {}
    environments: dict[str, list[str]] = {}
    environment_details: dict[str, list[str]] = {}
    prompt_versions: dict[str, list[str]] = {}
    implementations: dict[str, list[str]] = {}
    expected_validation_methods = {
        task["task_id"]: task["validation"]["method"]
        for task in suite["tasks"]
    }
    for scheme in suite["schemes"]:
        scheme_id = scheme["scheme_id"]
        selected = [run for run in runs if run["scheme_id"] == scheme_id]
        planner_models[scheme_id] = sorted(
            {
                f"{run['planner']['provider']}/{run['planner']['model']}"
                for run in selected
            }
        )
        adapter_prompt_versions[scheme_id] = sorted(
            {run["planner"]["adapter_prompt_version"] for run in selected}
        )
        environments[scheme_id] = sorted(
            {run["environment"]["environment_id"] for run in selected}
        )
        environment_details[scheme_id] = sorted(
            {
                "|".join(
                    (
                        run["environment"]["environment_id"],
                        run["environment"]["browser"],
                        run["environment"]["viewport"],
                    )
                )
                for run in selected
            }
        )
        prompt_versions[scheme_id] = sorted(
            {run["task_prompt_version"] for run in selected}
        )
        implementations[scheme_id] = sorted(
            {
                "/".join(
                    (
                        run["implementation"]["name"],
                        run["implementation"]["version"],
                        run["implementation"]["revision"],
                        run["implementation"]["branch"],
                    )
                )
                for run in selected
            }
        )

    if missing_keys:
        warnings.append(f"缺少 {len(missing_keys)} 次预期运行，当前报告不能作为完整排名依据。")

    expected_planner = experiment.get("shared_planner")
    if experiment.get("require_shared_planner") and expected_planner:
        expected_name = f"{expected_planner['provider']}/{expected_planner['model']}"
        for scheme_id, names in planner_models.items():
            if names and names != [expected_name]:
                message = (
                    f"方案 {scheme_id} 使用的规划模型 {', '.join(names)} "
                    f"与统一模型 {expected_name} 不一致。"
                )
                warnings.append(message)
                hard_issues.append(message)
    for scheme_id, names in planner_models.items():
        if len(names) > 1:
            message = f"方案 {scheme_id} 在同轮实验中混用了多个规划模型：{', '.join(names)}。"
            warnings.append(message)
            hard_issues.append(message)
    for scheme_id, versions in adapter_prompt_versions.items():
        if len(versions) > 1:
            message = (
                f"方案 {scheme_id} 混用了多个适配器提示版本：{', '.join(versions)}。"
            )
            warnings.append(message)
            hard_issues.append(message)
    for scheme_id, versions in implementations.items():
        if len(versions) > 1:
            message = (
                f"方案 {scheme_id} 在同轮实验中混用了多个实现版本："
                f"{', '.join(versions)}。"
            )
            warnings.append(message)
            hard_issues.append(message)

    expected_environment = experiment.get("environment_id")
    if expected_environment:
        for scheme_id, names in environments.items():
            if names and names != [expected_environment]:
                message = (
                    f"方案 {scheme_id} 的环境 {', '.join(names)} 与统一环境 "
                    f"{expected_environment} 不一致。"
                )
                warnings.append(message)
                hard_issues.append(message)
    for scheme_id, names in environments.items():
        if len(names) > 1:
            message = f"方案 {scheme_id} 混用了多个测试环境：{', '.join(names)}。"
            warnings.append(message)
            hard_issues.append(message)
    observed_environment_details = sorted(
        {detail for details in environment_details.values() for detail in details}
    )
    if len(observed_environment_details) > 1:
        message = "三组运行使用的浏览器版本或viewport不一致。"
        warnings.append(message)
        hard_issues.append(message)

    expected_prompt = experiment["task_prompt_version"]
    for scheme_id, names in prompt_versions.items():
        if names and names != [expected_prompt]:
            message = (
                f"方案 {scheme_id} 的任务提示版本 {', '.join(names)} "
                f"与统一版本 {expected_prompt} 不一致。"
            )
            warnings.append(message)
            hard_issues.append(message)

    validation_method_mismatches = [
        run
        for run in runs
        if run["validation"]["method"]
        != expected_validation_methods[run["task_id"]]
    ]
    if validation_method_mismatches:
        message = (
            f"有 {len(validation_method_mismatches)} 次运行未使用 suite 规定的统一成功判定方法。"
        )
        warnings.append(message)
        hard_issues.append(message)

    model_judge_count = sum(
        run["validation"]["method"] == "model_judge" for run in runs
    )
    evaluator_models = sorted(
        {
            f"{run['validation']['evaluator']['provider']}/"
            f"{run['validation']['evaluator']['model']}"
            for run in runs
            if run["validation"]["method"] == "model_judge"
            and run["validation"].get("evaluator") is not None
        }
    )
    self_judge_count = sum(
        run["validation"]["method"] == "model_judge"
        and run["validation"].get("evaluator") is not None
        and run["validation"]["evaluator"]["provider"].casefold()
        == run["planner"]["provider"].casefold()
        and run["validation"]["evaluator"]["model"].casefold()
        == run["planner"]["model"].casefold()
        for run in runs
    )
    if model_judge_count:
        warnings.append(
            f"有 {model_judge_count} 次运行使用模型判定结果，建议补充页面状态、DOM或API确定性证据。"
        )
    if self_judge_count:
        message = f"有 {self_judge_count} 次运行由执行任务的同一模型自评，存在评估偏差。"
        warnings.append(message)
        hard_issues.append(message)
    if len(evaluator_models) > 1:
        message = (
            "模型判定运行混用了多个评估模型："
            f"{', '.join(evaluator_models)}。"
        )
        warnings.append(message)
        hard_issues.append(message)

    return warnings, {
        "fair_comparison": not hard_issues,
        "hard_issues": hard_issues,
        "planner_models_by_scheme": planner_models,
        "adapter_prompt_versions_by_scheme": adapter_prompt_versions,
        "environments_by_scheme": environments,
        "environment_details_by_scheme": environment_details,
        "task_prompt_versions_by_scheme": prompt_versions,
        "implementations_by_scheme": implementations,
        "evaluator_models": evaluator_models,
        "model_judge_run_count": model_judge_count,
        "self_judge_run_count": self_judge_count,
        "validation_method_mismatch_count": len(validation_method_mismatches),
    }


def _build_conclusion(
    overall: Sequence[Mapping[str, Any]],
    fairness: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    if evidence["ranking_blocked"]:
        return {
            "decision": "evidence_blocked",
            "recommended_scheme_id": None,
            "summary": (
                "部分运行的原始截图、识别结果、动作轨迹或判定证据缺失，"
                "或归档哈希校验失败，暂不建议进行方案排名。"
            ),
            "ranking": [],
        }
    if not fairness["fair_comparison"]:
        return {
            "decision": "fairness_blocked",
            "recommended_scheme_id": None,
            "summary": "统一模型、任务提示或测试环境存在不一致，暂不建议进行方案排名。",
            "ranking": [],
        }
    if any(metric["coverage_rate"] < 1.0 for metric in overall):
        return {
            "decision": "incomplete",
            "recommended_scheme_id": None,
            "summary": "三组运行尚未全部完成。请补齐缺失任务后再比较完成率和耗时。",
            "ranking": [],
        }
    safe = [metric for metric in overall if metric["safety_violation_count"] == 0]
    if not safe:
        return {
            "decision": "safety_blocked",
            "recommended_scheme_id": None,
            "summary": "所有方案均出现安全违规，当前没有可推荐方案。",
            "ranking": [],
        }
    if max(metric["strict_success_rate"] for metric in safe) == 0:
        return {
            "decision": "no_successful_scheme",
            "recommended_scheme_id": None,
            "summary": "所有安全达标方案的任务完成率均为0，当前没有可推荐方案。",
            "ranking": [],
        }
    ranked = sorted(
        safe,
        key=lambda metric: (
            -metric["strict_success_rate"],
            -metric["full_repeat_success_rate"],
            metric["p95_success_duration_ms"]
            if metric["p95_success_duration_ms"] is not None
            else math.inf,
            metric["total_cost"],
            metric["scheme_id"],
        ),
    )
    winner = ranked[0]
    unsafe_count = len(overall) - len(safe)
    suffix = (
        f"；另有 {unsafe_count} 个方案因安全违规不参与推荐"
        if unsafe_count
        else ""
    )
    return {
        "decision": "ready_for_comparison",
        "recommended_scheme_id": winner["scheme_id"],
        "summary": (
            f"在安全达标且数据完整的方案中，{winner['scheme_label']} 的严格完成率最高；"
            f"同完成率时按重复稳定性、P95成功耗时和成本排序{suffix}。"
        ),
        "ranking": [
            {
                "scheme_id": metric["scheme_id"],
                "scheme_label": metric["scheme_label"],
                "strict_success_rate": metric["strict_success_rate"],
                "full_repeat_success_rate": metric["full_repeat_success_rate"],
                "p95_success_duration_ms": metric["p95_success_duration_ms"],
                "total_cost": metric["total_cost"],
            }
            for metric in ranked
        ],
    }


def _report_safe_run(run: Mapping[str, Any]) -> dict[str, Any]:
    """Project a run into report-safe fields without local notes or raw refs."""

    validation = run["validation"]
    failure = run.get("failure")
    return {
        "schema_version": run["schema_version"],
        "record_status": run["record_status"],
        "run_id": run["run_id"],
        "suite_id": run["suite_id"],
        "scheme_id": run["scheme_id"],
        "task_id": run["task_id"],
        "repetition": run["repetition"],
        "outcome": run["outcome"],
        "started_at": run["started_at"],
        "duration_ms": run["duration_ms"],
        "step_count": run["step_count"],
        "model_calls": run["model_calls"],
        "task_prompt_version": run["task_prompt_version"],
        "implementation": dict(run["implementation"]),
        "planner": dict(run["planner"]),
        "environment": dict(run["environment"]),
        "metrics": dict(run["metrics"]),
        "validation": {
            "method": validation["method"],
            "passed": validation["passed"],
            "evaluator": (
                dict(validation["evaluator"])
                if validation.get("evaluator") is not None
                else None
            ),
        },
        "failure": (
            {"category": failure["category"]} if failure is not None else None
        ),
        "evidence": dict(run["evidence"]),
    }


def _normalize_evidence(
    value: Any,
    *,
    require_archived: bool,
    scheme_id: str,
    task_id: str,
    repetition: int,
) -> dict[str, Any]:
    evidence = _copy_mapping(value, "run.evidence")
    status = _require_choice(
        evidence.get("status"), {"pending", "archived"}, "run.evidence.status"
    )
    if status == "pending":
        unexpected = sorted(set(evidence) - {"status"})
        if unexpected:
            raise EvaluationDataError(
                "pending run.evidence contains unsupported fields: "
                + ", ".join(unexpected)
            )
        if require_archived:
            raise EvaluationDataError("run.evidence must be archived")
        return {"status": "pending"}

    expected_fields = {
        "status",
        "manifest_ref",
        "manifest_sha256",
        "artifact_count",
        "total_bytes",
        "archive_duration_ms",
        "complete",
    }
    missing = sorted(expected_fields - set(evidence))
    unexpected = sorted(set(evidence) - expected_fields)
    if missing:
        raise EvaluationDataError(
            "archived run.evidence is missing fields: " + ", ".join(missing)
        )
    if unexpected:
        raise EvaluationDataError(
            "archived run.evidence contains unsupported fields: "
            + ", ".join(unexpected)
        )
    manifest_ref = _require_text(
        evidence.get("manifest_ref"), "run.evidence.manifest_ref", 1000
    )
    if "\\" in manifest_ref:
        raise EvaluationDataError(
            "run.evidence.manifest_ref must use forward slashes"
        )
    relative = PurePosixPath(manifest_ref)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in manifest_ref.split("/")
    ):
        raise EvaluationDataError(
            "run.evidence.manifest_ref must be a safe relative path"
        )
    expected_ref = (
        f"artifacts/{scheme_id}/{task_id}/r{repetition:03d}/manifest.json"
    )
    if relative.as_posix() != expected_ref:
        raise EvaluationDataError(
            "run.evidence.manifest_ref must use the canonical run path "
            f"{expected_ref!r}"
        )
    manifest_sha256 = _require_text(
        evidence.get("manifest_sha256"),
        "run.evidence.manifest_sha256",
        64,
    )
    if (
        len(manifest_sha256) != 64
        or manifest_sha256 != manifest_sha256.casefold()
        or any(character not in "0123456789abcdef" for character in manifest_sha256)
    ):
        raise EvaluationDataError(
            "run.evidence.manifest_sha256 must be a lowercase SHA-256 digest"
        )
    artifact_count = _require_int(
        evidence.get("artifact_count"),
        "run.evidence.artifact_count",
        minimum=1,
        maximum=MAX_EVIDENCE_ARTIFACTS,
    )
    total_bytes = _require_int(
        evidence.get("total_bytes"),
        "run.evidence.total_bytes",
        minimum=1,
        maximum=MAX_EVIDENCE_BYTES,
    )
    archive_duration_ms = _require_number(
        evidence.get("archive_duration_ms"),
        "run.evidence.archive_duration_ms",
        minimum=0,
        maximum=86_400_000,
    )
    complete = _require_bool(
        evidence.get("complete"), "run.evidence.complete"
    )
    if not complete:
        raise EvaluationDataError("archived run.evidence.complete must be true")
    return {
        "status": "archived",
        "manifest_ref": manifest_ref,
        "manifest_sha256": manifest_sha256,
        "artifact_count": artifact_count,
        "total_bytes": total_bytes,
        "archive_duration_ms": archive_duration_ms,
        "complete": True,
    }


def _sanitize_evidence_error(message: str, root: Path) -> str:
    lowered = message.casefold()
    if "sha-256" in lowered or "sha256" in lowered:
        code = "evidence_hash_mismatch"
    elif "schema" in lowered:
        code = "evidence_schema_invalid"
    elif "size" in lowered or "bytes" in lowered:
        code = "evidence_size_invalid"
    elif "path" in lowered or "directory" in lowered or "manifest_ref" in lowered:
        code = "evidence_reference_invalid"
    elif "missing" in lowered or "does not exist" in lowered:
        code = "evidence_missing"
    elif "count" in lowered or "cover every" in lowered:
        code = "evidence_coverage_invalid"
    else:
        code = "evidence_content_invalid"
    # Report artifacts are routinely shared beyond the restricted test area.
    # Never copy parser messages: they may contain page labels, business IDs,
    # selectors, URLs or local paths. Detailed diagnostics stay in test logs.
    return code


def _run_key(run: Mapping[str, Any]) -> tuple[str, str, int]:
    return (run["scheme_id"], run["task_id"], run["repetition"])


def _copy_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationDataError(f"{field} must be an object")
    return dict(value)


def _require_exact_schema(payload: Mapping[str, Any], expected: str, field: str) -> None:
    if payload.get("schema_version") != expected:
        raise EvaluationDataError(f"{field}.schema_version must be {expected!r}")


def _require_identifier(value: Any, field: str) -> str:
    text = _require_text(value, field, 200)
    if (
        not text[0].isascii()
        or not text[0].isalnum()
        or text.endswith(".")
        or not all(
            character.isascii()
            and (character.isalnum() or character in "._-")
            for character in text
        )
    ):
        raise EvaluationDataError(
            f"{field} must be a safe ASCII slug beginning with a letter or "
            "number and containing only letters, numbers, '.', '_' and '-'"
        )
    stem = text.split(".", 1)[0].casefold()
    windows_reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    if stem in windows_reserved:
        raise EvaluationDataError(
            f"{field} must not use a Windows reserved device name"
        )
    return text


def _require_text(value: Any, field: str, max_length: int = 500) -> str:
    if not isinstance(value, str):
        raise EvaluationDataError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise EvaluationDataError(f"{field} must not be empty")
    if len(text) > max_length:
        raise EvaluationDataError(f"{field} exceeds {max_length} characters")
    return text


def _optional_text(value: Any, field: str, max_length: int = 500) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EvaluationDataError(f"{field} must be a string")
    text = value.strip()
    if len(text) > max_length:
        raise EvaluationDataError(f"{field} exceeds {max_length} characters")
    return text


def _require_choice(value: Any, choices: Iterable[str], field: str) -> str:
    text = _require_text(value, field, 100)
    allowed = set(choices)
    if text not in allowed:
        raise EvaluationDataError(
            f"{field} must be one of: {', '.join(sorted(allowed))}"
        )
    return text


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise EvaluationDataError(f"{field} must be a boolean")
    return value


def _optional_bool(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    return _require_bool(value, field)


def _require_int(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvaluationDataError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise EvaluationDataError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return value


def _require_number(
    value: Any,
    field: str,
    *,
    minimum: float,
    maximum: float,
) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationDataError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise EvaluationDataError(
            f"{field} must be finite and between {minimum} and {maximum}"
        )
    return value


def _require_list(
    value: Any, field: str, minimum: int, maximum: int
) -> list[Any]:
    if not isinstance(value, list):
        raise EvaluationDataError(f"{field} must be an array")
    if len(value) < minimum or len(value) > maximum:
        raise EvaluationDataError(
            f"{field} must contain between {minimum} and {maximum} items"
        )
    return list(value)


def _normalize_text_list(
    value: Any,
    field: str,
    max_items: int,
    *,
    max_item_length: int = 200,
) -> list[str]:
    items = _require_list(value, field, 0, max_items)
    return [
        _require_text(item, f"{field}[{index}]", max_item_length)
        for index, item in enumerate(items)
    ]


def _require_timestamp(value: Any, field: str) -> str:
    text = _require_text(value, field, 100)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvaluationDataError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvaluationDataError(f"{field} must include a timezone")
    return text


def _metric_count(metrics: Mapping[str, Any], name: str) -> int:
    return _require_int(
        metrics.get(name),
        f"run.metrics.{name}",
        minimum=0,
        maximum=1_000_000_000,
    )


def _is_placeholder(value: str) -> bool:
    lowered = str(value).strip().casefold()
    return (
        lowered.startswith("fill-")
        or lowered.startswith("<fill")
        or "待填写" in lowered
    )


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    if denominator == 0:
        return None
    return round(float(numerator) / float(denominator), 8)


def _mean_or_none(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(float(statistics.fmean(values)), 4)


def _percentile(values: Sequence[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
    return round(float(ordered[index]), 4)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            stream.write(content)
            temporary_path = Path(stream.name)
        temporary_path.replace(path)
    except OSError as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise EvaluationDataError(f"cannot write report file: {path}") from exc


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        # Prevent spreadsheet applications from evaluating user-controlled text
        # such as failure reasons as formulas when a CSV is opened.
        return "'" + value
    return value


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _milliseconds(value: float | int | None) -> str:
    return "-" if value is None else f"{float(value):.0f} ms"


def _bytes(value: int | float | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "-"


def _number(value: float | int | None, *, digits: int = 2) -> str:
    return "-" if value is None else f"{float(value):.{digits}f}"


def _md_cell(value: Any) -> str:
    return _md_text(value).replace("|", "\\|")


def _md_text(value: Any) -> str:
    return (
        html.escape(str(value or "-"), quote=False)
        .replace("\r", " ")
        .replace("\n", " ")
    )


__all__ = [
    "EvaluationDataError",
    "REPORT_SCHEMA_VERSION",
    "RUN_SCHEMA_VERSION",
    "SUITE_SCHEMA_VERSION",
    "append_run_record",
    "build_run_template",
    "build_suite_template",
    "generate_report",
    "load_json_object",
    "load_run_records",
    "render_conclusion_markdown",
    "render_html_report",
    "render_issues_markdown",
    "render_markdown_report",
    "render_metrics_csv",
    "render_runs_csv",
    "validate_run_record",
    "validate_run_records",
    "validate_suite",
    "write_report_bundle",
]
