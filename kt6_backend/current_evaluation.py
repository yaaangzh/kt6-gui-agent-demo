"""One-run evaluator for the current local CV/OCR + model API scheme."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from .evaluation_artifacts import VISION_MODEL_CALL_SCHEMA_VERSION
from .evaluation_executor import (
    EvaluationExecutionError,
    EvaluationWorkspace,
    ExecutionTask,
    action_event,
    build_final_run_record,
    validation_result,
    utc_now,
)
from .evaluation_report import append_run_record, validate_suite
from .local_cv_canvas_vision import LocalCVTopologyVisionAdapter
from .openai_compatible_api import (
    ModelAPIError,
    ModelAPIResponseError,
    ModelAPITransportError,
    OpenAICompatibleChatClient,
)
from .openai_compatible_topology_model import (
    OPENAI_COMPATIBLE_TOPOLOGY_PROMPT_VERSION,
    OpenAICompatibleTopologyCall,
    OpenAICompatibleTopologySemanticAdapter,
)
from .topology_artifact_common import build_image_input
from .topology_cv_cli import build_cv_artifact_metadata
from .topology_cv_routing import (
    TASK_PROFILES,
    assess_cv_result,
    prepare_cv_payload_for_route,
)
from .topology_fusion import fuse_topology_payloads
from .topology_model_contract import TopologyModelResponseError
from .ui_graph import build_ui_graph
from .vision_recognition import CanvasFrame, CanvasVisionAdapter


CURRENT_ADAPTER_PROMPT_VERSION = OPENAI_COMPATIBLE_TOPOLOGY_PROMPT_VERSION


@dataclass(frozen=True)
class CurrentEvaluationConfig:
    base_url: str
    api_key: str
    provider: str
    model: str
    api_allowed_hosts: frozenset[str]
    allow_remote_model: bool = False
    timeout_seconds: float = 60.0
    max_tokens: int = 4096
    requested_profile: str = "auto"


@dataclass(frozen=True)
class CurrentModelResult:
    payload: dict[str, Any]
    input_tokens: int
    output_tokens: int


class CurrentSemanticModel(Protocol):
    def enrich(
        self,
        *,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
        cv_observations: dict[str, Any],
    ) -> CurrentModelResult:
        ...


class OpenAICompatibleCurrentSemanticModel:
    """Production semantic model wrapper with bounded call accounting."""

    def __init__(self, config: CurrentEvaluationConfig) -> None:
        if _is_remote_url(config.base_url) and not config.allow_remote_model:
            raise EvaluationExecutionError(
                "remote model use requires explicit --allow-remote-model"
            )
        calls: list[OpenAICompatibleTopologyCall] = []
        try:
            client = OpenAICompatibleChatClient(
                base_url=config.base_url,
                api_key=config.api_key,
                model=config.model,
                timeout_seconds=config.timeout_seconds,
                max_tokens=config.max_tokens,
                allowed_hosts=config.api_allowed_hosts,
            )
        except ValueError as exc:
            raise EvaluationExecutionError("model API configuration is invalid") from exc
        self._calls = calls
        self._adapter = OpenAICompatibleTopologySemanticAdapter(
            client,
            provider=config.provider,
            call_sink=calls.append,
        )

    def enrich(
        self,
        *,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
        cv_observations: dict[str, Any],
    ) -> CurrentModelResult:
        before = len(self._calls)
        payload = self._adapter.recognize_with_context(
            page=page,
            frames=frames,
            cv_observations=cv_observations,
        )
        if len(self._calls) != before + 1:
            raise EvaluationExecutionError("model call accounting is incomplete")
        call = self._calls[-1]
        return CurrentModelResult(
            payload=payload,
            input_tokens=int(call.usage.get("input_tokens", 0)),
            output_tokens=int(call.usage.get("output_tokens", 0)),
        )


def run_current_evaluation(
    *,
    suite: Mapping[str, Any],
    runs_path: Path,
    workspace_root: Path,
    task: ExecutionTask,
    repetition: int,
    image_path: Path,
    source_id: str,
    config: CurrentEvaluationConfig,
    implementation: Mapping[str, str],
    environment: Mapping[str, str],
    local_adapter: CanvasVisionAdapter | None = None,
    semantic_model: CurrentSemanticModel | None = None,
) -> dict[str, Any]:
    """Run one image task, archive all evidence and append one run index."""

    normalized_suite = validate_suite(suite)
    suite_task = _suite_task(normalized_suite, task)
    if suite_task.get("scenario_type") not in {"canvas", "mixed"}:
        raise EvaluationExecutionError(
            "current image evaluation requires a canvas or mixed suite task"
        )
    if config.requested_profile not in TASK_PROFILES:
        raise EvaluationExecutionError("requested_profile is unsupported")

    run_id = f"current-{task.task_id}-r{repetition}"
    workspace = EvaluationWorkspace(workspace_root, run_id)
    started_at = utc_now()
    started = time.perf_counter()
    trace_started = time.perf_counter()
    workspace.copy_file("original_screenshot", image_path)
    page, frames = build_image_input(image_path, source_id)
    frame = frames[0]
    actual_environment = dict(environment)
    actual_environment["viewport"] = f"{frame.width}x{frame.height}"
    metrics: dict[str, Any] = {
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
    }

    try:
        vision = local_adapter or LocalCVTopologyVisionAdapter()
        cv_result = vision.recognize(page=page, frames=frames)
        if not isinstance(cv_result, dict):
            raise EvaluationExecutionError("local CV produced no result")
    except Exception:
        return _finalize_run(
            workspace=workspace,
            runs_path=runs_path,
            suite=normalized_suite,
            task=task,
            repetition=repetition,
            started_at=started_at,
            started=started,
            trace_started=trace_started,
            implementation=implementation,
            planner=_planner_record(config),
            environment=actual_environment,
            metrics=metrics,
            success=False,
            failure_category="cv_error",
            action_status="error",
        )

    metrics["cv_calls"] = 1
    cv_path = workspace.write_json("cv_result", cv_result)
    cv_metadata = build_cv_artifact_metadata(
        source_id=source_id,
        frames=frames,
        adapter=vision,
        artifact_sha256=_sha256(cv_path),
    )
    workspace.write_json("cv_metadata", cv_metadata)
    routing = assess_cv_result(
        cv_result,
        requested_profile=config.requested_profile,
        trusted_provenance={
            "adapter_id": cv_metadata["adapter_id"],
            "adapter_version": cv_metadata["adapter_version"],
        },
    )
    routing["source"] = {
        "source_id": source_id,
        "sha256": frame.screenshot_sha256,
        "mime_type": frame.mime_type,
        "width": frame.width,
        "height": frame.height,
        "cv_adapter_id": cv_metadata["adapter_id"],
        "cv_adapter_version": cv_metadata["adapter_version"],
    }

    model_result: dict[str, Any] | None = None
    model_failure: tuple[str, str] | None = None
    model_duration_ms = 0.0
    if routing["decision"] == "model_assist":
        metrics["vision_model_calls"] = 1
        model_started = time.perf_counter()
        try:
            model_runtime = semantic_model or OpenAICompatibleCurrentSemanticModel(config)
            enriched = model_runtime.enrich(
                page=page,
                frames=frames,
                cv_observations=cv_result,
            )
            model_result = enriched.payload
            metrics["input_tokens"] = enriched.input_tokens
            metrics["output_tokens"] = enriched.output_tokens
        except ModelAPITransportError as exc:
            if isinstance(exc.__cause__, TimeoutError):
                metrics["timeout_count"] = 1
                model_failure = ("timeout", "vision_timeout")
            else:
                model_failure = ("transport_error", "model_api_transport_error")
        except (ModelAPIResponseError, TopologyModelResponseError, ValueError):
            model_failure = ("invalid_response", "model_api_invalid_response")
        except ModelAPIError:
            model_failure = ("error", "model_api_error")
        finally:
            model_duration_ms = max(
                0.0, (time.perf_counter() - model_started) * 1000.0
            )

    if model_failure is not None:
        status, error_code = model_failure
        routing.update(
            {
                "requirement_satisfied": False,
                "result_status": "incomplete",
                "execution_status": f"failed_{status}",
            }
        )
        workspace.write_json("routing_result", routing)
        workspace.write_text("model_stderr", f"{error_code}\n")
        workspace.write_jsonl(
            "vision_model_call",
            [
                _vision_call_record(
                    run_id=run_id,
                    config=config,
                    frame=frame,
                    duration_ms=model_duration_ms,
                    status=status,
                    error_code=error_code,
                    diagnostic_artifact_ids=["model-stderr-001"],
                )
            ],
        )
        return _finalize_run(
            workspace=workspace,
            runs_path=runs_path,
            suite=normalized_suite,
            task=task,
            repetition=repetition,
            started_at=started_at,
            started=started,
            trace_started=trace_started,
            implementation=implementation,
            planner=_planner_record(config),
            environment=actual_environment,
            metrics=metrics,
            success=False,
            failure_category=error_code,
            action_status="error",
            outcome="timeout" if status == "timeout" else "failure",
        )

    if routing["decision"] == "insufficient":
        routing["execution_status"] = "insufficient_evidence"
        workspace.write_json("routing_result", routing)
        return _finalize_run(
            workspace=workspace,
            runs_path=runs_path,
            suite=normalized_suite,
            task=task,
            repetition=repetition,
            started_at=started_at,
            started=started,
            trace_started=trace_started,
            implementation=implementation,
            planner=_planner_record(config),
            environment=actual_environment,
            metrics=metrics,
            success=False,
            failure_category="insufficient_evidence",
            action_status="completed",
        )

    try:
        if model_result is not None:
            model_path = workspace.write_json("model_result", model_result)
            routing.update(
                {
                    "requirement_satisfied": True,
                    "result_status": "complete",
                    "execution_status": "completed_with_model",
                }
            )
            workspace.write_json("routing_result", routing)
            workspace.write_jsonl(
                "vision_model_call",
                [
                    _vision_call_record(
                        run_id=run_id,
                        config=config,
                        frame=frame,
                        duration_ms=model_duration_ms,
                        status="success",
                        model_result_sha256=_sha256(model_path),
                    )
                ],
            )
            fused = fuse_topology_payloads(cv_result, model_result)
        else:
            routing["execution_status"] = "completed_without_model"
            workspace.write_json("routing_result", routing)
            prepared_cv, _disputed = prepare_cv_payload_for_route(cv_result, routing)
            fused = fuse_topology_payloads(
                prepared_cv,
                {"topology": {"nodes": [], "edges": []}},
            )
        fused["routing"] = copy.deepcopy(routing)
        workspace.write_json("fused_result", fused)
        graph = _ui_graph(run_id, task, fused)
        workspace.write_json("ui_graph", graph)
    except Exception:
        return _finalize_run(
            workspace=workspace,
            runs_path=runs_path,
            suite=normalized_suite,
            task=task,
            repetition=repetition,
            started_at=started_at,
            started=started,
            trace_started=trace_started,
            implementation=implementation,
            planner=_planner_record(config),
            environment=actual_environment,
            metrics=metrics,
            success=False,
            failure_category="fusion_error",
            action_status="error",
        )

    passed = deterministic_topology_validation(task, fused["result"])
    metrics["first_target_hit"] = passed
    return _finalize_run(
        workspace=workspace,
        runs_path=runs_path,
        suite=normalized_suite,
        task=task,
        repetition=repetition,
        started_at=started_at,
        started=started,
        trace_started=trace_started,
        implementation=implementation,
        planner=_planner_record(config),
        environment=actual_environment,
        metrics=metrics,
        success=passed,
        failure_category=None if passed else "validation_failed",
        action_status="completed",
    )


def deterministic_topology_validation(
    task: ExecutionTask, result: Mapping[str, Any]
) -> bool:
    """Evaluate explicit topology assertions without another model call."""

    assertions = task.validation.get("assertions")
    if not isinstance(assertions, list) or not 1 <= len(assertions) <= 50:
        raise EvaluationExecutionError(
            "validation.assertions must contain deterministic topology checks"
        )
    objects = result.get("objects", [])
    links = result.get("links", [])
    if not isinstance(objects, list) or not isinstance(links, list):
        raise EvaluationExecutionError("topology result is invalid")
    object_ids = {
        str(item.get("business_id", ""))
        for item in objects
        if isinstance(item, Mapping)
    }
    link_pairs = {
        (str(item.get("source", "")), str(item.get("target", "")))
        for item in links
        if isinstance(item, Mapping)
    }
    searchable = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).casefold()
    for index, assertion in enumerate(assertions):
        if not isinstance(assertion, Mapping):
            raise EvaluationExecutionError(
                f"validation.assertions[{index}] must be an object"
            )
        kind = assertion.get("kind")
        if kind in {"text_contains", "object_exists"}:
            value = assertion.get("value")
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise EvaluationExecutionError(
                    f"validation.assertions[{index}].value must be bounded text"
                )
            passed = (
                value.casefold() in searchable
                if kind == "text_contains"
                else value in object_ids
            )
        elif kind == "link_exists":
            source = assertion.get("source")
            target = assertion.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                raise EvaluationExecutionError(
                    f"validation.assertions[{index}] link endpoints must be text"
                )
            passed = (source, target) in link_pairs or (target, source) in link_pairs
        elif kind in {"minimum_object_count", "minimum_link_count"}:
            value = assertion.get("value")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise EvaluationExecutionError(
                    f"validation.assertions[{index}].value must be non-negative"
                )
            passed = len(objects if kind == "minimum_object_count" else links) >= value
        else:
            raise EvaluationExecutionError(
                f"validation.assertions[{index}].kind is unsupported"
            )
        if not passed:
            return False
    return True


def _finalize_run(
    *,
    workspace: EvaluationWorkspace,
    runs_path: Path,
    suite: Mapping[str, Any],
    task: ExecutionTask,
    repetition: int,
    started_at: str,
    started: float,
    trace_started: float,
    implementation: Mapping[str, str],
    planner: Mapping[str, str],
    environment: Mapping[str, str],
    metrics: Mapping[str, Any],
    success: bool,
    failure_category: str | None,
    action_status: str,
    outcome: str = "failure",
) -> dict[str, Any]:
    run_id = workspace.run_id
    workspace.write_jsonl(
        "action_trace",
        [
            action_event(
                run_id=run_id,
                step_index=1,
                action="analyze_topology",
                elapsed_ms=max(0.0, (time.perf_counter() - trace_started) * 1000.0),
                status=action_status,
            )
        ],
    )
    method = str(task.validation["method"])
    workspace.write_json(
        "validation_result",
        validation_result(
            run_id=run_id,
            task_id=task.task_id,
            method=method,
            passed=success,
        ),
    )
    category = failure_category or "evaluation_failed"
    record = build_final_run_record(
        suite,
        scheme_id="current",
        task_id=task.task_id,
        repetition=repetition,
        outcome="success" if success else outcome,
        started_at=started_at,
        duration_ms=max(0.0, (time.perf_counter() - started) * 1000.0),
        step_count=1,
        implementation=implementation,
        planner=planner,
        environment=environment,
        metrics=metrics,
        validation_passed=success,
        validation_method=method,
        failure_category=None if success else category,
        failure_reason="" if success else category,
    )
    workspace.write_pending_run(record)
    sources = [
        (role, path)
        for role, paths in workspace.artifact_sources.items()
        for path in paths
    ]
    return append_run_record(
        runs_path,
        record,
        suite,
        artifact_sources=sources,
    )


def _vision_call_record(
    *,
    run_id: str,
    config: CurrentEvaluationConfig,
    frame: CanvasFrame,
    duration_ms: float,
    status: str,
    model_result_sha256: str | None = None,
    error_code: str | None = None,
    diagnostic_artifact_ids: list[str] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": VISION_MODEL_CALL_SCHEMA_VERSION,
        "run_id": run_id,
        "call_index": 1,
        "producer": {"provider": config.provider, "model": config.model},
        "status": status,
        "duration_ms": duration_ms,
        "screenshot_artifact_id": "original-screenshot-001",
        "screenshot_sha256": frame.screenshot_sha256,
        "routing_artifact_id": "routing-result-001",
        "diagnostic_artifact_ids": diagnostic_artifact_ids or [],
    }
    if status == "success":
        record.update(
            {
                "model_result_artifact_id": "model-result-001",
                "model_result_sha256": model_result_sha256,
            }
        )
    else:
        record["error_code"] = error_code
    return record


def _ui_graph(
    run_id: str, task: ExecutionTask, fused: Mapping[str, Any]
) -> dict[str, Any]:
    result = fused.get("result")
    if not isinstance(result, Mapping):
        raise EvaluationExecutionError("fused result is invalid")
    return build_ui_graph(
        {
            "capture_id": f"capture-{run_id}",
            "capture": {
                "page": {"url": task.start_url, "title": task.instruction[:300]},
                "dom": {"elements": []},
            },
            "result": {
                "perception": {
                    "canvas_perception": {
                        "mode": "canvas_vision_adapter",
                        "provenance": {"semantic_source": "canvas_pixels"},
                        "elements": copy.deepcopy(result.get("objects", [])),
                        "relations": copy.deepcopy(result.get("links", [])),
                    }
                }
            },
        }
    )


def _suite_task(
    suite: Mapping[str, Any], task: ExecutionTask
) -> Mapping[str, Any]:
    if task.suite_id != suite.get("suite_id"):
        raise EvaluationExecutionError("execution task does not belong to suite")
    matches = [
        item
        for item in suite.get("tasks", [])
        if isinstance(item, Mapping) and item.get("task_id") == task.task_id
    ]
    if len(matches) != 1:
        raise EvaluationExecutionError("execution task is missing from suite")
    suite_task = matches[0]
    suite_validation = suite_task.get("validation")
    if (
        not isinstance(suite_validation, Mapping)
        or suite_validation.get("method") != task.validation.get("method")
    ):
        raise EvaluationExecutionError("execution task validation does not match suite")
    return suite_task


def _planner_record(config: CurrentEvaluationConfig) -> dict[str, str]:
    return {
        "provider": config.provider,
        "model": config.model,
        "adapter_prompt_version": CURRENT_ADAPTER_PROMPT_VERSION,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_remote_url(value: str) -> bool:
    host = urlsplit(value).hostname or ""
    normalized = host.rstrip(".").casefold()
    if normalized == "localhost":
        return False
    try:
        import ipaddress

        return not ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return True


__all__ = [
    "CURRENT_ADAPTER_PROMPT_VERSION",
    "CurrentEvaluationConfig",
    "CurrentModelResult",
    "CurrentSemanticModel",
    "OpenAICompatibleCurrentSemanticModel",
    "deterministic_topology_validation",
    "run_current_evaluation",
]
