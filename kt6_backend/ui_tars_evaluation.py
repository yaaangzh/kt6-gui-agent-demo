"""UI-TARS API + Playwright executor for the isolated comparison branch."""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import math
import re
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
from .evaluation_report import append_run_record, validate_suite
from .openai_compatible_api import (
    ModelAPIError,
    OpenAICompatibleChatClient,
)


UI_TARS_RESPONSE_SCHEMA_VERSION = "kt6.evaluation-ui-tars-response.v1"
PLANNER_CALL_SCHEMA_VERSION = "kt6.evaluation-planner-call.v1"
UI_TARS_ADAPTER_PROMPT_VERSION = "deepseek-plan-ui-tars-ground-v1"

_ACTION_LINE = re.compile(r"^\s*Action\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_ALLOWED_ACTIONS = frozenset(
    {
        "click",
        "left_double",
        "right_single",
        "drag",
        "type",
        "hotkey",
        "scroll",
        "wait",
        "finished",
        "call_user",
    }
)
_SAFE_HOTKEYS = frozenset(
    {
        "ENTER",
        "ESCAPE",
        "TAB",
        "SHIFT+TAB",
        "ARROWUP",
        "ARROWDOWN",
        "ARROWLEFT",
        "ARROWRIGHT",
        "PAGEUP",
        "PAGEDOWN",
        "HOME",
        "END",
        "CONTROL+A",
        "CTRL+A",
    }
)


@dataclass(frozen=True)
class ModelEndpointConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str = ""
    allowed_hosts: frozenset[str] = frozenset()
    timeout_seconds: float = 90.0
    max_tokens: int = 4096
    allow_remote: bool = False

    def client(self) -> OpenAICompatibleChatClient:
        try:
            client = OpenAICompatibleChatClient(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model,
                timeout_seconds=self.timeout_seconds,
                max_tokens=self.max_tokens,
                allowed_hosts=self.allowed_hosts,
            )
        except (TypeError, ValueError) as exc:
            raise EvaluationExecutionError("model endpoint configuration is invalid") from exc
        host = urlsplit(client.endpoint).hostname or ""
        if not _is_loopback(host) and not self.allow_remote:
            raise EvaluationExecutionError(
                "remote model use requires explicit allow_remote=true"
            )
        return client


