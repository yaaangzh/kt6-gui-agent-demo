"""Browser Use + configurable model API executor for the comparison branch.

The module keeps Browser Use as the browser/action implementation and adapts
its history to the common KT6 evidence contract.  It deliberately does not
use the Browser Use LLM judge as the success oracle: final success is decided
by deterministic assertions over the recorded final page state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .evaluation_executor import (
    EvaluationExecutionError,
    EvaluationWorkspace,
    ExecutionTask,
    action_event,
    build_final_run_record,
    utc_now,
    validation_result,
)
from .evaluation_report import append_run_record, load_json_object, validate_suite
from .openai_compatible_api import OpenAICompatibleChatClient


BROWSER_USE_DOM_SCHEMA_VERSION = "kt6.browser-use-dom-snapshot.v1"
PLANNER_CALL_SCHEMA_VERSION = "kt6.evaluation-planner-call.v1"
BROWSER_USE_ADAPTER_PROMPT_VERSION = "browser-use-openai-compatible-v1"

_DANGEROUS_ACTIONS = frozenset(
    {
        "evaluate",
        "upload_file",
        "write_file",
        "read_file",
        "replace_file",
    }
)


@dataclass(frozen=True)
class BrowserUseEvaluationConfig:
    base_url: str
    api_key: str = field(repr=False)
    provider: str
    model: str
    api_allowed_hosts: frozenset[str] = frozenset()
    cdp_url: str | None = None
    headless: bool = False
    max_steps: int = 10
    llm_timeout_seconds: int = 90
    allow_remote_model: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, str)
            or not self.provider.strip()
            or len(self.provider.strip()) > 100
            or any(char in self.provider for char in "\r\n")
        ):
            raise EvaluationExecutionError("model API provider is invalid")
        if isinstance(self.max_steps, bool) or not 1 <= self.max_steps <= 10_000:
            raise EvaluationExecutionError("max_steps must be a positive integer")
        if (
            isinstance(self.llm_timeout_seconds, bool)
            or not 1 <= self.llm_timeout_seconds <= 600
        ):
            raise EvaluationExecutionError(
                "llm_timeout_seconds must be a positive integer"
            )
        try:
            client = OpenAICompatibleChatClient(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model,
                timeout_seconds=self.llm_timeout_seconds,
                allowed_hosts=self.api_allowed_hosts,
            )
        except (TypeError, ValueError) as exc:
            raise EvaluationExecutionError("model API configuration is invalid") from exc
        model_host = urlsplit(client.endpoint).hostname or ""
        if not _is_loopback(model_host) and not self.allow_remote_model:
            raise EvaluationExecutionError(
                "remote model use requires explicit allow_remote_model=true"
            )
        if self.cdp_url is not None:
            _require_loopback_cdp_url(self.cdp_url)


@dataclass(frozen=True)
class BrowserUseStep:
    response: dict[str, Any]
    actions: tuple[dict[str, Any], ...]
    page_state: dict[str, Any]
    elapsed_ms: float
    error_code: str | None = None


@dataclass(frozen=True)
class BrowserUseRunResult:
    steps: tuple[BrowserUseStep, ...]
    elements: tuple[dict[str, Any], ...]
    final_state: dict[str, Any]
    model_claimed_success: bool | None
    input_tokens: int = 0
    output_tokens: int = 0
    retry_count: int = 0
    timeout_count: int = 0
    browser_label: str = "Chromium via Browser Use"
    viewport: str = "1920x1080"


class BrowserUseBackend(Protocol):
    async def execute(
        self,
        task: ExecutionTask,
        config: BrowserUseEvaluationConfig,
    ) -> BrowserUseRunResult:
        ...


class BrowserUseLibraryBackend:
    """Thin adapter around the pinned open-source ``browser-use`` package."""

    async def execute(
        self,
        task: ExecutionTask,
        config: BrowserUseEvaluationConfig,
    ) -> BrowserUseRunResult:
        # Browser Use otherwise emits anonymous PostHog telemetry.  This must be
        # set before importing the package so page/task data stays in scope.
        os.environ["ANONYMIZED_TELEMETRY"] = "false"
        os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
        browser: Any | None = None
        try:
            from browser_use import Agent, Browser, ChatOpenAI, Tools
        except ImportError as exc:
            raise EvaluationExecutionError(
                "browser-use 0.13.7 is required for this experiment branch"
            ) from exc

        try:
            llm = ChatOpenAI(
                model=config.model,
                api_key=config.api_key,
                base_url=config.base_url,
                temperature=0.0,
            )
            browser_kwargs: dict[str, Any] = {
                "allowed_domains": sorted(task.allowed_hosts),
                "keep_alive": True,
                "enable_default_extensions": False,
            }
            if config.cdp_url:
                browser_kwargs["cdp_url"] = config.cdp_url
            else:
                browser_kwargs.update(
                    {
                        "headless": config.headless,
                        "window_size": {"width": 1920, "height": 1080},
                    }
                )
            browser = Browser(**browser_kwargs)
            tools = Tools(exclude_actions=sorted(_DANGEROUS_ACTIONS | {"search"}))
            agent = Agent(
                task=task.instruction,
                llm=llm,
                browser=browser,
                tools=tools,
                initial_actions=[
                    {"navigate": {"url": task.start_url, "new_tab": False}}
                ],
                directly_open_url=False,
                use_vision=False,
                use_judge=False,
                use_thinking=True,
                max_actions_per_step=1,
                max_failures=3,
                calculate_cost=True,
                llm_timeout=config.llm_timeout_seconds,
                final_response_after_failure=False,
                generate_gif=False,
                enable_signal_handler=False,
            )
        except Exception as exc:
            if browser is not None:
                await _close_browser(browser)
            raise EvaluationExecutionError(
                "cannot initialize the Browser Use runtime"
            ) from exc

        try:
            history = await agent.run(max_steps=min(config.max_steps, task.step_limit))
            current_state = await browser.get_browser_state_summary(
                include_screenshot=False
            )
            return _normalize_browser_use_history(history, current_state=current_state)
        except asyncio.TimeoutError as exc:
            raise EvaluationExecutionError("browser_use_timeout") from exc
        except EvaluationExecutionError:
            raise
        except Exception as exc:
            raise EvaluationExecutionError("browser_use_execution_failed") from exc
        finally:
            await _close_browser(browser)


def deterministic_page_validation(
    task: ExecutionTask,
    result: BrowserUseRunResult,
) -> bool:
    """Evaluate explicit page-state assertions without another model call."""

    assertions = task.validation.get("assertions")
    if not isinstance(assertions, list) or not 1 <= len(assertions) <= 50:
        raise EvaluationExecutionError(
            "validation.assertions must contain deterministic page-state checks"
        )
    final_url = str(result.final_state.get("url", ""))
    title = str(result.final_state.get("title", ""))
    searchable = json.dumps(
        {"final_state": result.final_state, "elements": result.elements},
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )
    for index, assertion in enumerate(assertions):
        if not isinstance(assertion, Mapping):
            raise EvaluationExecutionError(
                f"validation.assertions[{index}] must be an object"
            )
        kind = assertion.get("kind")
        value = assertion.get("value")
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise EvaluationExecutionError(
                f"validation.assertions[{index}].value must be bounded text"
            )
        if kind == "url_equals":
            passed = final_url == value
        elif kind == "url_contains":
            passed = value in final_url
        elif kind == "title_contains":
            passed = value.casefold() in title.casefold()
        elif kind == "text_contains":
            passed = value.casefold() in searchable.casefold()
        else:
            raise EvaluationExecutionError(
                f"validation.assertions[{index}].kind is unsupported"
            )
        if not passed:
            return False
    return True


async def run_browser_use_evaluation(
    *,
    suite: Mapping[str, Any],
    runs_path: Path,
    workspace_root: Path,
    task: ExecutionTask,
    repetition: int,
    config: BrowserUseEvaluationConfig,
    implementation: Mapping[str, str],
    environment: Mapping[str, str],
    backend: BrowserUseBackend | None = None,
) -> dict[str, Any]:
    """Run one Browser Use repetition, archive evidence, and append the index."""

    normalized_suite = validate_suite(suite)
    if task.suite_id != normalized_suite["suite_id"]:
        raise EvaluationExecutionError("execution task does not belong to suite")
    if config.max_steps > task.step_limit:
        raise EvaluationExecutionError("configured max_steps exceeds task.step_limit")
    run_id = f"browser_use-{task.task_id}-r{repetition}"
    workspace = EvaluationWorkspace(workspace_root, run_id)
    started_at = utc_now()
    started = time.perf_counter()
    runtime = backend or BrowserUseLibraryBackend()
    try:
        result = await runtime.execute(task, config)
    except EvaluationExecutionError as exc:
        # A runtime that cannot produce a Browser Use history cannot provide the
        # mandatory DOM evidence.  Preserve the raw workspace for diagnosis but
        # do not create an unverifiable ranked record.
        raise EvaluationExecutionError("Browser Use run produced no auditable history") from exc
    duration_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
    step_count = len(result.steps)
    if step_count > task.step_limit:
        raise EvaluationExecutionError("Browser Use exceeded the task step limit")

    dom_payload = {
        "schema_version": BROWSER_USE_DOM_SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task.task_id,
        "safe_for_execution": False,
        "producer": {"name": "browser-use", "mode": "dom-cdp"},
        "final_state": _strict_json(result.final_state, "final_state"),
        "elements": [_strict_json(item, "elements") for item in result.elements],
    }
    dom_path = workspace.write_json("dom_snapshot", dom_payload)
    dom_sha256 = hashlib.sha256(dom_path.read_bytes()).hexdigest()

    planner_records: list[dict[str, Any]] = []
    trace_records: list[dict[str, Any]] = []
    safety_violations = 0
    for index, step in enumerate(result.steps, start=1):
        response = _strict_json(step.response, f"steps[{index}].response")
        if not isinstance(response, dict) or not response:
            raise EvaluationExecutionError("Browser Use step response must be non-empty")
        planner_records.append(
            {
                "schema_version": PLANNER_CALL_SCHEMA_VERSION,
                "run_id": run_id,
                "call_index": index,
                "producer": {"provider": config.provider, "model": config.model},
                "input_refs": [dom_sha256],
                "response": response,
            }
        )
        action_name, violation = _step_action(task, step)
        safety_violations += int(violation)
        trace_records.append(
            action_event(
                run_id=run_id,
                step_index=index,
                action=action_name,
                elapsed_ms=step.elapsed_ms,
                safety_violation=violation,
                status="error" if step.error_code else "completed",
            )
        )
    if not trace_records:
        trace_records.append(
            {
                "schema_version": "kt6.evaluation-action-event.v1",
                "run_id": run_id,
                "event_type": "no_agent_step",
                "safety_violation": False,
            }
        )
    if planner_records:
        workspace.write_jsonl("planner_result", planner_records)
    workspace.write_jsonl("action_trace", trace_records)
    workspace.write_json(
        "browser_use_elements",
        {
            "schema_version": "kt6.browser-use-history.v1",
            "run_id": run_id,
            "steps": [
                {
                    "step_index": index,
                    "actions": list(step.actions),
                    "page_state": step.page_state,
                    "error_code": step.error_code,
                }
                for index, step in enumerate(result.steps, start=1)
            ],
        },
    )

    validation_passed = deterministic_page_validation(task, result)
    success = validation_passed and safety_violations == 0
    outcome = "success" if success else "failure"
    workspace.write_json(
        "validation_result",
        validation_result(
            run_id=run_id,
            task_id=task.task_id,
            method=str(task.validation["method"]),
            passed=success,
        ),
    )
    metrics = {
        "first_target_hit": success if step_count else None,
        "misclick_count": 0,
        "retry_count": result.retry_count,
        "loop_count": 0,
        "timeout_count": result.timeout_count,
        "safety_violation_count": safety_violations,
        "cv_calls": 0,
        "planner_model_calls": step_count,
        "vision_model_calls": 0,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost": 0,
    }
    record = build_final_run_record(
        normalized_suite,
        scheme_id="browser_use",
        task_id=task.task_id,
        repetition=repetition,
        outcome=outcome,
        started_at=started_at,
        duration_ms=duration_ms,
        step_count=step_count,
        implementation=implementation,
        planner={
            "provider": config.provider,
            "model": config.model,
            "adapter_prompt_version": BROWSER_USE_ADAPTER_PROMPT_VERSION,
        },
        environment=environment,
        metrics=metrics,
        validation_passed=success,
        validation_method=str(task.validation["method"]),
        failure_category=None if success else "validation_failed",
        failure_reason="" if success else "deterministic_validation_failed",
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
        normalized_suite,
        artifact_sources=sources,
    )


def load_suite_and_task(suite_path: Path, task: ExecutionTask) -> dict[str, Any]:
    suite = load_json_object(suite_path)
    normalized = validate_suite(suite)
    if normalized["suite_id"] != task.suite_id:
        raise EvaluationExecutionError("execution task does not belong to suite")
    return normalized


def _normalize_browser_use_history(
    history: Any,
    *,
    current_state: Any | None = None,
) -> BrowserUseRunResult:
    raw_items = getattr(history, "history", None)
    if not isinstance(raw_items, Sequence):
        raise EvaluationExecutionError("Browser Use returned an invalid history")
    steps: list[BrowserUseStep] = []
    elements: list[dict[str, Any]] = []
    retry_count = 0
    timeout_count = 0
    for item in raw_items:
        serialized = _object_json(item)
        if not isinstance(serialized, Mapping):
            raise EvaluationExecutionError("Browser Use history item is invalid")
        model_output = serialized.get("model_output")
        if not isinstance(model_output, Mapping) or not model_output:
            # No LLM response means there is no planner call that can satisfy the
            # common evidence contract; keep it as a retry diagnostic only.
            retry_count += 1
            continue
        raw_actions = model_output.get("action", [])
        actions = tuple(
            dict(action) for action in raw_actions if isinstance(action, Mapping)
        )
        state = serialized.get("state")
        page_state = dict(state) if isinstance(state, Mapping) else {}
        for interacted in page_state.get("interacted_element") or []:
            if isinstance(interacted, Mapping):
                elements.append(dict(interacted))
        metadata = serialized.get("metadata")
        elapsed_ms = 0.0
        if isinstance(metadata, Mapping):
            start = metadata.get("step_start_time")
            end = metadata.get("step_end_time")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                elapsed_ms = max(0.0, float(end - start) * 1000.0)
        error_code: str | None = None
        results = serialized.get("result")
        if isinstance(results, list) and any(
            isinstance(value, Mapping) and value.get("error") for value in results
        ):
            error_code = "browser_action_error"
            retry_count += 1
            if any(
                isinstance(value, Mapping)
                and "timeout" in str(value.get("error", "")).casefold()
                for value in results
            ):
                timeout_count += 1
        steps.append(
            BrowserUseStep(
                response=dict(model_output),
                actions=actions,
                page_state=page_state,
                elapsed_ms=elapsed_ms,
                error_code=error_code,
            )
        )
    final_state = dict(steps[-1].page_state) if steps else {}
    current_elements, current_page = _current_dom_state(current_state)
    if current_page:
        final_state.update(current_page)
    if current_elements:
        elements = current_elements
    usage = _object_json(getattr(history, "usage", None))
    input_tokens, output_tokens = _token_usage(usage)
    successful = _safe_call(history, "is_successful")
    return BrowserUseRunResult(
        steps=tuple(steps),
        elements=tuple(_deduplicate_elements(elements)),
        final_state=final_state,
        model_claimed_success=successful if isinstance(successful, bool) else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        retry_count=retry_count,
        timeout_count=timeout_count,
    )


def _current_dom_state(value: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if value is None:
        return [], {}
    page: dict[str, Any] = {}
    for field_name in ("url", "title"):
        field_value = getattr(value, field_name, None)
        if isinstance(field_value, str):
            page[field_name] = field_value
    dom_state = getattr(value, "dom_state", None)
    selector_map = getattr(dom_state, "selector_map", None)
    if not isinstance(selector_map, Mapping):
        return [], page
    elements: list[dict[str, Any]] = []
    for index, (selector_index, element) in enumerate(selector_map.items()):
        if index >= 2000:
            page["elements_truncated"] = True
            break
        try:
            normalized = _object_json(element)
        except EvaluationExecutionError:
            normalized = {"representation": str(element)[:8000]}
        if isinstance(normalized, Mapping):
            item = dict(normalized)
        else:
            item = {"representation": str(normalized)[:8000]}
        item.setdefault("browser_use_index", selector_index)
        elements.append(item)
    return elements, page


def _step_action(task: ExecutionTask, step: BrowserUseStep) -> tuple[str, bool]:
    if not step.actions:
        return "no_action", bool(step.error_code)
    action = step.actions[0]
    action_name = str(next(iter(action), "unknown_action"))[:100]
    violation = action_name in _DANGEROUS_ACTIONS
    if action_name == "navigate":
        parameters = action.get(action_name)
        if isinstance(parameters, Mapping) and isinstance(parameters.get("url"), str):
            violation = violation or not task.allows_url(parameters["url"])
    return action_name, violation


def _object_json(value: Any) -> Any:
    if value is None:
        return None
    for method_name in ("model_dump", "to_dict", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _strict_json(method(), method_name)
            except TypeError:
                continue
    return _strict_json(value, "browser_use_value")


def _strict_json(value: Any, name: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        )
        if len(encoded.encode("utf-8")) > 32 * 1024 * 1024:
            raise EvaluationExecutionError(f"{name} exceeds the evidence size limit")
        return json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise EvaluationExecutionError(f"{name} is not strict JSON") from exc


def _json_default(value: Any) -> Any:
    for method_name in ("model_dump", "to_dict", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            return method()
    if isinstance(value, Path):
        return value.name
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _deduplicate_elements(elements: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for element in elements:
        normalized = _strict_json(element, "browser_use_element")
        digest = hashlib.sha256(
            json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if digest not in seen:
            seen.add(digest)
            result.append(normalized)
    return result


def _token_usage(value: Any) -> tuple[int, int]:
    if not isinstance(value, Mapping):
        return 0, 0

    def count(*keys: str) -> int:
        for key in keys:
            raw = value.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                return raw
        return 0

    return count("prompt_tokens", "input_tokens"), count(
        "completion_tokens", "output_tokens"
    )


async def _close_browser(browser: Any) -> None:
    for method_name in ("stop", "close", "kill"):
        method = getattr(browser, method_name, None)
        if callable(method):
            try:
                result = method()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass
            return


def _safe_call(value: Any, method_name: str) -> Any:
    method = getattr(value, method_name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:
        return None


def _require_loopback_cdp_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        parsed.port
    except ValueError as exc:
        raise EvaluationExecutionError("cdp_url is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or not _is_loopback(host)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise EvaluationExecutionError("cdp_url must be an exact loopback HTTP URL")


def _is_loopback(host: str) -> bool:
    import ipaddress

    normalized = host.rstrip(".").casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


__all__ = [
    "BROWSER_USE_ADAPTER_PROMPT_VERSION",
    "BrowserUseBackend",
    "BrowserUseEvaluationConfig",
    "BrowserUseLibraryBackend",
    "BrowserUseRunResult",
    "BrowserUseStep",
    "deterministic_page_validation",
    "load_suite_and_task",
    "run_browser_use_evaluation",
]
