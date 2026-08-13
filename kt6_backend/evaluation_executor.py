"""Shared, local-only execution helpers for the three evaluation branches."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .evaluation_artifacts import ROLE_FORMATS
from .evaluation_report import (
    OUTCOMES,
    VALIDATION_METHODS,
    build_run_template,
    load_json_object,
    validate_run_record,
    validate_suite,
)


EXECUTION_TASK_SCHEMA_VERSION = "kt6.evaluation-execution-task.v1"
ACTION_EVENT_SCHEMA_VERSION = "kt6.evaluation-action-event.v1"
VALIDATION_RESULT_SCHEMA_VERSION = "kt6.evaluation-validation-result.v1"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,198}[A-Za-z0-9])?$")
_SAFE_ERROR = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


class EvaluationExecutionError(RuntimeError):
    """A stable execution/serialization failure without secret material."""


@dataclass(frozen=True)
class ExecutionTask:
    suite_id: str
    task_id: str
    instruction: str
    start_url: str
    allowed_hosts: frozenset[str]
    step_limit: int
    validation: dict[str, Any]

    def allows_url(self, value: str) -> bool:
        try:
            parsed = urlsplit(str(value).strip())
            host = parsed.hostname
            parsed.port
        except ValueError:
            return False
        if parsed.scheme not in {"https", "http"} or not host:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.scheme == "http" and not _is_loopback(host):
            return False
        return host.rstrip(".").casefold() in self.allowed_hosts


def load_execution_task(path: Path) -> ExecutionTask:
    payload = load_json_object(path)
    if payload.get("schema_version") != EXECUTION_TASK_SCHEMA_VERSION:
        raise EvaluationExecutionError(
            f"execution task schema_version must be {EXECUTION_TASK_SCHEMA_VERSION}"
        )
    suite_id = _identifier(payload.get("suite_id"), "suite_id")
    task_id = _identifier(payload.get("task_id"), "task_id")
    instruction = _text(payload.get("instruction"), "instruction", 16_000)
    start_url = _text(payload.get("start_url"), "start_url", 4096)
    raw_hosts = payload.get("allowed_hosts")
    if not isinstance(raw_hosts, list) or not 1 <= len(raw_hosts) <= 100:
        raise EvaluationExecutionError("allowed_hosts must be a non-empty bounded array")
    hosts: set[str] = set()
    for index, raw_host in enumerate(raw_hosts):
        host = _text(raw_host, f"allowed_hosts[{index}]", 253)
        if ":" in host and not host.startswith("["):
            # Ports belong in URLs, not host allow-list entries.
            raise EvaluationExecutionError("allowed_hosts entries must not include ports")
        normalized = host.strip("[]").rstrip(".").casefold()
        if not normalized or any(char in normalized for char in "/\\@?#"):
            raise EvaluationExecutionError("allowed_hosts contains an invalid host")
        hosts.add(normalized)
    step_limit = payload.get("step_limit")
    if isinstance(step_limit, bool) or not isinstance(step_limit, int):
        raise EvaluationExecutionError("step_limit must be a positive integer")
    if not 1 <= step_limit <= 10_000:
        raise EvaluationExecutionError("step_limit must be a positive integer")
    raw_validation = payload.get("validation")
    if not isinstance(raw_validation, dict):
        raise EvaluationExecutionError("validation must be an object")
    method = raw_validation.get("method")
    if method not in VALIDATION_METHODS:
        raise EvaluationExecutionError("validation.method is unsupported")
    validation = _strict_json_copy(raw_validation, "validation")
    validation["method"] = method
    task = ExecutionTask(
        suite_id=suite_id,
        task_id=task_id,
        instruction=instruction,
        start_url=start_url,
        allowed_hosts=frozenset(hosts),
        step_limit=step_limit,
        validation=validation,
    )
    if not task.allows_url(start_url):
        raise EvaluationExecutionError(
            "start_url must use an allowed HTTPS host or loopback HTTP"
        )
    return task


class EvaluationWorkspace:
    """Exclusive raw-output directory with explicit evidence-role tracking."""

    MAX_ARTIFACTS = 200
    MAX_TOTAL_BYTES = 1024 * 1024 * 1024

    def __init__(self, root: Path, run_id: str) -> None:
        self.run_id = _identifier(run_id, "run_id")
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_dir = self.root / self.run_id
        try:
            self.run_dir.mkdir()
        except FileExistsError as exc:
            raise EvaluationExecutionError("evaluation run workspace already exists") from exc
        self._paths: dict[str, list[Path]] = {}
        self._total_bytes = 0

    @property
    def artifact_sources(self) -> dict[str, tuple[Path, ...]]:
        return {role: tuple(paths) for role, paths in self._paths.items()}

    def write_json(self, role: str, payload: Any) -> Path:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        return self.write_bytes(role, body, extension="json")

    def write_jsonl(self, role: str, records: Iterable[Mapping[str, Any]]) -> Path:
        chunks: list[bytes] = []
        count = 0
        for record in records:
            if not isinstance(record, Mapping):
                raise EvaluationExecutionError("JSONL records must be objects")
            chunks.append(
                json.dumps(
                    dict(record),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            count += 1
        if count == 0:
            raise EvaluationExecutionError("JSONL evidence must not be empty")
        return self.write_bytes(role, b"".join(chunks), extension="jsonl")

    def write_text(self, role: str, value: str, *, extension: str = "log") -> Path:
        if not isinstance(value, str) or not value:
            raise EvaluationExecutionError("text evidence must not be empty")
        return self.write_bytes(role, value.encode("utf-8"), extension=extension)

    def write_bytes(self, role: str, body: bytes, *, extension: str) -> Path:
        normalized_role = self._role(role, extension)
        if not isinstance(body, bytes) or not body:
            raise EvaluationExecutionError("artifact body must be non-empty bytes")
        self._reserve(len(body))
        path = self._next_path(normalized_role, extension)
        try:
            with path.open("xb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise EvaluationExecutionError("cannot write evaluation artifact") from exc
        self._paths.setdefault(normalized_role, []).append(path)
        self._total_bytes += len(body)
        return path

    def copy_file(self, role: str, source: Path) -> Path:
        resolved = Path(source).expanduser().resolve(strict=True)
        if resolved.is_symlink() or not resolved.is_file():
            raise EvaluationExecutionError("artifact source must be a regular file")
        extension = resolved.suffix.lstrip(".").casefold()
        normalized_role = self._role(role, extension)
        size = resolved.stat().st_size
        if size <= 0:
            raise EvaluationExecutionError("artifact source must not be empty")
        self._reserve(size)
        destination = self._next_path(normalized_role, extension)
        try:
            with resolved.open("rb") as source_handle, destination.open("xb") as output:
                shutil.copyfileobj(source_handle, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise EvaluationExecutionError("cannot copy evaluation artifact") from exc
        if destination.stat().st_size != size:
            destination.unlink(missing_ok=True)
            raise EvaluationExecutionError("artifact source changed while copying")
        self._paths.setdefault(normalized_role, []).append(destination)
        self._total_bytes += size
        return destination

    def write_pending_run(self, record: Mapping[str, Any]) -> Path:
        path = self.run_dir / "pending-run.json"
        body = json.dumps(
            dict(record),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        try:
            with path.open("xb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise EvaluationExecutionError("cannot write pending run result") from exc
        return path

    def _next_path(self, role: str, extension: str) -> Path:
        sequence = len(self._paths.get(role, [])) + 1
        stem = role.replace("_", "-")
        return self.run_dir / f"{stem}-{sequence:03d}.{extension}"

    @staticmethod
    def _role(role: str, extension: str) -> str:
        normalized = str(role).strip()
        formats = ROLE_FORMATS.get(normalized)
        if formats is None or extension not in formats:
            raise EvaluationExecutionError("unsupported artifact role or extension")
        return normalized

    def _reserve(self, byte_count: int) -> None:
        artifact_count = sum(len(paths) for paths in self._paths.values())
        if artifact_count >= self.MAX_ARTIFACTS:
            raise EvaluationExecutionError("evaluation workspace has too many artifacts")
        if byte_count <= 0 or self._total_bytes + byte_count > self.MAX_TOTAL_BYTES:
            raise EvaluationExecutionError("evaluation workspace exceeds size limit")


def build_final_run_record(
    suite: Mapping[str, Any],
    *,
    scheme_id: str,
    task_id: str,
    repetition: int,
    outcome: str,
    started_at: str,
    duration_ms: float,
    step_count: int,
    implementation: Mapping[str, str],
    planner: Mapping[str, str],
    environment: Mapping[str, str],
    metrics: Mapping[str, Any],
    validation_passed: bool,
    validation_method: str,
    failure_category: str | None = None,
    failure_reason: str = "",
    validation_evaluator: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    normalized_suite = validate_suite(suite)
    if outcome not in OUTCOMES:
        raise EvaluationExecutionError("outcome is unsupported")
    record = build_run_template(
        normalized_suite,
        scheme_id=scheme_id,
        task_id=task_id,
        repetition=repetition,
    )
    normalized_metrics = dict(record["metrics"])
    normalized_metrics.update(_strict_json_copy(dict(metrics), "metrics"))
    model_calls = int(normalized_metrics.get("planner_model_calls", 0)) + int(
        normalized_metrics.get("vision_model_calls", 0)
    )
    record.update(
        {
            "record_status": "final",
            "outcome": outcome,
            "started_at": started_at,
            "duration_ms": duration_ms,
            "step_count": step_count,
            "model_calls": model_calls,
            "implementation": dict(implementation),
            "planner": dict(planner),
            "environment": dict(environment),
            "metrics": normalized_metrics,
            "validation": {
                "method": validation_method,
                "passed": validation_passed,
                "evidence_ref": "validation_result-001",
                **(
                    {"evaluator": dict(validation_evaluator)}
                    if validation_evaluator is not None
                    else {}
                ),
            },
            "failure": None,
            "notes": "",
        }
    )
    if outcome != "success":
        category = _identifier(failure_category, "failure_category")
        record["failure"] = {
            "category": category,
            "reason": _text(failure_reason, "failure_reason", 1000, allow_empty=True),
        }
    try:
        return validate_run_record(record, normalized_suite)
    except (TypeError, ValueError) as exc:
        raise EvaluationExecutionError("completed run record is invalid") from exc


def action_event(
    *,
    run_id: str,
    step_index: int,
    action: str,
    elapsed_ms: float,
    safety_violation: bool = False,
    status: str = "completed",
) -> dict[str, Any]:
    if isinstance(step_index, bool) or not isinstance(step_index, int) or step_index < 1:
        raise EvaluationExecutionError("step_index must be positive")
    if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, (int, float)):
        raise EvaluationExecutionError("elapsed_ms must be a non-negative number")
    if elapsed_ms < 0:
        raise EvaluationExecutionError("elapsed_ms must be a non-negative number")
    if not isinstance(safety_violation, bool):
        raise EvaluationExecutionError("safety_violation must be boolean")
    return {
        "schema_version": ACTION_EVENT_SCHEMA_VERSION,
        "run_id": _identifier(run_id, "run_id"),
        "step_index": step_index,
        "action": _text(action, "action", 100),
        "status": _text(status, "status", 100),
        "elapsed_ms": elapsed_ms,
        "safety_violation": safety_violation,
    }


def validation_result(
    *,
    run_id: str,
    task_id: str,
    method: str,
    passed: bool,
    evaluator: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if method not in VALIDATION_METHODS:
        raise EvaluationExecutionError("validation method is unsupported")
    if not isinstance(passed, bool):
        raise EvaluationExecutionError("validation passed must be boolean")
    result: dict[str, Any] = {
        "schema_version": VALIDATION_RESULT_SCHEMA_VERSION,
        "evidence_id": "validation_result-001",
        "run_id": _identifier(run_id, "run_id"),
        "task_id": _identifier(task_id, "task_id"),
        "method": method,
        "passed": passed,
    }
    if evaluator is not None:
        result["evaluator"] = _strict_json_copy(dict(evaluator), "evaluator")
    return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or any(char in value for char in "\r\n"):
        raise EvaluationExecutionError(f"required environment variable is missing: {name}")
    return value


def optional_env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if any(char in value for char in "\r\n"):
        raise EvaluationExecutionError(f"environment variable is invalid: {name}")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise EvaluationExecutionError(f"{name} must be a safe identifier")
    return value


def _text(
    value: Any,
    name: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise EvaluationExecutionError(f"{name} must be text")
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized) > maximum:
        raise EvaluationExecutionError(f"{name} must be bounded text")
    return normalized


def _strict_json_copy(value: Any, name: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvaluationExecutionError(f"{name} must contain strict JSON values") from exc


def _is_loopback(host: str) -> bool:
    import ipaddress

    normalized = host.rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


__all__ = [
    "ACTION_EVENT_SCHEMA_VERSION",
    "EXECUTION_TASK_SCHEMA_VERSION",
    "EvaluationExecutionError",
    "EvaluationWorkspace",
    "ExecutionTask",
    "VALIDATION_RESULT_SCHEMA_VERSION",
    "action_event",
    "build_final_run_record",
    "load_execution_task",
    "optional_env",
    "required_env",
    "utc_now",
    "validation_result",
]