@dataclass(frozen=True)
class UITarsEvaluationConfig:
    planner: ModelEndpointConfig
    vision: ModelEndpointConfig
    coordinate_mode: str = "scale_1000"
    execute_actions: bool = False
    cdp_url: str | None = None
    headless: bool = False
    max_steps: int = 10
    viewport_width: int = 1920
    viewport_height: int = 1080
    wait_after_action_ms: int = 300

    def __post_init__(self) -> None:
        self.planner.client()
        self.vision.client()
        if self.coordinate_mode not in {"scale_1000", "unit", "pixel"}:
            raise EvaluationExecutionError("coordinate_mode is unsupported")
        for value, name, maximum in (
            (self.max_steps, "max_steps", 10_000),
            (self.viewport_width, "viewport_width", 16_384),
            (self.viewport_height, "viewport_height", 16_384),
            (self.wait_after_action_ms, "wait_after_action_ms", 60_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise EvaluationExecutionError(f"{name} must be a positive integer")
        if self.cdp_url is not None:
            _require_loopback_cdp_url(self.cdp_url)


@dataclass(frozen=True)
class BrowserObservation:
    png: bytes
    width: int
    height: int
    url: str
    title: str


@dataclass(frozen=True)
class UITarsAction:
    kind: str
    start: tuple[float, float] | None = None
    end: tuple[float, float] | None = None
    content: str | None = None
    direction: str | None = None
    key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind}
        if self.start is not None:
            result["start"] = list(self.start)
        if self.end is not None:
            result["end"] = list(self.end)
        if self.content is not None:
            result["content"] = self.content
        if self.direction is not None:
            result["direction"] = self.direction
        if self.key is not None:
            result["key"] = self.key
        return result


@dataclass(frozen=True)
class PlannerDecision:
    payload: dict[str, Any]
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class UITarsPrediction:
    raw_text: str
    response: dict[str, Any]
    action: UITarsAction | None
    input_tokens: int
    output_tokens: int
    error_code: str | None = None


class PlannerModel(Protocol):
    def plan(
        self,
        *,
        task: ExecutionTask,
        observation_ref: str,
        action_history: Sequence[Mapping[str, Any]],
    ) -> PlannerDecision:
        ...


class UITarsModel(Protocol):
    def predict(
        self,
        *,
        task: ExecutionTask,
        goal: str,
        observation: BrowserObservation,
        action_history: Sequence[Mapping[str, Any]],
    ) -> UITarsPrediction:
        ...


class BrowserOperator(Protocol):
    async def start(self, task: ExecutionTask) -> None:
        ...

    async def observe(self) -> BrowserObservation:
        ...

    async def execute(
        self,
        action: UITarsAction,
        observation: BrowserObservation,
        config: UITarsEvaluationConfig,
    ) -> dict[str, Any]:
        ...

    async def final_state(self) -> dict[str, Any]:
        ...

    async def close(self) -> None:
        ...


class DeepSeekStepPlanner:
    def __init__(self, config: ModelEndpointConfig) -> None:
        self.client = config.client()

    def plan(
        self,
        *,
        task: ExecutionTask,
        observation_ref: str,
        action_history: Sequence[Mapping[str, Any]],
    ) -> PlannerDecision:
        result = self.client.complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are the shared benchmark planner. Return one strict JSON "
                        "object with keys goal and status. status is continue or done. "
                        "Do not invent coordinates and do not request scripts or file access."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": task.instruction,
                            "observation_ref": observation_ref,
                            "previous_actions": list(action_history[-10:]),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                    ),
                },
            ],
            json_mode=True,
            temperature=0.0,
        )
        payload = result.json_content()
        goal = payload.get("goal")
        status = payload.get("status")
        if (
            not isinstance(goal, str)
            or not goal.strip()
            or len(goal) > 4000
            or status not in {"continue", "done"}
        ):
            raise EvaluationExecutionError("planner response violates its contract")
        return PlannerDecision(
            payload={"goal": goal.strip(), "status": status},
            input_tokens=result.usage["input_tokens"],
            output_tokens=result.usage["output_tokens"],
        )


class UITarsAPIModel:
    def __init__(self, config: ModelEndpointConfig) -> None:
        self.client = config.client()

    def predict(
        self,
        *,
        task: ExecutionTask,
        goal: str,
        observation: BrowserObservation,
        action_history: Sequence[Mapping[str, Any]],
    ) -> UITarsPrediction:
        image_url = "data:image/png;base64," + base64.b64encode(observation.png).decode(
            "ascii"
        )
        prompt = _ui_tars_prompt(task, goal, observation, action_history)
        result = self.client.complete(
            messages=[
                {"role": "system", "content": "You are UI-TARS, a GUI grounding model."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ],
            temperature=0.0,
        )
        action = parse_ui_tars_action(result.content)
        return UITarsPrediction(
            raw_text=result.content,
            response={
                "prediction": result.content,
                "parsed_action": action.as_dict(),
                "response_id": result.response_id,
                "response_model": result.model,
            },
            action=action,
            input_tokens=result.usage["input_tokens"],
            output_tokens=result.usage["output_tokens"],
        )


class PlaywrightBrowserOperator:
    """Coordinate-only browser operator; DOM is used only for final validation."""

    def __init__(self, config: UITarsEvaluationConfig) -> None:
        self.config = config
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._owns_browser = False
        self._task: ExecutionTask | None = None

    async def start(self, task: ExecutionTask) -> None:
        self._task = task
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise EvaluationExecutionError(
                "playwright 1.49.1 is required for the UI-TARS branch"
            ) from exc
        try:
            self._playwright = await async_playwright().start()
            if self.config.cdp_url:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    self.config.cdp_url
                )
                self._context = (
                    self._browser.contexts[0]
                    if self._browser.contexts
                    else await self._browser.new_context(
                        viewport={
                            "width": self.config.viewport_width,
                            "height": self.config.viewport_height,
                        }
                    )
                )
                self._page = (
                    self._context.pages[0]
                    if self._context.pages
                    else await self._context.new_page()
                )
            else:
                self._browser = await self._playwright.chromium.launch(
                    headless=self.config.headless
                )
                self._owns_browser = True
                self._context = await self._browser.new_context(
                    viewport={
                        "width": self.config.viewport_width,
                        "height": self.config.viewport_height,
                    }
                )
                self._page = await self._context.new_page()

            async def navigation_guard(route: Any, request: Any) -> None:
                if request.is_navigation_request() and not task.allows_url(request.url):
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()

            await self._context.route("**/*", navigation_guard)
            await self._page.goto(task.start_url, wait_until="domcontentloaded")
        except EvaluationExecutionError:
            raise
        except Exception as exc:
            await self.close()
            raise EvaluationExecutionError("cannot initialize Playwright operator") from exc

    async def observe(self) -> BrowserObservation:
        if self._page is None or self._task is None:
            raise EvaluationExecutionError("Playwright operator is not started")
        if not self._task.allows_url(self._page.url):
            raise EvaluationExecutionError("browser escaped the task host allowlist")
        try:
            png = await self._page.screenshot(
                type="png", full_page=False, animations="disabled"
            )
            viewport = self._page.viewport_size
            if viewport is None:
                viewport = await self._page.evaluate(
                    "() => ({width: window.innerWidth, height: window.innerHeight})"
                )
            width = int(viewport["width"])
            height = int(viewport["height"])
            title = await self._page.title()
        except Exception as exc:
            raise EvaluationExecutionError("cannot capture Playwright observation") from exc
        if not png or width < 1 or height < 1:
            raise EvaluationExecutionError("Playwright observation is invalid")
        return BrowserObservation(
            png=bytes(png),
            width=width,
            height=height,
            url=self._page.url,
            title=title,
        )

    async def execute(
        self,
        action: UITarsAction,
        observation: BrowserObservation,
        config: UITarsEvaluationConfig,
    ) -> dict[str, Any]:
        if self._page is None:
            raise EvaluationExecutionError("Playwright operator is not started")
        if not config.execute_actions:
            return {"status": "dry_run"}
        start = (
            map_ui_tars_point(
                action.start,
                width=observation.width,
                height=observation.height,
                mode=config.coordinate_mode,
            )
            if action.start is not None
            else None
        )
        end = (
            map_ui_tars_point(
                action.end,
                width=observation.width,
                height=observation.height,
                mode=config.coordinate_mode,
            )
            if action.end is not None
            else None
        )
        try:
            if action.kind == "click" and start:
                await self._page.mouse.click(*start)
            elif action.kind == "left_double" and start:
                await self._page.mouse.dblclick(*start)
            elif action.kind == "right_single" and start:
                await self._page.mouse.click(*start, button="right")
            elif action.kind == "drag" and start and end:
                await self._page.mouse.move(*start)
                await self._page.mouse.down()
                await self._page.mouse.move(*end, steps=10)
                await self._page.mouse.up()
            elif action.kind == "type" and action.content is not None:
                content = action.content
                submit = content.endswith("\n")
                if submit:
                    content = content[:-1]
                if content:
                    await self._page.keyboard.insert_text(content)
                if submit:
                    await self._page.keyboard.press("Enter")
            elif action.kind == "hotkey" and action.key:
                await self._page.keyboard.press(_playwright_key(action.key))
            elif action.kind == "scroll":
                if start:
                    await self._page.mouse.move(*start)
                dx, dy = _scroll_delta(action.direction or "down")
                await self._page.mouse.wheel(dx, dy)
            elif action.kind == "wait":
                await self._page.wait_for_timeout(1000)
            elif action.kind in {"finished", "call_user"}:
                pass
            else:
                raise EvaluationExecutionError("UI-TARS action is incomplete")
            await self._page.wait_for_timeout(config.wait_after_action_ms)
        except EvaluationExecutionError:
            raise
        except Exception as exc:
            raise EvaluationExecutionError("Playwright action failed") from exc
        if self._task is not None and not self._task.allows_url(self._page.url):
            raise EvaluationExecutionError("action navigated outside the task allowlist")
        return {"status": "completed", "mapped_start": start, "mapped_end": end}

    async def final_state(self) -> dict[str, Any]:
        if self._page is None:
            return {}
        try:
            body_text = await self._page.locator("body").inner_text(timeout=5000)
            return {
                "url": self._page.url,
                "title": await self._page.title(),
                "body_text": body_text[:200_000],
            }
        except Exception as exc:
            raise EvaluationExecutionError("cannot read deterministic final state") from exc

    async def close(self) -> None:
        try:
            if self._owns_browser and self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass


async def run_ui_tars_evaluation(
    *,
    suite: Mapping[str, Any],
    runs_path: Path,
    workspace_root: Path,
    task: ExecutionTask,
    repetition: int,
    config: UITarsEvaluationConfig,
    implementation: Mapping[str, str],
    environment: Mapping[str, str],
    planner: PlannerModel | None = None,
    vision_model: UITarsModel | None = None,
    operator: BrowserOperator | None = None,
) -> dict[str, Any]:
    normalized_suite = validate_suite(suite)
    if task.suite_id != normalized_suite["suite_id"]:
        raise EvaluationExecutionError("execution task does not belong to suite")
    if config.max_steps > task.step_limit:
        raise EvaluationExecutionError("configured max_steps exceeds task.step_limit")
    run_id = f"ui_tars-{task.task_id}-r{repetition}"
    workspace = EvaluationWorkspace(workspace_root, run_id)
    planner_runtime = planner or DeepSeekStepPlanner(config.planner)
    vision_runtime = vision_model or UITarsAPIModel(config.vision)
    browser = operator or PlaywrightBrowserOperator(config)
    started_at = utc_now()
    started = time.perf_counter()
    planner_records: list[dict[str, Any]] = []
    response_records: list[dict[str, Any]] = []
    trace_records: list[dict[str, Any]] = []
    action_records: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    timeout_count = 0
    failure_category: str | None = None
    finished = False
    try:
        await browser.start(task)
        for step_index in range(1, config.max_steps + 1):
            step_started = time.perf_counter()
            observation = await browser.observe()
            screenshot_path = workspace.write_bytes(
                "original_screenshot", observation.png, extension="png"
            )
            screenshot_sha = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
            screenshot_id = f"original-screenshot-{step_index:03d}"
            try:
                plan = planner_runtime.plan(
                    task=task,
                    observation_ref=screenshot_sha,
                    action_history=history,
                )
                plan_payload = plan.payload
                input_tokens += plan.input_tokens
                output_tokens += plan.output_tokens
            except (EvaluationExecutionError, ModelAPIError):
                plan_payload = {
                    "goal": task.instruction,
                    "status": "continue",
                    "error_code": "planner_call_failed",
                }
                failure_category = failure_category or "planner_api_error"
            planner_records.append(
                {
                    "schema_version": PLANNER_CALL_SCHEMA_VERSION,
                    "run_id": run_id,
                    "call_index": step_index,
                    "producer": {
                        "provider": "deepseek",
                        "model": config.planner.model,
                    },
                    "input_refs": [screenshot_sha],
                    "response": plan_payload,
                }
            )
            try:
                prediction = vision_runtime.predict(
                    task=task,
                    goal=str(plan_payload["goal"]),
                    observation=observation,
                    action_history=history,
                )
                input_tokens += prediction.input_tokens
                output_tokens += prediction.output_tokens
            except (EvaluationExecutionError, ModelAPIError):
                prediction = UITarsPrediction(
                    raw_text="",
                    response={"status": "error", "error_code": "ui_tars_call_failed"},
                    action=None,
                    input_tokens=0,
                    output_tokens=0,
                    error_code="ui_tars_call_failed",
                )
                failure_category = "vision_model_error"
            response_records.append(
                {
                    "schema_version": UI_TARS_RESPONSE_SCHEMA_VERSION,
                    "run_id": run_id,
                    "step_index": step_index,
                    "call_index": step_index,
                    "producer": {
                        "provider": "ui-tars",
                        "model": config.vision.model,
                    },
                    "screenshot_artifact_id": screenshot_id,
                    "screenshot_sha256": screenshot_sha,
                    "response": prediction.response,
                }
            )
            action = prediction.action
            status = "model_error"
            action_name = "invalid_prediction"
            execution: dict[str, Any] = {"status": status}
            if action is not None:
                action_name = action.kind
                action_records.append(
                    {"step_index": step_index, "action": action.as_dict()}
                )
                if action.kind == "call_user":
                    failure_category = failure_category or "requires_user"
                    status = "blocked"
                elif action.kind == "finished":
                    finished = True
                    status = "completed"
                else:
                    try:
                        execution = await browser.execute(action, observation, config)
                        status = str(execution.get("status", "completed"))
                    except EvaluationExecutionError:
                        failure_category = failure_category or "action_error"
                        status = "error"
            elapsed_ms = max(0.0, (time.perf_counter() - step_started) * 1000.0)
            trace_records.append(
                action_event(
                    run_id=run_id,
                    step_index=step_index,
                    action=action_name,
                    elapsed_ms=elapsed_ms,
                    safety_violation=False,
                    status=status,
                )
            )
            history.append(
                {
                    "step_index": step_index,
                    "goal": plan_payload.get("goal"),
                    "action": action.as_dict() if action else None,
                    "execution": execution,
                }
            )
            if prediction.error_code or finished or action is None or action.kind == "call_user":
                break
        final_state = await browser.final_state()
    finally:
        await browser.close()

    if not response_records:
        raise EvaluationExecutionError("UI-TARS produced no auditable model call")
    workspace.write_jsonl("planner_result", planner_records)
    workspace.write_jsonl("ui_tars_response", response_records)
    workspace.write_jsonl("action_trace", trace_records)
    workspace.write_json(
        "ui_tars_actions",
        {
            "schema_version": "kt6.evaluation-ui-tars-actions.v1",
            "run_id": run_id,
            "coordinate_mode": config.coordinate_mode,
            "execute_actions": config.execute_actions,
            "actions": action_records,
        },
    )
    validation_passed = _deterministic_validation(task, final_state)
    success = (
        config.execute_actions
        and finished
        and validation_passed
        and failure_category is None
    )
    if not config.execute_actions:
        failure_category = "dry_run_only"
    elif not validation_passed and failure_category is None:
        failure_category = "validation_failed"
    elif not finished and failure_category is None:
        failure_category = "step_limit"
    workspace.write_json(
        "validation_result",
        validation_result(
            run_id=run_id,
            task_id=task.task_id,
            method=str(task.validation["method"]),
            passed=success,
        ),
    )
    duration_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
    step_count = len(response_records)
    metrics = {
        "first_target_hit": success if action_records else None,
        "misclick_count": 0,
        "retry_count": 0,
        "loop_count": 0,
        "timeout_count": timeout_count,
        "safety_violation_count": 0,
        "cv_calls": 0,
        "planner_model_calls": step_count,
        "vision_model_calls": step_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": 0,
    }
    record = build_final_run_record(
        normalized_suite,
        scheme_id="ui_tars",
        task_id=task.task_id,
        repetition=repetition,
        outcome="success" if success else "failure",
        started_at=started_at,
        duration_ms=duration_ms,
        step_count=step_count,
        implementation=implementation,
        planner={
            "provider": "deepseek",
            "model": config.planner.model,
            "adapter_prompt_version": UI_TARS_ADAPTER_PROMPT_VERSION,
        },
        environment=environment,
        metrics=metrics,
        validation_passed=success,
        validation_method=str(task.validation["method"]),
        failure_category=None if success else failure_category or "evaluation_failed",
        failure_reason="" if success else failure_category or "evaluation_failed",
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


def parse_ui_tars_action(response: str) -> UITarsAction:
    if not isinstance(response, str) or not response.strip() or len(response) > 64_000:
        raise EvaluationExecutionError("UI-TARS response must be bounded text")
    matches = _ACTION_LINE.findall(response)
    if len(matches) != 1:
        raise EvaluationExecutionError("UI-TARS response must contain exactly one Action line")
    expression = matches[0].strip()
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise EvaluationExecutionError("UI-TARS Action syntax is invalid") from exc
    call = tree.body
    if (
        not isinstance(call, ast.Call)
        or not isinstance(call.func, ast.Name)
        or call.func.id not in _ALLOWED_ACTIONS
        or call.args
        or any(keyword.arg is None for keyword in call.keywords)
    ):
        raise EvaluationExecutionError("UI-TARS Action is not allowed")
    kwargs: dict[str, Any] = {}
    for keyword in call.keywords:
        assert keyword.arg is not None
        if keyword.arg in kwargs:
            raise EvaluationExecutionError("UI-TARS Action repeats an argument")
        try:
            kwargs[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, SyntaxError, RecursionError) as exc:
            raise EvaluationExecutionError("UI-TARS Action arguments must be literals") from exc
    kind = call.func.id
    if kind in {"click", "left_double", "right_single"}:
        _exact_keys(kwargs, {"start_box"})
        return UITarsAction(kind=kind, start=_box_center(kwargs["start_box"]))
    if kind == "drag":
        _exact_keys(kwargs, {"start_box", "end_box"})
        return UITarsAction(
            kind=kind,
            start=_box_center(kwargs["start_box"]),
            end=_box_center(kwargs["end_box"]),
        )
    if kind == "type":
        _exact_keys(kwargs, {"content"})
        content = kwargs["content"]
        if not isinstance(content, str) or not content or len(content) > 4000:
            raise EvaluationExecutionError("UI-TARS type content is invalid")
        return UITarsAction(kind=kind, content=content)
    if kind == "hotkey":
        _exact_keys(kwargs, {"key"})
        key = kwargs["key"]
        if not isinstance(key, str) or key.strip().upper() not in _SAFE_HOTKEYS:
            raise EvaluationExecutionError("UI-TARS hotkey is not allowlisted")
        return UITarsAction(kind=kind, key=key.strip().upper())
    if kind == "scroll":
        if set(kwargs) not in ({"direction"}, {"start_box", "direction"}):
            raise EvaluationExecutionError("UI-TARS scroll arguments are invalid")
        direction = kwargs["direction"]
        if direction not in {"up", "down", "left", "right"}:
            raise EvaluationExecutionError("UI-TARS scroll direction is invalid")
        start = _box_center(kwargs["start_box"]) if "start_box" in kwargs else None
        return UITarsAction(kind=kind, start=start, direction=direction)
    if kind in {"wait", "finished", "call_user"}:
        _exact_keys(kwargs, set())
        return UITarsAction(kind=kind)
    raise EvaluationExecutionError("UI-TARS Action is unsupported")


def map_ui_tars_point(
    point: tuple[float, float] | None,
    *,
    width: int,
    height: int,
    mode: str,
) -> tuple[float, float]:
    if point is None or width < 1 or height < 1:
        raise EvaluationExecutionError("UI-TARS coordinate mapping input is invalid")
    x, y = point
    if mode == "scale_1000":
        x, y = x / 1000.0 * width, y / 1000.0 * height
    elif mode == "unit":
        x, y = x * width, y * height
    elif mode != "pixel":
        raise EvaluationExecutionError("coordinate_mode is unsupported")
    if not all(math.isfinite(value) for value in (x, y)) or not (
        0 <= x < width and 0 <= y < height
    ):
        raise EvaluationExecutionError("UI-TARS coordinate is outside the viewport")
    return x, y


def _ui_tars_prompt(
    task: ExecutionTask,
    goal: str,
    observation: BrowserObservation,
    action_history: Sequence[Mapping[str, Any]],
) -> str:
    return (
        "You are given a browser screenshot and must output the next GUI action.\n"
        "Output exactly two lines: Thought: <brief reason> and Action: <one action>.\n"
        "Coordinate space is 0..1000 for both axes.\n"
        "Allowed actions:\n"
        "click(start_box='(x,y)')\nleft_double(start_box='(x,y)')\n"
        "right_single(start_box='(x,y)')\ndrag(start_box='(x,y)', end_box='(x,y)')\n"
        "type(content='text')\nhotkey(key='ENTER')\n"
        "scroll(direction='up|down|left|right')\nwait()\nfinished()\ncall_user()\n"
        f"Task: {task.instruction}\nCurrent subgoal: {goal}\n"
        f"Viewport: {observation.width}x{observation.height}\n"
        "Previous actions: "
        + json.dumps(list(action_history[-10:]), ensure_ascii=False, allow_nan=False)
    )


def _deterministic_validation(task: ExecutionTask, final_state: Mapping[str, Any]) -> bool:
    assertions = task.validation.get("assertions")
    if not isinstance(assertions, list) or not 1 <= len(assertions) <= 50:
        raise EvaluationExecutionError(
            "validation.assertions must contain deterministic page-state checks"
        )
    url = str(final_state.get("url", ""))
    title = str(final_state.get("title", ""))
    body = str(final_state.get("body_text", ""))
    for assertion in assertions:
        if not isinstance(assertion, Mapping):
            raise EvaluationExecutionError("validation assertion must be an object")
        kind = assertion.get("kind")
        value = assertion.get("value")
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise EvaluationExecutionError("validation assertion value is invalid")
        if kind == "url_equals":
            passed = url == value
        elif kind == "url_contains":
            passed = value in url
        elif kind == "title_contains":
            passed = value.casefold() in title.casefold()
        elif kind == "text_contains":
            passed = value.casefold() in body.casefold()
        else:
            raise EvaluationExecutionError("validation assertion kind is unsupported")
        if not passed:
            return False
    return True


def _box_center(value: Any) -> tuple[float, float]:
    if isinstance(value, str):
        values = re.findall(r"-?(?:\d+(?:\.\d*)?|\.\d+)", value)
        try:
            numbers = [float(item) for item in values]
        except ValueError as exc:
            raise EvaluationExecutionError("UI-TARS coordinate is invalid") from exc
    elif isinstance(value, (list, tuple)):
        numbers = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise EvaluationExecutionError("UI-TARS coordinate is invalid")
            numbers.append(float(item))
    else:
        raise EvaluationExecutionError("UI-TARS coordinate is invalid")
    if len(numbers) == 2:
        point = (numbers[0], numbers[1])
    elif len(numbers) == 4:
        point = ((numbers[0] + numbers[2]) / 2, (numbers[1] + numbers[3]) / 2)
    else:
        raise EvaluationExecutionError("UI-TARS coordinate requires two or four numbers")
    if not all(math.isfinite(item) for item in point):
        raise EvaluationExecutionError("UI-TARS coordinate is non-finite")
    return point


def _exact_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise EvaluationExecutionError("UI-TARS Action arguments are invalid")


def _playwright_key(key: str) -> str:
    aliases = {"CTRL": "Control", "CONTROL": "Control"}
    return "+".join(aliases.get(part, part.title()) for part in key.split("+"))


def _scroll_delta(direction: str) -> tuple[int, int]:
    return {
        "up": (0, -600),
        "down": (0, 600),
        "left": (-600, 0),
        "right": (600, 0),
    }[direction]


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
    "BrowserObservation",
    "BrowserOperator",
    "DeepSeekStepPlanner",
    "ModelEndpointConfig",
    "PlannerDecision",
    "PlannerModel",
    "PlaywrightBrowserOperator",
    "UITarsAPIModel",
    "UITarsAction",
    "UITarsEvaluationConfig",
    "UITarsModel",
    "UITarsPrediction",
    "map_ui_tars_point",
    "parse_ui_tars_action",
    "run_ui_tars_evaluation",
]
